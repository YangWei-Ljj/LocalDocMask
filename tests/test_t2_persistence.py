# -*- coding: utf-8 -*-
"""T2：SQLite v2 事务、幂等声明、安全复用、迁移/恢复。"""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from localdocmask.application.batch import BatchService
from localdocmask.application.composition import build_services
from localdocmask.application.pipeline import compute_config_hash
from localdocmask.domain.entities import AuditFinding, AuditReport, Candidate, ScanResult, Task
from localdocmask.domain.enums import AuditVerdict, ItemStatus, TaskStatus
from localdocmask.domain.state_machine import transition_task
from localdocmask.infrastructure.config.settings import Settings
from localdocmask.infrastructure.persistence import sqlite_repo
from localdocmask.infrastructure.persistence.sqlite_repo import (
    SCHEMA_VERSION,
    SqliteTaskRepository,
    initialize_database,
    make_idempotency_key,
    migrate,
    normalize_unknown_statuses,
    restore_database,
    backup_database,
)
from localdocmask.security.hashing import sha256_file

KEY = dict(source_hash="H1", mode="a", policy_version="p1",
           config_hash="cfg1", tool_version="tool1")


def _task(tmp_path: Path, task_id: str, *, status=TaskStatus.PENDING,
          with_output=False, **overrides) -> Task:
    src = tmp_path / f"{task_id}-src.docx"
    src.write_bytes(b"source")
    out = tmp_path / f"{task_id}-out.docx"
    if with_output:
        out.write_bytes(b"masked output")
    params = dict(KEY)
    params.update(overrides)
    return Task(
        source_path=src,
        task_id=task_id,
        file_type="docx",
        status=status,
        output_path=out if with_output else None,
        final_hash=sha256_file(out) if with_output else "",
        scan=ScanResult(candidates=[
            Candidate(text="张小明", category="姓名",
                      status=ItemStatus.REDACTED.value),
        ]),
        audit=AuditReport(
            passed=(status == TaskStatus.PASSED),
            findings=[AuditFinding(AuditVerdict.NO_RESIDUAL, "未发现残留", scope="text")],
        ),
        recognized_count=1,
        redacted_count=1,
        **params,
    )


def _make_claimed_success(repo: SqliteTaskRepository, tmp_path: Path,
                          task_id: str = "success-1"):
    task = _task(tmp_path, task_id)
    claim = repo.claim_or_get(task)
    assert claim.outcome == "created", claim
    out = tmp_path / f"{task_id}-claimed-out.docx"
    out.write_bytes(b"verified output")
    task.output_path = out
    task.final_hash = sha256_file(out)
    task.audit = AuditReport(passed=True, findings=[
        AuditFinding(AuditVerdict.NO_RESIDUAL, "ok", scope="text")])
    transition_task(task, TaskStatus.PROCESSING)
    transition_task(task, TaskStatus.PROCESSED_UNAUDITED)
    transition_task(task, TaskStatus.PASSED)
    repo.save_task(task)
    return task


def test_v1_to_v2_backup_migrate_integrity_restore(tmp_path):
    db = tmp_path / "v1.db"
    assert migrate(db, 0, 1) == 1
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, source_path, source_hash, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy", str(tmp_path / "legacy.docx"), "LEGACY-HASH", "PASSED", "now", "now"))
        conn.commit()
    finally:
        conn.close()

    backup = initialize_database(db)
    assert backup is not None and backup.exists()

    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert {"config_hash", "tool_version"} <= cols
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "idempotency_claims" in tables
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE task_id='legacy'").fetchone()[0] == 1
    finally:
        conn.close()

    repo = SqliteTaskRepository(db)
    try:
        assert repo.load_task("legacy") is not None
    finally:
        repo.close()

    restore_database(backup, db)
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "config_hash" not in cols
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE task_id='legacy'").fetchone()[0] == 1
    finally:
        conn.close()


def test_migration_failure_rolls_back_to_v1(tmp_path, monkeypatch):
    db = tmp_path / "fail.db"
    assert migrate(db, 0, 1) == 1
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, source_path, status, created_at, updated_at) "
            "VALUES ('old', 'x.docx', 'PENDING', 'now', 'now')")
        conn.commit()
    finally:
        conn.close()

    def broken_v2(conn):
        conn.execute("ALTER TABLE tasks ADD COLUMN config_hash TEXT NOT NULL DEFAULT ''")
        raise RuntimeError("inject migration failure")

    monkeypatch.setitem(sqlite_repo._MIGRATIONS, 2, broken_v2)
    with pytest.raises(RuntimeError):
        initialize_database(db)

    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "config_hash" not in cols
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "idempotency_claims" not in tables
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert list(tmp_path.glob("fail.db.bak-*")), "迁移前备份缺失"


