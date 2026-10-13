import subprocess

import pytest

from conftest import make_ctx, sh
from helpers import MERGE, SPLIT, approve, new_proposal
from mimar import proposals
from mimar.collect import run_collect
from mimar.execute import executor_for
from mimar.execute.runner import run_execute
from mimar.util import Refused, SafetyError, guard_git


def prep(ctx, kind, subject, approved=True):
    run_collect(ctx)
    p = new_proposal(ctx, kind, subject)
    return approve(ctx, p) if approved else p


def branches(remotes, repo):
    return sh("git", "--git-dir", str(remotes / f"{repo}.git"), "for-each-ref", "--format=%(refname:short)", "refs/heads").split()


def subjects(remotes, repo, ref):
    return sh("git", "--git-dir", str(remotes / f"{repo}.git"), "log", "--format=%s", ref).splitlines()


def head(remotes, repo, ref="main"):
    return sh("git", "--git-dir", str(remotes / f"{repo}.git"), "rev-parse", ref).strip()


# ---- the gate -------------------------------------------------------------------------

def test_refuses_without_approval_even_for_dry_run(ctx):
    p = prep(ctx, "merge", MERGE, approved=False)
    ex = executor_for(ctx, "merge")
    for dry in (True, False):
        with pytest.raises(Refused, match="no verified approval"):
            ex.apply(p, dry_run=dry)
    assert not ctx.db.q("select * from ledger") and not ctx.gh.writes_to("POST", "/pulls")


def test_status_flag_alone_is_not_approval(ctx):
    p = prep(ctx, "merge", MERGE, approved=False)
    ctx.db.x("update proposals set status='approved' where id=?", (p["id"],))
    with pytest.raises(Refused):
        executor_for(ctx, "merge").apply(proposals.get(ctx.db, p["id"]), dry_run=False)


def test_unverified_approval_row_does_not_count(ctx):
    from mimar.approve import core
    p = prep(ctx, "merge", MERGE, approved=False)
    core.record(ctx.db, p["id"], "github_label", "mallory", "approve", "2099-01-01T00:00:00Z", "x", False, "non maintainer")
    with pytest.raises(Refused):
        executor_for(ctx, "merge").apply(p, dry_run=False)


def test_kill_switch_blocks_everything(ctx, home):
    p = prep(ctx, "merge", MERGE)
    (home / "PAUSED").write_text("x")
    with pytest.raises(Refused, match="kill switch"):
        executor_for(ctx, "merge").apply(p, dry_run=False)
    (home / "PAUSED").unlink()
    ctx.cfg["paused"] = True
    with pytest.raises(Refused, match="kill switch"):
        executor_for(ctx, "merge").apply(p, dry_run=True)


def test_kill_switch_also_blocks_allowlisted_policy_fix(ctx, home):
    ctx.cfg["auto_apply"] = ["dependabot_alerts"]
    p = prep(ctx, "policy_fix", {"class": "dependabot_alerts", "repo": "old-tool"}, approved=False)
    (home / "PAUSED").write_text("x")
    with pytest.raises(Refused):
        executor_for(ctx, "policy_fix").apply(p, dry_run=False)


def test_policy_fix_only_when_allowlisted(ctx):
    fix = {"class": "branch_protection", "repo": "alpha-core", "branch": "main", "settings": ctx.cfg["policy"]["branch_protection"]}
    p = prep(ctx, "policy_fix", fix, approved=False)
    ex = executor_for(ctx, "policy_fix")
    ctx.cfg["auto_apply"] = []
    with pytest.raises(Refused):
        ex.apply(p, dry_run=False)
    ctx.cfg["auto_apply"] = ["branch_protection"]
    out = ex.apply(p, dry_run=False)
    put = ctx.gh.writes_to("PUT", "/branches/main/protection")[0]
    assert put[2]["allow_force_pushes"] is False and put[2]["required_pull_request_reviews"] == {"required_approving_review_count": 0}
    assert out["basis"] == "allowlisted policy_fix" and proposals.get(ctx.db, p["id"])["status"] == "done"
    # a non-allowlisted class stays blocked
    p2 = prep(ctx, "policy_fix", {"class": "secret_scanning", "repo": "alpha-core"}, approved=False)
    with pytest.raises(Refused):
        ex.apply(p2, dry_run=False)


def test_policy_fix_is_idempotent_calls(ctx):
    ctx.cfg["auto_apply"] = ["dependabot_alerts"]
    p = prep(ctx, "policy_fix", {"class": "dependabot_alerts", "repo": "old-tool"}, approved=False)
    ex = executor_for(ctx, "policy_fix")
    ex.apply(p, dry_run=False)
    assert len(ctx.gh.writes_to("PUT", "/vulnerability-alerts")) == 1
    from mimar.execute.simple import apply_policy_fix
    apply_policy_fix(ctx, p["subject"])                         # re-applying is a plain PUT, safe to repeat
    assert len(ctx.gh.writes_to("PUT", "/vulnerability-alerts")) == 2


