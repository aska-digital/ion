"""Pairwise coupling between repos: cross-references, duplicated code, co-change, shared deps, naming."""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field

from ..gitrepo import GitRepo, is_eligible
from ..util import parse_ts
from . import minhash

MANIFESTS = ("pyproject.toml", "package.json", "requirements.txt", "Cargo.toml", "go.mod", ".gitmodules", "setup.py",
             "setup.cfg", "Pipfile", "requirements-dev.txt")
BOILERPLATE = re.compile(r"(^|/)(LICENSE[^/]*|COPYING|CODE_OF_CONDUCT\.md|CONTRIBUTING\.md|SECURITY\.md|\.gitignore|"
                         r"\.github/.*|__init__\.py|conftest\.py)$", re.I)
WEIGHTS = {"xref": 0.6, "dup": 0.6, "cochange": 0.6, "deps": 0.3, "prefix": 0.05}
CORROBORATION = 0.35   # a content signal must reach this to vouch for co-change (a README link alone, at 0.3, does not)
DEFAULT_FILTERS = {"max_changeset_size": 40, "min_revs": 5, "min_shared_revs": 3, "prefix_weight": 0.05,
                   "bot_authors": ["dependabot", "renovate", "github-actions", "[bot]", "pre-commit-ci"],
                   "own_names": [], "own_emails": []}
MAX_FILES = 2500


def norm_name(s: str) -> str:
    return re.sub(r"[-_.]+", "_", s.lower())


def author_ids(login, name, email) -> set[str]:
    ids = set()
    if login:
        ids.add(login.lower())
    if email:
        local = email.split("@")[0].lower()
        local = re.sub(r"^\d+\+", "", local).split("+")[0]
        if local and local not in ("noreply", "root"):
            ids.add(local)
    if name:
        ids.add(re.sub(r"\s+", "", name.lower()))
    return ids


def filters_from_cfg(cfg) -> dict:
    """Co-change hygiene settings: `coupling:` in the config plus Mimar's own git identity (its commits, such as
    fleet-wide policy PRs, are not evidence that two repos belong together)."""
    f = {**DEFAULT_FILTERS, **(cfg.get("coupling") or {})}
    ident = cfg.get("git_identity") or {}
    f["own_names"] = [ident["name"].lower()] if ident.get("name") else []
    f["own_emails"] = [ident["email"].lower()] if ident.get("email") else []
    return f


def ignore_reason(name: str | None, email: str | None, nfiles: int | None, f: dict) -> str | None:
    """Why a commit is left out of co-change counting: 'huge', 'bot', 'own', or None to keep it."""
    if nfiles is not None and nfiles > f["max_changeset_size"]:
        return "huge"              # sweeps (licence headers, renames, mass config) say nothing about coupling
    n, e = (name or "").lower(), (email or "").lower()
    if (n and n in f["own_names"]) or (e and e in f["own_emails"]):
        return "own"
    if any(p.lower() in n or p.lower() in e for p in f["bot_authors"]):
        return "bot"
    return None


@dataclass
class RepoData:
    name: str
    private: bool = False
    pushed_at: str | None = None
    files: dict = field(default_factory=dict)          # path -> (sha, size)
    deps: set = field(default_factory=set)
    manifest_text: str = ""
    readme_text: str = ""
    imports: set = field(default_factory=set)           # normalised module roots imported by sources
    sigs: dict = field(default_factory=dict)            # path -> minhash signature (eligible text files)
    commits: list = field(default_factory=list)         # [(date_str, set(author ids))]
    eligible_bytes: int = 0
    ignored: dict = field(default_factory=dict)         # co-change commits left out, by reason


_PY_IMPORT = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w]*)(?:\.[\w.]*)?\s+import|import\s+([A-Za-z_][\w]*))", re.M)
_JS_IMPORT = re.compile(r"""(?:from\s+|require\(\s*|import\s+)['"]([^'"./][^'"]*)['"]""")


