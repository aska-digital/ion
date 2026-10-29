"""Environment checks."""
from __future__ import annotations

import os
import subprocess
import sys

from .util import filter_repo_path, home, which


def _ok(name, ok, detail=""):
    return {"name": name, "ok": ok, "detail": detail}


def doctor(ctx, cfg_error: str | None = None) -> list[dict]:
    cfg = ctx.cfg
    out = []
    out.append(_ok("config", cfg_error is None and bool(cfg.get("org")), cfg_error or f"{cfg.get('_path')} (org={cfg.get('org')})"))
    out.append(_ok("python", sys.version_info >= (3, 12), sys.version.split()[0]))
    gh = which("gh")
    if gh:
        p = subprocess.run([gh, "auth", "status"], capture_output=True, text=True)
        line = next((l.strip() for l in (p.stdout + p.stderr).splitlines() if "Logged in" in l), "not logged in")
        out.append(_ok("gh auth", p.returncode == 0, line))
    else:
        out.append(_ok("gh auth", False, "gh not found"))
    g = which("git")
    ver = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip() if g else "missing"
    sub = g and subprocess.run(["git", "subtree"], capture_output=True, text=True).returncode in (0, 129)
    out.append(_ok("git", bool(g) and bool(sub), f"{ver}; subtree {'ok' if sub else 'missing'}"))
    fr = filter_repo_path()
    filter_needed = bool({"split", "move_code"} - set(cfg.get("disabled_executors", [])))
    out.append(_ok("git-filter-repo", bool(fr) or not filter_needed,
                   fr or ("not found (needed for split and move_code)" if filter_needed else "optional; split and move_code disabled")))
    be = cfg["llm"]["backend"]
    if be == "claude_cli":
        out.append(_ok("llm backend", bool(which("claude")), "claude CLI " + ("found" if which("claude") else "missing")
                       + (" (ambient ANTHROPIC_API_KEY not passed to it)" if os.environ.get("ANTHROPIC_API_KEY") else "")))
    elif be == "anthropic_api":
        out.append(_ok("llm backend", bool(os.environ.get("ANTHROPIC_API_KEY")), "ANTHROPIC_API_KEY " + ("set" if os.environ.get("ANTHROPIC_API_KEY") else "missing")))
    else:
        out.append(_ok("llm backend", True, "none (templates only)"))
    uses_bus = "divan_bus" in cfg["approval_sources"] or any(n["type"] == "divan_bus" for n in cfg["notifiers"])
    if uses_bus:
        client = cfg["bus"].get("client")
        if not client or not os.path.exists(os.path.expanduser(client)):
            out.append(_ok("bus", False, f"client not found: {client}"))
        else:
            py = os.path.expanduser(cfg["bus"].get("python") or sys.executable)
            keyfile = os.path.expanduser(cfg["bus"].get("key") or f"~/.divan/{cfg['bus']['seat']}.key")
            out.append(_ok("bus seat key", os.path.exists(keyfile),
                           f"{keyfile} " + ("present" if os.path.exists(keyfile) else
                                            f"MISSING: seat '{cfg['bus']['seat']}' cannot sign, so it cannot read its inbox or post "
                                            "(not created by ion; register the seat on the bus first)")))
            try:
                rc, o, e = ctx.bus_runner([py, os.path.expanduser(client), "head"],
                                          {"DIVAN_URL": cfg["bus"]["url"], "DIVAN_SEAT": cfg["bus"]["seat"]}, None)
                out.append(_ok("bus", rc == 0, "relay reachable" if rc == 0 else ((e.strip().splitlines() or ["unreachable"])[-1][:120])))
            except Exception as ex:  # noqa: BLE001
                out.append(_ok("bus", False, str(ex)[:120]))
    else:
        out.append(_ok("bus", True, "not used"))
    out.append(_ok("state dir", os.access(home(), os.W_OK), str(home())))
    out.append(_ok("kill switch", True, "PAUSED" if cfg.is_paused() else "off (running allowed)"))
    out.extend(invariants(ctx))
    return out


def _networkx_status():
    try:
        import networkx
        from networkx.algorithms.community import louvain_communities
        return callable(louvain_communities), getattr(networkx, "__version__", "unknown")
    except Exception:
        return False, "networkx Louvain implementation unavailable"


