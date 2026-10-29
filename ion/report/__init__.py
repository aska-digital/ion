"""Report: markdown dashboard, run digest."""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..analyze import latest_analysis
from ..collect.snapshot import latest_snapshots
from ..util import days_since, home, iso
from ..runhistory import freshness, freshness_line


def dashboard(ctx) -> str:
    db, cfg = ctx.db, ctx.cfg
    a = latest_analysis(db)
    snaps, gen = latest_snapshots(db)
    L = [f"# Org health: {cfg['org']}", f"_Generated {iso()} from snapshot run {gen}._", ""]
    L += [freshness_line(ctx), ""]
    if not a:
        return "\n".join(L + ["No analysis yet. Run `ion collect` then `ion analyze`."])
    live = [s for s in snaps.values() if not s["archived"]]
    L += [f"- {len(snaps)} repos ({len(live)} active, {sum(1 for s in live if s['private'])} private)",
          f"- {sum(len(v) for v in a['health'].values())} health findings, "
          f"{sum(1 for v in a['policy'].values() for f in v if f['status'] == 'drift')} policy drifts, "
          f"{sum(1 for v in a['policy'].values() for f in v if f['status'] == 'blocked_by_plan')} blocked by plan", ""]
    L += ["## Repositories", "", "| repo | vis | last push | findings |", "|---|---|---|---|"]
    for n in sorted(snaps):
        s = snaps[n]
        d = days_since(s["pushed_at"])
        codes = ", ".join(f["code"] for f in a["health"].get(n, [])) or "-"
        L.append(f"| {n} | {'private' if s['private'] else 'public'}{', fork' if s['fork'] else ''}{', archived' if s['archived'] else ''} "
                 f"| {int(d) if d is not None else '?'}d ago | {codes} |")
    L += ["", "## Policy drift", ""]
    drift = [(n, f) for n, v in sorted(a["policy"].items()) for f in v if f["status"] == "drift"]
    L += [f"- {n}: {f['class']} ({f['detail']})" for n, f in drift] or ["None."]
    blocked = sorted({(n, f["class"]) for n, v in a["policy"].items() for f in v if f["status"] == "blocked_by_plan"})
    if blocked:
        L += ["", "Blocked by plan (not drift): " + ", ".join(f"{n}:{c}" for n, c in blocked)]
    L += ["", "## Coupled repos", ""]
    L += [f"- {p['a']} <> {p['b']}: {p['score']:.2f} ({', '.join(f'{k}={v}' for k, v in p['signals'].items() if v)})"
          for p in a["coupling"][:15]] or ["None above 0.15."]
    L += ["", "## Split candidates", ""]
    for r in a["cohesion"]:
        for c in r["candidates"]:
            L.append(f"- {r['repo']}: {', '.join(c['dirs'])} gap {c['gap']:.2f} score {c['score']:.2f}{'' if c['qualifies'] else ' (below threshold)'}")
    if not a["cohesion"]:
        L.append("None.")
    L += ["", "## Archive candidates", ""]
    L += [f"- {x['repo']}: {x['score']:.2f} ({'; '.join(x['reasons'])})" for x in a["archive"][:15]] or ["None."]
    L += ["", "## Proposals", ""]
    rows = db.q("select id,kind,status,score,title from proposals order by num desc limit 40")
    L += [f"- {r['id']} [{r['status']}] {r['title']} ({r['score']:.2f})" for r in rows] or ["None."]
    return "\n".join(L) + "\n"


def write_dashboard(ctx) -> Path:
    d = home() / "reports"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{iso()[:10]}.md"
    path.write_text(dashboard(ctx))
    return path


def _collect(ctx, results: dict):
    db = ctx.db
    ev = []
    for p in results.get("propose", {}).get("created", []):
        ev.append(f"new {p['id']} {p['title']} (score {p['score']:.2f})")
    for pid in results.get("propose", {}).get("replanned", []):
        ev.append(f"{pid} re-planned, approval needed again")
    for pid, old, new in results.get("approvals", {}).get("changed", []):
        ev.append(f"{pid}: {old} -> {new}")
    for pid, actor, reason in results.get("approvals", {}).get("ignored", []):
        ev.append(f"ignored approval signal on {pid} from {actor}: {reason}")
    ev += results.get("execute", {}).get("actions", [])
    errs = list(results.get("errors", []))
    waiting = [r["id"] + " " + r["title"] for r in db.q("select id,title from proposals where status='awaiting-approval' order by num")]
    f = freshness(ctx, stale_after_hours=ctx.cfg.get("report", {}).get("stale_after_hours", 48))
    stable = "\n".join(waiting) + f"\nfreshness:{f['status']}:{f['latest_status']}:{f['last_completed_status']}:{f['stale_after_hours']}"
    sig = hashlib.sha256(stable.encode()).hexdigest()      # the awaiting-approval list
    return ev, errs, waiting, sig


def build_digest(ctx, results: dict) -> tuple[str, bool]:
    """Short text for notifiers, and whether anything changed since the last digest."""
    ev, errs, waiting, sig = _collect(ctx, results)
    changed = bool(ev or errs) or (sig != ctx.db.kv_get("last_digest_sig"))
    lines = [f"ion {iso()[:10]}", freshness_line(ctx)] + [f"- {e}" for e in ev]
    if waiting:
        lines.append(f"awaiting approval ({len(waiting)}): " + "; ".join(waiting[:6]))
    lines += [f"- error: {e}" for e in errs]
    return "\n".join(lines), changed


def mark_digest_sent(ctx, results: dict) -> None:
    ctx.db.kv_set("last_digest_sig", _collect(ctx, results)[3])
