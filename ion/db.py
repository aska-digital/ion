"""SQLite state."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .util import home, iso

SCHEMA = """
create table if not exists runs(id integer primary key autoincrement, started text, finished text, cost integer default 0,
  remaining integer, partial integer default 0, repos integer default 0, note text);
create table if not exists snapshots(run_id integer, repo text, collected_at text, pushed_at text, private integer,
  archived integer, default_branch text, size integer, blob text, primary key(run_id, repo));
create table if not exists analyses(id integer primary key autoincrement, ts text, gen integer, blob text);
create table if not exists candidates(key text primary key, kind text, subject text, first_gen integer, last_gen integer,
  streak integer default 1, score real, evidence text, status text default 'pending', suppressed_until text,
  suppressed_score real, updated text);
create table if not exists proposals(id text primary key, num integer unique, kind text, key text, status text,
  score real, title text, body text, plan_hash text, epoch text, epoch_score real, created text, updated text,
  issue_number integer, approved_at text, state text default '{}', parent text, rejected_at text, rejected_score real);
create table if not exists approvals(id integer primary key autoincrement, proposal text, source text, actor text,
  action text, event_ts text, ref text, valid integer, reason text, recorded text, unique(proposal, source, ref));
create table if not exists mirrored_labels(proposal text, label text, event_id text, primary key(proposal, label, event_id));
create table if not exists ledger(id integer primary key autoincrement, proposal text, step text, phase text, ts text, detail text);
create table if not exists outbox(id integer primary key autoincrement, ts text, to_seat text, body text, sent integer default 0);
create table if not exists kv(k text primary key, v text);
create table if not exists write_log(id integer primary key autoincrement, ts text, type text, repo text, target text,
  proposal text, ok integer, detail text);
"""

MIGRATIONS = [("approvals", "plan_hash", "text")]


class DB:
    def __init__(self, path: str | Path | None = None):
        self.path = str(path or (home() / "state.db"))
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"pragma table_info({table})")}
            if col not in cols:
                self.conn.execute(f"alter table {table} add column {col} {typ}")
        self.conn.commit()

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()):
        r = self.conn.execute(sql, args).fetchone()
        return r

    def x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    def kv_get(self, k: str, default=None):
        r = self.one("select v from kv where k=?", (k,))
        return json.loads(r["v"]) if r else default

    def kv_set(self, k: str, v) -> None:
        self.x("insert into kv(k,v) values(?,?) on conflict(k) do update set v=excluded.v", (k, json.dumps(v)))

    # ledger -------------------------------------------------------------------
    def ledger_add(self, proposal: str, step: str, phase: str, detail: str = "") -> None:
        self.x("insert into ledger(proposal,step,phase,ts,detail) values(?,?,?,?,?)", (proposal, step, phase, iso(), detail[:4000]))

    def ledger_done(self, proposal: str, step: str) -> bool:
        return self.one("select 1 from ledger where proposal=? and step=? and phase='after'", (proposal, step)) is not None

    def ledger_detail(self, proposal: str, step: str) -> str | None:
        r = self.one("select detail from ledger where proposal=? and step=? and phase='after' order by id desc", (proposal, step))
        return r["detail"] if r else None

    # proposals ------------------------------------------------------------------
    def next_proposal_id(self) -> tuple[str, int]:
        r = self.one("select coalesce(max(num),0)+1 as n from proposals")
        n = r["n"]
        return f"P-{n:04d}", n

    def get_proposal(self, pid: str):
        return self.one("select * from proposals where id=?", (pid,))

    def set_state(self, pid: str, **updates) -> dict:
        row = self.get_proposal(pid)
        st = json.loads(row["state"] or "{}")
        st.update(updates)
        self.x("update proposals set state=?, updated=? where id=?", (json.dumps(st), iso(), pid))
        return st

    def close(self) -> None:
        self.conn.close()