def test_git_guard_refuses_force_and_deletes():
    for bad in (["git", "push", "--force", "origin", "x"], ["git", "push", "-f"], ["git", "push", "origin", "+x:y"],
                ["git", "push", "origin", ":main"], ["git", "push", "--force-with-lease"], ["git", "push", "--delete", "origin", "b"]):
        with pytest.raises(SafetyError):
            guard_git(bad)
    with pytest.raises(SafetyError):
        guard_git(["git", "branch", "-D", "x"])
    guard_git(["git", "push", "origin", "b:refs/heads/b"])


def test_safe_push_refuses_default_branch(ctx, tmp_path):
    ex = executor_for(ctx, "merge")
    with pytest.raises(SafetyError):
        ex.safe_push(["origin", "mimar/x:refs/heads/main"], tmp_path, "main")


# ---- merge: dry run + end to end ---------------------------------------------------------

def test_merge_dry_run_shows_exact_commands(ctx):
    p = prep(ctx, "merge", MERGE)
    out = executor_for(ctx, "merge").apply(p, dry_run=True)
    text = "\n".join(out["plan"])
    assert "git subtree add --prefix=alpha-cli" in text and "--squash" not in text
    assert "git push origin" in text and "--force" not in text
    assert not ctx.gh.writes_to("POST", "/pulls") and not ctx.db.q("select * from ledger")


def test_merge_end_to_end_keeps_history_and_never_touches_default(ctx, world):
    p = prep(ctx, "merge", MERGE)
    main_before, src_before = head(world, "alpha-core"), head(world, "alpha-cli")
    out = executor_for(ctx, "merge").apply(p, dry_run=False)
    pid = p["id"]
    assert out["phase"] == "open_pr" and not out["waiting"]
    assert head(world, "alpha-core") == main_before            # default branch untouched
    assert head(world, "alpha-cli") == src_before
    assert f"mimar/{pid}" in branches(world, "alpha-core")
    subs = subjects(world, "alpha-core", f"mimar/{pid}")
    for i in range(5):
        assert f"cli {i}" in subs and f"core {i}" in subs     # full history of both, not squashed
    assert len(subs) >= 11
    tree = sh("git", "--git-dir", str(world / "alpha-core.git"), "ls-tree", "-r", "--name-only", f"mimar/{pid}")
    assert "alpha-cli/alpha_cli/main.py" in tree
    (pr,) = [w for w in ctx.gh.writes_to("POST", "/pulls")]
    assert pr[1].endswith("/alpha-core/pulls") and pr[2]["head"] == f"mimar/{pid}" and pr[2]["base"] == "main"
    steps = [(r["step"], r["phase"]) for r in ctx.db.q("select step, phase from ledger where proposal=?", (pid,))]
    for s in ("open_pr.clone-target", "open_pr.subtree-add", "open_pr.push-branch", "open_pr.open-pr"):
        assert (s, "before") in steps and (s, "after") in steps
        assert steps.index((s, "before")) < steps.index((s, "after"))
    assert proposals.get(ctx.db, pid)["state"]["phase"] == "await_merge"


def test_merge_waits_for_pr_then_pointer_then_second_approval_archive(ctx, world):
    p = prep(ctx, "merge", MERGE)
    ex = executor_for(ctx, "merge")
    pid = p["id"]
    ex.apply(p, dry_run=False)
    out = ex.apply(proposals.get(ctx.db, pid), dry_run=False)             # PR not merged yet
    assert out["waiting"] and proposals.get(ctx.db, pid)["state"]["phase"] == "await_merge"
    ctx.gh.merged.add(("alpha-core", 1))
    ex.apply(proposals.get(ctx.db, pid), dry_run=False)                   # await -> pointer
    out = ex.apply(proposals.get(ctx.db, pid), dry_run=False)             # pointer PR in source
    assert f"mimar/{pid}-pointer" in branches(world, "alpha-cli")
    readme = sh("git", "--git-dir", str(world / "alpha-cli.git"), "show", f"mimar/{pid}-pointer:README.md")
    assert "alpha-core" in readme and "moved" in readme
    assert head(world, "alpha-cli") == head(world, "alpha-cli", "main")
    assert "alpha-cli" not in ctx.gh.archived                              # nothing archived yet
    ctx.gh.merged.add(("alpha-cli", 2))
    ex.apply(proposals.get(ctx.db, pid), dry_run=False)                   # pointer merged
    ex.apply(proposals.get(ctx.db, pid), dry_run=False)                   # archive step -> follow-up proposal
    assert "alpha-cli" not in ctx.gh.archived and not ctx.gh.writes_to("PATCH", "/repos/acme/alpha-cli")
    follow = proposals.view(ctx.db.one("select * from proposals where kind='archive'"))
    assert follow["parent"] == pid and follow["status"] == "awaiting-approval"
    with pytest.raises(Refused):
        executor_for(ctx, "archive").apply(follow, dry_run=False)         # needs its own approval
    follow = approve(ctx, follow)
    executor_for(ctx, "archive").apply(follow, dry_run=False)
    assert "alpha-cli" in ctx.gh.archived
    assert proposals.get(ctx.db, pid)["status"] == "done"


