"""Fleet tools over the local mirrors: `ion grep` and `ion autofix`.

Both work on the bare mirrors under the cache directory, so searching the whole org costs no API calls. Searching is
read-only. A fix is previewed locally as diffs; turning a preview into pull requests goes through a proposal that a
maintainer approves, and the executor then opens the PRs through the typed write requests (see execute/autofix.py).

An autofixer is a small class with the interface below. Register your own with `register(MyFixer)` from a module
named in the config under `autofix.plugins`.
"""
from __future__ import annotations

import fnmatch
import hashlib
import importlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .analyze import health
from .collect.snapshot import latest_snapshots
from .config import parse_repo_override
from .gitrepo import GitRepo
from .util import git, safe_write


# ---- grep ------------------------------------------------------------------------------------------------

@dataclass
class Match:
    repo: str
    path: str
    line: int
    text: str


def fleet_repos(ctx, repo_glob: str | None = None, include_forks: bool = False) -> dict[str, dict]:
    """Repos that have a mirror on disk: name -> snapshot. Excluded, archived and (by default) fork repos are skipped."""
    snaps, _ = latest_snapshots(ctx.db)
    out = {}
    for n, s in sorted(snaps.items()):
        if n in ctx.cfg["exclude"] or s.get("archived") or s.get("empty") or (s.get("fork") and not include_forks):
            continue
        if repo_glob and not fnmatch.fnmatch(n, repo_glob):
            continue
        if ctx.mirror_path(n).exists() and s.get("default_branch"):
            out[n] = s
    return out


def grep_mirrors(ctx, pattern: str, paths: list[str] | None = None, repo_glob: str | None = None, ignore_case: bool = False,
                 fixed: bool = False, max_matches: int = 500) -> list[Match]:
    """`git grep` over every mirror's default branch. Text files only; no checkout, no API."""
    out: list[Match] = []
    for name, snap in fleet_repos(ctx, repo_glob).items():
        ref = snap["default_branch"]
        argv = ["git", "-C", str(ctx.mirror_path(name)), "grep", "-I", "-n", "-z", "-F" if fixed else "-E"]
        if ignore_case:
            argv.append("-i")
        argv += ["-e", pattern, ref, "--", *(paths or [])]
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace")
        if p.returncode not in (0, 1):                   # 1 means "no match"
            raise RuntimeError(f"git grep failed in {name}: {p.stderr.strip()[:200]}")
        for rec in p.stdout.split("\n"):
            parts = rec.split("\0", 2)
            if len(parts) < 3 or not parts[1].isdigit():
                continue
            path = parts[0][len(ref) + 1:] if parts[0].startswith(ref + ":") else parts[0]
            out.append(Match(name, path, int(parts[1]), parts[2].rstrip("\r")))
            if len(out) >= max_matches:
                return out
    return out


# ---- autofixers -----------------------------------------------------------------------------------------

class Autofixer:
    """Interface for a fleet change. `find_repos` picks the repos that need it; `apply` edits a fresh checkout.

    The checkout's contents are untrusted: a committed symlink can point anywhere on this machine. `apply` writes files
    with `self.write(workdir, relpath, text)`, which refuses to write through a symlink or outside the checkout."""
    name = ""
    description = ""
    commit_message = ""
    pr_title = ""
    pr_body = ""

    def find_repos(self, ctx, repos: dict[str, dict]) -> list[str]:
        raise NotImplementedError

    def apply(self, workdir: Path, repo: str, snap: dict) -> None:
        raise NotImplementedError

    def write(self, workdir: Path, relpath: str, text: str) -> Path:
        return safe_write(workdir, relpath, text)


FIXERS: dict[str, type[Autofixer]] = {}


def register(cls: type[Autofixer]) -> type[Autofixer]:
    FIXERS[cls.name] = cls
    return cls


def load_plugins(cfg) -> None:
    for mod in (cfg.get("autofix") or {}).get("plugins") or []:
        importlib.import_module(mod)


def get_fixer(ctx, name: str) -> Autofixer:
    load_plugins(ctx.cfg)
    if name not in FIXERS:
        raise KeyError(f"unknown autofixer {name!r}; available: {', '.join(sorted(FIXERS)) or 'none'}")
    return FIXERS[name]()


def _tree(ctx, repo: str, snap: dict) -> set[str] | None:
    g = GitRepo(ctx.mirror_path(repo), snap["default_branch"])
    return set(g.files()) if g.resolve_ref() else None


