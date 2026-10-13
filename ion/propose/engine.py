"""Propose: candidates -> hysteresis/cooldown -> synthesis -> proposal rows + tracking issues."""
from __future__ import annotations

import json
import logging
import re

from .. import proposals, tracker
from ..analyze import boundaries, latest_analysis
from ..approve.core import evaluate
from ..collect.snapshot import latest_snapshots
from ..execute import executor_for
from ..util import iso, now
from . import scoring, synth

log = logging.getLogger("ion")
ACTIVE = ("awaiting-approval", "approved", "held", "executing")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def gather_candidates(ctx, analysis: dict, snaps: dict[str, dict]) -> list[dict]:
    cfg = ctx.cfg
    out = []
    for pair in analysis["coupling"]:
        if not scoring.merge_qualifies(pair, cfg):
            continue
        sa, sb = snaps[pair["a"]], snaps[pair["b"]]
        src, tgt = scoring.merge_direction(sa, sb)
        subject = {"source": src, "target": tgt, "prefix": src, "source_branch": snaps[src]["default_branch"],
                   "target_branch": snaps[tgt]["default_branch"], "visibility_conflict": pair["visibility_conflict"]}
        ev = {"signals": pair["signals"], **pair["evidence"]}
        apart = boundaries.separation(sa, sb, cfg)
        if apart:
            subject["cross_scope"] = apart
        out.append({"key": f"merge:{src}>{tgt}", "kind": "merge", "subject": subject, "score": pair["score"], "evidence": ev,
                    "persist": True,
                    "blocked": apart if cfg["boundaries"]["block_cross_scope_merge"] else None})
    for r in analysis["cohesion"]:
        good = [c for c in r["candidates"] if scoring.split_qualifies(c, cfg)]
        if not good:
            continue
        c = max(good, key=lambda c: c["score"])
        s = snaps[r["repo"]]
        subject = {"source": r["repo"], "dirs": c["dirs"], "new_repo": f"{r['repo']}-{slug(c['dirs'][0].split('/')[-1])}",
                   "private": bool(s["private"]), "source_branch": s["default_branch"]}
        out.append({"key": f"split:{r['repo']}:{'+'.join(c['dirs'])}", "kind": "split", "subject": subject, "score": c["score"],
                    "evidence": c, "persist": True})
    for a in analysis["archive"]:
        if a["qualifies"]:
            out.append({"key": f"archive:{a['repo']}", "kind": "archive", "subject": {"repo": a["repo"]}, "score": a["score"],
                        "evidence": a, "persist": True})
    for repo, findings in analysis["policy"].items():
        for f in findings:
            fix = f.get("fix")
            if f["status"] != "drift" or not fix or f.get("action") == "log":
                continue                       # `log` level: recorded in the analysis and dashboard, never proposed
            if fix["class"] == "health_fix":
                if not re.match(r"readme", fix["file"], re.I):
                    continue
                snap = snaps[repo]
                out.append({"key": f"health:{repo}:{fix['file']}", "kind": "health_fix",
                            "subject": {"repo": repo, "file": fix["file"], "description": snap.get("description")},
                            "score": 0.6, "evidence": {"detail": f["detail"]}, "persist": True})
            else:
                out.append({"key": f"policy:{fix['class']}:{repo}", "kind": "policy_fix", "subject": fix, "score": 1.0,
                            "evidence": {"detail": f["detail"], "status": f["status"]}, "persist": False})
    return out


def plan_for(ctx, kind: str, subject: dict, pid: str = "P-XXXX", phase: str | None = None):
    p = {"id": pid, "kind": kind, "subject": subject, "status": "awaiting-approval", "state": {"phase": phase} if phase else {},
         "epoch": iso(), "text": {}, "evidence": {}}
    plan = executor_for(ctx, kind).plan(p)
    plan.subject = subject
    return plan


def _latest_for_key(db, key: str):
    return db.one("select * from proposals where key=? order by num desc limit 1", (key,))