def test_merge_resumes_after_failure_without_repeating_steps(ctx, world, monkeypatch):
    p = prep(ctx, "merge", MERGE)
    ex = executor_for(ctx, "merge")
    real = ctx.gh.rest
    def flaky(method, path, body=None):
        if method == "POST" and path.endswith("/pulls"):
            raise RuntimeError("api down")
        return real(method, path, body)
    monkeypatch.setattr(ctx.gh, "rest", flaky)
    with pytest.raises(RuntimeError):
        ex.apply(p, dry_run=False)
    assert proposals.get(ctx.db, p["id"])["status"] == "failed"
    err = ctx.db.one("select * from ledger where phase='error'")
    assert err["step"] == "open_pr.open-pr"
    monkeypatch.setattr(ctx.gh, "rest", real)
    ctx.db.x("update proposals set status='approved' where id=?", (p["id"],))
    ex.apply(proposals.get(ctx.db, p["id"]), dry_run=False)
    befores = ctx.db.q("select count(*) n from ledger where step='open_pr.clone-target' and phase='before'")[0]["n"]
    assert befores == 1                                                    # resumed, did not re-clone
    assert len(ctx.gh.writes_to("POST", "/pulls")) == 1


def test_merge_refuses_when_prefix_exists(ctx, world):
    subj = {**MERGE, "prefix": "alpha_core"}                               # directory already in the target
    p = prep(ctx, "merge", subj)
    with pytest.raises(RuntimeError):
        executor_for(ctx, "merge").apply(p, dry_run=False)
    assert head(world, "alpha-core") == head(world, "alpha-core", "main")
    assert f"mimar/{p['id']}" not in branches(world, "alpha-core")


# ---- split --------------------------------------------------------------------------------

def test_split_dry_run_shows_filter_repo(ctx):
    p = prep(ctx, "split", {**SPLIT, "dirs": ["engine"]})
    text = "\n".join(executor_for(ctx, "split").apply(p, dry_run=True)["plan"])
    assert "git filter-repo --subdirectory-filter engine" in text and "git push origin HEAD:refs/heads/main" in text
    assert "--force" not in text


def test_split_end_to_end(ctx, world):
    p = prep(ctx, "split", SPLIT)
    ex = executor_for(ctx, "split")
    before = head(world, "mono")
    ex.apply(p, dry_run=False)
    pid = p["id"]
    assert (world / "mono-engine.git").exists()
    assert subjects(world, "mono-engine", "main") == [f"engine {i}" for i in (4, 3, 2, 1, 0)]     # history, only D's commits
    tree = sh("git", "--git-dir", str(world / "mono-engine.git"), "ls-tree", "-r", "--name-only", "main").split()
    assert any(t.startswith("engine/") for t in tree) and not any(t.startswith("site") for t in tree)
    assert head(world, "mono") == before                                   # source untouched so far
    created = ctx.gh.writes_to("POST", "/orgs/acme/repos")[0][2]
    assert created["name"] == "mono-engine" and created["private"] is False   # same visibility as the source
    assert ctx.gh.writes_to("PUT", "/repos/acme/mono-engine/branches/main/protection")           # policy copied
    assert ctx.db.ledger_done(pid, "create.verify")
    ex.apply(proposals.get(ctx.db, pid), dry_run=False)                    # pointer phase
    assert f"mimar/{pid}-remove" in branches(world, "mono")
    removed = sh("git", "--git-dir", str(world / "mono.git"), "ls-tree", "-r", "--name-only", f"mimar/{pid}-remove").split()
    assert not any(t.startswith("engine") for t in removed) and "MOVED.md" in removed and any(t.startswith("site/") for t in removed)
    assert head(world, "mono") == before
    assert proposals.get(ctx.db, pid)["status"] == "done"


