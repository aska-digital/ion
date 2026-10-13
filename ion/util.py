"""Small shared helpers: subprocess runner with guards, time, logging."""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("ion")

FORBIDDEN_GIT_FLAGS = {"--force", "-f", "--force-with-lease", "--force-if-includes", "--delete", "-D", "--mirror"}


class SafetyError(RuntimeError):
    """Raised when something would break a safety invariant."""


class Refused(RuntimeError):
    """Raised when an executor refuses to apply (no approval, kill switch, ...)."""


class Waiting(Exception):
    """A step that cannot proceed yet (PR not merged, per-run cap reached). Not an error; retried on a later run."""


def home() -> Path:
    p = Path(os.environ.get("ION_HOME", "~/.ion")).expanduser()
    legacy = Path("~/.mimar").expanduser()
    if "ION_HOME" not in os.environ and not p.exists() and legacy.is_dir():
        return legacy  # deprecation shim: operator state not migrated yet
    p.mkdir(parents=True, exist_ok=True)
    return p


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime | None = None) -> str:
    return (t or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def days_since(s: str | None, ref: dt.datetime | None = None) -> float | None:
    t = parse_ts(s)
    if t is None:
        return None
    return ((ref or now()) - t).total_seconds() / 86400


def which(name: str, extra: list[str] | None = None) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for d in extra or []:
        c = Path(d).expanduser() / name
        if c.exists() and os.access(c, os.X_OK):
            return str(c)
    return None


def filter_repo_path() -> str | None:
    return which("git-filter-repo", ["~/Library/Python/3.9/bin", "~/.local/bin"])


def git_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    fr = filter_repo_path()
    if fr:
        env["PATH"] = str(Path(fr).parent) + os.pathsep + env.get("PATH", "")
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


def redact(text: str) -> str:
    """Scrub anything that looks like a token before it can reach a log."""
    return re.sub(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-ant-[A-Za-z0-9_-]{20,})", "***", text)


def run(argv: list[str], cwd: str | Path | None = None, env: dict | None = None, input: str | None = None,
        timeout: int | None = 600, check: bool = True) -> subprocess.CompletedProcess:
    log.debug("run %s", redact(" ".join(argv)))
    p = subprocess.run(argv, cwd=cwd, env=env, input=input, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed ({p.returncode}): {redact(p.stderr.strip() or p.stdout.strip())[:800]}")
    return p


def guard_git(argv: list[str], default_branch: str | None = None) -> None:
    """Refuse destructive git invocations. Used by every executor git call."""
    if not argv or argv[0] != "git":
        return
    args = [a for a in argv[1:]]
    if "push" in args:
        for a in args:
            if a in FORBIDDEN_GIT_FLAGS or a.startswith("+") or a.startswith("--force"):
                raise SafetyError(f"refusing forced or destructive push: {a}")
            if a.startswith(":"):
                raise SafetyError("refusing to delete a remote ref")
    if "branch" in args and ("-D" in args or "-d" in args or "--delete" in args):
        raise SafetyError("refusing to delete a branch")


def git(argv: list[str], cwd: str | Path | None = None, **kw) -> subprocess.CompletedProcess:
    full = ["git", *argv]
    guard_git(full)
    return run(full, cwd=cwd, env=git_env(kw.pop("env", None)), **kw)
