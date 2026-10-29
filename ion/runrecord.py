"""Durable records for the five pipeline stages, with explicit unknown counters."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from .util import iso, redact


def ensure_schema(db):
    db.x('create table if not exists pipeline_runs(id integer primary key, started text, finished text, success integer)')
    db.x('''create table if not exists pipeline_steps(run_id integer, name text, duration_seconds real,
         graphql_points integer, rest_calls integer, writes_submitted integer, errors text,
         primary key(run_id,name))''')


def start(db):
    ensure_schema(db)
    return db.x('insert into pipeline_runs(started) values(?)', (iso(),)).lastrowid


def counters(ctx):
    return (getattr(ctx.gh, 'graphql_points', None), getattr(ctx.gh, 'rest_calls', None),
            ctx.db.one('select count(*) as n from write_log')['n'])


def record(ctx, run_id, name, duration, before, errors):
    after = counters(ctx)
    delta = [b-a if a is not None and b is not None else None for a,b in zip(before, after)]
    ctx.db.x('insert into pipeline_steps values(?,?,?,?,?,?,?)',
             (run_id, name, duration, *delta, json.dumps([redact(str(e)) for e in errors])))


def finish(db, run_id, success):
    db.x('update pipeline_runs set finished=?,success=? where id=?', (iso(), int(success), run_id))


def health(ctx, now=None):
    ensure_schema(ctx.db)
    row = ctx.db.one('select finished from pipeline_runs where success=1 order by id desc limit 1')
    limit = ctx.cfg.get('report', {}).get('stale_after_hours', 48)
    if not row:
        return {'last_success': None, 'stale': True, 'detail': 'No successful full pipeline run recorded.'}
    timestamp = row['finished']
    age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(timestamp.replace('Z', '+00:00'))).total_seconds()/3600
    stale = age > limit
    return {'last_success': timestamp, 'stale': stale,
            'detail': f'Last successful pipeline: {timestamp}.' + (f' WARNING: older than {limit} hours.' if stale else '')}
