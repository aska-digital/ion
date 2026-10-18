"""divan_bus approval source: `ion approve|reject|hold P-NNNN` from maintainer seats, signature-checked."""
from __future__ import annotations

import json
import os
import re
import sys

from . import core

MSG_RE = re.compile(r"^\s*ion\s+(approve|reject|hold)\s+(P-\d{4,})(?:\s+(?:plan-sha256:)?([0-9a-f]{8,64}))?\s*$", re.I | re.M)

VERIFY_SNIPPET = r'''
import hashlib, importlib.util, json, sys
client, url, seat = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("divan_client", client)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
envs = json.load(sys.stdin)
cli = m.Client(url, seat)
reg = cli.registry()
ok = []
for e in envs:
    pub = reg.get(e.get("from"), {}).get("pubkey")
    core = {k: v for k, v in e.items() if k not in ("seq", "chain")}
    rid = hashlib.sha256(m.canonical({k: v for k, v in core.items() if k not in ("id", "sig")})).hexdigest()
    if pub and rid == e.get("id") and m.verify_doc(core, pub):
        ok.append(e["id"])
print(json.dumps(ok))
'''


def _bus(cfg) -> dict:
    bus = cfg["bus"]
    if not bus.get("client"):
        raise RuntimeError("bus.client is not configured")
    return {"client": os.path.expanduser(bus["client"]),
            "python": os.path.expanduser(bus["python"]) if bus.get("python") else sys.executable,
            "env": {"DIVAN_URL": bus["url"], "DIVAN_SEAT": bus["seat"]}, "url": bus["url"], "seat": bus["seat"]}


def fetch_inbox(ctx, since: int) -> list[dict]:
    b = _bus(ctx.cfg)
    rc, out, err = ctx.bus_runner([b["python"], b["client"], "inbox", "--since", str(since), "--json"], b["env"], None)
    if rc != 0:
        raise RuntimeError(f"bus inbox failed: {err.strip()[:200]}")
    envs = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                envs.append(json.loads(line))
            except ValueError:
                pass
    return envs


def verified_ids(ctx, envs: list[dict]) -> set[str]:
    if not envs:
        return set()
    b = _bus(ctx.cfg)
    rc, out, err = ctx.bus_runner([b["python"], "-c", VERIFY_SNIPPET, b["client"], b["url"], b["seat"]], b["env"], json.dumps(envs))
    if rc != 0:
        return set()                                   # fail closed
    try:
        return set(json.loads(out.strip().splitlines()[-1]))
    except (ValueError, IndexError):
        return set()


def sync_bus(ctx) -> dict:
    from .. import proposals
    from .labels import mirror_label
    db, cfg = ctx.db, ctx.cfg
    res = {"seen": 0, "recorded": 0, "ignored": []}
    since = db.kv_get("bus_since", 0)
    envs = fetch_inbox(ctx, since)
    cands = [e for e in envs if not e.get("enc") and MSG_RE.search(e.get("body") or "")]
    vids = verified_ids(ctx, cands)
    seats = set(cfg["bus"]["maintainer_seats"])
    top = since
    for e in envs:
        top = max(top, int(e.get("seq") or 0))
    for e in cands:
        res["seen"] += 1
        for m in MSG_RE.finditer(e["body"]):
            action, pid, stated = m.group(1).lower(), m.group(2).upper(), (m.group(3) or "").lower()
            p = proposals.get(db, pid)
            if not p:
                continue
            sender = e.get("from", "")
            ok, reason = True, ""
            bound = None
            if e["id"] not in vids:
                ok, reason = False, "bus signature not verified"
            elif sender == cfg["bus"].get("seat"):
                ok, reason = False, "message from ion's own seat"
            elif sender not in seats:
                ok, reason = False, f"seat {sender} is not a maintainer seat"
            elif stated:
                # the sender names the plan hash it approved: it must be one the issue displayed before the message
                shown = [h for h in proposals.shown_before(p, e.get("ts", "")) if h.startswith(stated)]
                if shown:
                    bound = shown[-1]
                else:
                    ok, reason = False, f"plan hash {stated[:12]} was never displayed before this message"
            if core.record(db, pid, "divan_bus", sender, action, e.get("ts", ""), str(e["id"]), ok, reason, bound):
                res["recorded"] += 1
                if not ok:
                    res["ignored"].append((pid, sender, reason))
                elif p["status"] in ("awaiting-approval", "approved", "held"):
                    try:
                        mirror_label(ctx, p, action)
                    except Exception:  # noqa: BLE001
                        pass
    if top > since:
        db.kv_set("bus_since", top)
    return res
