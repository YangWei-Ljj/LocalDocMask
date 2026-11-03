# -*- coding: utf-8 -*-
"""LocalDocMask SQLite 维护 CLI：验证、备份、恢复和显式迁移。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from localdocmask.infrastructure.persistence.sqlite_repo import (  # noqa: E402
    SCHEMA_VERSION,
    backup_database,
    initialize_database,
    restore_database,
)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _file_info(path: Path | None) -> dict | None:
    if path is None:
        return None
    if not path.is_file():
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "size": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def inspect_database(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = sorted(row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        task_count = (int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
                      if "tasks" in tables else 0)
    finally:
        conn.close()
    return {
        "path": str(path),
        "integrity": integrity,
        "schema_version": version,
        "supported_schema_version": int(SCHEMA_VERSION),
        "compatible": version <= int(SCHEMA_VERSION),
        "tables": tables,
        "task_count": task_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="LocalDocMask 数据库维护")
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify")
    verify.add_argument("--db", required=True)

    backup = sub.add_parser("backup")
    backup.add_argument("--db", required=True)
    backup.add_argument("--output", required=True)

    restore = sub.add_parser("restore")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--db", required=True)
    restore.add_argument("--pre-restore-backup", default="")

    migrate = sub.add_parser("migrate")
    migrate.add_argument("--db", required=True)
    migrate.add_argument("--to", type=int, default=SCHEMA_VERSION)

    args = parser.parse_args()
    try:
        if args.command == "verify":
            result = {"status": "ok", "database": inspect_database(Path(args.db))}
        elif args.command == "backup":
            target = backup_database(Path(args.db), Path(args.output))
            result = {"status": "ok", "backup": _file_info(target),
                      "verification": inspect_database(target)}
        elif args.command == "restore":
            db = Path(args.db)
            pre_path = (Path(args.pre_restore_backup) if args.pre_restore_backup
                        else db.with_name(f"{db.name}.pre-restore-{_stamp()}.bak"))
            pre_backup = None
            if db.is_file() and db.stat().st_size:
                pre_backup = backup_database(db, pre_path)
            restore_database(Path(args.backup), db)
            result = {
                "status": "ok",
                "restored": _file_info(db),
                "verification": inspect_database(db),
                "pre_restore_backup": _file_info(pre_backup),
            }
        elif args.command == "migrate":
            db = Path(args.db)
            before = inspect_database(db)
            backup = initialize_database(db, schema_version=int(args.to))
            after = inspect_database(db)
            result = {
                "status": "ok" if after["schema_version"] == int(args.to) else "failed",
                "before": before,
                "after": after,
                "migration_backup": _file_info(backup),
            }
        else:  # pragma: no cover
            raise ValueError(args.command)
        ok = result.get("status") == "ok"
        if result.get("database") and result["database"].get("integrity") != "ok":
            ok = False
        if result.get("verification") and result["verification"].get("integrity") != "ok":
            ok = False
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if ok else 1
    except Exception as exc:
        print(json.dumps({
            "status": "failed",
            "error_code": type(exc).__name__,
        }, ensure_ascii=False, sort_keys=True, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
