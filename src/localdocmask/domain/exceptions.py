"""统一异常体系与错误码（见 module_contracts §1.3）。

本阶段仅新增，不强制替换既有抛出点；新代码应优先抛出本体系异常。
"""
from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    FILE_NOT_FOUND = "file_not_found"
    UNSUPPORTED_FORMAT = "unsupported_format"
    PARSE_ERROR = "parse_error"
    OCR_ERROR = "ocr_error"
    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_TIMEOUT = "llm_timeout"
    RESIDUAL_FOUND = "residual_found"
    UNCHECKABLE = "uncheckable"
    MAPPING_COLLISION = "mapping_collision"
    CONFIG_INVALID = "config_invalid"
    PERSISTENCE_ERROR = "persistence_error"
    TASK_CANCELLED = "task_cancelled"
    TASK_TIMEOUT = "task_timeout"
    QUEUE_FULL = "queue_full"
    RETRY_EXHAUSTED = "retry_exhausted"
    SECURITY_VIOLATION = "security_violation"
    RESOURCE_LIMIT = "resource_limit"
    INVALID_TASK_TRANSITION = "invalid_task_transition"


class LocalDocMaskError(RuntimeError):
    """所有业务异常的基类，携带结构化错误码。"""

    code: ErrorCode = ErrorCode.SECURITY_VIOLATION

    def __init__(self, message: str, code: ErrorCode | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code

    def __str__(self) -> str:
        return f"[{self.code.value}] {super().__str__()}"


class ParseError(LocalDocMaskError):
    code = ErrorCode.PARSE_ERROR


class LLMError(LocalDocMaskError):
    code = ErrorCode.LLM_UNAVAILABLE


class SecurityViolation(LocalDocMaskError):
    code = ErrorCode.SECURITY_VIOLATION


class UncheckableError(LocalDocMaskError):
    code = ErrorCode.UNCHECKABLE


class MappingCollisionError(LocalDocMaskError):
    code = ErrorCode.MAPPING_COLLISION


class ConfigError(LocalDocMaskError):
    code = ErrorCode.CONFIG_INVALID


class InvalidTaskTransition(SecurityViolation):
    """非法任务状态转移（失败关闭，任务保持原状态）。"""

    code = ErrorCode.INVALID_TASK_TRANSITION

    def __init__(self, current, target, reason: str = ""):
        self.current = current
        self.target = target
        current_text = getattr(current, "value", str(current))
        target_text = getattr(target, "value", str(target))
        message = f"非法任务状态转移：{current_text} -> {target_text}"
        if reason:
            message += f"（{reason}）"
        super().__init__(message, code=ErrorCode.INVALID_TASK_TRANSITION)


class TaskCancelledError(LocalDocMaskError):
    code = ErrorCode.TASK_CANCELLED

    def __init__(self, stage: str = "", reason: str = "user_requested"):
        self.stage = stage
        self.reason = reason
        message = f"任务已取消（阶段={stage or 'unknown'}，原因={reason}）"
        super().__init__(message, code=ErrorCode.TASK_CANCELLED)


class TaskTimedOutError(SecurityViolation):
    code = ErrorCode.TASK_TIMEOUT

    def __init__(self, stage: str = "", scope: str = "phase", budget: float = 0.0):
        self.stage = stage
        self.scope = scope
        self.budget = float(budget)
        message = (f"任务超时（阶段={stage or 'unknown'}，"
                   f"范围={scope}，预算={self.budget:.3f}s）")
        super().__init__(message, code=ErrorCode.TASK_TIMEOUT)


class QueueFullError(LocalDocMaskError):
    code = ErrorCode.QUEUE_FULL

    def __init__(self, accepted: int, capacity: int):
        self.accepted = int(accepted)
        self.capacity = int(capacity)
        super().__init__(
            f"任务队列已满：已接受 {self.accepted}，上限 {self.capacity}",
            code=ErrorCode.QUEUE_FULL,
        )


class RetryExhaustedError(LocalDocMaskError):
    code = ErrorCode.RETRY_EXHAUSTED

    def __init__(self, attempts: int, last_error: Exception | None = None):
        self.attempts = int(attempts)
        self.last_error = last_error
        error_name = type(last_error).__name__ if last_error is not None else "unknown"
        super().__init__(
            f"重试耗尽：共 {self.attempts} 次尝试，最后错误 {error_name}",
            code=ErrorCode.RETRY_EXHAUSTED,
        )
