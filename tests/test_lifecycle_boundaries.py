"""Features 8 (staged lifecycle) and 11 (boundary rules from repo tags)."""
import json

from conftest import make_ctx
from ion import proposals
from ion.analyze import boundaries, lifecycle, run_analyze
from ion.collect import run_collect
from ion.config import load_config
from ion.execute.runner import run_execute
from ion.propose.engine import run_propose
from ion.util import iso, now
import datetime as dt


def ago(days):
    return iso(now() - dt.timedelta(days=days))


def snap(name="r", days=0, **kw):
    return {"name": name, "pushed_at": ago(days), "archived": False, "fork": False, "topics": [], **kw}


CFG = load_config(None, {"org": "o"})        # stale_days 180, dormant_days 90


# ---- lifecycle ---------------------------------------------------------------------------------

def test_stage_progression_and_reset_by_push():
    st = lambda d, **kw: lifecycle.stage_of("r", snap(days=d, **kw), CFG)
    assert st(10) == "active" and st(179) == "active"
    assert st(200) == "stale" and st(269) == "stale"
    assert st(300) == "archive-proposed"
    assert st(0) == "active"                                  # a push anywhere in the history resets the stage
    assert lifecycle.stage_of("r", snap(archived=True), CFG) == "archived"


def test_frozen_by_topic_or_config():
    assert lifecycle.stage_of("r", snap(days=999, topics=["ion-frozen"]), CFG) == "frozen"
    cfg = load_config(None, {"org": "o", "lifecycle": {"frozen": ["r"]}})
    assert lifecycle.stage_of("r", snap(days=999), cfg) == "frozen"
    assert lifecycle.archive_candidates({"r": snap(days=999, topics=["ion-frozen"])}, [], CFG) == []


def test_only_the_last_stage_qualifies_for_a_proposal():
    snaps = {"fresh": snap("fresh", 10), "stale": snap("stale", 200), "gone": snap("gone", 400)}
    out = {c["repo"]: c for c in lifecycle.archive_candidates(snaps, [], CFG)}
    assert "fresh" not in out
    assert out["stale"]["stage"] == "stale" and not out["stale"]["qualifies"]
    assert any("becomes eligible" in r for r in out["stale"]["reasons"])
    assert out["gone"]["stage"] == "archive-proposed" and out["gone"]["qualifies"]


def test_dormant_days_is_configurable():
    cfg = load_config(None, {"org": "o", "lifecycle": {"dormant_days": 400}})
    assert lifecycle.stage_of("r", snap(days=300), cfg) == "stale"


def test_analysis_reports_stages(collected):
    a = run_analyze(collected)
    assert a["lifecycle"]["old-tool"] == "archive-proposed" and a["lifecycle"]["alpha-core"] == "active"
    assert "hermes-agent" not in a["lifecycle"]


def test_archive_is_proposed_but_never_executed_without_approval(ctx):
    run_collect(ctx); run_analyze(ctx); run_propose(ctx)
    run_collect(ctx); run_analyze(ctx); run_propose(ctx)
    arch = proposals.view(ctx.db.one("select * from proposals where kind='archive'"))
    assert arch["subject"]["repo"] == "old-tool" and arch["status"] == "awaiting-approval"
    r = run_execute(ctx, apply=True)
    assert "old-tool" not in ctx.gh.archived and not ctx.gh.writes_to("PATCH", "/repos/acme/old-tool")
    assert any(arch["id"] in s for s in r["skipped"])


# ---- boundaries --------------------------------------------------------------------------------

def bcfg(**kw):
    return load_config(None, {"org": "o", "boundaries": {"rules": [{"from": "scope:app", "allow": ["scope:app", "layer:shared"]}], **kw}})


def pair(a, b, a_refs_b=0.0, b_refs_a=0.0):
    return {"a": a, "b": b, "a_refs_b": a_refs_b, "b_refs_a": b_refs_a, "evidence": {"a_refs_b": ["manifest"], "b_refs_a": []}}


def test_violation_when_dependency_target_lacks_an_allowed_tag():
    snaps = {"app": snap("app", topics=["scope:app"]), "lib": snap("lib", topics=["scope:other"]),
             "shared": snap("shared", topics=["layer:shared"]), "bare": snap("bare")}
    v = boundaries.violations(snaps, [pair("app", "lib", 1.0), pair("app", "shared", 1.0), pair("app", "bare", 1.0)], bcfg())
    assert [(x["from"], x["to"]) for x in v] == [("app", "bare"), ("app", "lib")]
    assert v[1]["rule"] == "scope:app" and "not among the allowed" in v[1]["reason"] and v[0]["reason"] == "target has no tags"


