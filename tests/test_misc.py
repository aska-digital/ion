import json
import os
import sys

import pytest

from conftest import FakeBus, make_ctx
from helpers import MERGE, new_proposal
from mimar import cli, notify, report, tracker
from mimar.analyze import run_analyze
from mimar.collect import run_collect
from mimar.config import ConfigError, load_config
from mimar.propose.engine import run_propose


# ---- config ------------------------------------------------------------------------------

def test_shipped_configs_validate():
    here = os.path.dirname(os.path.dirname(__file__))
    ex = load_config(os.path.join(here, "mimar.example.yml"))
    assert ex["org"] == "your-org" and ex["auto_apply"] == []


def test_defaults_are_generic_and_safe():
    c = load_config(None, {"org": "o"})
    assert c["auto_apply"] == [] and c["llm"]["backend"] == "none" and c["bus"]["client"] is None
    assert c["approval_sources"] == ["github_label"] and c["tracker_repo"] == "mimar-tracker"


def test_invalid_config_rejected():
    with pytest.raises(ConfigError):
        load_config(None, {"org": "o", "llm": {"backend": "gpt"}})
    with pytest.raises(ConfigError):
        load_config(None, {"org": "o", "auto_apply": ["delete_repos"]})
    with pytest.raises(ConfigError):
        load_config(None, {"org": "o", "notifiers": [{"type": "pager"}]})
    with pytest.raises(ConfigError):   # a typo here used to crash mid-run in lowers_protection
        load_config(None, {"org": "o", "policy": {"branch_protection": {"required_approvals": "many"}}})
    with pytest.raises(ConfigError):
        load_config(None, {"org": "o", "policy": {"branch_protection": {"require_pr": "yes"}}})
    with pytest.raises(ConfigError):
        load_config(None, {"org": None}).org


def test_no_org_specific_paths_in_code():
    root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mimar")
    for dp, _, fs in os.walk(root):
        for f in fs:
            if f.endswith(".py"):
                t = open(os.path.join(dp, f)).read()
                for banned in ("divan-bus", "aska-digital", "saga-divan", "ahrazzle", "/Users/"):
                    assert banned not in t, f"{banned} in {f}"


# ---- notifiers / digest ----------------------------------------------------------------------

def test_notifier_plugins(ctx, tmp_path, capsys):
    ctx.cfg["notifiers"] = [{"type": "stdout"}, {"type": "file", "path": str(tmp_path / "d.log")},
                            {"type": "divan_bus", "client": "/c/divan.py", "to": "seat-x"}, {"type": "github_issue", "number": 7}]
    ctx.gh.issues[7] = {"body": ""}
    assert notify.notify(ctx, "hello") == []
    assert "hello" in capsys.readouterr().out and "hello" in (tmp_path / "d.log").read_text()
    assert ctx.bus_runner.posts[0][-3:] == ["--to", "seat-x", "hello"] or ctx.bus_runner.posts[0][-1] == "hello"
    assert ctx.gh.writes_to("PATCH", "/issues/7")


def test_webhook_payload_is_slack_and_discord_compatible(ctx, monkeypatch):
    sent = {}

    class R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass
    def fake(req, timeout=None):
        sent["body"] = json.loads(req.data); sent["url"] = req.full_url
        return R()
    monkeypatch.setattr("mimar.notify.urllib.request.urlopen", fake)
    monkeypatch.setenv("HOOK", "https://hooks.example/x")
    ctx.cfg["notifiers"] = [{"type": "webhook", "url_env": "HOOK"}]
    assert notify.notify(ctx, "digest") == []
    assert sent["body"] == {"text": "digest", "content": "digest"} and sent["url"] == "https://hooks.example/x"


def test_failed_notifier_queues_and_retries(tmp_path, home, world):
    bus = FakeBus(); bus.down = True
    ctx = make_ctx(tmp_path, world, {"notifiers": [{"type": "divan_bus", "client": "/c/divan.py", "to": "s"}]}, bus=bus)
    errs = notify.notify(ctx, "digest one")
    assert errs and ctx.db.one("select count(*) n from outbox where sent=0")["n"] == 1
    bus.down = False
    assert notify.notify(ctx, "digest two") == []
    assert ctx.db.one("select count(*) n from outbox where sent=0")["n"] == 0
    assert [p[-1] for p in bus.posts] == ["digest one", "digest two"]


def test_digest_only_when_something_changed(ctx):
    run_collect(ctx); run_analyze(ctx)
    res = {"propose": run_propose(ctx)}
    text, changed = report.build_digest(ctx, res)
    assert changed and "new P-" in text
    report.mark_digest_sent(ctx, res)
    _, changed2 = report.build_digest(ctx, {})
    assert not changed2


