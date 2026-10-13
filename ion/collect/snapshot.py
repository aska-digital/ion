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
    """Latest snapshot per repo, plus the newest run id (the 'generation')."""
    rows = db.q("select s.repo, s.blob, s.run_id from snapshots s join "
                "(select repo, max(run_id) m from snapshots group by repo) x on s.repo=x.repo and s.run_id=x.m")
    gen = max((r["run_id"] for r in rows), default=0)
    return {r["repo"]: json.loads(r["blob"]) for r in rows}, gen


def history(db, repo: str) -> list[dict]:
    return [json.loads(r["blob"]) for r in db.q("select blob from snapshots where repo=? order by run_id", (repo,))]
