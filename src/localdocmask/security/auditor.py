"""独立安全审计：重新提取输出文本/OCR/元数据/隐藏内容，残留即失败，无法检查即失败。"""
from __future__ import annotations

import zipfile

import numpy as np
import pymupdf
from PIL import Image

from ..recognition import patterns
from ..domain.entities import AuditFinding, AuditReport, AuditVerdict
from ..infrastructure.ocr.engine import OCREngine
from .admission import decode_ooxml_xml, find_unsafe_docx_parts, is_docx_ocr_media_part

_HIGH_RISK = [
    ("身份证号", patterns.ID_CARD_RE, lambda s: patterns.is_valid_id(s) and patterns.is_valid_birth(s)),
    ("手机号", patterns.MOBILE_RE, None),
    ("固定电话", patterns.LANDLINE_RE, None),
    ("邮箱", patterns.EMAIL_RE, None),
    ("护照号", patterns.PASSPORT_RE, None),
    ("银行卡号", patterns.BANK_CARD_RE, patterns.luhn_ok),
]


def _contains_any(blob: str, terms: list[str]) -> list[str]:
    return [t for t in terms if t in blob]


class Auditor:
    """独立于处理模块的审计器。"""

    def __init__(self, ocr_engine=None):
        self._ocr_engine = ocr_engine or OCREngine()

    @staticmethod
    def _coverage_for_ext(ext: str) -> list[str]:
        if ext in ("pdf", "docx"):
            return ["text", "ocr", "metadata", "hidden"]
        if ext in ("xlsx", "xlsm"):
            return ["text", "metadata", "hidden"]
        if ext in ("png", "jpg", "jpeg", "tif", "tiff", "bmp"):
            return ["ocr"]
        return ["container"]

    def audit(self, out_path: str, forbidden_terms: list[str]) -> AuditReport:
        ext = out_path.lower().rsplit(".", 1)[-1] if "." in out_path else ""
        report = AuditReport(coverage=self._coverage_for_ext(ext))
        terms = [t for t in forbidden_terms if t and len(t) >= 1]
        try:
            text = self._extract_text(out_path, ext, report)
        except Exception as e:
            report.coverage = list(dict.fromkeys([*report.coverage, "container"]))
            report.findings.append(AuditFinding(AuditVerdict.UNCHECKABLE, f"无法读取输出文件：{e}"))
            return report
        self._check_text(text, terms, report)
        self._check_ocr(out_path, ext, terms, report)
        self._check_metadata_hidden(out_path, ext, terms, report)
        if not report.findings:
            report.findings.append(AuditFinding(AuditVerdict.NO_RESIDUAL, "未发现已知敏感信息残留"))
        report.passed = all(
            f.verdict == AuditVerdict.NO_RESIDUAL for f in report.findings
        )
        return report

    def residual_terms(self, out_path: str, terms: list[str]) -> tuple[list[str], list[str]]:
        """逐项检查：返回 (残留的 terms, 无法检查的 terms)。独立于处理模块。"""
        terms = [t for t in terms if t]
        if not terms:
            return [], []
        ext = out_path.lower().rsplit(".", 1)[-1] if "." in out_path else ""
        try:
            blob = self._all_text(out_path, ext)
        except Exception:
            return [], list(terms)
        residual = [t for t in terms if t in blob]
        return residual, []

    def _all_text(self, out_path: str, ext: str) -> str:
        parts: list[str] = []
        parts.append(self._extract_text(out_path, ext))
        # OCR/嵌入媒体异常必须向上抛出；residual_terms 会将其归入 uncheckable。
        if ext == "pdf":
            doc = pymupdf.open(out_path)
            try:
                for p in doc:
                    if p.get_images(full=True):
                        pix = p.get_pixmap(dpi=150)
                        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                        for it in self._ocr_engine.ocr(img):
                            parts.append(it["text"])
            finally:
                doc.close()
        elif ext in ("png", "jpg", "jpeg", "tif", "tiff", "bmp"):
            with Image.open(out_path) as image:
                img = image.convert("RGB")
            for it in self._ocr_engine.ocr(np.asarray(img)):
                parts.append(it["text"])
        elif ext == "docx":
            import docx
            d = docx.Document(out_path)
            for part in d.part.package.parts:
                if is_docx_ocr_media_part(str(part.partname), part.blob):
                    for it in self._ocr_engine.ocr(part.blob):
                        parts.append(it["text"])
        # 压缩包内 XML：任何读取、CRC 或解码失败都必须向上抛出，由调用方判定无法检查。
        if ext in ("docx", "xlsx", "xlsm"):
            if not zipfile.is_zipfile(out_path):
                raise ValueError("OOXML 容器损坏，无法检查")
            try:
                with zipfile.ZipFile(out_path) as z:
                    names = z.namelist()
                    if ext == "docx":
                        unsafe_parts = find_unsafe_docx_parts(names)
                        if unsafe_parts:
                            raise ValueError(
                                f"DOCX 含无法检查的嵌入对象：{unsafe_parts[0]}")
                    for n in names:
                        low = n.lower()
                        if low.endswith(".xml") or low.endswith(".rels"):
                            try:
                                parts.append(decode_ooxml_xml(z.read(n)))
                            except Exception as exc:
                                raise ValueError(
                                    f"OOXML 部件无法读取或解码：{n}") from exc
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError("OOXML 容器无法读取") from exc
        return "\n".join(parts)

    def _check_text(self, text: str, terms: list[str], report: AuditReport) -> None:
        for t in terms:
            if t in text:
                report.findings.append(AuditFinding(AuditVerdict.RESIDUAL_FOUND, f"输出文本中发现残留：{t}"))
        for name, regex, validator in _HIGH_RISK:
            for m in regex.finditer(text):
                v = m.group(0)
                if validator is not None and not validator(v):
                    continue
                report.findings.append(AuditFinding(AuditVerdict.RESIDUAL_FOUND, f"输出文本中发现高风险模式：{name} {v}"))

    def _check_ocr(self, out_path: str, ext: str, terms: list[str], report: AuditReport) -> None:
        try:
            if ext == "pdf":
                doc = pymupdf.open(out_path)
                for pno in range(doc.page_count):
                    page = doc[pno]
                    if not page.get_images(full=True):
                        continue
                    pix = page.get_pixmap(dpi=200)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    self._ocr_check(img, terms, report, f"PDF 第 {pno + 1} 页")
                doc.close()
            elif ext in ("png", "jpg", "jpeg", "tif", "tiff", "bmp"):
                img = Image.open(out_path).convert("RGB")
                self._ocr_check(np.asarray(img), terms, report, "图片")
            elif ext == "docx":
                self._check_docx_images(out_path, terms, report)
        except Exception as e:
            report.findings.append(AuditFinding(AuditVerdict.UNCHECKABLE, f"OCR 复查失败：{e}"))

    def _check_docx_images(self, out_path: str, terms: list[str], report: AuditReport) -> None:
        import docx
        d = docx.Document(out_path)
        for part in d.part.package.parts:
            if is_docx_ocr_media_part(str(part.partname), part.blob):
                try:
                    self._ocr_check(part.blob, terms, report, "DOCX 嵌入图像")
                except Exception as e:
                    report.findings.append(AuditFinding(AuditVerdict.UNCHECKABLE, f"DOCX 嵌入图像 OCR 失败：{e}"))

    def _ocr_check(self, image, terms: list[str], report: AuditReport, where: str) -> None:
        try:
            items = self._ocr_engine.ocr(image)
        except Exception as e:
            report.findings.append(AuditFinding(AuditVerdict.UNCHECKABLE, f"{where} OCR 失败：{e}"))
            return
        for it in items:
            text = it["text"]
            for t in terms:
                if t in text:
                    report.findings.append(AuditFinding(AuditVerdict.RESIDUAL_FOUND, f"{where} OCR 发现残留：{t}"))
            for name, regex, validator in _HIGH_RISK:
                for m in regex.finditer(text):
                    v = m.group(0)
                    if validator is not None and not validator(v):
                        continue
                    report.findings.append(AuditFinding(AuditVerdict.RESIDUAL_FOUND, f"{where} OCR 发现高风险模式：{name} {v}"))

    def _check_metadata_hidden(self, out_path: str, ext: str, terms: list[str], report: AuditReport) -> None:
        try:
            if ext == "pdf":
                doc = pymupdf.open(out_path)
                meta = doc.metadata or {}
                for k, v in meta.items():
                    if v:
                        for t in terms:
                            if t in str(v):
                                report.findings.append(AuditFinding(AuditVerdict.RESIDUAL_FOUND, f"PDF 元数据残留：{t}"))
                if doc.embfile_count() > 0:
                    report.findings.append(AuditFinding(AuditVerdict.NEEDS_CONFIRM, "PDF 仍含附件"))
                for pno in range(doc.page_count):
                    for wdg in doc[pno].widgets() or []:
                        try:
                            val = wdg.field_value or ""
                        except Exception:
                            val = ""
                        for t in terms:
                            if val and t in str(val):
                                report.findings.append(AuditFinding(AuditVerdict.RESIDUAL_FOUND, f"PDF 表单字段残留：{t}"))
                doc.close()
            elif ext in ("docx", "xlsx", "xlsm") and zipfile.is_zipfile(out_path):
                with zipfile.ZipFile(out_path) as z:
                    names = z.namelist()
                    for n in names:
                        low = n.lower()
                        if low.endswith(".xml") or low.endswith(".rels"):
                            try:
                                data = decode_ooxml_xml(z.read(n))
                            except Exception as exc:
                                report.findings.append(AuditFinding(
                                    AuditVerdict.UNCHECKABLE,
                                    f"OOXML 部件无法读取或解码：{n} ({type(exc).__name__})",
                                    scope="hidden"))
                                continue
                            for t in terms:
                                if t in data:
                                    report.findings.append(AuditFinding(AuditVerdict.RESIDUAL_FOUND, f"隐藏部件残留：{t} ({n})"))
                    if ext == "docx":
                        unsafe_parts = find_unsafe_docx_parts(names)
                        if unsafe_parts:
                            report.findings.append(AuditFinding(
                                AuditVerdict.UNCHECKABLE,
                                f"输出文件仍含无法检查的嵌入对象：{unsafe_parts[0]}",
                                scope="hidden"))
                    elif ext in ("xlsx", "xlsm"):
                        if any(n.endswith("vbaProject.bin") for n in names):
                            report.findings.append(AuditFinding(AuditVerdict.NEEDS_CONFIRM, "输出文件仍含宏"))
                        if any("/embeddings/" in n.lower() for n in names):
                            report.findings.append(AuditFinding(AuditVerdict.NEEDS_CONFIRM, "输出文件仍含嵌入对象(OLE)"))
        except Exception as e:
            report.findings.append(AuditFinding(AuditVerdict.UNCHECKABLE, f"元数据/隐藏内容检查失败：{e}"))

    def _extract_text(self, out_path: str, ext: str, report=None) -> str:
        if ext == "pdf":
            doc = pymupdf.open(out_path)
            parts = [p.get_text() for p in doc]
            doc.close()
            return "\n".join(parts)
        if ext == "docx":
            import docx
            from docx.oxml.ns import qn
            d = docx.Document(out_path)
            parts = []
            for p in d.paragraphs:
                parts.append(p.text)
            for t in d.tables:
                for row in t.rows:
                    for c in row.cells:
                        parts.append(c.text)
            for s in d.sections:
                parts.append(s.header.paragraphs[0].text if s.header.paragraphs else "")
                parts.append(s.footer.paragraphs[0].text if s.footer.paragraphs else "")
            for el in d.element.body.iter(qn("w:t")):
                if el.text:
                    parts.append(el.text)
            # 脚注/尾注（直接从压缩包读取，python-docx 不解析这些部件）
            import lxml.etree as ET
            with zipfile.ZipFile(out_path) as z:
                for n in z.namelist():
                    low = n.lower()
                    if low.endswith("footnotes.xml") or low.endswith("endnotes.xml"):
                        try:
                            root = ET.fromstring(z.read(n))
                            for el in root.iter(qn("w:t")):
                                if el.text:
                                    parts.append(el.text)
                        except Exception:
                            pass
            return "\n".join(parts)
        if ext in ("xlsx", "xlsm"):
            import openpyxl
            wb = openpyxl.load_workbook(out_path, data_only=False)
            parts = []
            for ws in wb.worksheets:
                for row in ws.iter_rows():
                    for c in row:
                        if c.value is not None:
                            parts.append(str(c.value))
            wb.close()
            return "\n".join(parts)
        if ext in ("png", "jpg", "jpeg", "tif", "tiff", "bmp"):
            return ""
        raise ValueError(f"无法审计的格式：{ext}")
