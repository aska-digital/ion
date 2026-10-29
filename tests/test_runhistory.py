from argparse import Namespace

import pytest

from ion import cli
from ion.ghclient import GH, GHError
from ion.runhistory import Journal, freshness, recent_runs


def stub_pipeline(monkeypatch, collect=None):
    for name in ('collect', 'analyze', 'propose', 'sync', 'execute', 'report'):
        result = collect if name == 'collect' and collect is not None else {'errors': []}
        monkeypatch.setattr(cli, 'cmd_' + name, lambda *a, _r=result, **kw: _r)


def test_complete_run_and_freshness(ctx, monkeypatch):
    stub_pipeline(monkeypatch, {'run_id': 19})
    assert cli.cmd_run(ctx, Namespace(apply=False)) == 0
    run = recent_runs(ctx)[0]
    assert run['status'] == 'success' and run['mode'] == 'dry-run'
    assert run['collection_gen'] == 19 and len(run['config_sha']) == 64
    assert [s['name'] for s in run['steps']] == ['collect', 'analyze', 'propose', 'approvals', 'execute', 'report']
    assert all(s['duration_seconds'] >= 0 for s in run['steps'])
    assert freshness(ctx)['status'] == 'fresh'
    assert freshness(ctx, at='2099-01-01T00:00:00Z')['status'] == 'stale'


def test_partial_and_no_mirror_never_success(ctx, monkeypatch):
    stub_pipeline(monkeypatch, {'run_id': 1, 'partial': True, 'warnings': ['mirror failed']})
    assert cli.cmd_run(ctx, Namespace(apply=True)) == 1
    assert freshness(ctx)['status'] == 'never'
    ctx.sync_mirrors = False
    stub_pipeline(monkeypatch)
    assert cli.cmd_run(ctx, Namespace(apply=False)) == 1
    assert recent_runs(ctx)[0]['status'] != 'success'


def test_failure_skips_downstream_but_reports(ctx, monkeypatch):
    stub_pipeline(monkeypatch)
    def fail(*a):
        raise RuntimeError('token=secret-value')
    monkeypatch.setattr(cli, 'cmd_collect', fail)
    assert cli.cmd_run(ctx, Namespace(apply=False)) == 1
    run = recent_runs(ctx)[0]
    assert [s['status'] for s in run['steps']] == ['failed', 'skipped', 'skipped', 'skipped', 'skipped', 'success']
    assert 'secret-value' not in str(run)


