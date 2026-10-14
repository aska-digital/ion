"""Compare actual repo settings against the desired state in ion.yml."""
from __future__ import annotations

import fnmatch

from ..config import deep_merge
from .health import has_root_file


def effective_policy(cfg, override: dict | None) -> dict:
    return deep_merge(cfg["policy"], (override or {}).get("policy") or {})


LEGACY_ALLOW = {"required_files": "health_fix"}   # spelling in the old `auto_apply` list


def policy_key(cls: str) -> str:
    """`required_file:README.md` -> `required_files`; other classes are their own key."""
    return "required_files" if cls.startswith("required_file:") or cls == "health_fix" else cls


def governed(cfg, override: dict | None, repo: str) -> bool:
    """Is this repo under policy? opt-out: yes unless it says `enabled: false`. opt-in: only if it says `enabled: true`
    (or the config lists it in `policy_repos`) and has not turned itself off."""
    enabled = (override or {}).get("enabled")
    if cfg["policy_strategy"] == "opt-in":
        return enabled is True or (enabled is not False and repo in cfg["policy_repos"])
    return enabled is not False


def action_for(cfg, override: dict | None, cls: str) -> str:
    """log | issue | fix for a policy class. A repo's own `policy_actions` win over the org config; classes the org
    config does not mention are `fix` if the legacy `auto_apply` list names them, else `issue`."""
    key = policy_key(cls)
    for table in ((override or {}).get("policy_actions") or {}, cfg["policy_actions"]):
        if key in table:
            return table[key]
    legacy = {key, LEGACY_ALLOW.get(key)} & set(cfg["auto_apply"])
    return "fix" if legacy else "issue"


def _rule_for(snap: dict) -> dict | None:
    for r in snap.get("protection") or []:
        if fnmatch.fnmatch(snap.get("default_branch") or "", r.get("pattern") or ""):
            return r
    return None


def check(snap: dict, files: set[str] | None, cfg, override: dict | None = None) -> list[dict]:
    """Findings: {class, status: ok|drift|blocked_by_plan|unknown|skipped, detail, fix}."""
    pol = effective_policy(cfg, override)
    out: list[dict] = []
    repo, private = snap["name"], snap.get("private")
    if snap.get("archived") or snap.get("empty") or not governed(cfg, override, repo):
        return out
    free = cfg.get("plan", "free") == "free"

    def add(cls, status, detail, fix=None):
        out.append({"repo": repo, "class": cls, "status": status, "detail": detail, "fix": fix,
                    "action": action_for(cfg, override, cls)})

    # branch protection
    bp = pol.get("branch_protection")
    if bp:
        if snap.get("fork"):
            add("branch_protection", "skipped", "fork")
        elif private and free:
            add("branch_protection", "blocked_by_plan", "private-repo branch protection needs a paid plan")
        else:
            rule, branch = _rule_for(snap), snap.get("default_branch")
            if rule is None:
                add("branch_protection", "drift", f"no protection on {branch}", {"class": "branch_protection", "repo": repo, "branch": branch, "settings": bp})
            else:
                diffs = []
                if bool(rule.get("requiresApprovingReviews")) != bool(bp.get("require_pr")):
                    diffs.append("require_pr")
                elif bp.get("require_pr") and rule.get("requiredApprovingReviewCount", 0) != bp.get("required_approvals", 0):
                    diffs.append("required_approvals")
                if bool(rule.get("allowsForcePushes")) != bool(bp.get("allow_force_pushes")):
                    diffs.append("allow_force_pushes")
                if bool(rule.get("allowsDeletions")) != bool(bp.get("allow_deletions")):
                    diffs.append("allow_deletions")
                if bool(rule.get("isAdminEnforced")) != bool(bp.get("enforce_admins")):
                    diffs.append("enforce_admins")
                if diffs:
                    add("branch_protection", "drift", "differs: " + ", ".join(diffs),
                        {"class": "branch_protection", "repo": repo, "branch": branch, "settings": bp})
                else:
                    add("branch_protection", "ok", "matches policy")
    # dependabot alerts
    if pol.get("dependabot_alerts"):
        en = snap.get("vuln_enabled")
        if en is None:
            add("dependabot_alerts", "unknown", "status not readable")
        elif en:
            add("dependabot_alerts", "ok", "enabled")
        else:
            add("dependabot_alerts", "drift", "Dependabot alerts disabled", {"class": "dependabot_alerts", "repo": repo})
    # secret scanning
    ss = pol.get("secret_scanning")
    if ss and ss != "off":
        if private and ss == "public_only":
            if free:
                add("secret_scanning", "blocked_by_plan", "secret scanning on private repos needs a paid plan")
            else:
                add("secret_scanning", "unknown", "secret scanning status not collected for private repos")
        elif snap.get("fork"):
            add("secret_scanning", "skipped", "fork")
        else:
            st = snap.get("secret_scanning")
            if st is None:
                add("secret_scanning", "unknown", "status not readable")
            elif st == "enabled":
                add("secret_scanning", "ok", "enabled")
            else:
                add("secret_scanning", "drift", "secret scanning disabled", {"class": "secret_scanning", "repo": repo})
    # required files
    for req in pol.get("required_files") or []:
        if req.upper().startswith("LICENSE") and private:
            continue
        have = has_root_file(files, req)
        if have is None:
            have = True if (req.upper().startswith("LICENSE") and snap.get("license")) else None
        if have is None:
            add("required_file:" + req, "unknown", "tree not mirrored")
        elif have:
            add("required_file:" + req, "ok", "present")
        else:
            add("required_file:" + req, "drift", f"{req} missing",
                {"class": "health_fix", "repo": repo, "file": req})
    return out
