"""github_label approval source: verify WHO applied the label via the issue timeline."""
from __future__ import annotations

from .. import tracker
from . import core

LABEL_ACTIONS = {"mimar:approved": "approve", "mimar:rejected": "reject", "mimar:hold": "hold"}


def replay(events: list[dict]) -> dict[str, dict]:
    """label -> {present, labeled: [event dicts]} from a timeline."""
    state: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: e.get("created_at") or ""):
        if e.get("event") not in ("labeled", "unlabeled"):
            continue
        name = (e.get("label") or {}).get("name")
        if name not in LABEL_ACTIONS:
            continue
        st = state.setdefault(name, {"present": False, "labeled": []})
        if e["event"] == "labeled":
            st["present"] = True
            st["labeled"].append(e)
        else:
            st["present"] = False
    return state


def sync_labels(ctx, p: dict) -> dict:
    """Record label approvals for one proposal. Returns counters."""
    n = p.get("issue_number")
    out = {"recorded": 0, "ignored": []}
    if not n:
        return out
    events = ctx.gh.paginate(f"{tracker.repo_path(ctx)}/issues/{n}/timeline")
    maint = core.maintainers_for(ctx, p)
    mirrored = {r["event_id"] for r in ctx.db.q("select event_id from mirrored_labels where proposal=?", (p["id"],))}
    for label, st in replay(events).items():
        action = LABEL_ACTIONS[label]
        for e in st["labeled"]:
            eid = str(e.get("id"))
            actor = ((e.get("actor") or {}).get("login") or "").lower()
            if eid in mirrored:
                continue                                  # applied by mimar itself, counted via its bus source
            ok = actor in maint
            reason = "" if ok else f"label added by {actor or 'unknown'}, who is not a maintainer"
            if core.record(ctx.db, p["id"], "github_label", actor, action, e.get("created_at") or "", eid, ok, reason):
                out["recorded"] += 1
                if not ok:
                    out["ignored"].append((label, actor))
        if not st["present"]:
            ids = [str(e.get("id")) for e in st["labeled"]]
            for i in ids:
                ctx.db.x("update approvals set valid=0, reason='label removed' where proposal=? and ref=? and source='github_label'",
                         (p["id"], i))
    return out


def mirror_label(ctx, p: dict, action: str) -> None:
    """Apply the label for a bus decision so the record lives on GitHub; remember the event id as ours."""
    label = {v: k for k, v in LABEL_ACTIONS.items()}[action]
    n = p.get("issue_number")
    if not n or ctx.cfg.is_paused():
        return
    ctx.gh.rest("POST", f"{tracker.repo_path(ctx)}/issues/{n}/labels", {"labels": [label]})
    events = ctx.gh.paginate(f"{tracker.repo_path(ctx)}/issues/{n}/timeline")
    mine = [e for e in events if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label]
    if mine:
        last = max(mine, key=lambda e: e.get("created_at") or "")
        ctx.db.x("insert or ignore into mirrored_labels(proposal,label,event_id) values(?,?,?)", (p["id"], label, str(last.get("id"))))