def parse_deps(path: str, text: str) -> set[str]:
    deps: set[str] = set()
    name = path.rsplit("/", 1)[-1]
    try:
        if name == "package.json":
            data = json.loads(text)
            for k in ("dependencies", "devDependencies", "peerDependencies"):
                deps |= set((data.get(k) or {}).keys())
        elif name.startswith("requirements"):
            for line in text.splitlines():
                m = re.match(r"\s*([A-Za-z0-9_.-]+)", line)
                if m and not line.strip().startswith(("#", "-")):
                    deps.add(m.group(1))
        elif name == "pyproject.toml":
            import tomllib
            data = tomllib.loads(text)
            for d in (data.get("project") or {}).get("dependencies") or []:
                m = re.match(r"\s*([A-Za-z0-9_.-]+)", d)
                if m:
                    deps.add(m.group(1))
            for grp in ((data.get("project") or {}).get("optional-dependencies") or {}).values():
                for d in grp:
                    m = re.match(r"\s*([A-Za-z0-9_.-]+)", d)
                    if m:
                        deps.add(m.group(1))
        elif name == "Cargo.toml":
            import tomllib
            data = tomllib.loads(text)
            deps |= set((data.get("dependencies") or {}).keys())
        elif name == "go.mod":
            deps |= set(re.findall(r"^\s*(?:require\s+)?([\w.\-/]+\.[\w.\-/]+)\s+v", text, re.M))
    except Exception:  # noqa: BLE001
        pass
    return {norm_name(d) for d in deps}


def load_repo(name: str, path, ref: str | None, snap: dict | None = None, filters: dict | None = None) -> RepoData | None:
    repo = GitRepo(path, ref)
    if not repo.exists() or repo.resolve_ref() is None:
        return None
    snap = snap or {}
    rd = RepoData(name=name, private=bool(snap.get("private")), pushed_at=snap.get("pushed_at"))
    rd.files = repo.files()
    manifests = [p for p in rd.files if p.rsplit("/", 1)[-1] in MANIFESTS and rd.files[p][1] < 200_000]
    readmes = [p for p in rd.files if "/" not in p and p.lower().startswith("readme")]
    sources = [p for p in rd.files if re.search(r"\.(py|js|jsx|ts|tsx|mjs)$", p) and is_eligible(p)][:MAX_FILES]
    elig = [p for p in rd.files if is_eligible(p) and not BOILERPLATE.search(p) and rd.files[p][1] >= 1][:MAX_FILES]
    want = {rd.files[p][0] for p in manifests + readmes + sources + elig}
    blobs = repo.read_blobs(sorted(want))
    for p in manifests:
        t = blobs.get(rd.files[p][0], "")
        rd.manifest_text += "\n" + t
        rd.deps |= parse_deps(p, t)
    for p in readmes:
        rd.readme_text += "\n" + blobs.get(rd.files[p][0], "")
    for p in sources:
        t = blobs.get(rd.files[p][0], "")
        for m in _PY_IMPORT.finditer(t):
            rd.imports.add(norm_name(m.group(1) or m.group(2)))
        for m in _JS_IMPORT.finditer(t):
            mod = m.group(1)
            rd.imports.add(norm_name(mod.split("/")[1] if mod.startswith("@") and "/" in mod else mod.split("/")[0]))
    for p in elig:
        if rd.files[p][1] >= 200:
            rd.eligible_bytes += rd.files[p][1]
        t = blobs.get(rd.files[p][0])
        if t is None:
            continue
        sig = minhash.signature(minhash.shingles(minhash.normalise_lines(t)))
        if sig:
            rd.sigs[p] = sig
    filters = filters or DEFAULT_FILTERS
    commits, tried = [], False
    try:
        for c in repo.log_names(limit=400):
            tried = True
            why = ignore_reason(c["name"], c["email"], len(c["paths"]), filters)
            if why:
                rd.ignored[why] = rd.ignored.get(why, 0) + 1
                continue
            commits.append((c["date"], author_ids(None, c["name"], c["email"])))
    except RuntimeError:
        pass
    if not tried:
        for c in snap.get("commits") or []:
            why = ignore_reason(c.get("name"), c.get("email"), c.get("files"), filters)
            if why:
                rd.ignored[why] = rd.ignored.get(why, 0) + 1
                continue
            commits.append((c["date"], author_ids(c.get("login"), c.get("name"), c.get("email"))))
    rd.commits = commits
    return rd


