"""Analyze: deterministic signals from snapshots and mirrors."""
from __future__ import annotations

import itertools
import json
import logging

from ..collect.snapshot import latest_snapshots
from ..config import parse_repo_override
from ..gitrepo import GitRepo
from ..util import iso
from . import boundaries, cohesion, coupling, health, lifecycle, policy

log = logging.getLogger("mimar")


def run_analyze(ctx) -> dict:
    result = compute_analysis(ctx)
    ctx.db.x("insert into analyses(ts,gen,blob) values(?,?,?)", (iso(), result["gen"], json.dumps(result)))
    return result


def compute_analysis(ctx, cache: dict | None = None) -> dict:
    """The whole analysis as a pure function of the stored snapshots, the mirrors and the config. Writes nothing.
    `cache` lets two runs under different configs (plan-config) share the expensive per-repo loads."""
    cfg, db = ctx.cfg, ctx.db
    cache = cache if cache is not None else {}
    snaps, gen = latest_snapshots(db)
    live = {n: s for n, s in snaps.items() if not s["archived"] and not s["fork"] and not s.get("empty")
            and n not in cfg["exclude"]}
    overrides = {n: parse_repo_override(s.get("override_text"))[0] for n, s in snaps.items()}
    ignored = {n for n, o in overrides.items() if o.get("ignore")}
    live = {n: s for n, s in live.items() if n not in ignored}
    filters = coupling.filters_from_cfg(cfg)
    tree: dict[str, set | None] = {}
    data: dict[str, coupling.RepoData] = {}
    for n, s in snaps.items():
        if n in cfg["exclude"] or n in ignored:
            continue
        mp = ctx.mirror_path(n)
        tree[n] = None
        if mp.exists() and s.get("default_branch"):
            g = GitRepo(mp, s["default_branch"])
            if g.resolve_ref():
                tree[n] = cache.setdefault(("tree", n, s.get("head_oid")), set(g.files()))
                if n in live:
                    key = ("repo", n, s.get("head_oid"), json.dumps(filters, sort_keys=True))
                    if key not in cache:
                        cache[key] = coupling.load_repo(n, mp, s["default_branch"], s, filters)
                    if cache[key]:
                        data[n] = cache[key]
    health_f = {n: health.check(s, tree.get(n), cfg) for n, s in snaps.items() if n not in cfg["exclude"] and n not in ignored}
    policy_f = {n: policy.check(s, tree.get(n), cfg, overrides.get(n)) for n, s in snaps.items()
                if n not in cfg["exclude"] and n not in ignored}
    pairs = []
    for a, b in itertools.combinations(sorted(data), 2):
        p = coupling.pair_coupling(data[a], data[b], cfg["org"], int(cfg["thresholds"]["dup_min_bytes"]), filters)
        if p["score"] >= 0.15:
            pairs.append(p)
    pairs.sort(key=lambda p: -p["score"])
    cohs = []
    for n, s in live.items():
        if n in data:
            ck = ("coh", n, s.get("head_oid"), json.dumps(filters, sort_keys=True), json.dumps(cfg["thresholds"], sort_keys=True))
            if ck not in cache:
                cache[ck] = cohesion.analyze_repo(n, ctx.mirror_path(n), s["default_branch"], cfg, s, filters)
            r = cache[ck]
            if r and r["candidates"]:
                cohs.append(r)
    arch = lifecycle.archive_candidates(live, pairs, cfg)
    result = {"lifecycle": lifecycle.stages(snaps, cfg), "boundaries": boundaries.violations(snaps, pairs, cfg), "gen": gen, "repos": sorted(snaps), "analyzed": sorted(data), "health": health_f, "policy": policy_f,
              "coupling": pairs, "cohesion": cohs, "archive": arch, "lagging_forks": lifecycle.lagging_forks(snaps)}
    return result


def latest_analysis(db) -> dict | None:
    r = db.one("select blob from analyses order by id desc limit 1")
    return json.loads(r["blob"]) if r else None
