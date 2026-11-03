"""数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class TaskStatus(str, Enum):
    PENDING = "尚未处理"
    PROCESSING = "正在处理"
    NEEDS_REVIEW = "等待人工复核"
    PROCESSED_UNAUDITED = "处理完成但未审计"
    AUDIT_FAILED = "审计失败"
    UNSAFE = "无法确认安全"
    PASSED = "审计通过，可以导出"


class AuditVerdict(str, Enum):
    RESIDUAL_FOUND = "发现残留"
    NO_RESIDUAL = "未发现残留"
    UNCHECKABLE = "无法检查"
    INSUFFICIENT = "覆盖不足"
    NEEDS_CONFIRM = "需要人工确认"


@dataclass
class Candidate:
    text: str
    category: str
    location: str = ""
    confidence: float = 1.0
    source: str = "regex"
    confirmed: bool = True
    excluded: bool = False
    replacement: str = ""
    page: int = -1
    box: list | None = None  # [x0, y0, x1, y1]，PDF 中为 PDF 点坐标，图片中为像素坐标


@dataclass
class ScanResult:
    candidates: list[Candidate] = field(default_factory=list)
    text: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class AuditFinding:
    verdict: AuditVerdict
    detail: str


@dataclass
class AuditReport:
    passed: bool = False
    findings: list[AuditFinding] = field(default_factory=list)

    def summary(self) -> str:
        if not self.findings:
            return "无审计记录"
        return "；".join(f"{f.verdict.value}：{f.detail}" for f in self.findings)


@dataclass
class Task:
    source_path: Path
    status: TaskStatus = TaskStatus.PENDING
    source_hash: str = ""
    output_path: Path | None = None
    error: str = ""
    scan: ScanResult | None = None
    audit: AuditReport | None = None
