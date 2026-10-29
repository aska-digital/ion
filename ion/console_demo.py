"""Synthetic product tour. Does not open local state or contact GitHub."""
from __future__ import annotations

import datetime as dt

from .util import iso, now


def demo_state():
    at = now()
    stamp = iso(at)
    before = iso(at - dt.timedelta(hours=2))
    repos = []
    for name, description, lifecycle, private, findings in [
        ("atlas", "Shared libraries for the product family", "active", False, []),
        ("atlas-cli", "Command line tools built on Atlas", "active", False, ["missing_license"]),
        ("observatory", "Metrics and service health", "active", True, []),
        ("field-notes", "Product decisions and research", "active", True, ["missing_ci"]),
        ("relay", "Reliable delivery between services", "active", False, []),
        ("relay-legacy", "Previous transport implementation", "archive-proposed", False, ["stale"]),
        ("design-system", "Shared components and tokens", "active", False, []),
        ("archive-2023", "Preserved historical experiments", "archived", True, []),
    ]:
        repos.append(dict(name=name, description=description, lifecycle=lifecycle, private=private,
                          archived=lifecycle == "archived", fork=False, default_branch="main", pushed_at=before,
                          url=None, topics=["example"], governed=True,
                          health=[{"code": f, "detail": f.replace("_", " ").capitalize()} for f in findings],
                          policy=[{"class": "required_files", "status": "drift", "detail": "LICENSE has not been added"}] if name == "atlas-cli" else [],
                          mirror={"status": "current", "detail": "Synthetic matching commit"}))
    proposals = []
    for i, kind, status, title, names in [
        (1, "merge", "awaiting-approval", "Bring Atlas tools and libraries together", ["atlas-cli", "atlas"]),
        (2, "archive", "held", "Preserve the legacy relay as an archive", ["relay-legacy"]),
        (3, "autofix", "approved", "Add dependency update configuration", ["atlas", "relay"]),
        (4, "health_fix", "done", "Document the design system", ["design-system"]),
    ]:
        approval = {"awaiting-approval": "awaiting", "held": "held", "approved": "approved", "done": "approved"}[status]
        proposals.append(dict(id=f"P-{i:04d}", kind=kind, status=status, title=title, repos=names,
            score=.82, issue_url=None, plan_hash=(str(i) * 64), subject={"repos": names}, updated=stamp,
            evidence={"source": "Synthetic tour", "observations": ["Repeated imports", "Shared release cadence"] if kind == "merge" else ["Example evidence only"]},
            text={"rationale": "Review the evidence, scope, and rollback before changing the organisation."},
            approval={"state": approval, "reason": "Example recorded decision", "approvers": ["example-maintainer"] if approval == "approved" else []},
            plan={"phase": "open_pr", "hash_matches": True, "steps": [
                {"name": "prepare", "description": "Prepare the reviewed change in a work branch", "commands": []},
                {"name": "verify", "description": "Verify preserved contents and history", "commands": []},
                {"name": "open-pr", "description": "Open a pull request for a maintainer to review", "commands": []}],
                "rollback": ["Close the proposed pull request before merge."], "error": None},
            changesets=[], ledger=[{"step": "apply.open-pr", "phase": "after", "ts": before, "detail": "Example pull request opened"}] if status == "done" else []))
    stages = ["collect", "analyze", "propose", "approvals", "execute", "report"]
    steps = [dict(name=name, status="success", started=before, finished=before, duration_seconds=1.2,
                  errors=[], metrics={"graphql_points": 8 if name == "collect" else 0, "rest_calls": 0, "writes_succeeded": 0, "writes_failed": 0}) for name in stages]
    return dict(schema_version=1, generated_at=stamp, demo=True, org="Example organisation",
        summary=dict(repositories=8, active_repositories=7, health_findings=3, policy_drift=1, awaiting_approval=1, open_proposals=3),
        safety=dict(paused=False, breaker=None, execution_authority="Existing approval channels only"),
        freshness=dict(status="fresh", last_success_at=before, age_hours=2, stale_after_hours=48, latest_status="success", reason="Synthetic complete pipeline run within freshness limit"),
        readiness=dict(status="current", checks=[dict(id=name, label=label, status="pass", detail=detail) for name, label, detail in [
            ("collection", "Complete collection", "8 repositories in this synthetic snapshot"),
            ("analysis", "Analysis generation", "Analysis and collection both use generation 12"),
            ("config", "Configuration provenance", "Analysis matches the example configuration"),
            ("mirrors", "Content coverage", "All example mirrors match their collected commits"),
        ]]), collection=dict(generation=12, started=before, finished=before, repos=8, partial=False),
        analysis=dict(generation=12, ts=before, config_matches=True), repositories=repos, proposals=proposals,
        runs=[dict(id=12, started=before, finished=before, status="success", mode="dry-run", duration_seconds=7.2, errors=[], steps=steps,
                   metrics={"graphql_points": 8, "rest_calls": 0, "writes_succeeded": 0, "writes_failed": 0})])
