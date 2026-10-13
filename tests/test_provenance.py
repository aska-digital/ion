"""A4: merges and splits are verified by tree equality and the filter-repo commit map, and moved commits are stamped."""
import shutil

import pytest

from conftest import sh
from helpers import MERGE, SPLIT, approve, new_proposal
from mimar import proposals
from mimar.collect import run_collect
from mimar.execute import executor_for
from mimar.execute.provenance import ZERO, origin_callback, read_commit_map, verify_filtered, verify_merged_tree
from mimar.util import git


def gd(remotes, repo):
    return ["git", "--git-dir", str(remotes / f"{repo}.git")]


def prep(ctx, kind, subject):
    run_collect(ctx)
    return approve(ctx, new_proposal(ctx, kind, subject))


def test_split_stamps_origin_trailer_and_tree_equals_source(ctx, world):
    p = prep(ctx, "split", {**SPLIT, "dirs": ["site"], "new_repo": "mono-site"})
    executor_for(ctx, "split").apply(p, dry_run=False)
    # tree equality: the new repo's root tree is exactly the source's site/ tree
    new_tree = sh(*gd(world, "mono-site"), "rev-parse", "main^{tree}").strip()
    src_tree = sh(*gd(world, "mono"), "rev-parse", "main:site").strip()
    assert new_tree == src_tree
    # every moved commit names the original commit it came from
    src_shas = set(sh(*gd(world, "mono"), "rev-list", "main", "--", "site").split())
    msgs = sh(*gd(world, "mono-site"), "log", "--format=%B%x00", "main").split("\x00")
    origins = {l.split("@")[1] for m in msgs for l in m.splitlines() if l.startswith("Mimar-Origin: acme/mono@")}
    assert origins == src_shas and len(origins) == 5
    # the verification result is in the ledger with the tree hash
    detail = ctx.db.ledger_detail(p["id"], "create.verify")
    assert new_tree in detail and "map_sha256" in detail


def test_multi_dir_split_checks_each_tree_and_root(ctx, world):
    p = prep(ctx, "split", SPLIT)                               # engine + engine_utils
    executor_for(ctx, "split").apply(p, dry_run=False)
    for d in ("engine", "engine_utils"):
        assert sh(*gd(world, "mono-engine"), "rev-parse", f"main:{d}") == sh(*gd(world, "mono"), "rev-parse", f"main:{d}")
    assert ctx.db.ledger_done(p["id"], "create.verify")


@pytest.fixture
def filtered(tmp_path, world):
    """(source clone, filtered clone of mono keeping site/) built the way the split executor builds them."""
    src, flt = tmp_path / "src", tmp_path / "flt"
    for d in (src, flt):
        sh("git", "clone", "-q", "--no-local", str(world / "mono.git"), str(d))
    git(["filter-repo", "--subdirectory-filter", "site", "--commit-callback", origin_callback("acme", "mono"), "--force"], cwd=flt)
    return src, flt


def test_verify_filtered_accepts_a_faithful_result(filtered):
    src, flt = filtered
    r = verify_filtered(flt, src, "origin/main", ["site"])
    assert r["source_commits"] == 5 and len(r["tree"]) == 40


def test_verify_filtered_rejects_a_changed_tree(filtered):
    src, flt = filtered
    (flt / "m0.py").write_text("tampered\n")
    sh("git", "commit", "-qam", "tamper", cwd=flt)
    with pytest.raises(RuntimeError, match="tree mismatch"):
        verify_filtered(flt, src, "origin/main", ["site"])


def test_verify_filtered_rejects_a_commit_map_gap(filtered):
    src, flt = filtered
    mp = flt / ".git" / "filter-repo" / "commit-map"
    lines = mp.read_text().splitlines()
    lines[2] = lines[2].split()[0] + " " + ZERO                  # pretend one touching commit was pruned
    mp.write_text("\n".join(lines) + "\n")
    with pytest.raises(RuntimeError, match="commit map"):
        verify_filtered(flt, src, "origin/main", ["site"])
    assert len(read_commit_map(mp)) >= 5


def test_same_commit_count_is_not_enough(filtered):
    """A result with the right number of commits but different content must fail (the v1 check would have passed)."""
    src, flt = filtered
    (flt / "m0.py").write_text("different content\n")
    sh("git", "commit", "-qa", "--amend", "--no-edit", cwd=flt)       # still five commits
    assert int(sh("git", "rev-list", "--count", "HEAD", cwd=flt)) == 5
    with pytest.raises(RuntimeError):
        verify_filtered(flt, src, "origin/main", ["site"])


def test_merge_verifies_tree_and_stamps_merge_commit(ctx, world):
    p = prep(ctx, "merge", MERGE)
    executor_for(ctx, "merge").apply(p, dry_run=False)
    pid = p["id"]
    got = sh(*gd(world, "alpha-core"), "rev-parse", f"mimar/{pid}:alpha-cli").strip()
    assert got == sh(*gd(world, "alpha-cli"), "rev-parse", "main^{tree}").strip()
    top = sh(*gd(world, "alpha-core"), "log", "-1", "--format=%B", f"mimar/{pid}")
    tip = sh(*gd(world, "alpha-cli"), "rev-parse", "main").strip()
    assert f"Mimar-Proposal: {pid}" in top and f"Mimar-Origin: acme/alpha-cli@{tip}" in top
    assert got in ctx.db.ledger_detail(pid, "open_pr.verify-history")


def test_verify_merged_tree_rejects_wrong_tree(ctx, world, tmp_path):
    run_collect(ctx)
    tdir = tmp_path / "t"
    sh("git", "clone", "-q", "--no-local", str(world / "alpha-core.git"), str(tdir))
    mirror = tmp_path / "m.git"
    sh("git", "clone", "-q", "--mirror", str(world / "alpha-cli.git"), str(mirror))
    sh("git", "subtree", "add", "--prefix=alpha-cli", str(mirror), "main", "-m", "m", cwd=tdir)
    assert verify_merged_tree(tdir, "alpha-cli", mirror, "main")["tree"]
    with pytest.raises(RuntimeError, match="tree mismatch"):
        verify_merged_tree(tdir, "alpha-cli", mirror, "main~1")       # an older source state is not what was imported


def test_move_code_verifies_filter_and_import(ctx, world):
    p = prep(ctx, "move_code", {"source": "mono", "dir": "site", "target": "old-tool", "dest_prefix": "site"})
    executor_for(ctx, "move_code").apply(p, dry_run=False)
    pid = p["id"]
    assert ctx.db.ledger_done(pid, "add.verify-filter") and ctx.db.ledger_done(pid, "add.verify-import")
    assert sh(*gd(world, "old-tool"), "rev-parse", f"mimar/{pid}:site") == sh(*gd(world, "mono"), "rev-parse", "main:site")
    log = sh(*gd(world, "old-tool"), "log", "--format=%B", f"mimar/{pid}")
    assert "Mimar-Origin: acme/mono@" in log
