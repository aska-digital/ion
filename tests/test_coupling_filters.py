"""Feature 5: co-change hygiene (huge commits, bots, Mimar's own identity, minimum counts, prefix noise)."""
import os

from conftest import ENV, code, sh
from mimar.analyze import coupling
from mimar.analyze.coupling import (DEFAULT_FILTERS, RepoData, combine, cochange_signal, filters_from_cfg, ignore_reason,
                                    load_repo)
from mimar.config import load_config

F = DEFAULT_FILTERS


def test_ignore_reason_classes():
    assert ignore_reason("Dev", "d@x.io", 41, F) == "huge" and ignore_reason("Dev", "d@x.io", 40, F) is None
    assert ignore_reason("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com", 2, F) == "bot"
    assert ignore_reason("Renovate Bot", "r@x.io", 2, F) == "bot"
    assert ignore_reason("Dev", "12345+dev@users.noreply.github.com", 2, F) is None       # a normal noreply address is a person
    f = filters_from_cfg(load_config(None, {"org": "o", "git_identity": {"name": "mimar", "email": "mimar@example.com"}}))
    assert ignore_reason("mimar", "other@x.io", 1, f) == "own" and ignore_reason("X", "MIMAR@example.com", 1, f) == "own"


def commit_all(work, msg, date, name, email, files):
    for path, content in files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(content)
    sh("git", "add", "-A", cwd=work)
    env = {**ENV, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
          "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    sh("git", "commit", "-q", "-m", msg, cwd=work, env=env)


def test_load_repo_leaves_out_huge_bot_and_own_commits(tmp_path):
    w = tmp_path / "r"
    w.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=w)
    commit_all(w, "human", "2026-09-01T10:00:00+00:00", "Dev One", "dev1@example.com", {"a.py": code("a", 5)})
    commit_all(w, "sweep", "2026-09-02T10:00:00+00:00", "Dev One", "dev1@example.com", {f"s/f{i}.py": "x\n" for i in range(60)})
    commit_all(w, "bump", "2026-09-03T10:00:00+00:00", "dependabot[bot]", "bot@users.noreply.github.com", {"b.txt": "1\n"})
    commit_all(w, "mimar pr", "2026-09-04T10:00:00+00:00", "mimar", "mimar@example.com", {"c.txt": "1\n"})
    commit_all(w, "human 2", "2026-09-05T10:00:00+00:00", "Dev One", "dev1@example.com", {"d.py": code("d", 5)})
    cfg = load_config(None, {"org": "o", "git_identity": {"name": "mimar", "email": "mimar@example.com"}})
    rd = load_repo("r", w, "main", {}, filters_from_cfg(cfg))
    assert len(rd.commits) == 2 and rd.ignored == {"huge": 1, "bot": 1, "own": 1}


def rd(name, days, who="dev"):
    return RepoData(name=name, commits=[(f"2026-09-{d:02d}T10:00:00+00:00", {who}) for d in days])


def test_cochange_needs_min_revisions_and_min_shared():
    a, b = rd("a", [1, 3, 5, 7, 9]), rd("b", [1, 3, 5, 7, 9])
    s, ev = cochange_signal(a, b)
    assert s == 1.0                                              # 5 shared / average 5
    assert cochange_signal(rd("a", [1, 3, 5, 7]), rd("b", [1, 3, 5, 7]))[0] == 0.0       # only 4 revisions each: below min_revs
    assert cochange_signal(a, rd("b", [1, 2, 20, 21, 22]))[0] == 0.0                      # fewer than 3 shared
    assert cochange_signal(a, b, min_revs=3)[0] == 1.0


def test_cochange_degree_is_shared_over_average_revisions():
    a = rd("a", [1, 3, 5, 7, 9, 11, 13, 15])                     # 8 revisions
    b = rd("b", [1, 3, 5, 20, 22, 24])                           # 6 revisions, 3 shared
    s, ev = cochange_signal(a, b)
    assert abs(s - 3 / 7) < 1e-9 and ev["shared_windows"] == 3


def test_name_prefix_alone_no_longer_creates_coupling():
    assert combine({"prefix": 1.0}) == 0.0                       # v1 gave 0.15 here: 265 protean-* "pairs"
    assert combine({"prefix": 1.0, "cochange": 0.1}) == combine({"cochange": 0.1})      # prefix adds nothing
    weak = combine({"prefix": 1.0, "deps": 0.5})
    assert weak < combine({"prefix": 0.0, "deps": 0.5}) + 0.05 and weak > combine({"deps": 0.5})   # tiny bonus with corroboration
    assert combine({"xref": 1.0}) == 0.6                         # a real reference still reaches the merge threshold


def test_prefix_weight_is_configurable():
    base = combine({"prefix": 1.0, "dup": 0.5}, {"prefix": 0.05})
    assert combine({"prefix": 1.0, "dup": 0.5}, {"prefix": 0.5}) > base


def test_a_readme_link_does_not_vouch_for_cochange():
    """The v1 live run: two protean-* repos, one author, a README link (xref 0.3), co-change 0.8 reached the merge threshold."""
    assert combine({"xref": 0.3, "dup": 0.131, "cochange": 0.8, "prefix": 1.0}) < 0.6
    assert combine({"xref": 0.7, "cochange": 0.8}) > combine({"xref": 0.7})          # a real reference still lets co-change count fully
