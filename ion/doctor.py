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
    if getattr(ctx.gh, "is_app", False):
        try:
            actor = ctx.gh.token_login()
            ctx.gh.rest("GET", "/installation/repositories?per_page=1")
            out.append(_ok("GitHub App auth", True, actor + "; API only, git transport unsupported"))
        except Exception:
            out.append(_ok("GitHub App auth", False, "App credentials or installation could not be verified"))
    else:
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
    out.append(_ok("git-filter-repo", bool(fr), fr or "not found (needed for split and move_code)"))
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
    return out
