"""Feature 3 (approvals bound to a plan hash), A3 (unauthorised label removal), feature 4 (action levels)."""
import json

import pytest

from conftest import FakeBus, make_ctx
from helpers import FUTURE, MERGE, approve, new_proposal
from ion import proposals
from ion.analyze import policy, run_analyze
from ion.approve import core, sync_approvals
from ion.collect import run_collect
from ion.execute import executor_for
from ion.propose.engine import _refresh_existing, gather_candidates, plan_for
from ion.util import Refused


def setup(ctx):
    run_collect(ctx)
    return new_proposal(ctx, "merge", MERGE)


def cand(subject, score=0.8):
    return {"kind": "merge", "key": "merge:alpha-cli>alpha-core", "subject": subject, "score": score, "evidence": {}}


def test_issue_body_carries_the_plan_hash(ctx):
    p = setup(ctx)
    body = ctx.gh.issues[p["issue_number"]]["body"]
    assert f"plan-sha256: {p['plan_hash']}" in body and len(p["plan_hash"]) == 64
    assert proposals.get(ctx.db, p["id"])["state"]["hash_log"][-1][1] == p["plan_hash"]


def test_hash_changes_with_what_would_run(ctx):
    run_collect(ctx)
    a = plan_for(ctx, "merge", MERGE).fingerprint()
    assert a == plan_for(ctx, "merge", dict(MERGE)).fingerprint()
    assert a != plan_for(ctx, "merge", {**MERGE, "prefix": "cli"}).fingerprint()


def test_approval_counts_for_the_displayed_plan(ctx):
    p = setup(ctx)
    ctx.gh.add_label_event(p["issue_number"], "ion:approved", "maint")
    sync_approvals(ctx)
    row = ctx.db.one("select * from approvals where proposal=?", (p["id"],))
    assert row["plan_hash"] == p["plan_hash"] and row["valid"] == 1
    assert proposals.get(ctx.db, p["id"])["status"] == "approved"


def test_replan_invalidates_the_approval_and_clears_the_label(ctx):
    p = setup(ctx)
    ctx.gh.add_label_event(p["issue_number"], "ion:approved", "maint")
    ctx.gh.issues[p["issue_number"]]["labels"].append("ion:approved")
    sync_approvals(ctx)
    assert proposals.get(ctx.db, p["id"])["status"] == "approved"
    old_hash = p["plan_hash"]
    ctx.gh.writes.clear()
    res = {"replanned": [], "updated": []}
    _refresh_existing(ctx, proposals.get(ctx.db, p["id"]), cand({**MERGE, "prefix": "cli"}), res)    # the plan changes
    q = proposals.get(ctx.db, p["id"])
    assert res["replanned"] == [p["id"]] and q["status"] == "awaiting-approval" and q["plan_hash"] != old_hash
    # the label came off the issue (one DELETE), and the new hash is displayed
    assert len(ctx.gh.writes_to("DELETE", "/labels/ion:approved")) == 1
    assert "ion:approved" not in ctx.gh.issues[p["issue_number"]]["labels"]
    assert f"plan-sha256: {q['plan_hash']}" in ctx.gh.issues[p["issue_number"]]["body"]
    # the old approval no longer counts, even though the row is still on record
    assert core.evaluate(ctx, q)["state"] == "awaiting"
    with pytest.raises(Refused, match="no verified approval"):
        executor_for(ctx, "merge").apply(q, dry_run=False)
    sync_approvals(ctx)
    assert proposals.get(ctx.db, p["id"])["status"] == "awaiting-approval"
    # approving the new plan works
    ctx.gh.add_label_event(p["issue_number"], "ion:approved", "maint", ts="2101-01-01T00:00:00Z")
    sync_approvals(ctx)
    assert proposals.get(ctx.db, p["id"])["status"] == "approved"


def test_approval_for_an_earlier_hash_does_not_count_even_if_label_stays(ctx):
    p = setup(ctx)
    core.record(ctx.db, p["id"], "github_label", "maint", "approve", FUTURE, "x1", True, plan_hash="0" * 64)
    ev = core.evaluate(ctx, proposals.get(ctx.db, p["id"]))
    assert ev["state"] == "awaiting" and "earlier plan" in ev["reason"]


def test_approval_before_any_plan_was_displayed_does_not_count(ctx):
    p = setup(ctx)
    ctx.gh.add_label_event(p["issue_number"], "ion:approved", "maint", ts="2001-01-01T00:00:00Z")
    sync_approvals(ctx)
    assert ctx.db.one("select plan_hash from approvals where proposal=?", (p["id"],))["plan_hash"] is None
    assert proposals.get(ctx.db, p["id"])["status"] == "awaiting-approval"


