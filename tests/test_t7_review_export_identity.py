# -*- coding: utf-8 -*-
"""T7：复核持久化、单文件取消、统一导出和本地身份边界。"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import sqlite3
import subprocess
import time
import zipfile
from pathlib import Path

import pytest
from docx import Document
from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtWidgets import QApplication, QMessageBox

import localdocmask.application.pipeline as pipeline_mod
import localdocmask.presentation.gui.main_window as gui_mod
from localdocmask.application.batch import CancelToken
from localdocmask.application.pipeline import (
    ExportRecoveryError, TaskExecutionContext, TaskManager,
)
from localdocmask.domain.entities import (
    AuditFinding, AuditReport, Candidate, ScanResult, Task,
)
from localdocmask.domain.enums import (
    AuditVerdict, EntityDecision, ItemStatus, RiskLevel, TaskStatus,
)
from localdocmask.domain.exceptions import SecurityViolation
from localdocmask.domain.state_machine import current_actor
from localdocmask.infrastructure.config.settings import Settings, config_state_for
from localdocmask.infrastructure.logging.health import HealthService
from localdocmask.infrastructure.logging.metrics import LocalMetrics
import localdocmask.infrastructure.persistence.sqlite_repo as sqlite_repo_mod
from localdocmask.infrastructure.persistence.recovery import RecoveryService
from localdocmask.infrastructure.persistence.sqlite_repo import (
    SCHEMA_VERSION, SqliteTaskRepository, initialize_database, migrate,
)
from localdocmask.recognition.rule_based import Detector
from localdocmask.security.atomic_io import atomic_copy_user_file
from localdocmask.security.diagnostics import DiagnosticService
from localdocmask.security.hashing import sha256_file
from localdocmask.security.temp_lifecycle import TempLifecycle


def _docx(path: Path, text: str = "普通内容") -> Path:
    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))
    return path


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    processing = tmp_path / "processing"
    success = tmp_path / "success"
    quarantine = tmp_path / "quarantine"
    state = tmp_path / "state"
    metrics_dir = state / "metrics"
    diagnostics = tmp_path / "diagnostics"
    logs = tmp_path / "logs"
    for path in (processing, success, quarantine, state, metrics_dir,
                 diagnostics, logs):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pipeline_mod, "PROCESSING_DIR", processing)
    monkeypatch.setattr(pipeline_mod, "SUCCESS_DIR", success)
    monkeypatch.setattr(pipeline_mod, "QUARANTINE_DIR", quarantine)
    settings = Settings()
    settings.logging.log_dir = str(logs)
    repo = SqliteTaskRepository(tmp_path / "tasks.db")
    metrics = LocalMetrics(metrics_dir)
    health = HealthService(repo, metrics, None)
    temp = TempLifecycle(processing, state / "temp_manifest.jsonl")
    tm = TaskManager(
        Detector(), repository=repo, settings=settings,
        metrics=metrics, health=health, temp_lifecycle=temp)
    yield {
        "root": tmp_path, "processing": processing, "success": success,
        "quarantine": quarantine, "state": state, "diagnostics": diagnostics,
        "logs": logs, "settings": settings, "repo": repo,
        "metrics": metrics, "health": health, "temp": temp, "tm": tm,
    }
    repo.close()


def _review_task(tmp_path: Path, task_id: str = "review001") -> Task:
    source = _docx(tmp_path / f"{task_id}.docx", "虚构联系人")
    task = Task(
        source_path=source,
        task_id=task_id,
        file_type="docx",
        status=TaskStatus.NEEDS_REVIEW,
        source_hash=sha256_file(source),
        policy_version="p1",
        config_hash="c1",
        tool_version="t1",
        scan=ScanResult(candidates=[Candidate(
            text="虚构张三", category="姓名", location="第 1 段",
            risk_level=RiskLevel.HIGH,
            decision=EntityDecision.NEEDS_REVIEW,
            suggested_action="needs_review",
            source="regex", confirmed=False, excluded=False,
            status=ItemStatus.DETECTED.value,
        )]),
    )
    TaskManager._assign_candidate_identities(task.scan)
    return task


def _run_to_passed(env, name: str = "export.docx") -> Task:
    source = _docx(env["root"] / name)
    task = env["tm"].prepare(source, mode="a")
    env["tm"].scan(task)
    env["tm"].process(task, "a", "****", task.scan.candidates)
    env["tm"].finalize(task, task.audit)
    assert task.status == TaskStatus.PASSED
    return task


def test_review_state_roundtrip_and_original_not_persisted(runtime):
    env = runtime
    task = _review_task(env["root"])
    env["repo"].save_task(task)
    candidate_id = task.scan.candidates[0].candidate_id
    updated = env["tm"].update_candidate_review(
        task, candidate_id, confirmed=True, excluded=False,
        replacement="虚构客户甲", reason_code="manual_confirm")
    assert updated.final_action == "redact"
    loaded = env["repo"].load_task(task.task_id)
    assert loaded is not None
    candidate = loaded.scan.candidates[0]
    assert candidate.candidate_id == candidate_id
    assert candidate.decision == EntityDecision.CONFIRMED
    assert candidate.confirmed is True and candidate.excluded is False
    assert candidate.replacement == "虚构客户甲"
    assert candidate.review_actor == current_actor()
    assert candidate.reviewed_at
    assert candidate.review_reason_code == "manual_confirm"
    assert candidate.suggested_action == "needs_review"
    events = env["repo"].list_events(task.task_id)
    assert any(e["event_code"] == "task.review.candidate_updated" for e in events)
    ops = env["repo"]._conn.execute(
        "SELECT action FROM operation_log WHERE task_id=?", (task.task_id,)).fetchall()
    assert any(row[0] == "review_update" for row in ops)

    conn = sqlite3.connect(str(env["root"] / "tasks.db"))
    try:
        blob = "\n".join(
            str(value)
            for table in ("tasks", "task_items", "audit_findings",
                          "operation_log", "task_events")
            for row in conn.execute(f"SELECT * FROM {table}")
            for value in row)
    finally:
        conn.close()
    assert "虚构张三" not in blob

    diagnostics = DiagnosticService(
        settings=env["settings"], config_state=config_state_for(env["settings"]),
        health=env["health"], repository=env["repo"], metrics=env["metrics"],
        log_dir=env["logs"], diagnostics_dir=env["diagnostics"])
    bundle = diagnostics.create()
    with zipfile.ZipFile(bundle) as zf:
        diagnostic_text = "\n".join(
            zf.read(name).decode("utf-8", errors="replace")
            for name in zf.namelist())
    assert "虚构张三" not in diagnostic_text


def test_rescan_maps_review_state_by_stable_candidate_id(runtime, monkeypatch):
    env = runtime
    task = _review_task(env["root"], "rescan001")
    env["repo"].save_task(task)
    candidate_id = task.scan.candidates[0].candidate_id
    env["tm"].update_candidate_review(
        task, candidate_id, confirmed=True, replacement="虚构客户甲",
        reason_code="rescan_confirm")
    loaded = env["repo"].load_task(task.task_id)

    class Handler:
        name = "docx"

        def extract(self, path):
            return ScanResult(candidates=[Candidate(
                text="虚构张三", category="姓名", location="第 1 段",
                risk_level=RiskLevel.HIGH,
                decision=EntityDecision.NEEDS_REVIEW,
                suggested_action="needs_review", source="regex",
                confirmed=False, excluded=False,
            )])

    monkeypatch.setattr(pipeline_mod, "handler_for", lambda *a, **k: Handler())
    rescanned = env["tm"].scan(loaded)
    candidate = rescanned.candidates[0]
    assert candidate.candidate_id == candidate_id
    assert candidate.decision == EntityDecision.CONFIRMED
    assert candidate.confirmed is True
    assert candidate.replacement == "虚构客户甲"
    assert candidate.review_actor == current_actor()


def test_rescan_mapping_uncertainty_blocks_export(runtime, monkeypatch):
    env = runtime
    task = _review_task(env["root"], "mapping001")
    env["repo"].save_task(task)
    candidate_id = task.scan.candidates[0].candidate_id
    env["tm"].update_candidate_review(
        task, candidate_id, confirmed=True, replacement="虚构客户甲",
        reason_code="before_change")
    loaded = env["repo"].load_task(task.task_id)

    class ChangedHandler:
        name = "docx"

        def extract(self, path):
            return ScanResult(candidates=[Candidate(
                text="虚构王三", category="姓名", location="第 1 段",
                risk_level=RiskLevel.HIGH,
                decision=EntityDecision.NEEDS_REVIEW,
                confirmed=False, excluded=False,
            )])

    monkeypatch.setattr(pipeline_mod, "handler_for", lambda *a, **k: ChangedHandler())
    result = env["tm"].scan(loaded)
    assert "review_mapping_uncertain" in result.review_flags
    assert loaded.status == TaskStatus.NEEDS_REVIEW
    assert any("未解决" in reason or "review_flags" in reason
               for reason in env["tm"].export_gate_reasons(loaded))


def _manual_passed_task(env, task_id: str, *, pending: bool = False) -> Task:
    source = _docx(env["root"] / f"{task_id}-source.docx", "普通原件")
    output = env["success"] / f"{task_id}-out.docx"
    _docx(output, "已脱敏输出")
    candidate = Candidate(
        text="虚***三", category="姓名", location="第 1 段",
        candidate_id=f"{task_id}-cand", masked_value="虚***三",
        risk_level=RiskLevel.HIGH,
        decision=(EntityDecision.NEEDS_REVIEW if pending else EntityDecision.CONFIRMED),
        suggested_action="needs_review", confirmed=not pending, excluded=False,
        status=ItemStatus.DETECTED.value,
    )
    return Task(
        source_path=source, task_id=task_id, file_type="docx",
        status=TaskStatus.PASSED, source_hash=sha256_file(source),
        output_path=output, final_hash=sha256_file(output),
        policy_version="p1", config_hash="c1", tool_version="t1",
        scan=ScanResult(candidates=[candidate]),
        audit=AuditReport(True, [AuditFinding(
            AuditVerdict.NO_RESIDUAL, "未发现残留", "text")]),
        recognized_count=1, redacted_count=0, mode="a",
    )


def test_unified_export_is_atomic_and_writes_event(runtime):
    env = runtime
    task = _run_to_passed(env, "atomic-export.docx")
    export_dir = env["root"] / "user-export"
    export_dir.mkdir()
    destination = export_dir / "result.docx"
    result = env["tm"].export_to_location(task, destination)
    assert result == destination
    assert sha256_file(destination) == task.final_hash
    assert destination.read_bytes() == task.output_path.read_bytes()
    events = env["repo"].list_events(task.task_id)
    export_event = next(e for e in events
                        if e["event_code"] == "task.export.user_location")
    assert export_event["actor"] == current_actor()
    ops = env["repo"]._conn.execute(
        "SELECT action FROM operation_log WHERE task_id=?", (task.task_id,)).fetchall()
    assert any(row[0] == "user_export" for row in ops)


def test_export_refuses_existing_target_by_default(runtime):
    env = runtime
    task = _run_to_passed(env, "overwrite.docx")
    export_dir = env["root"] / "overwrite-export"
    export_dir.mkdir()
    destination = export_dir / "result.docx"
    destination.write_bytes(b"user-existing")
    with pytest.raises(SecurityViolation, match="拒绝覆盖"):
        env["tm"].export_to_location(task, destination)
    assert destination.read_bytes() == b"user-existing"
    assert not list(export_dir.glob(".*.tmp"))


def test_atomic_export_hash_failure_leaves_no_partial_file(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    destination = tmp_path / "target.bin"
    with pytest.raises(Exception):
        atomic_copy_user_file(source, destination, expected_sha256="0" * 64)
    assert not destination.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_export_gate_blocks_pending_review_audit_and_hash_anomaly(runtime):
    env = runtime
    pending = _manual_passed_task(env, "gate-pending", pending=True)
    env["repo"].save_task(pending)
    reasons = env["tm"].export_gate_reasons(pending)
    assert "unreviewed_candidates" in reasons

    task = _run_to_passed(env, "gate-valid.docx")
    task.output_path.write_bytes(b"tampered")
    reasons = env["tm"].export_gate_reasons(task)
    assert "output_hash_mismatch" in reasons

    audit_missing = _manual_passed_task(env, "gate-audit")
    audit_missing.audit = None
    env["repo"].save_task(audit_missing)
    reasons = env["tm"].export_gate_reasons(audit_missing)
    assert "audit_not_passed" in reasons


def test_export_before_cancel_has_no_output(runtime):
    env = runtime
    task = _run_to_passed(env, "cancel-export.docx")
    token = CancelToken()
    token.cancel("before_export")
    context = TaskExecutionContext.from_limits(env["tm"].limits, token)
    destination = env["root"] / "cancelled-export.docx"
    with pytest.raises(Exception):
        env["tm"].export_to_location(
            task, destination, execution_context=context)
    assert not destination.exists()


@pytest.fixture()
def win(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALDOCMASK_HOME", str(tmp_path / "gui-home"))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    window = gui_mod.MainWindow()
    yield window
    window.shutdown()
    window.close()
    window.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()


def _drain():
    QThreadPool.globalInstance().waitForDone(60000)
    QApplication.sendPostedEvents(None, 0)
    QApplication.processEvents()


def _mixed_docx(path: Path) -> Path:
    doc = Document()
    for text in (
        "本公司产品已上线，我们公司继续推进，本集团保持稳定。",
        "公开企业：中国石油股份有限公司。",
        "手机13812345678 邮箱a@b.com",
        "内部供应商：杭州云启科技有限公司。",
    ):
        doc.add_paragraph(text)
    doc.save(str(path))
    return path


def test_gui_candidate_display_is_masked_and_actor_visible(win):
    source = _mixed_docx(Path(os.environ["LOCALDOCMASK_HOME"]) / "masked.docx")
    win._add_files([str(source)])
    win._scan()
    _drain()
    task = win._active_task()
    assert task is not None and task.scan is not None
    phone = next(c for c in task.scan.candidates if c.text == "13812345678")
    row = next(r for r in range(win.cand_table.rowCount())
               if win._row_candidate(r) is phone)
    assert phone.text not in win.cand_table.item(row, 4).text()
    assert "*" in win.cand_table.item(row, 4).text()
    assert current_actor() in win.actor_label.text()


def test_single_scan_cancel_keeps_task_and_sets_cancelled(win, monkeypatch):
    source = _docx(Path(os.environ["LOCALDOCMASK_HOME"]) / "scan-cancel.docx")
    win._add_files([str(source)])

    class CancelOnSecond(gui_mod.CancelToken):
        def __init__(self):
            super().__init__()
            self.checks = 0

        @property
        def cancelled(self):
            self.checks += 1
            return self.checks >= 2

    monkeypatch.setattr(gui_mod, "CancelToken", CancelOnSecond)
    win._scan()
    _drain()
    assert len(win.tasks) == 1
    assert win.tasks[0].status == TaskStatus.CANCELLED
    assert not list(Path(os.environ["LOCALDOCMASK_HOME"]).glob("output/success/*"))


def test_single_process_cancel_has_no_success_output(win, monkeypatch):
    source = _docx(Path(os.environ["LOCALDOCMASK_HOME"]) / "process-cancel.docx",
                   "手机13800000001")
    win._add_files([str(source)])
    win._scan()
    _drain()

    class AlreadyCancelled(gui_mod.CancelToken):
        @property
        def cancelled(self):
            return True

    monkeypatch.setattr(gui_mod, "CancelToken", AlreadyCancelled)
    win._process_active()
    _drain()
    task = win._active_task()
    assert task.status == TaskStatus.CANCELLED
    assert not list(Path(os.environ["LOCALDOCMASK_HOME"]).glob("output/success/*"))


def test_batch_cancel_preserves_unprocessed_gui_tasks(win, monkeypatch):
    home = Path(os.environ["LOCALDOCMASK_HOME"])
    first = _docx(home / "batch-c1.docx", "手机13800000001")
    second = _docx(home / "batch-c2.docx", "手机13800000002")
    win._add_files([str(first), str(second)])

    class PreCanceled(gui_mod.CancelToken):
        def __init__(self):
            super().__init__()
            self.cancel()

    monkeypatch.setattr(gui_mod, "CancelToken", PreCanceled)
    win._batch()
    _drain()
    assert len(win.tasks) == 2
    assert all(t.status == TaskStatus.PENDING for t in win.tasks)
    assert all("批次已取消" in t.error for t in win.tasks)


def test_bulk_confirm_does_not_auto_confirm_needs_review(win):
    source = _mixed_docx(Path(os.environ["LOCALDOCMASK_HOME"]) / "bulk.docx")
    win._add_files([str(source)])
    win._scan()
    _drain()
    task = win._active_task()
    unresolved = [c for c in task.scan.candidates
                  if c.decision == EntityDecision.NEEDS_REVIEW]
    assert unresolved
    win._cand_all()
    _drain()
    refreshed = win._active_task()
    unresolved_ids = {c.candidate_id for c in refreshed.scan.candidates
                      if c.candidate_id in {x.candidate_id for x in unresolved}}
    assert unresolved_ids
    for candidate in refreshed.scan.candidates:
        if candidate.candidate_id in unresolved_ids:
            assert candidate.confirmed is False
            assert candidate.decision == EntityDecision.NEEDS_REVIEW


def test_actor_is_current_windows_user_for_review_and_export(runtime):
    env = runtime
    task = _review_task(env["root"], "actor001")
    env["repo"].save_task(task)
    candidate_id = task.scan.candidates[0].candidate_id
    env["tm"].update_candidate_review(
        task, candidate_id, confirmed=True, reason_code="actor_test")
    events = env["repo"].list_events(task.task_id)
    review_event = next(e for e in events
                        if e["event_code"] == "task.review.candidate_updated")
    assert review_event["actor"] == current_actor()
    assert current_actor() == (getpass.getuser() or "unknown-local-user")


def test_migration_v3_to_v4_review_state_fails_closed(tmp_path):
    db = tmp_path / "legacy.db"
    initialize_database(db, schema_version=3)
    conn = sqlite3.connect(str(db))
    try:
        task_id = "legacy-task"
        conn.execute(
            "INSERT INTO tasks (task_id, source_path, file_type, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, str(tmp_path / "legacy.docx"), "docx", "NEEDS_REVIEW",
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
        for idx, decision in enumerate(("auto_confirm", "suppress", "", "future_value"), 1):
            conn.execute(
                "INSERT INTO task_items (task_id, category, value_masked, "
                "risk, decision, status, failure_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, "姓名", f"虚***{idx}", "high", decision,
                 "detected", ""))
        conn.commit()
    finally:
        conn.close()

    moved_version = migrate(db, 3, SCHEMA_VERSION)
    assert moved_version == SCHEMA_VERSION
    backups = list(tmp_path.glob("legacy.db.bak-*"))
    assert backups and backups[0].exists()
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        task_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        item_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_items)")}
        rows = conn.execute(
            "SELECT decision, confirmed, excluded, final_action "
            "FROM task_items ORDER BY id").fetchall()
    finally:
        conn.close()
    assert version == SCHEMA_VERSION == 4
    assert "review_flags_json" in task_cols
    assert {"candidate_id", "original_fingerprint", "suggested_action",
            "final_action", "confirmed", "excluded", "replacement_value",
            "review_actor", "reviewed_at", "review_reason_code"} <= item_cols
    assert rows[0]["decision"] == "auto_confirm"
    assert rows[0]["confirmed"] == 1 and rows[0]["excluded"] == 0
    assert rows[0]["final_action"] == "redact"
    assert rows[1]["decision"] == "suppress"
    assert rows[1]["confirmed"] == 0 and rows[1]["excluded"] == 1
    assert rows[1]["final_action"] == "exclude"
    for row in rows[2:]:
        assert row["decision"] == "needs_review"
        assert row["confirmed"] == 0 and row["excluded"] == 0
        assert row["final_action"] == "pending"

    repo = SqliteTaskRepository(db)
    try:
        loaded = repo.load_task("legacy-task")
        assert loaded is not None and len(loaded.scan.candidates) == 4
        gate = TaskManager(
            repository=repo, settings=Settings()).export_gate_reasons(loaded)
        assert "task_not_passed" in gate
    finally:
        repo.close()


def test_migration_v3_to_v4_failure_rolls_back(tmp_path, monkeypatch):
    db = tmp_path / "legacy-fail.db"
    initialize_database(db, schema_version=3)

    def broken_v4(conn):
        raise RuntimeError("injected v4 failure")

    monkeypatch.setitem(sqlite_repo_mod._MIGRATIONS, 4, broken_v4)
    with pytest.raises(RuntimeError):
        migrate(db, 3, 4)
    conn = sqlite3.connect(str(db))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        item_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_items)")}
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    assert version == 3
    assert "candidate_id" not in item_cols
    assert integrity == "ok"
    assert list(tmp_path.glob("legacy-fail.db.bak-*"))


def test_acl_script_dry_run_and_unc_rejection(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "harden_data_acl.ps1"
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    assert pwsh and script.exists()
    result = subprocess.run(
        [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
         "-DataDir", str(tmp_path / "acl-data"), "-DryRun"],
        capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert "DRY_RUN_OK" in result.stdout
    assert "icacls.exe" in result.stdout
    assert "ACTOR" in result.stdout

    rejected = subprocess.run(
        [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
         "-DataDir", r"\\server\share\data", "-DryRun"],
        capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert rejected.returncode != 0


def test_export_event_failure_removes_published_user_file(runtime, monkeypatch):
    env = runtime
    task = _run_to_passed(env, "export-event-fail.docx")
    destination = env["root"] / "user-export-event-fail.docx"
    original_save = env["repo"].save_task

    def fail_export_event(task, *, operation=None, events=None, release_claim=False):
        if any(e.event_code == "task.export.user_location" for e in (events or [])):
            raise RuntimeError("synthetic export event failure")
        return original_save(task, operation=operation, events=events,
                             release_claim=release_claim)

    monkeypatch.setattr(env["repo"], "save_task", fail_export_event)
    with pytest.raises(RuntimeError):
        env["tm"].export_to_location(task, destination)
    assert not destination.exists()
    assert not list(env["root"].glob(".export-event-fail.docx.*.tmp"))
    assert not list(env["root"].glob(".export-event-fail.docx.*.bak"))


def test_export_cannot_replace_source_file(runtime):
    env = runtime
    task = _run_to_passed(env, "source-protect.docx")
    before = sha256_file(task.source_path)
    with pytest.raises(SecurityViolation, match="原始文件"):
        env["tm"].export_to_location(task, task.source_path, overwrite=True)
    assert sha256_file(task.source_path) == before


def test_review_rejects_replacement_containing_original(runtime):
    env = runtime
    task = _review_task(env["root"], "replacement-secret")
    env["repo"].save_task(task)
    candidate_id = task.scan.candidates[0].candidate_id
    with pytest.raises(SecurityViolation, match="原始敏感值"):
        env["tm"].update_candidate_review(
            task, candidate_id, confirmed=True,
            replacement="前缀-虚构张三-后缀", reason_code="bad_replacement")
    loaded = env["repo"].load_task(task.task_id)
    assert loaded.scan.candidates[0].replacement == ""


def test_gui_export_delegates_to_unified_gate_not_copy2():
    source = (Path(__file__).resolve().parents[1]
              / "src" / "localdocmask" / "presentation" / "gui"
              / "main_window.py").read_text(encoding="utf-8")
    assert "export_to_location" in source
    assert "shutil.copy2" not in source


def test_explicit_overwrite_is_allowed_and_evented(runtime):
    env = runtime
    task = _run_to_passed(env, "explicit-overwrite.docx")
    destination = env["root"] / "explicit-overwrite-result.docx"
    destination.write_bytes(b"old-user-data")
    result = env["tm"].export_to_location(
        task, destination, overwrite=True)
    assert result.read_bytes() == task.output_path.read_bytes()
    event = next(e for e in env["repo"].list_events(task.task_id)
                 if e["event_code"] == "task.export.user_location"
                 and "overwrite=1" in e["detail"])
    assert event["output_hash"] == task.final_hash


def test_overwrite_export_event_failure_restores_previous_file(runtime, monkeypatch):
    env = runtime
    task = _run_to_passed(env, "overwrite-restore.docx")
    destination = env["root"] / "overwrite-restore-result.docx"
    destination.write_bytes(b"previous-user-file")
    original_save = env["repo"].save_task

    def fail_event(task, *, operation=None, events=None, release_claim=False):
        if any(e.event_code == "task.export.user_location" for e in (events or [])):
            raise RuntimeError("injected event failure")
        return original_save(task, operation=operation, events=events,
                             release_claim=release_claim)

    monkeypatch.setattr(env["repo"], "save_task", fail_event)
    with pytest.raises(RuntimeError):
        env["tm"].export_to_location(task, destination, overwrite=True)
    assert destination.read_bytes() == b"previous-user-file"
    assert not list(env["root"].glob(".overwrite-restore-result.docx.*.bak"))


def test_export_rejects_passed_output_outside_success(runtime):
    env = runtime
    task = _manual_passed_task(env, "outside-success")
    outside = env["root"] / "outside.docx"
    task.output_path.rename(outside)
    task.output_path = outside
    task.final_hash = sha256_file(outside)
    env["repo"].save_task(task)
    reasons = env["tm"].export_gate_reasons(task)
    assert "output_outside_success_dir" in reasons
    destination = env["root"] / "must-not-export.docx"
    with pytest.raises(SecurityViolation):
        env["tm"].export_to_location(task, destination)
    assert not destination.exists()


def test_export_rejects_directory_missing_and_symlink_output(runtime, monkeypatch):
    env = runtime
    directory_task = _manual_passed_task(env, "dir-output")
    directory = env["success"] / "fake-output-dir"
    directory.mkdir()
    directory_task.output_path = directory
    directory_task.final_hash = "0" * 64
    env["repo"].save_task(directory_task)
    assert "output_path_unsafe" in env["tm"].export_gate_reasons(directory_task)

    missing_task = _manual_passed_task(env, "missing-output")
    missing_task.output_path.unlink()
    env["repo"].save_task(missing_task)
    assert "output_missing" in env["tm"].export_gate_reasons(missing_task)

    symlink_task = _manual_passed_task(env, "symlink-output")
    env["repo"].save_task(symlink_task)
    real_is_symlink = Path.is_symlink

    def fake_is_symlink(self):
        return self == symlink_task.output_path or real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    assert "output_path_unsafe" in env["tm"].export_gate_reasons(symlink_task)


@pytest.mark.parametrize("root_key", ["processing", "quarantine"])
def test_export_rejects_processing_and_quarantine_output(runtime, root_key):
    env = runtime
    task = _manual_passed_task(env, f"{root_key}-output")
    target = env[root_key] / f"{root_key}-artifact.docx"
    task.output_path.rename(target)
    task.output_path = target
    task.final_hash = sha256_file(target)
    env["repo"].save_task(task)
    reasons = env["tm"].export_gate_reasons(task)
    assert "output_outside_success_dir" in reasons
    assert "output_path_unsafe" in reasons


def test_export_gate_rejects_success_artifact_equal_to_source(runtime):
    env = runtime
    task = _manual_passed_task(env, "source-as-output")
    task.output_path = task.source_path
    task.final_hash = sha256_file(task.source_path)
    env["repo"].save_task(task)
    reasons = env["tm"].export_gate_reasons(task)
    assert "output_path_unsafe" in reasons
    assert "output_outside_success_dir" in reasons



def test_overwrite_recovery_failure_preserves_backup(runtime, monkeypatch):
    env = runtime
    task = _run_to_passed(env, "recovery-failure.docx")
    destination = env["root"] / "recovery-failure-result.docx"
    destination.write_bytes(b"previous-important-content")
    original_save = env["repo"].save_task
    real_copy_user_file = pipeline_mod.atomic_copy_user_file

    def fail_export_event(task, *, operation=None, events=None, release_claim=False):
        if any(e.event_code == "task.export.user_location" for e in (events or [])):
            raise RuntimeError("injected export event failure")
        return original_save(task, operation=operation, events=events,
                             release_claim=release_claim)

    def fail_backup_restore(src, dst, *args, **kwargs):
        if str(src).endswith(".bak"):
            raise OSError("injected restore failure")
        return real_copy_user_file(src, dst, *args, **kwargs)

    monkeypatch.setattr(env["repo"], "save_task", fail_export_event)
    monkeypatch.setattr(pipeline_mod, "atomic_copy_user_file", fail_backup_restore)
    source_hash = sha256_file(task.source_path)
    success_hash = sha256_file(task.output_path)
    with pytest.raises(ExportRecoveryError, match="备份已保留"):
        env["tm"].export_to_location(task, destination, overwrite=True)
    backups = list(env["root"].glob(".recovery-failure-result.docx.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"previous-important-content"
    assert sha256_file(task.source_path) == source_hash
    assert sha256_file(task.output_path) == success_hash
    ops = env["repo"]._conn.execute(
        "SELECT detail FROM operation_log WHERE task_id=? AND action=?",
        (task.task_id, "export_recovery_failed")).fetchall()
    assert any(row[0] == "backup_preserved" for row in ops)
    events = env["repo"].list_events(task.task_id)
    assert any(e["event_code"] == "task.export.recovery_failed" for e in events)
    assert env["repo"].verify_event_chain().valid


def _sensitive_name_task(env, name="张三_云启公司_合同.docx") -> Task:
    source = _docx(env["root"] / name, "联系人张三 手机13812345678")
    task = env["tm"].prepare(source, mode="a")
    scan = env["tm"].scan(task)
    assert scan.candidates
    if not any(candidate.text == "张三" for candidate in scan.candidates):
        scan.candidates.append(Candidate(
            text="张三", category="姓名", location="第 1 段",
            risk_level=RiskLevel.HIGH, source="regex"))
    for candidate in scan.candidates:
        candidate.decision = EntityDecision.AUTO_CONFIRM
        candidate.confirmed = True
        candidate.excluded = False
        candidate.final_action = "redact"
    env["tm"].process(task, "a", "****", scan.candidates)
    report = env["tm"].audit(task, scan.candidates)
    return env["tm"].finalize(task, report)


def test_sensitive_source_name_not_exposed_in_success_or_audit(runtime):
    env = runtime
    task = _sensitive_name_task(env)
    assert task.status == TaskStatus.PASSED
    assert sha256_file(task.source_path) == task.source_hash
    assert task.output_path.name == f"masked_{task.task_id}.docx"
    assert task.report_path.name == f"audit_{task.task_id}.json"
    assert task.output_path.parent == env["success"]
    assert task.report_path.parent == env["success"]
    assert all(token not in str(task.output_path) for token in ("张三", "云启公司"))
    assert all(token not in str(task.report_path) for token in ("张三", "云启公司"))

    report_text = task.report_path.read_text(encoding="utf-8")
    for token in ("张三", "云启公司", "张三_云启公司_合同.docx", "source_file"):
        assert token not in report_text
    payload = json.loads(report_text)
    expected_ref = f"source_{task.source_hash[:12]}.docx"
    assert payload["source_ref"] == expected_ref
    assert all(record.get("source_ref") == expected_ref for record in payload["records"])
    for record in payload["records"]:
        assert "source_file" not in record
    body = "\n".join(p.text for p in Document(str(task.output_path)).paragraphs)
    assert "张三" not in body and "13812345678" not in body
    assert env["repo"].verify_event_chain().valid


def test_sensitive_source_name_not_exposed_in_quarantine(runtime):
    env = runtime
    source = _docx(env["root"] / "张三_云启公司_合同.docx", "普通内容")
    before = sha256_file(source)
    task = env["tm"].prepare(source, mode="a")
    scan = env["tm"].scan(task)
    env["tm"].process(task, "a", "****", scan.candidates)
    processed_output = task.output_path
    env["tm"].mark_task_unsafe(task, reason="synthetic_failure")
    quarantine_names = [p.name for p in env["quarantine"].iterdir()]
    assert quarantine_names
    assert all("张三" not in name and "云启公司" not in name
               for name in quarantine_names)
    assert "张三" not in str(processed_output) and "云启公司" not in str(processed_output)
    assert not list(env["success"].iterdir())
    assert sha256_file(source) == before


def _recovery_task(env, task_id: str) -> tuple[Task, Path]:
    source = _docx(env["root"] / f"recovery_{task_id}.docx", "普通内容")
    output = env["success"] / f"masked_{task_id}.docx"
    output.write_bytes(b"masked-output")
    task = Task(
        source_path=source, task_id=task_id, file_type="docx",
        status=TaskStatus.PASSED, source_hash=sha256_file(source),
        output_path=output, final_hash=sha256_file(output),
        mode="a", policy_version="p1", config_hash="c1", tool_version="t1",
        audit=AuditReport(passed=True, findings=[
            AuditFinding(AuditVerdict.NO_RESIDUAL, "ok", "text")]),
    )
    env["repo"].save_task(task)
    report = env["success"] / f"audit_{task_id}.json"
    task.report_path = report
    env["repo"].save_task(task)
    return task, report


def _write_audit_payload(path: Path, task: Task, *, task_id=None,
                         source_hash=None, final_hash=None):
    path.write_text(json.dumps({
        "task_id": task_id if task_id is not None else task.task_id,
        "source_sha256": source_hash if source_hash is not None else task.source_hash,
        "final_sha256": final_hash if final_hash is not None else task.final_hash,
    }), encoding="utf-8")


def test_recovery_accepts_new_and_legacy_audit_names_only_with_matching_content(runtime):
    env = runtime
    task, new_report = _recovery_task(env, "recovernew")
    _write_audit_payload(new_report, task)
    assert RecoveryService._confirmed_artifact(task, new_report) is True

    legacy = env["success"] / "recovery_recovernew_审计记录_recovernew.json"
    _write_audit_payload(legacy, task)
    assert RecoveryService._confirmed_artifact(task, legacy) is True

    mismatched = env["success"] / f"audit_{task.task_id}.json"
    _write_audit_payload(mismatched, task, task_id="other-task")
    assert RecoveryService._confirmed_artifact(task, mismatched) is False
    _write_audit_payload(mismatched, task, source_hash="0" * 64)
    assert RecoveryService._confirmed_artifact(task, mismatched) is False
    _write_audit_payload(mismatched, task, final_hash="0" * 64)
    assert RecoveryService._confirmed_artifact(task, mismatched) is False


def test_recovery_quarantines_unbound_new_audit_report(runtime):
    env = runtime
    task, report = _recovery_task(env, "recoverbad")
    _write_audit_payload(report, task, task_id="other-task")
    RecoveryService().recover(
        env["repo"], env["processing"],
        success_dir=env["success"], quarantine_dir=env["quarantine"])
    assert not report.exists()
    assert list(env["quarantine"].glob("audit_recoverbad*.json"))
    assert task.output_path.exists()


def test_audit_json_is_self_contained_and_binds_policy_config_tool(runtime):
    task = _run_to_passed(runtime, "audit-self-contained.docx")
    payload = json.loads(task.report_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["audit_passed"] is True
    assert payload["policy_version"] == task.policy_version
    assert payload["config_hash"] == task.config_hash
    assert payload["tool_version"] == task.tool_version
    assert payload["coverage"] == ["text", "ocr", "metadata", "hidden"]
    assert payload["findings"]
    assert all(set(item) >= {"finding_id", "verdict", "scope", "detail"}
               for item in payload["findings"])


def test_audit_json_findings_do_not_leak_sensitive_values(runtime):
    env = runtime
    task = _review_task(env["root"], "audit-sensitive")
    task.audit = AuditReport(
        passed=False,
        coverage=["text"],
        findings=[AuditFinding(
            AuditVerdict.RESIDUAL_FOUND,
            "发现 张三 13812345678 张三_云启公司_合同.docx",
            "text",
        )],
    )
    env["tm"]._write_report(task)
    text = task.report_path.read_text(encoding="utf-8")
    assert "张三" not in text
    assert "13812345678" not in text
    assert "张三_云启公司_合同.docx" not in text
    payload = json.loads(text)
    assert payload["audit_passed"] is False
    assert payload["findings"][0]["detail"] == "发现已知敏感信息残留"


def test_audit_json_preserves_recovery_identity_fields(runtime):
    task = _run_to_passed(runtime, "audit-recovery-fields.docx")
    payload = json.loads(task.report_path.read_text(encoding="utf-8"))
    for field in ("task_id", "source_ref", "file_type", "source_sha256",
                  "final_sha256", "records", "recognized_count",
                  "redacted_count", "deleted_count"):
        assert field in payload


def test_audit_json_reflects_failed_audit(runtime):
    env = runtime
    task = _review_task(env["root"], "audit-failed-json")
    task.audit = AuditReport(
        passed=False,
        coverage=["text", "hidden"],
        findings=[AuditFinding(
            AuditVerdict.UNCHECKABLE,
            "无法检查 张三 13812345678",
            "hidden",
        )],
    )
    env["tm"]._write_report(task)
    payload = json.loads(task.report_path.read_text(encoding="utf-8"))
    assert payload["audit_passed"] is False
    assert payload["coverage"] == ["text", "hidden"]
    assert payload["findings"][0]["verdict"] == AuditVerdict.UNCHECKABLE.value
    assert payload["findings"][0]["detail"] == "存在无法检查的内容"
    report_text = json.dumps(payload, ensure_ascii=False)
    assert "张三" not in report_text
    assert "13812345678" not in report_text
