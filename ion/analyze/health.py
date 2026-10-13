"""Per-repo health findings. Pure functions over a snapshot dict plus an optional file listing."""
from __future__ import annotations

import re

from ..config import parse_repo_override
from ..util import days_since


def has_root_file(files: set[str] | None, wanted: str) -> bool | None:
    """True/False, or None when the tree is unknown. README.md matches README, README.rst ...; LICENSE matches COPYING."""
    if files is None:
        return None
    stem = re.sub(r"\.[A-Za-z]+$", "", wanted).lower()
    alts = {stem} | ({"copying", "licence", "license"} if stem in ("license", "licence") else set())
    for f in files:
        if "/" in f:
            continue
        if re.sub(r"\.[A-Za-z]+$", "", f).lower() in alts:
            return True
    return False


def override_widenings(cfg, ov: dict) -> list[str]:
    """What a repo's `.github/ion.yml` asks for beyond the org config (and so does not get)."""
    from .policy import LEVELS, org_action
    out = []
    org = {m.lower() for m in cfg["maintainers"]}
    extra = [m for m in ov.get("maintainers") or [] if m.lower() not in org]
    if extra:
        out.append("maintainers not in the org list: " + ", ".join(extra))
    for key, lvl in sorted((ov.get("policy_actions") or {}).items()):
        if LEVELS.index(lvl) > LEVELS.index(org_action(cfg, key)):
            out.append(f"policy_actions.{key}: {lvl} is above the org level {org_action(cfg, key)}")
    return out


def check(snap: dict, files: set[str] | None, cfg, now=None) -> list[dict]:
    th = cfg["thresholds"]
    f: list[dict] = []

    def add(code, sev, detail, fixable=False):
        f.append({"code": code, "severity": sev, "detail": detail, "fixable": fixable})

    if snap.get("archived"):
        return f
    if snap.get("empty"):
        add("empty_repo", "warn", "repository has no commits")
        return f
    if snap.get("override_error"):
        add("bad_override", "info", snap["override_error"])
    elif snap.get("override_text"):
        widen = override_widenings(cfg, parse_repo_override(snap["override_text"])[0])
        if widen:
            add("override_widens", "warn", ".github/ion.yml can only narrow the org config; ignored: " + "; ".join(widen))
    if files is not None:
        if has_root_file(files, "README.md") is False:
            add("missing_readme", "warn", "no README at the repository root", fixable=True)
        if has_root_file(files, "LICENSE") is False and not snap.get("license") and not snap.get("private"):
            add("missing_license", "warn", "public repository without a LICENSE")
        if not any(p.startswith(".github/workflows/") for p in files) and not snap.get("fork"):
            add("missing_ci", "info", "no GitHub Actions workflow")
    elif not snap.get("license") and not snap.get("private"):
        add("missing_license", "warn", "public repository without a detected licence")
    if not (snap.get("description") or "").strip():
        add("missing_description", "info", "no description")
    age = days_since(snap.get("pushed_at"), now)
    if age is not None and age > th["stale_days"]:
        add("stale", "warn", f"no push in {int(age)} days")
    pr_age = days_since(snap.get("oldest_pr_at"), now)
    if pr_age is not None and pr_age > th["stale_pr_days"]:
        add("old_pr", "info", f"oldest open PR is {int(pr_age)} days old")
    if (snap.get("vuln_alerts") or 0) >= th["vuln_alerts"]:
        add("vulnerabilities", "warn", f"{snap['vuln_alerts']} open vulnerability alerts")
    db = snap.get("default_branch")
    if db and db != cfg["policy"]["default_branch"]:
        add("default_branch", "info", f"default branch is '{db}', policy says '{cfg['policy']['default_branch']}'")
    return f
