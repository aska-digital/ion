"""Tiny MinHash + LSH over normalised line shingles (no external dependency)."""
from __future__ import annotations

import hashlib
import random
import re

NUM_PERM = 64
BANDS = 16
ROWS = NUM_PERM // BANDS
_P = (1 << 61) - 1
_rng = random.Random(1337)
_A = [_rng.randrange(1, _P) for _ in range(NUM_PERM)]
_B = [_rng.randrange(0, _P) for _ in range(NUM_PERM)]

_COMMENT = re.compile(r"^\s*(#|//|/\*|\*|--)")


def normalise_lines(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        s = " ".join(line.split())
        if not s or _COMMENT.match(s):
            continue
        out.append(s)
    return out


def shingles(lines: list[str], k: int = 3) -> set[int]:
    if len(lines) < k:
        return set()
    res = set()
    for i in range(len(lines) - k + 1):
        h = hashlib.blake2b("\n".join(lines[i:i + k]).encode(), digest_size=8).digest()
        res.add(int.from_bytes(h, "big"))
    return res


def signature(sh: set[int]) -> tuple[int, ...] | None:
    if not sh:
        return None
    return tuple(min((a * x + b) % _P for x in sh) for a, b in zip(_A, _B))


def similarity(s1: tuple[int, ...], s2: tuple[int, ...]) -> float:
    return sum(1 for x, y in zip(s1, s2) if x == y) / NUM_PERM


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def bands(sig: tuple[int, ...]):
    for i in range(BANDS):
        yield i, hash(sig[i * ROWS:(i + 1) * ROWS])


def near_duplicates(left: dict[str, tuple[int, ...]], right: dict[str, tuple[int, ...]], threshold: float = 0.8):
    """Pairs (left_key, right_key, est_jaccard) with similarity >= threshold, via LSH banding."""
    buckets: dict[tuple[int, int], list[str]] = {}
    for k, sig in right.items():
        for b in bands(sig):
            buckets.setdefault(b, []).append(k)
    seen, res = set(), []
    for lk, lsig in left.items():
        for b in bands(lsig):
            for rk in buckets.get(b, ()):
                if (lk, rk) in seen:
                    continue
                seen.add((lk, rk))
                s = similarity(lsig, right[rk])
                if s >= threshold:
                    res.append((lk, rk, s))
    return res