def _filter_version():
    path = filter_repo_path()
    if not path:
        return False, None
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15)
        version = result.stdout.strip()
        return result.returncode == 0 and bool(version), version or None
    except Exception:
        return False, None


def _live_override(ctx, name):
    import base64
    from .config import parse_repo_override
    from .ghclient import GHError
    for file in ("ion.yml", "mimar.yml"):
        try:
            data = ctx.gh.rest("GET", f"/repos/{ctx.cfg.org}/{name}/contents/.github/{file}")
        except GHError as e:
            if e.status == 404:
                continue
            raise
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            raise ValueError("repository policy file unreadable")
        text = base64.b64decode(data["content"]).decode("utf-8")
        override, error = parse_repo_override(text)
        if error:
            raise ValueError("repository policy file invalid")
        return override
    return {}


def invariants(ctx):
    """Read-only live checks required before the write-capable scheduled pipeline."""
    from .analyze.policy import action_for, governed, org_action
    cfg = ctx.cfg
    checks = []
    def add(name, ok, detail, critical=True):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "critical": critical})
    try:
        tracker = ctx.gh.rest("GET", f"/repos/{cfg.org}/{cfg['tracker_repo']}")
        private = isinstance(tracker, dict) and tracker.get("private") is True
        add("private tracker", private, "verified private" if private else "tracker is public or privacy is unreadable")
    except Exception:
        add("private tracker", False, "tracker privacy could not be verified")
    classes = ("branch_protection", "dependabot_alerts", "secret_scanning")
    if "policy_fix" not in cfg.get("disabled_executors", []) and any(org_action(cfg, cls) == "fix" for cls in classes):
        try:
            repos = ctx.gh.paginate(f"/orgs/{cfg.org}/repos?type=all&per_page=100")
            if not isinstance(repos, list):
                raise ValueError("repository list unreadable")
            # Listing alone can silently omit private repos with a narrow token. Require
            # complete inventory visibility before concluding that every target was checked.
            org = ctx.gh.rest("GET", f"/orgs/{cfg.org}")
            expected = org.get("total_private_repos")
            public = org.get("public_repos")
            if not isinstance(expected, int) or not isinstance(public, int) or len(repos) != expected + public:
                raise ValueError("complete organisation inventory could not be verified")
            names = [r.get("name") for r in repos]
            if len(set(names)) != len(names) or not all(names):
                raise ValueError("repository inventory contains missing or duplicate names")
            bad = []
            for item in repos:
                name = item.get("name")
                if not name:
                    raise ValueError("repository name missing")
                if name in cfg["exclude"]:
                    continue
                live = ctx.gh.rest("GET", f"/repos/{cfg.org}/{name}")
                if live.get("archived") or live.get("empty") or live.get("isEmpty"):
                    continue
                override = _live_override(ctx, name)
                if not governed(cfg, override, name) or not any(action_for(cfg, override, cls) == "fix" for cls in classes):
                    continue
                if (live.get("permissions") or {}).get("admin") is not True:
                    bad.append(name)
            add("policy admin rights", not bad, "live admin verified for fix-level governed repositories" if not bad else
                "admin rights missing or unreadable: " + ", ".join(sorted(bad)))
        except Exception:
            add("policy admin rights", False, "complete live inventory, governance, or admin rights could not be verified")
    else:
        add("policy admin rights", True, "no fix-level repository settings enabled", False)
    expected_graph = cfg.get("preflight", {}).get("expect_networkx", False)
    nx_ok, nx_detail = _networkx_status()
    add("Louvain clustering", nx_ok or not expected_graph,
        nx_detail + (" (required)" if expected_graph else " (optional; fallback allowed)"), expected_graph)
    disabled = set(cfg.get("disabled_executors", []))
    needs_filter = bool({"split", "move_code"} - disabled)
    version_ok, version = _filter_version()
    pin = cfg.get("preflight", {}).get("filter_repo_version")
    matched = version_ok and (not pin or version == pin)
    detail = f"observed {version or 'unavailable'}" + (f"; required {pin}" if pin else "; unpinned, no compatibility guarantee")
    add("filter-repo version", matched or not needs_filter, detail, needs_filter)
    return checks
