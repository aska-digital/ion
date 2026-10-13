"""Cohesion inside one repo: cluster top-level units, look for separable halves."""
from __future__ import annotations

import itertools
import re

from ..gitrepo import GitRepo, is_eligible
from .coupling import MANIFESTS, author_ids, norm_name, parse_deps

SUPPORT_DIRS = {"docs", "doc", "tests", "test", "scripts", "examples", "example", "tools", "assets", "static", "fixtures",
                "benchmarks", "deploy", "config", "configs"}
CONTAINER_DIRS = {"src", "packages", "apps", "services", "libs", "lib", "modules", "crates", "cmd"}
_PY_IMPORT = re.compile(r"^\s*(?:from\s+([A-Za-z_]\w*)[\w.]*\s+import|import\s+([A-Za-z_]\w*))", re.M)
_JS_REL = re.compile(r"""(?:from\s+|require\(\s*|import\s+)['"](\.{1,2}/[^'"]*)['"]""")
_JS_BARE = re.compile(r"""(?:from\s+|require\(\s*|import\s+)['"]([A-Za-z@][^'"./][^'"]*)['"]""")


def units(paths: list[str]) -> list[str]:
    tops = sorted({p.split("/")[0] for p in paths if "/" in p and not p.startswith(".")} - SUPPORT_DIRS)
    out: list[str] = []
    for t in tops:
        if t in CONTAINER_DIRS:
            subs = sorted({"/".join(p.split("/")[:2]) for p in paths if p.startswith(t + "/") and p.count("/") >= 2})
            out.extend(subs if subs else [t])
        else:
            out.append(t)
    return out


def unit_of(path: str, us: list[str]) -> str | None:
    best = None
    for u in us:
        if path.startswith(u + "/") and (best is None or len(u) > len(best)):
            best = u
    return best


def _components(nodes: list[str], edges: dict, thr: float) -> list[list[str]]:
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for (a, b), w in edges.items():
        if w >= thr:
            parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for n in nodes:
        groups.setdefault(find(n), []).append(n)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


def cluster(nodes: list[str], edges: dict, thr: float = 0.3) -> list[list[str]]:
    comps = _components(nodes, edges, thr)
    try:
        import networkx as nx
    except ImportError:
        return comps
    out = []
    for comp in comps:
        if len(comp) < 6:
            out.append(comp)
            continue
        g = nx.Graph()
        g.add_nodes_from(comp)
        for (a, b), w in edges.items():
            if a in comp and b in comp and w >= thr:
                g.add_edge(a, b, weight=w)
        parts = nx.community.louvain_communities(g, weight="weight", seed=1)
        out.extend(sorted(p) for p in parts)
    return sorted(out, key=lambda g: g[0])


