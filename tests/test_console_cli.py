"""CLI exports are private local artifacts, with no operator-state mutations."""
import json
import socket
import stat
import subprocess

import pytest

from ion import cli, console_state
from ion.db import DB


@pytest.fixture
def offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "operator"
    monkeypatch.setenv("ION_HOME", str(root))
    def forbidden(*args, **kwargs):
        pytest.fail("offline CLI invoked a live context, network or subprocess")
    monkeypatch.setattr(cli, "make_ctx", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    return root


@pytest.mark.parametrize("as_json", [True, False])
def test_demo_ignores_even_invalid_explicit_config_and_state(offline, tmp_path, monkeypatch, capsys, as_json):
    def forbidden(*args, **kwargs):
        pytest.fail("demo read configuration or state")
    monkeypatch.setattr(console_state, "open_console", forbidden)
    monkeypatch.setattr(console_state, "state_home", forbidden)
    monkeypatch.setattr(console_state, "load_config", forbidden)
    output = tmp_path / "tour.html"
    args = ["console", "--demo", "--config", str(tmp_path / "missing.yml")]
    args += ["--json"] if as_json else ["--output", str(output)]
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert not captured.err
    if as_json:
        assert json.loads(captured.out)["demo"] is True
    else:
        assert "<!doctype html>" in output.read_text().lower()
    assert not offline.exists()


def test_empty_json_does_not_create_operator_home(offline, capsys):
    assert cli.main(["console", "--json"]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    assert json.loads(captured.out)["readiness"]["status"] == "empty"
    assert not offline.exists()


def test_saved_state_export_preserves_every_file(offline, tmp_path, capsys):
    offline.mkdir()
    (offline / "ion.yml").write_text("org: acme\nmaintainers: [maint]\n")
    db = DB(offline / "state.db")
    db.kv_set("last_breaker_trip", {"reason": "saved failure", "ts": "2026-10-29T00:00:00Z"})
    db.close()
    before = {p.relative_to(offline): p.read_bytes() for p in offline.rglob("*") if p.is_file()}
    assert cli.main(["console", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["safety"]["breaker"]["reason"] == "saved failure"
    assert before == {p.relative_to(offline): p.read_bytes() for p in offline.rglob("*") if p.is_file()}


@pytest.mark.parametrize("as_json", [True, False])
def test_new_exports_private_and_never_overwrite(offline, tmp_path, capsys, as_json):
    output = tmp_path / ("export.json" if as_json else "export.html")
    args = ["console", "--demo", "--output", str(output)] + (["--json"] if as_json else [])
    assert cli.main(args) == 0
    original = output.read_bytes()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert cli.main(args) == 1
    assert output.read_bytes() == original
    assert "offline export failed" in capsys.readouterr().err


def test_symlink_export_refused_without_touching_target(offline, tmp_path, capsys):
    target = tmp_path / "precious"
    target.write_text("keep me")
    link = tmp_path / "output"
    link.symlink_to(target)
    assert cli.main(["console", "--demo", "--output", str(link)]) == 1
    assert target.read_text() == "keep me"
    assert link.is_symlink()
    assert "offline export failed" in capsys.readouterr().err


def test_bad_database_has_clear_nonzero_result(offline, capsys):
    offline.mkdir()
    (offline / "state.db").write_text("this is not sqlite")
    assert cli.main(["console", "--json"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "offline export failed" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("legacy", [True, False])
def test_runs_legacy_or_missing_state_offline(offline, capsys, legacy):
    if legacy:
        offline.mkdir()
        db = DB(offline / "state.db")
        db.x("drop table pipeline_steps")
        db.x("drop table pipeline_runs")
        db.close()
    assert cli.main(["runs", "--json", "--limit", "3"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == []
    assert not captured.err
    if not legacy:
        assert not offline.exists()