def test_save_task_rolls_back_on_item_failure(tmp_path, monkeypatch):
    repo = SqliteTaskRepository(tmp_path / "tx.db")
    task = _task(tmp_path, "tx-item")
    repo.save_task(task)
    original = sqlite_repo.mask_display

    def fail_mask(value, category=""):
        if value == "BOOM":
            raise RuntimeError("inject item failure")
        return original(value, category)

    task.status = TaskStatus.PASSED
    task.scan.candidates.append(Candidate(
        text="BOOM", category="姓名", status=ItemStatus.REDACTED.value))
    monkeypatch.setattr(sqlite_repo, "mask_display", fail_mask)
    with pytest.raises(RuntimeError):
        repo.save_task(task)

    loaded = repo.load_task("tx-item")
    assert loaded is not None
    assert loaded.status == TaskStatus.PENDING
    assert all(c.text != "BOOM" for c in loaded.scan.candidates)
    repo.close()


def test_save_task_rolls_back_on_audit_failure(tmp_path, monkeypatch):
    repo = SqliteTaskRepository(tmp_path / "tx-audit.db")
    task = _task(tmp_path, "tx-audit")
    repo.save_task(task)
    original = sqlite_repo.sanitize_detail

    def fail_detail(detail, candidates=None):
        if "FAIL_AUDIT" in detail:
            raise RuntimeError("inject audit failure")
        return original(detail, candidates)

    task.audit.findings[0].detail = "FAIL_AUDIT"
    monkeypatch.setattr(sqlite_repo, "sanitize_detail", fail_detail)
    with pytest.raises(RuntimeError):
        repo.save_task(task)

    loaded = repo.load_task("tx-audit")
    assert loaded is not None
    assert loaded.audit is not None
    assert loaded.audit.findings[0].detail == "未发现残留"
    repo.close()


def test_concurrent_claim_only_one_success(tmp_path):
    db = tmp_path / "concurrent.db"
    repo1 = SqliteTaskRepository(db)
    repo2 = SqliteTaskRepository(db)
    barrier = threading.Barrier(2)

    def worker(repo, task_id):
        task = _task(tmp_path, task_id)
        barrier.wait(timeout=5)
        return repo.claim_or_get(task)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, repo, tid)
                   for repo, tid in ((repo1, "c-1"), (repo2, "c-2"))]
        claims = [f.result(timeout=10) for f in futures]

    created = [c for c in claims if c.outcome == "created"]
    others = [c for c in claims if c.outcome != "created"]
    assert len(created) == 1, [c.outcome for c in claims]
    assert len(others) == 1
    assert others[0].outcome in {"in_progress", "reused"}

    winner = created[0].task
    out = tmp_path / "winner-out.docx"
    out.write_bytes(b"winner")
    winner.output_path = out
    winner.final_hash = sha256_file(out)
    winner.audit = AuditReport(passed=True, findings=[
        AuditFinding(AuditVerdict.NO_RESIDUAL, "ok")])
    transition_task(winner, TaskStatus.PROCESSING)
    transition_task(winner, TaskStatus.PROCESSED_UNAUDITED)
    transition_task(winner, TaskStatus.PASSED)
    repo1.save_task(winner)

    assert len(repo1.list_tasks()) == 1
    hit = repo1.find_reusable(KEY["source_hash"], KEY["mode"],
                              KEY["policy_version"], KEY["config_hash"],
                              KEY["tool_version"])
    assert hit is not None and hit.task_id == winner.task_id
    repo1.close()
    repo2.close()


def test_reuse_requires_existing_output_and_hash(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "reuse.db")
    task = _make_claimed_success(repo, tmp_path)
    args = (KEY["source_hash"], KEY["mode"], KEY["policy_version"],
            KEY["config_hash"], KEY["tool_version"])
    assert repo.find_reusable(*args) is not None

    Path(task.output_path).unlink()
    assert repo.find_reusable(*args) is None

    Path(task.output_path).write_bytes(b"tampered")
    assert repo.find_reusable(*args) is None

    new_task = _task(tmp_path, "reuse-new")
    claim = repo.claim_or_get(new_task)
    assert claim.outcome == "stale_recreated"
    assert repo.load_task(task.task_id) is not None
    assert len(repo.list_tasks()) == 2
    repo.close()


