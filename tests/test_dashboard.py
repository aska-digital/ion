"""Feature 7: the Org health dashboard issue; its checkboxes approve/hold, with the actor verified."""
import hashlib

from conftest import make_ctx
from helpers import MERGE, new_proposal
from ion import proposals
from ion.approve import core, sync_approvals
from ion.collect import run_collect
from ion.report import orghealth as dashboard

DASH = {"report": {"org_health_issue": True}, "approval_sources": ["github_label", "dashboard"]}


def dctx(tmp_path, world):
    return make_ctx(tmp_path, world, DASH)


def setup(ctx):
    run_collect(ctx)
    p = new_proposal(ctx, "merge", MERGE)
    dashboard.publish(ctx, {})
    return p, ctx.db.kv_get("dashboard_issue")


def tick(body, action, pid):
    out = []
    for line in body.splitlines():
        if f"ion:box {action} {pid} " in line:
            line = line.replace("- [ ]", "- [x]", 1)
        out.append(line)
    return "\n".join(out) + "\n"


def test_dashboard_is_created_once_and_edited_in_place(tmp_path, home, world):
    ctx = dctx(tmp_path, world)
    p, n = setup(ctx)
    issue = ctx.gh.issues[n]
    assert issue["title"] == "Org health" and "ion:dashboard" in issue["labels"]
    for heading in ("Awaiting approval", "Approved (queued)", "On hold", "Blocked by plan", "Rate-limited or stopped early",
                    "Errors", "Recently done"):
        assert f"## {heading}" in issue["body"]
    assert p["id"] in issue["body"] and f"ion:box approve {p['id']} {p['plan_hash'][:12]}" in issue["body"]
    assert f"issues/{p['issue_number']}" in issue["body"]                                # links to the proposal's issue
    before = len(ctx.gh.writes)
    assert dashboard.publish(ctx, {}).startswith("unchanged") and len(ctx.gh.writes) == before
    assert not [w for w in ctx.gh.writes if "/comments" in w[1]]
    assert len(ctx.gh.writes_to("POST", "/issues")) == 2                                 # the proposal and the dashboard, once


def test_disabled_by_default(ctx):
    run_collect(ctx)
    assert dashboard.publish(ctx, {}).startswith("disabled")


def test_maintainer_tick_approves_and_is_mirrored(tmp_path, home, world):
    ctx = dctx(tmp_path, world)
    p, n = setup(ctx)
    ctx.gh.human_edit(n, "maint", tick(ctx.gh.issues[n]["body"], "approve", p["id"]))
    r = sync_approvals(ctx)
    row = ctx.db.one("select * from approvals where source='dashboard'")
    assert row["actor"] == "maint" and row["valid"] == 1 and row["plan_hash"] == p["plan_hash"]
    assert proposals.get(ctx.db, p["id"])["status"] == "approved"
    assert "ion:approved" in ctx.gh.issues[p["issue_number"]]["labels"]                # mirrored: the record lives on GitHub


def test_tick_by_a_non_maintainer_is_ignored(tmp_path, home, world):
    ctx = dctx(tmp_path, world)
    p, n = setup(ctx)
    ctx.gh.human_edit(n, "mallory", tick(ctx.gh.issues[n]["body"], "approve", p["id"]))
    r = sync_approvals(ctx)
    assert any("not a maintainer" in why for _, _, why in r["ignored"])
    assert proposals.get(ctx.db, p["id"])["status"] == "awaiting-approval"


def test_a_maintainer_edit_does_not_launder_a_strangers_edit(tmp_path, home, world):
    ctx = dctx(tmp_path, world)
    p, n = setup(ctx)
    ctx.gh.human_edit(n, "mallory", tick(ctx.gh.issues[n]["body"], "approve", p["id"]), ts="2099-03-01T00:00:00Z")
    ctx.gh.human_edit(n, "maint", ctx.gh.issues[n]["body"], ts="2099-03-02T00:00:00Z")      # maintainer touches it afterwards
    sync_approvals(ctx)
    assert proposals.get(ctx.db, p["id"])["status"] == "awaiting-approval"


def test_unattributable_change_fails_closed(tmp_path, home, world):
    ctx = dctx(tmp_path, world)
    p, n = setup(ctx)
    ctx.gh.human_edit(n, None, tick(ctx.gh.issues[n]["body"], "approve", p["id"]))           # no edit history at all
    r = sync_approvals(ctx)
    assert any("no edits to attribute" in why for _, _, why in r["ignored"])
    assert proposals.get(ctx.db, p["id"])["status"] == "awaiting-approval"


