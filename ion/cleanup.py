"""Explicit cleanup of retained executor clones; active and unknown work stays intact."""
from __future__ import annotations

import shutil

TERMINAL = {"done", "failed", "rejected", "dropped"}


def prune_work(ctx, apply=False):
    root = ctx.work_dir(".").resolve()
    result = {"candidates": [], "removed": [], "preserved": [], "errors": []}
    if not root.is_dir():
        return result
    statuses = {r["id"]: r["status"] for r in ctx.db.q("select id,status from proposals")}
    for path in sorted(root.iterdir()):
        # Never follow a link, even to a terminal proposal directory.
        if path.is_symlink() or not path.is_dir() or statuses.get(path.name) not in TERMINAL:
            result["preserved"].append(path.name)
            continue
        result["candidates"].append(path.name)
        if apply:
            # Recheck immediately before deletion, in case a proposal was resumed.
            row = ctx.db.one("select status from proposals where id=?", (path.name,))
            if not row or row["status"] not in TERMINAL:
                result["preserved"].append(path.name)
                continue
            try:
                shutil.rmtree(path)
                result["removed"].append(path.name)
            except OSError as e:
                result["errors"].append(f"{path.name}: {e}")
    return result
