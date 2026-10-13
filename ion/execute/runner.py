"""Drive executors over proposals. Dry-run unless apply=True."""
from __future__ import annotations

import logging

from .. import proposals, tracker
from ..util import iso
from ..util import Refused
from ..writes import BreakerTripped, current_rule, lowers_protection
from . import executor_for
from .base import check_gate

log = logging.getLogger("ion")


def run_execute(ctx, pid: str | None = None, apply: bool = False) -> dict:
    db = ctx.db
    res = {"actions": [], "skipped": [], "errors": [], "plans": {}}
    if pid:
        rows = [db.get_proposal(pid)]
        if rows[0] is None:
            res["errors"].append(f"no such proposal {pid}")
            return res
    else:
        rows = db.q("select * from proposals where status in ('approved','executing','awaiting-approval','held') order by num")
    rows = [r for r in rows if r is not None]
    why = preflight(ctx, rows)
    if why:
        res["breaker"] = why
        res["errors"].append(f"circuit breaker: {why}")
        if apply:
            ctx.db.kv_set("last_breaker_trip", {"ts": iso(), "reason": why})
            return res                                   # abort the whole run before anything is written
    for row in rows:
        p = proposals.view(row)
        ex = executor_for(ctx, p["kind"])
        old = p["status"]
        try:
            out = ex.apply(p, dry_run=not apply)
        except BreakerTripped as e:
            res["breaker"] = str(e)
            res["errors"].append(f"circuit breaker: {e}")
            _sync(ctx, p, old)
            break
        except Refused as e:
            if pid:
                try:
                    res["plans"][p["id"]] = ex.plan(p).render()
                except Exception as e2:  # noqa: BLE001
                    res["plans"][p["id"]] = [f"(plan unavailable: {e2})"]
            res["skipped"].append(f"{p['id']}: {e}")
            continue
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"{p['id']}: {str(e)[:300]}")
            _sync(ctx, p, old)
            continue
        if out["dry_run"]:
            res["plans"][p["id"]] = out["plan"]
        else:
            if out["done"] or out["waiting"]:
                msg = f"{p['id']} {out['phase']}: " + (", ".join(out["done"]) or "no step done")
                if out["waiting"]:
                    msg += f" (waiting: {out['waiting']})"
                res["actions"].append(msg)
            _sync(ctx, p, old)
    return res


def preflight(ctx, rows) -> str | None:
    """Blast radius of everything this run would do, checked before the first write. Returns a reason to abort."""
    repos: set[str] = set()
    archives = 0
    lowerings: list[str] = []
    for row in rows:
        p = proposals.view(row)
        try:
            check_gate(ctx, p)
        except Refused:
            continue                                     # would be refused anyway; touches nothing
        repos |= set(proposals.involved_repos(p)) - {ctx.cfg["tracker_repo"]}
        if p["kind"] == "archive":
            archives += 1
        if p["kind"] == "policy_fix" and p["subject"].get("class") == "branch_protection":
            lowerings += lowers_protection(current_rule(ctx.db, p["subject"]["repo"]), p["subject"].get("settings") or {})
    return ctx.writes.check_blast(repos, archives, lowerings)


def _sync(ctx, p: dict, old: str) -> None:
    p2 = proposals.get(ctx.db, p["id"])
    if p2["status"] != old:
        try:
            tracker.set_state_label(ctx, p2, old, p2["status"])
            from ..propose.engine import refresh_issue
            refresh_issue(ctx, p2)
        except Exception as e:  # noqa: BLE001
            log.warning("issue sync failed: %s", str(e)[:200])
