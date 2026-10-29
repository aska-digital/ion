"""One tracking issue per proposal in the tracker repo. Bodies are edited in place; no comments, ever."""
from __future__ import annotations

import json

from .util import Waiting, iso

LABELS = {"ion:proposal": "5319e7", "ion:approved": "0e8a16", "ion:rejected": "b60205", "ion:hold": "fbca04",
          "ion:dashboard": "ededed", "needs decision": "D93F0B"}
NEEDS_DECISION = "needs decision"   # org-wide label: the command-centre decision feed
KINDS = ["merge", "split", "archive", "rename", "move_code", "policy_fix", "health_fix", "new_repo", "autofix"]
STATES = ["awaiting-approval", "approved", "held", "executing", "done", "failed", "rejected", "dropped"]


def repo_path(ctx) -> str:
    return f"/repos/{ctx.cfg.org}/{ctx.cfg['tracker_repo']}"


def ensure_labels(ctx) -> None:
    if ctx.db.kv_get("labels_ready:v2"):
        return
    wanted = dict(LABELS)
    wanted.update({f"kind:{k}": "1d76db" for k in KINDS})
    wanted.update({f"state:{s}": "cfd3d7" for s in STATES})
    for name, color in wanted.items():
        try:
            ctx.writes.tracker("create_label", name=name, color=color)
        except Waiting:
            return                                      # cap reached: try again next run
        except Exception:  # noqa: BLE001  (422 already exists)
            pass
    ctx.db.kv_set("labels_ready:v2", True)


def _changeset_section(ctx, p: dict) -> list[str]:
    from . import changesets
    try:
        rows = changesets.render(ctx, p)
    except Exception:  # noqa: BLE001
        return []
    return ["## Changesets (in order)", *rows, ""] if rows else []


def render_body(ctx, p: dict, plan_lines: list[str], rollback: list[str], approval: dict | None = None) -> str:
    t, s = p["text"], p["subject"]
    ev = json.dumps(p["evidence"], indent=2, default=str)
    if len(ev) > 9000:
        ev = ev[:9000] + "\n... (truncated)"
    maint = ", ".join(ctx.cfg["maintainers"]) or "(none configured)"
    lines = [
        f"**{p['id']}** · `{p['kind']}` · score **{p['score']:.2f}** · confidence {t.get('confidence', '?')} · status `{p['status']}`", "",
        t.get("summary", ""), "", "## Why", t.get("rationale", ""), "",
        "## Risks", *[f"- {r}" for r in t.get("risks", [])], "",
        "## Blast radius", t.get("blast_radius", ""), "",
        *_changeset_section(ctx, p),
        "## Plan (exact commands, dry-run output)", "```", *plan_lines, "```", "",
        "## Rollback", *[f"- {r}" for r in (t.get("rollback_plan") or rollback)], "",
        "## Evidence", "<details><summary>show</summary>", "", "```json", ev, "```", "</details>", "",
        "## How to decide",
        f"Add the label `ion:approved` (or `ion:rejected` / `ion:hold`). Only maintainers count: {maint}. "
        f"Approvals expire after {ctx.cfg['approval_ttl_days']} days, and an approval counts only for the exact plan "
        f"shown here: if the plan changes, the hash below changes, the label is taken off and approval is asked again.", "",
        "The approval hash covers the subject and initial phase steps, using `P-XXXX` for the proposal id. "
        "Later phase commands are displayed as execution advances and are not included in that hash.", "",
        f"`plan-sha256: {p['plan_hash']}`", ""]
    if approval:
        lines += [f"_Approval state: {approval['state']} ({approval['reason']})_", ""]
    notes = p["state"].get("notes") or []
    if notes:
        lines += ["## Notes", *[f"- {n}" for n in notes[-8:]], ""]
    lines.append(f"<sub>Last updated {iso()} by ion. This issue is edited in place; please do not comment.</sub>")
    return "\n".join(lines)


def _sync_needs_decision(ctx, p: dict, number: int) -> None:
    """`needs decision` is on the issue exactly while the proposal awaits a maintainer.

    The flag in proposal state is what we last applied, so a refresh that changes
    nothing writes nothing. A capped write leaves the flag stale and retries next refresh.
    """
    want = p["status"] == "awaiting-approval"
    if bool(p["state"].get("needs_decision_label")) == want:
        return
    try:
        if want:
            ctx.writes.tracker("add_label", number=int(number), labels=[NEEDS_DECISION])
        else:
            ctx.writes.tracker("remove_label", number=int(number), label=NEEDS_DECISION)
    except Waiting:
        return
    except Exception:  # noqa: BLE001  (e.g. removing a label that is not there)
        pass
    ctx.db.set_state(p["id"], needs_decision_label=want)


def upsert_issue(ctx, p: dict, body: str) -> int | None:
    if ctx.cfg.is_paused():
        return p.get("issue_number")
    ensure_labels(ctx)
    title = f"[{p['id']}] {p['text'].get('title') or p['title']}"
    if p.get("issue_number"):
        ctx.writes.tracker("edit_issue_body", number=int(p["issue_number"]), title=title, body=body)
        _sync_needs_decision(ctx, p, int(p["issue_number"]))
        return p["issue_number"]
    labels = ["ion:proposal", f"kind:{p['kind']}", f"state:{p['status']}"]
    if p["status"] == "awaiting-approval":
        labels.append(NEEDS_DECISION)
    issue = ctx.writes.tracker("open_issue", title=title, body=body, labels=labels)
    ctx.db.x("update proposals set issue_number=? where id=?", (issue["number"], p["id"]))
    ctx.db.set_state(p["id"], needs_decision_label=p["status"] == "awaiting-approval")
    return issue["number"]


def set_state_label(ctx, p: dict, old: str | None, new: str) -> None:
    n = p.get("issue_number")
    if not n or old == new or ctx.cfg.is_paused():
        return
    ensure_labels(ctx)
    if old:
        try:
            ctx.writes.tracker("remove_label", number=int(n), label=f"state:{old}")
        except Waiting:
            raise
        except Exception:  # noqa: BLE001
            pass
    ctx.writes.tracker("add_label", number=int(n), labels=[f"state:{new}"])
