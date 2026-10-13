import json

from conftest import FakeBus, FakeGH, make_ctx
from helpers import FUTURE, MERGE, approve, new_proposal
from mimar import proposals
from mimar.approve import core, sync_approvals
from mimar.collect import run_collect


def setup(ctx, **kw):
    run_collect(ctx)
    return new_proposal(ctx, "merge", MERGE)


def status(ctx, p):
    return proposals.get(ctx.db, p["id"])["status"]


def test_maintainer_label_approves(ctx):
    p = setup(ctx)
    ctx.gh.add_label_event(p["issue_number"], "mimar:approved", "maint")
    r = sync_approvals(ctx)
    assert status(ctx, p) == "approved" and (p["id"], "awaiting-approval", "approved") in r["changed"]
    assert "state:approved" in str(ctx.gh.writes)


def test_non_maintainer_label_is_rejected_and_audited(ctx):
    p = setup(ctx)
    ctx.gh.add_label_event(p["issue_number"], "mimar:approved", "mallory")
    r = sync_approvals(ctx)
    assert status(ctx, p) == "awaiting-approval"
    row = ctx.db.one("select * from approvals where proposal=?", (p["id"],))
    assert row["valid"] == 0 and "not a maintainer" in row["reason"]
    assert r["ignored"]


def test_non_maintainer_cannot_reject_or_hold_either(ctx):
    p = setup(ctx)
    ctx.gh.add_label_event(p["issue_number"], "mimar:rejected", "mallory")
    ctx.gh.add_label_event(p["issue_number"], "mimar:hold", "mallory")
    sync_approvals(ctx)
    assert status(ctx, p) == "awaiting-approval"


def test_label_removed_withdraws_approval(ctx):
    p = setup(ctx)
    ctx.gh.add_label_event(p["issue_number"], "mimar:approved", "maint", ts="2099-01-01T00:00:00Z")
    sync_approvals(ctx)
    ctx.gh.add_label_event(p["issue_number"], "mimar:approved", "maint", ts="2099-01-02T00:00:00Z", event="unlabeled")
    sync_approvals(ctx)
    assert status(ctx, p) == "awaiting-approval"


def test_label_older_than_epoch_is_stale(ctx):
    p = setup(ctx)
    ctx.gh.add_label_event(p["issue_number"], "mimar:approved", "maint", ts="2001-01-01T00:00:00Z")
    sync_approvals(ctx)
    assert status(ctx, p) == "awaiting-approval"


def test_reject_sets_cooldown_fields_and_hold_pauses(ctx):
    p = setup(ctx)
    ctx.gh.add_label_event(p["issue_number"], "mimar:hold", "maint")
    sync_approvals(ctx)
    assert status(ctx, p) == "held"
    ctx.gh.add_label_event(p["issue_number"], "mimar:hold", "maint", ts="2099-02-01T00:00:00Z", event="unlabeled")
    ctx.gh.add_label_event(p["issue_number"], "mimar:rejected", "maint", ts="2099-02-02T00:00:00Z")
    sync_approvals(ctx)
    row = proposals.get(ctx.db, p["id"])
    assert row["status"] == "rejected" and row["rejected_at"] and row["rejected_score"] == p["score"]


def test_quorum_needs_two_distinct_maintainers(tmp_path, home, world):
    ctx = make_ctx(tmp_path, world, {"quorum": 2, "maintainers": ["a", "b"]})
    p = setup(ctx)
    ctx.gh.add_label_event(p["issue_number"], "mimar:approved", "a")
    sync_approvals(ctx)
    assert status(ctx, p) == "awaiting-approval"
    ctx.gh.add_label_event(p["issue_number"], "mimar:approved", "b", ts="2099-01-02T00:00:00Z")
    sync_approvals(ctx)
    assert status(ctx, p) == "approved"


def test_approval_expires_after_ttl(ctx):
    p = setup(ctx)
    ctx.gh.add_label_event(p["issue_number"], "mimar:approved", "maint", ts="2026-09-20T00:00:00Z")
    ctx.db.x("update proposals set epoch='2026-09-01T00:00:00Z' where id=?", (p["id"],))
    r = sync_approvals(ctx)
    row = proposals.get(ctx.db, p["id"])
    assert row["status"] == "awaiting-approval" and row["epoch"] > "2026-10-02"
    assert (p["id"], "awaiting-approval", "expired") in r["changed"]
    sync_approvals(ctx)                      # old label is now before the new epoch: stays unapproved
    assert status(ctx, p) == "awaiting-approval"


