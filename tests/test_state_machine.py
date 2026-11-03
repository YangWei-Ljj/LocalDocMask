# -*- coding: utf-8 -*-
"""T1：显式任务状态机回归测试。"""
from pathlib import Path
import re
import sqlite3

import pytest

from localdocmask.domain.entities import AuditFinding, AuditReport, Task
from localdocmask.domain.enums import AuditVerdict, TaskStatus
from localdocmask.domain.exceptions import InvalidTaskTransition
from localdocmask.domain.state_machine import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    can_transition,
    coerce_task_status,
    is_terminal,
    transition_task,
)
from localdocmask.infrastructure.persistence.sqlite_repo import SqliteTaskRepository, initialize_database
from localdocmask.infrastructure.config.settings import SUCCESS_DIR
from localdocmask.security.hashing import sha256_file
from localdocmask.recognition.rule_based import Detector
from localdocmask.application.pipeline import TaskManager

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 显式规范表（不从实现读取，避免测试自证）
EXPECTED_ALLOWED = {
    TaskStatus.PENDING: {
        TaskStatus.PROCESSING,
        TaskStatus.NEEDS_REVIEW,
        TaskStatus.PROCESSED_UNAUDITED,
        TaskStatus.AUDIT_FAILED,
        TaskStatus.UNSAFE,
        TaskStatus.INTERRUPTED,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
    },
    TaskStatus.PROCESSING: {
        TaskStatus.PENDING,
        TaskStatus.NEEDS_REVIEW,
        TaskStatus.PROCESSED_UNAUDITED,
        TaskStatus.AUDIT_FAILED,
        TaskStatus.UNSAFE,
        TaskStatus.INTERRUPTED,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
    },
    TaskStatus.NEEDS_REVIEW: {
        TaskStatus.PROCESSING,
        TaskStatus.PROCESSED_UNAUDITED,
        TaskStatus.AUDIT_FAILED,
        TaskStatus.UNSAFE,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
    },
    TaskStatus.PROCESSED_UNAUDITED: {
        TaskStatus.PROCESSING,
        TaskStatus.NEEDS_REVIEW,
        TaskStatus.PASSED,
        TaskStatus.AUDIT_FAILED,
        TaskStatus.UNSAFE,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
    },
    TaskStatus.INTERRUPTED: {TaskStatus.PENDING},
    TaskStatus.PASSED: set(),
    TaskStatus.AUDIT_FAILED: set(),
    TaskStatus.UNSAFE: set(),
    TaskStatus.CANCELLED: set(),
    TaskStatus.TIMED_OUT: set(),
}


def _task(status: TaskStatus) -> Task:
    return Task(source_path=Path("x.docx"), status=status)


def test_transition_table_matches_spec():
    assert set(ALLOWED_TRANSITIONS) == set(EXPECTED_ALLOWED)
    for current, allowed in EXPECTED_ALLOWED.items():
        assert set(ALLOWED_TRANSITIONS[current]) == set(allowed)


def test_every_pair_allowed_or_rejected_and_state_preserved():
    for current in TaskStatus:
        for target in TaskStatus:
            task = _task(current)
            allowed = current == target or target in EXPECTED_ALLOWED[current]
            if allowed:
                transition_task(task, target)
                assert task.status == target
            else:
                with pytest.raises(InvalidTaskTransition):
                    transition_task(task, target)
                assert task.status == current, f"非法转移后状态被改变：{current}->{target}"


def test_terminal_states_are_immutable():
    for terminal in TERMINAL_STATUSES:
        assert is_terminal(terminal)
        for target in TaskStatus:
            task = _task(terminal)
            if target == terminal:
                transition_task(task, target)  # 同状态幂等
                assert task.status == terminal
            else:
                with pytest.raises(InvalidTaskTransition):
                    transition_task(task, target)
                assert task.status == terminal


def test_production_flow_transitions():
    flow = [
        TaskStatus.PENDING,
        TaskStatus.PROCESSING,
        TaskStatus.NEEDS_REVIEW,
        TaskStatus.PROCESSING,
        TaskStatus.PROCESSED_UNAUDITED,
        TaskStatus.PASSED,
    ]
    task = _task(flow[0])
    for target in flow[1:]:
        transition_task(task, target)
    assert task.status == TaskStatus.PASSED

    recovery = _task(TaskStatus.PROCESSING)
    transition_task(recovery, TaskStatus.INTERRUPTED)
    transition_task(recovery, TaskStatus.PENDING)
    assert recovery.status == TaskStatus.PENDING

    failure = _task(TaskStatus.PENDING)
    transition_task(failure, TaskStatus.AUDIT_FAILED)
    assert failure.status == TaskStatus.AUDIT_FAILED


def test_coerce_unknown_status_is_interrupted():
    assert coerce_task_status("PASSED") == TaskStatus.PASSED
    assert coerce_task_status("审计通过，可以导出") == TaskStatus.PASSED
    assert coerce_task_status("FUTURE_STATUS") == TaskStatus.INTERRUPTED
    assert coerce_task_status("") == TaskStatus.INTERRUPTED
    assert coerce_task_status(None) == TaskStatus.INTERRUPTED


def test_database_unknown_status_loads_as_interrupted(tmp_path):
    db = tmp_path / "unknown.db"
    repo = SqliteTaskRepository(db)
    try:
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "INSERT INTO tasks (task_id, source_path, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("t-unknown", str(tmp_path / "x.docx"), "FUTURE_STATUS", "now", "now"),
            )
        initialize_database(db)  # 启动归一：未知状态持久化为 INTERRUPTED
        loaded = repo.load_task("t-unknown")
        assert loaded is not None
        assert loaded.status == TaskStatus.INTERRUPTED
        assert loaded.status != TaskStatus.PENDING
        assert all(t.task_id != "t-unknown" for t in repo.list_tasks(TaskStatus.PENDING))
        assert any(t.task_id == "t-unknown" for t in repo.list_tasks(TaskStatus.INTERRUPTED))
        with sqlite3.connect(str(db)) as conn:
            stored = conn.execute("SELECT status FROM tasks WHERE task_id=?", ("t-unknown",)).fetchone()[0]
        assert stored == "INTERRUPTED"
    finally:
        repo.close()


def test_unknown_status_cannot_enter_success_dir(tmp_path):
    src = tmp_path / "src.docx"
    out = tmp_path / "out.docx"
    src.write_bytes(b"source")
    out.write_bytes(b"output")
    report = AuditReport(passed=True, findings=[
        AuditFinding(AuditVerdict.NO_RESIDUAL, "ok")])
    task = Task(
        source_path=src,
        task_id="t-interrupted",
        status=TaskStatus.INTERRUPTED,
        source_hash=sha256_file(src),
        output_path=out,
        final_hash=sha256_file(out),
        audit=report,
        records=[],
    )
    tm = TaskManager(Detector())
    dest = SUCCESS_DIR / out.name
    if dest.exists():
        dest.unlink()
    with pytest.raises(InvalidTaskTransition):
        tm.finalize(task, report)
    assert task.status == TaskStatus.INTERRUPTED
    assert not dest.exists()


def test_no_direct_task_status_assignment_in_src():
    pattern = re.compile(r"\.status\s*=\s*TaskStatus\.")
    offenders = []
    for path in (PROJECT_ROOT / "src").rglob("*.py"):
        if path.name == "state_machine.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno}")
    assert offenders == [], f"发现绕过状态机的直接赋值：{offenders}"
