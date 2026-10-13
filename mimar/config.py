"""Config loading: defaults + mimar.yml + per-repo overrides, validated with jsonschema."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import jsonschema
import yaml

from .util import home

DEFAULTS: dict = {
    "org": None,
    "paused": False,
    "maintainers": [],
    "plan": "free",
    "tracker_repo": "mimar-tracker",  # keep this repo PRIVATE: proposal bodies name private repos
    "approval_sources": ["github_label"],
    "bus": {"url": "http://127.0.0.1:8787", "seat": "mimar", "notify": None, "maintainer_seats": [],
            "client": None, "python": None},
    "notifiers": [{"type": "stdout"}],
    "llm": {"backend": "none", "model_default": "haiku", "model_structural": "sonnet", "max_calls_per_run": 20,
            "timeout_s": 180, "api_models": {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-4-5"}},
    "budget": {"graphql_points_per_run": 500},
    "collect": {"commits": 60, "page_size": 25},
    "policy": {
        "default_branch": "main",
        "branch_protection": {"require_pr": True, "required_approvals": 0, "allow_force_pushes": False,
                              "allow_deletions": False, "enforce_admins": False},
        "dependabot_alerts": True,
        "secret_scanning": "public_only",
        "required_files": ["README.md", "LICENSE"],
    },
    "auto_apply": [],
    "thresholds": {"merge_coupling": 0.6, "split_cohesion_gap": 0.5, "stale_days": 180, "persist_runs": 2,
                   "cooldown_days": 60, "rescore_delta": 0.15, "archive_score": 0.7, "stale_pr_days": 30,
                   "vuln_alerts": 5, "min_cluster_files": 8, "min_cluster_share": 0.15, "dup_min_bytes": 200},
    "approval_ttl_days": 14,
    "quorum": 1,
    "quorum_by_kind": {},
    "archive_requires_second_approval": True,
    "exclude": [],
    "git_identity": {"name": "mimar", "email": "mimar@askaconsult.com"},
    "ai_attribution": False,
    "license_template": None,
    "report": {"org_health_issue": False},
}

_STR_LIST = {"type": "array", "items": {"type": "string"}}
SCHEMA = {
    "type": "object",
    "properties": {
        "org": {"type": ["string", "null"]}, "paused": {"type": "boolean"}, "maintainers": _STR_LIST,
        "approval_sources": {"type": "array", "items": {"enum": ["github_label", "divan_bus"]}},
        "notifiers": {"type": "array", "items": {"type": "object", "required": ["type"],
                      "properties": {"type": {"enum": ["stdout", "file", "divan_bus", "github_issue", "webhook"]}}}},
        "bus": {"type": "object", "properties": {"url": {"type": "string"}, "seat": {"type": "string"},
                "notify": {"type": ["string", "null"]}, "maintainer_seats": _STR_LIST}},
        "llm": {"type": "object", "properties": {
            "backend": {"enum": ["claude_cli", "anthropic_api", "none"]},
            "max_calls_per_run": {"type": "integer", "minimum": 0}}},
        "budget": {"type": "object", "properties": {"graphql_points_per_run": {"type": "integer", "minimum": 1}}},
        "policy": {"type": "object"},
        "auto_apply": {"type": "array", "items": {"enum": ["branch_protection", "dependabot_alerts", "secret_scanning",
                                                           "health_fix"]}},
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
                   "exclude_paths": _STR_LIST},
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
    env = os.environ.get("MIMAR_CONFIG")
    if env:
        return Path(env).expanduser()
    for c in (Path.cwd() / "mimar.yml", home() / "mimar.yml"):
        if c.exists():
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
        if repo_override and repo_override.get("maintainers"):
            return list(repo_override["maintainers"])
        return list(self["maintainers"])

    def threshold(self, name: str) -> float:
        return self["thresholds"][name]


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> Config:
    p = Path(path).expanduser() if path else find_config_file()
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
        return {}, f"invalid .github/mimar.yml: {str(e).splitlines()[0]}"
