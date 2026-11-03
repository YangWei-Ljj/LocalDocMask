# -*- coding: utf-8 -*-
"""T9 合成数据库备份、恢复与 v3→v4 升级演练（不含二进制回滚）。"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from localdocmask.domain.exceptions import LocalDocMaskError  # noqa: E402
from localdocmask.infrastructure.persistence.sqlite_repo import (  # noqa: E402
    SCHEMA_VERSION,
    backup_database,
    initialize_database,
    restore_database,
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def db_state(path: Path) -> dict:
    conn = sqlite3.connect(str(path))
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        task_count = int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        marker = conn.execute(
            "SELECT source_path FROM tasks WHERE task_id='synthetic-t9'").fetchone()
    finally:
        conn.close()
    return {
        "schema_version": version,
        "integrity": integrity,
        "task_count": task_count,
        "synthetic_marker_present": bool(marker),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    build_root = ROOT / "build"
    build_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="localdocmask_t9_db_", dir=build_root) as temp:
        work = Path(temp)
        db = work / "tasks.db"
        initialize_database(db, schema_version=3)
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT INTO tasks (task_id, source_path, file_type, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("synthetic-t9", "synthetic-input.docx", "docx", "NEEDS_REVIEW",
                 "2026-10-30T00:00:00+00:00", "2026-10-30T00:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO task_items (task_id, category, value_masked, risk, decision, status, failure_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("synthetic-t9", "姓名", "测***", "high", "", "detected", ""),
            )
            conn.commit()
        finally:
            conn.close()

        success = work / "output" / "success" / "synthetic-success.docx"
        quarantine = work / "output" / "quarantine" / "synthetic-quarantine.docx"
        success.parent.mkdir(parents=True)
        quarantine.parent.mkdir(parents=True)
        success.write_bytes(b"synthetic-success-sentinel")
        quarantine.write_bytes(b"synthetic-quarantine-sentinel")
        sentinel_hashes = {"success": sha256(success), "quarantine": sha256(quarantine)}

        pre_upgrade = backup_database(db, work / "tasks-v3-before-upgrade.db")
        pre_state = db_state(pre_upgrade)
        migration_backup = initialize_database(db, schema_version=SCHEMA_VERSION)
        upgraded_state = db_state(db)
        migration_backup_state = db_state(migration_backup)

        rollback_db = work / "tasks-rollback-v3.db"
        restore_database(pre_upgrade, rollback_db)
        rollback_state = db_state(rollback_db)

        corrupt = work / "corrupt-backup.db"
        corrupt.write_bytes(b"not-a-sqlite-database")
        before_hash = sha256(db)
        restore_refused = False
        try:
            restore_database(corrupt, db)
        except Exception:
            restore_refused = True
        after_hash = sha256(db)

        future_db = work / "tasks-future-schema.db"
        shutil.copy2(db, future_db)
        conn = sqlite3.connect(str(future_db))
        try:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            conn.commit()
        finally:
            conn.close()
        future_refused = False
        try:
            initialize_database(future_db)
        except LocalDocMaskError:
            future_refused = True

        success_now = sha256(success)
        quarantine_now = sha256(quarantine)
        payload = {
            "status": "passed",
            "scope": "database-only",
            "pre_upgrade": pre_state,
            "upgrade": {
                "from_schema_version": pre_state["schema_version"],
                "to_schema_version": upgraded_state["schema_version"],
                "automatic_backup_present": migration_backup is not None,
                "automatic_backup_integrity": migration_backup_state["integrity"],
                "task_preserved": upgraded_state["synthetic_marker_present"],
                "integrity": upgraded_state["integrity"],
            },
            "database_restore": {
                "restored_schema_version": rollback_state["schema_version"],
                "integrity": rollback_state["integrity"],
                "task_preserved": rollback_state["synthetic_marker_present"],
                "note": "仅验证 v3 数据库备份恢复；不执行或验证旧版二进制。",
            },
            "restore_failure_safety": {
                "corrupt_backup_rejected": restore_refused,
                "current_database_unchanged": before_hash == after_hash,
            },
            "future_schema_guard": {
                "version": SCHEMA_VERSION + 1,
                "rejected": future_refused,
            },
            "success_quarantine_preserved": {
                "success": success_now == sentinel_hashes["success"],
                "quarantine": quarantine_now == sentinel_hashes["quarantine"],
            },
        }
        if not all([
            payload["upgrade"]["automatic_backup_present"],
            payload["upgrade"]["task_preserved"],
            payload["database_restore"]["task_preserved"],
            payload["restore_failure_safety"]["corrupt_backup_rejected"],
            payload["restore_failure_safety"]["current_database_unchanged"],
            payload["future_schema_guard"]["rejected"],
            all(payload["success_quarantine_preserved"].values()),
        ]):
            payload["status"] = "failed"
        output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                          encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
