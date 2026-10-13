"""Proposal rows <-> plain dicts."""
from __future__ import annotations

import json

from .util import iso


def view(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["state"] = json.loads(d.get("state") or "{}")
    d["subject"] = d["state"].get("subject", {})
    d["evidence"] = d["state"].get("evidence", {})
    d["text"] = d["state"].get("text", {})
    return d


def get(db, pid: str) -> dict | None:
    return view(db.get_proposal(pid))


def involved_repos(p: dict) -> list[str]:
    s = p["subject"]
    repos = [s[k] for k in ("repo", "source", "target") if s.get(k)]
    return list(dict.fromkeys(repos))


def create(db, kind: str, key: str, subject: dict, evidence: dict, text: dict, score: float, plan_hash: str,
           status: str = "awaiting-approval", parent: str | None = None) -> dict:
    pid, num = db.next_proposal_id()
    state = {"subject": subject, "evidence": evidence, "text": text}
    db.x("insert into proposals(id,num,kind,key,status,score,title,body,plan_hash,epoch,epoch_score,created,updated,state,parent)"
         " values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (pid, num, kind, key, status, score, text.get("title", key), "", plan_hash, iso(), score, iso(), iso(),
          json.dumps(state), parent))
    return get(db, pid)


def set_status(db, pid: str, status: str, **cols) -> None:
    sets = ["status=?", "updated=?"]
    args = [status, iso()]
    for k, v in cols.items():
        sets.append(f"{k}=?")
        args.append(v)
    db.x(f"update proposals set {', '.join(sets)} where id=?", (*args, pid))
