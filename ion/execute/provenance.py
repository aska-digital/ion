"""Provenance and exact verification for moved history.

A split or a code move rewrites history with git-filter-repo. Counting commits is a weak check; what matters is that
the new repository's tree is byte-for-byte (tree-hash for tree-hash) what the source had at that path. We check that,
and we use the commit map that filter-repo writes to confirm that every source commit which touched the moved paths
has a counterpart in the result. Each rewritten commit also gets a trailer naming where it came from, so a move can be
traced years later without any database:

    Ion-Origin: <org>/<repo>@<original commit sha>
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..util import git

ZERO = "0" * 40


def origin_callback(org: str, repo: str) -> str:
    """Body for `git filter-repo --commit-callback`: append a Ion-Origin trailer naming the original commit.

    The commit callback runs after filter-repo has rewritten hashes mentioned in messages, so the trailer keeps the
    *original* sha. (filter-repo's message callback only sees the text, not the commit id, so it cannot do this.)"""
    label = f"{org}/{repo}@".encode()
    return (
        "import re\n"
        f"line = {label!r} + commit.original_id\n"
        "if b'Ion-Origin: ' + line not in commit.message:\n"
        "    m = commit.message.rstrip(b'\\n')\n"
        "    last = m.split(b'\\n')[-1]\n"
        "    trailer_block = b'\\n\\n' in m and re.match(rb'^[A-Za-z][A-Za-z-]*: ', last) is not None\n"
        "    commit.message = m + (b'\\n' if trailer_block else b'\\n\\n') + b'Ion-Origin: ' + line + b'\\n'\n"
    )


def read_commit_map(path: str | Path) -> dict[str, str]:
    """filter-repo's `old new` map (first line is a header). Pruned commits map to zeros."""
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"commit map missing: {p}")
    out: dict[str, str] = {}
    for i, line in enumerate(p.read_text().splitlines()):
        parts = line.split()
        if i == 0 and parts[:2] == ["old", "new"]:
            continue
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def _rev(repo_dir, rev: str) -> str | None:
    p = git(["rev-parse", "--verify", "-q", rev], cwd=repo_dir, check=False)
    return p.stdout.strip() or None


def guard_filter_metadata(source_dir, source_ref="HEAD") -> None:
    # Root metadata can govern files under every moved directory. Until rules and
    # submodule paths can be translated faithfully, refuse rather than drop them.
    for metadata in (".gitattributes", ".gitmodules"):
        if _rev(source_dir, f"{source_ref}:{metadata}"):
            raise RuntimeError(f"cannot safely filter directories while root {metadata} exists; "
                               "migrate its attributes/submodule metadata explicitly before proposing this move")

def verify_filtered(filtered_dir, source_dir, source_ref: str, dirs: list[str]) -> dict:
    """Check a filter-repo result against its source. Raises RuntimeError on any mismatch.

    - every source commit touching `dirs` appears in the commit map with a real (non-zero) new commit that is
      reachable from the result's HEAD;
    - the result's tree equals the source's tree at `dirs` (one dir: root tree == `<ref>:<dir>`; several: each
      `HEAD:<d>` equals `<ref>:<d>` and the root holds nothing else)."""
    guard_filter_metadata(source_dir, source_ref)
    cmap = read_commit_map(Path(filtered_dir) / ".git" / "filter-repo" / "commit-map")
    touching = git(["rev-list", source_ref, "--", *dirs], cwd=source_dir).stdout.split()
    reachable = set(git(["rev-list", "HEAD"], cwd=filtered_dir).stdout.split())
    missing = [c[:10] for c in touching if cmap.get(c, ZERO) == ZERO or cmap[c] not in reachable]
    if missing:
        raise RuntimeError(f"commit map: {len(missing)} source commits touching {dirs} have no counterpart "
                           f"in the result (e.g. {', '.join(missing[:3])})")
    if len(dirs) == 1:
        got, want = _rev(filtered_dir, "HEAD^{tree}"), _rev(source_dir, f"{source_ref}:{dirs[0]}")
        if not want or got != want:
            raise RuntimeError(f"tree mismatch: result root tree {got} != source {dirs[0]} tree {want}")
        tree = got
    else:
        for d in dirs:
            got, want = _rev(filtered_dir, f"HEAD:{d}"), _rev(source_dir, f"{source_ref}:{d}")
            if not want or got != want:
                raise RuntimeError(f"tree mismatch at {d}: result {got} != source {want}")
        top = set(git(["ls-tree", "--name-only", "HEAD"], cwd=filtered_dir).stdout.split())
        if top != set(dirs):
            raise RuntimeError(f"result root holds {sorted(top)}, expected exactly {sorted(dirs)}")
        tree = _rev(filtered_dir, "HEAD^{tree}")
    digest = hashlib.sha256(Path(filtered_dir, ".git", "filter-repo", "commit-map").read_bytes()).hexdigest()
    return {"tree": tree, "source_commits": len(touching), "map_entries": len(cmap), "map_sha256": digest[:16],
            "result_commits": len(reachable)}


def verify_merged_tree(target_dir, prefix: str, source_git_dir, source_ref: str) -> dict:
    """After `git subtree add --prefix=<prefix>`: the target's `HEAD:<prefix>` tree must equal the source's tip tree,
    and the source tip must be an ancestor of the result (full history kept)."""
    got = _rev(target_dir, f"HEAD:{prefix}")
    want = git(["--git-dir", str(source_git_dir), "rev-parse", f"{source_ref}^{{tree}}"]).stdout.strip()
    if got != want:
        raise RuntimeError(f"tree mismatch: target {prefix}/ is {got}, source tip tree is {want}")
    tip = git(["--git-dir", str(source_git_dir), "rev-parse", source_ref]).stdout.strip()
    git(["merge-base", "--is-ancestor", tip, "HEAD"], cwd=target_dir)
    return {"tree": got, "source_tip": tip}
