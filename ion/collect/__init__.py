"""Collect: GraphQL metadata + git mirrors + snapshots."""
from __future__ import annotations

import logging

from ..config import parse_repo_override
from . import github, mirror, snapshot

log = logging.getLogger("ion")


def run_collect(ctx) -> dict:
    cfg, db = ctx.cfg, ctx.db
    key = f"collect_checkpoint:{cfg.org}"
    checkpoint = db.kv_get(key) or {}
    run_id = checkpoint.get("run_id") or snapshot.start_run(db)
    # A pass spans budget-limited invocations but remains one snapshot generation.
    db.kv_set(key, {"run_id": run_id, "after": checkpoint.get("after")})
    result = {"run_id": run_id, "repos": 0, "cost": 0, "mirrors": {}, "warnings": [], "partial": False}
    def persist_page(page, after):
        for snap in page:
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
        db.kv_set(key, {"run_id": run_id, "after": after})

    try:
        col = github.collect_org(ctx.gh, cfg.org, cfg["budget"]["graphql_points_per_run"],
                                 cfg["collect"]["commits"], cfg["collect"]["page_size"],
                                 after=checkpoint.get("after"), on_page=persist_page)
    except Exception as e:
        snapshot.finish_run(db, run_id, 0, None, True, result["repos"], f"error: {e}")
        raise
    result.update(cost=col.cost, partial=col.partial, warnings=result["warnings"] + list(col.warnings), remaining=col.remaining)
    if not col.partial:
        db.kv_set(key, None)
    total_repos = db.one("select count(*) as n from snapshots where run_id=?", (run_id,))["n"]
    snapshot.finish_run(db, run_id, col.cost, col.remaining, col.partial, total_repos, "; ".join(result["warnings"])[:500])
    return result
