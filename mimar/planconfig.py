"""`mimar plan-config`: what would this config change do, before it is used?

The config is the most powerful input Mimar has: a typo can turn every repo into a policy drift or every pair into a
merge candidate. This runs the analysis and the candidate gathering under the current config and under the new one,
over the same stored snapshots and mirrors, and reports the difference. It reads the local database and mirrors only;
it writes nothing (no analysis row, no candidate row, nothing on GitHub).
"""
from __future__ import annotations

import dataclasses

from .analyze import compute_analysis
from .collect.snapshot import latest_snapshots
from .propose.engine import gather_candidates, plan_for
from . import proposals


def flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and v:
            out.update(flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def config_diff(old: dict, new: dict) -> list[dict]:
    fo, fn = flatten(old), flatten(new)
    return [{"key": k, "old": fo.get(k), "new": fn.get(k)} for k in sorted(set(fo) | set(fn)) if fo.get(k) != fn.get(k)]


def _policy_view(a: dict) -> dict:
    return {(repo, f["class"]): (f["status"], f.get("action")) for repo, fs in a["policy"].items() for f in fs}


def _cands(ctx, a: dict, snaps: dict) -> dict:
    out = {}
    for c in gather_candidates(ctx, a, snaps):
        out[c["key"]] = c
    return out


def plan_config(ctx, new_cfg) -> dict:
    """Compare the current context's config with `new_cfg`. Returns a plain dict (see `render`)."""
    snaps, _ = latest_snapshots(ctx.db)
    cache: dict = {}
    new_ctx = dataclasses.replace(ctx, cfg=new_cfg)
    a_old, a_new = compute_analysis(ctx, cache), compute_analysis(new_ctx, cache)
    c_old, c_new = _cands(ctx, a_old, snaps), _cands(new_ctx, a_new, snaps)
    p_old, p_new = _policy_view(a_old), _policy_view(a_new)

    def brief(c):
        return {"key": c["key"], "kind": c["kind"], "score": round(c["score"], 3), **({"blocked": c["blocked"]} if c.get("blocked") else {})}

    policy = {"new_drift": [], "resolved_drift": [], "action_changes": [], "ungoverned": [], "newly_governed": []}
    for k in sorted(set(p_old) | set(p_new)):
        (so, ao), (sn, an) = p_old.get(k, (None, None)), p_new.get(k, (None, None))
        label = {"repo": k[0], "class": k[1]}
        if so is None and sn is not None:
            policy["newly_governed"].append(label)
        elif sn is None and so is not None:
            policy["ungoverned"].append(label)
        elif so != "drift" and sn == "drift":
            policy["new_drift"].append({**label, "action": an})
        elif so == "drift" and sn != "drift":
            policy["resolved_drift"].append(label)
        if ao != an and so is not None and sn is not None and "drift" in (so, sn):     # only where it changes what happens
            policy["action_changes"].append({**label, "old": ao, "new": an})

    tracked = []
    for row in ctx.db.q("select * from proposals where status in ('awaiting-approval','approved','held','executing') order by num"):
        p = proposals.view(row)
        c = c_new.get(p["key"])
        if c is None:
            tracked.append({"id": p["id"], "key": p["key"], "effect": "no longer a candidate under the new config"})
        elif c.get("blocked"):
            tracked.append({"id": p["id"], "key": p["key"], "effect": f"blocked: {c['blocked']}"})
        else:
            try:
                h = plan_for(new_ctx, c["kind"], c["subject"]).fingerprint()
            except Exception as e:  # noqa: BLE001
                h = None
                tracked.append({"id": p["id"], "key": p["key"], "effect": f"plan unavailable: {str(e)[:80]}"})
                continue
            if h != p["plan_hash"]:
                tracked.append({"id": p["id"], "key": p["key"], "effect": "plan changes: its approval would be cleared and asked again"})

    life = {n: [a_old["lifecycle"].get(n), a_new["lifecycle"].get(n)] for n in sorted(set(a_old["lifecycle"]) | set(a_new["lifecycle"]))
            if a_old["lifecycle"].get(n) != a_new["lifecycle"].get(n)}
    bk = lambda a: {(v["from"], v["to"]) for v in a["boundaries"]}
    return {
        "config_changes": config_diff(ctx.cfg, new_cfg),
        "analyzed": {"old": len(a_old["analyzed"]), "new": len(a_new["analyzed"])},
        "coupled_pairs": {"old": len(a_old["coupling"]), "new": len(a_new["coupling"])},
        "candidates": {
            "added": [brief(c_new[k]) for k in sorted(set(c_new) - set(c_old))],
            "removed": [brief(c_old[k]) for k in sorted(set(c_old) - set(c_new))],
            "rescored": [{"key": k, "old": round(c_old[k]["score"], 3), "new": round(c_new[k]["score"], 3)}
                         for k in sorted(set(c_old) & set(c_new)) if abs(c_old[k]["score"] - c_new[k]["score"]) >= 0.01],
            "newly_blocked": [brief(c_new[k]) for k in sorted(set(c_old) & set(c_new)) if c_new[k].get("blocked") and not c_old[k].get("blocked")]},
        "policy": policy,
        "lifecycle": life,
        "boundaries": {"new": sorted(bk(a_new) - bk(a_old)), "resolved": sorted(bk(a_old) - bk(a_new))},
        "open_proposals": tracked,
    }


def render(r: dict) -> list[str]:
    L = ["plan-config: effect of the new config on the current snapshot (nothing is written)", ""]
    L.append("config changes:" + (" none" if not r["config_changes"] else ""))
    for c in r["config_changes"]:
        L.append(f"  {c['key']}: {c['old']!r} -> {c['new']!r}")
    L.append(f"repos analyzed: {r['analyzed']['old']} -> {r['analyzed']['new']};  coupled pairs: "
             f"{r['coupled_pairs']['old']} -> {r['coupled_pairs']['new']}")
    c = r["candidates"]
    L.append(f"candidates: +{len(c['added'])} -{len(c['removed'])} rescored {len(c['rescored'])} newly blocked {len(c['newly_blocked'])}")
    for x in c["added"]:
        L.append(f"  + {x['kind']:<10} {x['score']:.2f}  {x['key']}" + (f"  [blocked: {x['blocked']}]" if x.get("blocked") else ""))
    for x in c["removed"]:
        L.append(f"  - {x['kind']:<10} {x['score']:.2f}  {x['key']}")
    for x in c["rescored"]:
        L.append(f"  ~ {x['key']}: {x['old']:.2f} -> {x['new']:.2f}")
    for x in c["newly_blocked"]:
        L.append(f"  ! {x['key']} is now blocked: {x['blocked']}")
    p = r["policy"]
    L.append(f"policy: {len(p['new_drift'])} new drifts, {len(p['resolved_drift'])} resolved, {len(p['action_changes'])} action-level changes, "
             f"{len(p['ungoverned'])} findings dropped (repos opted out), {len(p['newly_governed'])} newly governed")
    for x in p["new_drift"]:
        L.append(f"  + drift {x['repo']}: {x['class']} (action {x['action']})")
    for x in p["resolved_drift"]:
        L.append(f"  - drift {x['repo']}: {x['class']}")
    for x in p["action_changes"]:
        L.append(f"  ~ {x['repo']}: {x['class']} {x['old']} -> {x['new']}")
    if r["lifecycle"]:
        L.append("lifecycle stage changes: " + ", ".join(f"{n} {a}->{b}" for n, (a, b) in r["lifecycle"].items()))
    if r["boundaries"]["new"] or r["boundaries"]["resolved"]:
        L.append(f"boundary violations: +{len(r['boundaries']['new'])} -{len(r['boundaries']['resolved'])}")
    L.append(f"open proposals affected: {len(r['open_proposals'])}")
    for x in r["open_proposals"]:
        L.append(f"  {x['id']} {x['key']}: {x['effect']}")
    return L