def refresh_issue(ctx, p: dict, approval: dict | None = None) -> None:
    try:
        plan = plan_for(ctx, p["kind"], p["subject"], p["id"], p["state"].get("phase"))
        ex = executor_for(ctx, p["kind"])
        body = tracker.render_body(ctx, p, plan.render(), ex.rollback_notes(p), approval)
        tracker.upsert_issue(ctx, p, body)
        ctx.db.x("update proposals set body=? where id=?", (body, p["id"]))
        if not ctx.cfg.is_paused():
            proposals.note_shown(ctx.db, p["id"], p["plan_hash"])      # approvals bind to what was displayed
    except Exception as e:  # noqa: BLE001
        log.warning("issue update failed for %s: %s", p["id"], str(e)[:200])


def run_propose(ctx, dry_run: bool = False) -> dict:
    cfg, db = ctx.cfg, ctx.db
    analysis = latest_analysis(db)
    res = {"candidates": [], "created": [], "updated": [], "replanned": [], "suppressed": [], "dropped": [], "held": [], "errors": []}
    if not analysis:
        return res
    snaps, _ = latest_snapshots(db)
    gen = analysis["gen"]
    cands = gather_candidates(ctx, analysis, snaps)
    seen = set()
    for c in cands:
        key = c["key"]
        seen.add(key)
        row = db.one("select * from candidates where key=?", (key,))
        streak = scoring.next_streak(row["streak"] if row else None, row["last_gen"] if row else None, gen)
        info = {"key": key, "kind": c["kind"], "score": c["score"], "streak": streak, "state": "pending"}
        res["candidates"].append(info)
        if c.get("blocked"):
            info["state"] = f"blocked by boundary: {c['blocked']}"
            continue
        if not dry_run:
            db.x("insert into candidates(key,kind,subject,first_gen,last_gen,streak,score,evidence,updated) values(?,?,?,?,?,?,?,?,?)"
                 " on conflict(key) do update set last_gen=excluded.last_gen, streak=excluded.streak, score=excluded.score,"
                 " evidence=excluded.evidence, updated=excluded.updated",
                 (key, c["kind"], json.dumps(c["subject"]), gen, gen, streak, c["score"], json.dumps(c["evidence"], default=str), iso()))
        latest = _latest_for_key(db, key)
        if latest is not None and latest["status"] in ACTIVE:
            p = proposals.view(latest)
            info["state"] = "tracked:" + p["id"]
            if not dry_run:
                _refresh_existing(ctx, p, c, res)
            continue
        if latest is not None and latest["status"] in ("rejected", "dropped"):
            if scoring.cooldown_blocks(latest["rejected_at"], latest["rejected_score"], c["score"], cfg):
                info["state"] = "suppressed (cooldown)"
                res["suppressed"].append(key)
                continue
        if latest is not None and latest["status"] in ("done", "failed"):
            if c["kind"] not in ("policy_fix", "health_fix") or json.loads(latest["state"]).get("created_gen") == gen:
                info["state"] = f"closed ({latest['status']})"
                continue
        if c["persist"] and not scoring.ready(streak, cfg):
            info["state"] = f"waiting ({streak}/{cfg['thresholds']['persist_runs']} runs)"
            continue
        info["state"] = "would propose" if dry_run else "proposed"
        if dry_run:
            continue
        try:
            _create(ctx, c, gen, res)
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"{key}: {str(e)[:200]}")
    if not dry_run:
        for r in db.q("select key from candidates"):
            if r["key"] not in seen:
                db.x("delete from candidates where key=?", (r["key"],))
    return res


