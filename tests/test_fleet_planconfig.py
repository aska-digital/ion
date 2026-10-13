"""Features 9 (plan-config), 10 (ordered changesets) and 12 (mimar grep / autofix)."""
import json
import re

import pytest

from conftest import make_ctx, sh
from helpers import MERGE, SPLIT, approve, new_proposal
from mimar import changesets, cli, proposals
from mimar.analyze import run_analyze
from mimar.collect import run_collect
from mimar.config import load_config
from mimar.execute import executor_for
from mimar.execute.runner import run_execute
from mimar.fleet import (Autofixer, DependabotConfig, ReadmeSkeleton, detect_ecosystems, digest, get_fixer, grep_mirrors,
                         preview, register)
from mimar.planconfig import config_diff, plan_config, render
from mimar.propose.engine import run_propose
from mimar.util import Refused, Waiting


# ---- feature 10: changesets ---------------------------------------------------------------------

def test_merge_is_three_ordered_changesets(ctx):
    run_collect(ctx)
    p = new_proposal(ctx, "merge", MERGE)
    cs = changesets.build(p)
    assert [c["id"] for c in cs] == ["import", "pointer", "archive"]
    assert [c["depends_on"] for c in cs] == [[], ["import"], ["pointer"]]
    sts = {c["id"]: c["state"] for c in changesets.states(ctx, p)}
    assert sts == {"import": "planned", "pointer": "blocked", "archive": "blocked"}
    body = ctx.gh.issues[p["issue_number"]]["body"]
    assert "## Changesets (in order)" in body and "1. [ ] **import**" in body and "after import" in body


def test_pointer_cannot_run_before_the_import_pr_lands(ctx, world):
    run_collect(ctx)
    p = approve(ctx, new_proposal(ctx, "merge", MERGE))
    ex = executor_for(ctx, "merge")
    ex.apply(p, dry_run=False)                                                         # opens the import PR (#1)
    ctx.db.set_state(p["id"], phase="pointer")                                         # force the later phase
    out = ex.apply(proposals.get(ctx.db, p["id"]), dry_run=False)
    assert out["waiting"] and "pointer must wait for import" in out["waiting"]
    assert not ctx.gh.writes_to("POST", "/repos/acme/alpha-cli/pulls")                 # nothing was opened early
    ctx.gh.merged.add(("alpha-core", 1))
    out = ex.apply(proposals.get(ctx.db, p["id"]), dry_run=False)
    assert not out["waiting"] and ctx.gh.writes_to("POST", "/repos/acme/alpha-cli/pulls")


def test_dependent_pr_names_what_it_depends_on_and_issue_shows_live_state(ctx, world):
    run_collect(ctx)
    p = approve(ctx, new_proposal(ctx, "merge", MERGE))
    ex = executor_for(ctx, "merge")
    ex.apply(p, dry_run=False)
    ctx.gh.merged.add(("alpha-core", 1))
    ex.apply(proposals.get(ctx.db, p["id"]), dry_run=False)                            # await_merge -> pointer
    ex.apply(proposals.get(ctx.db, p["id"]), dry_run=False)                            # pointer PR
    pr = ctx.gh.prs[("alpha-cli", 2)]
    assert "Depends-On: acme/alpha-core#1" in pr["body"]
    q = proposals.get(ctx.db, p["id"])
    sts = {c["id"]: (c["state"], c.get("number")) for c in changesets.states(ctx, q)}
    assert sts["import"] == ("merged", 1) and sts["pointer"] == ("open", 2) and sts["archive"][0] == "blocked"
    from mimar.propose.engine import refresh_issue
    refresh_issue(ctx, q)
    body = ctx.gh.issues[p["issue_number"]]["body"]
    assert "1. [x] **import**" in body and "[acme/alpha-core#1]" in body and "2. [ ] **pointer**" in body and "`open`" in body


def test_merged_state_is_cached_so_it_is_not_asked_again(ctx):
    run_collect(ctx)
    p = new_proposal(ctx, "merge", MERGE)
    ctx.db.set_state(p["id"], pr_number=7)
    ctx.gh.merged.add(("alpha-core", 7))
    q = proposals.get(ctx.db, p["id"])
    assert changesets.states(ctx, q)[0]["state"] == "merged"
    q = proposals.get(ctx.db, p["id"])
    ctx.gh.merged.clear()                                                              # even if the API said otherwise now
    assert changesets.states(ctx, q)[0]["state"] == "merged"