def test_weak_references_and_untagged_sources_are_not_violations():
    snaps = {"app": snap("app", topics=["scope:app"]), "lib": snap("lib", topics=["scope:other"]), "free": snap("free")}
    assert boundaries.violations(snaps, [pair("app", "lib", 0.3)], bcfg()) == []          # a README link is not a dependency
    assert boundaries.violations(snaps, [pair("free", "lib", 1.0)], bcfg()) == []          # no rule for untagged sources
    assert boundaries.violations(snaps, [pair("lib", "app", 1.0)], bcfg()) == []


def test_direction_matters():
    snaps = {"app": snap("app", topics=["scope:app"]), "lib": snap("lib", topics=["scope:other"])}
    v = boundaries.violations(snaps, [pair("lib", "app", b_refs_a=1.0)], bcfg())           # app depends on lib, written as b -> a
    assert [(x["from"], x["to"]) for x in v] == [("app", "lib")]


def test_separation_flags_different_scopes_and_mutual_prohibition():
    a, b = snap("a", topics=["scope:x"]), snap("b", topics=["scope:y"])
    assert "different scopes" in boundaries.separation(a, b, CFG)
    assert boundaries.separation(a, snap("c", topics=["scope:x"]), CFG) is None
    assert boundaries.separation(a, snap("d"), CFG) is None
    cfg = load_config(None, {"org": "o", "boundaries": {"rules": [{"from": "t:a", "allow": ["t:a"]}, {"from": "t:b", "allow": ["t:b"]}]}})
    assert "forbid" in boundaries.separation(snap("a", topics=["t:a"]), snap("b", topics=["t:b"]), cfg)


def tag(ctx, repo, topics):
    row = ctx.db.one("select blob from snapshots where repo=? order by run_id desc", (repo,))
    s = json.loads(row["blob"])
    s["topics"] = topics
    ctx.db.x("update snapshots set blob=? where repo=? and run_id=(select max(run_id) from snapshots where repo=?)",
             (json.dumps(s), repo, repo))


def test_violations_in_analysis(ctx):
    ctx.cfg["boundaries"]["rules"] = [{"from": "scope:app", "allow": ["scope:app"]}]
    run_collect(ctx)
    tag(ctx, "alpha-cli", ["scope:app"]); tag(ctx, "alpha-core", ["scope:lib"])
    a = run_analyze(ctx)
    assert any(v["from"] == "alpha-cli" and v["to"] == "alpha-core" for v in a["boundaries"])


def two_runs(ctx):
    run_collect(ctx); run_analyze(ctx); r = run_propose(ctx)
    run_collect(ctx); run_analyze(ctx)
    return run_propose(ctx)


def test_cross_scope_merge_is_blocked_not_proposed(ctx):
    # topics live in the collected snapshots: tag them in the recorded GraphQL pages
    pages = ctx.gh.pages
    for page in pages:
        for n in page["data"]["organization"]["repositories"]["nodes"]:
            t = {"alpha-cli": "scope:app", "alpha-core": "scope:lib"}.get(n["name"])
            if t:
                n["repositoryTopics"] = {"nodes": [{"topic": {"name": t}}]}
    r = two_runs(ctx)
    state = next(c["state"] for c in r["candidates"] if c["kind"] == "merge")
    assert state.startswith("blocked by boundary") and "different scopes" in state
    assert not ctx.db.q("select 1 from proposals where kind='merge'")


def test_cross_scope_merge_can_be_proposed_with_a_warning(ctx):
    ctx.cfg["boundaries"]["block_cross_scope_merge"] = False
    for page in ctx.gh.pages:
        for n in page["data"]["organization"]["repositories"]["nodes"]:
            t = {"alpha-cli": "scope:app", "alpha-core": "scope:lib"}.get(n["name"])
            if t:
                n["repositoryTopics"] = {"nodes": [{"topic": {"name": t}}]}
    two_runs(ctx)
    m = proposals.view(ctx.db.one("select * from proposals where kind='merge'"))
    assert "different scopes" in m["subject"]["cross_scope"]
    assert any(r.startswith("BOUNDARY") for r in m["text"]["risks"]) and "crosses a boundary" in m["text"]["title"]
