# -*- coding: utf-8 -*-
"""本地脱敏诊断包生成（T6）：只读本地状态，不执行上传。"""
from __future__ import annotations

import json
import os
import uuid
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__
from ..domain.exceptions import ErrorCode, LocalDocMaskError
from ..infrastructure.logging.logger import sanitize_diagnostic_text
from .path_safety import safe_join


class DiagnosticError(LocalDocMaskError):
    code = ErrorCode.PERSISTENCE_ERROR


class DiagnosticService:
    def __init__(self, *, settings, config_state, health, repository,
                 metrics, log_dir: str | Path, diagnostics_dir: str | Path):
        self.settings = settings
        self.config_state = config_state
        self.health = health
        self.repository = repository
        self.metrics = metrics
        self.log_dir = Path(log_dir)
        self.diagnostics_dir = Path(diagnostics_dir)

    def create(self) -> Path:
        try:
            from ..infrastructure.persistence.sqlite_repo import SCHEMA_VERSION
            generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            health = self.health.check().as_dict()
            chain = self.repository.verify_event_chain()
            chain_payload = {
                "valid": bool(chain.valid),
                "checked": int(chain.checked),
                "first_bad_seq": chain.first_bad_seq,
                "reason": sanitize_diagnostic_text(chain.reason or ""),
            }
            payloads = {
                "manifest.json": {
                    "bundle_version": 1,
                    "generated_at": generated_at,
                    "application_version": __version__,
                    "tool_version": __version__,
                    "schema_version": int(SCHEMA_VERSION),
                    "config_schema_version": int(self.settings.schema_version),
                },
                "config_summary.json": self._config_summary(),
                "health.json": health,
                "metrics_summary.json": self.metrics.snapshot(),
                "task_summary.json": self._task_summary(),
                "event_chain.json": chain_payload,
            }
            logs = self._log_summary()
            self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
            name = (f"localdocmask_diagnostic_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
                    f"{uuid.uuid4().hex[:8]}.zip")
            target = safe_join(self.diagnostics_dir, name)
            temp = safe_join(self.diagnostics_dir, f".{name}.tmp")
            try:
                with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for member, payload in payloads.items():
                        zf.writestr(
                            member,
                            json.dumps(payload, ensure_ascii=False,
                                       indent=2, sort_keys=True),
                        )
                    zf.writestr(
                        "logs_summary.jsonl",
                        "\n".join(json.dumps(item, ensure_ascii=False,
                                             sort_keys=True)
                                  for item in logs) + ("\n" if logs else ""),
                    )
                with open(temp, "rb+") as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, target)
                return target
            finally:
                if temp.exists():
                    try:
                        temp.unlink()
                    except Exception:
                        pass
        except Exception as exc:
            if isinstance(exc, DiagnosticError):
                raise
            raise DiagnosticError(
                f"诊断包生成失败：{type(exc).__name__}") from exc

    def _config_summary(self) -> dict:
        s = self.settings
        return {
            "schema_version": int(s.schema_version),
            "config_hash": self.config_state.config_hash,
            "source": self.config_state.source,
            "degraded": bool(self.config_state.degraded),
            "warning_code": self.config_state.warning_code,
            "masking_mode": s.masking.mode,
            "mapping_persistence": s.masking.mapping_persistence,
            "recognition_review_mode": s.recognition.review_mode,
            "ner_enabled": bool(s.ner.enabled),
            "ollama_enabled": bool(s.ollama.enabled),
            "limits": {
                "max_workers": int(s.security.max_workers),
                "max_queue_depth": int(s.security.max_queue_depth),
                "max_file_size_mb": float(s.security.max_file_size_mb),
                "max_pdf_pages": int(s.security.max_pdf_pages),
                "max_image_pixels": int(s.security.max_image_pixels),
                "max_zip_entries": int(s.security.max_zip_entries),
                "tempfile_retention_days": float(
                    s.security.tempfile_retention_days),
            },
        }

    def _task_summary(self) -> dict:
        counts: dict[str, int] = {}
        for task in self.repository.list_tasks():
            name = getattr(task.status, "name", str(task.status))
            counts[name] = counts.get(name, 0) + 1
        return {"status_counts": counts, "total": sum(counts.values())}

    def _log_summary(self) -> list[dict]:
        path = self.log_dir / "localdocmask.log"
        if not path.exists():
            return []
        max_lines = max(1, int(self.settings.observability.diagnostics_log_lines))
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = list(deque(handle, maxlen=max_lines))
        output = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                item = {"level": "UNKNOWN", "logger": "unknown",
                        "message": line.strip()}
            output.append({
                "time": sanitize_diagnostic_text(str(item.get("time") or "")),
                "level": sanitize_diagnostic_text(str(item.get("level") or "")),
                "logger": sanitize_diagnostic_text(str(item.get("logger") or "")),
                "event": sanitize_diagnostic_text(str(item.get("event") or "")),
                "message": sanitize_diagnostic_text(str(item.get("message") or "")),
            })
        return output
