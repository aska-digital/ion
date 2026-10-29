"""The "Org health" dashboard issue: one issue in the tracker repo that is regenerated in place on every run.

It lists what needs a maintainer (awaiting approval, with an approve box and a hold box per proposal), what is queued,
what is held, what is blocked by plan limits or boundary rules, what stopped early (budget, per-run caps, circuit
breaker), errors, and what was done recently. Each proposal links to its own tracking issue.

The checkboxes are an approval channel, so the actor is verified like any other:

- a box carries the plan hash it was rendered for, and an approval counts only for that exact hash;
- the issue's edit history (`userContentEdits`) says who edited the body. Ion remembers the hash and time of its own
  last write; every edit after that must have been made by a maintainer of the repos the proposal touches, or the ticks
  are recorded as ignored. If the body changed but the edits cannot be attributed, that fails closed.
- Ion's next write replaces the whole body, so ticks never linger, and a tick is applied to the proposal at most once.
"""
from __future__ import annotations

import hashlib
import re

from .. import proposals
from ..analyze import latest_analysis
from ..approve import core, labels
from ..util import days_since, iso, parse_ts
from ..runhistory import freshness_line

BOX_RE = re.compile(r"^\s*- \[([ xX])\] <!-- ion:box (approve|hold) (P-\d{4,}) ([0-9a-f]{12}) -->", re.M)
EDITS_QUERY = """query($owner:String!, $repo:String!, $n:Int!) { repository(owner:$owner, name:$repo) { issue(number:$n) {
  userContentEdits(first:30) { nodes { createdAt editor { login } } } } } }"""
LIVE = ("awaiting-approval", "approved", "held", "executing")


def _link(ctx, p) -> str:
    n = p.get("issue_number")
    return f"[{p['id']}](https://github.com/{ctx.cfg.org}/{ctx.cfg['tracker_repo']}/issues/{n})" if n else p["id"]


def _line(ctx, p) -> str:
    return f"{_link(ctx, p)} {p['title']} · `{p['kind']}` · score {p['score']:.2f}"


def render_body(ctx, results: dict) -> tuple[str, dict]:
    db, cfg = ctx.db, ctx.cfg
    rows = [proposals.view(r) for r in db.q("select * from proposals order by num")]
    by = lambda *st: [p for p in rows if p["status"] in st]
    a = latest_analysis(db)
    last = db.one("select * from runs order by id desc limit 1")
    L = ["<!-- ion:dashboard -->", f"# Org health: {cfg['org']}", ""]
    meta = f"Updated {iso()}"
    if last:
        meta += f" · last collect: {last['repos']} repos, {last['cost']} GraphQL points" + (" (partial)" if last["partial"] else "")
    if cfg.is_paused():
        meta += " · **kill switch is ON**"
    L += [meta, "", freshness_line(ctx), ""]
    boxes: list[tuple[str, str]] = []

    L += ["## Awaiting approval", ""]
    waiting = by("awaiting-approval")
    for p in waiting:
        h = p["plan_hash"][:12]
        L.append(f"- [ ] <!-- ion:box approve {p['id']} {h} --> **approve** {_line(ctx, p)} · plan `{h}`")
        L.append(f"  - [ ] <!-- ion:box hold {p['id']} {h} --> hold")
        boxes += [("approve", p["id"]), ("hold", p["id"])]
    if not waiting:
        L.append("Nothing is waiting.")
    L += ["", "Ticking a box counts as a decision only if a maintainer made the edit and the plan hash is unchanged. "
          "The labels `ion:approved`, `ion:rejected` and `ion:hold` on the proposal issue work as well.", ""]

    L += ["## Approved (queued)", ""] + ([f"- {_line(ctx, p)} · `{p['status']}`" for p in by("approved", "executing")] or ["None."])
    L += ["", "## On hold", ""] + ([f"- {_line(ctx, p)} (remove the `ion:hold` label on its issue to release it)" for p in by("held")] or ["None."])

    L += ["", "## Blocked by plan", ""]
    blocked = []
    if a:
        blocked += [f"- {n}: {f['class']} needs a paid plan" for n, v in sorted(a["policy"].items()) for f in v if f["status"] == "blocked_by_plan"]
        blocked += [f"- boundary: {v['from']} depends on {v['to']} ({v['reason']})" for v in a.get("boundaries", [])]
    L += blocked or ["None."]

    L += ["", "## Rate-limited or stopped early", ""]
    stopped = []
    if last and last["partial"]:
        stopped.append(f"- collection stopped at its GraphQL budget after {last['repos']} repos")
    caps = {t: n for t, n in ctx.writes.counts.items() if n >= ctx.writes.caps.get(t, 10 ** 9)}
    stopped += [f"- per-run cap reached for `{t}` ({n}); the rest resumes next run" for t, n in sorted(caps.items())]
    trip = db.kv_get("last_breaker_trip")
    if trip and (days_since(trip["ts"]) or 99) < 2:
        stopped.append(f"- **circuit breaker tripped** {trip['ts']}: {trip['reason']}")
    if results.get("execute", {}).get("breaker"):
        stopped.append(f"- circuit breaker: {results['execute']['breaker']}")
    L += stopped or ["None."]

    L += ["", "## Errors", ""]
    errs = list(results.get("errors", [])) + [f"{p['id']} failed" for p in by("failed")]
    L += [f"- {e}" for e in errs[:15]] or ["None."]

    if a:
        stale = [(n, s) for n, s in sorted(a.get("lifecycle", {}).items()) if s in ("stale", "archive-proposed")]
        L += ["", "## Lifecycle", ""] + ([f"- {n}: `{s}`" for n, s in stale] or ["All repos are active."])

    L += ["", "## Recently done", ""]
    done = sorted(by("done"), key=lambda p: p["updated"], reverse=True)[:10]
    L += [f"- {_line(ctx, p)} ({p['updated'][:10]})" for p in done] or ["None."]
    L += ["", f"<sub>Regenerated in place by ion. Please do not comment here.</sub>"]
    return "\n".join(L) + "\n", {"boxes": boxes}


