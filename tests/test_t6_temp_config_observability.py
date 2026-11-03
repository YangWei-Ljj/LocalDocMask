# -*- coding: utf-8 -*-
"""T6：临时文件生命周期、配置治理、指标、健康检查和诊断包。"""
from __future__ import annotations

import json
import os
import time
import zipfile
from pathlib import Path

import pytest
from docx import Document

import localdocmask.application.pipeline as pipeline_mod
from localdocmask.application.batch import BatchService, CancelToken
from localdocmask.application.pipeline import TaskExecutionContext, TaskManager
from localdocmask.domain.entities import Task
from localdocmask.domain.enums import TaskStatus
from localdocmask.domain.exceptions import ConfigError, QueueFullError
from localdocmask.infrastructure.config.settings import (
    Settings, config_state_for, compute_config_hash,
)
from localdocmask.infrastructure.logging import logger as logmod
from localdocmask.infrastructure.logging.health import (
    DEGRADED, HEALTHY, UNHEALTHY, HealthService,
)
from localdocmask.infrastructure.logging.metrics import LocalMetrics
from localdocmask.infrastructure.persistence.sqlite_repo import SqliteTaskRepository
from localdocmask.recognition.rule_based import Detector
from localdocmask.security.diagnostics import DiagnosticService
from localdocmask.security.hashing import sha256_file
from localdocmask.security.temp_lifecycle import TempLifecycle, TempLifecycleError


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
    logs = tmp_path / "logs"
    state = tmp_path / "state"
    metrics_dir = state / "metrics"
    diagnostics = tmp_path / "diagnostics"
    for path in (processing, success, quarantine, logs, state,
                 metrics_dir, diagnostics):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pipeline_mod, "PROCESSING_DIR", processing)
    monkeypatch.setattr(pipeline_mod, "SUCCESS_DIR", success)
    monkeypatch.setattr(pipeline_mod, "QUARANTINE_DIR", quarantine)
    settings = Settings()
    settings.logging.log_dir = str(logs)
    repo = SqliteTaskRepository(tmp_path / "tasks.db")
    metrics = LocalMetrics(metrics_dir)
    config_state = config_state_for(settings)
    health = HealthService(repo, metrics, config_state)
    temp = TempLifecycle(processing, state / "temp_manifest.jsonl")
    tm = TaskManager(
        Detector(), repository=repo, settings=settings,
        metrics=metrics, health=health, temp_lifecycle=temp)
    yield {
        "root": tmp_path, "processing": processing, "success": success,
        "quarantine": quarantine, "logs": logs, "state": state,
        "metrics_dir": metrics_dir, "diagnostics": diagnostics,
        "settings": settings, "repo": repo, "metrics": metrics,
        "health": health, "config_state": config_state, "temp": temp,
        "tm": tm,
    }
    repo.close()


def _run_to_processed(env, name: str = "doc.docx", *, token=None):
    source = _docx(env["root"] / name)
    task = env["tm"].prepare(source, mode="a", cancel_token=token)
    env["tm"].scan(task)
    for candidate in task.scan.candidates:
        candidate.confirmed = True
    env["tm"].process(task, "a", "****", task.scan.candidates)
    return task


def test_success_cleans_processing_temp_and_records_metrics(runtime):
    env = runtime
    source = _docx(env["root"] / "success.docx")
    before = sha256_file(source)
    task = env["tm"].prepare(source, mode="a")
    env["tm"].scan(task)
    env["tm"].process(task, "a", "****", task.scan.candidates)
    assert list(env["processing"].iterdir())
    env["tm"].finalize(task, task.audit)
    assert task.status == TaskStatus.PASSED
    assert sha256_file(source) == before
    assert task.output_path.parent == env["success"]
    assert not [p for p in env["processing"].iterdir() if p.is_file()]
    assert env["temp"].active_records() == {}
    snapshot = env["metrics"].snapshot()
    assert snapshot["counters"].get("task_started") == 1
    assert snapshot["counters"].get("task_success") == 1
    assert set(snapshot["stages"]) >= {
        "admission", "scan", "process", "audit", "finalize"}


def test_cancel_moves_temp_to_quarantine_and_marks_manifest(runtime):
    env = runtime
    token = CancelToken()
    task = _run_to_processed(env, "cancel.docx", token=token)
    assert env["temp"].active_records()
    token.cancel("t6_cancel")
    with pytest.raises(Exception):
        env["tm"].finalize(task, task.audit)
    assert task.status == TaskStatus.CANCELLED
    assert not list(env["success"].iterdir())
    assert list(env["quarantine"].iterdir())
    assert env["temp"].active_records() == {}
    assert env["metrics"].snapshot()["counters"].get("task_cancelled") == 1


