"""Issue #14: states() and require() must agree on an unknown dependency id.

build() generates consistent ids today, so the disagreement is latent. The
contract pinned here is fail-closed in both places: an unknown dependency
counts as not landed."""
import pytest

from ion import changesets
from ion.util import Waiting

BUILT = [
    {"id": "a", "repo": "alpha-core", "kind": "pr", "pr_key": "pr_number", "depends_on": [], "title": "a"},
    {"id": "b", "repo": "alpha-core", "kind": "pr", "pr_key": "pr_number", "depends_on": ["ghost"], "title": "b"},
]


def _proposal(monkeypatch):
    monkeypatch.setattr(changesets, "build", lambda p: BUILT)
    return {"id": "P-1", "kind": "merge", "subject": {}, "state": {}}


def test_states_marks_unknown_dependency_blocked(ctx, monkeypatch):
    p = _proposal(monkeypatch)
    sts = {c["id"]: c["state"] for c in changesets.states(ctx, p, query=False)}
    assert sts["a"] == "planned"
    assert sts["b"] == "blocked"


def test_require_waits_on_unknown_dependency(ctx, monkeypatch):
    p = _proposal(monkeypatch)
    with pytest.raises(Waiting, match="ghost"):
        changesets.require(ctx, p, "b")