def test_tick_for_a_replaced_plan_does_not_count(tmp_path, home, world):
    ctx = dctx(tmp_path, world)
    p, n = setup(ctx)
    stale_body = tick(ctx.gh.issues[n]["body"], "approve", p["id"])
    ctx.db.x("update proposals set plan_hash=? where id=?", ("f" * 64, p["id"]))               # the plan moved on
    ctx.gh.human_edit(n, "maint", stale_body)
    sync_approvals(ctx)
    assert proposals.get(ctx.db, p["id"])["status"] == "awaiting-approval"
    row = ctx.db.one("select * from approvals where source='dashboard'")
    assert row["plan_hash"] == p["plan_hash"] != "f" * 64


def test_tick_hold_holds(tmp_path, home, world):
    ctx = dctx(tmp_path, world)
    p, n = setup(ctx)
    ctx.gh.human_edit(n, "maint", tick(ctx.gh.issues[n]["body"], "hold", p["id"]))
    sync_approvals(ctx)
    assert proposals.get(ctx.db, p["id"])["status"] == "held"


def test_edited_body_is_reset_on_next_publish_and_ticks_apply_once(tmp_path, home, world):
    ctx = dctx(tmp_path, world)
    p, n = setup(ctx)
    ctx.gh.human_edit(n, "maint", tick(ctx.gh.issues[n]["body"], "hold", p["id"]))
    sync_approvals(ctx)
    sync_approvals(ctx)                                                                  # reading again records nothing new
    assert len(ctx.db.q("select 1 from approvals where source='dashboard'")) == 1
    dashboard.publish(ctx, {})
    assert "- [x]" not in ctx.gh.issues[n]["body"]                                       # boxes reset by the rewrite


def test_dashboard_source_is_off_unless_configured(ctx):
    run_collect(ctx)
    p = new_proposal(ctx, "merge", MERGE)
    ctx.cfg["report"]["org_health_issue"] = True
    dashboard.publish(ctx, {})
    n = ctx.db.kv_get("dashboard_issue")
    ctx.gh.human_edit(n, "maint", tick(ctx.gh.issues[n]["body"], "approve", p["id"]))
    sync_approvals(ctx)
    assert proposals.get(ctx.db, p["id"])["status"] == "awaiting-approval"              # approval_sources lacks `dashboard`


def test_dashboard_shows_blocks_stops_and_errors(tmp_path, home, world):
    ctx = dctx(tmp_path, world)
    run_collect(ctx)
    from ion.analyze import run_analyze
    run_analyze(ctx)
    ctx.db.x("update runs set partial=1 where id=(select max(id) from runs)")
    ctx.db.kv_set("last_breaker_trip", {"ts": proposals.iso(), "reason": "too many repos"})
    body, _ = dashboard.render_body(ctx, {"errors": ["propose: boom"]})
    assert "stopped at its GraphQL budget" in body and "circuit breaker tripped" in body and "propose: boom" in body
    assert "secret-sauce: branch_protection needs a paid plan" in body and "## Lifecycle" in body and "old-tool: `archive-proposed`" in body


def test_deleted_dashboard_is_recreated_even_when_unchanged(tmp_path, home, world, monkeypatch):
    from ion.ghclient import GHError
    ctx = dctx(tmp_path, world)
    _, old = setup(ctx)
    original = ctx.gh.rest
    def rest(method, path, body=None):
        if method == "GET" and path.endswith(f"/issues/{old}"):
            raise GHError("Not Found", 404)
        return original(method, path, body)
    monkeypatch.setattr(ctx.gh, "rest", rest)
    assert dashboard.publish(ctx, {}).startswith("written")
    new = ctx.db.kv_get("dashboard_issue")
    assert new != old and ctx.db.kv_get("dashboard_written")["number"] == new


def test_dashboard_read_errors_do_not_create_duplicate(tmp_path, home, world, monkeypatch):
    import pytest
    from ion.ghclient import GHError
    ctx = dctx(tmp_path, world)
    _, old = setup(ctx)
    original = ctx.gh.rest
    def rest(method, path, body=None):
        if method == "GET" and path.endswith(f"/issues/{old}"):
            raise GHError("Forbidden", 403)
        return original(method, path, body)
    monkeypatch.setattr(ctx.gh, "rest", rest)
    with pytest.raises(GHError):
        dashboard.publish(ctx, {})
    assert ctx.db.kv_get("dashboard_issue") == old