def test_approval_binds_to_hash_shown_at_the_time_not_the_current_one(ctx):
    p = setup(ctx)
    h1 = p["plan_hash"]
    ctx.db.set_state(p["id"], hash_log=[["2026-01-01T00:00:00Z", h1], ["2026-01-05T00:00:00Z", "b" * 64]])
    assert proposals.hash_at(proposals.get(ctx.db, p["id"]), "2026-01-03T00:00:00Z") == h1
    assert proposals.hash_at(proposals.get(ctx.db, p["id"]), "2026-01-06T00:00:00Z") == "b" * 64
    assert proposals.hash_at(proposals.get(ctx.db, p["id"]), "2025-12-31T00:00:00Z") is None


def bus_ctx(tmp_path, world, bus):
    return make_ctx(tmp_path, world, {"approval_sources": ["divan_bus"], "bus": {
        "client": "/x/divan.py", "python": "/x/py", "seat": "ion", "maintainer_seats": ["saga-divan"], "notify": "saga-divan"}}, bus=bus)


def msg(seq, sender, body):
    return {"seq": seq, "id": f"id{seq}", "from": sender, "to": ["ion"], "body": body, "ts": FUTURE, "kind": "msg", "enc": None}


def test_bus_approval_may_name_the_plan_hash(tmp_path, home, world):
    bus = FakeBus()
    ctx = bus_ctx(tmp_path, world, bus)
    p = setup(ctx)
    bus.inbox = [msg(1, "saga-divan", f"ion approve {p['id']} plan-sha256:{p['plan_hash'][:12]}")]
    sync_approvals(ctx)
    assert proposals.get(ctx.db, p["id"])["status"] == "approved"


def test_bus_approval_with_unknown_or_old_hash_is_ignored(tmp_path, home, world):
    bus = FakeBus()
    ctx = bus_ctx(tmp_path, world, bus)
    p = setup(ctx)
    bus.inbox = [msg(1, "saga-divan", f"ion approve {p['id']} deadbeefdead")]
    r = sync_approvals(ctx)
    assert proposals.get(ctx.db, p["id"])["status"] == "awaiting-approval"
    assert any("never displayed" in why for _, _, why in r["ignored"])


def test_bus_message_from_ions_own_seat_is_ignored(tmp_path, home, world):
    bus = FakeBus()
    ctx = bus_ctx(tmp_path, world, bus)
    ctx.cfg["bus"]["maintainer_seats"] = ["saga-divan", "ion"]
    p = setup(ctx)
    bus.inbox = [msg(1, "ion", f"ion approve {p['id']}")]
    sync_approvals(ctx)
    assert proposals.get(ctx.db, p["id"])["status"] == "awaiting-approval"


# ---- A3: a decision label from a non-maintainer is taken off again --------------------------------

def test_non_maintainer_label_is_removed_with_one_call_and_no_comment(ctx):
    p = setup(ctx)
    n = p["issue_number"]
    ctx.gh.issues[n]["labels"].append("ion:approved")
    ctx.gh.add_label_event(n, "ion:approved", "mallory")
    ctx.gh.writes.clear()
    r = sync_approvals(ctx)
    assert r["ignored"]
    removals = ctx.gh.writes_to("DELETE", "/labels/ion:approved")
    assert len(removals) == 1 and "ion:approved" not in ctx.gh.issues[n]["labels"]
    assert not [w for w in ctx.gh.writes if "/comments" in w[1]]                    # never a comment
    ctx.gh.writes.clear()
    sync_approvals(ctx)                                                              # the label is gone: nothing more to do
    assert not ctx.gh.writes_to("DELETE", "/labels/ion:approved")
    assert proposals.get(ctx.db, p["id"])["status"] == "awaiting-approval"


def test_maintainer_label_is_left_alone(ctx):
    p = setup(ctx)
    ctx.gh.issues[p["issue_number"]]["labels"].append("ion:approved")
    ctx.gh.add_label_event(p["issue_number"], "ion:approved", "maint")
    ctx.gh.writes.clear()
    sync_approvals(ctx)
    assert not ctx.gh.writes_to("DELETE", "/labels/ion")


def test_non_maintainer_hold_and_reject_labels_are_removed_too(ctx):
    p = setup(ctx)
    for lab in ("ion:rejected", "ion:hold"):
        ctx.gh.issues[p["issue_number"]]["labels"].append(lab)
        ctx.gh.add_label_event(p["issue_number"], lab, "mallory")
    ctx.gh.writes.clear()
    sync_approvals(ctx)
    assert len(ctx.gh.writes_to("DELETE", "/labels/ion:rejected")) == 1 and len(ctx.gh.writes_to("DELETE", "/labels/ion:hold")) == 1


def test_no_label_removal_while_paused(ctx):
    p = setup(ctx)
    ctx.gh.add_label_event(p["issue_number"], "ion:approved", "mallory")
    ctx.cfg["paused"] = True
    ctx.gh.writes.clear()
    r = sync_approvals(ctx)
    assert not ctx.gh.writes and proposals.get(ctx.db, p["id"])["status"] == "awaiting-approval"


# ---- feature 4: action level per policy --------------------------------------------------------

