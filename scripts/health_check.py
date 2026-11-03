# -*- coding: utf-8 -*-
"""LocalDocMask 启动与健康验收（隔离数据根，不访问外部网络）。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True,
                        help="隔离测试数据根；不会使用项目 data/")
    parser.add_argument("--json-output", default="")
    parser.add_argument("--create-diagnostic", action="store_true")
    args = parser.parse_args()

    data_root = Path(args.data_dir).resolve()
    project_data = (ROOT / "data").resolve()
    if data_root == project_data:
        print("error: refuse_to_use_project_data_directory", file=sys.stderr)
        return 2
    data_root.mkdir(parents=True, exist_ok=True)
    os.environ["LOCALDOCMASK_HOME"] = str(data_root)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from localdocmask import __version__
    from localdocmask.application.composition import build_services
    from localdocmask.infrastructure.persistence.sqlite_repo import SCHEMA_VERSION

    services = None
    try:
        services = build_services()
        report = services.health.check().as_dict()
        db_path = data_root / "tasks.db"
        db_check = "missing"
        event_chain = False
        if db_path.is_file():
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            try:
                db_check = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                conn.close()
            event_chain = bool(services.repository.verify_event_chain().valid)

        required_dirs = [
            "input", "processing", "output/success", "output/quarantine",
            "logs", "state/metrics", "output/diagnostics",
        ]
        missing_dirs = [name for name in required_dirs
                        if not (data_root / name).is_dir()]
        diagnostic = None
        if args.create_diagnostic:
            package = services.diagnostics.create()
            diagnostic = {
                "created": True,
                "sha256": sha256_file(package),
                "size": package.stat().st_size,
            }

        payload = {
            "status": report.get("status"),
            "application_version": __version__,
            "database_schema_version": int(SCHEMA_VERSION),
            "config_schema_version": int(services.settings.schema_version),
            "config_source": services.config_state.source,
            "config_degraded": bool(services.config_state.degraded),
            "components": report.get("components") or {},
            "database_integrity": db_check,
            "event_chain_valid": event_chain,
            "missing_directories": missing_dirs,
            "diagnostic": diagnostic,
            "external_network_used": False,
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        if args.json_output:
            Path(args.json_output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_output).write_text(text + "\n", encoding="utf-8")
        print(text)
        if report.get("status") not in ("healthy", "degraded"):
            return 2
        if db_check != "ok" or missing_dirs or not event_chain:
            return 2
        return 0
    finally:
        if services is not None:
            services.close()


if __name__ == "__main__":
    raise SystemExit(main())
