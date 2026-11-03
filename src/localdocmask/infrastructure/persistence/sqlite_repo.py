"""基于标准库 sqlite3 的任务持久化（ADR-005）。

设计要点：
- 零新增依赖；数据库文件随数据目录（LOCALDOCMASK_HOME / data/）存放，gitignored。
- 显式 schema 版本号（PRAGMA user_version）+ 显式迁移函数表，不使用无版本
  create_all 漂移；新建与升级幂等。
- 幂等键：(source_hash, mode, policy_version, config_hash, tool_version) 由 idempotency_claims 唯一声明。
- task_events 以全局递增序号与 prev_hash 形成 SHA-256 哈希链（T3）。
- 安全约束（强制）：task_items 只存 mask_display 处理后的掩码值，绝不落原文；
  不记录完整映射表；审计 detail 为元信息，调用方不得放入敏感原文。

枚举按 ``name``（稳定标识符）存储，不存中文 value，便于跨版本比较与筛选。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ...domain.entities import (
    AuditFinding,
    AuditReport,
    Candidate,
    ScanResult,
    Task,
    ClaimResult,
    TaskEvent,
    EventChainReport,
)
from ...domain.enums import AuditVerdict, EntityDecision, RiskLevel, TaskStatus
from ...domain.state_machine import coerce_task_status, current_actor, is_terminal
from ...domain.exceptions import ErrorCode, LocalDocMaskError, SecurityViolation
from ...recognition import patterns as _patterns
from ...security.redaction.masker import mask_display

SCHEMA_VERSION = 4

#: 落库前对 audit_findings.detail 强制掩码的高风险模式（与 auditor 一致）。
#: 持久化边界强制处理，不依赖调用方自觉（M1 修复）。
_DETAIL_HIGH_RISK = [
    ("身份证号", _patterns.ID_CARD_RE,
     lambda s: _patterns.is_valid_id(s) and _patterns.is_valid_birth(s)),
    ("手机号", _patterns.MOBILE_RE, None),
    ("固定电话", _patterns.LANDLINE_RE, None),
    ("邮箱", _patterns.EMAIL_RE, None),
    ("护照号", _patterns.PASSPORT_RE, None),
    ("银行卡号", _patterns.BANK_CARD_RE, _patterns.luhn_ok),
]


def sanitize_detail(detail: str, candidates=None) -> str:
    """落库前对审计 finding.detail 做强制掩码，确保绝不写入完整原文。

    1) 用高风险正则（手机/身份证/邮箱/银行卡/固话/护照）命中即替换为 mask_display；
    2) 对本次任务候选原文逐一替换为其掩码值。
    内存中的 finding.detail 保持原样（仅供本机会话展示），仅持久化时过本函数。
    """
    if not detail:
        return detail
    out = detail
    for cat, rx, validator in _DETAIL_HIGH_RISK:
        def _repl(m, _cat=cat, _v=validator):
            val = m.group(0)
            if _v is not None and not _v(val):
                return m.group(0)
            return mask_display(val, _cat)
        out = rx.sub(_repl, out)
    for cand in candidates or []:
        text = getattr(cand, "text", "") or ""
        cat = getattr(cand, "category", "") or ""
        if text and text in out:
            out = out.replace(text, mask_display(text, cat))
    return out

#: 处于“未终结”状态的任务集合（供 list_open / 恢复逻辑复用）
OPEN_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.PENDING,
    TaskStatus.PROCESSING,
    TaskStatus.NEEDS_REVIEW,
    TaskStatus.PROCESSED_UNAUDITED,
    TaskStatus.INTERRUPTED,
})


def _now() -> str:
    """ISO 8601（UTC）时间戳。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_idempotency_key(source_hash: str, mode: str, policy_version: str,
                         config_hash: str, tool_version: str) -> str:
    """稳定幂等键：五个维度以分隔符连接后 SHA-256。"""
    parts = [source_hash or "", mode or "", policy_version or "",
             config_hash or "", tool_version or ""]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


EVENT_GENESIS_HASH = "0" * 64


def sanitize_event_detail(detail: str) -> str:
    """事件详情只允许短元信息：去控制字符、限长；不写入原文。"""
    if not detail:
        return ""
    text = str(detail).replace("\r", " ").replace("\n", " ").replace("\x00", " ")
    text = " ".join(text.split())
    return sanitize_detail(text)[:1000]


