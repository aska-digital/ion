"""Split directories out of repo A into a new repo N with history (git filter-repo)."""
from __future__ import annotations

import json

from .. import proposals
from ..util import Refused, SafetyError, filter_repo_path, safe_write
from ..writes import WriteRequest
from .base import Executor, Plan, Step, filter_args
from .provenance import origin_callback
from .simple import apply_policy_fix


class SplitExecutor(Executor):
    kind = "split"

    def plan(self, p: dict) -> Plan:
        phase = p["state"].get("phase", "create")
        s = p["subject"]
        return {"create": self._create, "pointer": self._pointer}.get(phase, lambda *_: Plan(p["id"], "split", "done"))(p, s)

    def _create(self, p, s) -> Plan:
        pid, A, N, dirs = p["id"], s["source"], s["new_repo"], s["dirs"]
        w = self.ctx.work_dir(pid)
        cdir = w / "filtered"
        a_url, n_url = self.ctx.remote_url(A), self.ctx.remote_url(N)
        base = s.get("source_branch") or self.default_branch(A)
        private = s.get("private", True)
        fargs = filter_args(dirs)

        def filt():
            fr = filter_repo_path()
            if not fr:
                raise RuntimeError("git-filter-repo not found")
            # --force is filter-repo's own flag, local clone only; the callback stamps Ion-Origin on every commit
            self.g(["filter-repo", *fargs, "--commit-callback", origin_callback(self.cfg.org, A), "--force"], cwd=cdir)

        def has_refs() -> bool:
            return bool(self.g([*self.auth(n_url), "ls-remote", n_url], check=False).stdout.strip())

        def create():
            try:
                existing = self.gh.rest("GET", f"/repos/{self.cfg.org}/{N}")
            except Exception:  # noqa: BLE001
                existing = None
            if existing is not None:
                # The filtered history goes into this repo next, so its visibility must be the one we would have
                # created it with: a public repo of that name must never receive a private source's history.
                want = "private" if private else "public"
                if not isinstance(existing.get("private"), bool):
                    raise SafetyError(f"{N} already exists and its visibility is unknown; refusing to push {want} history")
                if existing["private"] != bool(private):
                    have = "private" if existing["private"] else "public"
                    raise SafetyError(f"{N} already exists and is {have}, but the split is {want}; refusing to push into it")
                if self.db.one("select 1 from write_log where type='create_repo' and repo=? and proposal=? and ok=1",
                               (N, pid)):
                    return "exists (created by this proposal on an earlier run)"
                if has_refs():
                    raise SafetyError(f"{N} already exists and has refs; refusing to push into a non-empty repo")
                return "exists (empty, visibility matches)"
            self.ctx.writes.submit(WriteRequest("create_repo", N, {
                "name": N, "private": bool(private), "description": f"Split from {A}: {', '.join(dirs)}", "auto_init": False},
                pid))
            return "created"

        def push():
            if has_refs():
                raise SafetyError(f"{N} already has refs; refusing to push into a non-empty repo")
            self.g(["remote", "remove", "origin"], cwd=cdir, check=False)
            self.g(["remote", "add", "origin", n_url], cwd=cdir)
            self.safe_push(["origin", f"HEAD:refs/heads/{base}"], cdir, base, allow_default=True, repo=N)

        def verify():
            return json.dumps(self.verify_filtered(p, A, base, dirs, cdir), sort_keys=True)

        def policy():
            notes = []
            for f in self._policy_fixes(N, private):
                try:
                    notes.append(apply_policy_fix(self.ctx, f, pid))
                except Exception as e:  # noqa: BLE001
                    notes.append(f"{f['class']}: {e}")
            return "; ".join(notes) or "nothing to apply"

        return Plan(pid, "split", "create", [
            Step("clone-source", f"fresh clone of {A}", ["git", "clone", "--no-local", a_url, str(cdir)], lambda: self.clone(a_url, cdir)),
            Step("filter-repo", f"keep only {', '.join(dirs)} with history", ["git", "filter-repo", *fargs], filt),
            Step("create-repo", f"create {self.cfg.org}/{N} ({'private' if private else 'public'}), same visibility as {A}",
                 None, create),
            Step("push-new", f"push history to {N}", ["git", "push", "origin", f"HEAD:refs/heads/{base}"], push),
            Step("verify", f"check {N}'s tree equals {A}'s {', '.join(dirs)} tree and the commit map covers every touching commit",
                 None, verify, mutates=False),
            Step("apply-policy", f"apply the org policy to {N}", None, policy),
        ])

    def _policy_fixes(self, repo: str, private: bool) -> list[dict]:
        pol = self.cfg["policy"]
        fixes = []
        if pol.get("branch_protection") and not (private and self.cfg.get("plan", "free") == "free"):
            fixes.append({"class": "branch_protection", "repo": repo, "branch": self.cfg["policy"]["default_branch"],
                          "settings": pol["branch_protection"]})
        if pol.get("dependabot_alerts"):
            fixes.append({"class": "dependabot_alerts", "repo": repo})
        if pol.get("secret_scanning") == "public_only" and not private:
            fixes.append({"class": "secret_scanning", "repo": repo})
        return fixes

    def after_phase(self, p, plan):
        if plan.phase == "create":
            self.set_phase(p["id"], "pointer")
        elif plan.phase == "pointer":
            self.set_phase(p["id"], "done")
            proposals.set_status(self.db, p["id"], "done")

    def _pointer(self, p, s) -> Plan:
        pid, A, N, dirs = p["id"], s["source"], s["new_repo"], s["dirs"]
        if not self.db.ledger_done(pid, "create.verify"):
            raise Refused("new repo not verified; nothing will be removed from the source")
        base = s.get("source_branch") or self.default_branch(A)
        w = self.ctx.work_dir(pid) / "source-removal"
        url = self.ctx.remote_url(A)
        branch = f"ion/{pid}-remove"

        def edit():
            self.g(["checkout", "-q", "-B", branch, f"origin/{base}"], cwd=w)
            self.g(["rm", "-r", "-q", *dirs], cwd=w)
            safe_write(w, "MOVED.md",
                f"# Moved\n\n`{', '.join(dirs)}` now live in "
                f"[{self.cfg.org}/{N}](https://github.com/{self.cfg.org}/{N}) with their full history.\n")
            self.g(["add", "MOVED.md"], cwd=w)
            self.commit(w, f"Remove {', '.join(dirs)}; moved to {N} ({pid})")

        def pr():
            n = self.open_pr(A, branch, base, f"Remove {', '.join(dirs)}; moved to {N} [{pid}]",
                             f"The directories now live in {N} (history verified). This PR removes them here.")
            self.db.set_state(pid, removal_pr=n)
            return str(n)

        return Plan(pid, "split", "pointer", [
            Step("clone-source", f"clone {A}", ["git", "clone", url, str(w)], lambda: self.clone(url, w)),
            Step("remove-dirs", f"git rm {', '.join(dirs)} and add MOVED.md", ["git", "rm", "-r", *dirs], edit),
            Step("push", f"push {branch}", ["git", "push", "origin", f"{branch}:refs/heads/{branch}"],
                 lambda: self.safe_push(["origin", f"{branch}:refs/heads/{branch}"], w, base, repo=A)),
            Step("open-pr", f"open the removal PR in {A} and label it `needs decision`", None, pr)])

    def verify(self, p: dict) -> bool:
        return self.db.ledger_done(p["id"], "create.verify")

    def rollback_notes(self, p: dict) -> list[str]:
        s = p["subject"]
        return [f"Archive {s['new_repo']}.", f"{s['source']} still holds everything until the removal PR merges; close that PR to keep it."]
