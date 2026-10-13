"""Architecture boundaries from repo tags.

Tags are GitHub topics (for example `scope:protean`, `layer:shared`). Maintainers declare which tags a repo may depend
on in `boundaries.rules`:

    boundaries:
      rules:
        - {from: "scope:protean", allow: ["scope:protean", "layer:shared"]}

A dependency (a repo reaching another through its manifest, imports or git URLs) from a repo tagged `from` to a repo
that carries none of the `allow` tags is a violation. Violations are findings for the dashboard, not proposals. The
same data keeps coupling scores from overriding a deliberate separation: merging repos whose scopes differ, or which
the rules forbid from depending on each other in both directions, is flagged and (by default) not proposed.
"""
from __future__ import annotations


def tags_of(snap: dict) -> set[str]:
    return {t.lower() for t in (snap.get("topics") or [])}


def rules_of(cfg) -> list[dict]:
    return [{"from": r["from"].lower(), "allow": {a.lower() for a in r["allow"]}} for r in cfg["boundaries"]["rules"]]


def _edge_violation(rules: list[dict], src_tags: set[str], dst_tags: set[str]) -> dict | None:
    for r in rules:
        if r["from"] in src_tags and not (dst_tags & r["allow"]):
            why = ("target has no tags" if not dst_tags else
                   f"target tags {sorted(dst_tags)} are not among the allowed {sorted(r['allow'])}")
            return {"rule": r["from"], "reason": why}
    return None


def violations(snaps: dict[str, dict], pairs: list[dict], cfg) -> list[dict]:
    """Directed dependency edges that break a rule. Edges come from the coupling analysis (`a_refs_b`, `b_refs_a`)."""
    rules = rules_of(cfg)
    if not rules:
        return []
    out = []
    for p in pairs:
        for src, dst, strength in ((p["a"], p["b"], p["a_refs_b"]), (p["b"], p["a"], p["b_refs_a"])):
            if strength < 0.5 or src not in snaps or dst not in snaps:
                continue
            v = _edge_violation(rules, tags_of(snaps[src]), tags_of(snaps[dst]))
            if v:
                out.append({"from": src, "to": dst, "from_tags": sorted(tags_of(snaps[src])),
                            "to_tags": sorted(tags_of(snaps[dst])), **v,
                            "evidence": p["evidence"]["a_refs_b" if src == p["a"] else "b_refs_a"]})
    return sorted(out, key=lambda v: (v["from"], v["to"]))


def separation(a: dict, b: dict, cfg) -> str | None:
    """Why two repos are deliberately separate (so a merge proposal is flagged), or None."""
    prefix = cfg["boundaries"]["scope_prefix"].lower()
    ta, tb = tags_of(a), tags_of(b)
    sa, sb = {t for t in ta if t.startswith(prefix)}, {t for t in tb if t.startswith(prefix)}
    if sa and sb and not (sa & sb):
        return f"different scopes: {a['name']} is {sorted(sa)}, {b['name']} is {sorted(sb)}"
    rules = rules_of(cfg)
    if rules and ta and tb and _edge_violation(rules, ta, tb) and _edge_violation(rules, tb, ta):
        return f"boundary rules forbid {a['name']} and {b['name']} from depending on each other"
    return None
