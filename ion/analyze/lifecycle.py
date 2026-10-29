"""Archive candidates and lagging forks."""
from __future__ import annotations

from ..util import days_since


def is_frozen(name: str, snap: dict, cfg) -> bool:
    lc = cfg["lifecycle"]
    return name in lc["frozen"] or lc["frozen_topic"].lower() in {t.lower() for t in (snap.get("topics") or [])}


def stage_of(name: str, snap: dict, cfg, now=None) -> str:
    """active -> stale (no push for stale_days) -> archive-proposed (another dormant_days without a push).

    Any push puts a repo back to `active`, because the stage is computed from the last push. A frozen repo (listed in
    `lifecycle.frozen` or carrying the `ion-frozen` topic) never leaves `frozen`. The last stage only makes a repo
    *eligible* for an archive proposal; archiving itself always needs a maintainer's approval."""
    if snap.get("archived"):
        return "archived"
    if is_frozen(name, snap, cfg):
        return "frozen"
    age = days_since(snap.get("pushed_at"), now)
    stale = cfg["thresholds"]["stale_days"]
    if age is None or age <= stale:
        return "active"
    return "stale" if age <= stale + cfg["lifecycle"]["dormant_days"] else "archive-proposed"


def stages(snaps: dict[str, dict], cfg, now=None) -> dict[str, str]:
    return {n: stage_of(n, s, cfg, now) for n, s in snaps.items() if not s.get("fork") and n not in cfg["exclude"]}


def archive_candidates(snaps: dict[str, dict], pairs: list[dict], cfg, now=None, unread: set | None = None) -> list[dict]:
    """`unread`: live repos whose contents were not loaded (no mirror), so whether they reference a repo is unknown."""
    th = cfg["thresholds"]
    dependents: dict[str, set] = {n: set() for n in snaps}
    for p in pairs:
        if p["a_refs_b"] >= 0.5:
            dependents.setdefault(p["b"], set()).add(p["a"])
        if p["b_refs_a"] >= 0.5:
            dependents.setdefault(p["a"], set()).add(p["b"])
    out = []
    for name, s in snaps.items():
        if s.get("archived") or s.get("fork") or name in cfg["exclude"]:
            continue
        stage = stage_of(name, s, cfg, now)
        if stage in ("active", "frozen"):
            continue
        age = days_since(s.get("pushed_at"), now)
        score, reasons = 0.5, [f"no push in {int(age)} days"]
        others_unread = (unread or set()) - {name}
        if dependents.get(name):
            pass
        elif others_unread:
            reasons.append(f"references unknown: {len(others_unread)} other repos were not analysed")
        else:
            score += 0.2
            reasons.append("no other repo references it")
        superseded = None
        for p in pairs:
            other = p["b"] if p["a"] == name else p["a"] if p["b"] == name else None
            if other and p["signals"]["dup"] >= 0.6:
                os_ = snaps.get(other, {})
                if not os_.get("archived") and (days_since(os_.get("pushed_at"), now) or 1e9) < age:
                    superseded = other
        if superseded:
            score += 0.3
            reasons.append(f"largely duplicated by newer repo {superseded}")
        due = int(th["stale_days"] + cfg["lifecycle"]["dormant_days"] - age)
        if stage == "stale":
            reasons.append(f"stale; becomes eligible for an archive proposal in {max(due, 0) + 1} days without a push")
        out.append({"repo": name, "score": round(min(score, 1.0), 3), "reasons": reasons, "superseded_by": superseded,
                    "dependents": sorted(dependents.get(name, ())), "stage": stage,
                    "qualifies": stage == "archive-proposed" and score >= th["archive_score"]})
    return sorted(out, key=lambda x: -x["score"])


def lagging_forks(snaps: dict[str, dict], days: int = 90) -> list[dict]:
    out = []
    for name, s in snaps.items():
        par = s.get("parent")
        if s.get("fork") and par and not s.get("archived"):
            from ..util import parse_ts
            a, b = parse_ts(par.get("pushedAt")), parse_ts(s.get("pushed_at"))
            if a and b and (a - b).days > days:
                out.append({"repo": name, "upstream": par.get("nameWithOwner"), "behind_days": (a - b).days})
    return out
