"""统一文件准入（T4）：真实类型、容器安全、路径/资源上限。"""
from __future__ import annotations

import hashlib
import io
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

import pymupdf
from PIL import Image

from ..domain.exceptions import ErrorCode, SecurityViolation
from .hashing import sha256_file
from .limits import ResourceLimits

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class AdmissionError(SecurityViolation):
    """准入拒绝；reason_code 供批处理/事件记录，不包含正文。"""

    def __init__(self, message: str, reason_code: str = "admission_rejected",
                 code: ErrorCode = ErrorCode.SECURITY_VIOLATION):
        super().__init__(message, code=code)
        self.reason_code = reason_code
        self.task = None


@dataclass(frozen=True)
class AdmissionResult:
    path: Path
    real_type: str
    extension: str
    size: int
    source_hash: str
    page_count: int = 0
    pixel_count: int = 0


_EXT_EXPECTED = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsm",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".tif": "tiff",
    ".tiff": "tiff",
    ".bmp": "bmp",
}

_IMAGE_SIGNATURES = {
    "png": b"\x89PNG\r\n\x1a\n",
    "jpeg": b"\xff\xd8\xff",
    "tiff_le": b"II*\x00",
    "tiff_be": b"MM\x00*",
    "bmp": b"BM",
}


_DOCX_SAFE_MEDIA_EXTENSIONS = {
    ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp",
}
_DOCX_THUMBNAIL_NAMES = {
    "docprops/thumbnail.jpeg", "docprops/thumbnail.jpg", "docprops/thumbnail.png",
}
_DOCX_DEFAULT_THUMBNAIL_SHA256 = "96367138dc44ce09bf2c8f0f8e49348a1478d2c5c0af69bbc2bbc38b63cdcead"


def is_docx_ocr_media_part(name: str, blob: bytes | None = None) -> bool:
    """返回是否需要 OCR；仅精确哈希匹配的空白模板缩略图可跳过。"""
    low = str(name).replace("\\", "/").lstrip("/").lower()
    if low.startswith("word/media/") and Path(low).suffix in _DOCX_SAFE_MEDIA_EXTENSIONS:
        return True
    if low not in _DOCX_THUMBNAIL_NAMES:
        return False
    if blob is None:
        return True
    digest = hashlib.sha256(blob).hexdigest()
    return digest != _DOCX_DEFAULT_THUMBNAIL_SHA256


def find_unsafe_docx_parts(names: list[str]) -> list[str]:
    """返回 DOCX 中无法确认安全的二进制嵌入部件。"""
    unsafe: list[str] = []
    for raw in names:
        name = str(raw).replace("\\", "/").lstrip("/")
        low = name.lower()
        if not low or low.endswith("/"):
            continue
        if low.endswith(".xml") or low.endswith(".rels"):
            continue
        if is_docx_ocr_media_part(low):
            continue
        unsafe.append(name)
    return sorted(set(unsafe))


def decode_ooxml_xml(data: bytes) -> str:
    """严格解码 OOXML XML/REL 部件；编码损坏必须显式失败。"""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


