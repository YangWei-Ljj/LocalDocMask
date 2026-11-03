"""批次协调服务（module_contracts §3.3 / ADR-004）。

职责：
- 一个批次内共享同一个 ``ConsistencyMapper``，使同一真实实体在该批次所有
  文件中映射为同一虚构值（修复跨文件不一致 P1-3）；
- 逐文件 prepare→scan→（自动失败关闭：含 NEEDS_REVIEW 的文件归入 review
  不导出）→process（共享 mapper）→finalize；
- 失败隔离：单文件异常不影响其他文件；
- 协作式取消与进度回调；进度/报告只含文件名与掩码值，不含原文/映射。
"""
from __future__ import annotations

import inspect
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.entities import Task, TaskEvent
from ..domain.enums import EntityDecision, TaskStatus
from ..domain.exceptions import (
    InvalidTaskTransition, QueueFullError, RetryExhaustedError,
    TaskCancelledError, TaskTimedOutError,
)
from ..domain.state_machine import transition_task
from ..security.admission import AdmissionError
from ..security.limits import ResourceLimits
from ..security.redaction.mapping import ConsistencyMapper
from .pipeline import CancelToken, TaskExecutionContext


@dataclass
class BatchResult:
    passed: list = field(default_factory=list)    # 已通过门禁、成功导出的 Task
    review: list = field(default_factory=list)     # 含待复核项、未导出的 Task
    failed: list = field(default_factory=list)     # 失败/失败关闭被隔离的 Task
    skipped: list = field(default_factory=list)    # 幂等命中、跳过复用的 Task
    warnings: list = field(default_factory=list)  # 人类可读告警（仅文件名，无原文）


class ConcurrencyController:
    """无阻塞队列准入 + 有界工作槽；锁只在计数/释放时短暂持有。"""

    def __init__(self, max_workers: int, max_queue_depth: int,
                 metrics=None, health=None) -> None:
        self.max_workers = max(1, int(max_workers))
        self.max_queue_depth = max(1, int(max_queue_depth))
        self.metrics = metrics
        self.health = health
        self._slots = threading.BoundedSemaphore(self.max_workers)
        self._lock = threading.Lock()
        self._accepted = 0
        self._active = 0

    def _metric(self, name: str, value: float = 1, **labels) -> None:
        if self.metrics is None:
            return
        try:
            self.metrics.record(name, value, **labels)
        except Exception:
            if self.health is not None:
                self.health.mark_degraded("metrics", "metrics_write_failed")

    @contextmanager
    def accept(self, count: int):
        count = max(0, int(count))
        with self._lock:
            if self._accepted + count > self.max_queue_depth:
                self._metric("queue_rejected", count, code="queue_full")
                raise QueueFullError(self._accepted, self.max_queue_depth)
            self._accepted += count
            self._metric("queue_accepted", count)
        try:
            yield
        finally:
            with self._lock:
                self._accepted -= count

    @contextmanager
    def worker_slot(self, context: TaskExecutionContext):
        while not self._slots.acquire(blocking=False):
            context.check(None)
            time.sleep(0.01)
        try:
            context.check(None)
            with self._lock:
                self._active += 1
                active = self._active
            self._metric("worker_slots_in_use", active)
            yield
        finally:
            with self._lock:
                self._active = max(0, self._active - 1)
                active = self._active
            self._metric("worker_slots_in_use", active)
            self._slots.release()


