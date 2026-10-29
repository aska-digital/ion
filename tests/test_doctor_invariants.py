import base64
from types import SimpleNamespace

import pytest

from ion import cli
from ion import doctor as diagnostic
from ion.ghclient import GHError
from ion.util import Refused


class LiveGH:
    def __init__(self):
        self.calls = []
        self.private = True
        self.admin = True
        self.override = None
        self.inventory_known = True
    def rest(self, method, path, body=None):
        assert method == "GET"
        self.calls.append(path)
        if path == "/repos/acme/ion-tracker": return {"private": self.private}
        if path == "/orgs/acme": return {"public_repos": 1, "total_private_repos": 0} if self.inventory_known else {}
        if "/contents/" in path:
            if self.override is not None:
                return {"encoding": "base64", "content": base64.b64encode(self.override.encode()).decode()}
            raise GHError("Not Found", status=404)
        return {"name": "alpha-core", "permissions": {"admin": self.admin}, "archived": False}
    def paginate(self, path): return [{"name": "alpha-core"}]


def setup(ctx, monkeypatch):
    ctx.gh = LiveGH()
    monkeypatch.setattr(diagnostic, "_filter_version", lambda: (True, "observed-version"))
    monkeypatch.setattr(diagnostic, "_networkx_status", lambda: (True, "test-networkx"))
    return ctx.gh


def checks(ctx): return {c["name"]: c for c in diagnostic.invariants(ctx)}


def test_private_tracker_is_required_and_unreadable_is_failure(ctx, monkeypatch):
    gh = setup(ctx, monkeypatch)
    assert checks(ctx)["private tracker"]["ok"]
    gh.private = False
    assert not checks(ctx)["private tracker"]["ok"]
    gh.rest = lambda *a: (_ for _ in ()).throw(GHError("403", 403))
    assert not checks(ctx)["private tracker"]["ok"]


def test_fix_level_governance_and_admin_are_checked_live(ctx, monkeypatch):
    gh = setup(ctx, monkeypatch)
    ctx.cfg["policy_actions"] = {"branch_protection": "fix"}
    gh.admin = False
    assert not checks(ctx)["policy admin rights"]["ok"]
    gh.override = "enabled: false\n"
    assert checks(ctx)["policy admin rights"]["ok"]
    gh.override = "policy_actions: {branch_protection: issue}\n"
    assert checks(ctx)["policy admin rights"]["ok"]
    gh.override = None
    gh.admin = True
    assert checks(ctx)["policy admin rights"]["ok"]
    assert "/repos/acme/alpha-core" in gh.calls
    gh.inventory_known = False
    assert not checks(ctx)["policy admin rights"]["ok"]


def test_optional_louvain_and_filter_version_pin(ctx, monkeypatch):
    setup(ctx, monkeypatch)
    monkeypatch.setattr(diagnostic, "_networkx_status", lambda: (False, "absent"))
    assert checks(ctx)["Louvain clustering"]["ok"]
    ctx.cfg["preflight"] = {"expect_networkx": True, "filter_repo_version": "another-version"}
    assert not checks(ctx)["Louvain clustering"]["ok"]
    assert not checks(ctx)["filter-repo version"]["ok"]
    ctx.cfg["preflight"]["filter_repo_version"] = "observed-version"
    assert checks(ctx)["filter-repo version"]["ok"]
    monkeypatch.setattr(diagnostic, "_filter_version", lambda: (False, None))
    assert not checks(ctx)["filter-repo version"]["ok"]
    ctx.cfg["disabled_executors"] = ["split", "move_code"]
    assert checks(ctx)["filter-repo version"]["ok"]
    from ion.execute.simple import MoveCodeExecutor
    with pytest.raises(Refused, match="disabled"): MoveCodeExecutor(ctx).apply({})


def test_apply_run_aborts_before_pipeline_and_tracker_writes(ctx, monkeypatch):
    gh = setup(ctx, monkeypatch)
    gh.private = False
    def forbidden(*args, **kwargs): raise AssertionError("pipeline must not start")
    for name in ["cmd_collect", "cmd_analyze", "cmd_propose", "cmd_sync", "cmd_execute", "cmd_report"]:
        monkeypatch.setattr(cli, name, forbidden)
    assert cli.cmd_run(ctx, SimpleNamespace(apply=True)) == 1
    assert not getattr(gh, "writes", [])


def test_version_probe_reports_exact_output_without_claiming_support(monkeypatch):
    monkeypatch.setattr(diagnostic, "filter_repo_path", lambda: "/fake/filter-repo")
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="a40bce548d2c\n")
    monkeypatch.setattr(diagnostic.subprocess, "run", run)
    assert diagnostic._filter_version() == (True, "a40bce548d2c")
    assert calls == [["/fake/filter-repo", "--version"]]


def test_successful_preflight_allows_pipeline(ctx, monkeypatch):
    setup(ctx, monkeypatch)
    calls = []
    for name in ["cmd_collect", "cmd_analyze", "cmd_propose", "cmd_sync", "cmd_execute", "cmd_report"]:
        monkeypatch.setattr(cli, name, lambda *a, _name=name, **kw: calls.append(_name) or {})
    assert cli.cmd_run(ctx, SimpleNamespace(apply=True)) == 0
    assert calls == ["cmd_collect", "cmd_analyze", "cmd_propose", "cmd_sync", "cmd_execute", "cmd_report"]
