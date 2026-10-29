import json

import pytest

from conftest import FakeGH, make_ctx
from ion import proposals
from ion.analyze import run_analyze
from ion.config import load_config
from ion.propose import scoring, synth
from ion.propose.engine import gather_candidates, run_propose
from ion.propose.llm import ClaudeCLI, LLMError, NoneLLM, extract_json, make_llm
from ion.collect import run_collect


def cfg(**th):
    return load_config(None, {"org": "o", "thresholds": th})


# ---- pure scoring ------------------------------------------------------------------

def test_hysteresis_counts_generations_not_calls():
    assert scoring.next_streak(None, None, 5) == 1
    assert scoring.next_streak(1, 5, 5) == 1          # same snapshot again: no progress
    assert scoring.next_streak(1, 5, 6) == 2
    assert not scoring.ready(1, cfg()) and scoring.ready(2, cfg())


def test_cooldown_and_rescore_exception():
    c = cfg()
    now_ = "2026-10-01T00:00:00Z"
    from ion.util import parse_ts
    t = parse_ts("2026-10-13T00:00:00Z")
    assert scoring.cooldown_blocks(now_, 0.7, 0.75, c, now=t)           # inside 60 days, small rise
    assert not scoring.cooldown_blocks(now_, 0.7, 0.86, c, now=t)       # rose by >= 0.15
    assert not scoring.cooldown_blocks("2026-06-01T00:00:00Z", 0.7, 0.7, c, now=t)   # cooldown over
    assert not scoring.cooldown_blocks(None, None, 0.9, c)


def test_merge_direction_folds_smaller_into_larger():
    big, small = {"name": "big", "size": 5000, "commits": [1] * 50}, {"name": "small", "size": 10, "commits": [1]}
    assert scoring.merge_direction(big, small) == ("small", "big")
    assert scoring.merge_direction(small, big) == ("small", "big")


def test_replan_rules():
    c = cfg()
    assert scoring.replan_needed("a", "b", 0.7, 0.7, c)
    assert scoring.replan_needed("a", "a", 0.6, 0.76, c)
    assert not scoring.replan_needed("a", "a", 0.6, 0.7, c)


# ---- candidates -> proposals ------------------------------------------------------------

def analysed(ctx):
    run_collect(ctx)
    return run_analyze(ctx)


def test_candidates_cover_merge_split_archive_policy(ctx):
    a = analysed(ctx)
    from ion.collect.snapshot import latest_snapshots
    snaps, _ = latest_snapshots(ctx.db)
    keys = {c["key"] for c in gather_candidates(ctx, a, snaps)}
    assert any(k.startswith("merge:") and "alpha-core" in k for k in keys)
    assert "split:mono:engine+engine_utils" in keys or "split:mono:site+site_utils" in keys
    assert "archive:old-tool" in keys
    assert "policy:branch_protection:alpha-core" in keys
    assert not any("secret-sauce" in k and k.startswith("policy:branch") for k in keys)   # blocked_by_plan is not drift


def test_hysteresis_in_engine_then_issue_created(ctx):
    analysed(ctx)
    r1 = run_propose(ctx)
    assert r1["created"] and all(c["kind"] == "policy_fix" for c in [{"kind": proposals.get(ctx.db, x["id"])["kind"]} for x in r1["created"]])
    assert not ctx.db.q("select 1 from proposals where kind='merge'")
    r1b = run_propose(ctx)                       # same snapshot: still streak 1
    assert not ctx.db.q("select 1 from proposals where kind='merge'")
    run_collect(ctx); run_analyze(ctx)           # second snapshot generation
    r2 = run_propose(ctx)
    kinds = {r["kind"] for r in ctx.db.q("select kind from proposals")}
    assert {"merge", "split", "archive", "policy_fix"} <= kinds
    merge = proposals.view(ctx.db.one("select * from proposals where kind='merge'"))
    assert merge["subject"]["source"] in ("alpha-core", "alpha-cli") and merge["issue_number"]
    issue = ctx.gh.issues[merge["issue_number"]]
    assert "git subtree add" in issue["body"] and "ion:approved" in issue["body"]
    assert "ion:proposal" in issue["labels"] and "kind:merge" in issue["labels"]
    assert not ctx.gh.writes_to("POST", "/comments")           # never any comment


def test_done_health_fix_with_open_pr_is_not_reproposed(ctx):
    analysed(ctx); run_propose(ctx)
    run_collect(ctx); run_analyze(ctx); run_propose(ctx)
    rows = ctx.db.q("select * from proposals where kind='health_fix'")
    assert rows, "the fixture world should produce a health_fix proposal"
    p = proposals.view(rows[0])
    repo = p["subject"]["repo"]
    # the executor finished and its PR is still open, waiting for a maintainer merge
    proposals.set_status(ctx.db, p["id"], "done")
    ctx.db.set_state(p["id"], pr_number=41)
    run_collect(ctx); run_analyze(ctx)
    r = run_propose(ctx)
    assert len(ctx.db.q("select 1 from proposals where kind='health_fix'")) == len(rows)
    assert any("open" in str(c.get("state")) for c in r["candidates"]
               if c["key"] == p["key"]), r["candidates"]
    # once that PR is no longer open, a later generation may propose again
    ctx.gh.merged.add((repo, 41))
    run_collect(ctx); run_analyze(ctx); run_propose(ctx)
    assert len(ctx.db.q("select 1 from proposals where kind='health_fix'")) > len(rows)


