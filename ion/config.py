"""Config loading: defaults + ion.yml + per-repo overrides, validated with jsonschema."""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

import jsonschema
import yaml

from .util import home

DEFAULTS: dict = {
    "org": None,
    "paused": False,
    "maintainers": [],
    "plan": "free",
    "tracker_repo": "ion-tracker",  # keep this repo PRIVATE: proposal bodies name private repos
    "approval_sources": ["github_label"],
    "bus": {"url": "http://127.0.0.1:8787", "seat": "ion", "notify": None, "maintainer_seats": [],
            "client": None, "python": None, "key": None},
    "notifiers": [{"type": "stdout"}],
    "llm": {"backend": "none", "model_default": "haiku", "model_structural": "sonnet", "max_calls_per_run": 20,
            "timeout_s": 180, "api_models": {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5"}},
    "budget": {"graphql_points_per_run": 500},
    "collect": {"commits": 30, "page_size": 10},
    "policy": {
        "default_branch": "main",
        "branch_protection": {"require_pr": True, "required_approvals": 0, "allow_force_pushes": False,
                              "allow_deletions": False, "enforce_admins": False},
        "dependabot_alerts": True,
        "secret_scanning": "public_only",
        "required_files": ["README.md", "LICENSE"],
    },
    "auto_apply": [],             # legacy spelling of `policy_actions: {<class>: fix}`
    "policy_actions": {},         # class -> log | issue | fix. Unlisted classes: fix if in auto_apply, else issue
    "policy_strategy": "opt-out",  # opt-out: every repo is governed unless it opts out; opt-in: only repos that opt in
    "policy_repos": [],           # opt-in strategy: repos governed without needing their own .github/ion.yml
    "coupling": {"max_changeset_size": 40, "min_revs": 5, "min_shared_revs": 3, "prefix_weight": 0.05,
                 "bot_authors": ["dependabot", "renovate", "github-actions", "[bot]", "pre-commit-ci"]},
    "lifecycle": {"dormant_days": 90, "frozen": [], "frozen_topic": "ion-frozen"},
    "boundaries": {"rules": [], "scope_prefix": "scope:", "block_cross_scope_merge": True},
    "thresholds": {"merge_coupling": 0.6, "split_cohesion_gap": 0.5, "stale_days": 180, "persist_runs": 2,
                   "cooldown_days": 60, "rescore_delta": 0.15, "archive_score": 0.7, "stale_pr_days": 30,
                   "vuln_alerts": 5, "min_cluster_files": 8, "min_cluster_share": 0.15, "dup_min_bytes": 200},
    "approval_ttl_days": 14,
    "quorum": 1,
    "quorum_by_kind": {},
    "archive_requires_second_approval": True,
    "exclude": [],
    "git_identity": {"name": "ion", "email": "ion@askaconsult.com"},
    "ai_attribution": False,
    "license_template": None,
    "report": {"org_health_issue": False},
    "writes": {"caps": {}},
    "guards": {"enabled": True, "max_repo_fraction": 0.25, "min_repos": 4, "max_archives": 2, "max_writes": 150,
               "forbid_protection_lowering": True},
}

_STR_LIST = {"type": "array", "items": {"type": "string"}}
SCHEMA = {
    "type": "object",
    "properties": {
        "org": {"type": ["string", "null"]}, "paused": {"type": "boolean"}, "maintainers": _STR_LIST,
        "approval_sources": {"type": "array", "items": {"enum": ["github_label", "divan_bus", "dashboard"]}},
        "notifiers": {"type": "array", "items": {"type": "object", "required": ["type"],
                      "properties": {"type": {"enum": ["stdout", "file", "divan_bus", "github_issue", "webhook"]}}}},
        "bus": {"type": "object", "properties": {"url": {"type": "string"}, "seat": {"type": "string"},
                "notify": {"type": ["string", "null"]}, "maintainer_seats": _STR_LIST}},
        "llm": {"type": "object", "properties": {
            "backend": {"enum": ["claude_cli", "anthropic_api", "none"]},
            "max_calls_per_run": {"type": "integer", "minimum": 0}}},
        "budget": {"type": "object", "properties": {"graphql_points_per_run": {"type": "integer", "minimum": 1}}},
        "policy": {"type": "object", "properties": {
            "branch_protection": {"type": "object", "properties": {
                "require_pr": {"type": "boolean"}, "required_approvals": {"type": "integer", "minimum": 0},
                "allow_force_pushes": {"type": "boolean"}, "allow_deletions": {"type": "boolean"},
                "enforce_admins": {"type": "boolean"}}}}},
        "auto_apply": {"type": "array", "items": {"enum": ["branch_protection", "dependabot_alerts", "secret_scanning",
                                                           "health_fix"]}},
        "policy_actions": {"type": "object", "additionalProperties": {"enum": ["log", "issue", "fix"]},
                           "propertyNames": {"enum": ["branch_protection", "dependabot_alerts", "secret_scanning", "required_files"]}},
        "policy_strategy": {"enum": ["opt-in", "opt-out"]},
        "policy_repos": _STR_LIST,
        "coupling": {"type": "object"},
        "lifecycle": {"type": "object", "properties": {"dormant_days": {"type": "integer", "minimum": 1},
                                                       "frozen": _STR_LIST, "frozen_topic": {"type": "string"}}},
        "boundaries": {"type": "object", "properties": {
            "rules": {"type": "array", "items": {"type": "object", "required": ["from", "allow"], "properties": {
                "from": {"type": "string"}, "allow": _STR_LIST}}},
            "scope_prefix": {"type": "string"}, "block_cross_scope_merge": {"type": "boolean"}}},
        "writes": {"type": "object", "properties": {"caps": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}}}},
        "guards": {"type": "object", "properties": {
            "enabled": {"type": "boolean"}, "max_repo_fraction": {"type": "number", "minimum": 0, "maximum": 1},
            "min_repos": {"type": "integer", "minimum": 1}, "max_archives": {"type": "integer", "minimum": 0},
            "max_writes": {"type": "integer", "minimum": 1}, "forbid_protection_lowering": {"type": "boolean"}}},
        "thresholds": {"type": "object", "additionalProperties": {"type": "number"}},
        "approval_ttl_days": {"type": "integer", "minimum": 1},
        "quorum": {"type": "integer", "minimum": 1},
        "archive_requires_second_approval": {"type": "boolean"},
        "exclude": _STR_LIST,
        "git_identity": {"type": "object", "required": ["name", "email"]},
    },
}

