# -*- coding: utf-8 -*-
"""FIX-06：scan/process/audit 的可终止硬超时与并发隔离。"""
from __future__ import annotations

import multiprocessing as mp
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def _hang_forever() -> None:
    while True:
        time.sleep(60)


class HangingExtractHandler:
    name = "docx"

    def __init__(self, detector):
        self.detector = detector

    def extract(self, path):
        _hang_forever()

    def process(self, path, out_path, candidates):
        raise AssertionError("process should not run")


class HangingProcessHandler:
    name = "docx"

    def __init__(self, detector):
        self.detector = detector

    def extract(self, path):
        from localdocmask.domain.entities import ScanResult
        return ScanResult()

    def process(self, path, out_path, candidates):
        Path(out_path).write_bytes(b"partial-timeout-output")
        _hang_forever()


class SelectiveHandler:
    name = "docx"

    def __init__(self, detector):
        from localdocmask.infrastructure.formats.docx import DocxHandler
        self.detector = detector
        self._delegate = DocxHandler(detector)

    def extract(self, path):
        if "slow" in Path(path).name:
            _hang_forever()
        return self._delegate.extract(path)

    def process(self, path, out_path, candidates):
        if "slow" in Path(path).name:
            Path(out_path).write_bytes(b"slow-partial")
            _hang_forever()
        return self._delegate.process(path, out_path, candidates)


class HangingAuditor:
    def audit(self, out_path, forbidden_terms):
        _hang_forever()


class HangingNER:
    def extract_entities(self, text):
        _hang_forever()


def run_hanging_ner_worker(ner, text):
    return ner.extract_entities(text)


class HangingResidualAuditor:
    def residual_terms(self, out_path, terms):
        _hang_forever()


def _docx(path: Path, text: str = "联系人 13812345678") -> Path:
    from docx import Document

    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))
    return path


def _output_dirs(tmp_path, monkeypatch):
    import localdocmask.application.pipeline as pipeline_mod
    processing = tmp_path / "processing"
    success = tmp_path / "success"
    quarantine = tmp_path / "quarantine"
    for path in (processing, success, quarantine):
        path.mkdir()
    monkeypatch.setattr(pipeline_mod, "PROCESSING_DIR", processing)
    monkeypatch.setattr(pipeline_mod, "SUCCESS_DIR", success)
    monkeypatch.setattr(pipeline_mod, "QUARANTINE_DIR", quarantine)
    return processing, success, quarantine


def _settings():
    from localdocmask.infrastructure.config.settings import Settings
    settings = Settings()
    settings.security.retry_base_delay_seconds = 0.0
    settings.security.retry_max_delay_seconds = 0.0
    return settings


def _context(*, scan: float = 8.0, process: float = 8.0,
             audit: float = 8.0):
    from localdocmask.application.batch import CancelToken
    from localdocmask.application.pipeline import TaskExecutionContext

    return TaskExecutionContext(
        CancelToken(), 15.0,
        {"admission": 3.0, "scan": scan, "process": process,
         "audit": audit, "finalize": 3.0},
    )


def _assert_worker_cleaned(exc: TaskTimedOutError) -> None:
    assert getattr(exc, "worker_terminated", False) is True
    assert getattr(exc, "worker_joined", False) is True
    pid = getattr(exc, "worker_pid", None)
    assert pid
    mp.active_children()
    active = [child.pid for child in mp.active_children()]
    assert pid not in active
    print(
        "WORKER_CLEANED "
        f"pid={pid} terminated={getattr(exc, 'worker_terminated', False)} "
        f"killed={getattr(exc, 'worker_killed', False)} "
        f"joined={getattr(exc, 'worker_joined', False)} "
        f"exitcode={getattr(exc, 'worker_exitcode', None)} "
        f"active_children={active}")


def test_hanging_scan_is_hard_terminated(tmp_path, monkeypatch):
    import localdocmask.application.pipeline as pipeline_mod
    from localdocmask.application.pipeline import TaskManager
    from localdocmask.domain.enums import TaskStatus
    from localdocmask.domain.exceptions import TaskTimedOutError
    from localdocmask.infrastructure.persistence.sqlite_repo import SqliteTaskRepository
    from localdocmask.recognition.rule_based import Detector
    from localdocmask.security.hashing import sha256_file

    output_dirs = _output_dirs(tmp_path, monkeypatch)
    repo = SqliteTaskRepository(tmp_path / "scan-timeout.db")
    tm = TaskManager(Detector(), repository=repo, settings=_settings())
    source = _docx(tmp_path / "scan-hang.docx")
    before = sha256_file(source)
    context = _context(scan=0.25)
    monkeypatch.setattr(
        pipeline_mod, "handler_for",
        lambda *args, **kwargs: HangingExtractHandler(Detector()))
    try:
        task = tm.prepare(source, mode="a", execution_context=context)
        started = time.monotonic()
        with pytest.raises(TaskTimedOutError) as caught:
            tm.scan(task, execution_context=context)
        elapsed = time.monotonic() - started

        assert elapsed < 3.0
        assert task.status == TaskStatus.TIMED_OUT
        stored = repo.load_task(task.task_id)
        assert stored is not None and stored.status == TaskStatus.TIMED_OUT
        assert sha256_file(source) == before
        assert not list(output_dirs[1].iterdir())
        events = repo.list_events(task.task_id)
        assert any(e["event_code"] == "task.timed_out" and e["stage"] == "scan"
                   for e in events)
        assert repo.verify_event_chain().valid
        _assert_worker_cleaned(caught.value)
    finally:
        repo.close()


