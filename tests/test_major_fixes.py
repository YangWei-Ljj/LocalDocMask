# -*- coding: utf-8 -*-
"""Major 修复回归：M1(审计 detail 落库掩码) / M2(恢复/操作日志/敏感登记/幂等跳过/超时)。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from localdocmask.application.composition import build_services
from localdocmask.application.pipeline import TaskManager
from localdocmask.domain.entities import (
    AuditFinding,
    AuditReport,
    Candidate,
    ScanResult,
    Task,
)
from localdocmask.domain.enums import AuditVerdict, TaskStatus
from localdocmask.domain.exceptions import SecurityViolation
from localdocmask.infrastructure.persistence.sqlite_repo import sanitize_detail

COMPANY = "中国石油天然气集团有限公司"
PHONE = "13812345678"
IDNO = "11010519491231002X"


# ---------------------------------------------------------------------------
# M1：audit_findings.detail 落库前强制掩码
# ---------------------------------------------------------------------------
def test_sanitize_detail_unit():
    # 候选原文（公司名）被掩码
    out = sanitize_detail(
        f"输出文本中发现残留：{COMPANY}",
        [Candidate(text=COMPANY, category="公司名")])
    assert COMPANY not in out
    # 高风险正则（手机）被掩码，即便不在候选列表
    out2 = sanitize_detail(f"高风险模式：{PHONE}", [])
    assert PHONE not in out2
    # 身份证
    out3 = sanitize_detail(f"残留：{IDNO}", [])
    assert IDNO not in out3


def test_audit_detail_not_plaintext_in_db(tmp_path, monkeypatch):
    """失败关闭全程后，tasks.db.audit_findings.detail 不出现完整原文。"""
    monkeypatch.setenv("LOCALDOCMASK_HOME", str(tmp_path))
    svc = build_services()
    src = tmp_path / "s.docx"
    src.write_bytes(b"x")
    task = Task(source_path=src, task_id="m1", file_type="docx",
                status=TaskStatus.AUDIT_FAILED, source_hash="h-m1",
                mode="b", policy_version="recognition-v1")
    task.scan = ScanResult(candidates=[
        Candidate(text=COMPANY, category="公司名"),
        Candidate(text=PHONE, category="手机号"),
    ])
    task.audit = AuditReport(passed=False, findings=[
        AuditFinding(AuditVerdict.RESIDUAL_FOUND, f"输出文本中发现残留：{COMPANY}", scope="text"),
        AuditFinding(AuditVerdict.RESIDUAL_FOUND, f"输出文本中发现残留：{PHONE}", scope="text"),
        AuditFinding(AuditVerdict.RESIDUAL_FOUND, f"隐藏部件残留：{IDNO}", scope="hidden"),
    ])
    svc.repository.save_task(task)

    conn = sqlite3.connect(str(tmp_path / "tasks.db"))
    try:
        rows = conn.execute("SELECT detail FROM audit_findings").fetchall()
    finally:
        conn.close()
    blob = "\n".join(r[0] for r in rows)
    assert COMPANY not in blob, f"公司名明文落库：{blob}"
    assert PHONE not in blob, f"手机明文落库：{blob}"
    assert IDNO not in blob, f"身份证明文落库：{blob}"
    # 仍有结论可读（掩码后）
    assert "残留" in blob
    svc.close()


# ---------------------------------------------------------------------------
# M2：启动恢复 + 操作日志在真实运行路径非空
# ---------------------------------------------------------------------------
def test_startup_writes_recovery_operation(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALDOCMASK_HOME", str(tmp_path))
    svc = build_services()
    conn = sqlite3.connect(str(tmp_path / "tasks.db"))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM operation_log WHERE action='startup'").fetchone()[0]
    finally:
        conn.close()
    assert n >= 1  # build_services 真实调用了 recover 并写启动日志
    svc.close()


def test_operation_log_populated_after_real_run(tmp_path, monkeypatch):
    from docx import Document
    monkeypatch.setenv("LOCALDOCMASK_HOME", str(tmp_path))
    svc = build_services()
    src = tmp_path / "ops.docx"
    d = Document()
    d.add_paragraph(f"手机{PHONE} 邮箱a@b.com")
    d.save(str(src))
    task = svc.pipeline.prepare(src)
    svc.pipeline.scan(task)
    svc.pipeline.process(task, "a", "****", task.scan.candidates)
    svc.pipeline.finalize(task, task.audit)

    conn = sqlite3.connect(str(tmp_path / "tasks.db"))
    try:
        rows = conn.execute(
            "SELECT action FROM operation_log WHERE task_id=? ORDER BY id",
            (task.task_id,)).fetchall()
    finally:
        conn.close()
    actions = [r[0] for r in rows]
    assert "prepare" in actions
    assert "scan" in actions
    assert "process" in actions
    assert "finalize" in actions
    svc.close()


# ---------------------------------------------------------------------------
# M2：幂等自动跳过（force 可强制重跑）
# ---------------------------------------------------------------------------
def test_batch_idempotent_skip_and_force(tmp_path, monkeypatch):
    from docx import Document
    monkeypatch.setenv("LOCALDOCMASK_HOME", str(tmp_path))
    svc = build_services()
    src = tmp_path / "idem2.docx"
    d = Document()
    d.add_paragraph(f"手机{PHONE} 邮箱a@b.com")
    d.save(str(src))

    r1 = svc.batch.run([src], mode="b")
    assert len(r1.passed) == 1, r1.warnings
    # 第二次：命中已成功产物，跳过
    r2 = svc.batch.run([src], mode="b")
    assert len(r2.skipped) == 1
    assert len(r2.passed) == 0
    # force=True 强制重处理
    r3 = svc.batch.run([src], mode="b", force=True)
    assert len(r3.passed) == 1
    svc.close()


# ---------------------------------------------------------------------------
# M2：超时守卫在阶段边界失败关闭
# ---------------------------------------------------------------------------
def test_deadline_guard_raises():
    tm = TaskManager()  # 旧路径，无 settings
    tm._timeout_seconds = 0
    tm._deadline = 0.0  # 已过期
    with pytest.raises(SecurityViolation):
        tm._check_deadline()


# ---------------------------------------------------------------------------
# m6：review_flags 把文件路由到人工复核（自动安全批次不自动导出）
# ---------------------------------------------------------------------------
def test_review_flags_routes_to_review():
    from localdocmask.application.batch import BatchService
    scan = ScanResult(candidates=[],
                      review_flags=["第 1 页为图文混排，嵌入图像需人工复核"])
    assert BatchService._needs_review(scan) is True
