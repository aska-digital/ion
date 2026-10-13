"""Approve: sync approval sources, then move proposals between states."""
from __future__ import annotations

import json
import logging

from .. import proposals, tracker
from ..util import iso
from . import bus, core, labels

log = logging.getLogger("mimar")
LIVE = ("awaiting-approval", "approved", "held")


def sync_approvals(ctx) -> dict:
    db, cfg = ctx.db, ctx.cfg
    res = {"changed": [], "errors": [], "ignored": []}
    sources = cfg["approval_sources"]
    if "divan_bus" in sources:
        try:
            r = bus.sync_bus(ctx)
            res["ignored"] += r["ignored"]
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"bus: {str(e)[:200]}")
    if "dashboard" in sources:
        try:
            from ..report import orghealth as dashboard
            res["ignored"] += dashboard.sync(ctx)["ignored"]
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"dashboard: {str(e)[:200]}")
    for row in db.q("select id from proposals where status in ('awaiting-approval','approved','held')"):
        p = proposals.get(db, row["id"])
        if "github_label" in sources and p.get("issue_number"):
            try:
                r = labels.sync_labels(ctx, p)
                res["ignored"] += [(p["id"], a, f"label {l} by non-maintainer") for l, a in r["ignored"]]
            except Exception as e:  # noqa: BLE001
                res["errors"].append(f"{p['id']}: {str(e)[:200]}")
        _apply_state(ctx, p, res)
    return res


def _apply_state(ctx, p: dict, res: dict) -> None:
    db = ctx.db
    ev = core.evaluate(ctx, p)
    old = p["status"]
    new = {"approved": "approved", "awaiting": "awaiting-approval", "held": "held", "rejected": "rejected",
           "expired": "awaiting-approval"}[ev["state"]]
    st = p["state"]
    if ev["state"] == "expired":
        st["notes"] = st.get("notes", []) + [f"{iso()}: approval expired; approval is needed again"]
        db.x("update proposals set state=?, epoch=?, status='awaiting-approval', updated=? where id=?",
             (json.dumps(st), iso(), iso(), p["id"]))
        res["changed"].append((p["id"], old, "expired"))
        try:
            labels.clear_decision_labels(ctx, p)
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"{p['id']}: {str(e)[:160]}")
    elif new != old:
        extra = ""
        if new == "rejected":
            extra = f", rejected_at='{iso()}', rejected_score={p['score']}"
        if new == "approved":
            extra = f", approved_at='{ev.get('approved_at', iso())}'"
        db.x(f"update proposals set status=?, updated=?{extra} where id=?", (new, iso(), p["id"]))
        res["changed"].append((p["id"], old, new))
    else:
        return
    p2 = proposals.get(db, p["id"])
    try:
        tracker.set_state_label(ctx, p2, old, p2["status"])
        from ..propose.engine import refresh_issue
        refresh_issue(ctx, p2, ev)
    except Exception as e:  # noqa: BLE001
        res["errors"].append(f"{p['id']}: {str(e)[:160]}")
