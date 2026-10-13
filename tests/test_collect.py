import json

from conftest import FakeGH, make_ctx
from mimar.collect import github, run_collect
from mimar.collect.snapshot import latest_snapshots


def test_collect_snapshots_and_mirrors(ctx):
    r = run_collect(ctx)
    assert r["repos"] == 6 and r["cost"] == 2
    snaps, gen = latest_snapshots(ctx.db)
    assert set(snaps) == {"alpha-core", "alpha-cli", "mono", "old-tool", "secret-sauce", "hermes-agent"}
    assert snaps["secret-sauce"]["private"] is True
    assert snaps["alpha-core"]["commits"][0]["login"] == "dev1"
    assert r["mirrors"]["alpha-core"] == "cloned"
    assert "hermes-agent" not in r["mirrors"] or r["mirrors"].get("hermes-agent") is None  # excluded/forks never mirrored
    assert ctx.mirror_path("mono").exists()


def test_mirror_skipped_when_pushed_at_unchanged(ctx):
    run_collect(ctx)
    r2 = run_collect(ctx)
    assert r2["mirrors"]["mono"] == "skipped"


def test_budget_stops_cleanly(tmp_path, home, world):
    ctx = make_ctx(tmp_path, world, {"budget": {"graphql_points_per_run": 1}})
    r = run_collect(ctx)
    assert r["partial"] and r["repos"] == 3 and ctx.gh.graphql_calls == 1


def test_normalise_handles_missing_optional_fields():
    n = github.normalise({"name": "x", "isPrivate": True, "isFork": False, "isArchived": False,
                          "defaultBranchRef": None})
    assert "collaborators" not in n and n["commits"] == [] and n["default_branch"] is None


def test_collect_query_does_not_request_unused_collaborators():
    assert "collaborators" not in github.QUERY


def test_fetch_security_extras_logs_api_errors(caplog):
    import logging

    class BoomGH:
        def rest(self, *a, **k):
            raise RuntimeError("403 Forbidden")

    with caplog.at_level(logging.WARNING, logger="mimar"):
        assert github.fetch_security_extras(BoomGH(), "acme", {"name": "x"}) is None
    assert "security extras for x unreadable" in caplog.text


def test_override_file_parsed(tmp_path, home, world):
    from mimar.config import parse_repo_override
    ov, err = parse_repo_override("maintainers: [bob]\nignore: false\n")
    assert ov["maintainers"] == ["bob"] and err is None
    ov, err = parse_repo_override("bogus: 1\n")
    assert ov == {} and "invalid" in err
