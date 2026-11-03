# -*- coding: utf-8 -*-
"""T5：协作式取消、阶段超时、有限重试与并发背压。"""
from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from docx import Document

import localdocmask.application.pipeline as pipeline_mod
from localdocmask.application.batch import (
    BatchService, CancelToken, ConcurrencyController,
)
from localdocmask.application.pipeline import TaskExecutionContext, TaskManager
from localdocmask.domain.entities import AuditFinding, AuditReport, ScanResult, Task
from localdocmask.domain.enums import AuditVerdict, TaskStatus
from localdocmask.domain.exceptions import (
    QueueFullError, SecurityViolation, TaskCancelledError, TaskTimedOutError,
)
from localdocmask.infrastructure.config.settings import Settings
from localdocmask.infrastructure.llm.ollama import OllamaError
from localdocmask.infrastructure.persistence.recovery import RecoveryService
from localdocmask.infrastructure.persistence.sqlite_repo import SqliteTaskRepository
from localdocmask.recognition.rule_based import Detector
from localdocmask.security.hashing import sha256_file
from localdocmask.security.redaction.masker import Masker


def _docx(path: Path, text: str = "联系人 13812345678") -> Path:
    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))
    return path


def _confirm_candidates(task: Task) -> None:
    for candidate in task.scan.candidates:
        candidate.confirmed = True
        candidate.excluded = False


def _settings(**overrides) -> Settings:
    settings = Settings()
    settings.security.retry_base_delay_seconds = 0.0
    settings.security.retry_max_delay_seconds = 0.0
    for key, value in overrides.items():
        setattr(settings.security, key, value)
    return settings


@pytest.fixture()
def output_dirs(tmp_path, monkeypatch):
    processing = tmp_path / "processing"
    success = tmp_path / "success"
    quarantine = tmp_path / "quarantine"
    for path in (processing, success, quarantine):
        path.mkdir()
    monkeypatch.setattr(pipeline_mod, "PROCESSING_DIR", processing)
    monkeypatch.setattr(pipeline_mod, "SUCCESS_DIR", success)
    monkeypatch.setattr(pipeline_mod, "QUARANTINE_DIR", quarantine)
    return processing, success, quarantine


def _run_pipeline(tm: TaskManager, source: Path, token: CancelToken | None = None,
                  context: TaskExecutionContext | None = None) -> Task:
    task = tm.prepare(source, mode="a", cancel_token=token,
                      execution_context=context)
    tm.scan(task, execution_context=context)
    _confirm_candidates(task)
    tm.process(task, "a", "****", task.scan.candidates,
               execution_context=context)
    return task


def test_batch_stops_between_files_after_cancel(tmp_path, output_dirs):
    repo = SqliteTaskRepository(tmp_path / "batch-boundary.db")
    token = CancelToken()
    settings = _settings()
    tm = TaskManager(Detector(), repository=repo, settings=settings)
    service = BatchService(tm, settings)
    first = _docx(tmp_path / "first.docx", "普通内容一")
    second = _docx(tmp_path / "second.docx", "普通内容二")
    original = service._run_task_in_slot

    def cancel_after_first(*args, **kwargs):
        task = original(*args, **kwargs)
        if task.status == TaskStatus.PASSED:
            token.cancel("between_files")
        return task

    service._run_task_in_slot = cancel_after_first
    try:
        result = service.run([first, second], mode="a", cancel_token=token)
        assert [t.source_path.name for t in result.passed] == ["first.docx"]
        assert result.review == [] and result.failed == [] and result.skipped == []
        assert any("剩余 1 个文件" in warning for warning in result.warnings)
        stored = repo.list_tasks()
        assert [t.source_path.name for t in stored] == ["first.docx"]
        assert len(list(output_dirs[1].glob("masked_*.docx"))) == 1
    finally:
        repo.close()