class BatchService:
    """对一组源文件批量执行脱敏流水线。

    Parameters
    ----------
    pipeline:
        当前 TaskManager（目标接口 prepare/scan/process/finalize）。
    settings:
        应用 Settings；读取 mapping_scope / mapping_persistence 决定批次
        共享 mapper 的持久化形态。默认内存、不落盘。
    """

    def __init__(self, pipeline, settings=None) -> None:
        self.pipeline = pipeline
        self.settings = settings or getattr(pipeline, "settings", None)
        self.limits = ResourceLimits.from_settings(self.settings)
        self.metrics = getattr(pipeline, "metrics", None)
        self.health = getattr(pipeline, "health", None)
        self.concurrency = getattr(pipeline, "_concurrency_controller", None)
        if self.concurrency is None:
            self.concurrency = ConcurrencyController(
                self.limits.max_workers, self.limits.max_queue_depth,
                metrics=self.metrics, health=self.health)
            setattr(pipeline, "_concurrency_controller", self.concurrency)
        else:
            self.concurrency.metrics = self.metrics
            self.concurrency.health = self.health

    def _metric(self, name: str, value: float = 1, **labels) -> None:
        if self.metrics is None:
            return
        try:
            self.metrics.record(name, value, **labels)
        except Exception:
            if self.health is not None:
                self.health.mark_degraded("metrics", "metrics_write_failed")

    # ------------------------------------------------------------------
    def run(self, sources, mode: str = "b", mask: str = "****",
            progress_cb=None, cancel_token: CancelToken | None = None,
            force: bool = False) -> BatchResult:
        result = BatchResult()
        source_list = list(sources or [])
        if not source_list:
            return result
        token = cancel_token or CancelToken()
        mapper = self._build_mapper()
        total = len(source_list)
        repo = getattr(self.pipeline, "repository", None)

        if token.cancelled:
            result.warnings.append(f"用户取消：{total} 个文件未处理")
            return result

        with self.concurrency.accept(total):
            for index, source in enumerate(source_list):
                if token.cancelled:
                    result.warnings.append(
                        f"用户取消：剩余 {total - index} 个文件未处理，已完成的保留")
                    break
                name = Path(str(source)).name
                task: Task | None = None
                context = TaskExecutionContext.from_limits(self.limits, token)
                try:
                    self._progress(progress_cb, index, total, name, "prepare")
                    task = self._prepare(
                        source, mode, cancel_token=token,
                        execution_context=context)
                    context.bind_task(task.task_id)
                    setattr(task, "_execution_context", context)
                    context.check("queue")

                    if repo is not None:
                        claim = self._claim_with_retry(task, force=force)
                        if claim.outcome == "reused":
                            result.skipped.append(claim.task)
                            result.warnings.append(f"{name}：命中有效成功产物，复用跳过")
                            continue
                        if claim.outcome == "in_progress":
                            result.skipped.append(claim.task)
                            result.warnings.append(
                                f"{name}：同键任务正在处理，等待/跳过，不重复导出")
                            continue
                        if claim.outcome == "stale_recreated":
                            result.warnings.append(f"{name}：旧产物不可复用，已创建新尝试")
                        task = claim.task
                        context.bind_task(task.task_id)
                        setattr(task, "_execution_context", context)

                    task = self._run_task_in_slot(
                        task, index, total, name, mode, mask, mapper,
                        progress_cb, context)
                    if task.status == TaskStatus.PASSED:
                        result.passed.append(task)
                    elif task.status == TaskStatus.NEEDS_REVIEW:
                        result.review.append(task)
                        result.warnings.append(f"{name}：含待复核条目，未自动导出")
                    else:
                        result.failed.append(task)
                        self._metric("task_failed", 1, stage="batch",
                                     code=task.status.name)
                        result.warnings.append(
                            f"{name}：终态 {task.status.value}，未进入成功目录")
                except (TaskCancelledError, TaskTimedOutError) as exc:
                    if task is None:
                        task = getattr(exc, "task", None)
                    if task is not None:
                        if task.status not in (
                            TaskStatus.PASSED, TaskStatus.AUDIT_FAILED,
                            TaskStatus.UNSAFE, TaskStatus.CANCELLED,
                            TaskStatus.TIMED_OUT,
                        ):
                            self._mark_control_failure(
                                task, exc, context, stage="batch")
                        task.error = f"{exc.code.value}: {exc}"
                        result.failed.append(task)
                    result.warnings.append(
                        f"{name}：{exc.code.value}，未导出（{exc.stage or 'batch'}）")
                    if token.cancelled:
                        break
                except Exception as exc:
                    handled = self._handle_batch_exception(
                        result, name, source, task, exc, repo)
                    task = handled
                    if task is not None and task.status == TaskStatus.PASSED:
                        if task not in result.passed:
                            result.passed.append(task)
                    elif task is not None and task not in result.failed:
                        result.failed.append(task)
                    if token.cancelled:
                        break
        return result

    def _run_task_in_slot(self, task: Task, index: int, total: int, name: str,
                          mode: str, mask: str, mapper, progress_cb,
                          context: TaskExecutionContext) -> Task:
        with self.concurrency.worker_slot(context):
            self._progress(progress_cb, index, total, name, "scan")
            scan = self._scan(task, context)
            context.check("queue")
            if self._needs_review(scan):
                context.close_hard_worker()
                marker = getattr(self.pipeline, "mark_task_needs_review", None)
                if marker is not None:
                    marker(task, reason="batch_review_gate")
                else:
                    transition_task(task, TaskStatus.NEEDS_REVIEW,
                                    reason="batch_review_gate")
                return task

            self._progress(progress_cb, index, total, name, "process")
            self._run_process(task, mode, mask, scan, mapper, context)
            context.check("queue")
            self._progress(progress_cb, index, total, name, "finalize")
            report = getattr(task, "audit", None)
            return self._finalize(task, report, context)

    def _mark_control_failure(self, task: Task, exc: Exception,
                              context: TaskExecutionContext,
                              stage: str = "batch") -> None:
        marker = getattr(self.pipeline, "mark_task_cancelled", None)
        if isinstance(exc, TaskCancelledError):
            if marker is not None:
                marker(task, stage=stage, reason=exc.reason, context=context)
                return
            transition_task(task, TaskStatus.CANCELLED,
                            reason=f"{stage}:{exc.reason}")
            return
        marker = getattr(self.pipeline, "mark_task_timed_out", None)
        if marker is not None:
            marker(task, stage=stage, scope=exc.scope, budget=exc.budget,
                   context=context)
            return
        transition_task(task, TaskStatus.TIMED_OUT,
                        reason=f"{stage}:timeout")

    def _handle_batch_exception(self, result: BatchResult, name: str,
                                source, task: Task | None, exc: Exception,
                                repo) -> Task | None:
        err_text = f"{type(exc).__name__}: {exc}"
        admission_failure = isinstance(exc, AdmissionError)
        retry_failure = isinstance(exc, RetryExhaustedError)
        err_code = (exc.reason_code if admission_failure
                    else exc.code.value if retry_failure
                    else type(exc).__name__)

        if task is not None and task.status == TaskStatus.PASSED:
            confirmed = False
            if repo is not None:
                try:
                    stored = repo.load_task(task.task_id)
                    checker = getattr(repo, "is_reusable_task", None)
                    confirmed = (stored is not None
                                 and stored.status == TaskStatus.PASSED
                                 and checker is not None and checker(stored))
                except Exception:
                    confirmed = False
            else:
                confirmed = True
            task.error = err_text
            if confirmed:
                result.passed.append(task)
                result.warnings.append(
                    f"{name}：异常发生在 PASSED 之后，保留已确认成功，不回退")
            else:
                result.failed.append(task)
                result.warnings.append(
                    f"{name}：异常发生在 PASSED 之后但数据库未确认成功，禁止报告成功")
            return task

        if task is None and admission_failure:
            task = getattr(exc, "task", None)
        if task is not None:
            cleanup = getattr(self.pipeline, "quarantine_task_artifacts", None)
            if cleanup is not None:
                try:
                    cleanup(task, reason=err_code)
                except Exception as cleanup_exc:
                    result.warnings.append(
                        f"{name}：临时产物隔离失败（{type(cleanup_exc).__name__}）")
                    if self.health is not None:
                        self.health.mark_degraded("temp_cleanup", "cleanup_failed")
        if not admission_failure:
            self._metric("task_failed", 1, stage="batch", code=err_code)
        task_was_none = task is None
        previous_status = task.status if task is not None else TaskStatus.PENDING
        if task is None:
            stub = Task(source_path=Path(str(source)))
            transition_task(stub, TaskStatus.AUDIT_FAILED,
                            reason=f"batch_prepare_exception:{err_code}")
            stub.error = err_text
            task = stub
        else:
            try:
                transition_task(task, TaskStatus.AUDIT_FAILED,
                                reason=f"batch_exception:{err_code}")
            except InvalidTaskTransition:
                pass
            task.error = err_text

        persisted = bool(
            admission_failure and repo is not None
            and task.status == TaskStatus.AUDIT_FAILED)
        if repo is not None and not persisted:
            retry_events_queued = bool(getattr(exc, "_retry_events_queued", False))
            events = ([] if retry_events_queued
                      else list(getattr(exc, "retry_events", []) or []))
            if not retry_events_queued:
                events.append(TaskEvent(
                    event_code=(
                        "task.retry.exhausted" if retry_failure else
                        "phase.admission.failed" if admission_failure else
                        "phase.prepare.failed" if task_was_none else
                        "phase.batch.failed"),
                    stage=("process" if retry_failure else
                           "admission" if admission_failure else
                           "prepare" if task_was_none else "batch"),
                    from_status=getattr(previous_status, "name", str(previous_status)),
                    to_status=getattr(task.status, "name", str(task.status)),
                    error_code=err_code,
                    detail=f"error={err_code}"))
            try:
                saver = getattr(self.pipeline, "save_task_with_retry", None)
                if saver is not None:
                    saver(task, operation=("batch_exception", err_code),
                           events=events, stage="batch")
                else:
                    repo.save_task(task, operation=("batch_exception", err_code),
                                   events=events)
                persisted = True
            except Exception as persist_exc:
                result.warnings.append(
                    f"{name}：失败状态持久化失败（{type(persist_exc).__name__}: {persist_exc}），本轮不会导出，需人工检查")
        if persisted:
            result.warnings.append(f"{name}：{task.status.value}，已隔离并持久化")
        else:
            result.warnings.append(
                f"{name}：{task.status.value}，未导出（未确认持久化）")
        result.failed.append(task)
        return task

    def _claim_with_retry(self, task: Task, *, force: bool = False):
        repo = self.pipeline.repository
        max_attempts = max(1, int(self.limits.retry_max_attempts))
        retry_events = []
        current_status = task.status.name
        last_error = None
        for attempt in range(1, max_attempts + 1):
            try:
                claim = repo.claim_or_get(task, force=force)
                if attempt > 1:
                    retry_events.append(TaskEvent(
                        event_code="task.retry.attempt", stage="queue",
                        from_status=current_status, to_status=current_status,
                        detail=f"attempt={attempt};result=success"))
                if retry_events and claim.outcome != "reused":
                    claim.task.pending_events.extend(retry_events)
                return claim
            except Exception as exc:
                last_error = exc
                checker = getattr(self.pipeline, "is_retryable_error", None)
                retryable = checker(exc) if checker is not None else False
                if not retryable:
                    try:
                        setattr(exc, "retry_events", retry_events)
                        setattr(exc, "task", task)
                    except Exception:
                        pass
                    raise
                retry_events.append(TaskEvent(
                    event_code="task.retry.attempt", stage="queue",
                    from_status=current_status, to_status=current_status,
                    error_code=type(exc).__name__,
                    detail=f"attempt={attempt};result=failed;error={type(exc).__name__}"))
                self._metric("retry_attempt", 1, stage="queue",
                             code=type(exc).__name__, outcome="failed")
                if attempt >= max_attempts:
                    self._metric("retry_exhausted", 1, stage="queue",
                                 code=type(exc).__name__)
                    exhausted = RetryExhaustedError(attempt, exc)
                    setattr(exhausted, "retry_events", retry_events)
                    setattr(exhausted, "task", task)
                    raise exhausted from exc
                delayer = getattr(self.pipeline, "retry_delay", None)
                time.sleep(delayer(attempt) if delayer is not None else 0.0)
        raise RetryExhaustedError(max_attempts, last_error)

    # ------------------------------------------------------------------
    def _build_mapper(self) -> ConsistencyMapper:
        """批次级共享 mapper：按 settings(scope+persistence) 构建，与 GUI 同口径。"""
        if self.settings is not None:
            from .composition import build_mapper
            return build_mapper(self.settings)
        return ConsistencyMapper(scope="batch", persistence="memory")

    def _prepare(self, source, mode, *, cancel_token=None,
                 execution_context=None):
        """兼容旧 FakePipeline/自定义 pipeline 的 prepare 签名。"""
        sig = inspect.signature(self.pipeline.prepare)
        kwargs = {}
        if "mode" in sig.parameters:
            kwargs["mode"] = mode
        if "cancel_token" in sig.parameters:
            kwargs["cancel_token"] = cancel_token
        if "execution_context" in sig.parameters:
            kwargs["execution_context"] = execution_context
        return self.pipeline.prepare(source, **kwargs)

    def _scan(self, task, context):
        sig = inspect.signature(self.pipeline.scan)
        kwargs = {}
        if "execution_context" in sig.parameters:
            kwargs["execution_context"] = context
        return self.pipeline.scan(task, **kwargs)

    def _run_process(self, task, mode, mask, scan, mapper, context) -> None:
        candidates = list(getattr(scan, "candidates", []) or [])
        sig = inspect.signature(self.pipeline.process)
        kwargs = {}
        if "mapper" in sig.parameters:
            kwargs["mapper"] = mapper
        if "execution_context" in sig.parameters:
            kwargs["execution_context"] = context
        self.pipeline.process(task, mode, mask, candidates, **kwargs)

    def _finalize(self, task, report, context):
        sig = inspect.signature(self.pipeline.finalize)
        kwargs = {}
        if "execution_context" in sig.parameters:
            kwargs["execution_context"] = context
        return self.pipeline.finalize(task, report, **kwargs)

    @staticmethod
    def _needs_review(scan) -> bool:
        """自动失败关闭：任一显式 NEEDS_REVIEW 条目，或需人工复核信号，即挂起复核。"""
        if scan is None:
            return False
        for c in getattr(scan, "candidates", []) or []:
            decision = getattr(c, "decision", None)
            if decision == EntityDecision.NEEDS_REVIEW.value:
                return True
        if getattr(scan, "review_flags", None):
            return True
        return False

    @staticmethod
    def _progress(cb, index, total, filename, stage) -> None:
        if cb is None:
            return
        try:
            cb(index, total, filename, stage)
        except Exception:
            # 进度回调异常不应中断批次
            pass