def analyze_repo(name: str, path, ref: str | None, cfg, snap: dict | None = None) -> dict | None:
    repo = GitRepo(path, ref)
    if not repo.exists() or repo.resolve_ref() is None:
        return None
    th = cfg["thresholds"]
    files = {p: v for p, v in repo.files().items() if is_eligible(p) or p.rsplit("/", 1)[-1] in MANIFESTS}
    us = units(list(files))
    if len(us) < 2:
        return None
    nfiles = {u: 0 for u in us}
    nbytes = {u: 0 for u in us}
    for p, (sha, size) in files.items():
        u = unit_of(p, us)
        if u:
            nfiles[u] += 1
            nbytes[u] += size
    nodes = [u for u in us if nfiles[u] > 0]
    # import edges
    mod_to_unit = {norm_name(u.split("/")[-1]): u for u in nodes}
    srcs = [p for p in files if re.search(r"\.(py|js|jsx|ts|tsx|mjs)$", p) and unit_of(p, us)]
    blobs = repo.read_blobs(sorted({files[p][0] for p in srcs}))
    imp: dict[tuple[str, str], int] = {}
    for p in srcs:
        u = unit_of(p, us)
        t = blobs.get(files[p][0], "")
        targets = []
        for m in _PY_IMPORT.finditer(t):
            tu = mod_to_unit.get(norm_name(m.group(1) or m.group(2)))
            if tu:
                targets.append(tu)
        for m in _JS_REL.finditer(t):
            import posixpath
            rel = posixpath.normpath(posixpath.join(posixpath.dirname(p), m.group(1)))
            tu = unit_of(rel + "/x", us)
            if tu:
                targets.append(tu)
        for m in _JS_BARE.finditer(t):
            tu = mod_to_unit.get(norm_name(m.group(1).split("/")[0]))
            if tu:
                targets.append(tu)
        for tu in targets:
            if tu != u:
                k = tuple(sorted((u, tu)))
                imp[k] = imp.get(k, 0) + 1
    # co-change edges
    commits = repo.log_names(limit=400)
    touched: dict[str, set] = {u: set() for u in nodes}
    authors: dict[str, set] = {u: set() for u in nodes}
    for c in commits:
        seen = {unit_of(p, us) for p in c["paths"]} - {None}
        for u in seen:
            if u in touched:
                touched[u].add(c["oid"])
                authors[u] |= author_ids(None, c["name"], c["email"])
    edges: dict[tuple[str, str], float] = {}
    for a, b in itertools.combinations(sorted(nodes), 2):
        w_imp = min(1.0, imp.get((a, b), 0) / 2)
        both = len(touched[a] & touched[b])
        lo = min(len(touched[a]), len(touched[b]))
        w_co = both / lo if lo >= 3 else 0.0
        w = max(w_imp, w_co)
        if w > 0:
            edges[(a, b)] = round(w, 3)
    clusters = cluster(nodes, edges)
    total_files = sum(nfiles.values()) or 1
    meaningful = [c for c in clusters
                  if sum(nfiles[u] for u in c) >= th["min_cluster_files"]
                  and sum(nfiles[u] for u in c) / total_files >= th["min_cluster_share"]]
    result = {"repo": name, "units": nodes, "clusters": clusters, "edges": {f"{a}|{b}": w for (a, b), w in edges.items()},
              "candidates": []}
    if len(meaningful) < 2:
        return result
    manifest_units = {unit_of(p, us) for p in files if p.rsplit("/", 1)[-1] in MANIFESTS} - {None}

    def deps_of(units_: list[str]) -> set:
        d = set()
        for p in files:
            if p.rsplit("/", 1)[-1] in MANIFESTS and unit_of(p, us) in units_:
                d |= parse_deps(p, repo.read_blobs([files[p][0]]).get(files[p][0], ""))
        return d

    for c in sorted(meaningful, key=lambda c: sum(nfiles[u] for u in c)):
        rest = [u for u in nodes if u not in c]
        cs = set(c)
        cross = sum(w for (a, b), w in edges.items() if (a in cs) != (b in cs))
        intra = sum(w for (a, b), w in edges.items() if (a in cs) == (b in cs))
        if cross + intra == 0:
            both_have = bool(cs & manifest_units) and bool(set(rest) & manifest_units)
            gap = 1.0 if both_have else 0.0
        else:
            gap = 1 - cross / (cross + intra)
        da, db = deps_of(c), deps_of(rest)
        dep_dist = (1 - len(da & db) / len(da | db)) if (da or db) else 0.5
        ca = set().union(*[authors[u] for u in c]) if c else set()
        cb = set().union(*[authors[u] for u in rest]) if rest else set()
        contrib_dist = (1 - len(ca & cb) / len(ca | cb)) if (ca or cb) else 0.0
        score = round(0.6 * gap + 0.2 * dep_dist + 0.2 * contrib_dist, 4)
        result["candidates"].append({
            "dirs": list(c), "rest": rest, "gap": round(gap, 3), "score": score, "files": sum(nfiles[u] for u in c),
            "bytes": sum(nbytes[u] for u in c), "cross_weight": round(cross, 3), "intra_weight": round(intra, 3),
            "dep_distance": round(dep_dist, 3), "contributor_distance": round(contrib_dist, 3),
            "qualifies": gap >= th["split_cohesion_gap"]})
    return result
