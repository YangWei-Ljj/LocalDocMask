"""格式处理器注册与分发。"""
from __future__ import annotations

from pathlib import Path

from .pdf import PdfHandler
from .docx import DocxHandler
from .xlsx import XlsxHandler
from .image import ImageHandler

_EXT_MAP = {
    ".pdf": PdfHandler,
    ".docx": DocxHandler,
    ".xlsx": XlsxHandler,
    ".xlsm": XlsxHandler,
    ".png": ImageHandler,
    ".jpg": ImageHandler,
    ".jpeg": ImageHandler,
    ".tif": ImageHandler,
    ".tiff": ImageHandler,
    ".bmp": ImageHandler,
}

SUPPORTED = sorted(_EXT_MAP.keys())


def supported_extensions() -> list[str]:
    return list(SUPPORTED)


def handler_for(path: str | Path, detector):
    ext = Path(path).suffix.lower()
    cls = _EXT_MAP.get(ext)
    if cls is None:
        raise ValueError(f"不支持的文件格式：{ext or '(无扩展名)'}")
    return cls(detector)
