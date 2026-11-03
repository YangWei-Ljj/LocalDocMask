"""资源上限守卫：并发上限 / 单文件大小上限 / 单任务超时（ADR-008 加固）。

原则：
- 上限全部可配置（settings.security），并有安全默认值；
- 超限在入口即被明确拒绝（SecurityViolation, code=resource_limit），
  不进入解析/处理链路，避免资源失控；
- 批处理 BatchService 天然顺序处理（单工作线程消费文件列表），并发上限
  主要约束 GUI 后台 QThreadPool；本模块只提供值与判据，不启动线程。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..domain.exceptions import ErrorCode, SecurityViolation

#: 安全默认值（settings 未提供时的回退）
DEFAULT_MAX_FILE_SIZE_MB = 50.0
DEFAULT_MAX_WORKERS = 2
DEFAULT_TASK_TIMEOUT_SECONDS = 1800.0
DEFAULT_ADMISSION_TIMEOUT_SECONDS = 60.0
DEFAULT_SCAN_TIMEOUT_SECONDS = 300.0
DEFAULT_PROCESS_TIMEOUT_SECONDS = 900.0
DEFAULT_AUDIT_TIMEOUT_SECONDS = 300.0
DEFAULT_FINALIZE_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_QUEUE_DEPTH = 32
DEFAULT_RETRY_MAX_ATTEMPTS = 2
DEFAULT_RETRY_BASE_DELAY_SECONDS = 0.1
DEFAULT_RETRY_MAX_DELAY_SECONDS = 1.0
DEFAULT_MAX_PDF_PAGES = 500
DEFAULT_MAX_IMAGE_PIXELS = 100_000_000
DEFAULT_MAX_ZIP_ENTRIES = 2000
DEFAULT_MAX_ZIP_UNCOMPRESSED_MB = 500.0
DEFAULT_MAX_ZIP_RATIO = 100.0
DEFAULT_MAX_ZIP_ENTRY_MB = 200.0

_MB = 1024 * 1024


@dataclass(frozen=True)
class ResourceLimits:
    """从 settings 派生的运行期资源约束（不可变）。"""

    max_file_size_bytes: int
    max_workers: int
    task_timeout_seconds: float
    max_queue_depth: int = DEFAULT_MAX_QUEUE_DEPTH
    admission_timeout_seconds: float = DEFAULT_ADMISSION_TIMEOUT_SECONDS
    scan_timeout_seconds: float = DEFAULT_SCAN_TIMEOUT_SECONDS
    process_timeout_seconds: float = DEFAULT_PROCESS_TIMEOUT_SECONDS
    audit_timeout_seconds: float = DEFAULT_AUDIT_TIMEOUT_SECONDS
    finalize_timeout_seconds: float = DEFAULT_FINALIZE_TIMEOUT_SECONDS
    retry_max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS
    retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS
    retry_max_delay_seconds: float = DEFAULT_RETRY_MAX_DELAY_SECONDS
    max_pdf_pages: int = DEFAULT_MAX_PDF_PAGES
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS
    max_zip_entries: int = DEFAULT_MAX_ZIP_ENTRIES
    max_zip_uncompressed_bytes: int = int(DEFAULT_MAX_ZIP_UNCOMPRESSED_MB * _MB)
    max_zip_compression_ratio: float = DEFAULT_MAX_ZIP_RATIO
    max_zip_entry_bytes: int = int(DEFAULT_MAX_ZIP_ENTRY_MB * _MB)

    @classmethod
    def from_settings(cls, settings) -> "ResourceLimits":
        """从应用 Settings 读取；settings 为空或字段缺失时回退安全默认值。"""
        sec = getattr(settings, "security", None) if settings is not None else None
        try:
            size_mb = float(getattr(sec, "max_file_size_mb", DEFAULT_MAX_FILE_SIZE_MB))
        except (TypeError, ValueError):
            size_mb = DEFAULT_MAX_FILE_SIZE_MB
        try:
            workers = int(getattr(sec, "max_workers", DEFAULT_MAX_WORKERS))
        except (TypeError, ValueError):
            workers = DEFAULT_MAX_WORKERS
        try:
            timeout = float(getattr(sec, "task_timeout_seconds", DEFAULT_TASK_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            timeout = DEFAULT_TASK_TIMEOUT_SECONDS
        def _num(name, default):
            try:
                return float(getattr(sec, name, default))
            except (TypeError, ValueError):
                return float(default)
        def _int(name, default):
            try:
                return int(getattr(sec, name, default))
            except (TypeError, ValueError):
                return int(default)
        return cls(
            max_file_size_bytes=int(size_mb * _MB),
            max_workers=workers,
            task_timeout_seconds=timeout,
            max_queue_depth=_int("max_queue_depth", DEFAULT_MAX_QUEUE_DEPTH),
            admission_timeout_seconds=_num(
                "admission_timeout_seconds", DEFAULT_ADMISSION_TIMEOUT_SECONDS),
            scan_timeout_seconds=_num("scan_timeout_seconds", DEFAULT_SCAN_TIMEOUT_SECONDS),
            process_timeout_seconds=_num(
                "process_timeout_seconds", DEFAULT_PROCESS_TIMEOUT_SECONDS),
            audit_timeout_seconds=_num("audit_timeout_seconds", DEFAULT_AUDIT_TIMEOUT_SECONDS),
            finalize_timeout_seconds=_num(
                "finalize_timeout_seconds", DEFAULT_FINALIZE_TIMEOUT_SECONDS),
            retry_max_attempts=_int("retry_max_attempts", DEFAULT_RETRY_MAX_ATTEMPTS),
            retry_base_delay_seconds=_num(
                "retry_base_delay_seconds", DEFAULT_RETRY_BASE_DELAY_SECONDS),
            retry_max_delay_seconds=_num(
                "retry_max_delay_seconds", DEFAULT_RETRY_MAX_DELAY_SECONDS),
            max_pdf_pages=_int("max_pdf_pages", DEFAULT_MAX_PDF_PAGES),
            max_image_pixels=_int("max_image_pixels", DEFAULT_MAX_IMAGE_PIXELS),
            max_zip_entries=_int("max_zip_entries", DEFAULT_MAX_ZIP_ENTRIES),
            max_zip_uncompressed_bytes=int(_num("max_zip_uncompressed_mb", DEFAULT_MAX_ZIP_UNCOMPRESSED_MB) * _MB),
            max_zip_compression_ratio=_num("max_zip_compression_ratio", DEFAULT_MAX_ZIP_RATIO),
            max_zip_entry_bytes=int(_num("max_zip_entry_mb", DEFAULT_MAX_ZIP_ENTRY_MB) * _MB),
        )

    def check_file_size(self, path: str | Path) -> None:
        """入口守卫：文件超过大小上限即拒绝（失败关闭）。"""
        size = Path(path).stat().st_size
        if size > self.max_file_size_bytes:
            raise SecurityViolation(
                f"文件超过大小上限：{size} 字节 > 上限 {self.max_file_size_bytes} 字节",
                code=ErrorCode.RESOURCE_LIMIT,
            )
