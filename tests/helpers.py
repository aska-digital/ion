import json

from ion import proposals
from ion.approve import core
from ion.propose.engine import plan_for, refresh_issue

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


def set_override(ctx, repo, text):
    """Pretend the latest snapshot of `repo` carried this `.github/ion.yml`."""
    snap = json.loads(ctx.db.one("select blob from snapshots where repo=?", (repo,))["blob"])
    snap["override_text"] = text
    ctx.db.x("update snapshots set blob=? where repo=?", (json.dumps(snap), repo))


MERGE = {"source": "alpha-cli", "target": "alpha-core", "prefix": "alpha-cli", "source_branch": "main", "target_branch": "main",
         "visibility_conflict": False}
SPLIT = {"source": "mono", "dirs": ["engine", "engine_utils"], "new_repo": "mono-engine", "private": False, "source_branch": "main"}
