"""Features 1 and 2: typed write requests with per-type caps, and the run-wide circuit breaker."""
import json
import re
from pathlib import Path

import pytest

from conftest import make_ctx
from helpers import MERGE, approve, new_proposal
from ion import proposals
from ion.collect import run_collect
from ion.execute import executor_for
from ion.execute.runner import run_execute
from ion.util import Refused
from ion.writes import (BreakerTripped, CapReached, WriteRejected, WriteRequest, lowers_protection)


def req(t, repo="alpha-core", **payload):
    return WriteRequest(t, repo, payload)


# ---- typed requests -----------------------------------------------------------------------

def test_unknown_type_and_bad_payload_are_rejected(ctx):
    w = ctx.writes
    with pytest.raises(WriteRejected, match="unknown write type"):
        w.submit(req("delete_repo"))
    with pytest.raises(WriteRejected):
        w.submit(req("open_pr", title="t"))                                     # missing head/base/body
    with pytest.raises(WriteRejected):
        w.submit(req("open_pr", title="t", head="h", base="b", body="x", extra=1))   # unexpected key
    with pytest.raises(WriteRejected):
        w.submit(WriteRequest("archive_repo", "bad/repo name", {}))              # repo must look like a repo name
    assert not ctx.gh.writes


def test_push_branch_only_ion_branches_never_default(ctx):
    w = ctx.writes
    for branch in ("main", "feature/x"):
        with pytest.raises(WriteRejected):
            w.submit(req("push_branch", branch=branch, base="main"), lambda: None)
    w.submit(req("push_branch", branch="ion/P-0001", base="main"), lambda: None)
    w.submit(req("push_branch", repo="new", branch="main", base="main", initial=True), lambda: None)   # brand new empty repo


def test_every_write_is_logged(ctx):
    ctx.writes.submit(req("enable_dependabot_alerts", repo="old-tool"))
    row = ctx.db.one("select * from write_log")
    assert (row["type"], row["repo"], row["ok"]) == ("enable_dependabot_alerts", "old-tool", 1)
    assert ctx.gh.writes_to("PUT", "/repos/acme/old-tool/vulnerability-alerts")


def test_per_type_cap_defers_instead_of_failing(ctx):
    ctx.cfg["writes"] = {"caps": {"open_pr": 2}}
    w = ctx.writes
    for i in range(2):
        w.submit(req("open_pr", title="t", head=f"ion/P-{i}", base="main", body="b"))
    with pytest.raises(CapReached, match="cap of 2 open_pr"):
        w.submit(req("open_pr", title="t", head="ion/P-9", base="main", body="b"))
    assert len(ctx.gh.writes_to("POST", "/pulls")) == 2
    w.reset()                                                                  # a new run starts with fresh counters
    w.submit(req("open_pr", title="t", head="ion/P-9", base="main", body="b"))


def test_default_cap_is_five_prs_and_one_archive(ctx):
    ctx.cfg["guards"] = {"enabled": False}
    for i in range(5):
        ctx.writes.submit(req("open_pr", title="t", head=f"ion/P-{i}", base="main", body="b"))
    with pytest.raises(CapReached):
        ctx.writes.submit(req("open_pr", title="t", head="ion/P-5", base="main", body="b"))
    ctx.writes.submit(req("archive_repo", repo="old-tool"))
    with pytest.raises(CapReached):
        ctx.writes.submit(req("archive_repo", repo="alpha-cli"))


def test_cap_reached_leaves_the_proposal_waiting_not_failed(ctx, world):
    ctx.cfg["writes"] = {"caps": {"open_pr": 0}}
    run_collect(ctx)
    p = approve(ctx, new_proposal(ctx, "health_fix", {"repo": "alpha-core", "file": "README.md", "description": "d"}))
    out = executor_for(ctx, "health_fix").apply(p, dry_run=False)
    assert out["waiting"] and "cap" in out["waiting"]
    assert proposals.get(ctx.db, p["id"])["status"] == "executing"             # not 'failed': it resumes next run
    ctx.cfg["writes"] = {"caps": {"open_pr": 5}}
    out = executor_for(ctx, "health_fix").apply(proposals.get(ctx.db, p["id"]), dry_run=False)
    assert not out["waiting"] and proposals.get(ctx.db, p["id"])["status"] == "done"


def test_kill_switch_stops_the_broker_itself(ctx):
    ctx.cfg["paused"] = True
    with pytest.raises(Refused, match="kill switch"):
        ctx.writes.submit(req("archive_repo", repo="old-tool"))


def test_nothing_outside_the_broker_writes_to_github():
    """Static guard: only ion/writes.py may issue a non-GET GitHub API call."""
    root = Path(__file__).parent.parent / "ion"
    pat = re.compile(r"""\.rest\(\s*["'](POST|PUT|PATCH|DELETE)["']""")
    offenders = [f"{p.relative_to(root)}:{i}" for p in root.rglob("*.py") if p.name != "writes.py"
                 for i, line in enumerate(p.read_text().splitlines(), 1) if pat.search(line)]
    assert offenders == []


# ---- circuit breaker --------------------------------------------------------------------------