def test_hanging_process_is_hard_terminated_and_quarantined(
        tmp_path, monkeypatch):
    import localdocmask.application.pipeline as pipeline_mod
    from localdocmask.application.pipeline import TaskManager
    from localdocmask.domain.enums import TaskStatus
    from localdocmask.domain.exceptions import TaskTimedOutError
    from localdocmask.infrastructure.persistence.sqlite_repo import SqliteTaskRepository
    from localdocmask.recognition.rule_based import Detector
    from localdocmask.security.hashing import sha256_file

    output_dirs = _output_dirs(tmp_path, monkeypatch)
    repo = SqliteTaskRepository(tmp_path / "process-timeout.db")
    tm = TaskManager(Detector(), repository=repo, settings=_settings())
    source = _docx(tmp_path / "process-hang.docx", "普通内容")
    before = sha256_file(source)
    context = _context(scan=8.0, process=0.25)
    monkeypatch.setattr(
        pipeline_mod, "handler_for",
        lambda *args, **kwargs: HangingProcessHandler(Detector()))
    try:
        task = tm.prepare(source, mode="a", execution_context=context)
        tm.scan(task, execution_context=context)
        started = time.monotonic()
        with pytest.raises(TaskTimedOutError) as caught:
            tm.process(task, "a", "****", task.scan.candidates,
                       execution_context=context)
        elapsed = time.monotonic() - started

        assert elapsed < 3.0
        assert task.status == TaskStatus.TIMED_OUT
        stored = repo.load_task(task.task_id)
        assert stored is not None and stored.status == TaskStatus.TIMED_OUT
        assert sha256_file(source) == before
        assert not list(output_dirs[1].iterdir())
        assert list(output_dirs[2].iterdir())
        events = repo.list_events(task.task_id)
        assert any(e["event_code"] == "task.timed_out" and e["stage"] == "process"
                   for e in events)
        assert repo.verify_event_chain().valid
        _assert_worker_cleaned(caught.value)
    finally:
        repo.close()


def test_hanging_audit_is_hard_terminated(tmp_path, monkeypatch):
    from localdocmask.application.pipeline import TaskManager
    from localdocmask.domain.enums import TaskStatus
    from localdocmask.domain.exceptions import TaskTimedOutError
    from localdocmask.infrastructure.persistence.sqlite_repo import SqliteTaskRepository
    from localdocmask.recognition.rule_based import Detector
    from localdocmask.security.hashing import sha256_file

    output_dirs = _output_dirs(tmp_path, monkeypatch)
    repo = SqliteTaskRepository(tmp_path / "audit-timeout.db")
    tm = TaskManager(Detector(), repository=repo, settings=_settings())
    source = _docx(tmp_path / "audit-hang.docx")
    before = sha256_file(source)
    context = _context(scan=8.0, process=8.0, audit=8.0)
    try:
        task = tm.prepare(source, mode="a", execution_context=context)
        tm.scan(task, execution_context=context)
        task.scan.candidates.clear()
        tm.process(task, "a", "****", task.scan.candidates,
                   execution_context=context)
        assert task.status == TaskStatus.PROCESSED_UNAUDITED
        tm.auditor = HangingAuditor()
        audit_context = _context(scan=8.0, process=8.0, audit=0.25)

        started = time.monotonic()
        with pytest.raises(TaskTimedOutError) as caught:
            tm.audit(task, task.scan.candidates,
                     execution_context=audit_context)
        elapsed = time.monotonic() - started

        assert elapsed < 3.0
        assert task.status == TaskStatus.TIMED_OUT
        stored = repo.load_task(task.task_id)
        assert stored is not None and stored.status == TaskStatus.TIMED_OUT
        assert sha256_file(source) == before
        assert not list(output_dirs[1].iterdir())
        events = repo.list_events(task.task_id)
        assert any(e["event_code"] == "task.timed_out" and e["stage"] == "audit"
                   for e in events)
        assert repo.verify_event_chain().valid
        _assert_worker_cleaned(caught.value)
    finally:
        repo.close()


