"""Command line: mimar collect | analyze | propose | sync-approvals | execute | report | run | status | explain | pause | resume | doctor"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__, proposals
from .config import ConfigError, load_config
from .context import Context
from .db import DB
from .util import home, iso


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="mimar", description="Steward bot for a GitHub org. Never changes structure without approval.")
    ap.add_argument("--config", help="path to mimar.yml (default: $MIMAR_CONFIG, ./mimar.yml, ~/.mimar/mimar.yml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="fetch org metadata and git mirrors")
    c.add_argument("--no-mirror", action="store_true")
    sub.add_parser("analyze", help="compute health, policy, coupling and cohesion signals")
    p = sub.add_parser("propose", help="turn signals into proposals and tracking issues")
    p.add_argument("--dry-run", action="store_true", help="show candidates, write nothing")
    sub.add_parser("sync-approvals", help="read approvals from the configured sources")
    e = sub.add_parser("execute", help="run approved proposals (dry-run unless --apply)")
    g = e.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="default")
    g.add_argument("--apply", action="store_true")
    e.add_argument("proposal", nargs="?")
    sub.add_parser("report", help="write the markdown dashboard and send the digest")
    r = sub.add_parser("run", help="full pipeline (what the scheduler calls)")
    r.add_argument("--apply", action="store_true", help="let executors act (default: dry-run)")
    r.add_argument("--no-mirror", action="store_true")
    sub.add_parser("status")
    x = sub.add_parser("explain")
    x.add_argument("proposal")
    sub.add_parser("pause", help="engage the kill switch")
    sub.add_parser("resume", help="release the kill switch")
    sub.add_parser("doctor", help="check gh auth, git, filter-repo, claude, the bus and the config")
    return ap


def make_ctx(args, no_mirror=False) -> Context:
    from .propose.llm import make_llm
    cfg = load_config(args.config)
    ctx = Context(cfg=cfg, db=DB(), sync_mirrors=not no_mirror)
    from .ghclient import GH
    ctx.gh = GH()
    ctx.llm = make_llm(cfg)
    return ctx


def out(s: str = "") -> None:
    print(s)


def cmd_collect(ctx, args):
    from .collect import run_collect
    r = run_collect(ctx)
    out(f"collected {r['repos']} repos, {r['cost']} GraphQL points" + (f", {r['remaining']} remaining" if r.get("remaining") is not None else "")
        + (" (PARTIAL: budget reached)" if r["partial"] else ""))
    states = {}
    for v in r["mirrors"].values():
        states[v] = states.get(v, 0) + 1
    if states:
        out("mirrors: " + ", ".join(f"{k}={v}" for k, v in sorted(states.items())))
    for w in r["warnings"]:
        out(f"warning: {w}")
    return r


def cmd_analyze(ctx, args):
    from .analyze import run_analyze
    a = run_analyze(ctx)
    drift = sum(1 for v in a["policy"].values() for f in v if f["status"] == "drift")
    out(f"analyzed {len(a['analyzed'])} repos: {len(a['coupling'])} coupled pairs, {len(a['cohesion'])} repos with split candidates, "
        f"{sum(1 for x in a['archive'] if x['qualifies'])} archive candidates, {drift} policy drifts")
    return a


def cmd_propose(ctx, args, dry_run=False):
    from .propose.engine import run_propose
    r = run_propose(ctx, dry_run=dry_run or getattr(args, "dry_run", False))
    for c in sorted(r["candidates"], key=lambda c: -c["score"]):
        out(f"  {c['kind']:<11} {c['score']:.2f}  streak {c['streak']}  {c['state']:<28} {c['key']}")
    out(f"candidates={len(r['candidates'])} created={len(r['created'])} updated={len(r['updated'])} replanned={len(r['replanned'])} "
        f"suppressed={len(r['suppressed'])} errors={len(r['errors'])}")
    for e in r["errors"]:
        out(f"error: {e}")
    return r


def cmd_sync(ctx, args):
    from .approve import sync_approvals
    r = sync_approvals(ctx)
    for pid, old, new in r["changed"]:
        out(f"{pid}: {old} -> {new}")
    for pid, actor, why in r["ignored"]:
        out(f"ignored on {pid}: {actor}: {why}")
    for e in r["errors"]:
        out(f"error: {e}")
    if not (r["changed"] or r["ignored"] or r["errors"]):
        out("no approval changes")
    return r


def cmd_execute(ctx, args, apply=None):
    from .execute.runner import run_execute
    apply = args.apply if apply is None else apply
    r = run_execute(ctx, getattr(args, "proposal", None), apply=apply)
    for pid, lines in r["plans"].items():
        out(f"{pid} ({'dry-run' if apply is False else 'plan'}):")
        for l in lines:
            out("  " + l)
    for a in r["actions"]:
        out("did: " + a)
    for s in r["skipped"]:
        out("skipped: " + s)
    for e in r["errors"]:
        out("error: " + e)
    return r


def cmd_report(ctx, args, results=None):
    from .notify import notify
    from .report import build_digest, mark_digest_sent, write_dashboard
    path = write_dashboard(ctx)
    out(f"dashboard: {path}")
    text, changed = build_digest(ctx, results or {})
    if changed:
        errs = notify(ctx, text)
        if len(errs) < len(ctx.cfg["notifiers"]):
            mark_digest_sent(ctx, results or {})
        for e in errs:
            out(f"notify error (queued for retry): {e}")
    else:
        out("digest: nothing changed, not sent")
    return {"errors": []}


def cmd_run(ctx, args):
    results: dict = {"errors": []}
    if ctx.cfg.is_paused():
        out("kill switch is on: reads and reports only, no GitHub writes")
    steps = [("collect", lambda: cmd_collect(ctx, args)), ("analyze", lambda: cmd_analyze(ctx, args)),
             ("propose", lambda: cmd_propose(ctx, args)), ("approvals", lambda: cmd_sync(ctx, args)),
             ("execute", lambda: cmd_execute(ctx, args, apply=args.apply))]
    for name, fn in steps:
        try:
            results[name] = fn()
            results["errors"] += results[name].get("errors", []) if isinstance(results[name], dict) and name != "collect" else []
        except Exception as e:  # noqa: BLE001
            results["errors"].append(f"{name}: {str(e)[:300]}")
            out(f"{name} failed: {e}")
            if name in ("collect", "analyze"):
                break
    cmd_report(ctx, args, results)
    return 1 if results["errors"] else 0


def cmd_status(ctx, args):
    last = ctx.db.one("select * from runs order by id desc limit 1")
    out(f"kill switch: {'ON (paused)' if ctx.cfg.is_paused() else 'off'}")
    out(f"org: {ctx.cfg['org']}  tracker: {ctx.cfg['tracker_repo']}  llm: {ctx.cfg['llm']['backend']}  approvals: {','.join(ctx.cfg['approval_sources'])}")
    if last:
        out(f"last collect: run {last['id']} at {last['finished'] or last['started']}, {last['repos']} repos, {last['cost']} points"
            + (" (partial)" if last["partial"] else ""))
    else:
        out("no collect run yet")
    counts = ctx.db.q("select status, count(*) n from proposals group by status")
    out("proposals: " + (", ".join(f"{r['status']}={r['n']}" for r in counts) or "none"))
    for r in ctx.db.q("select id,kind,status,score,title from proposals where status not in ('done','dropped','rejected') order by num"):
        out(f"  {r['id']} {r['kind']:<10} {r['status']:<18} {r['score']:.2f} {r['title']}")


def cmd_explain(ctx, args):
    p = proposals.get(ctx.db, args.proposal.upper())
    if not p:
        out(f"no such proposal {args.proposal}")
        return 1
    from .approve.core import evaluate
    from .execute import executor_for
    ev = evaluate(ctx, p)
    out(f"{p['id']}  {p['kind']}  status={p['status']}  score={p['score']:.2f}")
    out(f"title: {p['text'].get('title', p['title'])}")
    for k in ("summary", "rationale", "blast_radius"):
        if p["text"].get(k):
            out(f"{k}: {p['text'][k]}")
    out("risks: " + "; ".join(p["text"].get("risks", [])))
    out("evidence:\n" + json.dumps(p["evidence"], indent=2, default=str)[:4000])
    ex = executor_for(ctx, p["kind"])
    try:
        out("plan:")
        for l in ex.plan(p).render():
            out("  " + l)
    except Exception as e:  # noqa: BLE001
        out(f"plan unavailable: {e}")
    out("rollback: " + "; ".join(p["text"].get("rollback_plan") or ex.rollback_notes(p)))
    out(f"approval: {ev['state']} ({ev['reason']}); approvers={ev['approvers']}; epoch={p['epoch']}")
    for r in ctx.db.q("select * from approvals where proposal=? order by id", (p["id"],)):
        out(f"  {r['source']} {r['actor']} {r['action']} valid={bool(r['valid'])} {r['reason']}")
    for r in ctx.db.q("select * from ledger where proposal=? order by id", (p["id"],)):
        out(f"  ledger {r['ts']} {r['step']} {r['phase']} {r['detail'][:80]}")
    return 0


def cmd_doctor(ctx, args, cfg_error=None):
    from .doctor import doctor
    bad = 0
    for c in doctor(ctx, cfg_error):
        out(f"[{'ok' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
        bad += 0 if c["ok"] else 1
    return 1 if bad else 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    if args.cmd in ("pause", "resume"):
        f = home() / "PAUSED"
        if args.cmd == "pause":
            f.write_text(f"paused {iso()}\n")
            out(f"paused: {f}")
        else:
            f.unlink(missing_ok=True)
            out("resumed (note: `paused: true` in the config also pauses)")
        return 0
    try:
        ctx = make_ctx(args, no_mirror=getattr(args, "no_mirror", False))
    except ConfigError as e:
        if args.cmd == "doctor":
            from .config import DEFAULTS, Config
            ctx = Context(cfg=Config(DEFAULTS), db=DB())
            return cmd_doctor(ctx, args, cfg_error=str(e))
        print(f"config error: {e}", file=sys.stderr)
        return 2
    try:
        if args.cmd == "doctor":
            return cmd_doctor(ctx, args)
        fn = {"collect": cmd_collect, "analyze": cmd_analyze, "propose": cmd_propose, "sync-approvals": cmd_sync,
              "execute": cmd_execute, "report": cmd_report, "run": cmd_run, "status": cmd_status, "explain": cmd_explain}[args.cmd]
        r = fn(ctx, args)
        return r if isinstance(r, int) else 0
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        ctx.db.close()


if __name__ == "__main__":
    sys.exit(main())
