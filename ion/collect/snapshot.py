"""One row per repo per run, so trends can be tracked."""
from __future__ import annotations

import json

from ..util import iso


def start_run(db) -> int:
    return db.x("insert into runs(started) values(?)", (iso(),)).lastrowid


def finish_run(db, run_id: int, cost: int, remaining, partial: bool, repos: int, note: str = "") -> None:
    db.x("update runs set finished=?, cost=?, remaining=?, partial=?, repos=?, note=? where id=?",
         (iso(), cost, remaining, int(partial), repos, note, run_id))


def write_snapshot(db, run_id: int, snap: dict) -> None:
    db.x("insert or replace into snapshots(run_id,repo,collected_at,pushed_at,private,archived,default_branch,size,blob)"
         " values(?,?,?,?,?,?,?,?,?)",
         (run_id, snap["name"], iso(), snap.get("pushed_at"), int(snap["private"]), int(snap["archived"]),
          snap.get("default_branch"), snap.get("size"), json.dumps(snap)))


def latest_snapshots(db) -> tuple[dict[str, dict], int]:
    """The org as the newest complete collect saw it, plus that run's id (the 'generation').

    Scoped to one run on purpose: the newest row per repo name would keep a renamed or
    deleted repo's last snapshot alive for ever, generating findings for a repo that is
    no longer there under that name. A partial or unfinished run never becomes the
    generation, so a budget-stopped collect cannot shrink the org view or advance streaks.
    """
    row = db.one("select id from runs where finished is not null and coalesce(partial, 1) = 0 "
                 "and exists (select 1 from snapshots where run_id = runs.id) order by id desc limit 1")
    if row is None:
        row = db.one("select max(run_id) as id from snapshots")
    gen = row["id"] if row and row["id"] is not None else 0
    if not gen:
        return {}, 0
    rows = db.q("select repo, blob from snapshots where run_id = ?", (gen,))
    return {r["repo"]: json.loads(r["blob"]) for r in rows}, gen


def history(db, repo: str) -> list[dict]:
    return [json.loads(r["blob"]) for r in db.q("select blob from snapshots where repo=? order by run_id", (repo,))]
