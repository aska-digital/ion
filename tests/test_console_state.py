"""Offline evidence must remain readable without turning observation into action."""
import datetime as dt
import json
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from ion.config import config_fingerprint, load_config
from ion.console_state import _mirror, build_state, open_console
from ion.db import DB
from ion.util import iso

AT = dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc)


@pytest.fixture
def local(tmp_path, home):
    cfg = load_config(None, {"org": "acme", "maintainers": ["maint"]})
    db = DB(tmp_path / "saved.db")
    ctx = SimpleNamespace(cfg=cfg, db=db, mirror_path=lambda name: tmp_path / "cache" / f"{name}.git")
    yield ctx
    db.close()


def save_snapshot(ctx, partial=0, finished=True):
    rid = ctx.db.x("insert into runs(started,finished,partial,repos) values(?,?,?,1)",
                   (iso(AT), iso(AT) if finished else None, partial)).lastrowid
    snap = {"name": "alpha", "private": False, "archived": False, "fork": False,
            "default_branch": "main", "head_oid": "a" * 40, "pushed_at": iso(AT)}
    ctx.db.x("insert into snapshots(run_id,repo,blob) values(?,?,?)", (rid, "alpha", json.dumps(snap)))
    return rid, snap


def checks(state):
    return {c["id"]: c["status"] for c in state["readiness"]["checks"]}


def test_absent_home_is_not_created(tmp_path, monkeypatch):
    root = tmp_path / "never-created"
    monkeypatch.setenv("ION_HOME", str(root))
    monkeypatch.chdir(tmp_path)
    with open_console() as ctx:
        result = build_state(ctx, AT)
    assert result["readiness"]["status"] == "empty"
    assert result["freshness"]["status"] == "never"
    assert not root.exists()


def test_existing_legacy_database_is_read_only(tmp_path, monkeypatch):
    root = tmp_path / "operator"
    root.mkdir()
    path = root / "state.db"
    db = DB(path)
    db.x("drop table pipeline_steps")
    db.x("drop table pipeline_runs")
    db.close()
    (root / "ion.yml").write_text("org: acme\nmaintainers: [maint]\n")
    monkeypatch.setenv("ION_HOME", str(root))
    monkeypatch.chdir(tmp_path)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError("offline observation invoked a process")
    monkeypatch.setattr(subprocess, "run", forbidden)
    with open_console() as ctx:
        state = build_state(ctx, AT)
        with pytest.raises((RuntimeError, sqlite3.OperationalError)):
            ctx.db.x("delete from runs")
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after
    assert state["runs"] == []


def test_partial_only_evidence_never_reports_complete(local):
    save_snapshot(local, partial=1)
    state = build_state(local, AT)
    assert state["summary"]["repositories"] == 1
    assert checks(state)["collection"] == "fail"
    assert state["readiness"]["status"] == "attention"


def test_new_incomplete_attempt_does_not_replace_complete_generation(local):
    gen, _ = save_snapshot(local)
    newer, _ = save_snapshot(local, finished=False)
    state = build_state(local, AT)
    assert state["collection"]["generation"] == gen
    assert state["collection"]["latest_attempt"]["id"] == newer
    assert checks(state)["latest-attempt"] == "warn"


def test_analysis_generation_and_config_are_distinct_evidence(local):
    gen, _ = save_snapshot(local)
    local.db.x("insert into analyses(ts,gen,blob) values(?,?,?)",
               (iso(AT), gen + 1, json.dumps({"config_fingerprint": config_fingerprint(local.cfg)})))
    state = build_state(local, AT)
    assert checks(state)["analysis"] == "fail"
    assert checks(state)["config"] == "pass"
    local.cfg["quorum"] += 1
    assert checks(build_state(local, AT))["config"] == "fail"


def test_mirror_packed_refs_mismatch_and_unknown(local):
    _, snap = save_snapshot(local)
    path = local.mirror_path("alpha")
    assert _mirror(local, "alpha", snap, {})["status"] == "missing"
    path.mkdir(parents=True)
    assert _mirror(local, "alpha", snap, {})["status"] == "unknown"
    (path / "packed-refs").write_text("# pack-refs with: peeled\n" + "a" * 40 + " refs/heads/main\n")
    assert _mirror(local, "alpha", snap, {})["status"] == "current"
    loose = path / "refs" / "heads" / "main"
    loose.parent.mkdir(parents=True)
    loose.write_text("b" * 40 + "\n")
    assert _mirror(local, "alpha", snap, {})["status"] == "mismatch"
    assert _mirror(local, "../escape", snap, {})["status"] == "unknown"


