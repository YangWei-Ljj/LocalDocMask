"""显式任务状态机（T1）。

目标：
- 用一张显式转移表定义合法任务状态变化；
- 所有生产代码通过 transition_task 统一入口；
- 非法转移抛出 InvalidTaskTransition，任务保持原状态；
- PASSED / AUDIT_FAILED / UNSAFE 为不可自动回退的终态；
- 数据库未知状态一律归一为 INTERRUPTED（失败关闭），不得回退 PENDING。
"""
from __future__ import annotations

import getpass
import os

from .enums import TaskStatus
from .exceptions import InvalidTaskTransition

#: 合法转移表。终态不在表中出现任何“离开”目标。
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({
        TaskStatus.PROCESSING,
        TaskStatus.NEEDS_REVIEW,
        TaskStatus.PROCESSED_UNAUDITED,
        TaskStatus.AUDIT_FAILED,
        TaskStatus.UNSAFE,
        TaskStatus.INTERRUPTED,  # PENDING claim 恢复：有产物时安全中断
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
    }),
    TaskStatus.PROCESSING: frozenset({
        TaskStatus.PENDING,             # 崩溃恢复：无产物，可安全重新入队
        TaskStatus.NEEDS_REVIEW,
        TaskStatus.PROCESSED_UNAUDITED,
        TaskStatus.AUDIT_FAILED,
        TaskStatus.UNSAFE,
        TaskStatus.INTERRUPTED,         # 崩溃恢复：有残留产物，不自动认定安全
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
    }),
    TaskStatus.NEEDS_REVIEW: frozenset({
        TaskStatus.PROCESSING,          # 复核后重新处理
        TaskStatus.PROCESSED_UNAUDITED,
        TaskStatus.AUDIT_FAILED,
        TaskStatus.UNSAFE,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
    }),
    TaskStatus.PROCESSED_UNAUDITED: frozenset({
        TaskStatus.PROCESSING,          # 重新处理
        TaskStatus.NEEDS_REVIEW,        # 复核门禁
        TaskStatus.PASSED,
        TaskStatus.AUDIT_FAILED,
        TaskStatus.UNSAFE,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
    }),
    TaskStatus.INTERRUPTED: frozenset({
        TaskStatus.PENDING,             # 仅允许显式重新入队，不允许直接 PASSED
    }),
    TaskStatus.PASSED: frozenset(),
    TaskStatus.AUDIT_FAILED: frozenset(),
    TaskStatus.UNSAFE: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.TIMED_OUT: frozenset(),
}

#: 不可自动回退的终态。
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.PASSED,
    TaskStatus.AUDIT_FAILED,
    TaskStatus.UNSAFE,
    TaskStatus.CANCELLED,
    TaskStatus.TIMED_OUT,
})


def coerce_task_status(value) -> TaskStatus:
    """把数据库/外部值归一为 TaskStatus。

    - 已知的 name（如 "PASSED"）或 value（如 "审计通过，可以导出"）正常返回；
    - 未知、空值、未来版本状态一律返回 INTERRUPTED。
    """
    if isinstance(value, TaskStatus):
        return value
    if value is None:
        return TaskStatus.INTERRUPTED
    text = str(value).strip()
    if not text:
        return TaskStatus.INTERRUPTED
    try:
        return TaskStatus[text.upper()]
    except KeyError:
        pass
    try:
        return TaskStatus(text)
    except ValueError:
        return TaskStatus.INTERRUPTED


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL_STATUSES


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    """判断转移是否合法；同状态视为幂等 no-op。"""
    if not isinstance(current, TaskStatus) or not isinstance(target, TaskStatus):
        return False
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def ensure_transition_allowed(current: TaskStatus, target: TaskStatus,
                              reason: str = "") -> None:
    """只校验不修改；非法时抛 InvalidTaskTransition。"""
    if not can_transition(current, target):
        raise InvalidTaskTransition(current, target, reason)


def transition_task(task, target: TaskStatus, *, reason: str = "",
                    actor: str = ""):
    """统一任务状态转移入口。

    - 非法转移：抛 InvalidTaskTransition，task.status 保持原值；
    - 同状态：幂等 no-op，返回原任务；
    - 合法：修改 task.status 并返回原任务。
    actor 预留给后续事件/审计接线（T3），当前不落盘。
    """
    current = task.status
    if not isinstance(current, TaskStatus):
        # 内存中的未知状态同样失败关闭：不可猜测为 PENDING
        current = coerce_task_status(current)
        task.status = current
    ensure_transition_allowed(current, target, reason)
    if current != target:
        task.status = target
    return task


def current_actor() -> str:
    """当前本地操作者：Windows 用户；取不到时使用明确占位，不随机伪造。"""
    try:
        name = getpass.getuser()
    except Exception:
        name = os.environ.get("USERNAME", "")
    return name.strip() or "unknown-local-user"