def test_dry_run_writes_nothing(ctx):
    analysed(ctx)
    before = len(ctx.gh.writes)
    r = run_propose(ctx, dry_run=True)
    assert r["candidates"] and len(ctx.gh.writes) == before
    assert not ctx.db.q("select 1 from proposals") and not ctx.db.q("select 1 from candidates")


def test_rejection_cooldown_suppresses_then_rescore_reopens(ctx):
    analysed(ctx); run_propose(ctx)
    run_collect(ctx); run_analyze(ctx); run_propose(ctx)
    arch = proposals.view(ctx.db.one("select * from proposals where kind='archive'"))
    ctx.db.x("update proposals set status='rejected', rejected_at=?, rejected_score=? where id=?",
             ("2999-01-01T00:00:00Z", arch["score"], arch["id"]))
    run_collect(ctx); run_analyze(ctx)
    r = run_propose(ctx)
    assert f"archive:old-tool" in r["suppressed"]
    assert len(ctx.db.q("select 1 from proposals where kind='archive'")) == 1
    ctx.db.x("update proposals set rejected_score=? where id=?", (arch["score"] - 0.2, arch["id"]))
    run_collect(ctx); run_analyze(ctx)
    r = run_propose(ctx)
    assert len(ctx.db.q("select 1 from proposals where kind='archive'")) == 2


def test_replan_resets_approval_epoch(ctx):
    analysed(ctx); run_propose(ctx)
    run_collect(ctx); run_analyze(ctx); run_propose(ctx)
    m = proposals.view(ctx.db.one("select * from proposals where kind='merge'"))
    ctx.db.x("update proposals set status='approved', epoch_score=? where id=?", (m["score"] - 0.3, m["id"]))
    run_collect(ctx); run_analyze(ctx)
    r = run_propose(ctx)
    assert m["id"] in r["replanned"]
    assert proposals.get(ctx.db, m["id"])["status"] == "awaiting-approval"


# ---- synthesis + LLM ----------------------------------------------------------------

class StubLLM:
    def __init__(self, out=None, raises=False, structural_seen=None):
        self.out, self.raises, self.calls, self.enabled = out, raises, 0, True
        self.seen = []

    def complete_json(self, prompt, structural=False):
        self.calls += 1
        self.seen.append((prompt, structural))
        if self.raises:
            raise LLMError("boom")
        return self.out


CAND = {"key": "merge:a>b", "kind": "merge", "score": 0.8, "subject": {"source": "a", "target": "b", "prefix": "a", "visibility_conflict": False},
        "evidence": {"signals": {"xref": 1.0}}}


def good(**kw):
    d = {"title": "merge a into b", "summary": "s", "rationale": "r", "risks": ["x"], "migration_plan": ["bogus"], "rollback_plan": ["y"],
         "blast_radius": "small", "confidence": 0.99, "recommend": "propose"}
    d.update(kw)
    return d


def test_synth_valid_llm_output_may_only_lower_confidence():
    out = synth.synthesize(CAND, ["real step"], StubLLM(good()))
    assert out["source"] == "llm" and out["confidence"] <= 0.8 and out["migration_plan"] == ["real step"]
    out = synth.synthesize(CAND, ["real step"], StubLLM(good(confidence=0.2, recommend="hold")))
    assert out["confidence"] == 0.2 and out["recommend"] == "hold"


def test_synth_falls_back_on_bad_schema_or_failure():
    assert synth.synthesize(CAND, ["p"], StubLLM({"title": "only"}))["source"] == "template"
    assert synth.synthesize(CAND, ["p"], StubLLM(raises=True))["source"] == "template"
    assert synth.synthesize(CAND, ["p"], None)["source"] == "template"


def test_synth_prompt_treats_repo_text_as_data_and_uses_structural_model_for_merge():
    s = StubLLM(good())
    synth.synthesize(CAND, ["p"], s)
    assert "untrusted" in s.seen[0][0] and s.seen[0][1] is True


def test_visibility_conflict_flagged_in_template():
    c = {**CAND, "subject": {**CAND["subject"], "visibility_conflict": True}}
    t = synth.template(c, [])
    assert "visibility decision" in t["title"] and "VISIBILITY" in t["risks"][0]


def test_template_text_refreshes_until_someone_approves(ctx):
    from helpers import approve
    analysed(ctx)
    run_propose(ctx)
    p = proposals.view(ctx.db.one("select * from proposals where kind='policy_fix'"))
    stale = {**p["text"], "risks": ["STALE WORDING"], "source": "template"}
    ctx.db.set_state(p["id"], text=stale)
    run_collect(ctx); run_analyze(ctx); run_propose(ctx)
    p = proposals.get(ctx.db, p["id"])
    assert p["text"]["risks"] != ["STALE WORDING"]          # unapproved: wording follows the template
    ctx.db.set_state(p["id"], text=stale)
    approve(ctx, p)
    run_collect(ctx); run_analyze(ctx); run_propose(ctx)
    p = proposals.get(ctx.db, p["id"])
    assert p["text"]["risks"] == ["STALE WORDING"]          # approved: what was shown stays shown


