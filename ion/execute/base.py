"""Executor base: plan/apply/verify/rollback_notes, the approval gate, ledgered steps."""
from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from typing import Callable

from ..approve.core import evaluate
from ..tracker import NEEDS_DECISION
from ..writes import WriteRequest
from ..util import Refused, SafetyError, Waiting, guard_git, git, git_env, run  # noqa: F401

log = logging.getLogger("ion")


@dataclass
class Step:
    name: str
    desc: str
    cmd: list[str] | None = None            # exact command shown to maintainers
    fn: Callable[[], str | None] | None = None
    mutates: bool = True


@dataclass
class Plan:
    proposal: str
    kind: str
    phase: str
    steps: list[Step] = field(default_factory=list)
    subject: dict = field(default_factory=dict)

    def render(self) -> list[str]:
        lines = []
        for s in self.steps:
            lines.append(f"[{s.name}] {s.desc}")
            if s.cmd:
                lines.append("    $ " + " ".join(shlex.quote(c) for c in s.cmd))
        return lines

    def commands(self) -> list[str]:
        return [" ".join(shlex.quote(c) for c in s.cmd) for s in self.steps if s.cmd]

    def fingerprint(self) -> str:
        """The plan hash (sha256, hex): what a maintainer's approval is bound to. It covers the kind, the subject and
        every step with its exact command, so any change of what would run changes the hash."""
        import hashlib
        doc = {"kind": self.kind, "subject": self.subject, "steps": [(s.name, s.desc, s.cmd) for s in self.steps]}
        return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()


def check_gate(ctx, p: dict) -> str:
    """Raise Refused unless the proposal may be applied. Returns the basis ('approval' or 'allowlisted policy_fix')."""
    cfg = ctx.cfg
    if ctx.cfg.is_paused():
        raise Refused("kill switch is on (PAUSED file or paused: true)")
    if p["status"] in ("rejected", "dropped", "done"):
        raise Refused(f"proposal is {p['status']}")
    if p["kind"] in ("policy_fix", "health_fix"):
        from ..analyze import policy
        repo = p["subject"].get("repo")
        ov = repo_override(ctx, repo)
        cls = p["subject"].get("class") if p["kind"] == "policy_fix" else "health_fix"
        if policy.governed(cfg, ov, repo) and policy.action_for(cfg, ov, cls) == "fix":
            return f"allowlisted {p['kind']}"
    started = bool(p["state"].get("started"))
    ev = evaluate(ctx, p, ignore_ttl=started)
    if ev["state"] != "approved":
        raise Refused(f"no verified approval ({ev['state']}: {ev['reason']})")
    return "approval"


def repo_override(ctx, repo: str | None) -> dict:
    from ..collect.snapshot import latest_snapshots
    from ..config import parse_repo_override
    if not repo:
        return {}
    return parse_repo_override((latest_snapshots(ctx.db)[0].get(repo) or {}).get("override_text"))[0]


