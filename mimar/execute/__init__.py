"""Executors: the only place that writes. Every one enforces the approval gate."""
from __future__ import annotations

from .base import Executor, Plan, Step, Waiting, check_gate  # noqa: F401
from .merge import MergeExecutor
from .simple import (ArchiveExecutor, HealthFixExecutor, MoveCodeExecutor, NewRepoExecutor, PolicyFixExecutor,
                     RenameExecutor)
from .split import SplitExecutor

REGISTRY = {c.kind: c for c in (MergeExecutor, SplitExecutor, ArchiveExecutor, RenameExecutor, MoveCodeExecutor,
                                PolicyFixExecutor, HealthFixExecutor, NewRepoExecutor)}


def executor_for(ctx, kind: str) -> Executor:
    return REGISTRY[kind](ctx)
