# -*- coding: utf-8 -*-
"""受控临时文件清单、TTL 清理与清理状态追踪（T6）。"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..domain.entities import TaskEvent
from ..domain.exceptions import ErrorCode, LocalDocMaskError
from ..domain.state_machine import current_actor
from .path_safety import is_within

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TempLifecycleError(LocalDocMaskError):
    code = ErrorCode.PERSISTENCE_ERROR


@dataclass
class CleanupReport:
    scanned: int = 0
    cleaned: int = 0
    kept_active: int = 0
    kept_fresh: int = 0
    failures: int = 0
    events_written: int = 0
    failure_codes: list[str] = field(default_factory=list)


class TempLifecycle:
    """按任务登记临时产物，并提供仅限 processing 根的 TTL 清理。"""

    def __init__(self, processing_root: str | Path, manifest_path: str | Path):
        root = Path(processing_root)
        raw = str(root)
        if raw.startswith("\\\\") or raw.startswith("//") or raw.startswith("\\\\?\\"):

            raise TempLifecycleError("拒绝 UNC/设备临时根目录")
        if not root.is_absolute():
            raise TempLifecycleError("临时根目录必须是绝对受控路径")
        if root.parent == root:
            raise TempLifecycleError("拒绝将文件系统根目录作为临时根目录")
        root.mkdir(parents=True, exist_ok=True)
        stat = os.lstat(root)
        if root.is_symlink() or (
            getattr(stat, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise TempLifecycleError("临时根目录不得是符号链接或重解析点")
        self.root = root.resolve()
        self.manifest_path = Path(manifest_path)
        if not self.manifest_path.is_absolute():
            raise TempLifecycleError("临时清单路径必须是绝对路径")
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def register(self, task_id: str, path: str | Path, stage: str) -> dict:
        self._validate_task_id(task_id)
        safe = self._safe_artifact(path)
        return self._append({
            "record_type": "artifact",
            "task_id": task_id,
            "stage": self._safe_code(stage, "unknown"),
            "relative_path": safe.relative_to(self.root).as_posix(),
            "status": "active",
            "created_at": self._now(),
        })

    def mark_path(self, task_id: str, path: str | Path, status: str) -> dict:
        self._validate_task_id(task_id)
        safe = self._safe_artifact(path)
        return self._append({
            "record_type": "artifact",
            "task_id": task_id,
            "stage": "cleanup",
            "relative_path": safe.relative_to(self.root).as_posix(),
            "status": self._safe_code(status, "unknown"),
            "created_at": self._now(),
        })

    def mark_task_artifacts(self, task_id: str, status: str) -> int:
        self._validate_task_id(task_id)
        records = self.active_records()
        count = 0
        for rel_path, record in records.items():
            if record.get("task_id") != task_id:
                continue
            self._append({
                "record_type": "artifact",
                "task_id": task_id,
                "stage": "cleanup",
                "relative_path": rel_path,
                "status": self._safe_code(status, "unknown"),
                "created_at": self._now(),
            })
            count += 1
        return count

    def active_records(self) -> dict[str, dict]:
        latest: dict[str, dict] = {}
        for record in self._read_records():
            rel_path = record.get("relative_path")
            if not isinstance(rel_path, str) or not rel_path:
                continue
            status = record.get("status")
            if status == "active":
                latest[rel_path] = record
            elif status in {"cleaned", "quarantined", "ttl_cleaned", "moved"}:
                latest.pop(rel_path, None)
        return latest

    def cleanup_expired(self, repository, ttl_seconds: float,
                        *, now: float | None = None) -> CleanupReport:
        if float(ttl_seconds) <= 0:
            raise TempLifecycleError("TTL 必须大于 0")
        now_value = time.time() if now is None else float(now)
        cutoff = now_value - float(ttl_seconds)
        open_tasks = repository.list_open()
        active_ids = {task.task_id for task in open_tasks}
        all_task_ids = {task.task_id for task in repository.list_tasks()}
        records = self.active_records()
        report = CleanupReport()

        for path in self._iter_files():
            report.scanned += 1
            rel_path = path.relative_to(self.root).as_posix()
            record = records.get(rel_path, {})
            task_id = str(record.get("task_id") or "")
            if not task_id:
                task_id = next((tid for tid in all_task_ids if tid in path.name), "")
            if task_id and task_id in active_ids:
                report.kept_active += 1
                continue
            try:
                stat = os.lstat(path)
                if path.is_symlink() or (
                    getattr(stat, "st_file_attributes", 0)
                    & _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise TempLifecycleError("unsafe_temp_path")
                reference_ts = float(stat.st_mtime)
                created_at = record.get("created_at")
                if created_at:
                    try:
                        reference_ts = max(
                            reference_ts,
                            datetime.fromisoformat(str(created_at)).timestamp(),
                        )
                    except ValueError:
                        pass
                if reference_ts >= cutoff:
                    report.kept_fresh += 1
                    continue
                path.unlink()
                report.cleaned += 1
                if task_id:
                    self.mark_path(task_id, path, "ttl_cleaned")
                    self._record_recovery_event(repository, task_id, report)
                else:
                    repository.append_operation(
                        "*", "recovery.temp_ttl_cleaned",
                        "count=1;reason=ttl")
                    report.events_written += 1
            except Exception as exc:
                report.failures += 1
                report.failure_codes.append(type(exc).__name__)
        if report.failures:
            error = TempLifecycleError(
                f"临时文件清理失败：{report.failures} 个；"
                f"已清理 {report.cleaned} 个"
            )
            error.report = report
            raise error
        return report

    def _record_recovery_event(self, repository, task_id: str,
                               report: CleanupReport) -> None:
        task = repository.load_task(task_id)
        if task is None:
            repository.append_operation(
                "*", "recovery.temp_ttl_cleaned", "count=1;reason=ttl")
            report.events_written += 1
            return
        status = task.status.name
        repository.save_task(task, events=[TaskEvent(
            event_code="recovery.temp_ttl_cleaned", stage="recover",
            from_status=status, to_status=status, actor=current_actor(),
            detail="count=1;reason=ttl")])
        report.events_written += 1

    def _iter_files(self):
        for root, dirs, files in os.walk(self.root, topdown=True, followlinks=False):
            safe_dirs = []
            for name in dirs:
                if name == ".git":
                    continue
                candidate = Path(root) / name
                if candidate.is_symlink():
                    yield candidate
                else:
                    safe_dirs.append(name)
            dirs[:] = safe_dirs
            for name in files:
                candidate = Path(root) / name
                try:
                    resolved = candidate.resolve(strict=False)
                except OSError:
                    resolved = candidate
                if not is_within(resolved, [self.root]):
                    yield candidate
                    continue
                if candidate == self.manifest_path:
                    continue
                yield candidate

    def _safe_artifact(self, path: str | Path) -> Path:
        candidate = Path(path)
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise TempLifecycleError("临时路径无法解析") from exc
        if not is_within(resolved, [self.root]) or resolved == self.root:
            raise TempLifecycleError("临时文件路径越界")
        return resolved

    def _append(self, record: dict) -> dict:
        payload = {"manifest_version": 1, **record}
        with self._lock:
            with open(self.manifest_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False,
                                        sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return payload

    def _read_records(self) -> list[dict]:
        if not self.manifest_path.exists():
            return []
        records: list[dict] = []
        with self._lock:
            for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
        return records

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not task_id or _TASK_ID_RE.fullmatch(str(task_id)) is None:
            raise TempLifecycleError("任务 ID 不符合受控格式")

    @staticmethod
    def _safe_code(value: str, default: str) -> str:
        text = "".join(ch for ch in str(value or "")
                       if ch.isascii() and (ch.isalnum() or ch in "_.-"))
        return text[:64] or default

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