class Executor:
    kind = ""

    def __init__(self, ctx):
        self.ctx = ctx
        self.cfg, self.db, self.gh = ctx.cfg, ctx.db, ctx.gh

    # interface ------------------------------------------------------------------------
    def plan(self, p: dict) -> Plan:
        raise NotImplementedError

    def verify(self, p: dict) -> bool:
        return True

    def rollback_notes(self, p: dict) -> list[str]:
        return []

    def after_phase(self, p: dict, plan: Plan) -> None:
        """Hook run after all steps of a phase succeeded (advances the phase machine)."""

    # execution ------------------------------------------------------------------------
    def apply(self, p: dict, plan: Plan | None = None, dry_run: bool = True) -> dict:
        if self.kind in self.cfg.get("disabled_executors", []):
            raise Refused(f"executor {self.kind} is disabled by configuration")
        basis = check_gate(self.ctx, p)          # enforced for dry runs too: no plan output without standing
        plan = plan or self.plan(p)
        if dry_run:
            return {"dry_run": True, "basis": basis, "phase": plan.phase, "plan": plan.render(), "done": [], "waiting": None}
        pid = p["id"]
        if not p["state"].get("started"):
            self.db.set_state(pid, started=True)
            self.db.x("update proposals set status='executing' where id=?", (pid,))
        done, waiting = [], None
        for st in plan.steps:
            key = f"{plan.phase}.{st.name}"
            if self.db.ledger_done(pid, key):
                continue
            self.db.ledger_add(pid, key, "before", st.desc)
            try:
                detail = st.fn() if st.fn else ""
            except Waiting as w:
                self.db.ledger_add(pid, key, "waiting", str(w))
                waiting = str(w)
                break
            except Exception as e:  # noqa: BLE001
                self.db.ledger_add(pid, key, "error", str(e))
                self.db.x("update proposals set status='failed' where id=?", (pid,))
                raise
            self.db.ledger_add(pid, key, "after", detail or "")
            done.append(key)
        else:
            self.after_phase(self.fresh(pid), plan)
        return {"dry_run": False, "basis": basis, "phase": plan.phase, "done": done, "waiting": waiting}

    def fresh(self, pid: str) -> dict:
        from .. import proposals
        return proposals.get(self.db, pid)

    # helpers --------------------------------------------------------------------------
    def identity_args(self) -> list[str]:
        i = self.cfg["git_identity"]
        return ["-c", f"user.name={i['name']}", "-c", f"user.email={i['email']}"]

    def g(self, argv: list[str], cwd=None, **kw):
        """Guarded git (refuses force pushes, branch deletion)."""
        return git([*argv], cwd=cwd, **kw)

    def auth(self, url: str) -> list[str]:
        from ..collect.mirror import CRED_HELPER
        return ["-c", "credential.helper=", "-c", f"credential.helper={CRED_HELPER}"] if url.startswith("https://") else []

    def clone(self, url: str, dest, mirror_of: bool = False) -> None:
        from pathlib import Path
        if (Path(dest) / ".git").exists():
            return
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        self.g([*self.auth(url), "clone", "--quiet", "--no-local", url, str(dest)])

    def verify_filtered(self, p: dict, source: str, base: str, dirs: list[str], filtered_dir) -> dict:
        """Exact check of a filter-repo result: tree equality plus commit-map coverage (see provenance.py)."""
        from .provenance import verify_filtered
        src = self.ctx.work_dir(p["id"]) / "verify-source"
        self.clone(self.ctx.remote_url(source), src)
        return verify_filtered(filtered_dir, src, f"origin/{base}", dirs)

    def commit(self, cwd, msg: str) -> None:
        """Commit with the configured identity. No AI trailers unless the config asks for them."""
        if self.cfg.get("ai_attribution"):
            msg += "\n\nCo-Authored-By: AI assistant <noreply@example.com>"
        self.g([*self.identity_args(), "commit", "-q", "-m", msg], cwd=cwd)

    def default_branch(self, repo: str) -> str:
        from ..collect.snapshot import latest_snapshots
        snaps, _ = latest_snapshots(self.db)
        return (snaps.get(repo) or {}).get("default_branch") or self.cfg["policy"]["default_branch"]

    def set_phase(self, pid: str, phase: str, **extra) -> None:
        self.db.set_state(pid, phase=phase, **extra)

    def ledger_json(self, pid: str, key: str) -> dict:
        d = self.db.ledger_detail(pid, key)
        try:
            return json.loads(d) if d else {}
        except ValueError:
            return {}

    def pr_merged(self, repo: str, number: int) -> bool:
        pr = self.gh.rest("GET", f"/repos/{self.cfg.org}/{repo}/pulls/{number}")
        return bool(pr.get("merged"))

    def open_pr(self, repo: str, head: str, base: str, title: str, body: str) -> int:
        m = re.search(r"\[(P-\d+)\]\s*$", title)
        proposal = m.group(1) if m else None
        pr = self.ctx.writes.submit(WriteRequest(
            "open_pr", repo, {"title": title, "head": head, "base": base, "body": body}, proposal))
        number = int(pr["number"])
        self._label_pr_needs_decision(repo, number, proposal)
        return number

    def _label_pr_needs_decision(self, repo: str, number: int, proposal: str | None) -> None:
        """Put an ion PR that now waits for a maintainer merge into the decision feed.

        This is feed metadata, never a reason to fail a step whose PR already exists.
        The plan step names the label as well, so the write is not hidden from the approver.
        """
        try:
            self.ctx.writes.submit(WriteRequest(
                "create_label", repo, {"name": NEEDS_DECISION, "color": "D93F0B"}, proposal))
        except Exception:  # noqa: BLE001  (an existing label 422s; add_label is the real check)
            pass
        try:
            self.ctx.writes.submit(WriteRequest(
                "add_label", repo, {"number": number, "labels": [NEEDS_DECISION]}, proposal))
        except Exception as e:  # noqa: BLE001
            log.warning("could not label PR %s#%s %r: %s", repo, number, NEEDS_DECISION, str(e)[:160])

    def safe_push(self, argv_tail: list[str], cwd, default_branch: str | None, allow_default: bool = False,
                  repo: str = "") -> None:
        """Push without force, as a typed `push_branch` request. Feature branches only, unless the destination is a
        brand new empty repo."""
        full = ["push", *argv_tail]
        refs = [a for a in argv_tail if not a.startswith("-")]
        dests = [r.split(":")[-1].replace("refs/heads/", "") for r in refs[1:]]
        for dest in dests:
            if default_branch and dest == default_branch and not allow_default:
                raise SafetyError(f"refusing to push to default branch {default_branch}")
            if not dest.startswith("ion/") and not allow_default:
                raise SafetyError(f"refusing to push non-ion branch {dest}")
        req = WriteRequest("push_branch", repo or "unknown", {"branch": dests[-1] if dests else "",
                                                               "base": default_branch or "main",
                                                               **({"initial": True} if allow_default else {})})
        self.ctx.writes.submit(req, lambda: self.g(full, cwd=cwd))


def filter_args(dirs: list[str]) -> list[str]:
    if len(dirs) == 1:
        return ["--subdirectory-filter", dirs[0]]
    args = []
    for d in dirs:
        args += ["--path", d + "/"]
    return args
