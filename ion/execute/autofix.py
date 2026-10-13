"""Fleet autofix as a proposal: one PR per repo, opened only after a maintainer approves the exact diffs."""
from __future__ import annotations

from .. import changesets, proposals
from ..fleet import diff_of, digest, get_fixer
from ..writes import WriteRequest
from .base import Executor, Plan, Step


class AutofixExecutor(Executor):
    """subject: {fixer, repos: [...], diffs: {repo: sha256 of the previewed diff}, branch_prefix}.

    The approval is bound to the plan hash, which covers `diffs`: a maintainer approves these exact changes. At run
    time each repo's fix is recomputed on a fresh clone and must match the approved diff, or the step stops."""
    kind = "autofix"

    def plan(self, p: dict) -> Plan:
        s = p["subject"]
        pid, fixer_name = p["id"], s["fixer"]
        fixer = get_fixer(self.ctx, fixer_name)
        branch = f"ion/{pid}"
        steps: list[Step] = []
        for repo in s["repos"]:
            w = self.ctx.work_dir(pid) / repo
            url = self.ctx.remote_url(repo)
            base = self.default_branch(repo)

            def fix(repo=repo, w=w, url=url, base=base):
                snap = {"default_branch": base, "description": self._description(repo)}
                got = digest(diff_of(self.ctx, fixer, repo, snap, w, url))
                want = s["diffs"].get(repo)
                if got != want:
                    raise RuntimeError(f"{repo}: the change differs from the approved diff ({got[:12]} != {str(want)[:12]}); "
                                       f"the repo changed since approval, so propose again")
                self.g(["checkout", "-q", "-B", branch], cwd=w)
                self.commit(w, f"{fixer.commit_message} ({pid})")
                return got[:12]

            def push(repo=repo, w=w, base=base):
                self.safe_push(["origin", f"{branch}:refs/heads/{branch}"], w, base, repo=repo)

            def pr(repo=repo, base=base):
                n = self.open_pr(repo, branch, base, f"{fixer.pr_title} [{pid}]",
                                 f"{fixer.pr_body}\n\nPart of {pid} in {self.cfg.org}/{self.cfg['tracker_repo']}. "
                                 f"A maintainer merges this PR.")
                prs = dict(self.fresh(pid)["state"].get("prs") or {})
                prs[repo] = n
                self.db.set_state(pid, prs=prs)
                return str(n)

            steps += [
                Step(f"{repo}.fix", f"{repo}: clone, apply {fixer_name}, check against the approved diff, commit",
                     ["ion", "autofix", fixer_name, "--repo", repo], fix),
                Step(f"{repo}.push", f"{repo}: push {branch}", ["git", "push", "origin", f"{branch}:refs/heads/{branch}"], push),
                Step(f"{repo}.open-pr", f"{repo}: open the PR and label it `needs decision`", None, pr)]
        return Plan(pid, "autofix", "apply", steps)

    def _description(self, repo: str) -> str:
        from ..collect.snapshot import latest_snapshots
        return (latest_snapshots(self.db)[0].get(repo) or {}).get("description") or ""

    def after_phase(self, p, plan):
        proposals.set_status(self.db, p["id"], "done")

    def rollback_notes(self, p):
        return [f"Close the PRs on branch ion/{p['id']}; nothing else is changed."]