def test_per_repo_maintainer_override(ctx):
    run_collect(ctx)
    snap = json.loads(ctx.db.one("select blob from snapshots where repo='alpha-core'")["blob"])
    snap["override_text"] = "maintainers: [bob]\n"
    ctx.db.x("update snapshots set blob=? where repo='alpha-core'", (json.dumps(snap),))
    p = new_proposal(ctx, "merge", MERGE)
    assert core.maintainers_for(ctx, p) == set()           # intersection of {bob} and {maint}
    ctx.gh.add_label_event(p["issue_number"], "mimar:approved", "maint")
    sync_approvals(ctx)
    assert status(ctx, p) == "awaiting-approval"


# ---- bus ----------------------------------------------------------------------------

def bus_ctx(tmp_path, world, bus, **o):
    cfg = {"approval_sources": ["divan_bus"], "bus": {"client": "/x/divan.py", "python": "/x/py", "seat": "mimar",
                                                         "maintainer_seats": ["saga-divan"], "notify": "saga-divan"}}
    cfg.update(o)
    return make_ctx(tmp_path, world, cfg, bus=bus)


def msg(seq, sender, body, id_=None):
    return {"seq": seq, "id": id_ or f"id{seq}", "from": sender, "to": ["mimar"], "body": body, "ts": FUTURE, "kind": "msg", "enc": None}


def test_bus_approval_from_maintainer_seat_mirrors_label(tmp_path, home, world):
    bus = FakeBus()
    ctx = bus_ctx(tmp_path, world, bus)
    p = setup(ctx)
    bus.inbox = [msg(1, "saga-divan", f"mimar approve {p['id']}")]
    sync_approvals(ctx)
    assert status(ctx, p) == "approved"
    assert any("mimar:approved" in str(w) for w in ctx.gh.writes_to("POST", "/labels"))   # mirrored onto the issue
    assert ctx.db.kv_get("bus_since") == 1


def test_bus_rejects_other_seats_unsigned_and_noise(tmp_path, home, world):
    bus = FakeBus()
    ctx = bus_ctx(tmp_path, world, bus)
    p = setup(ctx)
    bus.verified = {"id3"}
    bus.inbox = [msg(1, "random-seat", f"mimar approve {p['id']}"),          # wrong seat
                 msg(2, "saga-divan", f"mimar approve {p['id']}"),           # signature not verified
                 msg(3, "saga-divan", "please approve " + p["id"]),          # wrong format
                 msg(4, "saga-divan", "mimar approve P-9999")]               # unknown proposal
    r = sync_approvals(ctx)
    assert status(ctx, p) == "awaiting-approval"
    reasons = " ".join(x[2] for x in r["ignored"])
    assert "not a maintainer seat" in reasons or "signature" in reasons


def test_bus_reject_and_hold(tmp_path, home, world):
    bus = FakeBus()
    ctx = bus_ctx(tmp_path, world, bus)
    p = setup(ctx)
    bus.inbox = [msg(1, "saga-divan", f"mimar hold {p['id']}")]
    sync_approvals(ctx)
    assert status(ctx, p) == "held"
    bus.inbox = [msg(2, "saga-divan", f"MIMAR reject {p['id']}")]
    sync_approvals(ctx)
    assert status(ctx, p) == "rejected"


def test_mirrored_label_does_not_count_as_second_approver(tmp_path, home, world):
    bus = FakeBus()
    ctx = bus_ctx(tmp_path, world, bus, approval_sources=["github_label", "divan_bus"], quorum=2, maintainers=["maint", "mimar-bot"])
    p = setup(ctx)
    bus.inbox = [msg(1, "saga-divan", f"mimar approve {p['id']}")]
    sync_approvals(ctx)
    sync_approvals(ctx)
    assert status(ctx, p) == "awaiting-approval"      # saga-divan alone, label by the bot is ours and ignored
    ctx.gh.add_label_event(p["issue_number"], "mimar:approved", "maint", ts="2099-03-01T00:00:00Z")
    sync_approvals(ctx)
    assert status(ctx, p) == "approved"


def test_bus_down_is_reported_not_fatal(tmp_path, home, world):
    bus = FakeBus(); bus.down = True
    ctx = bus_ctx(tmp_path, world, bus)
    setup(ctx)
    r = sync_approvals(ctx)
    assert r["errors"] and "bus" in r["errors"][0]