def test_action_for_precedence():
    from ion.config import load_config
    cfg = load_config(None, {"org": "o", "auto_apply": ["dependabot_alerts"],
                             "policy_actions": {"branch_protection": "fix", "required_files": "log"}})
    assert policy.action_for(cfg, None, "branch_protection") == "fix"
    assert policy.action_for(cfg, None, "required_file:README.md") == "log"
    assert policy.action_for(cfg, None, "dependabot_alerts") == "fix"          # legacy auto_apply still works
    assert policy.action_for(cfg, None, "secret_scanning") == "issue"          # default is to ask
    assert policy.action_for(cfg, {"policy_actions": {"branch_protection": "log"}}, "branch_protection") == "log"


def test_opt_in_and_opt_out_strategies():
    from ion.config import load_config
    out = load_config(None, {"org": "o"})
    assert policy.governed(out, None, "r") and not policy.governed(out, {"enabled": False}, "r")
    inn = load_config(None, {"org": "o", "policy_strategy": "opt-in", "policy_repos": ["listed"]})
    assert not policy.governed(inn, None, "r") and policy.governed(inn, {"enabled": True}, "r")
    assert policy.governed(inn, None, "listed") and not policy.governed(inn, {"enabled": False}, "listed")


def analysed(ctx):
    run_collect(ctx)
    return run_analyze(ctx)


def policy_keys(a):
    return {(r, f["class"], f["action"]) for r, fs in a["policy"].items() for f in fs if f["status"] == "drift"}


def test_log_level_is_recorded_but_never_proposed(ctx):
    ctx.cfg["policy_actions"] = {"branch_protection": "log", "dependabot_alerts": "issue", "secret_scanning": "fix"}
    a = analysed(ctx)
    keys = policy_keys(a)
    assert ("alpha-core", "branch_protection", "log") in keys
    from ion.collect.snapshot import latest_snapshots
    snaps = latest_snapshots(ctx.db)[0]
    kinds = {c["key"] for c in gather_candidates(ctx, a, snaps)}
    assert "policy:branch_protection:alpha-core" not in kinds                      # logged only
    assert not any(k.startswith("policy:branch_protection") for k in kinds)


def test_issue_level_needs_approval_fix_level_does_not(ctx):
    run_collect(ctx)
    ctx.cfg["policy_actions"] = {"dependabot_alerts": "issue", "secret_scanning": "fix"}
    ex = executor_for(ctx, "policy_fix")
    p1 = new_proposal(ctx, "policy_fix", {"class": "dependabot_alerts", "repo": "old-tool"})
    with pytest.raises(Refused):
        ex.apply(p1, dry_run=False)
    p2 = new_proposal(ctx, "policy_fix", {"class": "secret_scanning", "repo": "alpha-core"})
    assert ex.apply(p2, dry_run=False)["basis"] == "allowlisted policy_fix"


def test_repo_can_opt_out_or_change_its_own_level(ctx):
    run_collect(ctx)
    snap = json.loads(ctx.db.one("select blob from snapshots where repo='alpha-core'")["blob"])
    snap["override_text"] = "enabled: false\n"
    ctx.db.x("update snapshots set blob=? where repo='alpha-core'", (json.dumps(snap),))
    a = run_analyze(ctx)
    assert a["policy"]["alpha-core"] == []                                         # opted out: no findings at all
    snap["override_text"] = "policy_actions: {branch_protection: log}\n"
    ctx.db.x("update snapshots set blob=? where repo='alpha-core'", (json.dumps(snap),))
    a = run_analyze(ctx)
    assert ("alpha-core", "branch_protection", "log") in policy_keys(a)


def test_opted_out_repo_cannot_be_fixed_through_the_allowlist(ctx):
    run_collect(ctx)
    ctx.cfg["policy_actions"] = {"dependabot_alerts": "fix"}
    snap = json.loads(ctx.db.one("select blob from snapshots where repo='old-tool'")["blob"])
    snap["override_text"] = "enabled: false\n"
    ctx.db.x("update snapshots set blob=? where repo='old-tool'", (json.dumps(snap),))
    p = new_proposal(ctx, "policy_fix", {"class": "dependabot_alerts", "repo": "old-tool"})
    with pytest.raises(Refused):
        executor_for(ctx, "policy_fix").apply(p, dry_run=False)


def test_archive_is_never_automatic(ctx):
    run_collect(ctx)
    ctx.cfg["policy_actions"] = {k: "fix" for k in ("branch_protection", "dependabot_alerts", "secret_scanning", "required_files")}
    ctx.cfg["auto_apply"] = ["branch_protection", "dependabot_alerts", "secret_scanning", "health_fix"]
    p = new_proposal(ctx, "archive", {"repo": "old-tool"})
    with pytest.raises(Refused, match="no verified approval"):
        executor_for(ctx, "archive").apply(p, dry_run=False)
    assert "old-tool" not in ctx.gh.archived
