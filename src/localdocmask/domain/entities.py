"""领域实体：候选、扫描结果、审计发现/报告、任务。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .enums import AuditVerdict, EntityDecision, ItemStatus, RiskLevel, TaskStatus

# 重新导出：实体的默认值直接引用枚举，同时保持从本模块也可导入
__all__ = [
    "Candidate", "ScanResult", "AuditFinding", "AuditReport", "Task", "ClaimResult",
    "TaskEvent", "EventChainReport", "TaskStatus", "ItemStatus", "AuditVerdict",
]


@dataclass
class Candidate:
    text: str
    category: str
    location: str = ""
    # T7：稳定候选 ID 与脱敏显示值；原文 text 仅保留在内存。
    candidate_id: str = ""
    masked_value: str = ""
    original_fingerprint: str = ""
    # 融合/校准结果（ADR-002/003）：校准后置信度、原始置信度、风险等级、三档处置与理由
    # 仅以值落库，原文不落库
    confidence: float = 1.0
    raw_confidence: float = 0.0
    risk_level: RiskLevel = RiskLevel.MEDIUM
    decision: EntityDecision = EntityDecision.NEEDS_REVIEW
    suggested_action: str = ""
    final_action: str = ""
    decision_reason: str = ""
    source: str = "regex"
    confirmed: bool = True
    excluded: bool = False
    replacement: str = ""
    page: int = -1
    box: list | None = None  # [x0, y0, x1, y1]，PDF 中为 PDF 点坐标，图片中为像素坐标
    image_part: str = ""  # DOCX 嵌入图像部件名
    # 条目级处理状态
    status: str = ItemStatus.DETECTED.value
    failure_reason: str = ""
    deletion_verified: bool = False
    delete: bool = False
    # T7：复核审计元信息，不保存候选原文。
    review_actor: str = ""
    reviewed_at: str = ""
    review_reason_code: str = ""


@dataclass
class ScanResult:
    candidates: list[Candidate] = field(default_factory=list)
    text: str = ""
    warnings: list[str] = field(default_factory=list)
    # 需路由到人工复核的信号（如 PDF 图文混排页无法 OCR 嵌入图像）。
    # 自动安全批次据此把整文件挂起复核、不自动导出。
    review_flags: list[str] = field(default_factory=list)


@dataclass
class AuditFinding:
    verdict: AuditVerdict
    detail: str
    scope: str = ""  # text/ocr/metadata/hidden；追加字段，保持向后兼容


@dataclass
class AuditReport:
    passed: bool = False
    findings: list[AuditFinding] = field(default_factory=list)
    coverage: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.findings:
            return "无审计记录"
        return "；".join(f"{f.verdict.value}：{f.detail}" for f in self.findings)


@dataclass
class Task:
    source_path: Path
    task_id: str = ""
    file_type: str = ""
    status: TaskStatus = TaskStatus.PENDING
    source_hash: str = ""
    output_path: Path | None = None
    final_hash: str = ""
    error: str = ""
    scan: ScanResult | None = None
    audit: AuditReport | None = None
    report_path: Path | None = None
    records: list[dict] = field(default_factory=list)
    recognized_count: int = 0
    redacted_count: int = 0
    deleted_count: int = 0
    # 幂等键（ADR-005）：处理模式 + 策略版本；追加字段，保持向后兼容
    mode: str = ""
    policy_version: str = ""
    # T2 幂等键：影响处理结果的配置哈希 + 工具版本
    config_hash: str = ""
    tool_version: str = ""
    # 待落盘事件（如 prepare 阶段）；由仓储在首次 save_task 时原子写入
    pending_events: list["TaskEvent"] = field(default_factory=list, repr=False)
    # 最近更新时间（ISO8601 UTC，来自仓储；界面任务中心展示用）
    updated_at: str = ""


@dataclass
class ClaimResult:
    """幂等声明结果（T2）。

    outcome:
    - created: 本次新建并持有处理权
    - reused: 命中有效成功产物，调用方应直接复用
    - in_progress: 同键任务尚未终结，调用方应等待/跳过
    - stale_recreated: 旧成功产物缺失/被篡改，已新建尝试
    - forced_created: force 明确要求的新尝试
    """

    task: Task
    outcome: str
    idem_key: str = ""
    reason: str = ""


@dataclass
class TaskEvent:
    """待写入的事件描述；event_id/seq/hash 由仓储计算并绑定任务/文件哈希。"""

    event_code: str
    stage: str = ""
    from_status: str = ""
    to_status: str = ""
    error_code: str = ""
    detail: str = ""
    actor: str = ""
    occurred_at: str = ""
    duration_ms: int = 0


@dataclass
class EventChainReport:
    valid: bool
    checked: int = 0
    first_bad_seq: int | None = None
    reason: str = ""