def _event_payload(*, event_id: str, global_seq: int, task_id: str,
                   event_code: str, from_status: str, to_status: str,
                   stage: str, actor: str, occurred_at: str,
                   source_hash: str, output_hash: str,
                   policy_version: str, config_hash: str, tool_version: str,
                   error_code: str, detail: str, duration_ms: int,
                   prev_hash: str) -> str:
    return json.dumps({
        "event_id": event_id,
        "global_seq": int(global_seq),
        "task_id": task_id,
        "event_code": event_code,
        "from_status": from_status,
        "to_status": to_status,
        "stage": stage,
        "actor": actor,
        "occurred_at": occurred_at,
        "source_hash": source_hash,
        "output_hash": output_hash,
        "policy_version": policy_version,
        "config_hash": config_hash,
        "tool_version": tool_version,
        "error_code": error_code,
        "detail": detail,
        "duration_ms": int(duration_ms),
        "prev_hash": prev_hash,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _insert_event_conn(conn: sqlite3.Connection, *, task_id: str,
                       event_code: str, from_status: str, to_status: str,
                       stage: str = "", actor: str = "", occurred_at: str = "",
                       source_hash: str = "", output_hash: str = "",
                       policy_version: str = "", config_hash: str = "",
                       tool_version: str = "", error_code: str = "",
                       detail: str = "", duration_ms: int = 0) -> dict:
    """在当前事务内追加一条事件并计算全局哈希链。"""
    seq_row = conn.execute(
        "SELECT COALESCE(MAX(global_seq), 0) + 1 FROM task_events").fetchone()
    seq = int(seq_row[0])
    prev_row = conn.execute(
        "SELECT event_hash FROM task_events ORDER BY global_seq DESC LIMIT 1"
    ).fetchone()
    prev_hash = prev_row[0] if prev_row else EVENT_GENESIS_HASH
    event_id = uuid.uuid4().hex
    occurred = occurred_at or _now()
    actor = actor or current_actor()
    detail = sanitize_event_detail(detail)
    payload = _event_payload(
        event_id=event_id, global_seq=seq, task_id=task_id,
        event_code=event_code, from_status=from_status, to_status=to_status,
        stage=stage, actor=actor, occurred_at=occurred,
        source_hash=source_hash or "", output_hash=output_hash or "",
        policy_version=policy_version or "", config_hash=config_hash or "",
        tool_version=tool_version or "",
        error_code=error_code or "", detail=detail,
        duration_ms=duration_ms or 0, prev_hash=prev_hash)
    event_hash = _event_hash(payload)
    conn.execute(
        """
        INSERT INTO task_events (
            event_id, global_seq, task_id, event_code, from_status,
            to_status, stage, actor, occurred_at, source_hash,
            output_hash, policy_version, config_hash, tool_version,
            error_code, detail, duration_ms, prev_hash, event_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (event_id, seq, task_id, event_code, from_status, to_status,
         stage, actor, occurred, source_hash or "", output_hash or "",
         policy_version or "", config_hash or "", tool_version or "",
         error_code or "", detail, duration_ms or 0, prev_hash, event_hash),
    )
    return {
        "event_id": event_id, "global_seq": seq, "task_id": task_id,
        "event_code": event_code, "from_status": from_status,
        "to_status": to_status, "stage": stage, "actor": actor,
        "occurred_at": occurred, "source_hash": source_hash or "",
        "output_hash": output_hash or "", "policy_version": policy_version or "",
        "config_hash": config_hash or "", "tool_version": tool_version or "",
        "error_code": error_code or "",
        "detail": detail, "duration_ms": duration_ms or 0,
        "prev_hash": prev_hash, "event_hash": event_hash,
    }


def verify_event_chain_conn(conn: sqlite3.Connection,
                            task_id: str | None = None) -> EventChainReport:
    """校验全局哈希链；task_id 仅限定报告范围，仍校验整条全局链。"""
    rows = conn.execute(
        "SELECT * FROM task_events ORDER BY global_seq").fetchall()
    expected_seq = 1
    expected_prev = EVENT_GENESIS_HASH
    checked = 0
    for row in rows:
        if task_id is None or row["task_id"] == task_id:
            checked += 1
        if int(row["global_seq"]) != expected_seq:
            return EventChainReport(
                valid=False, checked=checked,
                first_bad_seq=int(row["global_seq"]),
                reason=f"全局序号断裂：期望 {expected_seq}，实际 {row['global_seq']}")
        if row["prev_hash"] != expected_prev:
            return EventChainReport(
                valid=False, checked=checked,
                first_bad_seq=int(row["global_seq"]),
                reason="prev_hash 与上一条事件不匹配")
        payload = _event_payload(
            event_id=row["event_id"], global_seq=row["global_seq"],
            task_id=row["task_id"], event_code=row["event_code"],
            from_status=row["from_status"], to_status=row["to_status"],
            stage=row["stage"], actor=row["actor"],
            occurred_at=row["occurred_at"], source_hash=row["source_hash"],
            output_hash=row["output_hash"],
            policy_version=row["policy_version"],
            config_hash=row["config_hash"],
            tool_version=row["tool_version"],
            error_code=row["error_code"],
            detail=row["detail"], duration_ms=row["duration_ms"],
            prev_hash=row["prev_hash"])
        if _event_hash(payload) != row["event_hash"]:
            return EventChainReport(
                valid=False, checked=checked,
                first_bad_seq=int(row["global_seq"]),
                reason="event_hash 校验失败（事件内容被修改）")
        expected_prev = row["event_hash"]
        expected_seq += 1
    return EventChainReport(valid=True, checked=checked)


# ---------------------------------------------------------------------------
# Schema / 迁移
# ---------------------------------------------------------------------------

_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id          TEXT PRIMARY KEY,
        source_path      TEXT NOT NULL,
        source_hash      TEXT NOT NULL DEFAULT '',
        file_type        TEXT NOT NULL DEFAULT '',
        status           TEXT NOT NULL DEFAULT 'PENDING',
        mode             TEXT NOT NULL DEFAULT '',
        output_path      TEXT,
        final_hash       TEXT NOT NULL DEFAULT '',
        error            TEXT NOT NULL DEFAULT '',
        policy_version   TEXT NOT NULL DEFAULT '',
        recognized_count INTEGER NOT NULL DEFAULT 0,
        redacted_count   INTEGER NOT NULL DEFAULT 0,
        deleted_count    INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tasks_idem
        ON tasks(source_hash, mode, policy_version)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)
    """,
    """
    CREATE TABLE IF NOT EXISTS task_items (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id        TEXT NOT NULL
                       REFERENCES tasks(task_id) ON DELETE CASCADE,
        category       TEXT NOT NULL DEFAULT '',
        value_masked   TEXT NOT NULL DEFAULT '',
        risk           TEXT NOT NULL DEFAULT '',
        decision       TEXT NOT NULL DEFAULT '',
        status         TEXT NOT NULL DEFAULT '',
        failure_reason TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_task_items_task ON task_items(task_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_findings (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id  TEXT NOT NULL
                 REFERENCES tasks(task_id) ON DELETE CASCADE,
        verdict  TEXT NOT NULL DEFAULT '',
        scope    TEXT NOT NULL DEFAULT '',
        detail   TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audit_findings_task ON audit_findings(task_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS operation_log (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT,
        action  TEXT NOT NULL DEFAULT '',
        detail  TEXT NOT NULL DEFAULT '',
        ts      TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_operation_log_task ON operation_log(task_id)
    """,
)

_V2_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS idempotency_claims (
        idem_key       TEXT PRIMARY KEY,
        task_id        TEXT NOT NULL
                       REFERENCES tasks(task_id) ON DELETE CASCADE,
        source_hash    TEXT NOT NULL DEFAULT '',
        mode           TEXT NOT NULL DEFAULT '',
        policy_version TEXT NOT NULL DEFAULT '',
        config_hash    TEXT NOT NULL DEFAULT '',
        tool_version   TEXT NOT NULL DEFAULT '',
        final_hash     TEXT NOT NULL DEFAULT '',
        output_path    TEXT,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_claims_task
        ON idempotency_claims(task_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tasks_full_idem
        ON tasks(source_hash, mode, policy_version, config_hash, tool_version)
    """,
)


_V3_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS task_events (
        event_id     TEXT PRIMARY KEY,
        global_seq   INTEGER NOT NULL UNIQUE,
        task_id      TEXT NOT NULL
                     REFERENCES tasks(task_id) ON DELETE CASCADE,
        event_code   TEXT NOT NULL,
        from_status  TEXT NOT NULL DEFAULT '',
        to_status    TEXT NOT NULL DEFAULT '',
        stage        TEXT NOT NULL DEFAULT '',
        actor        TEXT NOT NULL DEFAULT '',
        occurred_at  TEXT NOT NULL,
        source_hash  TEXT NOT NULL DEFAULT '',
        output_hash  TEXT NOT NULL DEFAULT '',
        policy_version TEXT NOT NULL DEFAULT '',
        config_hash  TEXT NOT NULL DEFAULT '',
        tool_version TEXT NOT NULL DEFAULT '',
        error_code   TEXT NOT NULL DEFAULT '',
        detail       TEXT NOT NULL DEFAULT '',
        duration_ms  INTEGER NOT NULL DEFAULT 0,
        prev_hash    TEXT NOT NULL,
        event_hash   TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_events_task_seq
        ON task_events(task_id, global_seq)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_events_global_seq
        ON task_events(global_seq)
    """,
)


def _apply_v1(conn: sqlite3.Connection) -> None:
    """v0 -> v1：建全部表与索引（IF NOT EXISTS，幂等）。"""
    for stmt in _V1_STATEMENTS:
        conn.execute(stmt)


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _apply_v3(conn: sqlite3.Connection) -> None:
    """v2 -> v3：新增全局事件哈希链表与索引；不改写历史业务表。"""
    for stmt in _V3_STATEMENTS:
        conn.execute(stmt)


def _apply_v2(conn: sqlite3.Connection) -> None:
    """v1 -> v2：追加配置/工具版本字段、幂等声明表，并回填历史键。"""
    cols = _column_names(conn, "tasks")
    if "config_hash" not in cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN config_hash TEXT NOT NULL DEFAULT ''")
    if "tool_version" not in cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN tool_version TEXT NOT NULL DEFAULT ''")
    for stmt in _V2_STATEMENTS:
        conn.execute(stmt)

    # 回填：每个旧键选择最新任务作为“当前尝试”，不删除/合并历史任务。
    rows = conn.execute(
        "SELECT * FROM tasks ORDER BY updated_at DESC, created_at DESC"
    ).fetchall()
    seen: set[str] = set()
    for row in rows:
        source_hash = row["source_hash"] or ""
        if not source_hash:
            continue
        key = make_idempotency_key(
            source_hash, row["mode"] or "", row["policy_version"] or "",
            row["config_hash"] or "", row["tool_version"] or "")
        if key in seen:
            continue
        seen.add(key)
        conn.execute(
            """
            INSERT OR IGNORE INTO idempotency_claims
                (idem_key, task_id, source_hash, mode, policy_version,
                 config_hash, tool_version, final_hash, output_path,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key, row["task_id"], source_hash, row["mode"] or "",
                row["policy_version"] or "", row["config_hash"] or "",
                row["tool_version"] or "", row["final_hash"] or "",
                row["output_path"], row["created_at"], row["updated_at"] or _now(),
            ),
        )


def _apply_v4(conn: sqlite3.Connection) -> None:
    """v3 -> v4：补齐候选复核状态与元信息，不改写历史任务。"""
    task_cols = _column_names(conn, "tasks")
    if "review_flags_json" not in task_cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN review_flags_json TEXT NOT NULL DEFAULT '[]'")
    cols = _column_names(conn, "task_items")
    columns = {
        "candidate_id": "TEXT NOT NULL DEFAULT ''",
        "original_fingerprint": "TEXT NOT NULL DEFAULT ''",
        "location": "TEXT NOT NULL DEFAULT ''",
        "suggested_action": "TEXT NOT NULL DEFAULT ''",
        "final_action": "TEXT NOT NULL DEFAULT ''",
        "confirmed": "INTEGER NOT NULL DEFAULT 0",
        "excluded": "INTEGER NOT NULL DEFAULT 0",
        "replacement_value": "TEXT NOT NULL DEFAULT ''",
        "source": "TEXT NOT NULL DEFAULT ''",
        "confidence": "REAL NOT NULL DEFAULT 1.0",
        "review_actor": "TEXT NOT NULL DEFAULT ''",
        "reviewed_at": "TEXT NOT NULL DEFAULT ''",
        "review_reason_code": "TEXT NOT NULL DEFAULT ''",
    }
    for name, declaration in columns.items():
        if name not in cols:
            conn.execute(
                f"ALTER TABLE task_items ADD COLUMN {name} {declaration}")
    conn.execute(
        "UPDATE task_items SET candidate_id = printf('legacy-%d', id) "
        "WHERE candidate_id = ''")
    conn.execute(
        "UPDATE task_items SET suggested_action = decision "
        "WHERE suggested_action = ''")
    safe_decisions = (
        "'auto_confirm', 'entitydecision.auto_confirm', "
        "'suppress', 'entitydecision.suppress', "
        "'dismissed', 'entitydecision.dismissed'"
    )
    conn.execute(
        "UPDATE task_items SET confirmed = 1, excluded = 0, "
        "final_action = 'redact' "
        "WHERE lower(decision) IN ('auto_confirm', "
        "'entitydecision.auto_confirm')")
    conn.execute(
        "UPDATE task_items SET confirmed = 0, excluded = 1, "
        "final_action = 'exclude' "
        "WHERE lower(decision) IN ('suppress', 'entitydecision.suppress', "
        "'dismissed', 'entitydecision.dismissed')")
    conn.execute(
        "UPDATE task_items SET decision = 'needs_review', "
        "suggested_action = 'needs_review', confirmed = 0, excluded = 0, "
        "final_action = 'pending' "
        f"WHERE lower(decision) NOT IN ({safe_decisions})")


#: 迁移表：目标版本 -> 把库从 (目标版本-1) 升级到目标版本的函数。
_MIGRATIONS: dict[int, object] = {
    1: _apply_v1,
    2: _apply_v2,
    3: _apply_v3,
    4: _apply_v4,
}


def _current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def normalize_unknown_statuses(conn: sqlite3.Connection,
                                 *, record_events: bool = True) -> int:
    """把未知/未来状态持久化为 INTERRUPTED，并在 v3 记录恢复事件。"""
    known = {s.name for s in TaskStatus} | {s.value for s in TaskStatus}
    statuses = [r[0] for r in conn.execute(
        "SELECT DISTINCT status FROM tasks").fetchall()]
    changed = 0
    now = _now()
    has_events = _table_exists(conn, "task_events")
    for status in statuses:
        if status in known:
            continue
        task_rows = conn.execute(
            "SELECT task_id, source_hash, output_path, final_hash, "
            "policy_version, config_hash, tool_version "
            "FROM tasks WHERE status = ?", (status,)).fetchall()
        cur = conn.execute(
            "UPDATE tasks SET status = 'INTERRUPTED', updated_at = ? "
            "WHERE status = ?", (now, status))
        changed += cur.rowcount
        if record_events and has_events:
            for task_row in task_rows:
                _insert_event_conn(
                    conn, task_id=task_row["task_id"],
                    event_code="phase.recover.start", stage="recover",
                    from_status=str(status), to_status=str(status),
                    source_hash=task_row["source_hash"] or "",
                    output_hash=task_row["final_hash"] or "",
                    policy_version=task_row["policy_version"] or "",
                    config_hash=task_row["config_hash"] or "",
                    tool_version=task_row["tool_version"] or "",
                    detail="unknown_status")
                _insert_event_conn(
                    conn, task_id=task_row["task_id"],
                    event_code="recovery.unknown_status",
                    from_status=str(status), to_status="INTERRUPTED",
                    stage="recover", source_hash=task_row["source_hash"] or "",
                    output_hash=task_row["final_hash"] or "",
                    error_code="unknown_status",
                    detail="unknown_status_persisted")
    return changed


def _migrate_conn(conn: sqlite3.Connection, from_version: int,
                  to_version: int) -> int:
    """在显式事务中执行迁移；失败 rollback，绝不留下半迁移库。"""
    if from_version == to_version:
        return from_version
    try:
        conn.execute("BEGIN IMMEDIATE")
        version = from_version
        while version < to_version:
            nxt = version + 1
            step = _MIGRATIONS.get(nxt)
            if step is None:
                raise LocalDocMaskError(
                    f"缺少数据库迁移：v{from_version} -> v{to_version} "
                    f"（无到 v{nxt} 的迁移函数）",
                    code=ErrorCode.PERSISTENCE_ERROR,
                )
            step(conn)  # type: ignore[operator]
            conn.execute(f"PRAGMA user_version = {int(nxt)}")
            version = nxt
        conn.commit()
        return version
    except Exception:
        conn.rollback()
        raise


def _validate_sqlite_file(path: str | Path) -> None:
    """打开并执行 integrity_check；失败显式抛出，不静默放行。"""
    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or str(row[0]).lower() != "ok":
            raise LocalDocMaskError(
                f"SQLite 完整性检查失败：{path} -> {row[0] if row else 'no result'}",
                code=ErrorCode.PERSISTENCE_ERROR,
            )
    except LocalDocMaskError:
        raise
    except Exception as exc:
        raise LocalDocMaskError(
            f"无法验证 SQLite 文件：{path}（{type(exc).__name__}: {exc}）",
            code=ErrorCode.PERSISTENCE_ERROR,
        ) from exc
    finally:
        conn.close()


def backup_database(db_path: str | Path,
                    backup_path: str | Path | None = None) -> Path:
    """用 sqlite3.Connection.backup() 生成一致性备份。

    - 不复制主库文件，不依赖 wal_checkpoint 的返回；
    - 先写临时目标，完整性校验通过后 os.replace 原子落盘；
    - 任何连接/备份/校验失败都显式抛出。
    """
    db = Path(db_path)
    if not db.exists() or db.stat().st_size == 0:
        raise FileNotFoundError(f"数据库不存在或为空，无法备份：{db}")
    if backup_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = db.with_name(f"{db.name}.bak-{stamp}-{uuid.uuid4().hex[:8]}")
    dest = Path(backup_path)
    if dest.resolve() == db.resolve():
        raise ValueError("备份路径不能与源数据库相同")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.tmp-{uuid.uuid4().hex}")

    src_conn = None
    dst_conn = None
    try:
        src_conn = sqlite3.connect(str(db), timeout=5.0)
        dst_conn = sqlite3.connect(str(tmp), timeout=5.0)
        src_conn.backup(dst_conn)
        dst_conn.commit()
        dst_conn.close()
        dst_conn = None
        src_conn.close()
        src_conn = None
        _validate_sqlite_file(tmp)
        os.replace(tmp, dest)
        return dest
    except Exception:
        if dst_conn is not None:
            dst_conn.close()
        if src_conn is not None:
            src_conn.close()
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        raise


def restore_database(backup_path: str | Path, db_path: str | Path) -> None:
    """安全恢复：先验证备份与临时副本，再 checkpoint 目标、原子替换。

    验证前绝不删除目标库的 WAL/SHM；任一失败都保留原目标库。
    """
    backup = Path(backup_path)
    db = Path(db_path)
    if not backup.exists() or backup.stat().st_size == 0:
        raise FileNotFoundError(f"备份不存在或为空：{backup}")

    # 1) 先验证备份本身；失败时完全不触碰目标库
    _validate_sqlite_file(backup)

    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_name(f"{db.name}.restore-{uuid.uuid4().hex}.tmp")
    try:
        # 2) 写入临时目标并再次验证
        shutil.copy2(backup, tmp)
        _validate_sqlite_file(tmp)

        # 3) 目标存在时先 checkpoint，确保主库已包含 WAL 数据；
        #    若 checkpoint 失败，直接失败，不删除任何侧车文件。
        if db.exists() and db.stat().st_size > 0:
            conn = sqlite3.connect(str(db), timeout=5.0)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()

        # 4) 原子替换（os.replace）；侧车在 checkpoint 成功后才移除
        for suffix in ("-wal", "-shm"):
            side = Path(str(db) + suffix)
            if side.exists():
                side.unlink()
        os.replace(tmp, db)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        raise


def migrate(db_path: str | Path, from_version: int, to_version: int) -> int:
    """显式轻量迁移：迁移前自动备份，失败回滚；返回最终版本号。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        current = _current_version(conn)
        if current > to_version:
            raise LocalDocMaskError(
                f"数据库版本 v{current} 高于目标 v{to_version}，拒绝降级",
                code=ErrorCode.PERSISTENCE_ERROR,
            )
        if 0 < current < to_version:
            backup_database(db_path)
        version = _migrate_conn(conn, current, to_version)
        # v3 起才可记录恢复事件；v2 单独迁移时保留未知状态，留给 v3 启动归一
        if to_version >= 3:
            normalize_unknown_statuses(conn)
        conn.commit()
        return version
    finally:
        conn.close()


def initialize_database(db_path: str | Path,
                        schema_version: int = SCHEMA_VERSION) -> Path | None:
    """幂等建库：迁移前备份、迁移事务、未知状态持久化归一。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        current = _current_version(conn)
        if current > schema_version:
            raise LocalDocMaskError(
                f"数据库版本 v{current} 高于应用支持的 v{schema_version}，"
                f"拒绝继续（防止降级）",
                code=ErrorCode.PERSISTENCE_ERROR,
            )
    finally:
        conn.close()

    backup = None
    if 0 < current < schema_version:
        backup = backup_database(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _migrate_conn(conn, current, schema_version)
        normalize_unknown_statuses(conn)
        conn.commit()
        return backup
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------

def _coerce_status(name: str) -> TaskStatus:
    """数据库状态归一：未知/未来状态一律 INTERRUPTED（失败关闭）。

    T1 修复：旧实现把未知状态回退 PENDING，可能让未验证任务被重新处理或误导出。
    """
    return coerce_task_status(name)


def _coerce_verdict(name: str) -> AuditVerdict:
    try:
        return AuditVerdict[name]
    except KeyError:
        return AuditVerdict.NEEDS_CONFIRM


def _coerce_decision(name: str) -> EntityDecision:
    try:
        return EntityDecision(str(name))
    except (KeyError, ValueError):
        return EntityDecision.NEEDS_REVIEW


def _coerce_risk(name: str) -> RiskLevel:
    try:
        return RiskLevel(str(name))
    except (KeyError, ValueError):
        return RiskLevel.MEDIUM


class SqliteTaskRepository:
    """Sqlite 版 TaskRepository（含事务、写锁、幂等声明与安全复用）。"""

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        initialize_database(self._db_path)
        # isolation_level=None：由 _transaction() 显式管理 BEGIN/COMMIT/ROLLBACK
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False,
            isolation_level=None, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA synchronous = NORMAL")

    # -- 生命周期 ----------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SqliteTaskRepository":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self):
        """显式写事务；任何异常都 rollback。调用方需持有 _lock。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    # -- 内部：键/行 -------------------------------------------------------
    @staticmethod
    def _task_key(task: Task) -> str:
        return make_idempotency_key(
            task.source_hash, task.mode, task.policy_version,
            getattr(task, "config_hash", "") or "",
            getattr(task, "tool_version", "") or "")

    def _upsert_task_row(self, task: Task, now: str) -> None:
        row = self._conn.execute(
            "SELECT created_at FROM tasks WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()
        created_at = row["created_at"] if row else now
        status_name = getattr(task.status, "name", str(task.status))
        self._conn.execute(
            """
            INSERT INTO tasks (
                task_id, source_path, source_hash, file_type, status, mode,
                output_path, final_hash, error, policy_version,
                config_hash, tool_version,
                recognized_count, redacted_count, deleted_count,
                review_flags_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                source_path      = excluded.source_path,
                source_hash      = excluded.source_hash,
                file_type        = excluded.file_type,
                status           = excluded.status,
                mode             = excluded.mode,
                output_path      = excluded.output_path,
                final_hash       = excluded.final_hash,
                error            = excluded.error,
                policy_version   = excluded.policy_version,
                config_hash      = excluded.config_hash,
                tool_version     = excluded.tool_version,
                recognized_count = excluded.recognized_count,
                redacted_count   = excluded.redacted_count,
                deleted_count    = excluded.deleted_count,
                review_flags_json = excluded.review_flags_json,
                updated_at       = excluded.updated_at
            """,
            (
                task.task_id,
                str(task.source_path),
                task.source_hash,
                task.file_type,
                status_name,
                task.mode,
                str(task.output_path) if task.output_path else None,
                task.final_hash,
                task.error,
                task.policy_version,
                getattr(task, "config_hash", "") or "",
                getattr(task, "tool_version", "") or "",
                task.recognized_count,
                task.redacted_count,
                task.deleted_count,
                json.dumps(task.scan.review_flags if task.scan else [],
                           ensure_ascii=False),
                created_at,
                now,
            ),
        )

    def _upsert_claim(self, key: str, task: Task, now: str) -> None:
        self._conn.execute(
            """
            INSERT INTO idempotency_claims (
                idem_key, task_id, source_hash, mode, policy_version,
                config_hash, tool_version, final_hash, output_path,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idem_key) DO UPDATE SET
                task_id        = excluded.task_id,
                source_hash    = excluded.source_hash,
                mode           = excluded.mode,
                policy_version = excluded.policy_version,
                config_hash    = excluded.config_hash,
                tool_version   = excluded.tool_version,
                final_hash     = excluded.final_hash,
                output_path    = excluded.output_path,
                updated_at     = excluded.updated_at
            """,
            (
                key, task.task_id, task.source_hash, task.mode,
                task.policy_version, getattr(task, "config_hash", "") or "",
                getattr(task, "tool_version", "") or "", task.final_hash,
                str(task.output_path) if task.output_path else None,
                now, now,
            ),
        )

    def _refresh_claim_for_task(self, task: Task, now: str) -> None:
        """仅当声明仍指向当前任务时刷新产物信息，避免旧任务覆盖新尝试。"""
        key = self._task_key(task)
        self._conn.execute(
            """
            UPDATE idempotency_claims
               SET final_hash = ?, output_path = ?, updated_at = ?
             WHERE idem_key = ? AND task_id = ?
            """,
            (
                task.final_hash,
                str(task.output_path) if task.output_path else None,
                now, key, task.task_id,
            ),
        )

    # -- 写入 --------------------------------------------------------------
    def save_task(self, task: Task, *, operation: tuple[str, str] | None = None,
                  events: list[TaskEvent] | None = None,
                  release_claim: bool = False) -> None:
        """原子写入任务/条目/审计/操作日志/事件；事件失败则整体回滚。"""
        if not task.task_id:
            task.task_id = uuid.uuid4().hex
        now = _now()
        with self._lock:
            with self._transaction():
                self._upsert_task_row(task, now)

                self._conn.execute(
                    "DELETE FROM task_items WHERE task_id = ?", (task.task_id,))
                candidates = task.scan.candidates if task.scan else []
                for cand in candidates:
                    masked_value = (getattr(cand, "masked_value", "") or
                                    mask_display(cand.text, cand.category))
                    fingerprint = getattr(cand, "original_fingerprint", "") or ""
                    if not fingerprint and cand.text and cand.text != masked_value:
                        fingerprint = hashlib.sha256(
                            f"{cand.category}\x00{cand.text}".encode("utf-8")
                        ).hexdigest()
                    replacement = getattr(cand, "replacement", "") or ""
                    raw_text_known = bool(
                        cand.text and (not getattr(cand, "masked_value", "")
                                       or cand.text != cand.masked_value))
                    if raw_text_known and replacement and cand.text in replacement:
                        raise SecurityViolation(
                            "替换值不得包含原始敏感值",
                            code=ErrorCode.SECURITY_VIOLATION)
                    decision_value = getattr(cand.decision, "value", cand.decision)
                    risk_value = getattr(cand.risk_level, "value", cand.risk_level)
                    final_action = getattr(cand, "final_action", "") or (
                        "exclude" if cand.excluded else
                        "redact" if cand.confirmed else "pending")
                    self._conn.execute(
                        """
                        INSERT INTO task_items
                            (task_id, candidate_id, original_fingerprint,
                             category, value_masked,
                             location, risk, suggested_action, final_action,
                             decision, confirmed, excluded, replacement_value,
                             source, confidence, status, failure_reason,
                             review_actor, reviewed_at, review_reason_code)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            task.task_id,
                            getattr(cand, "candidate_id", "") or "",
                            fingerprint,
                            cand.category,
                            masked_value,
                            getattr(cand, "location", "") or "",
                            risk_value,
                            getattr(cand, "suggested_action", "") or decision_value,
                            final_action,
                            decision_value,
                            1 if cand.confirmed else 0,
                            1 if cand.excluded else 0,
                            replacement,
                            getattr(cand, "source", "") or "",
                            float(getattr(cand, "confidence", 0.0) or 0.0),
                            cand.status,
                            cand.failure_reason,
                            getattr(cand, "review_actor", "") or "",
                            getattr(cand, "reviewed_at", "") or "",
                            getattr(cand, "review_reason_code", "") or "",
                        ),
                    )

                self._conn.execute(
                    "DELETE FROM audit_findings WHERE task_id = ?", (task.task_id,))
                findings = task.audit.findings if task.audit else []
                for finding in findings:
                    self._conn.execute(
                        """
                        INSERT INTO audit_findings
                            (task_id, verdict, scope, detail)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            task.task_id,
                            finding.verdict.name,
                            finding.scope,
                            sanitize_detail(finding.detail, candidates),
                        ),
                    )

                if operation is not None:
                    action, detail = operation
                    self._conn.execute(
                        """
                        INSERT INTO operation_log (task_id, action, detail, ts)
                        VALUES (?, ?, ?, ?)
                        """,
                        (task.task_id, action, detail, now),
                    )

                all_events = list(getattr(task, "pending_events", []) or [])
                all_events.extend(events or [])
                for ev in all_events:
                    current_status = getattr(task.status, "name", str(task.status))
                    _insert_event_conn(
                        self._conn,
                        task_id=task.task_id,
                        event_code=ev.event_code,
                        from_status=ev.from_status or current_status,
                        to_status=ev.to_status or current_status,
                        stage=ev.stage,
                        actor=ev.actor,
                        occurred_at=ev.occurred_at,
                        source_hash=task.source_hash,
                        output_hash=task.final_hash,
                        policy_version=task.policy_version,
                        config_hash=getattr(task, "config_hash", "") or "",
                        tool_version=getattr(task, "tool_version", "") or "",
                        error_code=ev.error_code,
                        detail=ev.detail,
                        duration_ms=ev.duration_ms,
                    )

                if release_claim:
                    self._conn.execute(
                        "DELETE FROM idempotency_claims WHERE task_id = ?",
                        (task.task_id,))

                self._refresh_claim_for_task(task, now)
        pending = getattr(task, "pending_events", None)
        if pending is not None:
            pending.clear()

    def append_operation(self, task_id: str, action: str,
                         detail: str) -> None:
        """向 operation_log 追加一条关键操作记录（独立事务）。"""
        with self._lock:
            with self._transaction():
                self._conn.execute(
                    """
                    INSERT INTO operation_log (task_id, action, detail, ts)
                    VALUES (?, ?, ?, ?)
                    """,
                    (task_id, action, detail, _now()),
                )

    # -- 幂等与复用 --------------------------------------------------------
    def is_reusable_task(self, task: Task | None) -> bool:
        """成功复用必须：PASSED + 产物存在 + SHA-256 == final_hash。"""
        if task is None or task.status != TaskStatus.PASSED:
            return False
        if not task.output_path or not task.final_hash:
            return False
        try:
            from ...security.hashing import sha256_file
            return sha256_file(task.output_path) == task.final_hash
        except Exception:
            return False

    def _latest_task_for_key(self, source_hash: str, mode: str,
                             policy_version: str, config_hash: str,
                             tool_version: str) -> Task | None:
        row = self._conn.execute(
            """
            SELECT * FROM tasks
             WHERE source_hash = ? AND mode = ? AND policy_version = ?
               AND config_hash = ? AND tool_version = ?
             ORDER BY updated_at DESC, created_at DESC
             LIMIT 1
            """,
            (source_hash, mode, policy_version, config_hash, tool_version),
        ).fetchone()
        return self._row_to_task(row) if row else None

    def find_reusable(self, source_hash: str, mode: str, policy_version: str,
                      config_hash: str, tool_version: str, *,
                      exclude_task_id: str | None = None) -> Task | None:
        key = make_idempotency_key(source_hash, mode, policy_version,
                                   config_hash, tool_version)
        with self._lock:
            claim = self._conn.execute(
                "SELECT task_id FROM idempotency_claims WHERE idem_key = ?",
                (key,),
            ).fetchone()
            if claim is not None:
                task = self.load_task(claim["task_id"])
                if task is not None and not (
                        exclude_task_id and task.task_id == exclude_task_id):
                    return task if self.is_reusable_task(task) else None
            # 兼容单文件路径/迁移前任务：尚无 claim 时按完整键回退任务表
            latest = self._latest_task_for_key(
                source_hash, mode, policy_version, config_hash, tool_version)
            if latest is None or (
                    exclude_task_id and latest.task_id == exclude_task_id):
                return None
            return latest if self.is_reusable_task(latest) else None

    def claim_or_get(self, task: Task, *, force: bool = False) -> ClaimResult:
        """事务化幂等声明：同一键只有一个 created；force 产生新尝试。"""
        key = self._task_key(task)
        now = _now()
        with self._lock:
            with self._transaction():
                claim = self._conn.execute(
                    "SELECT * FROM idempotency_claims WHERE idem_key = ?",
                    (key,),
                ).fetchone()
                if claim is None:
                    # 兼容尚无 claim 的历史/单文件任务：先查完整键，避免重复成功
                    latest = self._latest_task_for_key(
                        task.source_hash, task.mode, task.policy_version,
                        getattr(task, "config_hash", "") or "",
                        getattr(task, "tool_version", "") or "")
                    if latest is not None and self.is_reusable_task(latest):
                        self._upsert_claim(key, latest, now)
                        return ClaimResult(task=latest, outcome="reused", idem_key=key)
                    if latest is not None and latest.status == TaskStatus.PENDING:
                        # 恢复已释放 claim 的 PENDING 任务应可被重新认领
                        self._upsert_claim(key, latest, now)
                        return ClaimResult(task=latest, outcome="created", idem_key=key)
                    if latest is not None and not is_terminal(latest.status):
                        self._upsert_claim(key, latest, now)
                        return ClaimResult(
                            task=latest, outcome="in_progress", idem_key=key,
                            reason="同键任务尚未终结，等待/跳过，不重复处理")
                    self._upsert_task_row(task, now)
                    self._upsert_claim(key, task, now)
                    return ClaimResult(task=task, outcome="created", idem_key=key)

                claimed = self.load_task(claim["task_id"])
                if not force and claimed is not None and self.is_reusable_task(claimed):
                    return ClaimResult(task=claimed, outcome="reused", idem_key=key)
                if not force and claimed is not None and not is_terminal(claimed.status):
                    return ClaimResult(
                        task=claimed, outcome="in_progress", idem_key=key,
                        reason="同键任务尚未终结，等待/跳过，不重复处理")

                # force，或旧声明指向的产物/任务已不可安全复用：新建尝试
                self._upsert_task_row(task, now)
                self._upsert_claim(key, task, now)
                outcome = "forced_created" if force else "stale_recreated"
                reason = "" if force else "旧产物缺失、被篡改或任务非成功终态"
                return ClaimResult(task=task, outcome=outcome,
                                   idem_key=key, reason=reason)

    # -- 读取 --------------------------------------------------------------
    def _row_to_task(self, row: sqlite3.Row) -> Task:
        status = _coerce_status(row["status"])
        out = row["output_path"]
        task = Task(
            source_path=Path(row["source_path"]),
            task_id=row["task_id"],
            file_type=row["file_type"],
            status=status,
            source_hash=row["source_hash"] or "",
            output_path=Path(out) if out else None,
            final_hash=row["final_hash"] or "",
            error=row["error"] or "",
            mode=row["mode"] or "",
            policy_version=row["policy_version"] or "",
            config_hash=row["config_hash"] or "",
            tool_version=row["tool_version"] or "",
            recognized_count=row["recognized_count"] or 0,
            redacted_count=row["redacted_count"] or 0,
            deleted_count=row["deleted_count"] or 0,
            updated_at=row["updated_at"] or "",
        )

        items = self._conn.execute(
            "SELECT * FROM task_items WHERE task_id = ? ORDER BY id",
            (task.task_id,),
        ).fetchall()
        try:
            review_flags = json.loads(row["review_flags_json"] or "[]")
            if not isinstance(review_flags, list):
                review_flags = ["review_flags_corrupt"]
        except (ValueError, TypeError):
            review_flags = ["review_flags_corrupt"]
        task.scan = ScanResult(review_flags=[str(v) for v in review_flags], candidates=[
            Candidate(
                text=item["value_masked"],
                category=item["category"],
                location=item["location"] or "",
                candidate_id=(item["candidate_id"]
                              or f"legacy-{item['id']}"),
                masked_value=item["value_masked"],
                original_fingerprint=item["original_fingerprint"] or "",
                confidence=float(item["confidence"] or 0.0),
                risk_level=_coerce_risk(item["risk"]),
                decision=_coerce_decision(item["decision"]),
                suggested_action=item["suggested_action"] or "",
                final_action=item["final_action"] or "",
                source=item["source"] or "",
                confirmed=bool(item["confirmed"]),
                excluded=bool(item["excluded"]),
                replacement=item["replacement_value"] or "",
                status=item["status"],
                failure_reason=item["failure_reason"],
                review_actor=item["review_actor"] or "",
                reviewed_at=item["reviewed_at"] or "",
                review_reason_code=item["review_reason_code"] or "",
            )
            for item in items
        ])

        findings = self._conn.execute(
            "SELECT * FROM audit_findings WHERE task_id = ? ORDER BY id",
            (task.task_id,),
        ).fetchall()
        if findings:
            task.audit = AuditReport(
                passed=(status == TaskStatus.PASSED),
                findings=[
                    AuditFinding(
                        verdict=_coerce_verdict(f["verdict"]),
                        detail=f["detail"],
                        scope=f["scope"],
                    )
                    for f in findings
                ],
            )
        return task

    def load_task(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,),
            ).fetchone()
            return self._row_to_task(row) if row else None

    def list_tasks(self, status: TaskStatus | None = None) -> list[Task]:
        """按状态 SQL 过滤；未知状态已在 initialize 阶段持久化归一。"""
        with self._lock:
            if status is None:
                rows = self._conn.execute(
                    "SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY updated_at DESC",
                    (status.name,),
                ).fetchall()
            return [self._row_to_task(r) for r in rows]

    def list_open(self) -> list[Task]:
        names = [s.name for s in OPEN_STATUSES]
        placeholders = ",".join("?" for _ in names)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tasks WHERE status IN ({placeholders}) "
                f"ORDER BY updated_at DESC",
                names,
            ).fetchall()
            return [self._row_to_task(r) for r in rows]

    def verify_event_chain(self, task_id: str | None = None) -> EventChainReport:
        """本地校验事件哈希链；返回是否有效及第一个异常序号。"""
        with self._lock:
            return verify_event_chain_conn(self._conn, task_id)

    def list_events(self, task_id: str | None = None) -> list[dict]:
        with self._lock:
            if task_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM task_events ORDER BY global_seq").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM task_events WHERE task_id = ? "
                    "ORDER BY global_seq", (task_id,)).fetchall()
            return [dict(row) for row in rows]

    def list_claims(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM idempotency_claims ORDER BY updated_at DESC").fetchall()
            return [dict(row) for row in rows]

    def find_by_source(self, source_hash: str, mode: str,
                       policy_version: str,
                       config_hash: str | None = None,
                       tool_version: str | None = None) -> Task | None:
        """兼容旧键；提供 config_hash/tool_version 时走 idempotency_claims。"""
        if config_hash is not None or tool_version is not None:
            return self.find_reusable(
                source_hash, mode, policy_version,
                config_hash or "", tool_version or "")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE source_hash = ? AND mode = ? AND policy_version = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (source_hash, mode, policy_version),
            ).fetchone()
            return self._row_to_task(row) if row else None