def test_policy_fix_risks_match_the_policy_class():
    def cand(cls):
        return {"kind": "policy_fix", "subject": {"class": cls, "repo": "r"}, "score": 1.0,
                "evidence": {"detail": "drift"}}
    bp = synth.template(cand("branch_protection"), [])["risks"]
    assert any("direct pushes" in r for r in bp)
    for cls, needle in (("dependabot_alerts", "alert"), ("secret_scanning", "secret")):
        risks = synth.template(cand(cls), [])["risks"]
        assert any(needle in r.lower() for r in risks), (cls, risks)
        assert not any("direct pushes" in r for r in risks), (cls, risks)


def test_llm_drop_creates_no_issue(ctx):
    analysed(ctx)
    ctx.llm = StubLLM(good(recommend="drop"))
    run_propose(ctx)
    run_collect(ctx); run_analyze(ctx)
    r = run_propose(ctx)
    assert r["dropped"] and not any(x["kind"] in ("merge", "split", "archive") for x in r["created"])
    assert ctx.llm.calls >= 1 and not [i for i in ctx.gh.issues.values() if "merge" in i["title"]]


def test_extract_json_and_claude_cli_backend(monkeypatch):
    assert extract_json('noise ```json\n{"a": 1}\n``` tail') == {"a": 1}
    assert extract_json('here {"a": {"b": 2}} ok') == {"a": {"b": 2}}
    calls = {}

    def fake_run(argv, input=None, capture_output=None, text=None, timeout=None, env=None):
        calls["argv"], calls["input"] = argv, input
        class P: returncode = 0; stdout = json.dumps({"result": '{"x": 1}'}); stderr = ""
        return P()
    monkeypatch.setattr("ion.propose.llm.subprocess.run", fake_run)
    c = load_config(None, {"org": "o", "llm": {"backend": "claude_cli"}})
    llm = make_llm(c)
    assert isinstance(llm, ClaudeCLI) and llm.complete_json("PROMPT", structural=True) == {"x": 1}
    assert calls["argv"] == ["claude", "-p", "--output-format", "json", "--model", "sonnet"] and calls["input"] == "PROMPT"


def test_claude_cli_does_not_inherit_ambient_api_key(monkeypatch):
    # ANTHROPIC_API_KEY belongs to the anthropic_api backend. Inherited by the CLI it
    # overrides the seat login, and a stale key makes every call exit 1 (seen live).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stale")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stale")
    monkeypatch.setenv("ION_TEST_SENTINEL", "kept")
    seen = {}

    def fake_run(argv, input=None, capture_output=None, text=None, timeout=None, env=None):
        seen["env"] = env
        class P: returncode = 0; stdout = json.dumps({"result": '{"x": 1}'}); stderr = ""
        return P()
    monkeypatch.setattr("ion.propose.llm.subprocess.run", fake_run)
    llm = make_llm(load_config(None, {"org": "o", "llm": {"backend": "claude_cli"}}))
    assert llm.complete_json("PROMPT") == {"x": 1}
    assert seen["env"] is not None, "env must be set explicitly, not inherited"
    assert "ANTHROPIC_API_KEY" not in seen["env"] and "ANTHROPIC_AUTH_TOKEN" not in seen["env"]
    assert seen["env"]["ION_TEST_SENTINEL"] == "kept"


def test_llm_call_cap_and_none_backend():
    c = load_config(None, {"org": "o", "llm": {"backend": "claude_cli", "max_calls_per_run": 0}})
    assert not make_llm(c).enabled
    assert not NoneLLM(load_config(None, {"org": "o"})).enabled
    with pytest.raises(LLMError):
        NoneLLM(load_config(None, {"org": "o"})).complete_json("x")


def test_merge_pair_key_survives_direction_flip(ctx, monkeypatch):
    a = analysed(ctx)
    from ion.collect.snapshot import latest_snapshots
    snaps, _ = latest_snapshots(ctx.db)
    before = {c["key"] for c in gather_candidates(ctx, a, snaps) if c["kind"] == "merge"}
    original = scoring.merge_direction
    monkeypatch.setattr(scoring, "merge_direction", lambda x, y: tuple(reversed(original(x, y))))
    after = {c["key"] for c in gather_candidates(ctx, a, snaps) if c["kind"] == "merge"}
    assert before and before == after


def test_canonical_merge_key_reuses_legacy_directional_proposal(ctx):
    from helpers import MERGE, new_proposal
    from ion.propose.engine import _latest_for_key
    p = new_proposal(ctx, "merge", MERGE)
    ctx.db.x("update proposals set key=? where id=?", (f"merge:{MERGE['source']}>{MERGE['target']}", p["id"]))
    key = "merge:" + "|".join(sorted((MERGE["source"], MERGE["target"])))
    assert _latest_for_key(ctx.db, key)["id"] == p["id"]
