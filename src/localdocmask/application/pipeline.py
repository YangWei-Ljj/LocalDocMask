"""任务管理器：逐项处理 → 失败删除 → 验证 → 最终审计 → 成功/隔离。

集成后（任务三）：
- 兼容旧构造 ``TaskManager(detector)``：不接入识别/持久化/映射，行为与既往一致；
- 组合根注入 ``recognition/repository/settings/auditor`` 后启用完整能力：
  scan 阶段做三档裁决（AUTO_CONFIRM/NEEDS_REVIEW/SUPPRESS），
  process 仅处理已确认候选、可注入批次共享 mapper，
  finalize 增加"复核门禁"，任务/条目/审计落 SQLite。
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import random
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from ..security.auditor import Auditor
from .. import __version__
from ..infrastructure.config.settings import (
    DATA_DIR, Settings, PROCESSING_DIR, SUCCESS_DIR, QUARANTINE_DIR,
    compute_config_hash as compute_settings_hash, ensure_dirs,
)
from ..infrastructure.formats.registry import handler_for, supported_extensions
from ..infrastructure.logging.logger import get_logger, register_sensitive_values
from ..security.hashing import sha256_file
from ..security.admission import AdmissionError, FileAdmission
from ..security.atomic_io import (
    AtomicExportError, atomic_copy, atomic_copy_user_file,
    atomic_write_bytes, quarantine_unconfirmed,
)
from ..security.limits import ResourceLimits
from ..security.path_safety import is_within, safe_join
from ..security.temp_lifecycle import TempLifecycle, TempLifecycleError
from ..security.redaction.masker import Masker, mask_display
from .process_guard import GuardedWorker
from ..domain.entities import AuditReport, ItemStatus, ScanResult, Task, TaskEvent
from ..domain.enums import AuditVerdict, EntityDecision, TaskStatus
from ..domain.exceptions import (
    ErrorCode, InvalidTaskTransition, RetryExhaustedError,
    SecurityViolation, TaskCancelledError, TaskTimedOutError,
)
from ..domain.state_machine import (
    can_transition, current_actor, ensure_transition_allowed, transition_task,
)

_log = get_logger("pipeline")

#: 策略版本：参与幂等键（源哈希 + 模式 + 策略版本）。策略语义变更时递增。
class ExportRecoveryError(SecurityViolation):
    """用户文件覆盖恢复失败；备份必须保留供人工恢复。"""


POLICY_VERSION = "recognition-v1"
TOOL_VERSION = __version__

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_PHASE_LIMIT_ATTRS = {
    "admission": "admission_timeout_seconds",
    "scan": "scan_timeout_seconds",
    "process": "process_timeout_seconds",
    "audit": "audit_timeout_seconds",
    "finalize": "finalize_timeout_seconds",
}


class CancelToken:
    """线程安全的协作式取消标志；不会强杀第三方库调用。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self.reason = "user_requested"

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "user_requested") -> None:
        with self._lock:
            if not self._event.is_set():
                self.reason = reason or "user_requested"
                self._event.set()


