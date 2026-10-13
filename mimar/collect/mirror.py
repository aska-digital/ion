"""Bare mirrors under ~/.mimar/cache, authenticated through a gh credential helper (token never in argv)."""
from __future__ import annotations

from pathlib import Path

from ..util import git, home

CRED_HELPER = (
    '!f() { test "$1" = get && { echo username=x-access-token; echo "password=$(gh auth token)"; }; }; f'
)


def cache_dir() -> Path:
    d = home() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def mirror_path(repo: str) -> Path:
    return cache_dir() / f"{repo}.git"


def remote_url(org: str, repo: str) -> str:
    return f"https://github.com/{org}/{repo}.git"


def sync_mirror(db, org: str, snap: dict, url: str | None = None, path: Path | None = None) -> str:
    """Clone or fetch. Returns 'cloned', 'fetched', 'skipped' or 'empty'."""
    if snap.get("empty"):
        return "empty"
    path = path or mirror_path(snap["name"])
    url = url or remote_url(org, snap["name"])
    key = f"mirror:{snap['name']}"
    helper = ["-c", "credential.helper=", "-c", f"credential.helper={CRED_HELPER}"] if url.startswith("https://") else []
    if not path.exists():
        git([*helper, "clone", "--mirror", "--quiet", url, str(path)])
        db.kv_set(key, snap.get("pushed_at"))
        return "cloned"
    if db.kv_get(key) == snap.get("pushed_at"):
        return "skipped"
    git([*helper, "-C", str(path), "fetch", "--prune", "--quiet", "origin"])
    db.kv_set(key, snap.get("pushed_at"))
    return "fetched"
