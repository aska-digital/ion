"""Campaigns: a structural proposal owns an ordered list of changesets across repos.

A merge is not one change, it is three: a PR in the target, then a pointer PR in the source, then (after its own
approval) an archive. Writing the order down as data does three things. A step cannot run before what it depends on has
landed (`require`). A crashed run resumes trivially, because each changeset's state comes from what actually exists
(the PR numbers recorded on the proposal and the PR's real state on GitHub). And the tracker issue can show the whole
campaign as an ordered checklist with live PR states, each dependent PR carrying a `Depends-On:` line.
"""
from __future__ import annotations

from .util import Waiting

TERMINAL = ("merged", "done")


def build(p: dict) -> list[dict]:
    """The ordered changesets of a proposal: {id, repo, kind: pr|repo|archive, title, depends_on, pr_key}."""
    s, k = p["subject"], p["kind"]
    if k == "merge":
        A, B = s["source"], s["target"]
        return [
            {"id": "import", "repo": B, "kind": "pr", "pr_key": "pr_number", "depends_on": [],
             "title": f"import {A} under {s['prefix']}/ with history"},
            {"id": "pointer", "repo": A, "kind": "pr", "pr_key": "pointer_pr", "depends_on": ["import"],
             "title": f"point {A}'s README at {B}"},
            {"id": "archive", "repo": A, "kind": "archive", "depends_on": ["pointer"],
             "title": f"archive {A} (a separate proposal, approved on its own)"}]
    if k == "split":
        A, N = s["source"], s["new_repo"]
        return [
            {"id": "create", "repo": N, "kind": "repo", "depends_on": [], "ledger": "create.verify",
             "title": f"create {N} with the history of {', '.join(s['dirs'])}, verified"},
            {"id": "removal", "repo": A, "kind": "pr", "pr_key": "removal_pr", "depends_on": ["create"],
             "title": f"remove {', '.join(s['dirs'])} from {A}"}]
    if k == "move_code":
        S, T = s["source"], s["target"]
        return [
            {"id": "add", "repo": T, "kind": "pr", "pr_key": "pr_number", "depends_on": [],
             "title": f"add {s.get('dest_prefix') or s['dir']}/ to {T} with history"},
            {"id": "remove", "repo": S, "kind": "pr", "pr_key": "removal_pr", "depends_on": ["add"],
             "title": f"remove {s['dir']} from {S}"}]
    if k == "autofix":
        return [{"id": r, "repo": r, "kind": "pr", "pr_key": f"pr:{r}", "depends_on": [],
                 "title": f"{s['fixer']} in {r}"} for r in s["repos"]]
    return []


def pr_number(p: dict, cs: dict) -> int | None:
    key = cs.get("pr_key")
    if not key:
        return None
    if key.startswith("pr:"):
        return (p["state"].get("prs") or {}).get(key[3:])
    return p["state"].get(key)


def _pr_state(ctx, repo: str, n: int, cache: dict) -> str:
    """open | merged | closed. Merged is terminal, so it is cached on the proposal and never asked again."""
    k = f"{repo}#{n}"
    if cache.get(k) == "merged":
        return "merged"
    try:
        pr = ctx.gh.rest("GET", f"/repos/{ctx.cfg.org}/{repo}/pulls/{n}") or {}
    except Exception:  # noqa: BLE001
        return cache.get(k, "open")
    st = "merged" if pr.get("merged") else ("closed" if pr.get("state") == "closed" else "open")
    cache[k] = st
    return st


def states(ctx, p: dict, query: bool = True) -> list[dict]:
    """Changesets with a live `state`: planned, blocked (a dependency has not landed), open, merged, closed, done or
    awaiting-approval (the follow-up archive proposal)."""
    cache = dict(p["state"].get("pr_states") or {})
    out: list[dict] = []
    by_id: dict[str, dict] = {}
    for cs in build(p):
        cs = dict(cs)
        # An unknown dependency id counts as not landed, here and in require():
        # the gate fails closed and the dashboard says the same thing (issue #14).
        deps_ok = all(d in by_id and by_id[d]["state"] in TERMINAL for d in cs["depends_on"])
        n = pr_number(p, cs)
        if cs["kind"] == "pr":
            if n:
                cs["number"] = n
                cs["state"] = _pr_state(ctx, cs["repo"], n, cache) if query else cache.get(f"{cs['repo']}#{n}", "open")
            else:
                cs["state"] = "planned" if deps_ok else "blocked"
        elif cs["kind"] == "repo":
            cs["state"] = "done" if ctx.db.ledger_done(p["id"], cs["ledger"]) else ("planned" if deps_ok else "blocked")
        else:  # archive: tracked through the follow-up proposal
            child = ctx.db.one("select status from proposals where parent=? and kind='archive' order by num desc", (p["id"],))
            cs["state"] = ("done" if child["status"] == "done" else "awaiting-approval") if child else ("planned" if deps_ok else "blocked")
        by_id[cs["id"]] = cs
        out.append(cs)
    if query and cache != (p["state"].get("pr_states") or {}) and p.get("id"):
        ctx.db.set_state(p["id"], pr_states=cache)
    return out


def require(ctx, p: dict, cs_id: str) -> str:
    """Raise Waiting unless every changeset that `cs_id` depends on has landed (merged or done)."""
    cs = {c["id"]: c for c in states(ctx, p)}
    if cs_id not in cs:
        return "no ordering constraints"
    waiting = [d for d in cs[cs_id]["depends_on"] if d not in cs or cs[d]["state"] not in TERMINAL]
    if waiting:
        raise Waiting(f"{cs_id} must wait for {', '.join(waiting)} to land first")
    return "dependencies landed: " + (", ".join(cs[cs_id]["depends_on"]) or "none")


def depends_on_line(ctx, p: dict, cs_id: str) -> str:
    """`Depends-On: org/repo#N` lines for the PRs a changeset depends on (for the dependent PR's body)."""
    cs = {c["id"]: c for c in build(p)}
    lines = []
    for d in cs.get(cs_id, {}).get("depends_on", []):
        n = pr_number(p, cs[d])
        if n:
            lines.append(f"Depends-On: {ctx.cfg.org}/{cs[d]['repo']}#{n}")
    return "\n".join(lines)


def render(ctx, p: dict) -> list[str]:
    sts = states(ctx, p)
    if len(sts) < 2 and p["kind"] != "autofix":
        return []
    lines = []
    for i, cs in enumerate(sts, 1):
        box = "x" if cs["state"] in TERMINAL else " "
        ref = f" [{ctx.cfg.org}/{cs['repo']}#{cs['number']}]" if cs.get("number") else f" ({ctx.cfg.org}/{cs['repo']})"
        dep = f" · after {', '.join(cs['depends_on'])}" if cs["depends_on"] else ""
        lines.append(f"{i}. [{box}] **{cs['id']}**: {cs['title']}{ref} · `{cs['state']}`{dep}")
    return lines