def test_autofix_projection_does_not_load_plugins(local, monkeypatch):
    from ion import proposals
    from ion import fleet
    monkeypatch.setattr(fleet, "load_plugins", lambda *a: pytest.fail("plugin imported"))
    text = {"title": "<script>bad()</script>", "summary": "untrusted", "risks": [], "rollback_plan": []}
    p = proposals.create(local.db, "autofix", "autofix:one", {"repos": ["alpha"], "fixer": "hostile.module"}, {}, text, .8, "abc")
    state = build_state(local, AT)
    dossier = state["proposals"][0]
    assert dossier["id"] == p["id"]
    assert "not recomputed offline" in dossier["plan"]["error"]
    # Data preserves strings; HTML renderer has separate escaping coverage.
    assert dossier["title"] == text["title"]


def test_saved_approved_status_does_not_hide_expiration(local):
    from ion import proposals
    p = proposals.create(local.db, "archive", "archive:alpha", {"repo": "alpha"}, {},
                         {"title": "archive alpha"}, .8, "saved-plan", status="approved")
    local.db.x("update proposals set epoch=? where id=?", ("2026-09-01T00:00:00Z", p["id"]))
    local.db.x("insert into approvals(proposal,source,actor,action,event_ts,ref,valid,plan_hash) values(?,?,?,?,?,?,?,?)",
               (p["id"], "github_label", "maint", "approve", "2026-09-02T00:00:00Z", "old", 1, "saved-plan"))
    local.cfg["archive_requires_second_approval"] = False
    state = build_state(local, AT)
    assert state["proposals"][0]["status"] == "approved"
    assert state["proposals"][0]["approval"]["state"] == "expired"
    assert local.db.get_proposal(p["id"])["status"] == "approved"


def test_export_redacts_legacy_credentials_and_secret_fields(local):
    from ion import proposals
    prefix, suffix = 'saved evidence ' * 100, ' retained tail' * 100
    secret_note = prefix + 'https://user:old-password@example.com/path Authorization: Bearer old-token' + suffix
    local.db.x('insert into runs(started,finished,partial,note) values(?,?,1,?)', (iso(AT), iso(AT), secret_note))
    p = proposals.create(local.db, 'archive', 'archive:alpha', {'repo': 'alpha'},
                         {'api_key': 'bare-api-secret', 'nested': {'access_token': ['bare-token-secret'], 'count': 42}},
                         {'title': 'ordinary evidence'}, .8, 'saved-plan')
    local.db.ledger_add(p['id'], 'saved', 'before', 'password=ledger-secret')
    state = build_state(local, AT)
    exported = json.dumps(state)
    for secret in ('old-password', 'old-token', 'bare-api-secret', 'bare-token-secret', 'ledger-secret'):
        assert secret not in exported
    note = state['collection']['latest_attempt']['note']
    assert note.startswith(prefix) and note.endswith(suffix)
    evidence = state['proposals'][0]['evidence']
    assert evidence == {'api_key': '***', 'nested': {'access_token': ['***'], 'count': 42}}


def test_wal_database_refused_without_creating_sidecars(tmp_path, monkeypatch):
    root = tmp_path / "wal-state"
    root.mkdir()
    path = root / "state.db"
    db = DB(path)
    db.conn.execute("pragma journal_mode=WAL")
    db.close()
    assert not (root / "state.db-wal").exists()
    assert not (root / "state.db-shm").exists()
    monkeypatch.setenv("ION_HOME", str(root))
    monkeypatch.chdir(tmp_path)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    with pytest.raises(ValueError, match="WAL.*consistent SQLite backup"):
        with open_console():
            pytest.fail("WAL state was opened")
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}


def test_malformed_proposal_stays_visible_without_hiding_good_records(local):
    from ion import proposals
    good = proposals.create(local.db, "archive", "archive:good", {"repo": "alpha"}, {},
                            {"title": "Good record"}, .8, "saved-plan")
    bad = proposals.create(local.db, "archive", "archive:bad", {"repo": "beta"}, {},
                           {"title": "Malformed record"}, .8, "saved-plan")
    local.db.x("update proposals set state=? where id=?", ("{broken-json", bad["id"]))
    state = build_state(local, AT)
    dossiers = {p["id"]: p for p in state["proposals"]}
    assert dossiers[good["id"]]["title"] == "Good record"
    assert dossiers[bad["id"]]["title"] == "Malformed record"
    assert dossiers[bad["id"]]["approval"]["state"] == "unknown"
    assert "malformed" in dossiers[bad["id"]]["plan"]["error"].lower()
    assert checks(state)["stored-evidence"] == "fail"


@pytest.mark.parametrize("blob", ["{broken-json", "[]", "null"])
def test_malformed_analysis_fails_readiness_without_hiding_fleet(local, blob):
    gen, _ = save_snapshot(local)
    local.db.x("insert into analyses(ts,gen,blob) values(?,?,?)", (iso(AT), gen, blob))
    state = build_state(local, AT)
    assert state["summary"]["repositories"] == 1
    assert state["readiness"]["status"] == "attention"
    assert checks(state)["stored-evidence"] == "fail"
    assert state["analysis"]["config_matches"] is None