def test_timeout_moves_temp_to_quarantine(runtime):
    env = runtime
    token = CancelToken()
    context = TaskExecutionContext(
        token, 2.0,
        {"admission": 1.0, "scan": 1.0, "process": 0.03,
         "audit": 1.0, "finalize": 1.0})
    source = _docx(env["root"] / "timeout.docx")
    before = sha256_file(source)

    def slow_handler(in_path, out_path, candidates):
        import shutil
        shutil.copyfile(in_path, out_path)
        time.sleep(0.08)

    env["tm"]._run_handler = slow_handler
    task = env["tm"].prepare(source, mode="a", execution_context=context)
    env["tm"].scan(task, execution_context=context)
    with pytest.raises(Exception):
        env["tm"].process(task, "a", "****", task.scan.candidates,
                          execution_context=context)
    assert task.status == TaskStatus.TIMED_OUT
    assert sha256_file(source) == before
    assert not list(env["success"].iterdir())
    assert list(env["quarantine"].iterdir())
    assert env["temp"].active_records() == {}
    assert env["metrics"].snapshot()["counters"].get("task_timed_out") == 1


def test_generic_failure_quarantines_partial_temp(runtime, monkeypatch):
    env = runtime
    service = BatchService(env["tm"], env["settings"])
    source = _docx(env["root"] / "failure.docx")

    def failing_handler(in_path, out_path, candidates):
        Path(out_path).write_bytes(b"partial")
        raise PermissionError("synthetic denied")

    monkeypatch.setattr(env["tm"], "_run_handler", failing_handler)
    result = service.run([source], mode="a")
    assert len(result.failed) == 1
    assert result.failed[0].status == TaskStatus.AUDIT_FAILED
    assert not list(env["success"].iterdir())
    assert list(env["quarantine"].iterdir())
    assert env["temp"].active_records() == {}
    assert env["metrics"].snapshot()["counters"].get("task_failed", 0) >= 1


def test_ttl_cleanup_removes_terminal_orphan_and_writes_event(runtime):
    env = runtime
    task = Task(source_path=env["root"] / "old.docx", task_id="ttl001",
                status=TaskStatus.CANCELLED)
    env["repo"].save_task(task)
    orphan = env["processing"] / "ttl001_old_temp.docx"
    orphan.write_bytes(b"old")
    old_time = time.time() - 7200
    os.utime(orphan, (old_time, old_time))
    report = env["temp"].cleanup_expired(env["repo"], ttl_seconds=60)
    assert report.cleaned == 1
    assert not orphan.exists()
    events = env["repo"].list_events(task.task_id)
    assert any(e["event_code"] == "recovery.temp_ttl_cleaned" for e in events)
    assert env["repo"].verify_event_chain().valid


def test_ttl_cleanup_keeps_active_task_file(runtime):
    env = runtime
    task = Task(source_path=env["root"] / "active.docx", task_id="active001",
                status=TaskStatus.PENDING)
    env["repo"].save_task(task)
    active = env["processing"] / "active001_temp.docx"
    active.write_bytes(b"active")
    old_time = time.time() - 7200
    os.utime(active, (old_time, old_time))
    report = env["temp"].cleanup_expired(env["repo"], ttl_seconds=60)
    assert report.kept_active == 1
    assert active.exists()


def test_ttl_cleanup_never_touches_quarantine(runtime):
    env = runtime
    quarantine_file = env["quarantine"] / "keep-me.docx"
    quarantine_file.write_bytes(b"manual")
    old_time = time.time() - 7200
    os.utime(quarantine_file, (old_time, old_time))
    env["temp"].cleanup_expired(env["repo"], ttl_seconds=60)
    assert quarantine_file.exists()