def test_dashboard_markdown(ctx):
    run_collect(ctx); run_analyze(ctx)
    md = report.dashboard(ctx)
    assert "# Org health: acme" in md and "alpha-cli <> alpha-core" in md and "old-tool" in md and "blocked by plan" in md.lower()


# ---- tracker ------------------------------------------------------------------------------------

def test_issue_body_updated_in_place_never_commented(ctx):
    run_collect(ctx)
    p = new_proposal(ctx, "merge", MERGE)
    n = p["issue_number"]
    first = ctx.gh.issues[n]["body"]
    from mimar.propose.engine import refresh_issue
    refresh_issue(ctx, p)
    assert ctx.gh.writes_to("PATCH", f"/issues/{n}") and ctx.gh.issues[n]["body"] != ""
    assert not [w for w in ctx.gh.writes if "comments" in w[1]]
    assert ctx.gh.issues[n]["title"].startswith(f"[{p['id']}]")
    assert "Only maintainers count" in first


def test_paused_blocks_issue_writes(ctx, home):
    run_collect(ctx)
    (home / "PAUSED").write_text("x")
    before = len(ctx.gh.writes)
    new_proposal(ctx, "merge", MERGE)
    assert len(ctx.gh.writes) == before


# ---- cli ----------------------------------------------------------------------------------------

def test_pause_resume_cli(home, capsys):
    assert cli.main(["pause"]) == 0 and (home / "PAUSED").exists()
    assert cli.main(["resume"]) == 0 and not (home / "PAUSED").exists()


def test_status_and_explain(ctx, capsys, monkeypatch):
    run_collect(ctx)
    p = new_proposal(ctx, "merge", MERGE)
    cli.cmd_status(ctx, None)
    out = capsys.readouterr().out
    assert "kill switch: off" in out and p["id"] in out
    class A: proposal = p["id"]
    assert cli.cmd_explain(ctx, A) == 0
    out = capsys.readouterr().out
    assert "git subtree add" in out and "approval: awaiting" in out and "rollback" in out


def test_execute_cli_default_is_dry_run(ctx, capsys):
    from helpers import approve
    run_collect(ctx)
    p = approve(ctx, new_proposal(ctx, "archive", {"repo": "old-tool"}))
    class A: apply = False; dry_run = False; proposal = None
    cli.cmd_execute(ctx, A)
    assert "old-tool" not in ctx.gh.archived and "dry-run" in capsys.readouterr().out


def test_run_pipeline_dry_by_default(ctx, capsys):
    class A: apply = False; no_mirror = False; dry_run = False; proposal = None
    rc = cli.cmd_run(ctx, A)
    out = capsys.readouterr().out
    assert rc == 0 and "dashboard:" in out
    assert not ctx.gh.archived and not ctx.gh.writes_to("POST", "/pulls")


def test_doctor_reports_checks(ctx):
    from mimar.doctor import doctor
    checks = {c["name"]: c for c in doctor(ctx)}
    assert checks["git"]["ok"] and checks["git-filter-repo"]["ok"] and checks["llm backend"]["ok"]
    assert checks["kill switch"]["detail"].startswith("off")
    ctx.cfg["approval_sources"] = ["divan_bus"]
    ctx.cfg["bus"]["client"] = "/nonexistent/divan.py"
    assert not {c["name"]: c for c in doctor(ctx)}["bus"]["ok"]


def test_doctor_checks_the_bus_seat_key_without_creating_it(ctx, tmp_path):
    from mimar.doctor import doctor
    client = tmp_path / "divan.py"
    client.write_text("# stub")
    ctx.cfg["approval_sources"] = ["divan_bus"]
    ctx.cfg["bus"].update(client=str(client), python="python3", seat="mimar", key=str(tmp_path / "mimar.key"))
    checks = {c["name"]: c for c in doctor(ctx)}
    assert checks["bus"]["ok"] and not checks["bus seat key"]["ok"] and "MISSING" in checks["bus seat key"]["detail"]
    assert not (tmp_path / "mimar.key").exists()                                    # doctor never creates a key
    (tmp_path / "mimar.key").write_text("k")
    assert {c["name"]: c for c in doctor(ctx)}["bus seat key"]["ok"]


def test_tokens_are_redacted():
    from mimar.util import redact
    assert "gho_" not in redact("auth failed for gho_" + "a" * 36) and "sk-ant" not in redact("key sk-ant-" + "b" * 30)
