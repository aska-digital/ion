"""Analyze: deterministic signals from snapshots and mirrors."""
from __future__ import annotations

import itertools
import json
import logging

from ..collect.snapshot import latest_snapshots
from ..config import parse_repo_override
from ..gitrepo import GitRepo
from ..util import iso
from . import cohesion, coupling, health, lifecycle, policy

log = logging.getLogger("mimar")


def run_analyze(ctx) -> dict:
    cfg, db = ctx.cfg, ctx.db
    snaps, gen = latest_snapshots(db)
    live = {n: s for n, s in snaps.items() if not s["archived"] and not s["fork"] and not s.get("empty")
            and n not in cfg["exclude"]}
    overrides = {n: parse_repo_override(s.get("override_text"))[0] for n, s in snaps.items()}
    ignored = {n for n, o in overrides.items() if o.get("ignore")}
    live = {n: s for n, s in live.items() if n not in ignored}
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
                tree[n] = set(g.files())
                if n in live:
                    rd = coupling.load_repo(n, mp, s["default_branch"], s)
                    if rd:
                        data[n] = rd
    health_f = {n: health.check(s, tree.get(n), cfg) for n, s in snaps.items() if n not in cfg["exclude"] and n not in ignored}
    policy_f = {n: policy.check(s, tree.get(n), cfg, overrides.get(n)) for n, s in snaps.items()
                if n not in cfg["exclude"] and n not in ignored}
    pairs = []
    for a, b in itertools.combinations(sorted(data), 2):
        p = coupling.pair_coupling(data[a], data[b], cfg["org"], int(cfg["thresholds"]["dup_min_bytes"]))
        if p["score"] >= 0.15:
            pairs.append(p)
    pairs.sort(key=lambda p: -p["score"])
    cohs = []
    for n, s in live.items():
        if n in data:
            r = cohesion.analyze_repo(n, ctx.mirror_path(n), s["default_branch"], cfg, s)
            if r and r["candidates"]:
                cohs.append(r)
    arch = lifecycle.archive_candidates(live, pairs, cfg)
    result = {"gen": gen, "repos": sorted(snaps), "analyzed": sorted(data), "health": health_f, "policy": policy_f,
              "coupling": pairs, "cohesion": cohs, "archive": arch, "lagging_forks": lifecycle.lagging_forks(snaps)}
    db.x("insert into analyses(ts,gen,blob) values(?,?,?)", (iso(), gen, json.dumps(result)))
    return result


def latest_analysis(db) -> dict | None:
    r = db.one("select blob from analyses order by id desc limit 1")
    return json.loads(r["blob"]) if r else None