def test_split_single_dir_uses_subdirectory_filter(ctx, world):
    p = prep(ctx, "split", {**SPLIT, "dirs": ["site"], "new_repo": "mono-site"})
    executor_for(ctx, "split").apply(p, dry_run=False)
    tree = sh("git", "--git-dir", str(world / "mono-site.git"), "ls-tree", "-r", "--name-only", "main").split()
    assert "m0.py" in tree and not any(t.startswith("site/") for t in tree)


def test_split_refuses_non_empty_target(ctx, world):
    sh("git", "init", "-q", "--bare", "-b", "main", str(world / "mono-engine.git"))
    work = world / "_prefill"
    sh("git", "init", "-q", "-b", "main", str(work))
    (work / "f").write_text("x")
    sh("git", "add", "-A", cwd=work); sh("git", "commit", "-qm", "pre", cwd=work)
    sh("git", "push", "-q", str(world / "mono-engine.git"), "main", cwd=work)
    p = prep(ctx, "split", SPLIT)
    with pytest.raises(SafetyError, match="non-empty"):
        executor_for(ctx, "split").apply(p, dry_run=False)
    assert subjects(world, "mono-engine", "main") == ["pre"]


def test_split_pointer_refuses_until_verified(ctx):
    p = prep(ctx, "split", SPLIT)
    ctx.db.set_state(p["id"], phase="pointer")
    with pytest.raises(Refused, match="not verified"):
        executor_for(ctx, "split").apply(proposals.get(ctx.db, p["id"]), dry_run=False)


def test_split_verify_failure_stops_before_removal(ctx, world, monkeypatch):
    p = prep(ctx, "split", SPLIT)
    ex = executor_for(ctx, "split")
    monkeypatch.setattr(type(ex), "verify_filtered", lambda self, *a: (_ for _ in ()).throw(RuntimeError("tree mismatch")))
    with pytest.raises(RuntimeError, match="mismatch"):
        ex.apply(p, dry_run=False)
    assert not any(b.endswith("-remove") for b in branches(world, "mono"))


# ---- others ---------------------------------------------------------------------------------

def test_archive_executor_never_deletes(ctx):
    p = prep(ctx, "archive", {"repo": "old-tool"})
    executor_for(ctx, "archive").apply(p, dry_run=False)
    assert "old-tool" in ctx.gh.archived and not ctx.gh.writes_to("DELETE", "/repos/acme/old-tool")
    assert proposals.get(ctx.db, p["id"])["status"] == "done"


def test_health_fix_opens_readme_pr(ctx, world):
    p = prep(ctx, "health_fix", {"repo": "alpha-core", "file": "README.md", "description": "core lib"})
    executor_for(ctx, "health_fix").apply(p, dry_run=False)
    assert f"mimar/{p['id']}" in branches(world, "alpha-core")
    assert "core lib" in sh("git", "--git-dir", str(world / "alpha-core.git"), "show", f"mimar/{p['id']}:README.md")
    author = sh("git", "--git-dir", str(world / "alpha-core.git"), "log", "-1", "--format=%an <%ae>%n%B", f"mimar/{p['id']}")
    assert "mimar <mimar@example.com>" in author and "Co-Authored-By" not in author      # config identity, no AI trailer


def test_move_code_two_phase(ctx, world):
    p = prep(ctx, "move_code", {"source": "mono", "dir": "site", "target": "old-tool", "dest_prefix": "site"})
    ex = executor_for(ctx, "move_code")
    ex.apply(p, dry_run=False)
    pid = p["id"]
    tree = sh("git", "--git-dir", str(world / "old-tool.git"), "ls-tree", "-r", "--name-only", f"mimar/{pid}").split()
    assert "site/m0.py" in tree
    out = ex.apply(proposals.get(ctx.db, pid), dry_run=False)
    assert out["waiting"]
    ctx.gh.merged.add(("old-tool", 1))
    ex.apply(proposals.get(ctx.db, pid), dry_run=False)
    assert f"mimar/{pid}-remove" in branches(world, "mono")


def test_runner_dry_run_default_and_apply(ctx, world):
    p = prep(ctx, "archive", {"repo": "old-tool"})
    r = run_execute(ctx)
    assert p["id"] in r["plans"] and "old-tool" not in ctx.gh.archived
    r = run_execute(ctx, apply=True)
    assert "old-tool" in ctx.gh.archived and r["actions"]


def test_runner_skips_unapproved_and_shows_plan_when_asked(ctx):
    p = prep(ctx, "merge", MERGE, approved=False)
    r = run_execute(ctx, p["id"])
    assert r["skipped"] and "git subtree add" in "\n".join(r["plans"][p["id"]])
    assert not ctx.gh.writes_to("POST", "/pulls")