def test_force_creates_new_attempt_and_preserves_old(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "force.db")
    old = _make_claimed_success(repo, tmp_path)
    new_task = _task(tmp_path, "force-new")
    claim = repo.claim_or_get(new_task, force=True)
    assert claim.outcome == "forced_created"
    assert claim.task.task_id == "force-new"
    assert repo.load_task(old.task_id) is not None
    assert len(repo.list_tasks()) == 2
    claim_row = repo._conn.execute(
        "SELECT task_id FROM idempotency_claims WHERE idem_key=?",
        (make_idempotency_key(**KEY),)).fetchone()
    assert claim_row["task_id"] == "force-new"
    repo.close()


def test_config_hash_is_stable_and_excludes_non_result_config(tmp_path):
    base = Settings()
    h1 = compute_config_hash(base)
    assert h1 == compute_config_hash(Settings())
    base.recognition.auto_confirm_threshold = 0.8
    assert compute_config_hash(base) != h1

    other = Settings()
    other.logging.level = "DEBUG"
    other.paths.data_dir = str(tmp_path / "elsewhere")
    other.security.max_workers = 8
    assert compute_config_hash(other) != h1

    a = Settings(); a.ner.model_dir = r"C:\models\nerA"
    b = Settings(); b.ner.model_dir = r"D:\other\nerA"
    assert compute_config_hash(a) == compute_config_hash(b)


def test_list_open_uses_sql_status_filter(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "list.db")
    for i in range(20):
        repo.save_task(_task(tmp_path, f"closed-{i}", status=TaskStatus.PASSED,
                             with_output=True, source_hash=f"hash-{i}"))
    statements = []
    repo._conn.set_trace_callback(statements.append)
    try:
        assert repo.list_open() == []
    finally:
        repo._conn.set_trace_callback(None)
    sql = "\n".join(statements)
    assert "WHERE status IN" in sql
    assert "SELECT * FROM tasks ORDER BY updated_at DESC" not in sql
    repo.close()


def test_unknown_status_is_persisted_as_interrupted(tmp_path):
    db = tmp_path / "unknown.db"
    repo = SqliteTaskRepository(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, source_path, status, created_at, updated_at) "
            "VALUES ('u-1', 'x.docx', 'FUTURE_STATUS', 'now', 'now')")
        conn.commit()
    finally:
        conn.close()
    initialize_database(db)
    assert repo.list_tasks(TaskStatus.PENDING) == []
    interrupted = repo.list_tasks(TaskStatus.INTERRUPTED)
    assert any(t.task_id == "u-1" for t in interrupted)
    assert any(t.task_id == "u-1" for t in repo.list_open())
    with sqlite3.connect(str(db)) as conn:
        stored = conn.execute(
            "SELECT status FROM tasks WHERE task_id='u-1'").fetchone()[0]
    assert stored == "INTERRUPTED"
    repo.close()


def test_save_task_operation_log_failure_rolls_back(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "tx-operation.db")
    task = _task(tmp_path, "tx-operation")
    repo.save_task(task)
    repo._conn.execute(
        "CREATE TRIGGER fail_operation BEFORE INSERT ON operation_log "
        "BEGIN SELECT RAISE(ABORT, 'inject operation failure'); END")
    task.status = TaskStatus.PROCESSING
    with pytest.raises(sqlite3.DatabaseError):
        repo.save_task(task, operation=("test", "should rollback"))
    loaded = repo.load_task("tx-operation")
    assert loaded is not None and loaded.status == TaskStatus.PENDING
    count = repo._conn.execute(
        "SELECT COUNT(*) FROM operation_log WHERE task_id=?",
        ("tx-operation",)).fetchone()[0]
    assert count == 0
    repo.close()


# ---------------------------------------------------------------------------
# P0-1：批次异常必须持久化失败状态
# ---------------------------------------------------------------------------

