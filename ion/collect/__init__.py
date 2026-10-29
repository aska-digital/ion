"""Collect: GraphQL metadata + git mirrors + snapshots."""
from __future__ import annotations

import logging

from ..config import parse_repo_override
from . import github, mirror, snapshot

log = logging.getLogger("ion")


def run_collect(ctx) -> dict:
    cfg, db = ctx.cfg, ctx.db
    if ctx.sync_mirrors and not getattr(ctx.gh, "supports_git", True):
        from ..util import Refused
        raise Refused("GitHub App git transport is unsupported; collect with --no-mirror")
    run_id = snapshot.start_run(db)
    result = {"run_id": run_id, "repos": 0, "cost": 0, "mirrors": {}, "warnings": [], "partial": False}
    try:
        col = github.collect_org(ctx.gh, cfg.org, cfg["budget"]["graphql_points_per_run"],
                                 cfg["collect"]["commits"], cfg["collect"]["page_size"])
    except Exception as e:  # noqa: BLE001
        snapshot.finish_run(db, run_id, 0, None, True, 0, f"error: {e}")
        raise
    result.update(cost=col.cost, partial=col.partial, warnings=list(col.warnings), remaining=col.remaining)
    for snap in col.repos:
        _, err = parse_repo_override(snap.get("override_text"))
        if err:
            snap["override_error"] = err
        if not snap["private"] and not snap["fork"] and not snap["archived"]:
            extras = github.fetch_security_extras(ctx.gh, cfg.org, snap)
            if extras:
                snap.update(extras)
        snapshot.write_snapshot(db, run_id, snap)
        result["repos"] += 1
        skip = snap["name"] in cfg["exclude"] or snap["fork"]
        if ctx.sync_mirrors and not skip:
            try:
                result["mirrors"][snap["name"]] = mirror.sync_mirror(
                    db, cfg.org, snap, url=ctx.remote_url(snap["name"]) if ctx.remote_url_fn else None,
                    path=ctx.mirror_path(snap["name"]) if ctx.mirror_path_fn else None)
            except Exception as e:  # noqa: BLE001
                result["mirrors"][snap["name"]] = "error"
                result["warnings"].append(f"mirror {snap['name']}: {str(e)[:200]}")
    snapshot.finish_run(db, run_id, col.cost, col.remaining, col.partial, result["repos"], "; ".join(result["warnings"])[:500])
    return result