def test_concurrent_timeout_does_not_kill_normal_task(
        tmp_path, monkeypatch):
    import localdocmask.application.pipeline as pipeline_mod
    from localdocmask.application.pipeline import TaskManager
    from localdocmask.domain.enums import TaskStatus
    from localdocmask.domain.exceptions import TaskTimedOutError
    from localdocmask.infrastructure.persistence.sqlite_repo import SqliteTaskRepository
    from localdocmask.recognition.rule_based import Detector

    output_dirs = _output_dirs(tmp_path, monkeypatch)
    slow_repo = SqliteTaskRepository(tmp_path / "slow.db")
    fast_repo = SqliteTaskRepository(tmp_path / "fast.db")
    settings = _settings()
    slow_tm = TaskManager(Detector(), repository=slow_repo, settings=settings)
    fast_tm = TaskManager(Detector(), repository=fast_repo, settings=settings)
    slow_source = _docx(tmp_path / "slow-concurrent.docx")
    fast_source = _docx(tmp_path / "fast-concurrent.docx")
    slow_context = _context(scan=0.25, process=8.0, audit=8.0)
    fast_context = _context(scan=8.0, process=8.0, audit=8.0)
    monkeypatch.setattr(
        pipeline_mod, "handler_for",
        lambda *args, **kwargs: SelectiveHandler(Detector()))

    slow_task = slow_tm.prepare(slow_source, mode="a",
                                execution_context=slow_context)
    fast_task = fast_tm.prepare(fast_source, mode="a",
                                execution_context=fast_context)

    def run_slow():
        with pytest.raises(TaskTimedOutError) as caught:
            slow_tm.scan(slow_task, execution_context=slow_context)
        return caught.value

    def run_fast():
        scan = fast_tm.scan(fast_task, execution_context=fast_context)
        for candidate in scan.candidates:
            candidate.confirmed = True
            candidate.excluded = False
        fast_tm.process(fast_task, "a", "****", scan.candidates,
                        execution_context=fast_context)
        report = fast_tm.audit(fast_task, scan.candidates,
                               execution_context=fast_context)
        return fast_tm.finalize(fast_task, report,
                                execution_context=fast_context)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow_future = pool.submit(run_slow)
            fast_future = pool.submit(run_fast)
            slow_error = slow_future.result(timeout=20)
            completed = fast_future.result(timeout=20)

        assert slow_task.status == TaskStatus.TIMED_OUT
        assert completed.status == TaskStatus.PASSED
        assert completed.task_id == fast_task.task_id
        assert not list(output_dirs[0].glob(f"work_{slow_task.task_id}_*"))
        successes = list(output_dirs[1].glob("masked_*.docx"))
        assert len(successes) == 1
        assert f"masked_{fast_task.task_id}.docx" == successes[0].name
        assert slow_repo.verify_event_chain().valid
        assert fast_repo.verify_event_chain().valid
        _assert_worker_cleaned(slow_error)
        assert mp.active_children() == []
    finally:
        slow_repo.close()
        fast_repo.close()


def test_unpicklable_operation_fails_closed_in_worker():
    from localdocmask.application.process_guard import (
        GuardedWorker, UnpicklableOperationError,
    )

    worker = GuardedWorker()
    try:
        with pytest.raises(UnpicklableOperationError):
            worker.run(
                lambda: None,
                timeout_seconds=1.0,
                stage="scan",
            )
    finally:
        worker.close()


def test_review_task_closes_worker_and_can_resume(tmp_path, monkeypatch):
    import localdocmask.application.pipeline as pipeline_mod
    from localdocmask.application.batch import BatchService
    from localdocmask.application.pipeline import TaskManager
    from localdocmask.domain.enums import TaskStatus
    from localdocmask.infrastructure.persistence.sqlite_repo import SqliteTaskRepository
    from localdocmask.recognition.rule_based import Detector
    from localdocmask.security.hashing import sha256_file

    processing, success, _ = _output_dirs(tmp_path, monkeypatch)
    repo = SqliteTaskRepository(tmp_path / "review-worker.db")
    settings = _settings()
    tm = TaskManager(Detector(), repository=repo, settings=settings)
    service = BatchService(tm, settings)
    source = _docx(tmp_path / "review-worker.docx")
    before = sha256_file(source)
    try:
        result = service.run([source], mode="a")
        assert len(result.review) == 1
        task = result.review[0]
        assert task.status == TaskStatus.NEEDS_REVIEW
        context = tm.get_execution_context(task)
        assert context._hard_worker is None
        assert not list(processing.glob(f"work_{task.task_id}_*"))
        assert mp.active_children() == []

        for candidate in task.scan.candidates:
            candidate.confirmed = True
            candidate.excluded = False
        tm.process(task, "a", "****", task.scan.candidates)
        assert task.status == TaskStatus.PROCESSED_UNAUDITED
        assert not list(success.iterdir())
        tm.finalize(task, task.audit)
        assert task.status == TaskStatus.PASSED
        assert sha256_file(source) == before
        assert len(list(success.iterdir())) >= 1
        assert mp.active_children() == []
    finally:
        repo.close()