class FileAdmission:
    def __init__(self, settings=None, limits: ResourceLimits | None = None):
        self.limits = limits or ResourceLimits.from_settings(settings)

    def admit(self, path: str | Path) -> AdmissionResult:
        p = Path(path)
        self._check_path(p)
        size = p.stat().st_size
        if size > self.limits.max_file_size_bytes:
            raise AdmissionError(
                f"文件超过大小上限：{size} > {self.limits.max_file_size_bytes}",
                reason_code="file_too_large", code=ErrorCode.RESOURCE_LIMIT)
        digest_before = sha256_file(p)

        ext = p.suffix.lower()
        expected = _EXT_EXPECTED.get(ext)
        if expected is None:
            raise AdmissionError(
                f"不支持的文件扩展名：{ext or '(无扩展名)'}",
                reason_code="unsupported_extension")

        with open(p, "rb") as f:
            head = f.read(1024)
        real_type = self._detect_type(p, head)
        if real_type != expected:
            raise AdmissionError(
                f"扩展名与实际类型不一致：扩展名 {ext}，实际 {real_type}",
                reason_code="extension_mismatch")

        page_count = 0
        pixel_count = 0
        if real_type == "pdf":
            page_count = self._check_pdf(p)
        elif real_type in ("docx", "xlsx", "xlsm"):
            self._inspect_zip(p, real_type)
        elif real_type in ("png", "jpeg", "tiff", "bmp"):
            pixel_count = self._check_image(p, real_type)

        digest = sha256_file(p)
        if digest != digest_before:
            raise AdmissionError(
                "文件在准入检查期间发生变化，拒绝处理",
                reason_code="source_changed_during_admission")
        return AdmissionResult(
            path=p, real_type=real_type, extension=ext, size=size,
            source_hash=digest, page_count=page_count, pixel_count=pixel_count)

    def _check_path(self, p: Path) -> None:
        # 在任何文件系统访问前拒绝 UNC/设备路径，避免触发远程访问。
        raw = str(p)
        win = PureWindowsPath(raw)
        if (raw.startswith(("\\", "/")) or "\\?\\" in raw
                or win.drive.startswith("\\\\")):
            raise AdmissionError("拒绝 UNC/设备路径", reason_code="unc_path_rejected")
        if win.drive and not win.root:
            raise AdmissionError("拒绝盘符相对路径", reason_code="drive_relative_path")
        if any(part == ".." for part in p.parts):
            raise AdmissionError("拒绝包含 .. 的路径", reason_code="path_traversal_rejected")

        # 对目标及其现有祖先逐级 lstat，阻止通过 junction/重解析点跨出受控位置。
        probe = p
        while True:
            try:
                st = os.lstat(probe)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise AdmissionError(f"无法检查文件属性：{type(exc).__name__}",
                                     reason_code="path_check_failed") from exc
            else:
                attrs = getattr(st, "st_file_attributes", 0)
                if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                    raise AdmissionError("拒绝 junction/重解析点路径",
                                         reason_code="reparse_point_rejected")
                if probe == p and not probe.is_file():
                    raise AdmissionError(f"路径不是普通文件：{p}", reason_code="not_a_file")
            parent = probe.parent
            if parent == probe:
                break
            probe = parent

        if not p.exists():
            raise AdmissionError(f"文件不存在：{p}", reason_code="file_not_found",
                                 code=ErrorCode.FILE_NOT_FOUND)
        if p.is_symlink():
            raise AdmissionError("拒绝符号链接文件", reason_code="symlink_rejected")
        try:
            p.resolve(strict=True)
        except OSError as exc:
            raise AdmissionError(f"路径无法解析：{type(exc).__name__}",
                                 reason_code="path_resolve_failed") from exc
        try:
            with open(p, "rb"):
                pass
        except OSError as exc:
            raise AdmissionError(f"文件不可读：{type(exc).__name__}",
                                 reason_code="file_unreadable") from exc

    def _detect_type(self, p: Path, head: bytes) -> str:
        if head.lstrip().startswith(b"%PDF-"):
            return "pdf"
        if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
            return self._zip_ooxml_type(p)
        for name in ("png", "jpeg"):
            if head.startswith(_IMAGE_SIGNATURES[name]):
                return name
        if head.startswith(_IMAGE_SIGNATURES["tiff_le"]) or \
                head.startswith(_IMAGE_SIGNATURES["tiff_be"]):
            return "tiff"
        if head.startswith(_IMAGE_SIGNATURES["bmp"]):
            return "bmp"
        raise AdmissionError("无法识别的真实文件类型", reason_code="unknown_type")

    def _zip_ooxml_type(self, p: Path) -> str:
        try:
            with zipfile.ZipFile(p) as zf:
                infos = zf.infolist()
                names = [i.filename for i in infos]
                if len(infos) > self.limits.max_zip_entries:
                    raise AdmissionError(
                        f"ZIP 条目数超限：{len(infos)} > {self.limits.max_zip_entries}",
                        reason_code="zip_entries_exceeded", code=ErrorCode.RESOURCE_LIMIT)
                if len(names) != len(set(names)):
                    raise AdmissionError("ZIP 包含重复条目名", reason_code="zip_duplicate_entry")
                total = 0
                for info in infos:
                    self._safe_zip_name(info.filename)
                    if info.flag_bits & 0x1:
                        raise AdmissionError("ZIP 包含加密条目", reason_code="zip_encrypted")
                    if info.file_size < 0 or info.compress_size < 0:
                        raise AdmissionError("ZIP 条目声明了非法大小",
                                             reason_code="zip_invalid_size")
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == 0o120000:
                        raise AdmissionError("拒绝 ZIP 符号链接条目",
                                             reason_code="zip_symlink_entry")
                    if info.file_size > self.limits.max_zip_entry_bytes:
                        raise AdmissionError(
                            f"ZIP 单条目解压大小超限：{info.filename}",
                            reason_code="zip_entry_too_large", code=ErrorCode.RESOURCE_LIMIT)
                    total += info.file_size
                    ratio = info.file_size / max(info.compress_size, 1)
                    if ratio > self.limits.max_zip_compression_ratio:
                        raise AdmissionError(
                            f"ZIP 压缩比超限：{info.filename}",
                            reason_code="zip_ratio_exceeded", code=ErrorCode.RESOURCE_LIMIT)
                if total > self.limits.max_zip_uncompressed_bytes:
                    raise AdmissionError(
                        f"ZIP 解压总大小超限：{total} > {self.limits.max_zip_uncompressed_bytes}",
                        reason_code="zip_total_too_large", code=ErrorCode.RESOURCE_LIMIT)
                if "[Content_Types].xml" not in names:
                    raise AdmissionError("ZIP 缺少 OOXML [Content_Types].xml",
                                         reason_code="ooxml_content_types_missing")
                if "word/document.xml" in names:
                    unsafe = find_unsafe_docx_parts(names)
                    if unsafe:
                        raise AdmissionError(
                            "DOCX 含无法检查的二进制嵌入对象，拒绝处理："
                            f"{unsafe[0]}",
                            reason_code="docx_embedded_object")
                    self._validate_docx_media_parts(zf, names)
                    self._validate_ooxml_parts(zf, names)
                    return "docx"
                if "xl/workbook.xml" in names:
                    self._validate_ooxml_parts(zf, names)
                    has_vba = any(n.lower().endswith("vbaproject.bin") for n in names)
                    return "xlsm" if has_vba else "xlsx"
                raise AdmissionError("ZIP 不是受支持的 Word/Excel OOXML 容器",
                                     reason_code="ooxml_unknown_container")
        except AdmissionError:
            raise
        except (zipfile.BadZipFile, OSError) as exc:
            raise AdmissionError(f"ZIP 容器损坏：{type(exc).__name__}",
                                 reason_code="zip_corrupt") from exc

    @staticmethod
    def _validate_docx_media_parts(zf: zipfile.ZipFile, names: list[str]) -> None:
        """确认所有需 OCR 的 DOCX 图片可读取和解析。"""
        for name in names:
            if not is_docx_ocr_media_part(name):
                continue
            try:
                with Image.open(io.BytesIO(zf.read(name))) as image:
                    image.verify()
            except Exception as exc:
                raise AdmissionError(
                    f"DOCX 图像/缩略图部件无法检查：{name}",
                    reason_code="docx_media_unreadable") from exc

    @staticmethod
    def _validate_ooxml_parts(zf: zipfile.ZipFile, names: list[str]) -> None:
        """读取并严格解码全部 XML/REL，CRC 或编码失败时失败关闭。"""
        for name in names:
            low = name.lower()
            if not (low.endswith(".xml") or low.endswith(".rels")):
                continue
            try:
                decode_ooxml_xml(zf.read(name))
            except Exception as exc:
                raise AdmissionError(
                    f"OOXML XML/REL 部件无法读取或解码：{name}",
                    reason_code="ooxml_part_unreadable") from exc

    def _inspect_zip(self, p: Path, expected_type: str) -> None:
        """准入阶段再次验证 OOXML 容器，并确认扩展名对应类型。"""
        actual_type = self._zip_ooxml_type(p)
        compatible_excel = {actual_type, expected_type} <= {"xlsx", "xlsm"}
        if actual_type != expected_type and not compatible_excel:
            raise AdmissionError(
                f"OOXML 扩展名与实际类型不一致：扩展名 {expected_type}，实际 {actual_type}",
                reason_code="extension_mismatch")

    @staticmethod
    def _safe_zip_name(name: str) -> None:
        norm = name.replace("\\", "/")
        if not norm or norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
            raise AdmissionError("ZIP 条目包含绝对路径/盘符",
                                 reason_code="zip_path_escape")
        parts = PurePosixPath(norm).parts
        if any(part == ".." for part in parts):
            raise AdmissionError("ZIP 条目包含 .. 路径穿越",
                                 reason_code="zip_path_escape")

    def _check_pdf(self, p: Path) -> int:
        try:
            doc = pymupdf.open(str(p))
        except Exception as exc:
            raise AdmissionError(f"PDF 解析失败：{type(exc).__name__}",
                                 reason_code="pdf_corrupt") from exc
        try:
            if doc.needs_pass:
                raise AdmissionError("PDF 已加密", reason_code="pdf_encrypted")
            pages = doc.page_count
            if pages > self.limits.max_pdf_pages:
                raise AdmissionError(
                    f"PDF 页数超限：{pages} > {self.limits.max_pdf_pages}",
                    reason_code="pdf_pages_exceeded", code=ErrorCode.RESOURCE_LIMIT)
            return pages
        finally:
            doc.close()

    def _check_image(self, p: Path, real_type: str) -> int:
        expected_formats = {
            "png": "PNG", "jpeg": "JPEG", "tiff": "TIFF", "bmp": "BMP",
        }
        try:
            with Image.open(p) as img:
                actual_format = (img.format or "").upper()
                if actual_format != expected_formats[real_type]:
                    raise AdmissionError(
                        f"图片扩展名与实际格式不一致：实际 {actual_format or 'unknown'}",
                        reason_code="extension_mismatch")
                img.verify()
            with Image.open(p) as img:
                width, height = img.size
                frames = int(getattr(img, "n_frames", 1) or 1)
                pixels = int(width) * int(height) * max(1, frames)
        except AdmissionError:
            raise
        except Image.DecompressionBombError as exc:
            raise AdmissionError(
                "图片像素数超过安全解析上限",
                reason_code="image_pixels_exceeded", code=ErrorCode.RESOURCE_LIMIT) from exc
        except Exception as exc:
            raise AdmissionError(f"图片结构解析失败：{type(exc).__name__}",
                                 reason_code="image_corrupt") from exc
        if pixels > self.limits.max_image_pixels:
            raise AdmissionError(
                f"图片像素数超限：{pixels} > {self.limits.max_image_pixels}",
                reason_code="image_pixels_exceeded", code=ErrorCode.RESOURCE_LIMIT)
        return pixels