def test_scan_cancel_persists_cancelled_event(tmp_path, output_dirs, monkeypatch):
    token = CancelToken()
    repo = SqliteTaskRepository(tmp_path / "scan-cancel.db")
    tm = TaskManager(Detector(), repository=repo, settings=_settings())
    source = _docx(tmp_path / "scan.docx")

    class CancelDuringExtract:
        name = "docx"

        def extract(self, path):
            token.cancel("scan_test")
            return ScanResult()

    monkeypatch.setattr(pipeline_mod, "handler_for", lambda *a, **k: CancelDuringExtract())
    try:
        task = tm.prepare(source, mode="a", cancel_token=token)
        with pytest.raises(TaskCancelledError):
            tm.scan(task)
        assert task.status == TaskStatus.CANCELLED
        stored = repo.load_task(task.task_id)
        assert stored is not None and stored.status == TaskStatus.CANCELLED
        events = repo.list_events(task.task_id)
        assert any(e["event_code"] == "task.cancelled" and e["stage"] == "scan"
                   for e in events)
        assert not list(output_dirs[1].iterdir())
        assert repo.verify_event_chain().valid
    finally:
        repo.close()


def test_process_candidate_loop_cancel_is_terminal(tmp_path, output_dirs, monkeypatch):
    token = CancelToken()
    repo = SqliteTaskRepository(tmp_path / "process-cancel.db")
    tm = TaskManager(Detector(), repository=repo, settings=_settings())
    source = _docx(tmp_path / "process.docx", "张三 13812345678")
    original_replacement = Masker.replacement

    def cancel_on_candidate(self, category, value):
        token.cancel("candidate_loop")
        return original_replacement(self, category, value)

    monkeypatch.setattr(Masker, "replacement", cancel_on_candidate)
    try:
        task = tm.prepare(source, mode="a", cancel_token=token)
        tm.scan(task)
        _confirm_candidates(task)
        with pytest.raises(TaskCancelledError):
            tm.process(task, "a", "****", task.scan.candidates)
        assert task.status == TaskStatus.CANCELLED
        events = repo.list_events(task.task_id)
        assert any(e["event_code"] == "task.cancelled" and e["stage"] == "process"
                   for e in events)
        assert not list(output_dirs[1].iterdir())
    finally:
        repo.close()


def test_audit_cancel_cleans_artifacts(tmp_path, output_dirs):
    token = CancelToken()
    repo = SqliteTaskRepository(tmp_path / "audit-cancel.db")
    tm = TaskManager(Detector(), repository=repo, settings=_settings())
    source = _docx(tmp_path / "audit.docx")
    before = sha256_file(source)

    def cancel_audit(path, terms):
        token.cancel("audit_test")
        return AuditReport(
            passed=True,
            findings=[AuditFinding(AuditVerdict.NO_RESIDUAL, "synthetic ok", "text")],
        )

    tm.auditor.audit = cancel_audit
    try:
        task = tm.prepare(source, mode="a", cancel_token=token)
        tm.scan(task)
        _confirm_candidates(task)
        with pytest.raises(TaskCancelledError):
            tm.process(task, "a", "****", task.scan.candidates)
        assert task.status == TaskStatus.CANCELLED
        assert sha256_file(source) == before
        assert not list(output_dirs[1].iterdir())
        assert list(output_dirs[2].iterdir())
        codes = {e["event_code"] for e in repo.list_events(task.task_id)}
        assert "task.cancelled" in codes
    finally:
        repo.close()


def test_finalize_precheck_cancel_has_no_success_file(tmp_path, output_dirs):
    token = CancelToken()
    repo = SqliteTaskRepository(tmp_path / "finalize-cancel.db")
    tm = TaskManager(Detector(), repository=repo, settings=_settings())
    source = _docx(tmp_path / "finalize.docx")
    before = sha256_file(source)
    try:
        task = _run_pipeline(tm, source, token)
        assert task.status == TaskStatus.PROCESSED_UNAUDITED
        token.cancel("before_finalize")
        with pytest.raises(TaskCancelledError):
            tm.finalize(task, task.audit)
        assert task.status == TaskStatus.CANCELLED
        assert sha256_file(source) == before
        assert not list(output_dirs[1].iterdir())
        events = repo.list_events(task.task_id)
        assert any(e["event_code"] == "task.cancelled" and e["stage"] == "finalize"
                   for e in events)
    finally:
        repo.close()