@dataclass
class TaskExecutionContext:
    """单个任务的独立预算与取消上下文，可安全跨线程传递。"""

    cancel_token: CancelToken
    task_timeout_seconds: float
    phase_budgets: dict[str, float] = field(default_factory=dict)
    task_id: str = ""
    _started_at: float = field(default_factory=time.monotonic, init=False, repr=False)
    _task_deadline: float = field(init=False, repr=False)
    _phase_deadlines: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    artifacts: set[Path] = field(default_factory=set, init=False, repr=False)
    _hard_worker: GuardedWorker | None = field(default=None, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.task_timeout_seconds = float(self.task_timeout_seconds)
        if self.task_timeout_seconds <= 0:
            raise ValueError("task_timeout_seconds 必须大于 0")
        self._task_deadline = self._started_at + self.task_timeout_seconds

    @classmethod
    def from_limits(cls, limits: ResourceLimits,
                    cancel_token: CancelToken | None = None) -> "TaskExecutionContext":
        budgets = {
            stage: float(getattr(limits, attr))
            for stage, attr in _PHASE_LIMIT_ATTRS.items()
        }
        return cls(cancel_token=cancel_token or CancelToken(),
                   task_timeout_seconds=float(limits.task_timeout_seconds),
                   phase_budgets=budgets)

    def bind_task(self, task_id: str) -> None:
        with self._lock:
            self.task_id = task_id or self.task_id

    def begin_stage(self, stage: str) -> None:
        self.check(None)
        self.arm_phase_deadline(stage)

    def arm_phase_deadline(self, stage: str) -> None:
        """从当前时刻起算阶段预算，仍受任务总截止时间约束。"""
        now = time.monotonic()
        budget = self.phase_budgets.get(stage)
        deadline = self._task_deadline
        if budget is not None:
            deadline = min(deadline, now + max(0.0, float(budget)))
        with self._lock:
            self._phase_deadlines[stage] = deadline

    def extend_phase_deadline(self, stage: str, seconds: float) -> None:
        """补偿 worker 启动/反序列化耗时，不重置阶段总预算。"""
        delta = max(0.0, float(seconds))
        with self._lock:
            current = self._phase_deadlines.get(stage, self._task_deadline)
            self._phase_deadlines[stage] = min(
                self._task_deadline, current + delta)

    def check(self, stage: str | None = None) -> None:
        if self.cancel_token.cancelled:
            raise TaskCancelledError(stage or "", self.cancel_token.reason)
        now = time.monotonic()
        if now >= self._task_deadline:
            raise TaskTimedOutError(stage or "", "task", self.task_timeout_seconds)
        if stage:
            with self._lock:
                deadline = self._phase_deadlines.get(stage)
                budget = self.phase_budgets.get(stage, 0.0)
            if deadline is not None and now >= deadline:
                raise TaskTimedOutError(stage, "phase", float(budget))

    def track_artifact(self, path: str | Path) -> None:
        with self._lock:
            self.artifacts.add(Path(path))

    def untrack_artifact(self, path: str | Path) -> None:
        with self._lock:
            self.artifacts.discard(Path(path))

    def elapsed_ms(self, started: float) -> int:
        return max(0, int((time.monotonic() - started) * 1000))

    def get_hard_worker(self) -> GuardedWorker:
        if self._hard_worker is None or self._hard_worker.closed:
            self._hard_worker = GuardedWorker()
        return self._hard_worker

    def close_hard_worker(self) -> None:
        worker = self._hard_worker
        self._hard_worker = None
        if worker is not None:
            worker.close()

    def __del__(self):
        try:
            self.close_hard_worker()
        except Exception:
            pass

    def task_remaining_seconds(self) -> float:
        return max(0.0, self._task_deadline - time.monotonic())

    def hard_timeout(self, stage: str) -> tuple[float, str, float]:
        """返回阶段操作的剩余硬预算、超时范围和配置预算。"""
        now = time.monotonic()
        task_remaining = max(0.0, self._task_deadline - now)
        with self._lock:
            phase_deadline = self._phase_deadlines.get(stage)
            phase_budget = float(self.phase_budgets.get(stage, 0.0))
        if phase_deadline is None:
            return task_remaining, "task", self.task_timeout_seconds
        phase_remaining = max(0.0, phase_deadline - now)
        if task_remaining <= phase_remaining:
            return task_remaining, "task", self.task_timeout_seconds
        return phase_remaining, "phase", phase_budget


def compute_config_hash(settings: Settings | None = None) -> str:
    """兼容入口：确定性配置哈希由配置基础设施统一实现。"""
    return compute_settings_hash(settings)


class TaskManager:
    def __init__(self, detector=None, use_ollama: bool = False, ollama_model: str = "", *,
                 recognition=None, repository=None, settings=None, auditor=None,
                 metrics=None, health=None, temp_lifecycle=None):
        # 旧路径：detector 为位置参数；新路径：recognition 自带预加载 detector
        self.detector = detector
        self.recognition = recognition
        self.repository = repository
        self.settings = settings
        self.auditor = auditor or Auditor()
        self.metrics = metrics
        self.health = health
        self.temp_lifecycle = temp_lifecycle
        self.use_ollama = use_ollama
        self.ollama_model = ollama_model
        self.limits = ResourceLimits.from_settings(settings)
        self._timeout_seconds = float(self.limits.task_timeout_seconds)
        # 仅为旧测试/旧调用保留；生产任务使用 TaskExecutionContext。
        self._deadline: float | None = None
        if recognition is not None:
            # 组合根路径：格式处理器用预加载词库的 Detector 定位带 box 的候选
            self.detector = recognition.detector

    def _metric(self, metric: str, value: float = 1, *, stage: str = "",
                code: str = "", outcome: str = "") -> None:
        if self.metrics is None:
            return
        try:
            self.metrics.record(
                metric, value, stage=stage, code=code, outcome=outcome)
        except Exception:
            if self.health is not None:
                self.health.mark_degraded("metrics", "metrics_write_failed")

    def _register_temp(self, task_id: str, path: Path, stage: str) -> None:
        if self.temp_lifecycle is not None:
            self.temp_lifecycle.register(task_id, path, stage)

    def quarantine_task_artifacts(self, task: Task, *,
                                  reason: str = "failure") -> None:
        """失败关闭时把本任务受控临时产物移入 quarantine。"""
        context = getattr(task, "_execution_context", None)
        errors = []
        try:
            self._move_to_quarantine(task)
        except Exception as exc:
            errors.append(exc)
        if context is not None:
            for artifact in getattr(context, "artifacts", set()):
                if not artifact.exists():
                    continue
                try:
                    quarantine_unconfirmed(
                        artifact, QUARANTINE_DIR,
                        self._safe_reason_code(reason))
                    if self.temp_lifecycle is not None:
                        self.temp_lifecycle.mark_path(
                            task.task_id, artifact, "quarantined")
                except Exception as exc:
                    errors.append(exc)
            context.artifacts.clear()
        if self.temp_lifecycle is not None:
            try:
                self.temp_lifecycle.mark_task_artifacts(
                    task.task_id, "quarantined")
            except Exception as exc:
                errors.append(exc)
        if errors:
            self._metric("quarantine_failed", 1, stage="cleanup",
                         code=type(errors[0]).__name__)
            raise TempLifecycleError("任务临时产物隔离失败") from errors[0]

    def _cleanup_processing_artifacts(self, task: Task,
                                      *artifacts: Path | None) -> None:
        """成功发布后、DB PASSED 前删除 processing 中已确认无用的中间文件。"""
        seen = set()
        for artifact in artifacts:
            if artifact is None or artifact in seen:
                continue
            seen.add(artifact)
            if not artifact.exists():
                if self.temp_lifecycle is not None:
                    self.temp_lifecycle.mark_path(
                        task.task_id, artifact, "cleaned")
                continue
            if not is_within(artifact, [PROCESSING_DIR]):
                # 测试/旧调用可能提供自建临时文件；T6 只清理受控 processing 根。
                continue
            artifact.unlink()
            if self.temp_lifecycle is not None:
                self.temp_lifecycle.mark_path(
                    task.task_id, artifact, "cleaned")

    @staticmethod
    def _safe_reason_code(reason: str) -> str:
        text = "".join(ch for ch in str(reason or "")
                       if ch.isascii() and (ch.isalnum() or ch in "_.-"))
        return text[:64] or "unspecified"

    def _check_deadline(self, context: TaskExecutionContext | None = None,
                        stage: str | None = None) -> None:
        """显式上下文守卫；无上下文时兼容旧的共享 deadline 单元测试。"""
        if context is not None:
            context.check(stage)
            return
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise TaskTimedOutError(stage or "", "task", self._timeout_seconds)

    def _context_for(self, task: Task,
                     context: TaskExecutionContext | None = None) -> TaskExecutionContext:
        ctx = context or getattr(task, "_execution_context", None)
        if ctx is None:
            ctx = TaskExecutionContext.from_limits(self.limits)
            setattr(task, "_execution_context", ctx)
        ctx.bind_task(task.task_id)
        return ctx

    def get_execution_context(self, task: Task) -> TaskExecutionContext:
        return self._context_for(task)

    @staticmethod
    def _is_test_injected_operation(operation) -> bool:
        if os.environ.get(
                "LOCALDOCMASK_TEST_ALLOW_INPROCESS_HANDLERS") != "1":
            return False
        module = getattr(operation, "__module__", "")
        # FIX-06 专项测试显式使用顶层、可跨 spawn 的 handler，必须走硬超时。
        return not module.startswith("test_hard_timeout")

    def _run_hard_timeout_operation(self, operation, args: tuple, *,
                                    stage: str,
                                    context: TaskExecutionContext):
        if self._is_test_injected_operation(operation):
            # 旧测试注入局部闭包/对象，Windows spawn 无法序列化。该回退只在
            # 显式测试开关存在时启用；生产环境不设置开关，无法序列化即失败关闭。
            context.check(stage)
            return operation(*args)
        timeout_seconds, scope, budget = context.hard_timeout(stage)
        started = time.monotonic()
        return context.get_hard_worker().run(
            operation, args,
            timeout_seconds=timeout_seconds,
            stage=stage,
            timeout_scope=scope,
            timeout_budget=budget,
            cancel_token=context.cancel_token,
            startup_timeout_seconds=min(
                30.0, max(1.0, context.task_remaining_seconds())),
            on_ready=lambda: context.extend_phase_deadline(
                stage, time.monotonic() - started),
        )

    def is_retryable_error(self, exc: Exception) -> bool:
        if isinstance(exc, (
            TaskCancelledError, TaskTimedOutError, InvalidTaskTransition,
            SecurityViolation, RetryExhaustedError,
        )):
            return False
        if isinstance(exc, sqlite3.OperationalError):
            text = str(exc).lower()
            return ("locked" in text or "busy" in text)
        if type(exc).__name__ == "OllamaError":
            text = str(exc).lower()
            return ("无法连接本地" in text or " 429" in text
                    or any(f" {code}" in text for code in range(500, 600)))
        if isinstance(exc, OSError):
            return exc.errno in {
                errno.EAGAIN, errno.EBUSY, errno.EINTR, errno.EIO, errno.ETIMEDOUT,
            }
        return False

    def retry_delay(self, failed_attempt: int) -> float:
        base = max(0.0, float(self.limits.retry_base_delay_seconds))
        capped = min(max(0.0, float(self.limits.retry_max_delay_seconds)),
                     base * (2 ** max(0, failed_attempt - 1)))
        return random.uniform(capped * 0.5, capped) if capped else 0.0

    def save_task_with_retry(self, task: Task, *, operation=None, events=None,
                             release_claim: bool = False,
                             stage: str = "persistence") -> None:
        self._persist_with_retry(
            task, operation=operation, events=events,
            release_claim=release_claim, stage=stage)

    def _persist_with_retry(self, task: Task, *, operation=None, events=None,
                            release_claim: bool = False,
                            stage: str = "persistence") -> None:
        if self.repository is None:
            return
        base_events = list(events or [])
        failures: list[TaskEvent] = []
        max_attempts = max(1, int(self.limits.retry_max_attempts))
        current_status = getattr(task.status, "name", str(task.status))
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            extra = list(failures)
            if attempt > 1:
                extra.append(TaskEvent(
                    event_code="task.retry.attempt", stage=stage,
                    from_status=current_status, to_status=current_status,
                    actor=current_actor(),
                    detail=f"attempt={attempt};result=success"))
            try:
                self.repository.save_task(
                    task, operation=operation,
                    events=[*base_events, *extra],
                    release_claim=release_claim)
                return
            except Exception as exc:
                last_error = exc
                if not self.is_retryable_error(exc):
                    try:
                        setattr(exc, "retry_events", list(failures))
                        setattr(exc, "task", task)
                    except Exception:
                        pass
                    raise
                failures.append(TaskEvent(
                    event_code="task.retry.attempt", stage=stage,
                    from_status=current_status, to_status=current_status,
                    actor=current_actor(), error_code=type(exc).__name__,
                    detail=f"attempt={attempt};result=failed;error={type(exc).__name__}"))
                self._metric("retry_attempt", 1, stage=stage,
                             code=type(exc).__name__, outcome="failed")
                if attempt >= max_attempts:
                    self._metric("retry_exhausted", 1, stage=stage,
                                 code=type(exc).__name__)
                    exhausted = RetryExhaustedError(attempt, exc)
                    setattr(exhausted, "retry_events", list(failures))
                    setattr(exhausted, "task", task)
                    raise exhausted from exc
                time.sleep(self.retry_delay(attempt))

    def _retry_operation(self, func, *, task: Task, stage: str,
                         context: TaskExecutionContext,
                         cleanup=None):
        failures: list[TaskEvent] = []
        max_attempts = max(1, int(self.limits.retry_max_attempts))
        current_status = getattr(task.status, "name", str(task.status))
        for attempt in range(1, max_attempts + 1):
            context.check(stage)
            try:
                result = func()
                if attempt > 1:
                    task.pending_events.extend(failures)
                    task.pending_events.append(TaskEvent(
                        event_code="task.retry.attempt", stage=stage,
                        from_status=current_status, to_status=current_status,
                        actor=current_actor(),
                        detail=f"attempt={attempt};result=success"))
                return result
            except Exception as exc:
                if not self.is_retryable_error(exc):
                    task.pending_events.extend(failures)
                    try:
                        setattr(exc, "retry_events", list(failures))
                        setattr(exc, "task", task)
                    except Exception:
                        pass
                    raise
                failures.append(TaskEvent(
                    event_code="task.retry.attempt", stage=stage,
                    from_status=current_status, to_status=current_status,
                    actor=current_actor(), error_code=type(exc).__name__,
                    detail=f"attempt={attempt};result=failed;error={type(exc).__name__}"))
                self._metric("retry_attempt", 1, stage=stage,
                             code=type(exc).__name__, outcome="failed")
                if cleanup is not None:
                    cleanup()
                if attempt >= max_attempts:
                    self._metric("retry_exhausted", 1, stage=stage,
                                 code=type(exc).__name__)
                    exhausted = RetryExhaustedError(attempt, exc)
                    setattr(exhausted, "retry_events", list(failures))
                    setattr(exhausted, "task", task)
                    raise exhausted from exc
                time.sleep(self.retry_delay(attempt))

    def _append_retry_exhaustion(self, task: Task, exc: Exception,
                                 stage: str) -> None:
        if not isinstance(exc, RetryExhaustedError):
            return
        if getattr(exc, "_retry_events_queued", False):
            return
        task.pending_events.extend(getattr(exc, "retry_events", []) or [])
        current = task.status.name
        task.pending_events.append(TaskEvent(
            event_code="task.retry.exhausted", stage=stage,
            from_status=current, to_status=current,
            actor=current_actor(), error_code=exc.code.value,
            detail=f"attempts={exc.attempts};result=exhausted"))
        setattr(exc, "_retry_events_queued", True)

    def _finish_control(self, task: Task, exc: Exception, *, stage: str,
                        started: float, context: TaskExecutionContext) -> None:
        if not isinstance(exc, (TaskCancelledError, TaskTimedOutError)):
            return
        if task.status in (
            TaskStatus.PASSED, TaskStatus.AUDIT_FAILED, TaskStatus.UNSAFE,
            TaskStatus.CANCELLED, TaskStatus.TIMED_OUT,
        ):
            return
        target = (TaskStatus.CANCELLED if isinstance(exc, TaskCancelledError)
                  else TaskStatus.TIMED_OUT)
        if isinstance(exc, TaskCancelledError):
            raw_reason = str(exc.reason)
            reason = "".join(
                ch for ch in raw_reason
                if ch.isascii() and (ch.isalnum() or ch in "_.-")
            )[:64] or "unspecified"
        else:
            reason = f"scope={exc.scope};budget={exc.budget:.3f}s"
        task.error = f"{exc.code.value}: stage={stage};reason={reason}"
        cleanup_errors = []
        if task.output_path is not None and task.output_path.exists():
            context.untrack_artifact(task.output_path)
        if task.report_path is not None and task.report_path.exists():
            context.untrack_artifact(task.report_path)
        try:
            self._move_to_quarantine(task)
        except Exception as cleanup_exc:
            cleanup_errors.append(cleanup_exc)
        for artifact in sorted(context.artifacts):
            if not artifact.exists():
                continue
            try:
                quarantine_unconfirmed(
                    artifact, QUARANTINE_DIR,
                    "cancelled" if target == TaskStatus.CANCELLED else "timed_out")
                if self.temp_lifecycle is not None:
                    self.temp_lifecycle.mark_path(
                        task.task_id, artifact, "quarantined")
                self._metric("quarantine", 1, stage=stage,
                             code="cancelled_temp" if target == TaskStatus.CANCELLED else "timeout_temp")
            except Exception as cleanup_exc:
                cleanup_errors.append(cleanup_exc)
        context.artifacts.clear()
        if self.temp_lifecycle is not None:
            try:
                self.temp_lifecycle.mark_task_artifacts(
                    task.task_id, "quarantined")
            except Exception as cleanup_exc:
                cleanup_errors.append(cleanup_exc)
        context.close_hard_worker()
        if cleanup_errors:
            task.error += f"；清理失败：{type(cleanup_errors[0]).__name__}"
        event_code = ("task.cancelled" if target == TaskStatus.CANCELLED
                      else "task.timed_out")
        try:
            self._transition(
                task, target, stage=stage, event_code=event_code,
                detail=f"stage={stage};reason={reason}",
                error_code=exc.code.value,
                operation=("cancel" if target == TaskStatus.CANCELLED else "timeout",
                           f"stage={stage};reason={reason}"),
                duration_ms=context.elapsed_ms(started))
            self._metric(
                "task_cancelled" if target == TaskStatus.CANCELLED else "task_timed_out",
                1, stage=stage, code=exc.code.value)
        except Exception as persist_exc:
            setattr(exc, "persistence_error", persist_exc)

    def mark_task_cancelled(self, task: Task, *, stage: str = "queue",
                            reason: str = "user_requested",
                            context: TaskExecutionContext | None = None) -> None:
        ctx = self._context_for(task, context)
        started = ctx._started_at
        self._finish_control(
            task, TaskCancelledError(stage, reason),
            stage=stage, started=started, context=ctx)

    def mark_task_timed_out(self, task: Task, *, stage: str = "queue",
                            scope: str = "task", budget: float = 0.0,
                            context: TaskExecutionContext | None = None) -> None:
        ctx = self._context_for(task, context)
        self._finish_control(
            task, TaskTimedOutError(stage, scope, budget),
            stage=stage, started=ctx._started_at, context=ctx)

    def _op(self, task: Task, action: str, detail: str) -> None:
        """写操作日志；detail 仅阶段/计数/状态，绝不写原文。"""
        if self.repository is None:
            return
        try:
            self.repository.append_operation(task.task_id, action, detail)
        except Exception:
            pass

    def _transition(self, task: Task, target: TaskStatus, *, stage: str,
                    event_code: str, detail: str = "", error_code: str = "",
                    operation: tuple[str, str] | None = None,
                    duration_ms: int = 0,
                    additional_events: list[TaskEvent] | None = None) -> None:
        """状态变更 + 事件写入同一事务；失败时内存状态也保持不变。"""
        current = task.status
        try:
            ensure_transition_allowed(current, target, detail)
        except InvalidTaskTransition:
            if self.repository is not None:
                rejected = TaskEvent(
                    event_code="task.transition.rejected", stage=stage,
                    from_status=getattr(current, "name", str(current)),
                    to_status=getattr(target, "name", str(target)),
                    error_code="invalid_transition",
                    detail=f"{getattr(current, 'name', current)}->{getattr(target, 'name', target)}")
                try:
                    self._persist_with_retry(
                        task, events=[rejected], stage=stage)
                except Exception:
                    pass
            raise
        event = TaskEvent(
            event_code=event_code, stage=stage,
            from_status=getattr(current, "name", str(current)),
            to_status=getattr(target, "name", str(target)),
            actor=current_actor(), duration_ms=max(0, int(duration_ms)),
            detail=detail, error_code=error_code)
        if self.repository is None:
            transition_task(task, target, reason=detail)
            return
        # 用副本持久化目标状态；save_task 失败不会污染内存任务
        candidate = replace(task, status=target)
        self._persist_with_retry(
            candidate, operation=operation,
            events=[event, *(additional_events or [])], stage=stage)
        transition_task(task, target, reason=detail)
        if event_code.endswith((".end", ".failed")):
            self._metric(
                "stage_duration_ms", max(0, int(duration_ms)),
                stage=stage,
                outcome=("success" if event_code.endswith(".end") else "failed"))

    def _record_phase(self, task: Task, event_code: str, stage: str,
                      *, detail: str = "", error_code: str = "",
                      duration_ms: int = 0) -> None:
        """记录不改状态的任务阶段事件（start/end/failed）。"""
        if self.repository is None:
            return
        current = getattr(task.status, "name", str(task.status))
        event = TaskEvent(
            event_code=event_code, stage=stage, from_status=current,
            to_status=current, actor=current_actor(),
            duration_ms=max(0, int(duration_ms)), detail=detail,
            error_code=error_code)
        self._persist_with_retry(task, events=[event], stage=stage)
        if event_code.endswith((".end", ".failed")):
            self._metric(
                "stage_duration_ms", max(0, int(duration_ms)),
                stage=stage,
                outcome=("success" if event_code.endswith(".end") else "failed"))
        if event_code == "phase.audit.failed":
            self._metric("audit_failed", 1, stage=stage, code=error_code)

    def supported(self) -> list[str]:
        return supported_extensions()

    def prepare(self, source_path: str | Path, mode: str | None = None, *,
                cancel_token: CancelToken | None = None,
                execution_context: TaskExecutionContext | None = None) -> Task:
        prepare_started = time.monotonic()
        ensure_dirs()
        p = Path(source_path)
        # 所有存在性/扩展名/真实类型判断统一交给 admission；
        # 此处不得先访问 UNC、junction 或不可信路径。
        task = Task(source_path=p)
        task.task_id = uuid.uuid4().hex[:12]
        context = execution_context or TaskExecutionContext.from_limits(
            self.limits, cancel_token)
        context.bind_task(task.task_id)
        setattr(task, "_execution_context", context)
        self._metric("task_started", 1, stage="prepare")
        task.pending_events.append(TaskEvent(
            event_code="phase.prepare.start", stage="prepare",
            from_status=TaskStatus.PENDING.name, to_status=TaskStatus.PENDING.name,
            actor=current_actor(),
            occurred_at=datetime.now(timezone.utc).isoformat(timespec="seconds")))
        admission_started = time.monotonic()
        task.pending_events.append(TaskEvent(
            event_code="phase.admission.start", stage="admission",
            from_status=TaskStatus.PENDING.name, to_status=TaskStatus.PENDING.name,
            actor=current_actor(), detail="admission_start"))
        try:
            context.begin_stage("admission")
            context.check("admission")
            admission = FileAdmission(self.settings).admit(p)
            context.check("admission")
        except (TaskCancelledError, TaskTimedOutError) as exc:
            exc.task = task
            self._finish_control(
                task, exc, stage="admission", started=prepare_started,
                context=context)
            raise
        except AdmissionError as exc:
            exc.task = task
            task.error = f"{exc.reason_code}: {exc}"
            self._metric("task_failed", 1, stage="admission",
                         code=exc.reason_code)
            if self.repository is not None:
                self._transition(
                    task, TaskStatus.AUDIT_FAILED, stage="admission",
                    event_code="phase.admission.failed",
                    detail=f"reason={exc.reason_code}",
                    error_code=exc.reason_code,
                    operation=("admission", exc.reason_code))
            raise
        self._metric(
            "stage_duration_ms", int((time.monotonic() - admission_started) * 1000),
            stage="admission", outcome="success")
        task.file_type = admission.real_type
        task.source_hash = admission.source_hash
        task.pending_events.append(TaskEvent(
            event_code="phase.admission.end", stage="admission",
            from_status=TaskStatus.PENDING.name, to_status=TaskStatus.PENDING.name,
            actor=current_actor(),
            detail=(f"type={admission.real_type};size={admission.size};"
                    f"pages={admission.page_count};pixels={admission.pixel_count}"),
            duration_ms=int((time.monotonic() - admission_started) * 1000)))
        task.policy_version = POLICY_VERSION
        task.config_hash = compute_config_hash(self.settings)
        task.tool_version = TOOL_VERSION
        if mode:
            task.mode = mode
        task._reusable = None
        if self.repository is not None and task.mode:
            finder = getattr(self.repository, "find_reusable", None)
            if finder is not None:
                prior = finder(
                    task.source_hash, task.mode, task.policy_version,
                    task.config_hash, task.tool_version)
                if prior is not None:
                    task._reusable = prior
        self._op(task, "prepare", f"file_type={task.file_type}")
        task.pending_events.append(TaskEvent(
            event_code="phase.prepare.end", stage="prepare",
            from_status=TaskStatus.PENDING.name, to_status=TaskStatus.PENDING.name,
            actor=current_actor(), detail=f"file_type={task.file_type}",
            duration_ms=int((time.monotonic() - prepare_started) * 1000)))
        return task

    @staticmethod
    def _candidate_final_action(candidate: Candidate) -> str:
        if candidate.excluded:
            return "exclude"
        if candidate.confirmed:
            return "redact"
        return "pending"

    @staticmethod
    def _assign_candidate_identities(scan: ScanResult) -> None:
        ordinals: dict[tuple[str, str, str], int] = {}
        for candidate in scan.candidates:
            masked = mask_display(candidate.text, candidate.category)
            candidate.masked_value = masked
            candidate.original_fingerprint = hashlib.sha256(
                f"{candidate.category}\x00{candidate.text}".encode("utf-8")
            ).hexdigest()
            decision = getattr(candidate.decision, "value", candidate.decision)
            if not candidate.suggested_action:
                candidate.suggested_action = str(decision)
            candidate.final_action = TaskManager._candidate_final_action(candidate)
            key = (candidate.category or "", masked, candidate.location or "")
            ordinal = ordinals.get(key, 0)
            ordinals[key] = ordinal + 1
            stable = f"candidate-v1|{key[0]}|{key[1]}|{key[2]}|{ordinal}"
            candidate.candidate_id = hashlib.sha256(
                stable.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _merge_review_state(scan: ScanResult,
                            previous: ScanResult | None) -> bool:
        if previous is None or not previous.candidates:
            return False
        previous_by_id: dict[str, Candidate] = {}
        duplicate_ids = set()
        for candidate in previous.candidates:
            cid = candidate.candidate_id or ""
            if not cid or cid in previous_by_id:
                duplicate_ids.add(cid)
                continue
            previous_by_id[cid] = candidate
        uncertain = bool(duplicate_ids)
        matched = 0
        for candidate in scan.candidates:
            old = previous_by_id.get(candidate.candidate_id)
            if old is None:
                uncertain = True
                candidate.confirmed = False
                candidate.excluded = False
                candidate.decision = EntityDecision.NEEDS_REVIEW
                candidate.final_action = "pending"
                continue
            if (not old.original_fingerprint
                    or candidate.original_fingerprint != old.original_fingerprint):
                uncertain = True
                candidate.confirmed = False
                candidate.excluded = False
                candidate.decision = EntityDecision.NEEDS_REVIEW
                candidate.final_action = "pending"
                continue
            matched += 1
            candidate.confirmed = bool(old.confirmed)
            candidate.excluded = bool(old.excluded)
            candidate.decision = old.decision
            candidate.replacement = old.replacement
            candidate.final_action = old.final_action or TaskManager._candidate_final_action(old)
            candidate.review_actor = old.review_actor
            candidate.reviewed_at = old.reviewed_at
            candidate.review_reason_code = old.review_reason_code
            candidate.status = old.status
            candidate.failure_reason = old.failure_reason
        if matched != len(previous.candidates):
            uncertain = True
        if uncertain and "review_mapping_uncertain" not in scan.review_flags:
            scan.review_flags.append("review_mapping_uncertain")
        return uncertain

    def update_candidate_review(self, task: Task, candidate_id: str, *,
                                confirmed: bool, excluded: bool = False,
                                replacement: str = "",
                                reason_code: str = "manual_review") -> Candidate:
        """持久化单个候选复核结果；原文只存在内存，事件只记录元信息。"""
        if task.status in (TaskStatus.PASSED, TaskStatus.AUDIT_FAILED,
                           TaskStatus.UNSAFE, TaskStatus.CANCELLED,
                           TaskStatus.TIMED_OUT):
            raise InvalidTaskTransition(
                task.status, TaskStatus.NEEDS_REVIEW, "terminal_review_update")
        if task.status not in (TaskStatus.NEEDS_REVIEW,
                               TaskStatus.PROCESSED_UNAUDITED):
            raise SecurityViolation("当前任务状态不允许修改复核结果")
        candidates = list(task.scan.candidates if task.scan else [])
        matches = [c for c in candidates if c.candidate_id == candidate_id]
        if len(matches) != 1:
            raise SecurityViolation("候选 ID 缺失或不唯一，禁止猜测复核目标")
        candidate = matches[0]
        replacement = (replacement or "").strip()
        if len(replacement) > 512 or "\x00" in replacement or "{\"" in replacement:
            raise SecurityViolation("替换值格式不安全")
        raw_known = bool(candidate.text and (
            not candidate.masked_value or candidate.text != candidate.masked_value))
        if raw_known and replacement and candidate.text in replacement:
            raise SecurityViolation("替换值不得包含原始敏感值")
        if replacement and candidate.original_fingerprint:
            replacement_fp = hashlib.sha256(
                f"{candidate.category}\x00{replacement}".encode("utf-8")
            ).hexdigest()
            if replacement_fp == candidate.original_fingerprint:
                raise SecurityViolation("替换值不得等于原始敏感值")
        if confirmed and excluded:
            raise SecurityViolation("候选不能同时确认和排除")
        old_values = {name: getattr(candidate, name) for name in (
            "confirmed", "excluded", "decision", "replacement",
            "final_action", "review_actor", "reviewed_at",
            "review_reason_code")}
        old_audit = task.audit
        old_error = task.error
        old_records = list(task.records)
        old_review_flags = list(task.scan.review_flags if task.scan else [])
        candidate.confirmed = bool(confirmed)
        candidate.excluded = bool(excluded)
        if excluded:
            candidate.decision = EntityDecision.DISMISSED
            candidate.final_action = "exclude"
        elif confirmed:
            candidate.decision = EntityDecision.CONFIRMED
            candidate.final_action = "redact"
        else:
            candidate.decision = EntityDecision.NEEDS_REVIEW
            candidate.final_action = "pending"
        candidate.replacement = replacement
        candidate.review_actor = current_actor()
        candidate.reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        candidate.review_reason_code = self._safe_reason_code(reason_code)
        task.audit = None
        task.records = []
        if task.scan and "review_mapping_uncertain" in task.scan.review_flags:
            if all(c.reviewed_at for c in candidates):
                task.scan.review_flags = [
                    flag for flag in task.scan.review_flags
                    if flag != "review_mapping_uncertain"]
        detail = (f"candidate={candidate_id};decision={candidate.decision.value};"
                  f"confirmed={int(candidate.confirmed)};"
                  f"excluded={int(candidate.excluded)};"
                  f"reason={candidate.review_reason_code}")
        try:
            self._transition(
                task, TaskStatus.NEEDS_REVIEW, stage="review",
                event_code="task.review.candidate_updated", detail=detail,
                error_code="", operation=("review_update", detail))
            self._metric("review_update", 1, stage="review",
                         outcome=candidate.final_action)
        except Exception:
            for name, value in old_values.items():
                setattr(candidate, name, value)
            task.audit = old_audit
            task.error = old_error
            task.records = old_records
            if task.scan is not None:
                task.scan.review_flags = old_review_flags
            raise
        return candidate

    def scan(self, task: Task, *,
             execution_context: TaskExecutionContext | None = None) -> ScanResult:
        context = self._context_for(task, execution_context)
        started = time.monotonic()
        try:
            context.begin_stage("scan")
            context.check("scan")
            previous_scan = task.scan
            self._transition(task, TaskStatus.PROCESSING, stage="scan",
                             event_code="phase.scan.start", detail="scan_start")
            handler = handler_for(task.source_path, self.detector)
            scan = self._run_hard_timeout_operation(
                handler.extract, (str(task.source_path),),
                stage="scan", context=context)
            context.check("scan")

            if self.recognition is not None:
                scan.candidates = self.recognition._llm_supplement(
                    scan.candidates, scan.text or "", location="")
                context.check("scan")
                if self.recognition.ner is not None:
                    target = self.recognition.ner_worker_target
                    if target is None:
                        raise RuntimeError("NER hard worker 未配置")
                    try:
                        pairs = self._run_hard_timeout_operation(
                            target,
                            (*self.recognition.ner_worker_args,
                             (scan.text or "")[:8000]),
                            stage="scan", context=context)
                    except TaskCancelledError:
                        raise
                    except TaskTimedOutError:
                        if self.health is not None:
                            self.health.mark_degraded(
                                "local_ner", "ner_inference_timeout")
                        raise
                    except Exception:
                        if self.health is not None:
                            self.health.mark_degraded(
                                "local_ner", "ner_inference_failed")
                        raise
                    scan.candidates = self.recognition.merge_ner_entities(
                        scan.candidates, pairs, location="")
                context.check("scan")
                scan.candidates = self.recognition.adjudicate(
                    scan.candidates, scan.text or "")
            elif self.use_ollama and scan.text:
                self._ollama_assist(task, scan, context)
            context.check("scan")

            self._assign_candidate_identities(scan)
            self._merge_review_state(scan, previous_scan)
            for c in scan.candidates:
                context.check("scan")
                c.status = ItemStatus.DETECTED.value
            task.scan = scan
            try:
                register_sensitive_values([c.text for c in scan.candidates
                                           if c.text and not c.excluded])
            except Exception:
                pass
            target = (TaskStatus.NEEDS_REVIEW
                      if (scan.candidates or scan.review_flags)
                      else TaskStatus.PROCESSED_UNAUDITED)
            if target == TaskStatus.NEEDS_REVIEW:
                context.close_hard_worker()
            detail = f"candidates={len(scan.candidates)} review_flags={len(scan.review_flags)}"
            context.check("scan")
            self._transition(
                task, target, stage="scan", event_code="phase.scan.end",
                detail=detail, duration_ms=context.elapsed_ms(started),
                operation=("scan", detail))
            return scan
        except (TaskCancelledError, TaskTimedOutError) as exc:
            self._finish_control(
                task, exc, stage="scan", started=started, context=context)
            raise
        except Exception as exc:
            try:
                context.check("scan")
            except (TaskCancelledError, TaskTimedOutError) as control_exc:
                self._finish_control(
                    task, control_exc, stage="scan", started=started,
                    context=context)
                raise control_exc from exc
            self._append_retry_exhaustion(task, exc, "scan")
            self._record_phase(
                task, "phase.scan.failed", "scan",
                detail=f"error={type(exc).__name__}",
                error_code=type(exc).__name__,
                duration_ms=context.elapsed_ms(started))
            raise

    def process(self, task: Task, mode: str, mask: str, candidates: list,
                mapper=None, *, execution_context: TaskExecutionContext | None = None) -> Path:
        """逐项处理 + 失败删除 + 删除验证 + 最终审计。返回最终输出文件路径。"""
        ensure_dirs()
        context = self._context_for(task, execution_context)
        started = time.monotonic()
        try:
            context.begin_stage("process")
            context.check("process")
            self._transition(task, TaskStatus.PROCESSING, stage="process",
                             event_code="phase.process.start", detail="process_start")
            return self._process_impl(
                task, mode, mask, candidates, mapper, started, context)
        except (TaskCancelledError, TaskTimedOutError) as exc:
            self._finish_control(
                task, exc, stage="process", started=started, context=context)
            raise
        except Exception as exc:
            try:
                context.check("process")
            except (TaskCancelledError, TaskTimedOutError) as control_exc:
                self._finish_control(
                    task, control_exc, stage="process", started=started,
                    context=context)
                raise control_exc from exc
            self._append_retry_exhaustion(task, exc, "process")
            self._record_phase(
                task, "phase.process.failed", "process",
                detail=f"error={type(exc).__name__}",
                error_code=type(exc).__name__,
                duration_ms=context.elapsed_ms(started))
            raise

    def _run_handler_with_retry(self, task: Task, in_path: str, out_path: str,
                                candidates: list,
                                context: TaskExecutionContext) -> None:
        def cleanup():
            try:
                Path(out_path).unlink(missing_ok=True)
            except Exception:
                pass

        injected = self.__dict__.get("_run_handler")
        if injected is not None:
            def invoke_injected():
                return self._run_hard_timeout_operation(
                    injected, (in_path, out_path, candidates),
                    stage="process", context=context)
            operation = invoke_injected
        else:
            handler = handler_for(in_path, self.detector)
            def invoke_handler():
                return self._run_hard_timeout_operation(
                    handler.process, (in_path, out_path, candidates),
                    stage="process", context=context)
            operation = invoke_handler

        self._retry_operation(
            operation, task=task, stage="process", context=context,
            cleanup=cleanup)

    def _process_impl(self, task: Task, mode: str, mask: str,
                      candidates: list, mapper, started: float,
                      context: TaskExecutionContext) -> Path:
        context.check("process")
        task.mode = mode

        masker = Masker(mode, mask, mapper=mapper)
        active = sorted(
            [c for c in candidates if not c.excluded and c.confirmed],
            key=lambda c: len(c.text), reverse=True)
        task.recognized_count = len(active)
        for c in active:
            context.check("process")
            c.replacement = masker.replacement(c.category, c.text)
            c.status = ItemStatus.REDACTED.value
            c.delete = False
            c.deletion_verified = False
            c.failure_reason = ""
            context.check("process")
        out1 = self._out_path(task, "masked")
        context.track_artifact(out1)
        self._run_handler_with_retry(
            task, str(task.source_path), str(out1), active, context)
        context.check("process")
        resid, unv = self._run_hard_timeout_operation(
            self.auditor.residual_terms,
            (str(out1), [c.text for c in active]),
            stage="process", context=context)
        context.check("process")
        resid = set(resid)
        unv = set(unv)
        deletion = []
        for c in active:
            context.check("process")
            if c.text in unv:
                c.status = ItemStatus.AUDIT_FAILED.value
                c.failure_reason = "无法验证脱敏结果，转入删除流程"
                deletion.append(c)
            elif c.text in resid:
                c.status = ItemStatus.AUDIT_FAILED.value
                c.failure_reason = "脱敏后仍检测到原始敏感值残留"
                deletion.append(c)
            else:
                c.status = ItemStatus.AUDIT_PASSED.value
        final = out1
        if deletion:
            for c in deletion:
                context.check("process")
                c.status = ItemStatus.DELETION_PENDING.value
                c.replacement = ""
                c.delete = True
            out2 = self._out_path(task, "deleted")
            context.track_artifact(out2)
            self._run_handler_with_retry(
                task, str(out1), str(out2), deletion, context)
            context.check("process")
            resid2, unv2 = self._run_hard_timeout_operation(
                self.auditor.residual_terms,
                (str(out2), [c.text for c in deletion]),
                stage="process", context=context)
            context.check("process")
            resid2 = set(resid2)
            unv2 = set(unv2)
            for c in deletion:
                context.check("process")
                if c.text in unv2:
                    c.status = ItemStatus.UNRESOLVED.value
                    c.failure_reason = "无法验证删除结果（输出不可读）"
                    c.deletion_verified = False
                elif c.text in resid2:
                    c.status = ItemStatus.UNRESOLVED.value
                    c.failure_reason = "删除后仍检测到敏感值残留"
                    c.deletion_verified = False
                else:
                    c.status = ItemStatus.DELETION_VERIFIED.value
                    c.deletion_verified = True
            final = out2
            clean = self._out_path(task, "masked")
            if clean.exists():
                clean.unlink()
            shutil.move(str(out2), str(clean))
            if self.temp_lifecycle is not None:
                self.temp_lifecycle.mark_path(task.task_id, out2, "moved")
            context.untrack_artifact(out2)
            context.track_artifact(clean)
            final = clean
        context.check("process")
        task.output_path = final
        task.final_hash = sha256_file(final)
        task.redacted_count = sum(
            1 for c in active if c.status == ItemStatus.AUDIT_PASSED.value)
        task.deleted_count = sum(1 for c in active if c.deletion_verified)
        task.records = [self._record(task, i, c) for i, c in enumerate(active)]
        context.check("process")
        report = self.audit(task, active, execution_context=context)
        task.audit = report
        context.check("process")

        def cleanup_report():
            if task.report_path:
                try:
                    task.report_path.unlink(missing_ok=True)
                except Exception:
                    pass

        self._retry_operation(
            lambda: self._write_report(task), task=task, stage="process",
            context=context, cleanup=cleanup_report)
        if task.report_path is not None:
            context.track_artifact(task.report_path)
        context.check("process")
        detail = f"mode={mode} active={len(active)}"
        self._transition(
            task, TaskStatus.PROCESSED_UNAUDITED, stage="process",
            event_code="phase.process.end", detail=detail,
            duration_ms=context.elapsed_ms(started),
            operation=("process", detail))
        return final

    def audit(self, task: Task, candidates: list, *,
              execution_context: TaskExecutionContext | None = None) -> AuditReport:
        if task.output_path is None:
            raise RuntimeError("尚未处理，无法审计")
        context = self._context_for(task, execution_context)
        started = time.monotonic()
        try:
            context.begin_stage("audit")
            context.check("audit")
            self._record_phase(task, "phase.audit.start", "audit")
            terms = [c.text for c in candidates if not c.excluded and c.text]
            report = self._run_hard_timeout_operation(
                self.auditor.audit, (str(task.output_path), terms),
                stage="audit", context=context)
            context.check("audit")
            task.audit = report
            self._record_phase(
                task, "phase.audit.end", "audit",
                detail=f"passed={report.passed};findings={len(report.findings)}",
                duration_ms=context.elapsed_ms(started))
            return report
        except (TaskCancelledError, TaskTimedOutError) as exc:
            self._finish_control(
                task, exc, stage="audit", started=started, context=context)
            raise
        except Exception as exc:
            try:
                context.check("audit")
            except (TaskCancelledError, TaskTimedOutError) as control_exc:
                self._finish_control(
                    task, control_exc, stage="audit", started=started,
                    context=context)
                raise control_exc from exc
            self._append_retry_exhaustion(task, exc, "audit")
            self._record_phase(
                task, "phase.audit.failed", "audit",
                detail=f"error={type(exc).__name__}",
                error_code=type(exc).__name__,
                duration_ms=context.elapsed_ms(started))
            raise

    def _validate_success_artifact(self, stored: Task) -> tuple[Path | None, list[str]]:
        reasons: list[str] = []
        success_root = Path(SUCCESS_DIR)
        try:
            success_info = os.lstat(success_root)
            if (success_root.is_symlink()
                    or getattr(success_info, "st_file_attributes", 0)
                    & _FILE_ATTRIBUTE_REPARSE_POINT):
                reasons.append("output_path_unsafe")
        except OSError:
            reasons.append("output_path_unsafe")
        if stored.output_path is None:
            return None, list(dict.fromkeys(reasons + ["output_missing"]))
        path = Path(stored.output_path)
        if not os.path.lexists(path):
            return path, ["output_missing"]
        try:
            info = os.lstat(path)
            if (path.is_symlink()
                    or not stat.S_ISREG(info.st_mode)
                    or (getattr(info, "st_file_attributes", 0)
                        & _FILE_ATTRIBUTE_REPARSE_POINT)):
                reasons.append("output_path_unsafe")
            try:
                if path.resolve() == stored.source_path.resolve():
                    reasons.append("output_path_unsafe")
            except OSError:
                reasons.append("output_path_unsafe")
            if (not is_within(path, [SUCCESS_DIR])
                    or path.resolve() == Path(SUCCESS_DIR).resolve()):
                reasons.append("output_outside_success_dir")
            if is_within(path, [PROCESSING_DIR, QUARANTINE_DIR]):
                reasons.append("output_path_unsafe")
            if is_within(path, [SUCCESS_DIR]):
                lexical_root = Path(os.path.abspath(SUCCESS_DIR))
                lexical_path = Path(os.path.abspath(path))
                for parent in lexical_path.parents:
                    if os.path.normcase(str(parent)) == os.path.normcase(str(lexical_root)):
                        break
                    try:
                        parent_info = os.lstat(parent)
                    except OSError:
                        reasons.append("output_path_unsafe")
                        break
                    if (parent.is_symlink() or
                            getattr(parent_info, "st_file_attributes", 0)
                            & _FILE_ATTRIBUTE_REPARSE_POINT):
                        reasons.append("output_path_unsafe")
                        break
            if not reasons and sha256_file(path) != stored.final_hash:
                reasons.append("output_hash_mismatch")
        except OSError:
            reasons.append("output_path_unsafe")
        return path, list(dict.fromkeys(reasons))

    def export_gate_reasons(self, task: Task) -> list[str]:
        reasons: list[str] = []
        if task.status != TaskStatus.PASSED:
            reasons.append("task_not_passed")
        if self.repository is None:
            reasons.append("database_unavailable")
            return reasons
        try:
            stored = self.repository.load_task(task.task_id)
        except Exception:
            reasons.append("database_unavailable")
            return reasons
        if stored is None or stored.status != TaskStatus.PASSED:
            reasons.append("database_not_passed")
        if stored is not None:
            _, artifact_reasons = self._validate_success_artifact(stored)
            reasons.extend(artifact_reasons)
            if stored.audit is None or not stored.audit.passed or not stored.audit.findings:
                reasons.append("audit_not_passed")
            if stored.scan is None:
                reasons.append("review_state_unavailable")
            else:
                if stored.scan.review_flags:
                    reasons.append("review_flags_present")
                for candidate in stored.scan.candidates:
                    decision_value = getattr(candidate.decision, "value", candidate.decision)
                    if (decision_value == EntityDecision.NEEDS_REVIEW.value
                            and not candidate.confirmed and not candidate.excluded):
                        reasons.append("unreviewed_candidates")
                        break
        try:
            if (stored is None or not stored.source_hash
                    or sha256_file(stored.source_path) != stored.source_hash):
                reasons.append("source_hash_changed")
        except Exception:
            reasons.append("source_hash_unavailable")
        return list(dict.fromkeys(reasons))

    def export_to_location(self, task: Task, destination: str | Path, *,
                           overwrite: bool = False,
                           execution_context: TaskExecutionContext | None = None) -> Path:
        """统一用户导出入口：重新验证 DB/审计/哈希/复核后原子写入。"""
        context = self._context_for(task, execution_context)
        context.check("export")
        reasons = self.export_gate_reasons(task)
        if reasons:
            raise SecurityViolation(
                "导出安全门禁拒绝：" + ",".join(reasons),
                code=ErrorCode.SECURITY_VIOLATION)
        stored = self.repository.load_task(task.task_id)
        if stored is None or not stored.final_hash:
            raise SecurityViolation("导出数据库确认失败")
        source_output, artifact_reasons = self._validate_success_artifact(stored)
        if artifact_reasons or source_output is None:
            raise SecurityViolation(
                "导出安全门禁拒绝：" + ",".join(artifact_reasons))
        final = Path(destination)
        try:
            if final.resolve() == stored.source_path.resolve():
                raise SecurityViolation("拒绝导出覆盖原始文件")
            if is_within(final, [DATA_DIR]):
                raise SecurityViolation("用户导出目标不得位于受控数据目录")
        except SecurityViolation:
            raise
        except OSError as exc:
            raise SecurityViolation("导出目标路径无法解析") from exc

        existed = final.exists()
        backup = None
        original_hash = ""
        published = False
        keep_backup = False
        if existed:
            if not overwrite:
                raise SecurityViolation("目标文件已存在，默认拒绝覆盖")
            original_hash = sha256_file(final)
            backup = final.with_name(f".{final.name}.{uuid.uuid4().hex}.bak")
            try:
                shutil.copy2(final, backup)
                if sha256_file(backup) != original_hash:
                    raise SecurityViolation("覆盖前备份哈希校验失败")
            except Exception:
                if backup.exists():
                    try:
                        backup.unlink()
                    except Exception:
                        pass
                raise
        try:
            context.check("export")
            exported = atomic_copy_user_file(
                source_output, final, expected_sha256=stored.final_hash,
                overwrite=overwrite)
            published = True
            context.check("export")
            event = TaskEvent(
                event_code="task.export.user_location", stage="export",
                from_status=TaskStatus.PASSED.name,
                to_status=TaskStatus.PASSED.name,
                actor=current_actor(), detail=f"overwrite={int(overwrite)};hash_ok=true")
            self._persist_with_retry(
                stored, operation=("user_export", "status=PASSED;hash_ok"),
                events=[event], stage="export")
            self._metric("export_success", 1, stage="export",
                         outcome=("overwrite" if overwrite else "new_file"))
            return exported
        except Exception as exc:
            recovery_error = None
            target_changed = (
                published or
                (existed and final.exists()
                 and sha256_file(final) != original_hash)
            )
            if target_changed and backup is not None:
                try:
                    atomic_copy_user_file(
                        backup, final, expected_sha256=original_hash,
                        overwrite=True)
                    if sha256_file(final) != original_hash:
                        raise RuntimeError("恢复后哈希校验失败")
                except Exception as restore_exc:
                    keep_backup = True
                    recovery_error = ExportRecoveryError(
                        f"覆盖导出恢复失败，备份已保留：{backup}")
                    recovery_error.__cause__ = restore_exc
            elif target_changed and not existed:
                try:
                    final.unlink(missing_ok=True)
                except Exception as cleanup_exc:
                    recovery_error = ExportRecoveryError(
                        "新导出文件清理失败，禁止报告导出成功")
                    recovery_error.__cause__ = cleanup_exc
            if recovery_error is not None:
                self._metric("export_recovery_failed", 1, stage="export",
                             code="backup_preserved" if keep_backup else "cleanup_failed")
                try:
                    self._record_phase(
                        stored, "task.export.recovery_failed", "export",
                        detail=("backup_preserved" if keep_backup
                                else "cleanup_failed"),
                        error_code="export_recovery_failed")
                except Exception:
                    pass
                try:
                    self.repository.append_operation(
                        task.task_id, "export_recovery_failed",
                        "backup_preserved" if keep_backup else "cleanup_failed")
                except Exception:
                    pass
                raise recovery_error from exc
            self._metric("export_failed", 1, stage="export",
                         code="export_failed")
            raise
        finally:
            if backup is not None and backup.exists() and not keep_backup:
                try:
                    backup.unlink()
                except Exception:
                    pass

    def finalize(self, task: Task, report: AuditReport, *,
                 execution_context: TaskExecutionContext | None = None) -> Task:
        """导出安全门禁 wrapper：start/failed/end 事件与状态转移同事务。"""
        context = self._context_for(task, execution_context)
        started = time.monotonic()
        try:
            context.begin_stage("finalize")
            context.check("finalize")
            self._record_phase(task, "phase.finalize.start", "finalize")
            return self._finalize_impl(task, report, started, context)
        except (TaskCancelledError, TaskTimedOutError) as exc:
            self._finish_control(
                task, exc, stage="finalize", started=started, context=context)
            raise
        except Exception as exc:
            try:
                context.check("finalize")
            except (TaskCancelledError, TaskTimedOutError) as control_exc:
                self._finish_control(
                    task, control_exc, stage="finalize", started=started,
                    context=context)
                raise control_exc from exc
            self._append_retry_exhaustion(task, exc, "finalize")
            self._record_phase(
                task, "phase.finalize.failed", "finalize",
                detail=f"error={type(exc).__name__}",
                error_code=type(exc).__name__,
                duration_ms=context.elapsed_ms(started))
            raise
        finally:
            context.close_hard_worker()

    def _finalize_impl(self, task: Task, report: AuditReport,
                       started: float,
                       context: TaskExecutionContext) -> Task:
        context.check("finalize")
        def block(msg: str):

            task.error = msg
            self._move_to_quarantine(task)
            self._transition(
                task, TaskStatus.UNSAFE, stage="finalize",
                event_code="phase.finalize.failed", detail=msg,
                error_code="finalize_blocked",
                operation=("finalize", msg),
                duration_ms=int((time.monotonic() - started) * 1000))
            return task

        if sha256_file(task.source_path) != task.source_hash:
            return block("源文件哈希发生变化，原件可能被修改，已停止导出")
        if report is None or task.audit is not report:
            return block("审计报告无效或未经真实审计，已停止导出")
        if not report.findings:
            return block("审计未产生任何结论，已停止导出")
        if task.output_path is None or not task.output_path.exists():
            return block("输出文件缺失或未生成，已停止导出")
        if task.final_hash and sha256_file(task.output_path) != task.final_hash:
            return block("输出文件在审计之后被修改，审计结果失效，已停止导出")

        unresolved = [r for r in task.records if r["final_disposition"] == "unresolved"]
        if unresolved:
            task.error = f"存在 {len(unresolved)} 条无法确认安全的敏感信息"
            self._move_to_quarantine(task)
            self._transition(
                task, TaskStatus.AUDIT_FAILED, stage="finalize",
                event_code="phase.finalize.failed", detail="unresolved_items",
                error_code="residual", operation=("finalize", "unresolved_items"),
                duration_ms=int((time.monotonic() - started) * 1000))
            return task

        pending_review = [
            c for c in (task.scan.candidates if task.scan else [])
            if getattr(c.decision, "value", c.decision) == EntityDecision.NEEDS_REVIEW.value
            and not c.confirmed and not c.excluded
        ]
        review_flags = list(task.scan.review_flags if task.scan else [])
        if pending_review or review_flags:
            task.error = (
                f"存在 {len(pending_review)} 条待人工复核项未裁决，阻断导出")
            if review_flags:
                task.error += "；存在未解决的复核标记"
            self._move_to_quarantine(task)
            self._transition(
                task, TaskStatus.NEEDS_REVIEW, stage="finalize",
                event_code="phase.finalize.failed", detail="pending_review",
                error_code="needs_review", operation=("finalize", "pending_review"),
                duration_ms=int((time.monotonic() - started) * 1000))
            return task

        if not report.passed:
            residual = any(f.verdict.value == "发现残留" for f in report.findings)
            target = TaskStatus.AUDIT_FAILED if residual else TaskStatus.UNSAFE
            task.error = "审计未通过"
            self._metric("audit_failed", 1, stage="finalize",
                         code="residual" if residual else "uncheckable")
            self._move_to_quarantine(task)
            self._transition(
                task, target, stage="finalize",
                event_code="phase.finalize.failed", detail="audit_not_passed",
                error_code="residual" if residual else "uncheckable",
                operation=("finalize", "audit_not_passed"),
                duration_ms=int((time.monotonic() - started) * 1000))
            return task

        if self.repository is not None and task.mode:
            finder = getattr(self.repository, "find_reusable", None)
            if finder is not None:
                existing = finder(
                    task.source_hash, task.mode, task.policy_version,
                    task.config_hash, task.tool_version,
                    exclude_task_id=task.task_id)
                if existing is not None:
                    task.error = "同键已有有效成功产物，已复用，未重复导出"
                    self._record_phase(
                        task, "phase.finalize.end", "finalize",
                        detail="reused_existing",
                        duration_ms=int((time.monotonic() - started) * 1000))
                    return existing

        context.check("finalize")
        ensure_transition_allowed(task.status, TaskStatus.PASSED, "finalize_success")
        old_output = task.output_path
        old_report = task.report_path
        out_name = f"masked_{task.task_id}{old_output.suffix.lower()}"
        report_name = f"audit_{task.task_id}.json"
        dest = None
        report_dest = None
        try:
            context.check("finalize")
            self._record_phase(task, "phase.export.start", "export")
            dest = self._retry_operation(
                lambda: atomic_copy(
                    old_output, SUCCESS_DIR, out_name,
                    expected_sha256=task.final_hash),
                task=task, stage="finalize", context=context)
            context.check("finalize")
            self._record_phase(
                task, "phase.export.hash", "export",
                detail="hash_ok")
            if task.report_path and task.report_path.exists():
                report_dest = self._retry_operation(
                    lambda: atomic_copy(
                        task.report_path, SUCCESS_DIR, report_name),
                    task=task, stage="finalize", context=context)
            context.check("finalize")
            self._cleanup_processing_artifacts(
                task, old_output, old_report, *sorted(context.artifacts))
            context.artifacts.clear()
            context.check("finalize")
            self._record_phase(
                task, "phase.export.replace", "export",
                detail="atomic_replace_done")
            task.output_path = dest
            if report_dest is not None:
                task.report_path = report_dest
            context.check("finalize")
            elapsed = context.elapsed_ms(started)
            finalize_end = TaskEvent(
                event_code="phase.finalize.end", stage="finalize",
                from_status=TaskStatus.PROCESSED_UNAUDITED.name,
                to_status=TaskStatus.PASSED.name,
                actor=current_actor(), duration_ms=elapsed,
                detail="status=PASSED")
            self._transition(
                task, TaskStatus.PASSED, stage="finalize",
                event_code="phase.export.db_confirm",
                detail="status=PASSED;db_confirm=ok",
                operation=("finalize", "status=PASSED"),
                duration_ms=elapsed, additional_events=[finalize_end])
            self._metric("task_success", 1, stage="finalize")
            self._metric("stage_duration_ms", elapsed, stage="finalize",
                         outcome="success")
        except Exception as exc:
            self._append_retry_exhaustion(task, exc, "finalize")
            task.output_path = old_output
            task.report_path = old_report
            cleanup_errors = []
            if dest is not None:
                try:
                    quarantine_unconfirmed(dest, QUARANTINE_DIR, "unconfirmed_export")
                    self._metric("quarantine", 1, stage="finalize",
                                 code="unconfirmed_export")
                except Exception as cleanup_exc:
                    cleanup_errors.append(cleanup_exc)
            if report_dest is not None:
                try:
                    quarantine_unconfirmed(report_dest, QUARANTINE_DIR, "unconfirmed_report")
                    self._metric("quarantine", 1, stage="finalize",
                                 code="unconfirmed_report")
                except Exception as cleanup_exc:
                    cleanup_errors.append(cleanup_exc)
            task.error = f"导出失败：{type(exc).__name__}: {exc}"
            try:
                self._record_phase(
                    task, "phase.export.failed", "export",
                    detail=("compensation_failed" if cleanup_errors
                            else f"error={type(exc).__name__}"),
                    error_code=("export_compensation_failed" if cleanup_errors
                                else type(exc).__name__))
            except Exception:
                pass
            if cleanup_errors:
                raise AtomicExportError(
                    "导出失败且未确认产物无法清理，任务禁止标记成功") from cleanup_errors[0]
            raise
        return task

    def mark_task_needs_review(self, task: Task, *,
                              reason: str = "review_required") -> Task:
        """统一复核入口：状态、事件、操作日志同事务；已在复核态则不重复。"""
        if task.status == TaskStatus.NEEDS_REVIEW:
            return task
        self._transition(
            task, TaskStatus.NEEDS_REVIEW, stage="review",
            event_code="task.needs_review", detail=reason,
            operation=("review_gate", reason))
        return task

    def mark_task_unsafe(self, task: Task, *, reason: str = "gui_error",
                         error_code: str = "gui_error") -> Task:
        """统一失败关闭入口：UNSAFE + 事件 + 操作日志；终态拒绝回退。"""
        old_error = task.error
        task.error = reason
        try:
            ensure_transition_allowed(task.status, TaskStatus.UNSAFE, reason)
            try:
                self.quarantine_task_artifacts(task, reason=error_code)
            except Exception as cleanup_exc:
                task.error = f"{reason}；临时产物隔离失败：{type(cleanup_exc).__name__}"
            self._transition(
                task, TaskStatus.UNSAFE, stage="gui",
                event_code="task.unsafe", detail=reason,
                error_code=error_code,
                operation=("mark_unsafe", error_code))
        except Exception:
            task.error = old_error
            raise
        return task

    def run(self, task: Task, mode: str, mask: str, candidates: list) -> AuditReport:
        self.process(task, mode, mask, candidates)
        self.finalize(task, task.audit)
        return task.audit

    # ---------- 内部工具 ----------
    def _run_handler(self, in_path: str, out_path: str, candidates: list) -> None:
        handler = handler_for(in_path, self.detector)
        handler.process(in_path, out_path, candidates)

    def _out_path(self, task: Task, role: str) -> Path:
        """生成不含原始文件名 stem 的受控 processing 路径。"""
        handler = handler_for(str(task.source_path), self.detector)
        if handler.name == "image":
            extension = ".png"
        elif task.source_path.suffix.lower() == ".xlsm":
            extension = ".xlsx"
        else:
            extension = task.source_path.suffix.lower()
        if role not in {"masked", "deleted"}:
            # 兼容旧调用：先验证历史标签不构成路径穿越，再归一为中性角色。
            safe_join(PROCESSING_DIR, f"{role}{extension}")
            role = "deleted" if "删除" in role else "masked"
        name = f"work_{task.task_id}_{role}{extension}"
        path = safe_join(PROCESSING_DIR, name)
        self._register_temp(task.task_id, path, "process")
        return path

    @staticmethod
    def _source_ref(task: Task) -> str:
        extension = task.source_path.suffix.lower()
        return f"source_{task.source_hash[:12]}{extension}"

    def _record(self, task: Task, idx: int, c) -> dict:
        if c.status == ItemStatus.AUDIT_PASSED.value:
            disposition, action, audit_status = "success", "redact", "passed"
        elif c.deletion_verified:
            disposition, action, audit_status = "deleted", "delete", "passed"
        elif c.status in (ItemStatus.UNRESOLVED.value, ItemStatus.FAILED.value):
            disposition, action, audit_status = "unresolved", ("delete" if c.delete else "redact"), "failed"
        else:
            disposition, action, audit_status = "pending", "redact", "pending"
        return {
            "record_id": f"{task.task_id}-{idx + 1:03d}",
            "task_id": task.task_id,
            "source_ref": self._source_ref(task),
            "file_type": task.file_type,
            "sensitive_type": c.category,
            "source_location": c.location,
            "sensitive_value_masked": mask_display(c.text, c.category),
            "processing_action": action,
            "redaction_status": c.status,
            "audit_status": audit_status,
            "failure_reason": c.failure_reason,
            "deletion_status": "verified" if c.deletion_verified else ("performed" if c.delete else "n/a"),
            "deletion_verified": c.deletion_verified,
            "final_disposition": disposition,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }

    @staticmethod
    def _safe_audit_findings(report: AuditReport) -> list[dict]:
        safe_detail = {
            AuditVerdict.RESIDUAL_FOUND: "发现已知敏感信息残留",
            AuditVerdict.UNCHECKABLE: "存在无法检查的内容",
            AuditVerdict.NEEDS_CONFIRM: "存在需人工确认内容",
            AuditVerdict.NO_RESIDUAL: "未发现已知敏感信息残留",
            AuditVerdict.INSUFFICIENT: "审计覆盖不足",
        }
        return [
            {
                "finding_id": f"audit-{index:03d}",
                "verdict": finding.verdict.value,
                "scope": finding.scope or "general",
                "detail": safe_detail.get(
                    finding.verdict, "审计结论无法安全描述"),
            }
            for index, finding in enumerate(report.findings, 1)
        ]

    def _write_report(self, task: Task) -> None:
        """在 final audit 之后原子生成自包含、脱敏的结构化审计记录。"""
        if task.audit is None:
            raise RuntimeError("审计报告缺失，禁止写入审计 JSON")
        report_name = f"work_{task.task_id}_audit.json"
        payload = {
            "schema_version": 2,
            "audit_passed": bool(task.audit.passed),
            "coverage": list(dict.fromkeys(task.audit.coverage)),
            "findings": self._safe_audit_findings(task.audit),
            "task_id": task.task_id,
            "source_ref": self._source_ref(task),
            "file_type": task.file_type,
            "source_sha256": task.source_hash,
            "final_sha256": task.final_hash,
            "recognized_count": task.recognized_count,
            "redacted_count": task.redacted_count,
            "deleted_count": task.deleted_count,
            "records": task.records,
            "policy_version": task.policy_version or POLICY_VERSION,
            "config_hash": task.config_hash or compute_config_hash(self.settings),
            "tool_version": task.tool_version or TOOL_VERSION,
        }
        existing = safe_join(PROCESSING_DIR, report_name)
        self._register_temp(task.task_id, existing, "report")
        if existing.exists():
            existing.unlink()
        task.report_path = atomic_write_bytes(
            PROCESSING_DIR, report_name,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))

    def _ollama_assist(self, task: Task, scan, context: TaskExecutionContext):
        from ..domain.entities import Candidate
        from ..infrastructure.llm.ollama import OllamaClient
        try:
            cli = OllamaClient(model=self.ollama_model) if self.ollama_model else OllamaClient()
            entities = self._retry_operation(
                lambda: cli.extract_entities(scan.text[:8000]),
                task=task, stage="scan", context=context)
            existing = {(c.category, c.text) for c in scan.candidates}
            for cat, text in entities:
                if (cat, text) in existing:
                    continue
                scan.candidates.append(Candidate(text=text, category=cat, location="本地模型辅助",
                                                 confidence=0.6, source="ollama"))
        except RetryExhaustedError:
            raise
        except Exception as e:
            scan.warnings.append(f"本地模型辅助识别失败：{e}")

    def _move_to_quarantine(self, task: Task):
        QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
        output_path = task.output_path
        report_path = getattr(task, "report_path", None)
        if output_path is not None and output_path.exists():
            dest = safe_join(
                QUARANTINE_DIR,
                f"{output_path.stem}.{uuid.uuid4().hex[:8]}{output_path.suffix}")
            shutil.move(str(output_path), str(dest))
            task.output_path = dest
            if self.temp_lifecycle is not None:
                self.temp_lifecycle.mark_path(task.task_id, output_path,
                                               "quarantined")
            self._metric("quarantine", 1, stage="cleanup", code="task_output")
        if report_path is not None and report_path.exists():
            dest = safe_join(
                QUARANTINE_DIR,
                f"{report_path.stem}.{uuid.uuid4().hex[:8]}{report_path.suffix}")
            shutil.move(str(report_path), str(dest))
            if self.temp_lifecycle is not None:
                self.temp_lifecycle.mark_path(task.task_id, report_path,
                                               "quarantined")
            self._metric("quarantine", 1, stage="cleanup", code="task_report")
