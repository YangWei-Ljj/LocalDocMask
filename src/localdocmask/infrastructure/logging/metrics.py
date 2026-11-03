# -*- coding: utf-8 -*-
"""本地 JSONL 指标：仅聚合计数/耗时，不记录任务正文、文件名或路径（T6）。"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from ...domain.exceptions import ErrorCode, LocalDocMaskError
from ...security.path_safety import safe_join

_METRIC_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9_.:-]{0,64}$")


class MetricsError(LocalDocMaskError):
    code = ErrorCode.PERSISTENCE_ERROR


class LocalMetrics:
    """轻量本地指标写入器；写入失败显式抛错，不伪造记录。"""

    def __init__(self, root: str | Path, filename: str = "metrics.jsonl",
                 max_bytes: int = 10 * 1024 * 1024, enabled: bool = True):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = safe_join(self.root, filename)
        self.max_bytes = max(1, int(max_bytes))
        self.enabled = bool(enabled)
        self._lock = threading.RLock()

    def record(self, metric: str, value: float = 1,
               *, stage: str = "", code: str = "", outcome: str = "") -> None:
        if not self.enabled:
            return
        if _METRIC_RE.fullmatch(metric or "") is None:
            raise MetricsError("指标名称不符合受控格式")
        for label in (stage, code, outcome):
            if label and _LABEL_RE.fullmatch(str(label)) is None:
                raise MetricsError("指标标签不符合受控格式")
        payload = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metric": metric,
            "value": float(value),
            "stage": stage,
            "code": code,
            "outcome": outcome,
        }
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            try:
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception as exc:
                raise MetricsError(
                    f"指标写入失败：{type(exc).__name__}") from exc

    def snapshot(self) -> dict:
        counters: dict[str, float] = {}
        stages: dict[str, dict[str, float]] = {}
        if not self.enabled or not self.path.exists():
            return {"counters": counters, "stages": stages}
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            metric = item.get("metric")
            value = item.get("value")
            if not isinstance(metric, str) or not isinstance(value, (int, float)):
                continue
            if metric == "stage_duration_ms":
                stage = str(item.get("stage") or "unknown")
                bucket = stages.setdefault(
                    stage, {"count": 0.0, "total_ms": 0.0, "max_ms": 0.0})
                bucket["count"] += 1
                bucket["total_ms"] += float(value)
                bucket["max_ms"] = max(bucket["max_ms"], float(value))
            else:
                counters[metric] = counters.get(metric, 0.0) + float(value)
        return {"counters": counters, "stages": stages}

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                backup = self.path.with_name(self.path.name + ".1")
                os.replace(self.path, backup)
        except OSError as exc:
            raise MetricsError("指标轮转失败") from exc
