"""Archive candidates and lagging forks."""
from __future__ import annotations

from ..util import days_since


def archive_candidates(snaps: dict[str, dict], pairs: list[dict], cfg, now=None) -> list[dict]:
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
        age = days_since(s.get("pushed_at"), now)
        if age is None or age <= th["stale_days"]:
            continue
        score, reasons = 0.5, [f"no push in {int(age)} days"]
        if not dependents.get(name):
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
        out.append({"repo": name, "score": round(min(score, 1.0), 3), "reasons": reasons, "superseded_by": superseded,
                    "dependents": sorted(dependents.get(name, ())), "qualifies": score >= th["archive_score"]})
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
