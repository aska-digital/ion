from ion.analyze import run_analyze
from ion.analyze import health, minhash, policy
from ion.analyze.coupling import combine, parse_deps, prefix_signal
from conftest import code


def test_coupling_pair_detected(collected):
    a = run_analyze(collected)
    pair = next(p for p in a["coupling"] if {p["a"], p["b"]} == {"alpha-core", "alpha-cli"})
    s = pair["signals"]
    assert s["xref"] >= 0.8                      # pyproject names alpha-core, imports alpha_core
    assert s["dup"] >= 0.3                       # identical + near-identical files
    assert s["cochange"] > 0.3                   # same author, same days
    assert s["prefix"] == 1.0
    assert pair["score"] >= 0.6
    assert not any("mono" in (p["a"], p["b"]) or "old-tool" in (p["a"], p["b"]) for p in a["coupling"] if p["score"] >= 0.6)


def test_cohesion_finds_two_halves(collected):
    a = run_analyze(collected)
    mono = next(r for r in a["cohesion"] if r["repo"] == "mono")
    assert sorted(map(sorted, mono["clusters"])) == [["engine", "engine_utils"], ["site", "site_utils"]]
    best = max(mono["candidates"], key=lambda c: c["score"])
    assert best["gap"] == 1.0 and best["qualifies"]
    assert not any(r["repo"] == "alpha-core" for r in a["cohesion"] if any(c["qualifies"] for c in r["candidates"]))


def test_lifecycle_archive_candidate(collected):
    a = run_analyze(collected)
    old = next(x for x in a["archive"] if x["repo"] == "old-tool")
    assert old["qualifies"] and old["score"] >= 0.7
    assert not any(x["repo"] == "alpha-core" and x["qualifies"] for x in a["archive"])


def test_excluded_fork_never_analyzed(collected):
    a = run_analyze(collected)
    assert "hermes-agent" not in a["health"] and "hermes-agent" not in a["analyzed"]


def test_health_findings(collected):
    a = run_analyze(collected)
    codes = {f["code"] for f in a["health"]["old-tool"]}
    assert "stale" in codes and "missing_ci" in codes
    assert "missing_readme" not in codes


def test_health_missing_readme_and_empty(collected):
    snap = {"name": "x", "private": False, "pushed_at": "2026-10-10T00:00:00Z", "default_branch": "master", "description": ""}
    codes = {f["code"] for f in health.check(snap, {"a.py"}, collected.cfg)}
    assert {"missing_readme", "missing_license", "missing_description", "default_branch"} <= codes
    assert health.check({"name": "e", "empty": True}, None, collected.cfg)[0]["code"] == "empty_repo"


def test_policy_drift_and_plan_limits(collected):
    a = run_analyze(collected)
    pol = {(r, f["class"]): f["status"] for r, fs in a["policy"].items() for f in fs}
    assert pol[("alpha-core", "branch_protection")] == "drift"        # no rule
    assert pol[("alpha-cli", "branch_protection")] == "ok"
    assert pol[("secret-sauce", "branch_protection")] == "blocked_by_plan"
    assert pol[("secret-sauce", "secret_scanning")] == "blocked_by_plan"
    assert pol[("old-tool", "dependabot_alerts")] == "drift"
    assert pol[("alpha-core", "secret_scanning")] == "ok"
    assert pol[("alpha-core", "required_file:README.md")] == "ok"


def test_policy_secret_scanning_private_repo_on_paid_plan_is_unknown(collected):
    snap = {"name": "r", "private": True, "fork": False, "archived": False, "default_branch": "main",
            "protection": [], "vuln_enabled": True}
    paid = dict(collected.cfg)
    paid["plan"] = "team"
    fs = policy.check(snap, {"README.md"}, paid, None)
    ss = [f for f in fs if f["class"] == "secret_scanning"]
    assert len(ss) == 1 and ss[0]["status"] == "unknown"


def test_policy_per_repo_override_changes_requirements(collected):
    snap = {"name": "r", "private": False, "fork": False, "archived": False, "default_branch": "main", "protection": [],
            "vuln_enabled": True, "secret_scanning": "enabled"}
    off = policy.check(snap, {"README.md"}, collected.cfg, {"policy": {"branch_protection": None}})
    assert not any(f["class"] == "branch_protection" for f in off)
    on = policy.check(snap, {"README.md"}, collected.cfg, None)
    assert any(f["class"] == "branch_protection" and f["status"] == "drift" for f in on)


def test_minhash_near_duplicates():
    a = minhash.signature(minhash.shingles(minhash.normalise_lines(code("z", 40))))
    b = minhash.signature(minhash.shingles(minhash.normalise_lines(code("z", 40).replace("value_z_5 ", "other_5 "))))
    c = minhash.signature(minhash.shingles(minhash.normalise_lines(code("q", 40))))
    assert minhash.similarity(a, b) > 0.8 and minhash.similarity(a, c) < 0.2
    assert minhash.near_duplicates({"x": a}, {"y": b, "z": c})[0][:2] == ("x", "y")


def test_combine_is_noisy_or_and_weak_signals_alone_do_not_merge():
    assert combine({"xref": 1.0}) == 0.6
    assert combine({"prefix": 1.0, "deps": 1.0}) < 0.6
    assert combine({"xref": 1, "dup": 1, "cochange": 1}) > 0.9
    assert combine({"cochange": 1.0}) < 0.6                       # same author on the same days is not enough


def test_parse_deps_variants():
    assert parse_deps("package.json", '{"dependencies": {"left_pad": "1"}, "devDependencies": {"jest": "1"}}') == {"left_pad", "jest"}
    assert parse_deps("requirements.txt", "Flask==2\n# c\n-e .\nrequests>=2\n") == {"flask", "requests"}
    assert prefix_signal("protean-ops", "protean-kit") == 1.0 and prefix_signal("a-b", "c-d") == 0.0
