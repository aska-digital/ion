"""Pure scoring, hysteresis and cooldown rules."""
from __future__ import annotations

from ..util import now as _now
from ..util import parse_ts


def merge_direction(a: dict, b: dict) -> tuple[str, str]:
    """(source, target): the smaller, less active repo folds into the larger one."""
    def weight(s):
        return (s.get("size") or 0) + 50 * len(s.get("commits") or [])
    wa, wb = weight(a), weight(b)
    if wa == wb:
        return (a["name"], b["name"]) if a["name"] > b["name"] else (b["name"], a["name"])
    return (a["name"], b["name"]) if wa < wb else (b["name"], a["name"])


def merge_qualifies(pair: dict, cfg) -> bool:
    return pair["score"] >= cfg["thresholds"]["merge_coupling"]


def split_qualifies(c: dict, cfg) -> bool:
    return c["gap"] >= cfg["thresholds"]["split_cohesion_gap"]


def next_streak(prev_streak: int | None, prev_gen: int | None, gen: int) -> int:
    """Streak counts distinct snapshot generations in a row. Re-running on the same snapshot does not count."""
    if prev_streak is None:
        return 1
    if prev_gen == gen:
        return prev_streak
    return prev_streak + 1


def ready(streak: int, cfg) -> bool:
    return streak >= cfg["thresholds"]["persist_runs"]


def cooldown_blocks(rejected_at: str | None, rejected_score: float | None, score: float, cfg, now=None) -> bool:
    """A rejected (or dropped) proposal is suppressed for cooldown_days unless its score rose by rescore_delta."""
    t = parse_ts(rejected_at)
    if t is None:
        return False
    if ((now or _now()) - t).days >= cfg["thresholds"]["cooldown_days"]:
        return False
    return score < (rejected_score or 0.0) + cfg["thresholds"]["rescore_delta"]


def replan_needed(old_hash: str | None, new_hash: str, epoch_score: float | None, score: float, cfg) -> bool:
    if old_hash is not None and old_hash != new_hash:
        return True
    return epoch_score is not None and abs(score - epoch_score) >= cfg["thresholds"]["rescore_delta"]
