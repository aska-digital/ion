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


def own_login(ctx) -> str | None:
    """The GitHub login Mimar writes as (cached). None if it cannot be read."""
    if ctx._own_login is None:
        try:
            ctx._own_login = ((ctx.gh.rest("GET", "/user") or {}).get("login") or "").lower()
        except Exception:  # noqa: BLE001
            ctx._own_login = ""
    return ctx._own_login or None


def authorised(ctx, actor: str, maint: set[str]) -> tuple[bool, str]:
    """May this GitHub actor's decision label count? Maintainers only.

    Mimar's own label events never reach this check: it puts decision labels on only to mirror a bus decision, and it
    remembers the event id (`mirrored_labels`) so those are skipped. A login is not used to recognise Mimar, because in
    phase 1 Mimar runs on a maintainer's own token and the two logins coincide."""
    if actor in maint:
        return True, ""
    return False, f"label added by {actor or 'unknown'}, who is not a maintainer"


def sync_labels(ctx, p: dict) -> dict:
    """Record label approvals for one proposal. Returns counters.

    A decision label put on by anyone who is not a maintainer is recorded as ignored and taken off again (one API
    call, no comment), so the issue never shows an approval that does not count."""
    n = p.get("issue_number")
    out = {"recorded": 0, "ignored": [], "removed": []}
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
            ok, reason = authorised(ctx, actor, maint)
            if core.record(ctx.db, p["id"], "github_label", actor, action, e.get("created_at") or "", eid, ok, reason):
                out["recorded"] += 1
                if not ok:
                    out["ignored"].append((label, actor))
        if st["present"] and st["labeled"]:
            last = st["labeled"][-1]
            lactor = ((last.get("actor") or {}).get("login") or "").lower()
            if str(last.get("id")) not in mirrored and not authorised(ctx, lactor, maint)[0]:
                try:
                    ctx.writes.tracker("remove_label", number=int(n), label=label)
                    out["removed"].append((label, lactor))
                except Exception as e:  # noqa: BLE001
                    out.setdefault("errors", []).append(str(e)[:120])
        if not st["present"]:
            ids = [str(e.get("id")) for e in st["labeled"]]
            for i in ids:
                ctx.db.x("update approvals set valid=0, reason='label removed' where proposal=? and ref=? and source='github_label'"
                         " and valid=1 and reason=''", (p["id"], i))
    return out


def clear_decision_labels(ctx, p: dict, labels: tuple[str, ...] = ("mimar:approved", "mimar:hold")) -> list[str]:
    """Take decision labels off the issue (after a re-plan or an expiry). One call per label that is present."""
    n = p.get("issue_number")
    if not n or ctx.cfg.is_paused():
        return []
    present = replay(ctx.gh.paginate(f"{tracker.repo_path(ctx)}/issues/{n}/timeline"))
    done = []
    for label in labels:
        if present.get(label, {}).get("present"):
            ctx.writes.tracker("remove_label", number=int(n), label=label)
            done.append(label)
    return done


def mirror_label(ctx, p: dict, action: str) -> None:
    """Apply the label for a bus decision so the record lives on GitHub; remember the event id as ours."""
    label = {v: k for k, v in LABEL_ACTIONS.items()}[action]
    n = p.get("issue_number")
    if not n or ctx.cfg.is_paused():
        return
    ctx.writes.tracker("add_label", number=int(n), labels=[label])
    events = ctx.gh.paginate(f"{tracker.repo_path(ctx)}/issues/{n}/timeline")
    mine = [e for e in events if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label]
    if mine:
        last = max(mine, key=lambda e: e.get("created_at") or "")
        ctx.db.x("insert or ignore into mirrored_labels(proposal,label,event_id) values(?,?,?)", (p["id"], label, str(last.get("id"))))
