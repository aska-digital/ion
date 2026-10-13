#!/usr/bin/env python3
"""Fail if any dependency's licence is outside the allowlist.

Mimar is Apache-2.0 and may only depend on permissively licensed code: MIT, BSD, Apache-2.0, ISC, PSF and MPL-2.0
(the last as an unmodified dependency). This script has no dependencies of its own and runs anywhere Python 3.11+ does:

    uv run python scripts/check_licenses.py            # the declared dependencies and everything they pull in
    uv run python scripts/check_licenses.py --all      # every distribution installed in this environment

It reads the installed packages' metadata (PEP 639 `License-Expression`, then trove classifiers, then the `License`
field) and evaluates SPDX expressions: `A OR B` passes if either side is allowed, `A AND B` needs both. Unknown or
missing licences fail rather than pass; add a justified entry to OVERRIDES if a package genuinely has no metadata.
"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

ALLOWED = {"MIT", "BSD", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "APACHE-2.0", "ISC", "PSF", "PSF-2.0", "MPL-2.0"}
# Never allowed as a dependency, whatever its metadata says (pygit2 is GPLv2 with a linking exception).
DENY_PACKAGES = {"pygit2"}
# package name (normalised) -> (licence, reason). Keep empty unless metadata is genuinely missing.
OVERRIDES: dict[str, tuple[str, str]] = {}

CLASSIFIERS = {
    "MIT License": "MIT", "BSD License": "BSD", "Apache Software License": "Apache-2.0", "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0", "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
}
TEXT_HINTS = [(r"\bMIT\b", "MIT"), (r"Apache(?:\s+License)?,?\s+(?:Version\s+)?2", "Apache-2.0"), (r"\bBSD\b", "BSD"),
              (r"\bISC\b", "ISC"), (r"Python Software Foundation|\bPSF\b", "PSF-2.0"), (r"Mozilla Public License.*2", "MPL-2.0")]


def norm_name(n: str) -> str:
    return re.sub(r"[-_.]+", "-", n).lower()


def _tokens(expr: str) -> list[str]:
    return re.findall(r"\(|\)|[^\s()]+", expr)


def expression_ok(expr: str) -> tuple[bool, list[str]]:
    """Evaluate an SPDX expression against ALLOWED. Returns (ok, the licence ids that were not allowed)."""
    toks = _tokens(expr)
    pos = 0
    bad: list[str] = []

    def atom():
        nonlocal pos
        t = toks[pos]
        if t == "(":
            pos += 1
            v = orx()
            pos += 1                                # ")"
            return v
        pos += 1
        if pos < len(toks) and toks[pos].upper() == "WITH":     # "GPL-2.0 WITH Classpath-exception": still the base licence
            pos += 2
        lic = re.sub(r"\+$", "", t).upper()
        lic = re.sub(r"-(ONLY|OR-LATER)$", "", lic)
        ok = lic in ALLOWED
        if not ok:
            bad.append(t)
        return ok

    def andx():
        nonlocal pos
        v = atom()
        while pos < len(toks) and toks[pos].upper() == "AND":
            pos += 1
            v = atom() and v
        return v

    def orx():
        nonlocal pos
        v = andx()
        while pos < len(toks) and toks[pos].upper() == "OR":
            pos += 1
            v = andx() or v
        return v

    if not toks:
        return False, ["(empty)"]
    ok = orx()
    return ok, ([] if ok else bad)


def licence_of(dist) -> tuple[str | None, str]:
    """(SPDX-like expression, where it came from)."""
    m = dist.metadata
    name = norm_name(m["Name"])
    if name in OVERRIDES:
        return OVERRIDES[name][0], "override"
    if m.get("License-Expression"):
        return m["License-Expression"].strip(), "License-Expression"
    cls = [c.split(" :: ")[-1] for c in m.get_all("Classifier") or [] if c.startswith("License :: ")]
    mapped = [CLASSIFIERS.get(c, c) for c in cls if c != "OSI Approved"]
    if mapped:
        return " OR ".join(mapped), "classifiers"          # several classifiers mean a dual licence: the user may pick one
    lic = (m.get("License") or "").strip()
    if lic and lic.upper() != "UNKNOWN":
        if len(lic) < 80 and "\n" not in lic:
            return lic, "License field"
        for rx, spdx in TEXT_HINTS:
            if re.search(rx, lic[:2000]):
                return spdx, "License text"
    return None, "none"


def requirement_name(req: str) -> str | None:
    base = req.split(";")[0]
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", base)
    if not m:
        return None
    if "extra ==" in req.split(";", 1)[-1] and ";" in req:
        return None                                          # an optional extra of a dependency, not requested
    return norm_name(m.group(1))


def declared(pyproject: Path) -> set[str]:
    data = tomllib.loads(pyproject.read_text())
    names: set[str] = set()
    proj = data.get("project", {})
    for r in proj.get("dependencies", []):
        names.add(requirement_name(r))
    for grp in proj.get("optional-dependencies", {}).values():
        for r in grp:
            names.add(requirement_name(r))
    for grp in data.get("dependency-groups", {}).values():
        for r in grp:
            if isinstance(r, str):
                names.add(requirement_name(r))
    return {n for n in names if n}


def closure(roots: set[str]) -> tuple[dict[str, object], list[str]]:
    """Installed distributions reachable from `roots`; names that are required but not installed."""
    seen: dict[str, object] = {}
    missing: list[str] = []
    todo = sorted(roots)
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        try:
            d = metadata.distribution(n)
        except metadata.PackageNotFoundError:
            missing.append(n)
            continue
        seen[n] = d
        for r in d.requires or []:
            rn = requirement_name(r)
            if rn and rn not in seen:
                todo.append(rn)
    return seen, missing


def check(dists: dict[str, object]) -> list[dict]:
    rows = []
    for n, d in sorted(dists.items()):
        expr, src = licence_of(d)
        if n in DENY_PACKAGES:
            ok, bad = False, ["denied package"]
        elif expr is None:
            ok, bad = False, ["no licence metadata"]
        else:
            ok, bad = expression_ok(expr)
        rows.append({"name": d.metadata["Name"], "version": d.metadata["Version"], "license": expr or "UNKNOWN",
                     "source": src, "ok": ok, "bad": bad})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true", help="check every installed distribution, not only Mimar's closure")
    ap.add_argument("--pyproject", default=str(Path(__file__).resolve().parent.parent / "pyproject.toml"))
    args = ap.parse_args(argv)
    pyproject = Path(args.pyproject)
    roots = declared(pyproject)
    denied = sorted(roots & DENY_PACKAGES)
    if args.all:
        dists = {norm_name(d.metadata["Name"]): d for d in metadata.distributions()}
        missing: list[str] = []
    else:
        dists, missing = closure(roots)
    dists.pop("mimar", None)                                  # ourselves
    rows = check(dists)
    width = max((len(r["name"]) for r in rows), default=10)
    for r in rows:
        print(f"{'ok  ' if r['ok'] else 'FAIL'} {r['name']:<{width}} {r['version']:<12} {r['license']}"
              + ("" if r["ok"] else f"   <- not allowed: {', '.join(r['bad'])}"))
    for n in missing:
        print(f"warn {n}: required but not installed in this environment (skipped; install it to check it)")
    undeclared = [n for n in sorted(roots) if n not in dists and n not in DENY_PACKAGES and n not in missing]
    failed = [r for r in rows if not r["ok"]]
    print(f"\n{len(rows)} packages checked; allowed: MIT, BSD, Apache-2.0, ISC, PSF, MPL-2.0; "
          f"{len(failed)} not allowed" + (f"; denied in pyproject: {', '.join(denied)}" if denied else ""))
    return 1 if (failed or denied or undeclared) else 0


if __name__ == "__main__":
    sys.exit(main())