# ---- signals ----------------------------------------------------------------------

def xref_directed(src: RepoData, target: str, org: str | None = None) -> tuple[float, list[str]]:
    """How strongly does `src` reference repo `target`? (0..1, evidence)"""
    t, ev, weights = norm_name(target), [], []
    pat = re.compile(r"(?<![\w-])" + re.escape(target).replace(r"\-", "[-_]") + r"(?![\w-])", re.I)
    in_deps = t in src.deps
    gitmod = bool(re.search(r"git(?:hub\.com)?[:/][\w.-]+/" + re.escape(target) + r"(?:\.git)?\b", src.manifest_text, re.I))
    if in_deps or gitmod or (len(target) >= 4 and pat.search(src.manifest_text)):
        weights.append(1.0)
        ev.append("named in manifest/dependencies/git URL")
    if t in src.imports:
        weights.append(0.7)
        ev.append("imported by source files")
    if len(target) >= 4 and re.search(r"github\.com/[\w.-]+/" + re.escape(target) + r"(?![\w-])|`" + re.escape(target) + "`",
                                      src.readme_text, re.I):
        weights.append(0.3)
        ev.append("linked from README")
    p = 1.0
    for w in weights:
        p *= 1 - w
    return 1 - p, ev


def dup_signal(a: RepoData, b: RepoData, min_bytes: int = 200, near: float = 0.8) -> tuple[float, dict]:
    def elig(r: RepoData):
        return {p: v for p, v in r.files.items() if p in r.sigs or (is_eligible(p) and not BOILERPLATE.search(p))}
    fa, fb = elig(a), elig(b)
    by_sha_b = {}
    for p, (sha, size) in fb.items():
        if size >= min_bytes:
            by_sha_b.setdefault(sha, []).append(p)
    exact, exact_bytes, matched_a = [], 0, set()
    for p, (sha, size) in fa.items():
        if size >= min_bytes and sha in by_sha_b:
            exact.append((p, by_sha_b[sha][0]))
            exact_bytes += size
            matched_a.add(p)
    la = {p: s for p, s in a.sigs.items() if p not in matched_a and fa.get(p, ("", 0))[1] >= min_bytes}
    matched_b = {q for _, q in exact}
    rb = {p: s for p, s in b.sigs.items() if p not in matched_b and fb.get(p, ("", 0))[1] >= min_bytes}
    pairs = minhash.near_duplicates(la, rb, near)
    near_bytes, near_pairs, used = 0, [], set()
    for pa, pb, sim in sorted(pairs, key=lambda x: -x[2]):
        if pa in used:
            continue
        used.add(pa)
        near_bytes += fa[pa][1]
        near_pairs.append((pa, pb, round(sim, 2)))
    denom = min(a.eligible_bytes, b.eligible_bytes)
    score = min(1.0, (exact_bytes + near_bytes) / denom) if denom else 0.0
    return score, {"exact": exact[:10], "exact_count": len(exact), "near": near_pairs[:10], "near_count": len(near_pairs),
                   "shared_bytes": exact_bytes + near_bytes}