@register
class ReadmeSkeleton(Autofixer):
    name = "readme-skeleton"
    description = "add a README skeleton to repos that have none"
    commit_message = "Add README skeleton"
    pr_title = "Add README skeleton"
    pr_body = "Adds a README skeleton for a maintainer to fill in."

    def find_repos(self, ctx, repos):
        out = []
        for n, s in repos.items():
            ov = parse_repo_override(s.get("override_text"))[0]
            if ov.get("ignore") or ov.get("enabled") is False:
                continue
            if health.has_root_file(_tree(ctx, n, s), "README.md") is False:
                out.append(n)
        return out

    def apply(self, workdir, repo, snap):
        self.write(workdir, "README.md",
                   f"# {repo}\n\n{snap.get('description') or ''}\n\n## Status\n\n"
                   "TODO: describe what this repository is for, how to run it, and who maintains it.\n")


ECOSYSTEMS = [("pip", re.compile(r"(^|/)(pyproject\.toml|requirements[^/]*\.txt|setup\.py|Pipfile)$")),
              ("npm", re.compile(r"(^|/)package\.json$")), ("cargo", re.compile(r"(^|/)Cargo\.toml$")),
              ("gomod", re.compile(r"(^|/)go\.mod$")), ("docker", re.compile(r"(^|/)Dockerfile[^/]*$")),
              ("github-actions", re.compile(r"^\.github/workflows/[^/]+\.ya?ml$"))]


def detect_ecosystems(files: set[str]) -> list[tuple[str, str]]:
    """(ecosystem, directory) pairs found in a file listing, one per ecosystem at its shallowest directory."""
    found: dict[str, str] = {}
    for eco, rx in ECOSYSTEMS:
        dirs = sorted({"/" + f.rsplit("/", 1)[0] if "/" in f else "/" for f in files if rx.search(f)},
                      key=lambda d: (d != "/", d.count("/"), d))
        if dirs:
            found[eco] = "/" if eco == "github-actions" else dirs[0]
    return sorted(found.items())


@register
class DependabotConfig(Autofixer):
    name = "dependabot-config"
    description = "add .github/dependabot.yml with the ecosystems detected in the repo"
    commit_message = "Add Dependabot version updates configuration"
    pr_title = "Add Dependabot configuration"
    pr_body = "Enables weekly Dependabot version updates for the package ecosystems found in this repository."

    def find_repos(self, ctx, repos):
        out = []
        for n, s in repos.items():
            tree = _tree(ctx, n, s)
            if tree is not None and ".github/dependabot.yml" not in tree and ".github/dependabot.yaml" not in tree \
                    and detect_ecosystems(tree):
                out.append(n)
        return out

    def apply(self, workdir, repo, snap):
        files = {str(p.relative_to(workdir)) for p in workdir.rglob("*") if p.is_file() and ".git" not in p.parts}
        lines = ["version: 2", "updates:"]
        for eco, d in detect_ecosystems(files):
            lines += [f"  - package-ecosystem: {eco}", f"    directory: \"{d}\"", "    schedule:", "      interval: weekly"]
        self.write(workdir, ".github/dependabot.yml", "\n".join(lines) + "\n")


# ---- preview ---------------------------------------------------------------------------------------------

def checkout(ctx, source_url: str, dest: Path, branch: str) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(["clone", "--quiet", "--no-local", "--branch", branch, source_url, str(dest)])


def diff_of(ctx, fixer: Autofixer, repo: str, snap: dict, workdir: Path, url: str | None = None) -> str:
    """Fresh checkout of the repo (from its mirror unless `url` is given), apply the fixer, return the staged diff."""
    checkout(ctx, url or str(ctx.mirror_path(repo)), workdir, snap["default_branch"])
    fixer.apply(workdir, repo, snap)
    git(["add", "-A"], cwd=workdir)
    return git(["diff", "--cached", "--no-color"], cwd=workdir).stdout


def preview(ctx, fixer: Autofixer, repo_glob: str | None = None) -> dict[str, str]:
    """repo -> unified diff, for every repo the fixer would change. Nothing leaves the machine."""
    repos = fleet_repos(ctx, repo_glob)
    root = ctx.work_dir("preview") / fixer.name
    out = {}
    try:
        for name in fixer.find_repos(ctx, repos):
            d = diff_of(ctx, fixer, name, repos[name], root / name)
            if d.strip():
                out[name] = d
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return out


def digest(diff: str) -> str:
    return hashlib.sha256(diff.encode()).hexdigest()