def test_split_and_move_code_changesets(ctx):
    run_collect(ctx)
    s = new_proposal(ctx, "split", SPLIT)
    assert [c["id"] for c in changesets.build(s)] == ["create", "removal"]
    m = new_proposal(ctx, "move_code", {"source": "mono", "dir": "site", "target": "old-tool", "dest_prefix": "site"})
    assert [(c["id"], c["repo"]) for c in changesets.build(m)] == [("add", "old-tool"), ("remove", "mono")]
    with pytest.raises(Waiting):
        changesets.require(ctx, m, "remove")
    assert changesets.require(ctx, m, "add").startswith("dependencies landed")


# ---- feature 12: grep ---------------------------------------------------------------------------

def test_grep_searches_mirrors_without_the_api(collected):
    before = collected.gh.graphql_calls, len(collected.gh.writes)
    ms = grep_mirrors(collected, "compute_core0", fixed=True)
    assert {m.repo for m in ms} == {"alpha-core"} and all(m.path.startswith("alpha_core/") for m in ms) and ms[0].line >= 1
    assert "compute_core0" in ms[0].text
    assert (collected.gh.graphql_calls, len(collected.gh.writes)) == before


def test_grep_filters(collected):
    all_ = grep_mirrors(collected, "import", paths=["*.py"])
    assert any(m.repo == "alpha-cli" and m.path.endswith("main.py") for m in all_)
    assert {m.repo for m in grep_mirrors(collected, "value_", repo_glob="alpha-*")} == {"alpha-core", "alpha-cli"}
    assert grep_mirrors(collected, "VALUE_UTIL_0", ignore_case=True, repo_glob="alpha-core")
    assert not grep_mirrors(collected, "VALUE_UTIL_0", repo_glob="alpha-core")
    assert len(grep_mirrors(collected, "value_", max_matches=3)) == 3
    assert not grep_mirrors(collected, "no_such_string_anywhere")


def test_grep_skips_excluded_and_archived(collected):
    assert "hermes-agent" not in {m.repo for m in grep_mirrors(collected, ".")}


def test_grep_cli(collected, capsys, monkeypatch):
    args = cli.build_parser().parse_args(["grep", "compute_core0", "-F"])
    assert cli.cmd_grep(collected, args) == 0
    out = capsys.readouterr().out
    assert "alpha-core:alpha_core/mod0.py:1:" in out and "matches in 1 repos" in out
    assert cli.cmd_grep(collected, cli.build_parser().parse_args(["grep", "zzzz-none"])) == 1


# ---- feature 12: autofix ------------------------------------------------------------------------

def test_detect_ecosystems():
    got = dict(detect_ecosystems({"pyproject.toml", "web/package.json", "web/yarn.lock", ".github/workflows/ci.yml", "Dockerfile"}))
    assert got == {"pip": "/", "npm": "/web", "docker": "/", "github-actions": "/"}
    assert detect_ecosystems({"README.md"}) == []


def test_autofix_preview_shows_diffs_and_writes_nothing(collected):
    before = len(collected.gh.writes)
    d = preview(collected, DependabotConfig())
    assert set(d) == {"alpha-core", "alpha-cli"} | ({"mono", "old-tool", "secret-sauce"} & set(d))   # repos with a manifest and no config
    assert "package-ecosystem: pip" in d["alpha-core"] and "+++ b/.github/dependabot.yml" in d["alpha-core"]
    assert len(collected.gh.writes) == before


def test_autofix_repo_filter_and_listing(collected, capsys):
    assert set(preview(collected, DependabotConfig(), "alpha-c*")) == {"alpha-core", "alpha-cli"}
    cli.cmd_autofix(collected, cli.build_parser().parse_args(["autofix"]))
    out = capsys.readouterr().out
    assert "dependabot-config" in out and "readme-skeleton" in out


def test_autofix_propose_files_one_proposal_and_opens_no_pr(collected, capsys):
    args = cli.build_parser().parse_args(["autofix", "dependabot-config", "--repo", "alpha-*", "--propose"])
    cli.cmd_autofix(collected, args)
    row = ctx_row(collected)
    p = proposals.view(row)
    assert p["status"] == "awaiting-approval" and p["subject"]["repos"] == ["alpha-cli", "alpha-core"]
    assert set(p["subject"]["diffs"]) == {"alpha-cli", "alpha-core"} and p["issue_number"]
    assert not collected.gh.writes_to("POST", "/pulls") and not collected.gh.writes_to("PUT", "/protection")
    body = collected.gh.issues[p["issue_number"]]["body"]
    assert "autofix" in body and "plan-sha256" in body and "mimar autofix dependabot-config --repo alpha-core" in body
    cli.cmd_autofix(collected, args)                                                     # same request again: no second proposal
    assert len(collected.db.q("select 1 from proposals where kind='autofix'")) == 1


