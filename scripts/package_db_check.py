# -*- coding: utf-8 -*-
"""Validate the packaged application's isolated SQLite smoke-test state."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--min-startups", type=int, default=0)
    args = parser.parse_args()
    db = Path(args.db)
    if not db.is_file():
        print(json.dumps({"status": "failed", "reason": "database_missing"}))
        return 1
    conn = sqlite3.connect(str(db), timeout=5.0)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        startups = int(conn.execute(
            "SELECT COUNT(*) FROM operation_log WHERE action='startup'").fetchone()[0])
    finally:
        conn.close()
    payload = {
        "status": "passed" if integrity == "ok" and startups >= args.min_startups else "failed",
        "integrity": integrity,
        "schema_version": version,
        "startup_events": startups,
        "minimum_expected_startups": args.min_startups,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
