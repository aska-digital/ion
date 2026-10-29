"""Batched GraphQL collection of org repo metadata."""
from __future__ import annotations

import logging
import time

from ..ghclient import GH, GHError

log = logging.getLogger("ion")

QUERY = """
query($org: String!, $after: String, $n: Int!, $page: Int!) {
  rateLimit { cost remaining resetAt }
  organization(login: $org) {
    repositories(first: $page, after: $after, orderBy: {field: PUSHED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name url description isPrivate isFork isArchived isEmpty diskUsage pushedAt createdAt
        hasVulnerabilityAlertsEnabled
        licenseInfo { spdxId }
        defaultBranchRef {
          name
          target { ... on Commit { oid history(first: $n) { nodes {
            oid committedDate changedFilesIfAvailable author { name email user { login } } } } } }
        }
        languages(first: 6, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name } } }
        repositoryTopics(first: 10) { nodes { topic { name } } }
        issues(states: OPEN) { totalCount }
        pullRequests(states: OPEN) { totalCount }
        oldestPR: pullRequests(states: OPEN, first: 1, orderBy: {field: CREATED_AT, direction: ASC}) {
          nodes { number createdAt } }
        vulnerabilityAlerts(first: 1, states: OPEN) { totalCount }
        branchProtectionRules(first: 10) { nodes { pattern requiresApprovingReviews requiredApprovingReviewCount
          allowsForcePushes allowsDeletions isAdminEnforced } }
        ionYml: object(expression: "HEAD:.github/ion.yml") { ... on Blob { text } }
        legacyYml: object(expression: "HEAD:.github/mimar.yml") { ... on Blob { text } }
        parent { nameWithOwner pushedAt }
      }
    }
  }
}
"""


def _commits(node: dict) -> list[dict]:
    ref = node.get("defaultBranchRef") or {}
    hist = ((ref.get("target") or {}).get("history") or {}).get("nodes") or []
    out = []
    for c in hist:
        a = c.get("author") or {}
        out.append({"oid": c["oid"], "date": c["committedDate"], "files": c.get("changedFilesIfAvailable"),
                    "login": (a.get("user") or {}).get("login"), "name": a.get("name"), "email": a.get("email")})
    return out


def normalise(node: dict) -> dict:
    ref = node.get("defaultBranchRef") or {}
    langs = {e["node"]["name"]: e["size"] for e in ((node.get("languages") or {}).get("edges") or [])}
    oldest = ((node.get("oldestPR") or {}).get("nodes") or [None])[0]
    return {
        "name": node["name"], "url": node.get("url"), "description": node.get("description"),
        "private": bool(node["isPrivate"]), "fork": bool(node["isFork"]), "archived": bool(node["isArchived"]),
        "empty": bool(node.get("isEmpty")), "size": node.get("diskUsage") or 0, "pushed_at": node.get("pushedAt"),
        "created_at": node.get("createdAt"), "default_branch": ref.get("name"),
        "head_oid": (ref.get("target") or {}).get("oid"), "languages": langs,
        "topics": [t["topic"]["name"] for t in ((node.get("repositoryTopics") or {}).get("nodes") or [])],
        "license": (node.get("licenseInfo") or {}).get("spdxId"),
        "open_issues": (node.get("issues") or {}).get("totalCount", 0),
        "open_prs": (node.get("pullRequests") or {}).get("totalCount", 0),
        "oldest_pr_at": oldest["createdAt"] if oldest else None,
        "vuln_alerts": ((node.get("vulnerabilityAlerts") or {}).get("totalCount")),
        "vuln_enabled": node.get("hasVulnerabilityAlertsEnabled"),
        "protection": (node.get("branchProtectionRules") or {}).get("nodes") or [],
        "override_text": (node.get("ionYml") or node.get("legacyYml") or {}).get("text"),
        "parent": node.get("parent"),
        "commits": _commits(node),
    }


class Collected:
    def __init__(self):
        self.repos: list[dict] = []
        self.cost = 0
        self.remaining: int | None = None
        self.partial = False
        self.warnings: list[str] = []


def collect_org(gh: GH, org: str, budget: int, commits: int = 60, page_size: int = 25,
                *, after=None, on_page=None, sleep=time.sleep) -> Collected:
    """Collect a bounded portion of a pass, retaining its last accepted cursor."""
    out = Collected()
    out.after = after
    last_cost = 1
    while True:
        if out.cost + last_cost > budget:
            out.partial = True
            out.warnings.append(f"budget of {budget} points reached after {len(out.repos)} repos")
            break
        body = None
        for attempt in range(3):
            try:
                candidate = gh.graphql(QUERY, {"org": org, "after": after, "n": commits, "page": page_size})
                errors = candidate.get("errors") or []
                if any(e.get("type") == "RATE_LIMITED" for e in errors):
                    raise GHError("GraphQL rate limited", status=429)
                body = candidate
                break
            except GHError as e:
                if attempt == 2:
                    out.partial = True
                    out.warnings.append(f"collection paused after retries: {e}"[:200])
                else:
                    sleep(0.5 * 2 ** attempt)
        if body is None:
            break
        errors = body.get("errors") or []
        out.warnings.extend(str(e.get("message", e))[:200] for e in errors)
        rl = (body.get("data") or {}).get("rateLimit") or {}
        last_cost = max(1, int(rl.get("cost", 1)))
        out.cost += last_cost
        out.remaining = rl.get("remaining", out.remaining)
        conn = (((body.get("data") or {}).get("organization") or {}).get("repositories"))
        info = (conn or {}).get("pageInfo")
        if errors or not conn or not isinstance(info, dict) or "hasNextPage" not in info:
            out.partial = True
            out.warnings.append("incomplete GraphQL page; cursor retained for next run")
            break
        next_cursor = info.get("endCursor")
        if info["hasNextPage"] and (not next_cursor or next_cursor == after):
            out.partial = True
            out.warnings.append("missing or repeated pagination cursor; page will be retried")
            break
        page = [normalise(n) for n in conn.get("nodes") or [] if n]
        # Persist before moving the cursor: an interrupted next request cannot lose a page.
        if on_page:
            on_page(page, next_cursor if info["hasNextPage"] else None)
        out.repos.extend(page)
        if not info["hasNextPage"]:
            out.after = None
            break
        after = out.after = next_cursor
        if out.remaining is not None and out.remaining < last_cost:
            out.partial = True
            out.warnings.append(f"rate limit exhausted; reset at {rl.get('resetAt', 'unknown')}")
            break
    return out


def fetch_security_extras(gh: GH, org: str, snap: dict) -> dict | None:
    """Secret scanning status is not in GraphQL; one REST read per public non-fork repo."""
    try:
        data = gh.rest("GET", f"/repos/{org}/{snap['name']}")
    except Exception as e:
        log.warning("security extras for %s unreadable: %s", snap["name"], e)
        return None
    sa = (data or {}).get("security_and_analysis")
    if sa is None:
        return None
    return {"secret_scanning": (sa.get("secret_scanning") or {}).get("status")}
