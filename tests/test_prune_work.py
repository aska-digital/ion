from pathlib import Path
from types import SimpleNamespace
from ion.cleanup import prune_work
from ion.cli import build_parser


class DB:
    def __init__(self, statuses): self.statuses = statuses
    def q(self, sql): return [{"id": k, "status": v} for k, v in self.statuses.items()]
    def one(self, sql, args): return {"status": self.statuses[args[0]]}


def test_prune_preview_and_apply_preserve_active_unknown_and_symlinks(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    statuses = {"P-1": "done", "P-2": "executing", "P-3": "failed", "P-4": "approved"}
    for name in [*statuses, "unknown"]:
        d = root / name
        d.mkdir()
        (d / "evidence").write_text("keep unless terminal")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "P-link").symlink_to(outside, target_is_directory=True)
    statuses["P-link"] = "dropped"
    ctx = SimpleNamespace(work_dir=lambda pid: root / pid, db=DB(statuses))
    assert prune_work(ctx)["candidates"] == ["P-1", "P-3"]
    assert (root / "P-1").exists()
    assert prune_work(ctx, apply=True)["removed"] == ["P-1", "P-3"]
    assert all((root / n).exists() for n in ["P-2", "P-4", "unknown", "P-link"])
    assert outside.exists()


def test_prune_is_preview_by_default():
    assert not build_parser().parse_args(["prune-work"]).apply
    assert build_parser().parse_args(["prune-work", "--apply"]).apply
