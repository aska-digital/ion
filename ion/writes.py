"""Typed write requests and the run-wide circuit breaker.

Analysis, the proposal engine and the LLM never call GitHub for writing. Everything that changes state outside the
local database is a `WriteRequest` of a known type. The broker validates it against a schema, checks the kill switch,
applies a per-type cap for the run, asks the circuit breaker whether the run as a whole is still sane, performs the
call and logs it. A bug upstream can therefore not fan out into forty PRs: the fifth `open_pr` of a run is deferred
(a `CapReached`, which executors treat as "try again next run"), and a run that starts touching an unreasonable share
of the org is stopped for good (`BreakerTripped`).
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

import jsonschema

from .util import Refused, Waiting, iso

log = logging.getLogger("ion")

_S = {"type": "string"}
_REPO = {"type": "string", "pattern": r"^[A-Za-z0-9._-]+$"}


def _obj(required: list[str], **props) -> dict:
    return {"type": "object", "required": required, "properties": props, "additionalProperties": False}


# type -> (payload schema, default cap per run, does it need a repo)
TYPES: dict[str, tuple[dict, int]] = {
    "create_label": (_obj(["name", "color"], name=_S, color=_S), 40),
    "open_issue": (_obj(["title", "body"], title=_S, body=_S, labels={"type": "array", "items": _S}), 20),
    "edit_issue_body": (_obj(["number", "body"], number={"type": "integer"}, title=_S, body=_S), 80),
    "add_label": (_obj(["number", "labels"], number={"type": "integer"}, labels={"type": "array", "items": _S, "minItems": 1}), 80),
    "remove_label": (_obj(["number", "label"], number={"type": "integer"}, label=_S), 80),
    "open_pr": (_obj(["title", "head", "base", "body"], title=_S, head=_S, base=_S, body=_S), 5),
    "push_branch": (_obj(["branch", "base"], branch=_S, base=_S, initial={"type": "boolean"}), 15),
    "put_branch_protection": (_obj(["branch", "settings"], branch=_S, settings={"type": "object"}), 10),
    "enable_dependabot_alerts": (_obj([]), 10),
    "enable_secret_scanning": (_obj([]), 10),
    "archive_repo": (_obj([]), 1),
    "create_repo": (_obj(["name", "private"], name=_REPO, private={"type": "boolean"}, description=_S,
                         auto_init={"type": "boolean"}), 2),
    "rename_repo": (_obj(["new_name"], new_name=_REPO), 2),
}
DEFAULT_CAPS = {k: v[1] for k, v in TYPES.items()}
# types that count towards the "how much of the org did this run touch" breaker rule
REPO_CHANGING = {"open_pr", "push_branch", "put_branch_protection", "enable_dependabot_alerts", "enable_secret_scanning",
                 "archive_repo", "create_repo", "rename_repo"}

DEFAULT_GUARDS = {"enabled": True, "max_repo_fraction": 0.25, "min_repos": 4, "max_archives": 2, "max_writes": 150,
                  "forbid_protection_lowering": True}


class WriteRejected(Refused):
    """The request is malformed or not allowed."""


class CapReached(Waiting):
    """The per-run cap for this write type is used up. The step is deferred, not failed."""


class BreakerTripped(Refused):
    """The circuit breaker stopped the run."""


@dataclass(frozen=True)
class WriteRequest:
    type: str
    repo: str
    payload: dict = field(default_factory=dict)
    proposal: str | None = None

    @property
    def target(self) -> str:
        p = self.payload
        for k in ("number", "branch", "name", "new_name", "head"):
            if k in p:
                return str(p[k])
        return ""


def lowers_protection(rule: dict | None, settings: dict) -> list[str]:
    """Ways in which applying `settings` would make an existing protection rule weaker. Empty list: not weaker."""
    if not rule:
        return []
    out = []
    if rule.get("requiresApprovingReviews") and not settings.get("require_pr"):
        out.append("drops required pull-request reviews")
    elif settings.get("require_pr") and rule.get("requiredApprovingReviewCount", 0) > int(settings.get("required_approvals", 0)):
        out.append("lowers the required approval count")
    if not rule.get("allowsForcePushes") and settings.get("allow_force_pushes"):
        out.append("allows force pushes")
    if not rule.get("allowsDeletions") and settings.get("allow_deletions"):
        out.append("allows deleting the branch")
    if rule.get("isAdminEnforced") and not settings.get("enforce_admins"):
        out.append("stops enforcing rules for admins")
    return out


def current_rule(db, repo: str) -> dict | None:
    from .collect.snapshot import latest_snapshots
    import fnmatch
    snap = latest_snapshots(db)[0].get(repo)
    for r in (snap or {}).get("protection") or []:
        if fnmatch.fnmatch(snap.get("default_branch") or "", r.get("pattern") or ""):
            return r
    return None


class Broker:
    def __init__(self, ctx):
        self.ctx = ctx
        self.counts: dict[str, int] = {}
        self.total = 0
        self.touched: set[str] = set()
        self.tripped: str | None = None

    # configuration ------------------------------------------------------------------------
    @property
    def caps(self) -> dict[str, int]:
        return {**DEFAULT_CAPS, **(self.ctx.cfg.get("writes", {}).get("caps") or {})}

    @property
    def guards(self) -> dict:
        return {**DEFAULT_GUARDS, **(self.ctx.cfg.get("guards") or {})}

    def reset(self) -> None:
        self.counts, self.total, self.touched, self.tripped = {}, 0, set(), None

    # breaker --------------------------------------------------------------------------------
    def trip(self, reason: str) -> None:
        self.tripped = reason
        self.ctx.db.kv_set("last_breaker_trip", {"ts": iso(), "reason": reason})
        log.error("circuit breaker tripped: %s", reason)
        raise BreakerTripped(reason)

    def _repo_universe(self) -> int:
        from .collect.snapshot import latest_snapshots
        cfg = self.ctx.cfg
        snaps, _ = latest_snapshots(self.ctx.db)
        return sum(1 for n, s in snaps.items() if not s.get("archived") and n not in cfg["exclude"])

    def check_blast(self, repos: set[str], archives: int, lowerings: list[str]) -> str | None:
        """The pure rule set, shared by the preflight check and the in-flight check. Returns a reason or None."""
        g = self.guards
        if not g["enabled"]:
            return None
        if lowerings and g["forbid_protection_lowering"]:
            return "a branch-protection change would lower protection: " + "; ".join(lowerings[:3])
        if archives > g["max_archives"]:
            return f"{archives} repos would be archived in one run (limit {g['max_archives']})"
        total = self._repo_universe()
        if total and len(repos) >= g["min_repos"] and len(repos) / total > g["max_repo_fraction"]:
            return (f"the run would touch {len(repos)} of {total} repos ({len(repos) / total:.0%}); "
                    f"limit is {g['max_repo_fraction']:.0%}")
        return None

    def _guard(self, req: WriteRequest) -> None:
        if self.tripped:
            raise BreakerTripped(self.tripped)
        g = self.guards
        if not g["enabled"]:
            return
        if self.total + 1 > g["max_writes"]:
            self.trip(f"more than {g['max_writes']} writes in one run")
        lowerings = []
        if req.type == "put_branch_protection":
            lowerings = lowers_protection(current_rule(self.ctx.db, req.repo), req.payload.get("settings") or {})
        repos = set(self.touched)
        if req.type in REPO_CHANGING and req.repo != self.ctx.cfg["tracker_repo"]:
            repos.add(req.repo)
        archives = self.counts.get("archive_repo", 0) + (1 if req.type == "archive_repo" else 0)
        why = self.check_blast(repos, archives, lowerings)
        if why:
            self.trip(why)

    # validation + execution -------------------------------------------------------------------
    def validate(self, req: WriteRequest) -> None:
        spec = TYPES.get(req.type)
        if spec is None:
            raise WriteRejected(f"unknown write type {req.type!r}")
        try:
            jsonschema.validate(req.repo, _REPO)
            jsonschema.validate(req.payload, spec[0])
        except jsonschema.ValidationError as e:
            raise WriteRejected(f"{req.type}: {e.message}") from e
        if req.type == "push_branch":
            b, base = req.payload["branch"], req.payload["base"]
            if b == base and not req.payload.get("initial"):
                raise WriteRejected(f"push_branch: refusing to push the default branch {base}")
            if not (b.startswith("ion/") or req.payload.get("initial")):
                raise WriteRejected("push_branch: only ion/* branches may be pushed")
        if req.type == "put_branch_protection" and req.payload["branch"].startswith("ion/"):
            raise WriteRejected("put_branch_protection: not for work branches")

    def submit(self, req: WriteRequest, fn: Callable[[], object] | None = None):
        """Validate, cap, guard, perform, log. `fn` performs non-REST requests (a guarded git push)."""
        cfg = self.ctx.cfg
        self.validate(req)
        if cfg.is_paused():
            raise Refused("kill switch is on: no writes")
        if self.counts.get(req.type, 0) >= int(self.caps.get(req.type, 0)):
            raise CapReached(f"per-run cap of {self.caps.get(req.type, 0)} {req.type} requests reached")
        self._guard(req)
        ok, detail = True, ""
        try:
            result = fn() if fn else self._rest(req)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)[:300]
            self._log(req, ok, detail)
            raise
        # Only a write that happened uses a cap slot or touches a repo. A failed
        # write is still logged above with ok=0, but it changed nothing, so it
        # must not burn the blast-radius budget (issue #60).
        self.counts[req.type] = self.counts.get(req.type, 0) + 1
        self.total += 1
        if req.type in REPO_CHANGING and req.repo != cfg["tracker_repo"]:
            self.touched.add(req.repo)
        self._log(req, ok, detail)
        return result

    def _log(self, req: WriteRequest, ok: bool, detail: str) -> None:
        self.ctx.db.x("insert into write_log(ts,type,repo,target,proposal,ok,detail) values(?,?,?,?,?,?,?)",
                      (iso(), req.type, req.repo, req.target, req.proposal, int(ok), detail))

    def _rest(self, req: WriteRequest):
        gh, org, r, p = self.ctx.gh, self.ctx.cfg.org, req.repo, req.payload
        base = f"/repos/{org}/{r}"
        t = req.type
        if t == "create_label":
            return gh.rest("POST", base + "/labels", p)
        if t == "open_issue":
            return gh.rest("POST", base + "/issues", p)
        if t == "edit_issue_body":
            return gh.rest("PATCH", f"{base}/issues/{p['number']}", {k: v for k, v in p.items() if k != "number"})
        if t == "add_label":
            return gh.rest("POST", f"{base}/issues/{p['number']}/labels", {"labels": p["labels"]})
        if t == "remove_label":
            return gh.rest("DELETE", f"{base}/issues/{p['number']}/labels/{urllib.parse.quote(p['label'], safe=':')}")
        if t == "open_pr":
            return gh.rest("POST", base + "/pulls", p)
        if t == "put_branch_protection":
            from .execute.simple import protection_body
            return gh.rest("PUT", f"{base}/branches/{p['branch']}/protection", protection_body(p["settings"]))
        if t == "enable_dependabot_alerts":
            return gh.rest("PUT", base + "/vulnerability-alerts")
        if t == "enable_secret_scanning":
            return gh.rest("PATCH", base, {"security_and_analysis": {"secret_scanning": {"status": "enabled"}}})
        if t == "archive_repo":
            return gh.rest("PATCH", base, {"archived": True})
        if t == "create_repo":
            return gh.rest("POST", f"/orgs/{org}/repos", p)
        if t == "rename_repo":
            return gh.rest("PATCH", base, {"name": p["new_name"]})
        raise WriteRejected(f"no transport for {t}")

    # convenience ----------------------------------------------------------------------------
    def tracker(self, type: str, **payload):
        return self.submit(WriteRequest(type, self.ctx.cfg["tracker_repo"], payload))

    def summary(self) -> dict:
        return {"counts": dict(self.counts), "total": self.total, "touched": sorted(self.touched), "tripped": self.tripped}