def test_cancel_after_atomic_copy_cannot_become_passed(tmp_path, output_dirs, monkeypatch):
    token = CancelToken()
    repo = SqliteTaskRepository(tmp_path / "race-cancel.db")
    tm = TaskManager(Detector(), repository=repo, settings=_settings())
    source = _docx(tmp_path / "race.docx")
    original_copy = pipeline_mod.atomic_copy

    def copy_then_cancel(*args, **kwargs):
        result = original_copy(*args, **kwargs)
        token.cancel("after_atomic_copy")
        return result

    monkeypatch.setattr(pipeline_mod, "atomic_copy", copy_then_cancel)
    try:
        task = _run_pipeline(tm, source, token)
        with pytest.raises(TaskCancelledError):
            tm.finalize(task, task.audit)
        assert task.status == TaskStatus.CANCELLED
        stored = repo.load_task(task.task_id)
        assert stored is not None and stored.status != TaskStatus.PASSED
        assert not list(output_dirs[1].iterdir())
        assert repo.verify_event_chain().valid
    finally:
        repo.close()


def test_slow_handler_times_out_and_late_success_is_rejected(tmp_path, output_dirs):
    token = CancelToken()
    context = TaskExecutionContext(
        token, task_timeout_seconds=10.0,
        phase_budgets={"admission": 1.0, "scan": 3.0,
                       "process": 0.05, "audit": 3.0, "finalize": 1.0})
    repo = SqliteTaskRepository(tmp_path / "timeout.db")
    tm = TaskManager(Detector(), repository=repo, settings=_settings())
    source = _docx(tmp_path / "timeout.docx")
    before = sha256_file(source)

    def late_success(in_path, out_path, candidates):
        shutil.copyfile(in_path, out_path)
        time.sleep(0.10)

    tm._run_handler = late_success
    try:
        task = tm.prepare(source, mode="a", execution_context=context)
        tm.scan(task, execution_context=context)
        _confirm_candidates(task)
        with pytest.raises(TaskTimedOutError):
            tm.process(task, "a", "****", task.scan.candidates,
                       execution_context=context)
        assert task.status == TaskStatus.TIMED_OUT
        assert sha256_file(source) == before
        assert not list(output_dirs[1].iterdir())
        assert list(output_dirs[2].iterdir())
        events = repo.list_events(task.task_id)
        assert any(e["event_code"] == "task.timed_out" and e["stage"] == "process"
                   for e in events)
        assert repo.verify_event_chain().valid
    finally:
        repo.close()


def test_independent_task_deadlines_do_not_cross_talk(tmp_path, monkeypatch):
    tm = TaskManager(settings=_settings())
    slow_token = CancelToken()
    fast_token = CancelToken()
    slow_context = TaskExecutionContext(slow_token, 2.0, {"scan": 0.05})
    fast_context = TaskExecutionContext(fast_token, 2.0, {"scan": 1.0})
    slow = _docx(tmp_path / "slow.docx")
    fast = _docx(tmp_path / "fast.docx")

    class SlowFastHandler:
        name = "docx"

        def extract(self, path):
            if "slow" in Path(path).name:
                time.sleep(0.10)
            return ScanResult()

    monkeypatch.setattr(pipeline_mod, "handler_for", lambda *a, **k: SlowFastHandler())
    slow_task = tm.prepare(slow, execution_context=slow_context)
    fast_task = tm.prepare(fast, execution_context=fast_context)

    with ThreadPoolExecutor(max_workers=2) as pool:
        slow_future = pool.submit(tm.scan, slow_task, execution_context=slow_context)
        fast_future = pool.submit(tm.scan, fast_task, execution_context=fast_context)
        with pytest.raises(TaskTimedOutError):
            slow_future.result(timeout=3)
        fast_future.result(timeout=3)

    assert slow_task.status == TaskStatus.TIMED_OUT
    assert fast_task.status not in (TaskStatus.TIMED_OUT, TaskStatus.CANCELLED)


