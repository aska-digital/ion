"""Batched GraphQL collection of org repo metadata."""
from __future__ import annotations

import logging

from ..ghclient import GH

log = logging.getLogger("mimar")

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
        mimarYml: object(expression: "HEAD:.github/mimar.yml") { ... on Blob { text } }
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
        "override_text": (node.get("mimarYml") or {}).get("text"),
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


def collect_org(gh: GH, org: str, budget: int, commits: int = 60, page_size: int = 25) -> Collected:
    """Paginate the org. Stops cleanly when the next page might exceed the budget."""
    out = Collected()
    after = None
    last_cost = 1
    while True:
        if out.cost + last_cost > budget:
            out.partial = True
            out.warnings.append(f"budget of {budget} points reached after {len(out.repos)} repos")
            break
        body = gh.graphql(QUERY, {"org": org, "after": after, "n": commits, "page": page_size})
        for e in body.get("errors") or []:
            out.warnings.append(str(e.get("message", e))[:200])
        rl = (body.get("data") or {}).get("rateLimit") or {}
        last_cost = max(1, int(rl.get("cost", 1)))
        out.cost += last_cost
        out.remaining = rl.get("remaining", out.remaining)
        conn = ((body["data"] or {}).get("organization") or {}).get("repositories") or {}
        out.repos.extend(normalise(n) for n in conn.get("nodes") or [] if n)
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
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