def test_temp_path_escape_wrong_root_and_cleanup_failure_are_explicit(
        runtime, tmp_path, monkeypatch):
    env = runtime
    with pytest.raises(TempLifecycleError):
        TempLifecycle(Path("relative"), tmp_path / "manifest.jsonl")
    with pytest.raises(TempLifecycleError):
        TempLifecycle(r"\\server\share\processing", tmp_path / "manifest.jsonl")
    with pytest.raises(TempLifecycleError):
        env["temp"].register("task001", tmp_path / "outside.docx", "process")

    orphan = env["processing"] / "orphan.docx"
    orphan.write_bytes(b"old")
    old_time = time.time() - 7200
    os.utime(orphan, (old_time, old_time))
    real_unlink = Path.unlink

    def fail_unlink(self, *args, **kwargs):
        if self == orphan:
            raise PermissionError("synthetic denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(TempLifecycleError, match="清理失败"):
        env["temp"].cleanup_expired(env["repo"], ttl_seconds=60)


def test_invalid_config_without_last_good_is_rejected(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("LOCALDOCMASK_HOME", str(home))
    path = tmp_path / "invalid.yaml"
    path.write_text("schema_version: 1\nsecurity:\n  max_workers: 0\n",
                    encoding="utf-8")
    with pytest.raises(ConfigError):
        Settings.load_with_metadata(path)


def test_last_good_config_fallback_is_degraded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("LOCALDOCMASK_HOME", str(home))
    good = tmp_path / "good.yaml"
    good.write_text(
        "schema_version: 1\n"
        "recognition:\n  auto_confirm_threshold: 0.8\n",
        encoding="utf-8")
    loaded = Settings.load_with_metadata(good)
    assert loaded.state.degraded is False
    assert loaded.settings.recognition.auto_confirm_threshold == 0.8

    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\nsecurity:\n  max_workers: 0\n",
                   encoding="utf-8")
    fallback = Settings.load_with_metadata(bad)
    assert fallback.state.degraded is True
    assert fallback.state.source == "last_good"
    assert fallback.state.warning_code == "config_fallback"
    assert fallback.settings.recognition.auto_confirm_threshold == 0.8

    repo = SqliteTaskRepository(home / "tasks.db")
    try:
        health = HealthService(
            repo, LocalMetrics(home / "metrics"), fallback.state)
        assert health.check().status == DEGRADED
    finally:
        repo.close()


def test_config_hash_is_stable_and_covers_processing_and_resource_fields(tmp_path):
    base = Settings()
    h1 = compute_config_hash(base)
    time.sleep(0.01)
    assert compute_config_hash(Settings()) == h1

    non_processing = Settings()
    non_processing.paths.data_dir = str(tmp_path / "other")
    non_processing.logging.level = "DEBUG"
    non_processing.observability.metrics_max_bytes = 1
    assert compute_config_hash(non_processing) == h1

    changed = Settings()
    changed.recognition.auto_confirm_threshold = 0.8
    assert compute_config_hash(changed) != h1
    for field, value in (
        ("max_workers", 8), ("max_queue_depth", 64),
        ("tempfile_retention_days", 2.0), ("max_pdf_pages", 501),
        ("task_timeout_seconds", 1801.0),
    ):
        changed = Settings()
        setattr(changed.security, field, value)
        assert compute_config_hash(changed) != h1


def test_failure_logs_metrics_and_diagnostics_do_not_leak_secrets(runtime):
    env = runtime
    logmod._configured = False
    logmod._extra_sensitive.clear()
    import logging
    logging.getLogger("localdocmask").handlers.clear()
    logmod.configure_logging(env["settings"].logging)
    logger = logmod.get_logger("t6_secret_test")
    phone = "13812345678"
    token = "t6-super-secret-token"
    body = "SYNTHETIC_DOCUMENT_BODY_SHOULD_NOT_APPEAR"
    logmod.register_sensitive_values([body])
    logger.info("phone=%s api_key=%s path=%s body=%s",
                phone, token, env["root"] / "private.docx", body)
    for handler in logging.getLogger("localdocmask").handlers:
        handler.flush()

    task = Task(source_path=env["root"] / "private.docx",
                task_id="diag001", status=TaskStatus.AUDIT_FAILED)
    env["repo"].save_task(task)
    env["metrics"].record("task_failed", 1, stage="batch", code="test")
    service = DiagnosticService(
        settings=env["settings"], config_state=env["config_state"],
        health=env["health"], repository=env["repo"], metrics=env["metrics"],
        log_dir=env["logs"], diagnostics_dir=env["diagnostics"])
    bundle = service.create()
    with zipfile.ZipFile(bundle) as zf:
        names = zf.namelist()
        content = "\n".join(
            zf.read(name).decode("utf-8", errors="replace")
            for name in names)
    assert phone not in content
    assert token not in content
    assert "api_key=<redacted>" in content
    assert body not in content
    assert str(env["root"]) not in content
    assert "mapping_table" not in content.lower()
    assert set(names) >= {
        "manifest.json", "config_summary.json", "health.json",
        "metrics_summary.json", "task_summary.json", "event_chain.json",
        "logs_summary.jsonl",
    }
    logging.getLogger("localdocmask").handlers.clear()
    logmod._configured = False


def test_metrics_are_aggregate_only_and_exclude_sensitive_content(runtime):
    env = runtime
    env["metrics"].record("task_failed", 1, stage="process", code="parse_error")
    raw = (env["metrics_dir"] / "metrics.jsonl").read_text(encoding="utf-8")
    assert "13812345678" not in raw
    assert "private.docx" not in raw
    assert str(env["root"]) not in raw
    for line in raw.splitlines():
        item = json.loads(line)
        assert set(item) == {"time", "metric", "value", "stage", "code", "outcome"}


def test_health_distinguishes_healthy_degraded_unhealthy(runtime):
    env = runtime
    healthy = HealthService(env["repo"], env["metrics"], env["config_state"])
    assert healthy.check().status == HEALTHY
    healthy.mark_degraded("local_llm", "optional_service_unavailable")
    assert healthy.check().status == DEGRADED
    healthy.mark_unhealthy("database", "database_unavailable")
    assert healthy.check().status == UNHEALTHY


def test_cleanup_failure_prevents_passed_state(tmp_path, runtime, monkeypatch):
    env = runtime
    task = _run_to_processed(env, "cleanup-fail.docx")
    source_hash = sha256_file(task.source_path)
    original_output = task.output_path
    real_unlink = Path.unlink

    def fail_processing_unlink(self, *args, **kwargs):
        if self == original_output:
            raise PermissionError("synthetic cleanup denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_processing_unlink)
    with pytest.raises(Exception):
        env["tm"].finalize(task, task.audit)
    assert task.status != TaskStatus.PASSED
    assert not list(env["success"].iterdir())
    assert sha256_file(task.source_path) == source_hash


def test_diagnostic_generation_failure_is_explicit(runtime, monkeypatch):
    env = runtime
    service = DiagnosticService(
        settings=env["settings"], config_state=env["config_state"],
        health=env["health"], repository=env["repo"], metrics=env["metrics"],
        log_dir=env["logs"], diagnostics_dir=env["diagnostics"])
    monkeypatch.setattr(zipfile, "ZipFile",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(Exception, match="诊断包生成失败"):
        service.create()


def test_event_chain_anomaly_is_unhealthy_and_counted(runtime):
    env = runtime
    task = Task(source_path=env["root"] / "chain.docx", task_id="chain001",
                status=TaskStatus.PENDING)
    from localdocmask.domain.entities import TaskEvent
    env["repo"].save_task(task, events=[TaskEvent(
        event_code="test.event", stage="test",
        from_status="PENDING", to_status="PENDING", detail="original")])
    env["repo"]._conn.execute(
        "UPDATE task_events SET detail='tampered' WHERE task_id=?",
        (task.task_id,))
    env["repo"]._conn.commit()
    health = HealthService(env["repo"], env["metrics"], env["config_state"])
    report = health.check()
    assert report.status == UNHEALTHY
    assert report.components["event_chain"]["reason"] == "event_chain_invalid"
    assert env["metrics"].snapshot()["counters"].get("event_chain_invalid") == 1



def test_queue_and_worker_metrics_are_recorded(runtime):
    env = runtime
    env["settings"].security.max_queue_depth = 1
    service = BatchService(env["tm"], env["settings"])
    one = _docx(env["root"] / "metric-one.docx")
    two = _docx(env["root"] / "metric-two.docx")
    with pytest.raises(QueueFullError):
        service.run([one, two], mode="a")
    snapshot = env["metrics"].snapshot()
    assert snapshot["counters"].get("queue_rejected") == 2

    service.run([one], mode="a")
    snapshot = env["metrics"].snapshot()
    assert snapshot["counters"].get("queue_accepted", 0) >= 1
    assert "worker_slots_in_use" in snapshot["counters"]


def test_metrics_write_failure_marks_degraded_not_success(runtime, monkeypatch):
    env = runtime
    def fail_record(*args, **kwargs):
        raise OSError("synthetic metrics failure")
    monkeypatch.setattr(env["metrics"], "record", fail_record)
    env["tm"]._metric("task_success", 1, stage="finalize")
    assert env["health"].check().status == DEGRADED
    assert "task_success" not in env["metrics"].snapshot()["counters"]