def test_transient_database_lock_retries_then_succeeds(tmp_path, output_dirs, monkeypatch):
    repo = SqliteTaskRepository(tmp_path / "retry-success.db")
    settings = _settings(retry_max_attempts=2)
    tm = TaskManager(Detector(), repository=repo, settings=settings)
    source = _docx(tmp_path / "retry.docx")
    original_save = repo.save_task
    failed = {"done": False}

    def flaky_save(task, *, operation=None, events=None, release_claim=False):
        event_codes = {e.event_code for e in (events or [])}
        if not failed["done"] and "phase.scan.end" in event_codes:
            failed["done"] = True
            raise sqlite3.OperationalError("database is locked")
        return original_save(
            task, operation=operation, events=events,
            release_claim=release_claim)

    monkeypatch.setattr(repo, "save_task", flaky_save)
    try:
        task = _run_pipeline(tm, source)
        tm.finalize(task, task.audit)
        assert task.status == TaskStatus.PASSED
        attempts = [e for e in repo.list_events(task.task_id)
                    if e["event_code"] == "task.retry.attempt"]
        assert len(attempts) == 2
        assert "result=failed" in attempts[0]["detail"]
        assert "result=success" in attempts[1]["detail"]
        assert repo.verify_event_chain().valid
    finally:
        repo.close()


def test_non_retryable_errors_are_not_repeated(tmp_path, output_dirs):
    tm = TaskManager(settings=_settings())
    assert not tm.is_retryable_error(SecurityViolation("security"))
    assert not tm.is_retryable_error(TaskCancelledError("scan", "cancel"))
    assert not tm.is_retryable_error(TaskTimedOutError("process", "phase", 1.0))
    assert tm.is_retryable_error(OllamaError("无法连接本地 Ollama"))
    assert not tm.is_retryable_error(OllamaError("Ollama 返回 404"))

    repo = SqliteTaskRepository(tmp_path / "audit-no-retry.db")
    tm = TaskManager(Detector(), repository=repo, settings=_settings())
    source = _docx(tmp_path / "audit-no-retry.docx")
    calls = {"count": 0}

    def fail_audit(path, terms):
        calls["count"] += 1
        raise SecurityViolation("audit residual")

    tm.auditor.audit = fail_audit
    try:
        task = tm.prepare(source, mode="a")
        tm.scan(task)
        _confirm_candidates(task)
        with pytest.raises(SecurityViolation):
            tm.process(task, "a", "****", task.scan.candidates)
        assert calls["count"] == 1
        assert task.status != TaskStatus.PASSED
        assert not list(output_dirs[1].iterdir())
    finally:
        repo.close()


def test_retry_exhaustion_never_exports(tmp_path, output_dirs, monkeypatch):
    repo = SqliteTaskRepository(tmp_path / "retry-exhausted.db")
    settings = _settings(retry_max_attempts=2)
    tm = TaskManager(Detector(), repository=repo, settings=settings)
    source = _docx(tmp_path / "exhausted.docx")
    try:
        task = _run_pipeline(tm, source)
        original_save = repo.save_task
        failed_calls = {"count": 0}

        def always_lock_on_passed(task, *, operation=None, events=None,
                                  release_claim=False):
            codes = {e.event_code for e in (events or [])}
            if "phase.export.db_confirm" in codes:
                failed_calls["count"] += 1
                raise sqlite3.OperationalError("database is locked")
            return original_save(
                task, operation=operation, events=events,
                release_claim=release_claim)

        monkeypatch.setattr(repo, "save_task", always_lock_on_passed)
        with pytest.raises(Exception):
            tm.finalize(task, task.audit)
        assert failed_calls["count"] == 2
        assert task.status != TaskStatus.PASSED
        stored = repo.load_task(task.task_id)
        assert stored is not None and stored.status != TaskStatus.PASSED
        assert not list(output_dirs[1].iterdir())
        codes = [e["event_code"] for e in repo.list_events(task.task_id)]
        assert codes.count("task.retry.attempt") == 2
        assert "task.retry.exhausted" in codes
        assert "phase.export.db_confirm" not in codes
        assert repo.verify_event_chain().valid
    finally:
        repo.close()


def test_same_idempotency_key_concurrency_has_one_success(tmp_path, output_dirs):
    repo = SqliteTaskRepository(tmp_path / "concurrent.db")
    settings = _settings(max_workers=2, max_queue_depth=4)
    tm = TaskManager(Detector(), repository=repo, settings=settings)
    service = BatchService(tm, settings)
    source = _docx(tmp_path / "same.docx", "普通并发测试内容")
    before = sha256_file(source)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(service.run, [source], mode="a")
                       for _ in range(2)]
            results = [f.result(timeout=30) for f in futures]
        passed = sum(len(r.passed) for r in results)
        skipped = sum(len(r.skipped) for r in results)
        assert passed == 1
        assert skipped == 1
        outputs = list(output_dirs[1].glob("masked_*.docx"))
        assert len(outputs) == 1
        assert sha256_file(source) == before
        assert repo.verify_event_chain().valid
    finally:
        repo.close()


