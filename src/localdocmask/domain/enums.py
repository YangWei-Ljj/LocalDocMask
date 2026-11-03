"""领域枚举：任务状态、条目状态、审计结论。"""
from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "尚未处理"
    PROCESSING = "正在处理"
    NEEDS_REVIEW = "等待人工复核"
    PROCESSED_UNAUDITED = "处理完成但未审计"
    AUDIT_FAILED = "审计失败"
    UNSAFE = "无法确认安全"
    PASSED = "审计通过，可以导出"
    INTERRUPTED = "已中断"  # 崩溃恢复：处理中断且未自动认定安全（ADR-005）
    CANCELLED = "已取消"
    TIMED_OUT = "已超时"


class ItemStatus(str, Enum):
    DETECTED = "detected"
    REDACTED = "redacted"
    AUDIT_PASSED = "audit_passed"
    AUDIT_FAILED = "audit_failed"
    DELETION_PENDING = "deletion_pending"
    DELETED = "deleted"
    DELETION_VERIFIED = "deletion_verified"
    FAILED = "failed"
    UNRESOLVED = "unresolved"


class AuditVerdict(str, Enum):
    RESIDUAL_FOUND = "发现残留"
    NO_RESIDUAL = "未发现残留"
    UNCHECKABLE = "无法检查"
    INSUFFICIENT = "覆盖不足"
    NEEDS_CONFIRM = "需要人工确认"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EntityDecision(str, Enum):
    AUTO_CONFIRM = "auto_confirm"
    NEEDS_REVIEW = "needs_review"
    SUPPRESS = "suppress"
    CONFIRMED = "confirmed"      # 用户确认
    DISMISSED = "dismissed"      # 用户放行