def ctx_row(ctx):
    return ctx.db.one("select * from proposals where kind='autofix'")


def test_autofix_needs_approval_then_opens_prs_through_typed_requests(collected, world):
    cli.cmd_autofix(collected, cli.build_parser().parse_args(["autofix", "dependabot-config", "--repo", "alpha-*", "--propose"]))
    p = proposals.view(ctx_row(collected))
    ex = executor_for(collected, "autofix")
    with pytest.raises(Refused, match="no verified approval"):
        ex.apply(p, dry_run=False)
    assert not collected.gh.writes_to("POST", "/pulls")
    p = approve(collected, p)
    out = ex.apply(p, dry_run=False)
    assert not out["waiting"] and proposals.get(collected.db, p["id"])["status"] == "done"
    assert {w[1].split("/")[3] for w in collected.gh.writes_to("POST", "/pulls")} == {"alpha-core", "alpha-cli"}
    for repo in ("alpha-core", "alpha-cli"):
        files = sh("git", "--git-dir", str(world / f"{repo}.git"), "ls-tree", "-r", "--name-only", f"mimar/{p['id']}")
        assert ".github/dependabot.yml" in files
        assert sh("git", "--git-dir", str(world / f"{repo}.git"), "rev-parse", "main") != ""
    types = [r["type"] for r in collected.db.q("select type from write_log where type in ('push_branch','open_pr')")]
    assert types.count("push_branch") == 2 and types.count("open_pr") == 2              # every PR went through the broker


def test_autofix_pr_cap_defers_the_rest_to_the_next_run(collected, world):
    collected.cfg["writes"] = {"caps": {"open_pr": 1}}
    cli.cmd_autofix(collected, cli.build_parser().parse_args(["autofix", "dependabot-config", "--repo", "alpha-*", "--propose"]))
    p = approve(collected, proposals.view(ctx_row(collected)))
    ex = executor_for(collected, "autofix")
    out = ex.apply(p, dry_run=False)
    assert out["waiting"] and len(collected.gh.writes_to("POST", "/pulls")) == 1
    assert proposals.get(collected.db, p["id"])["status"] == "executing"
    collected.writes.reset()                                                               # next run
    out = ex.apply(proposals.get(collected.db, p["id"]), dry_run=False)
    assert not out["waiting"] and len(collected.gh.writes_to("POST", "/pulls")) == 2
    assert proposals.get(collected.db, p["id"])["status"] == "done"


def test_autofix_stops_if_the_repo_changed_since_approval(collected, world, tmp_path):
    cli.cmd_autofix(collected, cli.build_parser().parse_args(["autofix", "dependabot-config", "--repo", "alpha-core", "--propose"]))
    p = approve(collected, proposals.view(ctx_row(collected)))
    # someone adds a dependabot config (or any manifest) before the executor runs
    work = tmp_path / "w"
    sh("git", "clone", "-q", str(world / "alpha-core.git"), str(work))
    (work / "package.json").write_text("{}\n")
    sh("git", "add", "-A", cwd=work); sh("git", "commit", "-qm", "js", cwd=work); sh("git", "push", "-q", "origin", "main", cwd=work)
    with pytest.raises(RuntimeError, match="differs from the approved diff"):
        executor_for(collected, "autofix").apply(p, dry_run=False)
    assert not collected.gh.writes_to("POST", "/pulls")


def test_autofix_fleet_size_is_checked_by_the_breaker(collected):
    cli.cmd_autofix(collected, cli.build_parser().parse_args(["autofix", "dependabot-config", "--propose"]))
    p = approve(collected, proposals.view(ctx_row(collected)))
    collected.cfg["guards"] = {"max_repo_fraction": 0.2, "min_repos": 2}
    r = run_execute(collected, apply=True)
    assert r["breaker"] and not collected.gh.writes_to("POST", "/pulls")


def test_plugin_fixers_register_by_name(collected):
    @register
    class Touch(Autofixer):
        name = "touch-file"
        description = "x"
        commit_message = "t"
        pr_title = "t"
        pr_body = "t"
        def find_repos(self, ctx, repos):
            return ["old-tool"]
        def apply(self, workdir, repo, snap):
            (workdir / "TOUCHED").write_text("x\n")
    d = preview(collected, get_fixer(collected, "touch-file"))
    assert list(d) == ["old-tool"] and "+++ b/TOUCHED" in d["old-tool"]
    with pytest.raises(KeyError, match="unknown autofixer"):
        get_fixer(collected, "nope")


