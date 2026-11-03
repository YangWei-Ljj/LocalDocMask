"""结构化日志设施：JSON 文件 + 可读控制台，内置敏感值掩码过滤（ADR-008）。

- 文件输出结构化 JSON（时间 / 级别 / logger / task_id / event / 消息）；
- 控制台输出人读格式；
- 日志文件写入 settings.LOG_DIR（遵循 LOCALDOCMASK_HOME），按大小轮转并按份数保留；
- RedactionFilter 复用 security.redaction.masker.mask_display 对消息中的
  手机 / 身份证 / 邮箱 / 银行卡等高风险模式及已登记候选值做掩码，
  绝不写入完整原文、完整提示词或完整实体。
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ...recognition import patterns
from ...security.redaction.masker import mask_display

_SENSITIVE_PATTERNS = [
    ("身份证号", patterns.ID_CARD_RE),
    ("手机号", patterns.MOBILE_RE),
    ("固定电话", patterns.LANDLINE_RE),
    ("邮箱", patterns.EMAIL_RE),
    ("银行卡号", patterns.BANK_CARD_RE),
]

# 进程内登记的已知敏感值（如当次扫描候选原文），日志输出前统一掩码
_extra_sensitive: set[str] = set()

_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|"
    r"password|authorization)\s*[:=]\s*[^\s,;]+")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*")
_WINDOWS_PATH_PATTERN = re.compile(r"(?i)\b[A-Z]:\\[^\s\"'<>]+")
_UNC_PATH_PATTERN = re.compile(r"\\\\[^\s\"'<>]+")

_configured = False


def register_sensitive_values(values) -> None:
    """登记需要在日志中掩码的已知敏感值（原文）。仅内存保存，不落盘。"""
    for v in values or []:
        if isinstance(v, str) and v:
            _extra_sensitive.add(v)


def redact_sensitive_text(text: str) -> str:
    """公开的日志/诊断文本脱敏入口；供诊断包复用。"""
    return _redact_text(text)


def sanitize_diagnostic_text(text: str) -> str:
    """在 PII 掩码基础上清除密钥形态和完整本机路径。"""
    out = _redact_text(str(text or ""))
    out = _BEARER_PATTERN.sub("Bearer <redacted>", out)
    out = _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=<redacted>", out)
    out = _WINDOWS_PATH_PATTERN.sub("<path>", out)
    out = _UNC_PATH_PATTERN.sub("<path>", out)
    return out[:2000]


def _redact_text(text: str) -> str:
    if not text:
        return text
    for cat, rx in _SENSITIVE_PATTERNS:
        text = rx.sub(lambda m: mask_display(m.group(0), cat), text)
    for v in _extra_sensitive:
        text = text.replace(v, mask_display(v, "自定义敏感词"))
    text = _BEARER_PATTERN.sub("Bearer <redacted>", text)
    text = _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    return text


#: LogRecord 标准属性名；这些不参与 extra 敏感值扫描（避免误改正文/堆栈）
_RESERVED_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "process", "processName", "taskName",
})


class RedactionFilter(logging.Filter):
    """对 record 消息、参数与 extra 扩展属性中的敏感原文做掩码。

    历史缺陷：``logger.info("x", extra={"event": phone})`` 的 event 会被
    JsonFormatter 直接落盘而不经过 msg/args 掩码，造成完整原文泄漏。
    现对 record.__dict__ 中所有非保留字符串属性统一掩码。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # 先完整格式化，再统一脱敏，避免密钥正则误删格式占位符。
            rendered = record.getMessage()
            record.msg = _redact_text(rendered)
            record.args = ()
            # extra 扩展属性（event/task_id/业务字段等）同样掩码
            for key, value in list(record.__dict__.items()):
                if key in _RESERVED_ATTRS:
                    continue
                if isinstance(value, str):
                    record.__dict__[key] = _redact_text(value)
        except Exception:
            # 过滤失败绝不能影响主流程；保守放行（消息原样最多只是未掩码）
            pass
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "task_id": getattr(record, "task_id", None),
            "event": getattr(record, "event", None),
            "message": record.getMessage(),
        }
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return json.dumps({"level": record.levelname, "message": str(record.msg)},
                              ensure_ascii=False)


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{datetime.now().strftime('%H:%M:%S')} {record.levelname:<7} {record.name}: {record.getMessage()}"
        task_id = getattr(record, "task_id", None)
        if task_id:
            base += f" [task={task_id}]"
        return base


def _safe_log_dir(cfg) -> Path:
    candidate = getattr(cfg, "log_dir", None) if cfg is not None else None
    if candidate:
        return Path(candidate)
    # 延迟导入，避免循环依赖
    from ..config.settings import LOG_DIR
    return Path(LOG_DIR)


def configure_logging(cfg=None) -> None:
    """初始化日志。重复调用幂等；未配置或初始化失败均回退安全默认，不抛异常。"""
    global _configured
    if _configured:
        return
    try:
        level = getattr(cfg, "level", "INFO") or "INFO"
        log_dir = _safe_log_dir(cfg)
        max_bytes = int(getattr(cfg, "max_bytes", 10 * 1024 * 1024) or 10 * 1024 * 1024)
        backup_count = int(getattr(cfg, "backup_count", 30) or 30)
        log_dir.mkdir(parents=True, exist_ok=True)

        root = logging.getLogger("localdocmask")
        root.setLevel(level)
        root.handlers.clear()
        root.propagate = False

        redaction = RedactionFilter()

        file_handler = RotatingFileHandler(
            log_dir / "localdocmask.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(_JsonFormatter())
        file_handler.addFilter(redaction)
        root.addHandler(file_handler)

        console_handler = logging.StreamHandler(stream=sys.stdout)
        console_handler.setFormatter(_ConsoleFormatter())
        console_handler.addFilter(redaction)
        root.addHandler(console_handler)

        _configured = True
        root.info("日志系统初始化完成", extra={"event": "logging.init"})
    except Exception:
        # 安全失败：日志不可用不能阻塞应用启动
        pass


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"localdocmask.{name}")
