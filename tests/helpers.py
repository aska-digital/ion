from mimar import proposals
from mimar.approve import core
from mimar.propose.engine import plan_for, refresh_issue

FUTURE = "2099-01-01T00:00:00Z"


def new_proposal(ctx, kind, subject, score=0.8, issue=True):
    plan = plan_for(ctx, kind, subject)
    text = {"title": f"{kind} test", "summary": "s", "rationale": "r", "risks": [], "rollback_plan": ["undo"], "confidence": 0.8,
            "blast_radius": "x", "migration_plan": [], "recommend": "propose"}
    p = proposals.create(ctx.db, kind, f"{kind}:test:{subject}", subject, {"signals": {}}, text, score, plan.fingerprint())
    if issue:
        refresh_issue(ctx, proposals.get(ctx.db, p["id"]))
    return proposals.get(ctx.db, p["id"])


def approve(ctx, p, actor="maint", ref=None):
    core.record(ctx.db, p["id"], "github_label", actor, "approve", FUTURE, ref or f"t-{actor}", True)
    ctx.db.x("update proposals set status='approved' where id=?", (p["id"],))
    return proposals.get(ctx.db, p["id"])


MERGE = {"source": "alpha-cli", "target": "alpha-core", "prefix": "alpha-cli", "source_branch": "main", "target_branch": "main",
         "visibility_conflict": False}
SPLIT = {"source": "mono", "dirs": ["engine", "engine_utils"], "new_repo": "mono-engine", "private": False, "source_branch": "main"}