def test_hanging_ner_is_hard_terminated(tmp_path, monkeypatch):
    import localdocmask.application.pipeline as pipeline_mod
    from localdocmask.application.pipeline import TaskManager
    from localdocmask.domain.enums import TaskStatus
    from localdocmask.domain.exceptions import TaskTimedOutError
    from localdocmask.infrastructure.persistence.sqlite_repo import SqliteTaskRepository
    from localdocmask.recognition.pipeline import RecognitionPipeline
    from localdocmask.security.hashing import sha256_file

    processing, success, quarantine = _output_dirs(tmp_path, monkeypatch)
    repo = SqliteTaskRepository(tmp_path / "ner-timeout.db")
    settings = _settings()
    recognition = RecognitionPipeline(
        ner=HangingNER(),
        ner_worker_target=run_hanging_ner_worker,
        ner_worker_args=(HangingNER(),),
    )
    tm = TaskManager(recognition=recognition, repository=repo, settings=settings)
    source = _docx(tmp_path / "ner-hang.docx")
    before = sha256_file(source)
    context = _context(scan=0.25, process=8.0, audit=8.0)
    try:
        task = tm.prepare(source, mode="a", execution_context=context)
        started = time.monotonic()
        with pytest.raises(TaskTimedOutError) as caught:
            tm.scan(task, execution_context=context)
        elapsed = time.monotonic() - started

        assert elapsed < 3.0
        assert task.status == TaskStatus.TIMED_OUT
        assert sha256_file(source) == before
        assert not list(success.iterdir())
        assert not list(quarantine.iterdir())
        events = repo.list_events(task.task_id)
        assert any(e["event_code"] == "task.timed_out" and e["stage"] == "scan"
                   for e in events)
        assert repo.verify_event_chain().valid
        _assert_worker_cleaned(caught.value)
    finally:
        repo.close()


def test_hanging_residual_terms_is_hard_terminated(tmp_path, monkeypatch):
    import localdocmask.application.pipeline as pipeline_mod
    from localdocmask.application.pipeline import TaskManager
    from localdocmask.domain.enums import TaskStatus
    from localdocmask.domain.exceptions import TaskTimedOutError
    from localdocmask.infrastructure.persistence.sqlite_repo import SqliteTaskRepository
    from localdocmask.recognition.rule_based import Detector
    from localdocmask.security.hashing import sha256_file

    processing, success, quarantine = _output_dirs(tmp_path, monkeypatch)
    repo = SqliteTaskRepository(tmp_path / "residual-timeout.db")
    settings = _settings()
    tm = TaskManager(Detector(), repository=repo, settings=settings)
    source = _docx(tmp_path / "residual-hang.docx", "普通内容")
    before = sha256_file(source)
    context = _context(scan=8.0, process=0.25, audit=8.0)
    tm.auditor = HangingResidualAuditor()
    try:
        task = tm.prepare(source, mode="a", execution_context=context)
        tm.scan(task, execution_context=context)
        task.scan.candidates.clear()
        started = time.monotonic()
        with pytest.raises(TaskTimedOutError) as caught:
            tm.process(task, "a", "****", task.scan.candidates,
                       execution_context=context)
        elapsed = time.monotonic() - started

        assert elapsed < 3.0
        assert task.status == TaskStatus.TIMED_OUT
        assert sha256_file(source) == before
        assert not list(success.iterdir())
        assert list(quarantine.iterdir())
        assert not list(processing.glob(f"work_{task.task_id}_*"))
        events = repo.list_events(task.task_id)
        assert any(e["event_code"] == "task.timed_out" and e["stage"] == "process"
                   for e in events)
        assert repo.verify_event_chain().valid
        _assert_worker_cleaned(caught.value)
    finally:
        repo.close()


def test_pyinstaller_freeze_support_is_wired():
    root = Path(__file__).resolve().parents[1]
    main = (root / "main.py").read_text(encoding="utf-8")
    spec = (root / "LocalDocMask.spec").read_text(encoding="utf-8")
    assert "multiprocessing.freeze_support()" in main
    assert "localdocmask.application.process_guard" in spec
