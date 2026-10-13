import json

import pytest

from conftest import FakeGH, make_ctx
from mimar import proposals
from mimar.analyze import run_analyze
from mimar.config import load_config
from mimar.propose import scoring, synth
from mimar.propose.engine import gather_candidates, run_propose
from mimar.propose.llm import ClaudeCLI, LLMError, NoneLLM, extract_json, make_llm
from mimar.collect import run_collect


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
    from mimar.util import parse_ts
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
    from mimar.collect.snapshot import latest_snapshots
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
    assert "git subtree add" in issue["body"] and "mimar:approved" in issue["body"]
    assert "mimar:proposal" in issue["labels"] and "kind:merge" in issue["labels"]
    assert not ctx.gh.writes_to("POST", "/comments")           # never any comment


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

    def fake_run(argv, input=None, capture_output=None, text=None, timeout=None):
        calls["argv"], calls["input"] = argv, input
        class P: returncode = 0; stdout = json.dumps({"result": '{"x": 1}'}); stderr = ""
        return P()
    monkeypatch.setattr("mimar.propose.llm.subprocess.run", fake_run)
    c = load_config(None, {"org": "o", "llm": {"backend": "claude_cli"}})
    llm = make_llm(c)
    assert isinstance(llm, ClaudeCLI) and llm.complete_json("PROMPT", structural=True) == {"x": 1}
    assert calls["argv"] == ["claude", "-p", "--output-format", "json", "--model", "sonnet"] and calls["input"] == "PROMPT"


def test_llm_call_cap_and_none_backend():
    c = load_config(None, {"org": "o", "llm": {"backend": "claude_cli", "max_calls_per_run": 0}})
    assert not make_llm(c).enabled
    assert not NoneLLM(load_config(None, {"org": "o"})).enabled
    with pytest.raises(LLMError):
        NoneLLM(load_config(None, {"org": "o"})).complete_json("x")