def test_readme_skeleton_finds_repos_without_readme(collected, world, tmp_path):
    d = preview(collected, ReadmeSkeleton())
    assert list(d) == ["mono"]                                                               # the only fixture repo with no README
    assert "+# mono" in d["mono"] and "TODO: describe" in d["mono"]


def test_preview_cleans_workdir_when_fixer_raises(collected):
    class Boom(Autofixer):
        name = "boom-test"
        description = "x"
        commit_message = "t"
        pr_title = "t"
        pr_body = "t"

        def find_repos(self, ctx, repos):
            return ["old-tool"]

        def apply(self, workdir, repo, snap):
            raise RuntimeError("boom")

    root = collected.work_dir("preview") / "boom-test"
    with pytest.raises(RuntimeError, match="boom"):
        preview(collected, Boom())
    assert not root.exists()


# ---- feature 9: plan-config ---------------------------------------------------------------------

def analysed(ctx):
    run_collect(ctx)
    return run_analyze(ctx)


def with_cfg(ctx, **over):
    from mimar.config import deep_merge
    return load_config(None, deep_merge({k: v for k, v in ctx.cfg.items() if not k.startswith("_")}, over))


def test_plan_config_reports_policy_and_candidate_changes_and_writes_nothing(ctx):
    analysed(ctx)
    run_propose(ctx)
    snapshot = [(t, ctx.db.one(f"select count(*) c from {t}")["c"]) for t in ("analyses", "candidates", "proposals", "approvals", "write_log")]
    gh_writes = len(ctx.gh.writes)
    new = with_cfg(ctx, policy_actions={"branch_protection": "log"}, exclude=["hermes-agent", "old-tool"],
                   thresholds={"merge_coupling": 0.95})
    r = plan_config(ctx, new)
    keys = {c["key"] for c in r["config_changes"]}
    assert {"exclude", "policy_actions.branch_protection", "thresholds.merge_coupling"} <= keys
    assert any(c["key"] == "archive:old-tool" for c in r["candidates"]["removed"])
    assert any(c["key"].startswith("merge:") for c in r["candidates"]["removed"])
    assert any(a["class"] == "branch_protection" and a["old"] != a["new"] for a in r["policy"]["action_changes"])
    assert any("no longer a candidate" in x["effect"] for x in r["open_proposals"])
    assert [(t, ctx.db.one(f"select count(*) c from {t}")["c"]) for t in ("analyses", "candidates", "proposals", "approvals", "write_log")] == snapshot
    assert len(ctx.gh.writes) == gh_writes
    text = "\n".join(render(r))
    assert "thresholds.merge_coupling: 0.6 -> 0.95" in text and "archive:old-tool" in text


def test_plan_config_flags_a_plan_that_would_change(ctx):
    analysed(ctx); run_propose(ctx)
    run_collect(ctx); run_analyze(ctx); run_propose(ctx)
    new = with_cfg(ctx, policy={"branch_protection": {"required_approvals": 2}})
    r = plan_config(ctx, new)
    assert any(x["effect"].startswith("plan changes") for x in r["open_proposals"])


def test_plan_config_identical_config_changes_nothing(ctx):
    analysed(ctx)
    r = plan_config(ctx, with_cfg(ctx))
    assert r["config_changes"] == [] and r["candidates"]["added"] == r["candidates"]["removed"] == []
    assert r["policy"]["new_drift"] == [] and r["open_proposals"] == []


def test_plan_config_shows_new_boundary_violations_and_stage_changes(ctx):
    analysed(ctx)
    new = with_cfg(ctx, lifecycle={"frozen": ["old-tool"]})
    assert plan_config(ctx, new)["lifecycle"] == {"old-tool": ["archive-proposed", "frozen"]}


def test_plan_config_cli_with_set_overrides(ctx, capsys):
    analysed(ctx)
    args = cli.build_parser().parse_args(["plan-config", "--set", "thresholds.merge_coupling=0.99", "--set", "exclude=[hermes-agent, old-tool]"])
    assert cli.cmd_plan_config(ctx, args) == 0
    out = capsys.readouterr().out
    assert "thresholds.merge_coupling: 0.6 -> 0.99" in out and "archive:old-tool" in out and "nothing is written" in out
    assert cli.cmd_plan_config(ctx, cli.build_parser().parse_args(["plan-config"])) == 2


def test_config_diff_is_flat_and_ignores_private_keys():
    a, b = {"a": {"b": 1, "c": 2}, "_path": "x"}, {"a": {"b": 1, "c": 3}, "_path": "y"}
    assert config_diff(a, b) == [{"key": "a.c", "old": 2, "new": 3}]