def test_interrupt_and_abandoned_run(ctx, monkeypatch):
    stub_pipeline(monkeypatch)
    def interrupt(*a):
        raise KeyboardInterrupt()
    monkeypatch.setattr(cli, 'cmd_analyze', interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli.cmd_run(ctx, Namespace(apply=False))
    assert recent_runs(ctx)[0]['status'] == 'interrupted'
    Journal(ctx)
    assert recent_runs(ctx)[0]['status'] == 'running'
    assert recent_runs(ctx)[0]['finished'] is None
    assert freshness(ctx)['status'] == 'never'


def test_report_errors_fail_pipeline(ctx, monkeypatch):
    stub_pipeline(monkeypatch)
    monkeypatch.setattr(cli, 'cmd_report', lambda *a: {'errors': ['publish failed']})
    assert cli.cmd_run(ctx, Namespace(apply=False)) == 1
    assert recent_runs(ctx)[0]['steps'][-1]['status'] == 'failed'


def test_api_counters_include_failures():
    gh = GH(lambda *a: (1, '', 'failed'))
    with pytest.raises(GHError):
        gh.rest('GET', '/user')
    with pytest.raises(GHError):
        gh.graphql('query {}')
    assert gh.metrics == dict(rest_calls=1, graphql_calls=1, api_failures=2, graphql_points=0)
    gh = GH(lambda *a: (0, '{"data":{"rateLimit":{"cost":3}},"errors":[{"message":"partial"}]}', ''))
    gh.graphql('query {}')
    assert gh.metrics['graphql_points'] == 3 and gh.metrics['api_failures'] == 1


def test_report_captures_publish_and_notifier_errors(ctx, monkeypatch, tmp_path):
    from ion import notify, report
    from ion.report import orghealth
    monkeypatch.setattr(report, 'write_dashboard', lambda c: tmp_path / 'dashboard.md')
    def fail(*a):
        raise RuntimeError('password=secret-value')
    monkeypatch.setattr(orghealth, 'publish', fail)
    monkeypatch.setattr(notify, 'notify', lambda *a: ['notifier down'])
    result = cli.cmd_report(ctx, Namespace())
    assert len(result['errors']) == 2
    assert 'secret-value' not in str(result)


def test_digest_signature_changes_with_freshness_not_clock(ctx):
    from ion.report import build_digest, mark_digest_sent
    assert build_digest(ctx, {})[1]
    mark_digest_sent(ctx, {})
    assert not build_digest(ctx, {})[1]
    journal = Journal(ctx)
    with journal.step('collect'):
        pass
    journal.finish()
    assert build_digest(ctx, {})[1]
    mark_digest_sent(ctx, {})
    assert not build_digest(ctx, {})[1]
    ctx.db.x("update pipeline_runs set finished='2000-01-01T00:00:00Z'")
    assert build_digest(ctx, {})[1]


def test_legacy_read_only_database_has_no_history(tmp_path):
    from types import SimpleNamespace
    import sqlite3
    conn = sqlite3.connect(tmp_path / 'legacy.db')
    conn.row_factory = sqlite3.Row
    class ReadOnly:
        def q(self, sql, args=()):
            return conn.execute(sql, args).fetchall()
        def one(self, sql, args=()):
            return conn.execute(sql, args).fetchone()
    ctx = SimpleNamespace(db=ReadOnly())
    assert recent_runs(ctx) == []
    assert freshness(ctx)['status'] == 'never'


@pytest.mark.parametrize('text,secret', [
    ('https://user:plaintext@example.com/path', 'plaintext'),
    ('https://plaintext@example.com/path', 'plaintext'),
    ('Authorization: Bearer plaintext', 'plaintext'),
    ('Proxy-Authorization: Basic plaintext', 'plaintext'),
    ('Bearer plaintext', 'plaintext'),
    ('access_token=plaintext&repo=alpha', 'plaintext'),
    ('api_key: plaintext', 'plaintext'),
    ('"Authorization": "Bearer plaintext"', 'plaintext'),
    ('"password": "plain text secret"', 'plain text secret'),
    ("token='plain text secret'", 'plain text secret'),
])
def test_safe_text_redacts_credentials_without_truncation(text, secret):
    from ion.runhistory import safe_text
    prefix, suffix = 'evidence ' * 100, ' useful tail' * 100
    result = safe_text(prefix + text + suffix)
    assert secret not in result
    assert result.startswith(prefix) and result.endswith(suffix)
    assert '***' in result


def test_safe_text_preserves_ordinary_evidence():
    from ion.runhistory import safe_text
    text = 'Repository alpha has 42 commits; token count is 10. https://example.com/alpha?q=one&branch=main'
    assert safe_text(text) == text


def test_reports_during_run_explain_pending_outcome(ctx, monkeypatch):
    from ion.report import dashboard, build_digest
    from ion.report.orghealth import render_body
    from ion.runhistory import freshness_line
    stub_pipeline(monkeypatch)
    assert cli.cmd_run(ctx, Namespace(apply=False)) == 0
    previous = freshness(ctx)['last_success_at']
    journal = Journal(ctx)
    pending = 'Report captured while pipeline is running; this run outcome is pending'
    assert freshness(ctx)['last_success_at'] == previous
    assert pending in freshness_line(ctx)
    assert pending in dashboard(ctx)
    assert pending in build_digest(ctx, {})[0]
    assert pending in render_body(ctx, {})[0]
    journal.finish(['report failed'])
    assert pending not in freshness_line(ctx)
    assert freshness(ctx)['last_success_at'] == previous
    assert freshness(ctx)['latest_status'] == 'failed'


def test_digest_signature_preserves_failure_history_during_next_run(ctx):
    from ion.report import build_digest, mark_digest_sent
    prior = Journal(ctx)
    prior.finish()
    failed = Journal(ctx)
    mark_digest_sent(ctx, {})
    assert not build_digest(ctx, {})[1]
    failed.finish(['report failed'])
    Journal(ctx)
    assert build_digest(ctx, {})[1]