REPO_OVERRIDE_SCHEMA = {
    "type": "object",
    "properties": {"maintainers": _STR_LIST, "ignore": {"type": "boolean"}, "policy": {"type": "object"},
                   "exclude_paths": _STR_LIST, "enabled": {"type": "boolean"},
                   "policy_actions": {"type": "object", "additionalProperties": {"enum": ["log", "issue", "fix"]}}},
    "additionalProperties": False,
}


class ConfigError(ValueError):
    pass


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def find_config_file() -> Path | None:
    env = os.environ.get("ION_CONFIG") or os.environ.get("MIMAR_CONFIG")
    if env:
        return Path(env).expanduser()
    unset = "ION_HOME" not in os.environ          # then ~/.ion/ion.yml counts even while state is still in ~/.mimar
    for c in (Path.cwd() / "ion.yml", home() / "ion.yml", *([Path("~/.ion/ion.yml").expanduser()] if unset else [])):
        if c.exists():
            return c
    # Deprecation shim: this bot was called mimar until 2026-10-13.
    legacy = Path("~/.mimar").expanduser()
    for c in (Path.cwd() / "mimar.yml", legacy / "mimar.yml"):
        if c.exists() and "ION_HOME" not in os.environ:
            # move the state too: config alone would leave state.db and the PAUSED kill switch behind in ~/.mimar
            steps = ["mkdir -p ~/.ion"] + (["mv ~/.mimar/* ~/.ion/"] if legacy.is_dir() else [])
            steps.append(f"mv {'~/.ion/mimar.yml' if c.parent == legacy else c} ~/.ion/ion.yml")
            print(f"ion: reading legacy {c}; migrate: {' && '.join(steps)} (the name 'mimar' is deprecated)", file=sys.stderr)
            return c
    return None


class Config(dict):
    """A dict with helpers. Immutable by convention."""

    @property
    def org(self) -> str:
        if not self["org"]:
            raise ConfigError("config has no `org`")
        return self["org"]

    def is_paused(self) -> bool:
        return bool(self.get("paused")) or (home() / "PAUSED").exists()

    def maintainers_for(self, repo_override: dict | None) -> list[str]:
        """The org maintainers, narrowed by a repo's own list. Anyone who can push to a repo can edit its
        `.github/ion.yml`, so that list can only remove org maintainers, never add a login: names not in the org list
        are dropped, and a list that names none of them leaves the repo with no approvers (fails closed)."""
        org = list(self["maintainers"])
        mine = (repo_override or {}).get("maintainers")
        if not mine:
            return org
        mine = {m.lower() for m in mine}
        return [m for m in org if m.lower() in mine]

    def threshold(self, name: str) -> float:
        return self["thresholds"][name]


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> Config:
    p = Path(path).expanduser() if path else find_config_file()
    if path and not p.exists():
        raise ConfigError(f"config file not found: {p}")
    data = {}
    if p and p.exists():
        data = yaml.safe_load(p.read_text()) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{p}: top level must be a mapping")
    merged = deep_merge(DEFAULTS, data)
    merged = deep_merge(merged, overrides or {})
    try:
        jsonschema.validate(merged, SCHEMA)
    except jsonschema.ValidationError as e:
        raise ConfigError(f"invalid config: {e.message} at {'/'.join(map(str, e.absolute_path))}") from e
    cfg = Config(merged)
    cfg["_path"] = str(p) if p else None
    return cfg


def parse_repo_override(text: str | None) -> tuple[dict, str | None]:
    """Return (override, error). Invalid overrides are ignored and reported."""
    if not text:
        return {}, None
    try:
        data = yaml.safe_load(text) or {}
        jsonschema.validate(data, REPO_OVERRIDE_SCHEMA)
        return data, None
    except (yaml.YAMLError, jsonschema.ValidationError) as e:
        return {}, f"invalid .github/ion.yml: {str(e).splitlines()[0]}"