def _make_services(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALDOCMASK_HOME", str(tmp_path))
    return build_services()


def test_batch_corrupt_file_persists_failure_and_retry_not_blocked(tmp_path, monkeypatch):
    svc = _make_services(tmp_path, monkeypatch)
    try:
        bad = tmp_path / "broken.docx"
        bad.write_bytes(b"this is not a zip/docx")
        batch = BatchService(svc.pipeline, svc.settings)

        first = batch.run([bad], mode="a")
        assert len(first.failed) == 1, first.warnings
        first_task = first.failed[0]
        stored = svc.repository.load_task(first_task.task_id)
        assert stored is not None
        assert stored.status in (TaskStatus.AUDIT_FAILED, TaskStatus.UNSAFE)
        assert stored.status != TaskStatus.PENDING

        second = batch.run([bad], mode="a")
        assert len(second.failed) == 1, second.warnings
        assert all(t.task_id != first_task.task_id for t in second.skipped)
        assert not any("同键任务正在处理" in w for w in second.warnings), second.warnings
        stored2 = svc.repository.load_task(second.failed[0].task_id)
        assert stored2 is not None
        assert stored2.status in (TaskStatus.AUDIT_FAILED, TaskStatus.UNSAFE)
    finally:
        svc.close()


def test_batch_persistence_failure_is_explicit(tmp_path, monkeypatch):
    svc = _make_services(tmp_path, monkeypatch)
    try:
        bad = tmp_path / "broken.docx"
        bad.write_bytes(b"this is not a zip/docx")
        original = svc.repository.save_task

        def failing_save(task, *, operation=None):
            if task.status == TaskStatus.AUDIT_FAILED:
                raise RuntimeError("inject persistence failure")
            return original(task, operation=operation)

        monkeypatch.setattr(svc.repository, "save_task", failing_save)
        result = BatchService(svc.pipeline, svc.settings).run([bad], mode="a")
        assert len(result.failed) == 1
        joined = "\n".join(result.warnings)
        assert "持久化失败" in joined, result.warnings
        assert "已隔离并持久化" not in joined, result.warnings
    finally:
        svc.close()


class _PassedThenFailPipeline:
    repository = None

    def prepare(self, source):
        return Task(source_path=Path(str(source)), task_id="passed-before-error",
                    status=TaskStatus.PASSED)

    def scan(self, task):
        return ScanResult(candidates=[])

    def process(self, task, mode, mask, candidates, mapper=None):
        raise RuntimeError("post-success exception")

    def finalize(self, task, report):
        return task


def test_passed_task_not_reverted_on_batch_exception(tmp_path):
    src = tmp_path / "passed.docx"
    src.write_bytes(b"x")
    result = BatchService(_PassedThenFailPipeline()).run([src], mode="a")
    assert len(result.passed) == 1
    assert result.passed[0].status == TaskStatus.PASSED
    assert result.failed == []
    assert any("保留已确认成功" in w for w in result.warnings), result.warnings


# ---------------------------------------------------------------------------
# P0-2：SQLite backup() 与安全恢复
# ---------------------------------------------------------------------------

def _insert_task_row(db, task_id):
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, source_path, source_hash, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, f"{task_id}.docx", f"hash-{task_id}", "PENDING", "now", "now"))
        conn.commit()
    finally:
        conn.close()


def test_backup_with_active_reader_and_committed_wal_data(tmp_path):
    db = tmp_path / "wal.db"
    repo = SqliteTaskRepository(db)
    repo.save_task(_task(tmp_path, "before", source_hash="before"))
    reader = sqlite3.connect(str(db), timeout=5.0)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM tasks").fetchone()

    writer = sqlite3.connect(str(db), timeout=5.0)
    _insert_task_row(db, "during-wal")
    writer.close()

    backup = backup_database(db)
    assert backup.exists()
    conn = sqlite3.connect(str(backup))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        ids = {r[0] for r in conn.execute("SELECT task_id FROM tasks")}
    finally:
        conn.close()
    assert ids == {"before", "during-wal"}
    reader.rollback()
    reader.close()
    repo.close()


def test_backup_of_invalid_database_raises(tmp_path):
    bad = tmp_path / "bad-source.db"
    bad.write_bytes(b"not a sqlite database")
    with pytest.raises(Exception):
        backup_database(bad)


def test_restore_rejects_corrupt_backup_and_preserves_target(tmp_path):
    target = tmp_path / "target.db"
    repo = SqliteTaskRepository(target)
    repo.save_task(_task(tmp_path, "keep-me", source_hash="keep"))
    repo.close()
    bad = tmp_path / "bad-backup.db"
    bad.write_bytes(b"not a sqlite database")

    with pytest.raises(Exception):
        restore_database(bad, target)

    conn = sqlite3.connect(str(target))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE task_id='keep-me'").fetchone()[0] == 1
    finally:
        conn.close()


def test_restore_valid_backup_replaces_target(tmp_path):
    source = tmp_path / "source.db"
    src_repo = SqliteTaskRepository(source)
    src_repo.save_task(_task(tmp_path, "from-source", source_hash="source"))
    src_repo.close()
    backup = backup_database(source)

    target = tmp_path / "target.db"
    tgt_repo = SqliteTaskRepository(target)
    tgt_repo.save_task(_task(tmp_path, "from-target", source_hash="target"))
    tgt_repo.close()

    restore_database(backup, target)
    conn = sqlite3.connect(str(target))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE task_id='from-source'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE task_id='from-target'").fetchone()[0] == 0
    finally:
        conn.close()
