"""Small executors: archive, rename, policy_fix, health_fix, new_repo, move_code."""
from __future__ import annotations

import json

from .. import proposals
from ..util import SafetyError, safe_write
from .. import changesets
from ..writes import WriteRequest
from .base import Executor, Plan, Step, filter_args
from .provenance import origin_callback


def protection_body(settings: dict) -> dict:
    return {
        "required_status_checks": None,
        "enforce_admins": bool(settings.get("enforce_admins", False)),
        "required_pull_request_reviews": ({"required_approving_review_count": int(settings.get("required_approvals", 0))}
                                          if settings.get("require_pr") else None),
        "restrictions": None,
        "allow_force_pushes": bool(settings.get("allow_force_pushes", False)),
        "allow_deletions": bool(settings.get("allow_deletions", False)),
    }


def apply_policy_fix(ctx, fix: dict, proposal: str | None = None) -> str:
    """Idempotent typed write requests. Returns a short note."""
    repo, cls = fix["repo"], fix["class"]
    w = ctx.writes
    if cls == "branch_protection":
        w.submit(WriteRequest("put_branch_protection", repo, {"branch": fix["branch"], "settings": fix["settings"]}, proposal))
        return f"branch_protection on {repo}:{fix['branch']}"
    if cls == "dependabot_alerts":
        w.submit(WriteRequest("enable_dependabot_alerts", repo, {}, proposal))
        return f"dependabot_alerts on {repo}"
    if cls == "secret_scanning":
        w.submit(WriteRequest("enable_secret_scanning", repo, {}, proposal))
        return f"secret_scanning on {repo}"
    raise ValueError(f"unknown policy class {cls}")


class PolicyFixExecutor(Executor):
    kind = "policy_fix"

    def plan(self, p: dict) -> Plan:
        s = p["subject"]
        return Plan(p["id"], "policy_fix", "apply", [Step(
            "apply", f"{s['class']} on {s['repo']}", None, lambda: apply_policy_fix(self.ctx, s, p['id']))])

    def after_phase(self, p, plan):
        proposals.set_status(self.db, p["id"], "done")

    def rollback_notes(self, p):
        return ["Revert the setting in the repository's settings page."]


class ArchiveExecutor(Executor):
    kind = "archive"

    def plan(self, p):
        r = p["subject"]["repo"]
        return Plan(p["id"], "archive", "apply", [
            Step("archive", f"archive {r} (read-only; nothing is deleted)", None,
                 lambda: self.ctx.writes.submit(WriteRequest("archive_repo", r, {}, p["id"])) and "archived"),
            Step("verify", "confirm archived", None,
                 lambda: self._chk(r), mutates=False)])

    def _chk(self, r):
        if not self.gh.rest("GET", f"/repos/{self.cfg.org}/{r}").get("archived"):
            raise RuntimeError("repo is not archived after the call")
        return "archived"

    def after_phase(self, p, plan):
        proposals.set_status(self.db, p["id"], "done")

    def rollback_notes(self, p):
        return [f"Unarchive {p['subject']['repo']} from its settings page."]


class RenameExecutor(Executor):
    kind = "rename"

    def plan(self, p):
        s = p["subject"]
        return Plan(p["id"], "rename", "apply", [Step(
            "rename", f"rename {s['repo']} to {s['new_name']} (GitHub keeps a redirect from the old name)", None,
            lambda: self.ctx.writes.submit(WriteRequest("rename_repo", s["repo"], {"new_name": s["new_name"]}, p["id"])) and "renamed")])

    def after_phase(self, p, plan):
        proposals.set_status(self.db, p["id"], "done")

    def rollback_notes(self, p):
        return [f"Rename back to {p['subject']['repo']}."]


