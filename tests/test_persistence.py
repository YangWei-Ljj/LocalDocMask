"""持久化层测试（ADR-005）：建库幂等、save/load 往返、列表过滤、幂等键、
操作日志、崩溃恢复，以及“库中绝不出现完整敏感原文”的安全断言。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from localdocmask.domain.entities import (
    AuditFinding,
    AuditReport,
    Candidate,
    ScanResult,
    Task,
)
from localdocmask.domain.enums import AuditVerdict, ItemStatus, TaskStatus
from localdocmask.security.hashing import sha256_file
from localdocmask.infrastructure.persistence.recovery import (
    RecoveryService,
    existing_success,
)
from localdocmask.infrastructure.persistence.sqlite_repo import (
    SCHEMA_VERSION,
    SqliteTaskRepository,
    initialize_database,
    migrate,
)

# 构造一个明显敏感的原文，用于“库中无明文”断言
SECRET_NAME = "张小明"
SECRET_PHONE = "13900001111"
SECRET_ID = "11010519491231002X"
SECRET_FULL = SECRET_NAME + SECRET_PHONE  # 拼接串，绝不应整体出现在库中


def _make_task(tmp_path: Path, task_id: str, status: TaskStatus,
               *, with_output: bool, mode: str = "a",
               policy_version: str = "1.0", source_hash: str = "hash-src") -> Task:
    src = tmp_path / f"{task_id}-source.docx"
    src.write_bytes(b"source")
    out = tmp_path / f"{task_id}-output.docx"
    if with_output:
        out.write_bytes(b"masked output")
    task = Task(
        source_path=src,
        task_id=task_id,
        file_type="docx",
        status=status,
        source_hash=source_hash,
        mode=mode,
        policy_version=policy_version,
        output_path=out if with_output else None,
        final_hash=sha256_file(out) if with_output else "",
        config_hash="cfg-1",
        tool_version="tool-1",
        scan=ScanResult(candidates=[
            Candidate(text=SECRET_FULL, category="姓名",
                      status=ItemStatus.REDACTED.value),
            Candidate(text=SECRET_ID, category="身份证号",
                      risk_level="high", decision="auto_confirm",
                      status=ItemStatus.REDACTED.value,
                      failure_reason=""),
        ]),
        audit=AuditReport(
            passed=(status == TaskStatus.PASSED),
            findings=[
                AuditFinding(AuditVerdict.NO_RESIDUAL, "未发现残留",
                             scope="text"),
            ],
        ),
        recognized_count=2,
        redacted_count=2,
    )
    return task


def _dump_all_text(db_path: Path) -> str:
    """把库中所有表所有单元格拼成一个大字符串，用于明文扫描。"""
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        chunks = []
        for table in tables:
            for row in conn.execute(f"SELECT * FROM {table}"):
                chunks.extend(str(v) for v in row)
        return "\n".join(chunks)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 建表 / 迁移幂等
# ---------------------------------------------------------------------------

def test_initialize_is_idempotent(tmp_path):
    db = tmp_path / "sub" / "app.db"
    initialize_database(db)
    initialize_database(db)  # 第二次：不应报错
    migrate(db, SCHEMA_VERSION, SCHEMA_VERSION)  # 同版本 no-op

    conn = sqlite3.connect(str(db))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert version == SCHEMA_VERSION
    assert {"tasks", "task_items", "audit_findings", "operation_log", "idempotency_claims", "task_events"} <= tables


def test_migrate_explicit(tmp_path):
    db = tmp_path / "explicit.db"
    result = migrate(db, 0, SCHEMA_VERSION)
    assert result == SCHEMA_VERSION
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# save / load 往返
# ---------------------------------------------------------------------------

def test_save_load_roundtrip(tmp_path):
    db = tmp_path / "roundtrip.db"
    repo = SqliteTaskRepository(db)
    task = _make_task(tmp_path, "t-rt", TaskStatus.PASSED, with_output=True)
    repo.save_task(task)

    loaded = repo.load_task("t-rt")
    assert loaded is not None
    assert loaded.task_id == "t-rt"
    assert loaded.status == TaskStatus.PASSED
    assert loaded.source_hash == "hash-src"
    assert loaded.mode == "a"
    assert loaded.policy_version == "1.0"
    assert loaded.file_type == "docx"
    assert Path(loaded.output_path).name.endswith("output.docx")
    assert loaded.final_hash == sha256_file(Path(loaded.output_path))
    assert loaded.config_hash == "cfg-1"
    assert loaded.tool_version == "tool-1"
    assert loaded.recognized_count == 2
    assert loaded.redacted_count == 2

    # 条目：数量一致，且文本为掩码值而非原文
    assert loaded.scan is not None
    assert len(loaded.scan.candidates) == 2
    masked = {c.category: c.text for c in loaded.scan.candidates}
    assert SECRET_FULL not in masked["姓名"]
    assert masked["姓名"].startswith("张")
    assert SECRET_ID not in masked["身份证号"]
    assert masked["身份证号"].startswith("1101") and masked["身份证号"].endswith("002X")

    # 审计结论
    assert loaded.audit is not None
    assert loaded.audit.passed is True
    assert loaded.audit.findings[0].verdict == AuditVerdict.NO_RESIDUAL
    assert loaded.audit.findings[0].scope == "text"

    repo.close()


def test_load_missing_returns_none(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "empty.db")
    assert repo.load_task("no-such") is None
    repo.close()


# ---------------------------------------------------------------------------
# 列表与过滤
# ---------------------------------------------------------------------------

def test_list_tasks_and_open(tmp_path):
    db = tmp_path / "list.db"
    repo = SqliteTaskRepository(db)
    repo.save_task(_make_task(tmp_path, "t-pending", TaskStatus.PENDING,
                              with_output=False))
    repo.save_task(_make_task(tmp_path, "t-processing", TaskStatus.PROCESSING,
                              with_output=False))
    repo.save_task(_make_task(tmp_path, "t-passed", TaskStatus.PASSED,
                              with_output=True))

    assert len(repo.list_tasks()) == 3
    pending = repo.list_tasks(TaskStatus.PENDING)
    assert [t.task_id for t in pending] == ["t-pending"]

    open_ids = {t.task_id for t in repo.list_open()}
    assert open_ids == {"t-pending", "t-processing"}
    repo.close()


# ---------------------------------------------------------------------------
# 幂等键
# ---------------------------------------------------------------------------

def test_find_by_source_idempotency_key(tmp_path):
    db = tmp_path / "idem.db"
    repo = SqliteTaskRepository(db)
    repo.save_task(_make_task(tmp_path, "t-1", TaskStatus.PASSED,
                              with_output=True, source_hash="H1",
                              mode="a", policy_version="1.0"))

    hit = repo.find_by_source("H1", "a", "1.0")
    assert hit is not None and hit.task_id == "t-1"
    # 键的任一部分不同都不算命中
    assert repo.find_by_source("H1", "b", "1.0") is None
    assert repo.find_by_source("H1", "a", "2.0") is None
    assert repo.find_by_source("H2", "a", "1.0") is None
    repo.close()


# ---------------------------------------------------------------------------
# 操作日志
# ---------------------------------------------------------------------------

def test_append_operation(tmp_path):
    db = tmp_path / "ops.db"
    repo = SqliteTaskRepository(db)
    repo.save_task(_make_task(tmp_path, "t-op", TaskStatus.PENDING,
                              with_output=False))
    repo.append_operation("t-op", "scan_started", "开始扫描")
    repo.append_operation("t-op", "scan_done", "扫描完成 2 条")

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT action, detail FROM operation_log WHERE task_id=? "
            "ORDER BY id", ("t-op",)).fetchall()
    finally:
        conn.close()
    assert rows == [("scan_started", "开始扫描"), ("scan_done", "扫描完成 2 条")]
    repo.close()


# ---------------------------------------------------------------------------
# 崩溃恢复
# ---------------------------------------------------------------------------

def test_recover_moves_interrupted_without_output_to_pending(tmp_path):
    db = tmp_path / "rec.db"
    repo = SqliteTaskRepository(db)
    # 处理中、output 文件不存在（崩溃时未产出）
    repo.save_task(_make_task(tmp_path, "t-crash", TaskStatus.PROCESSING,
                              with_output=False))
    # 处理中、output 已落盘（残留产物，不得自动放行）
    repo.save_task(_make_task(tmp_path, "t-crash-out", TaskStatus.PROCESSING,
                              with_output=True))
    # 已排队，不应被动
    repo.save_task(_make_task(tmp_path, "t-queued", TaskStatus.PENDING,
                              with_output=False))

    affected = RecoveryService().recover(repo, tmp_path / "processing")
    affected_ids = {t.task_id: t.status for t in affected}
    assert affected_ids["t-crash"] == TaskStatus.PENDING
    assert affected_ids["t-crash-out"] == TaskStatus.INTERRUPTED

    after = {t.task_id: t.status for t in repo.list_tasks()}
    assert after["t-crash"] == TaskStatus.PENDING
    assert after["t-crash-out"] == TaskStatus.INTERRUPTED
    assert after["t-queued"] == TaskStatus.PENDING  # 未被动
    repo.close()


def test_existing_success(tmp_path):
    db = tmp_path / "success.db"
    repo = SqliteTaskRepository(db)
    repo.save_task(_make_task(tmp_path, "t-ok", TaskStatus.PASSED,
                              with_output=True, source_hash="H9",
                              mode="a", policy_version="1.0"))

    info = existing_success(repo, "H9", "a", "1.0")
    assert info is not None
    assert info["task_id"] == "t-ok"
    assert info["final_hash"] == sha256_file(Path(info["output_path"]))

    # 模式不同 -> 不可复用
    assert existing_success(repo, "H9", "b", "1.0") is None

    # 删除产物 -> 不复用
    Path(info["output_path"]).unlink()
    assert existing_success(repo, "H9", "a", "1.0") is None
    repo.close()


# ---------------------------------------------------------------------------
# 安全：库中绝不出现完整敏感原文
# ---------------------------------------------------------------------------

def test_no_plaintext_sensitive_values_in_db(tmp_path):
    db = tmp_path / "security.db"
    repo = SqliteTaskRepository(db)
    repo.save_task(_make_task(tmp_path, "t-sec", TaskStatus.PASSED,
                              with_output=True))
    repo.append_operation("t-sec", "finalized", "已完成")
    repo.close()

    blob = _dump_all_text(db)
    for secret in (SECRET_FULL, SECRET_PHONE, SECRET_ID, SECRET_NAME):
        assert secret not in blob, f"敏感原文泄漏进数据库：{secret!r}"