def _written(db) -> dict | None:
    return db.kv_get("dashboard_written")


def publish(ctx, results: dict) -> str:
    """Write the dashboard issue (create it on first use). Returns a short status string."""
    if not ctx.cfg["report"].get("org_health_issue"):
        return "disabled (report.org_health_issue is false)"
    body, _ = render_body(ctx, results)
    sha = hashlib.sha256(body.encode()).hexdigest()
    # the timestamp inside the body would make every body unique: compare without it
    stable = hashlib.sha256(re.sub(r"Updated [0-9T:Z-]+", "", body).encode()).hexdigest()
    prev = _written(ctx.db) or {}
    if prev.get("stable") == stable and not ctx.db.kv_get("dashboard_dirty"):
        return f"unchanged (#{prev.get('number')})"
    n = ctx.db.kv_get("dashboard_issue")
    title = ctx.cfg["report"].get("dashboard_title", "Org health")
    if n:
        ctx.writes.tracker("edit_issue_body", number=int(n), title=title, body=body)
    else:
        issue = ctx.writes.tracker("open_issue", title=title, body=body, labels=["ion:dashboard"])
        n = int(issue["number"])
        ctx.db.kv_set("dashboard_issue", n)
    ctx.db.kv_set("dashboard_written", {"sha": sha, "stable": stable, "ts": iso(), "number": n})
    ctx.db.kv_set("dashboard_dirty", False)
    return f"written (#{n})"


def sync(ctx) -> dict:
    """Record ticked boxes as approvals, after verifying who edited the issue. Returns {recorded, ignored}."""
    res = {"recorded": 0, "ignored": []}
    n = ctx.db.kv_get("dashboard_issue")
    w = _written(ctx.db)
    if not n or not w:
        return res
    issue = ctx.gh.rest("GET", f"/repos/{ctx.cfg.org}/{ctx.cfg['tracker_repo']}/issues/{n}") or {}
    body = issue.get("body") or ""
    if hashlib.sha256(body.encode()).hexdigest() == w["sha"]:
        return res                                           # nobody has edited it since we wrote it
    ctx.db.kv_set("dashboard_dirty", True)                   # a human edited it: our next write must reset the boxes
    ticked = [(m.group(2), m.group(3), m.group(4)) for m in BOX_RE.finditer(body) if m.group(1) != " "]
    if not ticked:
        return res
    data = ctx.gh.graphql(EDITS_QUERY, {"owner": ctx.cfg.org, "repo": ctx.cfg["tracker_repo"], "n": int(n)})
    nodes = ((((data.get("data") or {}).get("repository") or {}).get("issue") or {}).get("userContentEdits") or {}).get("nodes") or []
    since = parse_ts(w["ts"])
    edits = [(parse_ts(e["createdAt"]), ((e.get("editor") or {}).get("login") or "").lower()) for e in nodes]
    edits = sorted(e for e in edits if e[0] and since and e[0] > since)
    body_sha = hashlib.sha256(body.encode()).hexdigest()[:12]
    for action, pid, short in ticked:
        p = proposals.get(ctx.db, pid)
        if not p or p["status"] not in ("awaiting-approval", "approved", "held"):
            continue
        maint = core.maintainers_for(ctx, p)
        editors = {login for _, login in edits}
        ok, reason = True, ""
        if not edits:
            ok, reason = False, "the body changed but the edit history shows no edits to attribute it to"
        elif not editors <= maint:
            who = ", ".join(sorted(editors - maint))
            ok, reason = False, f"the issue was edited by {who}, who is not a maintainer of these repos"
        shown = [h for _, h in p["state"].get("hash_log", []) if h.startswith(short)]
        bound = shown[-1] if shown else None
        if ok and bound is None:
            ok, reason = False, f"plan hash {short} was never displayed for {pid}"
        actor = edits[-1][1] if edits else ""
        ts = iso(edits[-1][0]) if edits else iso()
        if core.record(ctx.db, pid, "dashboard", actor, action, ts, f"dash:{body_sha}:{pid}:{action}", ok, reason, bound):
            res["recorded"] += 1
            if not ok:
                res["ignored"].append((pid, actor or "unknown", reason))
            elif bound == p["plan_hash"]:
                try:
                    labels.mirror_label(ctx, p, action)      # so the decision is also on the proposal issue
                except Exception:  # noqa: BLE001
                    pass
    return res
