"""Merge repo A into repo B with full history (git subtree add, no squash)."""
from __future__ import annotations

import json

from .. import proposals
from ..collect.mirror import mirror_path as _mp
from ..util import Refused, SafetyError, safe_write, readme_path, readme_text
from .. import changesets
from ..writes import WriteRequest
from .base import Executor, Plan, Step, Waiting
from .provenance import verify_merged_tree


class MergeExecutor(Executor):
    kind = "merge"

    def _paths(self, p):
        w = self.ctx.work_dir(p["id"])
        return w / "target", w / "source-pointer"

    def plan(self, p: dict) -> Plan:
        s = p["subject"]
        phase = p["state"].get("phase", "open_pr")
        return {"open_pr": self._open_pr, "await_merge": self._await, "pointer": self._pointer,
                "await_pointer": self._await_pointer, "archive": self._archive}.get(phase, lambda *_: Plan(p["id"], "merge", "done"))(p, s)

    # phase 1 ------------------------------------------------------------------------
    def _open_pr(self, p, s) -> Plan:
        pid, A, B = p["id"], s["source"], s["target"]
        tdir, _ = self._paths(p)
        branch = f"ion/{pid}"
        mirror = self.ctx.mirror_path(A)
        tgt_default = s.get("target_branch") or self.default_branch(B)
        src_branch = s.get("source_branch") or self.default_branch(A)
        if branch == tgt_default:
            raise SafetyError("work branch equals default branch")
        a_url, b_url = self.ctx.remote_url(A), self.ctx.remote_url(B)

        def refresh_mirror():
            if mirror.exists():
                self.g([*self.auth(a_url), "-C", str(mirror), "fetch", "--prune", "--quiet", "origin"])
            else:
                self.g([*self.auth(a_url), "clone", "--mirror", "--quiet", a_url, str(mirror)])
            return str(mirror)

        def subtree():
            tip = self.g(["--git-dir", str(mirror), "rev-parse", src_branch]).stdout.strip()
            self.g([*self.identity_args(), "subtree", "add", f"--prefix={s['prefix']}", str(mirror), src_branch,
                    "-m", f"Merge {A} into {B} under {s['prefix']}/ (history preserved) [{pid}]\n\n"
                          f"Ion-Proposal: {pid}\nIon-Origin: {self.cfg.org}/{A}@{tip}"], cwd=tdir)

        def check_history():
            r = verify_merged_tree(tdir, s["prefix"], mirror, src_branch)
            return json.dumps(r, sort_keys=True)

        def push():
            self.safe_push(["origin", f"{branch}:refs/heads/{branch}"], tdir, tgt_default, repo=B)

        def pr():
            n = self.open_pr(B, branch, tgt_default, f"Merge {A} into {B} [{pid}]",
                             f"Brings {A} into `{s['prefix']}/` with its full commit history (subtree merge, not squashed).\n\n"
                             f"Tracked in {self.cfg.org}/{self.cfg['tracker_repo']} as {pid}.\n\nA maintainer merges this PR.")
            self.db.set_state(pid, pr_number=n)
            return str(n)

        return Plan(pid, "merge", "open_pr", [
            Step("refresh-source-mirror", f"fetch the {A} mirror", ["git", "-C", str(mirror), "fetch", "--prune", "origin"], refresh_mirror),
            Step("clone-target", f"clone {B}", ["git", "clone", b_url, str(tdir)], lambda: self.clone(b_url, tdir)),
            Step("branch", f"create work branch {branch}", ["git", "checkout", "-b", branch],
                 lambda: self.g(["checkout", "-q", "-B", branch, f"origin/{tgt_default}"], cwd=tdir) and None),
            Step("subtree-add", f"import {A} history under {s['prefix']}/ (no squash)",
                 ["git", "subtree", "add", f"--prefix={s['prefix']}", str(mirror), src_branch], subtree),
            Step("verify-history", f"check {B}'s {s['prefix']}/ tree equals {A}'s tip tree and the tip is an ancestor of the result",
                 None, check_history),
            Step("push-branch", f"push {branch} (never the default branch, never forced)", ["git", "push", "origin", f"{branch}:refs/heads/{branch}"], push),
            Step("open-pr", f"open a PR in {B} and label it `needs decision`", None, pr),
        ])

    def after_phase(self, p, plan):
        pid = p["id"]
        nxt = {"open_pr": "await_merge", "await_merge": "pointer", "pointer": "await_pointer",
               "await_pointer": "archive", "archive": "done"}.get(plan.phase)
        if nxt:
            self.set_phase(pid, nxt)
        if nxt == "done":
            proposals.set_status(self.db, pid, "done")

    # phase 2 ------------------------------------------------------------------------
    def _await(self, p, s) -> Plan:
        def chk():
            n = p["state"].get("pr_number")
            if not n or not self.pr_merged(s["target"], n):
                raise Waiting(f"PR #{n} in {s['target']} not merged yet")
            return "merged"
        return Plan(p["id"], "merge", "await_merge", [Step("pr-merged", "wait for the merge PR to be merged by a maintainer", None, chk, mutates=False)])

    def _pointer(self, p, s) -> Plan:
        pid, A = p["id"], s["source"]
        _, pdir = self._paths(p)
        url = self.ctx.remote_url(A)
        base = s.get("source_branch") or self.default_branch(A)
        branch = f"ion/{pid}-pointer"

        def edit():
            self.g(["checkout", "-q", "-B", branch, f"origin/{base}"], cwd=pdir)
            readme = readme_path(pdir)
            safe_write(pdir, readme, readme_text(readme,
                f"# {A}\n\nThis repository has moved. Its code and history now live in "
                f"[{self.cfg.org}/{s['target']}](https://github.com/{self.cfg.org}/{s['target']}) under `{s['prefix']}/`.\n"))
            self.g(["add", readme], cwd=pdir)
            self.commit(pdir, f"Point README at {s['target']} ({pid})")

        def pr():
            dep = changesets.depends_on_line(self.ctx, self.fresh(pid), "pointer")
            n = self.open_pr(A, branch, base, f"Point README at {s['target']} [{pid}]",
                             f"Replaces the README with a pointer to {s['target']}. The repository will be archived after this merges."
                             + (f"\n\n{dep}" if dep else ""))
            self.db.set_state(pid, pointer_pr=n)
            return str(n)

        return Plan(pid, "merge", "pointer", [
            Step("ordering", "the import PR must have landed before the pointer PR is opened", None,
                 lambda: changesets.require(self.ctx, self.fresh(pid), "pointer"), mutates=False),
            Step("clone-source", f"clone {A}", ["git", "clone", url, str(pdir)], lambda: self.clone(url, pdir)),
            Step("edit-readme", "replace README with a pointer", None, edit),
            Step("push", f"push {branch}", ["git", "push", "origin", f"{branch}:refs/heads/{branch}"],
                 lambda: self.safe_push(["origin", f"{branch}:refs/heads/{branch}"], pdir, base, repo=A)),
            Step("open-pr", f"open the pointer PR in {A} and label it `needs decision`", None, pr)])

    def _await_pointer(self, p, s) -> Plan:
        def chk():
            n = p["state"].get("pointer_pr")
            if not n or not self.pr_merged(s["source"], n):
                raise Waiting(f"pointer PR #{n} in {s['source']} not merged yet")
            return "merged"
        return Plan(p["id"], "merge", "await_pointer", [Step("pointer-merged", "wait for the pointer PR to merge", None, chk, mutates=False)])

    def _archive(self, p, s) -> Plan:
        A = s["source"]

        def go():
            if self.cfg["archive_requires_second_approval"]:
                from ..propose.engine import create_followup
                np_ = create_followup(self.ctx, p, "archive", {"repo": A, "reason": f"merged into {s['target']} ({p['id']})"})
                return f"created {np_['id']}, awaiting its own approval"
            self.ctx.writes.submit(WriteRequest("archive_repo", A))
            return "archived"
        return Plan(p["id"], "merge", "archive", [Step(
            "ordering", "the pointer PR must have landed before the source is archived", None,
            lambda: changesets.require(self.ctx, self.fresh(p["id"]), "archive"), mutates=False), Step(
            "archive-source", f"archive {A}" + (" via a separate approved proposal" if self.cfg["archive_requires_second_approval"] else ""),
            None, go)])

    def verify(self, p: dict) -> bool:
        s = p["subject"]
        return bool(p["state"].get("pr_number")) and self.db.ledger_done(p["id"], "open_pr.verify-history")

    def rollback_notes(self, p: dict) -> list[str]:
        s = p["subject"]
        return [f"Close the PR in {s['target']} (branch ion/{p['id']}); nothing else changed.",
                f"{s['source']} is only modified by the pointer PR; revert it and unarchive if archived."]