class NewRepoExecutor(Executor):
    kind = "new_repo"

    def plan(self, p):
        s = p["subject"]
        return Plan(p["id"], "new_repo", "apply", [Step(
            "create", f"create {s['name']} ({'private' if s.get('private', True) else 'public'})", None,
            lambda: self.ctx.writes.submit(WriteRequest("create_repo", s["name"], {
                "name": s["name"], "private": s.get("private", True), "description": s.get("description", ""),
                "auto_init": True}, p["id"])) and "created")])

    def after_phase(self, p, plan):
        proposals.set_status(self.db, p["id"], "done")

    def rollback_notes(self, p):
        return [f"Archive {p['subject']['name']}."]


README_SKELETON = "# {name}\n\n{description}\n\n## Status\n\nTODO: describe what this repository is for, how to run it, and who maintains it.\n"


class HealthFixExecutor(Executor):
    kind = "health_fix"

    def plan(self, p):
        s = p["subject"]
        pid, repo = p["id"], s["repo"]
        w = self.ctx.work_dir(pid) / "repo"
        url = self.ctx.remote_url(repo)
        base = self.default_branch(repo)
        branch = f"ion/{pid}"
        snaps_desc = s.get("description") or ""

        def edit():
            self.g(["checkout", "-q", "-B", branch, f"origin/{base}"], cwd=w)
            safe_write(w, "README.md", README_SKELETON.format(name=repo, description=snaps_desc))
            self.g(["add", "README.md"], cwd=w)
            self.commit(w, f"Add README skeleton ({pid})")

        def pr():
            n = self.open_pr(repo, branch, base, f"Add README skeleton [{pid}]", "Adds a README skeleton to be filled in by a maintainer.")
            self.db.set_state(pid, pr_number=n)
            return str(n)
        return Plan(pid, "health_fix", "apply", [
            Step("clone", f"clone {repo}", ["git", "clone", url, str(w)], lambda: self.clone(url, w)),
            Step("edit", "add README.md skeleton", None, edit),
            Step("push", f"push {branch}", ["git", "push", "origin", f"{branch}:refs/heads/{branch}"],
                 lambda: self.safe_push(["origin", f"{branch}:refs/heads/{branch}"], w, base, repo=repo)),
            Step("open-pr", "open the PR and label it `needs decision`", None, pr)])

    def after_phase(self, p, plan):
        proposals.set_status(self.db, p["id"], "done")

    def rollback_notes(self, p):
        return ["Close the PR."]


