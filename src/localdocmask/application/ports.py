"""应用层端口（依赖倒置，见 module_contracts §3.1）。

应用层在此声明接口，基础设施层提供实现（如 SqliteTaskRepository）。
本文件目前只落地 TaskRepository；其余端口（ContentHandler / LLMClient /
OCREngine / AuditorPort / MapperPort）待对应应用服务接入时再补充。
"""
from __future__ import annotations

from typing import Protocol

from ..domain.entities import ClaimResult, Task


class TaskRepository(Protocol):
    """任务持久化端口。

    实现方（如 infrastructure/persistence/sqlite_repo.py 的
    SqliteTaskRepository）必须保证：
    - 不向库中写入完整敏感原文（条目值经 mask_display 处理）；
    - save 为 upsert（任务 + 条目 + 审计结论整体替换）；
    - load 返回的任务条目文本为掩码值，而非原文。
    """

    def save(self, task: Task) -> None:
        """upsert 任务及其条目、审计结论。"""
        ...

    def load(self, task_id: str) -> Task | None:
        """按 task_id 重建任务（含条目/审计概要）；不存在返回 None。"""
        ...

    def list_open(self) -> list[Task]:
        """列出未终结的任务（PENDING/PROCESSING/NEEDS_REVIEW/
        PROCESSED_UNAUDITED/INTERRUPTED）。"""
        ...

    def claim_or_get(self, task: Task, *, force: bool = False) -> ClaimResult:
        """事务化幂等声明；返回 created/reused/in_progress/stale/forced。"""
        ...

    def find_reusable(self, source_hash: str, mode: str, policy_version: str,
                      config_hash: str, tool_version: str, *,
                      exclude_task_id: str | None = None) -> Task | None:
        """命中 PASSED 且产物存在、SHA-256 与 final_hash 一致时返回任务。"""
        ...