def cochange_signal(a: RepoData, b: RepoData, min_shared: int = 3, min_revs: int = 5) -> tuple[float, dict]:
    """Change coupling in the usual sense: shared revisions divided by the two repos' average number of revisions.

    A "revision" is a day with a kept commit (see `ignore_reason`); a day is shared if the other repo has a commit by an
    overlapping author on the same or an adjacent day. Repos with fewer than `min_revs` revisions, or pairs sharing fewer
    than `min_shared`, give no signal at all."""
    def days(r: RepoData):
        d: dict[str, set] = {}
        for ts, ids in r.commits:
            t = parse_ts(ts)
            if t:
                d.setdefault(t.astimezone(dt.timezone.utc).date().isoformat(), set()).update(ids)
        return d
    da, db = days(a), days(b)
    if len(da) < min_revs or len(db) < min_revs:
        return 0.0, {"shared_windows": 0, "too_few_revisions": [len(da), len(db)]}

    def near_day(day: str, other: dict, ids: set) -> bool:
        d0 = dt.date.fromisoformat(day)
        for off in (0, -1, 1):
            k = (d0 + dt.timedelta(days=off)).isoformat()
            if k in other and (ids & other[k]):
                return True
        return False
    shared_a = {d for d, ids in da.items() if near_day(d, db, ids)}
    shared_b = {d for d, ids in db.items() if near_day(d, da, ids)}
    shared = min(len(shared_a), len(shared_b))
    if shared < min_shared:
        return 0.0, {"shared_windows": shared}
    avg = (len(da) + len(db)) / 2
    return min(1.0, shared / avg), {"shared_windows": shared, "days_a": len(da), "days_b": len(db),
                                    "ignored_a": a.ignored, "ignored_b": b.ignored}


def deps_signal(a: RepoData, b: RepoData) -> float:
    if len(a.deps) < 3 or len(b.deps) < 3:
        return 0.0
    return minhash.jaccard(a.deps, b.deps)


def prefix_signal(a: str, b: str) -> float:
    pa, pb = re.split(r"[-_]", a)[0].lower(), re.split(r"[-_]", b)[0].lower()
    return 1.0 if pa == pb and len(pa) >= 3 and a != b else 0.0


def combine(signals: dict[str, float], weights: dict[str, float] | None = None) -> float:
    """Noisy-or of weighted signals.

    - Co-change alone is half-weighted: one author working on several repos on the same days is weak evidence unless a
      substantive content signal (xref, duplication, shared deps, at least CORROBORATION) agrees. A mere README link
      scores 0.3 and is not enough.
    - A shared name prefix counts only when some other signal is at least 0.15: `protean-*` repos share a prefix because
      of their naming scheme, not because they should be merged, and the v1 live run showed that this alone flooded the
      coupled-pairs list."""
    w = {**WEIGHTS, **(weights or {})}
    sig = dict(signals)
    if max(sig.get("xref", 0), sig.get("dup", 0), sig.get("deps", 0)) < CORROBORATION:
        sig["cochange"] = sig.get("cochange", 0) * 0.5
    if max(sig.get(k, 0) for k in ("xref", "dup", "deps", "cochange")) < 0.15:
        sig["prefix"] = 0.0
    p = 1.0
    for k, wk in w.items():
        p *= 1 - wk * max(0.0, min(1.0, sig.get(k, 0.0)))
    return round(1 - p, 4)


def pair_coupling(a: RepoData, b: RepoData, org: str | None = None, min_bytes: int = 200, filters: dict | None = None) -> dict:
    f = filters or DEFAULT_FILTERS
    xa, eva = xref_directed(a, b.name, org)
    xb, evb = xref_directed(b, a.name, org)
    xref = min(1.0, max(xa, xb) + 0.2 * min(xa, xb))
    dup, dupev = dup_signal(a, b, min_bytes)
    co, coev = cochange_signal(a, b, f['min_shared_revs'], f['min_revs'])
    sig = {"xref": round(xref, 3), "dup": round(dup, 3), "cochange": round(co, 3),
           "deps": round(deps_signal(a, b), 3), "prefix": prefix_signal(a.name, b.name)}
    return {"a": a.name, "b": b.name, "score": combine(sig, {"prefix": f["prefix_weight"]}), "signals": sig,
            "evidence": {"a_refs_b": eva, "b_refs_a": evb, "dup": dupev, "cochange": coev,
                         "shared_deps": sorted(a.deps & b.deps)[:15]},
            "a_refs_b": xa, "b_refs_a": xb, "visibility_conflict": a.private != b.private}