def _create(ctx, c: dict, gen: int, res: dict) -> None:
    db = ctx.db
    plan = plan_for(ctx, c["kind"], c["subject"])
    text = synth.synthesize(c, plan.render(), ctx.llm)
    if text["recommend"] == "drop":
        p = proposals.create(db, c["kind"], c["key"], c["subject"], c["evidence"], text, c["score"], plan.fingerprint(), status="dropped")
        proposals.set_status(db, p["id"], "dropped", rejected_at=iso(), rejected_score=c["score"])
        res["dropped"].append(c["key"])
        return
    if text["recommend"] == "hold":
        res["held"].append(c["key"])
        db.x("update candidates set status='llm-hold' where key=?", (c["key"],))
        return
    status = "awaiting-approval"
    p = proposals.create(db, c["kind"], c["key"], c["subject"], c["evidence"], text, c["score"], plan.fingerprint(), status=status)
    db.set_state(p["id"], created_gen=gen)
    p = proposals.get(db, p["id"])
    plan = plan_for(ctx, p["kind"], p["subject"], p["id"])   # real id in the issue's commands
    refresh_issue(ctx, p)
    res["created"].append({"id": p["id"], "kind": p["kind"], "title": text["title"], "score": c["score"]})


def _refresh_existing(ctx, p: dict, c: dict, res: dict) -> None:
    db = ctx.db
    plan = plan_for(ctx, c["kind"], c["subject"])
    new_hash = plan.fingerprint()
    replan = scoring.replan_needed(p["plan_hash"], new_hash, p["epoch_score"], c["score"], ctx.cfg)
    st = p["state"]
    st["subject"], st["evidence"] = c["subject"], c["evidence"]
    if replan and p["status"] in ("awaiting-approval", "approved", "held"):
        notes = st.get("notes", []) + [f"{iso()}: evidence changed; plan re-made, approval is needed again"]
        st["notes"] = notes
        db.x("update proposals set state=?, score=?, plan_hash=?, epoch=?, epoch_score=?, status='awaiting-approval', updated=? where id=?",
             (json.dumps(st), c["score"], new_hash, iso(), c["score"], iso(), p["id"]))
        res["replanned"].append(p["id"])
        _clear_stale_approvals(ctx, p, "plan changed")
    else:
        db.x("update proposals set state=?, score=?, updated=? where id=?", (json.dumps(st), c["score"], iso(), p["id"]))
        res["updated"].append(p["id"])
    refresh_issue(ctx, proposals.get(db, p["id"]))


def create_followup(ctx, parent: dict, kind: str, subject: dict) -> dict:
    """A second proposal (e.g. archive after a merge). Needs its own approval."""
    plan = plan_for(ctx, kind, subject)
    cand = {"key": f"{kind}:{subject.get('repo')}:{parent['id']}", "kind": kind, "subject": subject, "score": 0.9,
            "evidence": {"reasons": [subject.get("reason", "")]}}
    text = synth.template(cand, plan.render())
    p = proposals.create(ctx.db, kind, cand["key"], subject, cand["evidence"], text, 0.9, plan.fingerprint(), parent=parent["id"])
    refresh_issue(ctx, proposals.get(ctx.db, p["id"]))
    return p


def _clear_stale_approvals(ctx, p: dict, why: str) -> None:
    """After a re-plan, earlier approvals no longer count (their hash differs), and the label comes off the issue."""
    from ..approve import labels
    ctx.db.x("update approvals set valid=0, reason=? where proposal=? and valid=1", (why, p["id"]))
    try:
        labels.clear_decision_labels(ctx, proposals.get(ctx.db, p["id"]))
    except Exception as e:  # noqa: BLE001
        log.warning("could not clear labels on %s: %s", p["id"], str(e)[:160])


def file_proposal(ctx, kind: str, key: str, subject: dict, evidence: dict, score: float = 0.8) -> tuple[dict, bool]:
    """File a proposal that did not come from the analyzers (a maintainer asked for it, e.g. a fleet autofix).

    Returns (proposal, created). An active proposal with the same key is returned as is."""
    latest = _latest_for_key(ctx.db, key)
    if latest is not None and latest["status"] in ACTIVE:
        return proposals.view(latest), False
    plan = plan_for(ctx, kind, subject)
    text = synth.template({"key": key, "kind": kind, "subject": subject, "evidence": evidence, "score": score}, plan.render())
    p = proposals.create(ctx.db, kind, key, subject, evidence, text, score, plan.fingerprint())
    p = proposals.get(ctx.db, p["id"])
    refresh_issue(ctx, p)
    return proposals.get(ctx.db, p["id"]), True
