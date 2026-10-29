from datetime import datetime, timezone
from types import SimpleNamespace
from ion import cli, runrecord
from ion.db import DB


class Cfg(dict):
    def is_paused(self):
        return False


def context(tmp_path):
    return SimpleNamespace(db=DB(tmp_path/'test.db'), cfg=Cfg(report={'stale_after_hours': 24}),
                           gh=SimpleNamespace(graphql_points=0, rest_calls=0))


def setup_steps(monkeypatch, ctx, partial=False, failure=False):
    def collect(*args):
        ctx.gh.graphql_points += 3
        if failure:
            raise RuntimeError('read failed')
        return {'partial': partial}
    monkeypatch.setattr(cli, 'cmd_collect', collect)
    monkeypatch.setattr(cli, 'cmd_analyze', lambda *args: {})
    monkeypatch.setattr(cli, 'cmd_propose', lambda *args: {})
    monkeypatch.setattr(cli, 'cmd_sync', lambda *args: {})
    def execute(*args, **kwargs):
        ctx.gh.rest_calls += 2
        ctx.db.x("insert into write_log(type,ok) values('open_pr',1)")
        return {'errors': []}
    monkeypatch.setattr(cli, 'cmd_execute', execute)
    monkeypatch.setattr(cli, 'cmd_report', lambda *args: {})


def test_complete_run_records_steps_metrics_and_success(tmp_path, monkeypatch):
    ctx=context(tmp_path); setup_steps(monkeypatch,ctx)
    assert cli.cmd_run(ctx,SimpleNamespace(apply=False)) == 0
    rows=ctx.db.q('select * from pipeline_steps order by rowid')
    assert [r['name'] for r in rows] == ['collect','analyze','propose','approvals','execute']
    assert rows[0]['graphql_points']==3
    assert rows[-1]['rest_calls']==2 and rows[-1]['writes_submitted']==1
    assert all(r['duration_seconds']>=0 for r in rows)
    assert runrecord.health(ctx)['last_success']


def test_partial_and_failed_collection_do_not_claim_success(tmp_path, monkeypatch):
    ctx=context(tmp_path); setup_steps(monkeypatch,ctx,partial=True)
    assert cli.cmd_run(ctx,SimpleNamespace(apply=False)) == 1
    assert runrecord.health(ctx)['last_success'] is None
    setup_steps(monkeypatch,ctx,failure=True)
    assert cli.cmd_run(ctx,SimpleNamespace(apply=False)) == 1
    assert len(ctx.db.q('select * from pipeline_steps where run_id=2')) == 1
    assert 'read failed' in ctx.db.one('select errors from pipeline_steps where run_id=2')['errors']


def test_staleness_and_unknown_metrics(tmp_path):
    ctx=context(tmp_path); ctx.gh=object()
    n=runrecord.start(ctx.db)
    before=runrecord.counters(ctx)
    runrecord.record(ctx,n,'collect',1,before,[])
    row=ctx.db.one('select * from pipeline_steps')
    assert row['graphql_points'] is None and row['rest_calls'] is None
    ctx.db.x("update pipeline_runs set finished='2026-01-01T00:00:00Z',success=1")
    assert runrecord.health(ctx,datetime(2026,1,3,tzinfo=timezone.utc))['stale']


def test_failed_graphql_transport_does_not_report_zero_cost():
    from ion.ghclient import GH, GHError
    import pytest
    gh = GH(runner=lambda *args: (1, "", "unavailable"))
    with pytest.raises(GHError):
        gh.graphql("query { viewer { login } }")
    assert gh.graphql_points is None


def test_transport_exception_leaves_api_request_metrics_unknown():
    from ion.ghclient import GH
    import pytest
    def interrupted(*args):
        raise TimeoutError("request timed out")
    gh = GH(runner=interrupted)
    with pytest.raises(TimeoutError):
        gh.graphql("query { viewer { login } }")
    assert gh.graphql_points is None
    with pytest.raises(TimeoutError):
        gh.rest("GET", "/user")
    assert gh.rest_calls is None
    gh.rest_calls = 0
    with pytest.raises(TimeoutError):
        gh.paginate("/repos/acme/x/issues")
    assert gh.rest_calls is None
