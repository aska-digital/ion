"""Approval state machine: which recorded approvals count, and what that means for a proposal."""
from __future__ import annotations

import datetime as dt

from ..config import parse_repo_override
from ..collect.snapshot import latest_snapshots
from .. import proposals
from ..proposals import involved_repos
from ..util import iso, now, parse_ts


def maintainers_for(ctx, p: dict) -> set[str]:
    """An actor must be a maintainer of every repo the proposal touches."""
    snaps, _ = latest_snapshots(ctx.db)
    sets = []
    for r in involved_repos(p) or [None]:
        ov = parse_repo_override((snaps.get(r) or {}).get("override_text"))[0] if r else {}
        sets.append({m.lower() for m in ctx.cfg.maintainers_for(ov)})
    return set.intersection(*sets) if sets else set()


def quorum_for(cfg, kind: str) -> int:
    return int(cfg["quorum_by_kind"].get(kind, cfg["quorum"]))


def record(db, pid: str, source: str, actor: str, action: str, event_ts: str, ref: str, valid: bool, reason: str = "",
           plan_hash: str | None = None) -> bool:
    """Idempotent insert. Returns True if new. The approval is bound to the plan hash that the issue displayed just
    before the event (or to `plan_hash` when the source states one itself)."""
    if plan_hash is None:
        p = proposals.get(db, pid)
        plan_hash = proposals.hash_at(p, event_ts) if p else None
    cur = db.x("insert or ignore into approvals(proposal,source,actor,action,event_ts,ref,valid,reason,recorded,plan_hash)"
               " values(?,?,?,?,?,?,?,?,?,?)", (pid, source, actor, action, event_ts, ref, int(valid), reason, iso(), plan_hash))
    return cur.rowcount > 0


def evaluate(ctx, p: dict, ignore_ttl: bool = False, at: dt.datetime | None = None) -> dict:
    """-> {state: approved|rejected|held|expired|awaiting, approvers: [...], reason}"""
    cfg, db = ctx.cfg, ctx.db
    epoch = parse_ts(p["epoch"])
    maint = maintainers_for(ctx, p)
    rows = db.q("select * from approvals where proposal=? and valid=1 order by event_ts, id", (p["id"],))
    latest: dict[tuple[str, str], dict] = {}
    stale = 0
    for r in rows:
        t = parse_ts(r["event_ts"])
        if t is None or (epoch and t < epoch):
            continue
        if r["plan_hash"] != p["plan_hash"]:
            stale += 1                        # given for a different plan than the one now proposed
            continue
        latest[(r["source"], r["actor"])] = dict(r)
    actions = {k: v for k, v in latest.items()}
    if any(v["action"] == "reject" for v in actions.values()):
        return {"state": "rejected", "approvers": [], "reason": "rejected by a maintainer", "stale": stale}
    if any(v["action"] == "hold" for v in actions.values()):
        return {"state": "held", "approvers": [], "reason": "on hold", "stale": stale}
    approvers = {}
    for (src, actor), v in actions.items():
        if v["action"] == "approve" and (src == "divan_bus" or actor.lower() in maint):
            approvers[actor.lower()] = v["event_ts"]
    need = quorum_for(cfg, p["kind"])
    if len(approvers) < need:
        why = f"{len(approvers)}/{need} approvals"
        if stale:
            why += f"; {stale} given for an earlier plan do not count"
        return {"state": "awaiting", "approvers": sorted(approvers), "reason": why, "stale": stale}
    first = sorted(approvers.values())[need - 1]
    if not ignore_ttl:
        age = ((at or now()) - parse_ts(first)).days
        if age >= cfg["approval_ttl_days"]:
            return {"state": "expired", "approvers": sorted(approvers), "reason": f"approval older than {cfg['approval_ttl_days']} days"}
    return {"state": "approved", "approvers": sorted(approvers), "reason": "ok", "approved_at": first}
