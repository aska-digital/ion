import json
from pathlib import Path

from conftest import make_ctx
from ion.collect import run_collect, github
from ion.collect.snapshot import latest_snapshots
from ion.ghclient import GHError


def test_budget_resumes_and_completes_one_generation(tmp_path, home, world):
    ctx = make_ctx(tmp_path, world, {"budget": {"graphql_points_per_run": 1}})
    ctx.sync_mirrors = False
    first = run_collect(ctx)
    second = run_collect(ctx)
    assert first["partial"] and not second["partial"]
    assert first["run_id"] == second["run_id"]
    snaps, generation = latest_snapshots(ctx.db)
    assert len(snaps) == 6 and "old-tool" in snaps
    assert generation == first["run_id"]
    assert ctx.db.kv_get("collect_checkpoint:acme") is None


def test_error_page_retains_prior_snapshots_and_cursor(tmp_path, home, world):
    ctx = make_ctx(tmp_path, world)
    ctx.sync_mirrors = False
    good = ctx.gh.pages[1]
    ctx.gh.pages[1] = {"data": None, "errors": [{"message": "timed out"}]}
    first = run_collect(ctx)
    assert first["partial"] and first["repos"] == 3 and first["warnings"]
    assert len(ctx.db.q("select * from snapshots")) == 3
    ctx.gh.pages[1] = good
    second = run_collect(ctx)
    assert not second["partial"] and second["run_id"] == first["run_id"]
    assert len(latest_snapshots(ctx.db)[0]) == 6


def test_transient_and_persistent_errors_keep_page_progress():
    pages = [json.loads(p.read_text()) for p in sorted((Path(__file__).parent / "fixtures").glob("graphql_org_page*.json"))]
    class GH:
        def __init__(self, fail_forever=False): self.calls = 0; self.fail_forever = fail_forever
        def graphql(self, query, variables):
            self.calls += 1
            if self.calls == 1: return pages[0]
            if self.calls == 2 or self.fail_forever: raise GHError("HTTP 502", 502)
            return pages[1]
    accepted, delays = [], []
    col = github.collect_org(GH(), "acme", 500, on_page=lambda p,c: accepted.extend(p), sleep=delays.append)
    assert not col.partial and len(accepted) == 6 and delays == [0.5]
    accepted.clear()
    col = github.collect_org(GH(True), "acme", 500, on_page=lambda p,c: accepted.extend(p), sleep=lambda _: None)
    assert col.partial and len(accepted) == 3 and col.after and col.warnings


def test_missing_page_info_and_rate_limit_are_partial():
    class GH:
        def __init__(self, body): self.body = body; self.calls = 0
        def graphql(self, query, variables): self.calls += 1; return self.body
    missing = github.collect_org(GH({"data": {"organization": {"repositories": {"nodes": []}}}}), "acme", 500)
    assert missing.partial and missing.warnings
    gh = GH({"data": None, "errors": [{"type": "RATE_LIMITED", "message": "slow down"}]})
    result = github.collect_org(gh, "acme", 500, sleep=lambda _: None)
    assert result.partial and gh.calls == 3 and result.warnings
