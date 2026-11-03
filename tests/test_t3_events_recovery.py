# -*- coding: utf-8 -*-
"""T3：事件哈希链、原子状态变更、启动恢复。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document
from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtWidgets import QApplication, QMessageBox

from localdocmask.application.batch import BatchService
from localdocmask.application.pipeline import TaskManager
from localdocmask.domain.entities import ScanResult, Task, TaskEvent
from localdocmask.domain.enums import TaskStatus
from localdocmask.domain.exceptions import InvalidTaskTransition
from localdocmask.domain.state_machine import transition_task
from localdocmask.infrastructure.config.settings import Settings
from localdocmask.infrastructure.persistence import sqlite_repo
from localdocmask.infrastructure.persistence.recovery import RecoveryService
from localdocmask.infrastructure.persistence.sqlite_repo import (
    SCHEMA_VERSION,
    SqliteTaskRepository,
    initialize_database,
    migrate,
    restore_database,
)
from localdocmask.recognition.rule_based import Detector
from localdocmask.security.hashing import sha256_file
import localdocmask.application.pipeline as pipeline_mod
import localdocmask.presentation.gui.main_window as gui_mod


def _task(tmp_path: Path, task_id: str, status=TaskStatus.PENDING,
          *, source_hash="H", mode="a", policy="p", cfg="c", tool="t",
          with_output=False, config_hash=None) -> Task:
    src = tmp_path / f"{task_id}-src.docx"
    src.write_bytes(b"source")
    out = tmp_path / f"{task_id}-out.docx"
    if with_output:
        out.write_bytes(b"output")
    return Task(
        source_path=src,
        task_id=task_id,
        file_type="docx",
        status=status,
        source_hash=source_hash,
        mode=mode,
        policy_version=policy,
        config_hash=cfg if config_hash is None else config_hash,
        tool_version=tool,
        output_path=out if with_output else None,
        final_hash=sha256_file(out) if with_output else "",
        scan=ScanResult(candidates=[]),
    )


def _event(code="test.event", stage="test", from_status="PENDING",
           to_status="PENDING", detail="step=1", error_code="") -> TaskEvent:
    return TaskEvent(event_code=code, stage=stage,
                     from_status=from_status, to_status=to_status,
                     detail=detail, error_code=error_code, actor="test-actor")


def _seed_events(repo: SqliteTaskRepository, task_id="t-events", count=3):
    task = _task(repo._db_path.parent, task_id)
    repo.save_task(task)
    for i in range(1, count + 1):
        repo.save_task(task, events=[_event(
            code=f"test.event.{i}", detail=f"step={i}")])
    return task


def test_v2_to_v3_migration_backup_restore_integrity(tmp_path):
    db = tmp_path / "v2.db"
    assert migrate(db, 0, 2) == 2
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, source_path, source_hash, status, created_at, updated_at) "
            "VALUES ('legacy','x.docx','LEGACY','PENDING','now','now')")
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(str(db))
    try:
        tables_v2 = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "task_events" not in tables_v2

    backup = initialize_database(db)
    assert backup is not None and backup.exists()
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "task_events" in tables
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE task_id='legacy'").fetchone()[0] == 1
    finally:
        conn.close()

    restore_database(backup, db)
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "task_events" not in tables
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE task_id='legacy'").fetchone()[0] == 1
    finally:
        conn.close()


def test_v2_to_v3_migration_failure_rolls_back(tmp_path, monkeypatch):
    db = tmp_path / "v2fail.db"
    assert migrate(db, 0, 2) == 2

    def broken_v3(conn):
        conn.execute("CREATE TABLE task_events (event_id TEXT PRIMARY KEY)")
        raise RuntimeError("inject v3 failure")

    monkeypatch.setitem(sqlite_repo._MIGRATIONS, 3, broken_v3)
    with pytest.raises(RuntimeError):
        initialize_database(db)
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "task_events" not in tables
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert list(tmp_path.glob("v2fail.db.bak-*")), "v2->v3 迁移前备份缺失"


def test_pipeline_writes_phase_and_transition_events(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "pipeline.db")
    tm = TaskManager(Detector(persons=["张三"]), repository=repo, settings=Settings())
    try:
        src = tmp_path / "event.docx"
        d = Document()
        d.add_paragraph("张三 13812345678")
        d.save(str(src))
        task = tm.prepare(src, mode="a")
        scan = tm.scan(task)
        tm.process(task, "a", "****", scan.candidates)
        tm.audit(task, scan.candidates)
        tm.finalize(task, task.audit)

        events = repo.list_events(task.task_id)
        codes = {e["event_code"] for e in events}
        for required in (
            "phase.prepare.start", "phase.prepare.end",
            "phase.scan.start", "phase.scan.end",
            "phase.process.start", "phase.process.end",
            "phase.audit.start", "phase.audit.end",
            "phase.finalize.start", "phase.finalize.end",
        ):
            assert required in codes, (required, sorted(codes))
        assert all(e["actor"] for e in events)
        assert any(e["source_hash"] == task.source_hash for e in events)
        assert any(e["output_hash"] == task.final_hash for e in events)
        assert any(
            e["policy_version"] == task.policy_version
            and e["config_hash"] == task.config_hash
            and e["tool_version"] == task.tool_version
            for e in events)
        report = repo.verify_event_chain()
        assert report.valid, report.reason
    finally:
        repo.close()


def test_illegal_transition_rejected_without_state_change(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "illegal.db")
    tm = TaskManager(Detector(), repository=repo, settings=Settings())
    task = _task(tmp_path, "t-passed", status=TaskStatus.PASSED, with_output=True)
    repo.save_task(task)
    try:
        with pytest.raises(InvalidTaskTransition):
            tm.scan(task)
        assert task.status == TaskStatus.PASSED
        stored = repo.load_task(task.task_id)
        assert stored is not None and stored.status == TaskStatus.PASSED
        assert any(e["event_code"] == "task.transition.rejected"
                   for e in repo.list_events(task.task_id))
    finally:
        repo.close()


def test_event_write_failure_rolls_back_task_state(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "event-fail.db")
    task = _task(tmp_path, "t-event-fail")
    repo.save_task(task)
    repo._conn.execute(
        "CREATE TRIGGER fail_event BEFORE INSERT ON task_events "
        "BEGIN SELECT RAISE(ABORT, 'inject event failure'); END")
    transition_task(task, TaskStatus.PROCESSING)
    with pytest.raises(sqlite3.DatabaseError):
        repo.save_task(task, events=[_event(from_status="PENDING",
                                            to_status="PROCESSING")])
    stored = repo.load_task(task.task_id)
    assert stored is not None and stored.status == TaskStatus.PENDING
    repo.close()


def test_events_do_not_contain_candidate_original_values(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "safe-events.db")
    tm = TaskManager(Detector(persons=["张三"]), repository=repo, settings=Settings())
    try:
        src = tmp_path / "safe.docx"
        d = Document()
        d.add_paragraph("张三 13812345678")
        d.save(str(src))
        task = tm.prepare(src, mode="a")
        scan = tm.scan(task)
        tm.process(task, "a", "****", scan.candidates)
        tm.audit(task, scan.candidates)
        tm.finalize(task, task.audit)
        blob = json.dumps(repo.list_events(task.task_id), ensure_ascii=False)
        assert "13812345678" not in blob
        assert "张三" not in blob
        assert task.task_id in blob
        assert "PENDING" in blob and "PROCESSING" in blob
        assert "scan" in blob and "finalize" in blob
    finally:
        repo.close()


@pytest.mark.parametrize("column,value", [
    ("detail", "tampered"),
    ("from_status", "PROCESSING"),
    ("occurred_at", "1999-01-01T00:00:00Z"),
    ("error_code", "tampered-error"),
])
def test_event_chain_detects_field_tampering(tmp_path, column, value):
    db = tmp_path / "tamper.db"
    repo = SqliteTaskRepository(db)
    _seed_events(repo)
    events = repo.list_events()
    target = events[1] if len(events) > 1 else events[0]
    repo._conn.execute(
        f"UPDATE task_events SET {column}=? WHERE event_id=?",
        (value, target["event_id"]))
    repo._conn.commit()
    report = repo.verify_event_chain()
    assert not report.valid
    assert report.first_bad_seq == target["global_seq"]
    repo.close()


def test_event_chain_detects_deleted_middle_event(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "delete.db")
    _seed_events(repo)
    events = repo.list_events()
    repo._conn.execute(
        "DELETE FROM task_events WHERE event_id=?", (events[1]["event_id"],))
    repo._conn.commit()
    report = repo.verify_event_chain()
    assert not report.valid
    assert report.first_bad_seq == events[2]["global_seq"]
    repo.close()


def test_event_chain_detects_swapped_order(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "swap.db")
    _seed_events(repo)
    events = repo.list_events()
    a, b = events[0], events[1]
    repo._conn.execute("UPDATE task_events SET global_seq=-1 WHERE event_id=?", (a["event_id"],))
    repo._conn.execute("UPDATE task_events SET global_seq=? WHERE event_id=?", (a["global_seq"], b["event_id"]))
    repo._conn.execute("UPDATE task_events SET global_seq=? WHERE event_id=?", (b["global_seq"], a["event_id"]))
    repo._conn.commit()
    report = repo.verify_event_chain()
    assert not report.valid
    repo.close()


def test_recovery_processing_no_output_to_pending(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "recover1.db")
    task = _task(tmp_path, "r-nooutput", status=TaskStatus.PROCESSING)
    repo.save_task(task)
    affected = RecoveryService().recover(repo, tmp_path / "processing")
    loaded = repo.load_task(task.task_id)
    assert loaded is not None and loaded.status == TaskStatus.PENDING
    assert any(t.task_id == task.task_id for t in affected)
    assert any(e["event_code"] == "recovery.requeue"
               for e in repo.list_events(task.task_id))
    repo.close()


def test_recovery_processing_with_output_to_interrupted(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "recover2.db")
    task = _task(tmp_path, "r-output", status=TaskStatus.PROCESSING,
                 with_output=True)
    repo.save_task(task)
    RecoveryService().recover(repo, tmp_path / "processing")
    loaded = repo.load_task(task.task_id)
    assert loaded is not None and loaded.status == TaskStatus.INTERRUPTED
    assert any(e["event_code"] == "recovery.interrupted"
               for e in repo.list_events(task.task_id))
    repo.close()


def test_recovery_unknown_status_persisted_and_logged(tmp_path):
    db = tmp_path / "recover3.db"
    repo = SqliteTaskRepository(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, source_path, status, created_at, updated_at) "
            "VALUES ('unknown-1','x.docx','FUTURE','now','now')")
        conn.commit()
    finally:
        conn.close()
    initialize_database(db)
    loaded = repo.load_task("unknown-1")
    assert loaded is not None and loaded.status == TaskStatus.INTERRUPTED
    assert any(e["event_code"] == "recovery.unknown_status"
               for e in repo.list_events("unknown-1"))
    assert all(t.task_id != "unknown-1" for t in repo.list_tasks(TaskStatus.PENDING))
    repo.close()


def test_pending_claim_released_on_recovery_and_retryable(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "recover4.db")
    task = _task(tmp_path, "claim-pending")
    claim = repo.claim_or_get(task)
    assert claim.outcome == "created"
    RecoveryService().recover(repo, tmp_path / "processing")
    assert repo.list_claims() == []
    loaded = repo.load_task(task.task_id)
    assert loaded is not None and loaded.status == TaskStatus.PENDING
    assert any(e["event_code"] == "recovery.pending_claim_released"
               for e in repo.list_events(task.task_id))

    retry = _task(tmp_path, "claim-retry")
    result = repo.claim_or_get(retry)
    assert result.outcome == "created"
    assert result.task.status == TaskStatus.PENDING
    repo.close()


def test_recovery_persistence_failure_fails_closed(tmp_path, monkeypatch):
    repo = SqliteTaskRepository(tmp_path / "recover5.db")
    task = _task(tmp_path, "recover-fail", status=TaskStatus.PROCESSING)
    repo.save_task(task)

    def fail_save(*args, **kwargs):
        raise RuntimeError("recovery persistence failure")

    monkeypatch.setattr(repo, "save_task", fail_save)
    with pytest.raises(Exception):
        RecoveryService().recover(repo, tmp_path / "processing")
    loaded = repo.load_task(task.task_id)
    assert loaded is not None
    assert loaded.status != TaskStatus.PASSED
    repo.close()


def test_event_chain_supports_task_scope(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "scope.db")
    t1 = _seed_events(repo, task_id="scope-1", count=3)
    _seed_events(repo, task_id="scope-2", count=2)
    report = repo.verify_event_chain(task_id=t1.task_id)
    assert report.valid
    assert report.checked == 3
    events = repo.list_events(t1.task_id)
    repo._conn.execute(
        "UPDATE task_events SET detail='tampered' WHERE event_id=?",
        (events[0]["event_id"],))
    repo._conn.commit()
    report = repo.verify_event_chain(task_id=t1.task_id)
    assert not report.valid
    assert report.first_bad_seq == events[0]["global_seq"]
    repo.close()


class _ReviewFlagHandler:
    def extract(self, path):
        return ScanResult(
            candidates=[], text="SECRET BODY SHOULD NOT APPEAR",
            warnings=[], review_flags=["mixed_layout_uncheckable"])


def test_scan_review_flags_persists_needs_review(tmp_path, monkeypatch):
    repo = SqliteTaskRepository(tmp_path / "reviewflag.db")
    monkeypatch.setattr(pipeline_mod, "handler_for", lambda *a, **k: _ReviewFlagHandler())
    tm = TaskManager(Detector(), repository=repo, settings=Settings())
    try:
        src = tmp_path / "reviewflag.docx"
        doc = Document()
        doc.add_paragraph("review flag fixture")
        doc.save(str(src))
        task = tm.prepare(src, mode="a")
        scan = tm.scan(task)
        assert scan.review_flags == ["mixed_layout_uncheckable"]
        assert task.status == TaskStatus.NEEDS_REVIEW
        loaded = repo.load_task(task.task_id)
        assert loaded is not None and loaded.status == TaskStatus.NEEDS_REVIEW
        matching = [e for e in repo.list_events(task.task_id)
                    if e["from_status"] == "PROCESSING"
                    and e["to_status"] == "NEEDS_REVIEW"]
        assert matching
        blob = json.dumps(matching, ensure_ascii=False)
        assert "SECRET BODY" not in blob
        assert "mixed_layout_uncheckable" not in blob
    finally:
        repo.close()


def test_batch_review_flags_persists_and_skips_process(tmp_path, monkeypatch):
    db = tmp_path / "batch-review.db"
    repo = SqliteTaskRepository(db)
    monkeypatch.setattr(pipeline_mod, "handler_for", lambda *a, **k: _ReviewFlagHandler())
    tm = TaskManager(Detector(), repository=repo, settings=Settings())
    called = []
    monkeypatch.setattr(tm, "process", lambda *a, **k: called.append("process"))
    monkeypatch.setattr(tm, "finalize", lambda *a, **k: called.append("finalize"))
    src = tmp_path / "batch-review.docx"
    doc = Document()
    doc.add_paragraph("batch review fixture")
    doc.save(str(src))
    task = None
    try:
        result = BatchService(tm).run([src], mode="a")
        assert len(result.review) == 1
        task = result.review[0]
        assert task.status == TaskStatus.NEEDS_REVIEW
        loaded = repo.load_task(task.task_id)
        assert loaded is not None and loaded.status == TaskStatus.NEEDS_REVIEW
        assert called == []
    finally:
        repo.close()
    restarted = SqliteTaskRepository(db)
    try:
        loaded = restarted.load_task(task.task_id)
        assert loaded is not None and loaded.status == TaskStatus.NEEDS_REVIEW
    finally:
        restarted.close()


def test_mark_task_unsafe_persists_event_and_operation(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "unsafe.db")
    tm = TaskManager(Detector(), repository=repo, settings=Settings())
    task = _task(tmp_path, "unsafe-1")
    repo.save_task(task)
    try:
        tm.mark_task_unsafe(task, reason="gui failure", error_code="gui_error")
        loaded = repo.load_task(task.task_id)
        assert loaded is not None and loaded.status == TaskStatus.UNSAFE
        assert any(e["event_code"] == "task.unsafe"
                   and e["to_status"] == "UNSAFE"
                   for e in repo.list_events(task.task_id))
        ops = repo._conn.execute(
            "SELECT action FROM operation_log WHERE task_id=?",
            (task.task_id,)).fetchall()
        assert any(r[0] == "mark_unsafe" for r in ops)
        assert repo.verify_event_chain().valid
    finally:
        repo.close()


def test_mark_unsafe_rejects_passed_task(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "unsafe-passed.db")
    tm = TaskManager(Detector(), repository=repo, settings=Settings())
    task = _task(tmp_path, "unsafe-passed", status=TaskStatus.PASSED,
                 with_output=True)
    repo.save_task(task)
    try:
        with pytest.raises(InvalidTaskTransition):
            tm.mark_task_unsafe(task, reason="should not revert")
        loaded = repo.load_task(task.task_id)
        assert loaded is not None and loaded.status == TaskStatus.PASSED
        assert not any(e["event_code"] == "task.unsafe"
                       for e in repo.list_events(task.task_id))
    finally:
        repo.close()


def test_mark_unsafe_event_failure_rolls_back(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "unsafe-event-fail.db")
    tm = TaskManager(Detector(), repository=repo, settings=Settings())
    task = _task(tmp_path, "unsafe-event-fail")
    repo.save_task(task)
    repo._conn.execute(
        "CREATE TRIGGER fail_unsafe_event BEFORE INSERT ON task_events "
        "BEGIN SELECT RAISE(ABORT, 'inject unsafe event failure'); END")
    try:
        with pytest.raises(sqlite3.DatabaseError):
            tm.mark_task_unsafe(task, reason="event failure")
        loaded = repo.load_task(task.task_id)
        assert loaded is not None and loaded.status == TaskStatus.PENDING
    finally:
        repo.close()


def test_gui_error_path_calls_unified_mark_unsafe(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALDOCMASK_HOME", str(tmp_path))
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    win = gui_mod.MainWindow()
    task = _task(tmp_path, "gui-task")
    win.tasks = [task]
    win._refresh_file_list()
    calls = []

    class _Pipe:
        def mark_task_unsafe(self, task, *, reason, error_code):
            calls.append((task.task_id, reason, error_code))
            task.status = TaskStatus.UNSAFE

    win.services = SimpleNamespace(pipeline=_Pipe(), close=lambda: None)
    try:
        win._on_error("boom")
        assert calls == [("gui-task", "boom", "gui_error")]
        assert task.status == TaskStatus.UNSAFE
    finally:
        win.services = None
        win.shutdown()
        win.close()
        win.deleteLater()
        QThreadPool.globalInstance().waitForDone(60000)
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        QApplication.processEvents()


def test_gui_source_does_not_call_bare_transition():
    text = (Path(__file__).resolve().parents[1] / "src" / "localdocmask"
            / "presentation" / "gui" / "main_window.py").read_text(encoding="utf-8")
    assert "transition_task(" not in text