def test_queue_full_fails_fast_before_processing(tmp_path):
    settings = _settings(max_workers=1, max_queue_depth=1)
    tm = TaskManager(settings=settings)
    service = BatchService(tm, settings)
    first = _docx(tmp_path / "q1.docx")
    second = _docx(tmp_path / "q2.docx")
    prepared = {"count": 0}
    original_prepare = tm.prepare

    def counted_prepare(*args, **kwargs):
        prepared["count"] += 1
        return original_prepare(*args, **kwargs)

    tm.prepare = counted_prepare
    with pytest.raises(QueueFullError) as caught:
        service.run([first, second])
    assert caught.value.code.value == "queue_full"
    assert prepared["count"] == 0


def test_accepted_pending_claim_is_recoverable_before_execution(tmp_path, output_dirs):
    repo = SqliteTaskRepository(tmp_path / "queued-recovery.db")
    tm = TaskManager(Detector(), repository=repo, settings=_settings())
    source = _docx(tmp_path / "queued.docx")
    try:
        task = tm.prepare(source, mode="a")
        claim = repo.claim_or_get(task)
        assert claim.outcome == "created"
        assert repo.load_task(task.task_id).status == TaskStatus.PENDING
        RecoveryService().recover(repo, output_dirs[0])
        recovered = repo.load_task(task.task_id)
        assert recovered is not None and recovered.status == TaskStatus.PENDING
        assert repo.list_claims() == []
    finally:
        repo.close()


def test_concurrent_controller_does_not_deadlock(tmp_path):
    controller = ConcurrencyController(max_workers=2, max_queue_depth=4)
    barrier = threading.Barrier(2)

    def worker(index):
        token = CancelToken()
        context = TaskExecutionContext(token, 2.0)
        with controller.accept(1):
            with controller.worker_slot(context):
                barrier.wait(timeout=2)
                return index

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=5) for f in
                   [pool.submit(worker, 1), pool.submit(worker, 2)]]
    assert sorted(results) == [1, 2]


def test_pre_cancelled_token_blocks_admission(tmp_path, output_dirs):
    token = CancelToken()
    token.cancel("before_admission")
    repo = SqliteTaskRepository(tmp_path / "admission-cancel.db")
    tm = TaskManager(Detector(), repository=repo, settings=_settings())
    source = _docx(tmp_path / "admission.docx")
    try:
        with pytest.raises(TaskCancelledError) as caught:
            tm.prepare(source, mode="a", cancel_token=token)
        task = caught.value.task
        assert task.status == TaskStatus.CANCELLED
        assert repo.load_task(task.task_id).status == TaskStatus.CANCELLED
        events = repo.list_events(task.task_id)
        assert any(e["event_code"] == "task.cancelled" and e["stage"] == "admission"
                   for e in events)
        assert not list(output_dirs[1].iterdir())
    finally:
        repo.close()


def test_transient_temp_io_error_retries_handler(tmp_path, output_dirs):
    import errno

    repo = SqliteTaskRepository(tmp_path / "io-retry.db")
    settings = _settings(retry_max_attempts=2)
    tm = TaskManager(Detector(), settings=settings, repository=repo)
    source = _docx(tmp_path / "io-retry.docx", "普通内容")
    calls = {"count": 0}

    def flaky_handler(in_path, out_path, candidates):
        calls["count"] += 1
        if calls["count"] == 1:
            raise BlockingIOError(errno.EAGAIN, "temporary busy")
        shutil.copyfile(in_path, out_path)

    tm._run_handler = flaky_handler
    try:
        task = _run_pipeline(tm, source)
        tm.finalize(task, task.audit)
        assert task.status == TaskStatus.PASSED
        assert calls["count"] == 2
        attempts = [e for e in repo.list_events(task.task_id)
                    if e["event_code"] == "task.retry.attempt"]
        assert len(attempts) == 2
        assert repo.verify_event_chain().valid
    finally:
        repo.close()