class MoveCodeExecutor(Executor):
    """Move directory D from repo S into repo T: PR 1 adds it to T (subtree-style), PR 2 removes it from S after PR 1 merges."""
    kind = "move_code"

    def plan(self, p):
        s = p["subject"]
        phase = p["state"].get("phase", "add")
        pid, S, T, D = p["id"], s["source"], s["target"], s["dir"]
        w = self.ctx.work_dir(pid)
        dest = s.get("dest_prefix") or D
        s_url, t_url = self.ctx.remote_url(S), self.ctx.remote_url(T)
        sb, tb = self.default_branch(S), self.default_branch(T)
        if phase == "add":
            branch = f"ion/{pid}"
            fdir, tdir = w / "filtered", w / "target"

            def filt():
                self.g(["filter-repo", *filter_args([D]), "--commit-callback", origin_callback(self.cfg.org, S), "--force"],
                       cwd=fdir)

            def verify_filter():
                return json.dumps(self.verify_filtered(p, S, sb, [D], fdir), sort_keys=True)

            def verify_import():
                want = self.g(["rev-parse", "HEAD^{tree}"], cwd=fdir).stdout.strip()
                got = self.g(["rev-parse", f"HEAD:{dest}"], cwd=tdir).stdout.strip()
                if got != want:
                    raise RuntimeError(f"tree mismatch: {T}/{dest} is {got}, filtered {D} is {want}")
                return want

            def sub():
                self.g(["checkout", "-q", "-B", branch, f"origin/{tb}"], cwd=tdir)
                cur = self.g(["rev-parse", "--abbrev-ref", "HEAD"], cwd=fdir).stdout.strip()
                self.g([*self.identity_args(), "subtree", "add", f"--prefix={dest}", str(fdir), cur, "-m",
                        f"Move {D} from {S} (history preserved) [{pid}]\n\nIon-Proposal: {pid}"], cwd=tdir)

            def pr():
                n = self.open_pr(T, branch, tb, f"Move {D} from {S} [{pid}]", f"Adds `{dest}/` with its history from {S}. {S} is cleaned up in a second PR after this merges.")
                self.db.set_state(pid, pr_number=n)
                return str(n)
            return Plan(pid, "move_code", "add", [
                Step("clone-source", f"fresh clone of {S}", ["git", "clone", s_url, str(fdir)], lambda: self.clone(s_url, fdir)),
                Step("filter", f"keep only {D}", ["git", "filter-repo", *filter_args([D])], filt),
                Step("verify-filter", f"check the filtered tree equals {S}'s {D} tree and the commit map is complete", None,
                     verify_filter, mutates=False),
                Step("clone-target", f"clone {T}", ["git", "clone", t_url, str(tdir)], lambda: self.clone(t_url, tdir)),
                Step("subtree-add", f"git subtree add --prefix={dest}", None, sub),
                Step("verify-import", f"check {T}'s {dest}/ tree equals the filtered tree", None, verify_import, mutates=False),
                Step("push", f"push {branch}", ["git", "push", "origin", f"{branch}:refs/heads/{branch}"],
                     lambda: self.safe_push(["origin", f"{branch}:refs/heads/{branch}"], tdir, tb, repo=T)),
                Step("open-pr", f"open PR in {T} and label it `needs decision`", None, pr)])
        if phase == "remove":
            from .base import Waiting
            branch = f"ion/{pid}-remove"
            sdir = w / "source-removal"

            def merged():
                n = p["state"].get("pr_number")
                if not n or not self.pr_merged(T, n):
                    raise Waiting(f"PR #{n} in {T} not merged yet")
                return "merged"

            def edit():
                self.g(["checkout", "-q", "-B", branch, f"origin/{sb}"], cwd=sdir)
                self.g(["rm", "-r", "-q", "--", D], cwd=sdir)
                safe_write(sdir, "MOVED.md", f"# Moved\n\n`{D}` now lives in {self.cfg.org}/{T} under `{dest}/`.\n")
                self.g(["add", "MOVED.md"], cwd=sdir)
                self.commit(sdir, f"Remove {D}; moved to {T} ({pid})")

            def pr():
                dep = changesets.depends_on_line(self.ctx, self.fresh(pid), "remove")
                n = self.open_pr(S, branch, sb, f"Remove {D}; moved to {T} [{pid}]", f"{D} now lives in {T}." + (f"\n\n{dep}" if dep else ""))
                self.db.set_state(pid, removal_pr=n)
                return str(n)
            return Plan(pid, "move_code", "remove", [
                Step("pr-merged", f"wait for the PR in {T} to merge", None, merged, mutates=False),
                Step("clone-source", f"clone {S}", ["git", "clone", s_url, str(sdir)], lambda: self.clone(s_url, sdir)),
                Step("remove", f"git rm -r {D}", ["git", "rm", "-r", D], edit),
                Step("push", f"push {branch}", ["git", "push", "origin", f"{branch}:refs/heads/{branch}"],
                     lambda: self.safe_push(["origin", f"{branch}:refs/heads/{branch}"], sdir, sb, repo=S)),
                Step("open-pr", f"open PR in {S} and label it `needs decision`", None, pr)])
        return Plan(pid, "move_code", "done")

    def after_phase(self, p, plan):
        if plan.phase == "add":
            self.set_phase(p["id"], "remove")
        elif plan.phase == "remove":
            self.set_phase(p["id"], "done")
            proposals.set_status(self.db, p["id"], "done")

    def rollback_notes(self, p):
        return ["Close the open PRs; the source repo is untouched until the removal PR merges."]
