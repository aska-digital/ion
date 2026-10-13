"""Drive executors over proposals. Dry-run unless apply=True."""
from __future__ import annotations

import logging

from .. import proposals, tracker
from ..util import Refused
from . import executor_for

log = logging.getLogger("mimar")


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
    for row in rows:
        p = proposals.view(row)
        ex = executor_for(ctx, p["kind"])
        old = p["status"]
        try:
            out = ex.apply(p, dry_run=not apply)
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


def _sync(ctx, p: dict, old: str) -> None:
    p2 = proposals.get(ctx.db, p["id"])
    if p2["status"] != old:
        try:
            tracker.set_state_label(ctx, p2, old, p2["status"])
            from ..propose.engine import refresh_issue
            refresh_issue(ctx, p2)
        except Exception as e:  # noqa: BLE001
            log.warning("issue sync failed: %s", str(e)[:200])
