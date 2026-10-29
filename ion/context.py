"""Run context: everything the pipeline touches, injectable for tests."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .collect.mirror import mirror_path as default_mirror_path
from .collect.mirror import remote_url as default_remote_url
from .config import Config
from .db import DB
from .ghclient import GH, make_gh
from .util import home


def default_bus_runner(argv: list[str], env: dict | None = None, input: str | None = None):
    import os
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run(argv, env=e, input=input, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


@dataclass
class Context:
    cfg: Config
    db: DB
    gh: GH | None = None
    llm: object | None = None
    bus_runner: object = default_bus_runner
    remote_url_fn: object | None = None
    mirror_path_fn: object | None = None
    work_root: Path | None = None
    sync_mirrors: bool = True
    writes: object = None
    _own_login: str | None = None

    def __post_init__(self):
        if self.gh is None:
            self.gh = make_gh(self.cfg)
        from .writes import Broker
        self.writes = Broker(self)

    def remote_url(self, repo: str) -> str:
        return (self.remote_url_fn or (lambda r: default_remote_url(self.cfg.org, r)))(repo)

    def mirror_path(self, repo: str) -> Path:
        return Path((self.mirror_path_fn or default_mirror_path)(repo))

    def work_dir(self, pid: str) -> Path:
        root = self.work_root or (home() / "work")
        return Path(root) / pid
