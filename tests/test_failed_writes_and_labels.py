"""Issues #60 and #15: a failed write must not consume its per-run cap slot,
and label setup must only swallow the already-exists case."""
import pytest

from ion import tracker
from ion.ghclient import GHError
from ion.util import Waiting
from ion.writes import WriteRequest


def _boom():
    raise RuntimeError("transient 5xx")


def test_failed_write_does_not_consume_cap(ctx):
    ctx.cfg["writes"] = {"caps": {"open_issue": 1}}
    w = ctx.writes
    req = WriteRequest("open_issue", "alpha-core", {"title": "t", "body": "b"})
    with pytest.raises(RuntimeError, match="transient 5xx"):
        w.submit(req, _boom)
    # The failure is logged with ok=0, but the cap slot is still free.
    row = ctx.db.one("select ok from write_log where type='open_issue'")
    assert row["ok"] == 0
    assert w.counts.get("open_issue", 0) == 0
    assert w.total == 0
    w.submit(req, lambda: {"number": 1})
    assert w.counts["open_issue"] == 1


def test_label_setup_surfaces_real_errors(ctx, monkeypatch):
    def fail(type, **payload):
        raise GHError("forbidden", status=403)

    monkeypatch.setattr(ctx.writes, "tracker", fail)
    with pytest.raises(GHError):
        tracker.ensure_labels(ctx)
    assert not ctx.db.kv_get("labels_ready:v2")


def test_label_setup_tolerates_already_existing_labels(ctx, monkeypatch):
    def exists(type, **payload):
        raise GHError("Validation Failed (422): already exists", status=422)

    monkeypatch.setattr(ctx.writes, "tracker", exists)
    tracker.ensure_labels(ctx)
    assert ctx.db.kv_get("labels_ready:v2")


def test_label_setup_still_defers_on_cap(ctx, monkeypatch):
    def capped(type, **payload):
        raise Waiting("cap")

    monkeypatch.setattr(ctx.writes, "tracker", capped)
    tracker.ensure_labels(ctx)
    assert not ctx.db.kv_get("labels_ready:v2")
