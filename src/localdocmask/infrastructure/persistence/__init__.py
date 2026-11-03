"""持久化层（ADR-005：标准库 SQLite，零新增依赖）。"""
from __future__ import annotations

from .recovery import RecoveryService, existing_success
from .sqlite_repo import (
    OPEN_STATUSES,
    SCHEMA_VERSION,
    SqliteTaskRepository,
    initialize_database,
    migrate,
)

__all__ = [
    "RecoveryService",
    "SqliteTaskRepository",
    "SCHEMA_VERSION",
    "OPEN_STATUSES",
    "existing_success",
    "initialize_database",
    "migrate",
]
