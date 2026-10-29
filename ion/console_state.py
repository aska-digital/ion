"""Offline, read-only projection of the steward's saved evidence.

No live service, plugin, executor action, or Git subprocess is used here. Missing
evidence is a visible unknown, not permission to perform an operation.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from . import changesets, proposals
from .analyze import policy
from .analyze.lifecycle import stage_of
from .approve.core import evaluate
from .collect.snapshot import latest_snapshots
from .config import Config, config_fingerprint, load_config, parse_repo_override
from .db import DB
from .util import iso, now, parse_ts, redact


def state_home() -> Path:
    """Resolve the same state location as the CLI without creating it."""
    path = Path(os.environ.get("ION_HOME", "~/.ion")).expanduser()
    legacy = Path("~/.mimar").expanduser()
    if "ION_HOME" not in os.environ and not path.exists() and legacy.is_dir():
        return legacy
    return path


class OfflineConfig(Config):
    def is_paused(self):
        return bool(self.get("paused")) or (state_home() / "PAUSED").exists()


class ReadOnlyDB(DB):
    def __init__(self, path: Path):
        self.path = str(path)
        # SQLite read-only connections may still create or update WAL shared-memory
        # files. Ion uses rollback journals by default. Refuse WAL rather than
        # quietly ignoring uncheckpointed evidence with immutable=1.
        with path.open("rb") as stream:
            header = stream.read(20)
        if header[:16] == b"SQLite format 3\x00" and header[18:20] != b"\x01\x01":
            raise ValueError("WAL databases require a consistent SQLite backup in rollback-journal mode for offline export")
        self.conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma query_only=ON")
        # A read transaction holds one consistent database snapshot for the export.
        self.conn.execute("begin")

    def x(self, *args, **kwargs):
        raise RuntimeError("the offline console cannot change state")


class OfflineService:
    def __getattr__(self, name):
        raise RuntimeError(f"{name} is unavailable in the offline console")


@contextmanager
def open_console(config_path=None):
    root = state_home()
    if config_path is None:
        config_path = os.environ.get("ION_CONFIG") or os.environ.get("MIMAR_CONFIG")
        if config_path is None:
            choices = [Path.cwd() / "ion.yml", root / "ion.yml"]
            if "ION_HOME" not in os.environ:
                choices += [Path.cwd() / "mimar.yml", Path("~/.mimar/mimar.yml").expanduser()]
            config_path = next((p for p in choices if p.is_file()), None)
    # An explicit empty config avoids find_config_file(), which creates ION_HOME.
    if config_path is None:
        from .config import DEFAULTS, deep_merge
        cfg = OfflineConfig(deep_merge(DEFAULTS, {}))
    else:
        cfg = OfflineConfig(load_config(config_path))
    path = root / "state.db"
    db = ReadOnlyDB(path) if path.exists() else DB(":memory:")
    try:
        ctx = SimpleNamespace(cfg=cfg, db=db, gh=OfflineService(), writes=OfflineService(),
                              state_root=root, remote_url=lambda r: f"https://github.com/{cfg.org}/{r}.git",
                              mirror_path=lambda r: root / "cache" / f"{r}.git",
                              work_dir=lambda p: root / "work" / p)
        yield ctx
    finally:
        db.close()


def _url(org, repo, suffix=""):
    if not org or not re.fullmatch(r"[A-Za-z0-9_.-]+", str(org)) or not re.fullmatch(r"[A-Za-z0-9_.-]+", str(repo)):
        return None
    return f"https://github.com/{quote(org, safe='')}/{quote(repo, safe='')}{suffix}"


def _mirror(ctx, name, snap, override):
    if snap.get("archived") or snap.get("fork") or snap.get("empty") or name in ctx.cfg["exclude"] or override.get("ignore"):
        return {"status": "not-required", "detail": "Outside content analysis"}
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in (".", ".."):
        return {"status": "unknown", "detail": "Invalid repository path"}
    path = Path(ctx.mirror_path(name))
    branch, expected = snap.get("default_branch"), snap.get("head_oid")
    if not path.is_dir():
        return {"status": "missing", "detail": "No local mirror"}
    if not branch or not expected:
        return {"status": "unknown", "detail": "Snapshot has no branch or commit identity"}
    ref = f"refs/heads/{branch}"
    if any(p in (".", "..", "") for p in ref.split("/")) or "\\" in ref:
        return {"status": "unknown", "detail": "Invalid branch path"}
    try:
        loose = path / ref
        actual = None
        if loose.is_file() and loose.resolve().is_relative_to(path.resolve()):
            actual = loose.read_text().strip()
        elif (path / "packed-refs").is_file():
            for line in (path / "packed-refs").read_text().splitlines():
                fields = line.split()
                if len(fields) == 2 and fields[1] == ref:
                    actual = fields[0]
                    break
        if actual is None:
            return {"status": "unknown", "detail": "No readable direct ref (reftable and symbolic refs are not resolved)"}
        return {"status": "current" if actual == expected else "mismatch", "expected": expected, "actual": actual,
                "detail": "Mirror matches the collected commit" if actual == expected else "Mirror differs from the collected commit"}
    except (OSError, UnicodeError) as e:
        return {"status": "unknown", "detail": redact(str(e))}


def _proposal(ctx, row, at):
    p = proposals.view(row)
    try:
        approval = evaluate(ctx, p, at=at)
    except (KeyError, IndexError, sqlite3.Error, ValueError, TypeError) as e:
        approval = {"state": "unknown", "reason": f"Stored approval evidence unavailable: {redact(str(e))}", "approvers": []}
    plan = {"steps": [], "rollback": [], "error": None, "hash_matches": None}
    try:
        from .execute import executor_for
        if p["kind"] == "autofix":
            # plan() loads arbitrary configured plugin modules for this kind.
            raise ValueError("Autofix plugin plans are not recomputed offline. Review the recorded issue body and diff hashes.")
        ex = executor_for(ctx, p["kind"])
        generated = ex.plan(p)
        generated.subject = p["subject"]
        plan.update(phase=generated.phase, fingerprint=generated.fingerprint(),
                    hash_matches=generated.fingerprint() == p["plan_hash"],
                    steps=[{"name": s.name, "description": s.desc,
                            "commands": [shlex.join(s.cmd)] if s.cmd else []} for s in generated.steps],
                    rollback=ex.rollback_notes(p))
    except Exception as e:  # A bad saved proposal must not hide the rest of the fleet.
        plan["error"] = redact(str(e))
    try:
        ordered = changesets.states(ctx, p, query=False)
        for item in ordered:
            item["evidence"] = "saved state; not checked live"
            if item.get("number"):
                item["url"] = _url(ctx.cfg.get("org"), item["repo"], f"/pull/{item['number']}")
                # states() otherwise guesses 'open' for an unobserved PR.
                if f"{item['repo']}#{item['number']}" not in p["state"].get("pr_states", {}):
                    item["state"] = "unknown"
    except (KeyError, TypeError, ValueError):
        ordered = []
    return {k: p.get(k) for k in ("id", "kind", "status", "title", "score", "plan_hash", "subject", "evidence", "text", "updated")} | {
        "repos": proposals.involved_repos(p), "approval": approval, "plan": plan,
        "issue_url": _url(ctx.cfg.get("org"), ctx.cfg["tracker_repo"], f"/issues/{p['issue_number']}") if p.get("issue_number") else None,
        "recorded_body": p.get("body", ""), "changesets": ordered,
        "ledger": [dict(r) for r in ctx.db.q("select step,phase,ts,detail from ledger where proposal=? order by id", (p["id"],))]}


def build_state(ctx, at=None) -> dict:
    """Return versioned local evidence. 'current' describes evidence, never execution authority."""
    from .runhistory import freshness, recent_runs
    at = at or now()
    cfg, db = ctx.cfg, ctx.db
    snaps, gen = latest_snapshots(db)
    selected = db.one("select * from runs where id=?", (gen,))
    latest = db.one("select * from runs order by id desc limit 1")
    collection = dict(selected) if selected else {}
    collection.update(generation=gen, latest_attempt=dict(latest) if latest else {})
    analysis_row = db.one("select * from analyses order by id desc limit 1")
    errors = []
    try:
        analysis = json.loads(analysis_row["blob"]) if analysis_row else {}
        if not isinstance(analysis, dict):
            raise ValueError("analysis is not an object")
    except (ValueError, TypeError) as e:
        errors.append(f"Stored analysis could not be read: {e}")
        analysis = {}
    config_matches = analysis.get("config_fingerprint") == config_fingerprint(cfg) if analysis.get("config_fingerprint") else None
    reports = []
    for name, snap in sorted(snaps.items()):
        override, error = parse_repo_override(snap.get("override_text"))
        reports.append({k: snap.get(k) for k in ("description", "private", "archived", "fork", "default_branch", "pushed_at")} | {
            "name": name, "url": _url(cfg.get("org"), name), "topics": snap.get("topics", []),
            "lifecycle": stage_of(name, snap, cfg, now=at), "governed": policy.governed(cfg, override, name),
            "health": analysis.get("health", {}).get(name, []), "policy": analysis.get("policy", {}).get(name, []),
            "mirror": _mirror(ctx, name, snap, override), "override_error": error})
    dossiers = []
    for row in db.q("select * from proposals order by num desc"):
        try:
            dossiers.append(_proposal(ctx, row, at))
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            errors.append(f"Proposal {row['id']} could not be read: {e}")
            dossiers.append({"id": row["id"], "kind": row["kind"], "status": row["status"], "title": row["title"],
                "plan_hash": row["plan_hash"], "repos": [], "score": row["score"], "error": str(e),
                "plan": {"steps": [], "rollback": [], "error": "Saved proposal is malformed", "hash_matches": None},
                "approval": {"state": "unknown", "reason": "Saved proposal is malformed", "approvers": []}})
    fresh = freshness(ctx, at=at, stale_after_hours=cfg["report"]["stale_after_hours"])
    complete = bool(selected and selected["finished"] and not selected["partial"])
    bad_mirrors = [r for r in reports if r["mirror"]["status"] not in ("current", "not-required")]
    generation_matches = bool(analysis_row and analysis_row["gen"] == gen and gen)
    checks = [
        {"id": "collection", "status": "pass" if complete else "fail", "label": "Complete collection",
         "detail": f"Snapshot generation {gen}" if complete else "No complete collection. Saved partial evidence may be shown."},
        {"id": "latest-attempt", "status": "pass" if complete and latest and latest["id"] == gen else "warn",
         "label": "Latest collection attempt", "detail": "Latest attempt supplies this snapshot" if complete and latest and latest["id"] == gen else "A newer attempt is incomplete, or no collection has finished."},
        {"id": "analysis", "status": "pass" if generation_matches else "fail", "label": "Analysis generation",
         "detail": f"Analysis uses generation {analysis_row['gen']}" if analysis_row else "No analysis has been saved."},
        {"id": "config", "status": "pass" if config_matches else "unknown" if config_matches is None else "fail",
         "label": "Configuration provenance", "detail": "Matches current configuration" if config_matches else "Analysis has no configuration fingerprint. Run analysis again." if config_matches is None else "Configuration changed since analysis. Run analysis again."},
        {"id": "mirrors", "status": "warn" if bad_mirrors else "pass" if snaps else "unknown", "label": "Content coverage",
         "detail": f"{len(bad_mirrors)} repositories lack a matching readable mirror ref."},
        {"id": "freshness", "status": "pass" if fresh["status"] == "fresh" else "warn", "label": "Pipeline freshness", "detail": fresh["reason"]},
    ]
    if errors:
        checks.append({"id": "stored-evidence", "status": "fail", "label": "Saved evidence errors", "detail": "; ".join(errors)})
    # Offline evidence must never advertise a previous successful run as the latest outcome.
    if fresh.get("latest_status") not in (None, "success"):
        checks.append({"id": "latest-pipeline", "status": "warn", "label": "Latest pipeline outcome", "detail": str(fresh["latest_status"])})
    if any(p["plan"]["hash_matches"] is False for p in dossiers if p["status"] not in ("done", "dropped", "rejected")):
        checks.append({"id": "plan-hashes", "status": "warn", "label": "Plan reproducibility",
                       "detail": "Some regenerated phase plans differ from their stored approval hash. Inspect the dossier and tracker issue."})
    payload = {"schema_version": 1, "generated_at": iso(at), "demo": False, "org": cfg.get("org") or "Not configured",
        "summary": {"repositories": len(reports), "active_repositories": sum(not r["archived"] for r in reports),
                    "health_findings": sum(len(r["health"]) for r in reports),
                    "policy_drift": sum(f.get("status") == "drift" for r in reports for f in r["policy"]),
                    "awaiting_approval": sum(p["status"] == "awaiting-approval" for p in dossiers),
                    "open_proposals": sum(p["status"] not in ("done", "dropped", "rejected") for p in dossiers)},
        "safety": {"paused": cfg.is_paused(), "breaker": db.kv_get("last_breaker_trip"),
                   "execution_authority": "Existing approval channels only"},
        "freshness": fresh, "readiness": {"status": "empty" if not snaps else "current" if all(c["status"] == "pass" for c in checks) else "attention", "checks": checks},
        "collection": collection, "analysis": {"generation": analysis_row["gen"] if analysis_row else None,
                                                "ts": analysis_row["ts"] if analysis_row else None, "config_matches": config_matches},
        "repositories": reports, "proposals": dossiers, "runs": recent_runs(ctx)}
    # Scrub every string leaf, including legacy errors that predate the journal.
    # Secret-shaped field names protect bare values with no credential syntax.
    from .runhistory import safe_text
    secret_keys = {"authorization", "proxyauthorization", "password", "passwd", "secret", "clientsecret",
                   "apikey", "xapikey", "accesstoken", "refreshtoken", "authtoken", "token"}

    def scrub(value, secret=False):
        if isinstance(value, dict):
            return {k: scrub(v, secret or re.sub(r"[^a-z0-9]", "", str(k).lower()) in secret_keys)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v, secret) for v in value]
        if secret and value is not None:
            return "***"
        return safe_text(value) if isinstance(value, str) else value

    return scrub(json.loads(json.dumps(payload, default=str)))