def test_breaker_trips_when_a_run_touches_too_much_of_the_org(ctx):
    run_collect(ctx)
    ctx.cfg["guards"] = {"max_repo_fraction": 0.3, "min_repos": 2}               # 5 live repos: a third touched is too much
    w = ctx.writes
    w.submit(req("enable_dependabot_alerts", repo="alpha-core"))
    with pytest.raises(BreakerTripped, match="touch 2 of 5 repos"):
        w.submit(req("enable_dependabot_alerts", repo="old-tool"))
    assert w.tripped and len(ctx.gh.writes_to("PUT", "/vulnerability-alerts")) == 1
    with pytest.raises(BreakerTripped):                                          # the whole run is stopped, not one write
        w.submit(req("add_label", repo="ion-tracker", number=1, labels=["x"]))
    assert ctx.db.kv_get("last_breaker_trip")["reason"].startswith("the run would touch")
    w.reset()                                                                    # next run starts clean
    assert not w.tripped


def test_tracker_writes_do_not_count_as_touching_repos(ctx):
    run_collect(ctx)
    ctx.cfg["guards"] = {"max_repo_fraction": 0.1, "min_repos": 1}
    for i in range(1, 6):
        ctx.writes.tracker("add_label", number=i, labels=["x"])                 # the tracker is Ion's own repo


def test_breaker_trips_on_write_volume(ctx):
    ctx.cfg["guards"] = {"max_writes": 3}
    ctx.cfg["writes"] = {"caps": {"add_label": 50}}
    for i in range(3):
        ctx.writes.tracker("add_label", number=i + 1, labels=["x"])
    with pytest.raises(BreakerTripped, match="more than 3 writes"):
        ctx.writes.tracker("add_label", number=9, labels=["x"])


def test_breaker_can_be_switched_off(ctx):
    run_collect(ctx)
    ctx.cfg["guards"] = {"enabled": False, "min_repos": 1, "max_repo_fraction": 0.0}
    ctx.writes.submit(req("enable_dependabot_alerts", repo="alpha-core"))
    ctx.writes.submit(req("enable_dependabot_alerts", repo="old-tool"))


def test_lowers_protection_rules():
    rule = {"requiresApprovingReviews": True, "requiredApprovingReviewCount": 1, "allowsForcePushes": False,
            "allowsDeletions": False, "isAdminEnforced": True}
    same = {"require_pr": True, "required_approvals": 1, "enforce_admins": True}
    assert lowers_protection(rule, same) == [] and lowers_protection(None, {"allow_force_pushes": True}) == []
    assert lowers_protection(rule, {**same, "required_approvals": 0})
    assert lowers_protection(rule, {**same, "allow_force_pushes": True})
    assert lowers_protection(rule, {**same, "allow_deletions": True})
    assert lowers_protection(rule, {**same, "enforce_admins": False})
    assert lowers_protection(rule, {"require_pr": False})
    assert lowers_protection(rule, {**same, "required_approvals": 2}) == []                  # stronger is fine


def test_breaker_blocks_a_protection_downgrade_in_flight(ctx):
    run_collect(ctx)
    weak = {"require_pr": False, "allow_force_pushes": True}                    # as if a typo in ion.yml
    with pytest.raises(BreakerTripped, match="lower protection"):
        ctx.writes.submit(req("put_branch_protection", repo="mono", branch="main", settings=weak))
    assert not ctx.gh.writes


def test_preflight_aborts_the_whole_run_before_any_write(ctx):
    """Three approved archives exceed max_archives=2: nothing at all is archived, and the run reports why."""
    run_collect(ctx)
    ps = [approve(ctx, new_proposal(ctx, "archive", {"repo": r})) for r in ("old-tool", "alpha-cli", "mono")]
    ctx.gh.writes.clear()
    r = run_execute(ctx, apply=True)
    assert "3 repos would be archived" in r["breaker"] and any("circuit breaker" in e for e in r["errors"])
    assert not ctx.gh.archived and not ctx.gh.writes_to("PATCH", "/repos/acme/")
    assert all(proposals.get(ctx.db, p["id"])["status"] == "approved" for p in ps)


def test_preflight_blocks_policy_fix_that_lowers_protection(ctx):
    run_collect(ctx)
    fix = {"class": "branch_protection", "repo": "mono", "branch": "main", "settings": {"require_pr": False}}
    ctx.cfg["policy_actions"] = {"branch_protection": "fix"}
    new_proposal(ctx, "policy_fix", fix)
    r = run_execute(ctx, apply=True)
    assert "lower protection" in r["breaker"] and not ctx.gh.writes_to("PUT", "/protection")


def test_dry_run_reports_the_breaker_without_tripping_state(ctx):
    run_collect(ctx)
    for r in ("old-tool", "alpha-cli", "mono"):
        approve(ctx, new_proposal(ctx, "archive", {"repo": r}))
    r = run_execute(ctx)                                                         # dry run
    assert r["breaker"] and ctx.db.kv_get("last_breaker_trip") is None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "patch"])
def test_runtime_transport_refuses_unbrokered_methods(ctx, method):
    from ion.ghclient import GH
    from ion.util import SafetyError
    calls = []
    gh = GH(runner=lambda *a: calls.append(a))
    for client in (gh, ctx.gh):
        with pytest.raises(SafetyError, match="write broker"):
            client.rest(method=method, path="/repos/acme/x")
    assert calls == []


@pytest.mark.parametrize("query", ["mutation { deleteRepository }", "# query\n mutation X { change }", "query { viewer { login } } mutation { change }"])
def test_runtime_transport_refuses_graphql_mutations(ctx, query):
    from ion.ghclient import GH
    from ion.util import SafetyError
    for client in (GH(runner=lambda *a: pytest.fail("transport invoked")), ctx.gh):
        with pytest.raises(SafetyError, match="GraphQL mutations"):
            client.graphql(query)
