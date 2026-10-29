"""Durable pipeline journal, independent of collection generations."""
from __future__ import annotations

import json
import re
import time
import sqlite3
from contextlib import contextmanager

from .util import iso, now, parse_ts, redact


def safe_text(value) -> str:
    """Scrub credential syntax without truncating the surrounding saved evidence."""
    text = redact(str(value))
    # A URL may contain just a username/token, or a username and password.
    text = re.sub(r"(?i)(https?://)[^\s/@]+@", r"\1***@", text)
    # Consume the scheme together with the credential so Basic/Bearer payloads
    # cannot remain behind after redacting only the scheme name.
    text = re.sub(r"(?i)(\b(?:authorization|proxy-authorization)\s*[:=]\s*)(?:(?:basic|bearer|token)\s+)?[^\s,;\"'<>]+",
                  r"\1***", text)
    text = re.sub(r"(?i)(\bbearer\s+)[^\s,;\"'<>]+", r"\1***", text)
    field = r"(?:authorization|proxy[_-]?authorization|password|passwd|secret|client[_-]?secret|api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|token)"
    # Quoted forms also cover JSON, YAML and credentials containing spaces.
    text = re.sub(r"(?i)([\"']?\b" + field + r"[\"']?\s*[:=]\s*)([\"'])(.*?)\2",
                  lambda m: m[1] + m[2] + "***" + m[2], text)
    text = re.sub(r"(?i)(\b" + field + r"\s*[:=]\s*)[^\s,;\"'&<>]+", r"\1***", text)
    return text


def clean_error(value) -> str:
    return safe_text(value)[:500]


def _metrics(ctx):
    metrics = dict(getattr(ctx.gh, 'metrics', {}))
    row = ctx.db.one('select coalesce(max(id),0) id from write_log')
    metrics['write_log_id'] = row['id']
    return metrics


def _delta(ctx, before):
    after = _metrics(ctx)
    result = {k: v - before.get(k, 0) for k, v in after.items() if k != 'write_log_id'}
    rows = ctx.db.q('select ok, count(*) n from write_log where id>? group by ok', (before['write_log_id'],))
    result.update(writes_succeeded=0, writes_failed=0)
    for row in rows:
        result['writes_succeeded' if row['ok'] else 'writes_failed'] = row['n']
    return result


class Journal:
    def __init__(self, ctx, apply=False):
        self.ctx = ctx
        self.start = time.monotonic()
        self.before = _metrics(ctx)
        # Only the digest is persisted; no config values or credentials enter the journal.
        from .config import config_fingerprint
        sha = config_fingerprint(ctx.cfg)
        self.id = ctx.db.x('insert into pipeline_runs(started,status,execution_mode,config_sha) values(?,?,?,?)',
                           (iso(), 'running', 'apply' if apply else 'dry-run', sha)).lastrowid
        self.statuses = []

    @contextmanager
    def step(self, name):
        sid = self.ctx.db.x('insert into pipeline_steps(run_id,name,started,status) values(?,?,?,?)',
                            (self.id, name, iso(), 'running')).lastrowid
        start, before = time.monotonic(), _metrics(self.ctx)
        state = {'status': 'success', 'errors': []}
        try:
            yield state
        except BaseException as exc:
            state.update(status='interrupted' if not isinstance(exc, Exception) else 'failed', errors=[clean_error(exc)])
            raise
        finally:
            metrics = _delta(self.ctx, before)
            if metrics.get('api_failures', 0) or metrics['writes_failed']:
                if state['status'] == 'success':
                    state['status'] = 'warning'
            self.statuses.append(state['status'])
            self.ctx.db.x('update pipeline_steps set finished=?,duration_seconds=?,status=?,metrics=?,errors=? where id=?',
                          (iso(), time.monotonic()-start, state['status'], json.dumps(metrics),
                           json.dumps([clean_error(e) for e in state['errors']]), sid))

    def skip(self, name, reason):
        self.statuses.append('skipped')
        self.ctx.db.x('insert into pipeline_steps(run_id,name,status,errors) values(?,?,?,?)',
                      (self.id, name, 'skipped', json.dumps([clean_error(reason)])))

    def finish(self, errors=(), interrupted=False):
        status = ('interrupted' if interrupted else 'failed' if errors or 'failed' in self.statuses else
                  'warning' if any(s != 'success' for s in self.statuses) else 'success')
        metrics = _delta(self.ctx, self.before)
        if status == 'success' and (metrics.get('api_failures', 0) or metrics['writes_failed']):
            status = 'warning'
        self.ctx.db.x('update pipeline_runs set finished=?,duration_seconds=?,status=?,metrics=?,errors=? where id=?',
                      (iso(), time.monotonic()-self.start, status, json.dumps(metrics),
                       json.dumps([clean_error(e) for e in errors]), self.id))

    def collection(self, result):
        self.ctx.db.x('update pipeline_runs set collection_gen=? where id=?', (result.get('run_id'), self.id))


def recent_runs(ctx, limit=20):
    try:
        rows = [dict(r) for r in ctx.db.q('select * from pipeline_runs order by id desc limit ?', (max(0, int(limit)),))]
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        return []
    for row in rows:
        row['mode'] = row['execution_mode']
        row['steps'] = [dict(s) for s in ctx.db.q('select * from pipeline_steps where run_id=? order by id', (row['id'],))]
        for item in [row] + row['steps']:
            for key in ('metrics', 'errors'):
                item[key] = json.loads(item[key])
    return rows


def freshness(ctx, at=None, stale_after_hours=48):
    at = parse_ts(at) if isinstance(at, str) else at
    try:
        latest = ctx.db.one('select status from pipeline_runs order by id desc limit 1')
        completed = ctx.db.one("select status from pipeline_runs where finished is not null order by id desc limit 1")
        success = ctx.db.one("select finished from pipeline_runs where status='success' and finished is not null order by id desc limit 1")
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        latest = success = completed = None
    age = max(0, ((at or now()) - parse_ts(success['finished'])).total_seconds()/3600) if success else None
    status = 'never' if not success else 'stale' if age >= stale_after_hours else 'fresh'
    return dict(status=status, last_success_at=success['finished'] if success else None, age_hours=age,
                stale_after_hours=stale_after_hours, latest_status=latest['status'] if latest else None,
                last_completed_status=completed['status'] if completed else None,
                reason='No complete successful pipeline run' if not success else
                       'Last complete pipeline run exceeds freshness limit' if status == 'stale' else 'Complete pipeline run within freshness limit')


def freshness_line(ctx):
    f = freshness(ctx, stale_after_hours=ctx.cfg.get('report', {}).get('stale_after_hours', 48))
    line = f"Pipeline freshness: **{f['status']}** · last complete success: {f['last_success_at'] or 'never'} · latest: {f['latest_status'] or 'none'} · limit: {f['stale_after_hours']}h"

    if f['latest_status'] == 'running':
        line += " · Report captured while pipeline is running; this run outcome is pending"
    return line
