"""Proposal rows <-> plain dicts."""
from __future__ import annotations

import json

from .util import iso, parse_ts


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
    repos = [s[k] for k in ("repo", "source", "target") if s.get(k)] + list(s.get("repos") or [])
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


# ---- plan-hash binding ------------------------------------------------------------------------
# `hash_log` is the list of [timestamp, plan_hash] pairs for what the tracker issue actually showed. An approval is
# bound to the hash that was on display just before the approval event, so nobody can approve plan A and have plan B run.

def shown_before(p: dict, event_ts: str | None) -> list[str]:
    """Plan hashes the issue displayed strictly before `event_ts`, oldest first."""
    t = parse_ts(event_ts)
    if t is None:
        return []
    return [h for ts, h in p["state"].get("hash_log", []) if (parse_ts(ts) or t) < t]


def hash_at(p: dict, event_ts: str | None) -> str | None:
    """The plan hash that was on display just before `event_ts` (None if none was displayed yet)."""
    shown = shown_before(p, event_ts)
    return shown[-1] if shown else None


def note_shown(db, pid: str, plan_hash: str) -> bool:
    """Record that the issue now shows `plan_hash`. Returns True if it is a change from what was shown before."""
    row = db.get_proposal(pid)
    st = json.loads(row["state"] or "{}")
    log = st.get("hash_log", [])
    if log and log[-1][1] == plan_hash:
        return False
    log.append([iso(), plan_hash])
    db.set_state(pid, hash_log=log[-20:])
    return True
