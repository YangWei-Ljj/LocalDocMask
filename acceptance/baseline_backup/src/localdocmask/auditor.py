"""独立安全审计：重新提取输出文本/OCR/元数据/隐藏内容，残留即失败，无法检查即失败。"""
from __future__ import annotations

import io
import zipfile

import numpy as np
import pymupdf
from PIL import Image

from . import patterns
from .models import AuditFinding, AuditReport, AuditVerdict
from .ocr import OCREngine

_HIGH_RISK = [
    ("身份证号", patterns.ID_CARD_RE, lambda s: patterns.is_valid_id(s) and patterns.is_valid_birth(s)),
    ("手机号", patterns.MOBILE_RE, None),
    ("邮箱", patterns.EMAIL_RE, None),
    ("银行卡号", patterns.BANK_CARD_RE, patterns.luhn_ok),
]


class Auditor:
    """独立于处理模块的审计器。"""

    def audit(self, out_path: str, forbidden_terms: list[str]) -> AuditReport:
        report = AuditReport()
        terms = [t for t in forbidden_terms if t and len(t) >= 2]
        ext = out_path.lower().rsplit(".", 1)[-1] if "." in out_path else ""
        try:
            text = self._extract_text(out_path, ext, report)
        except Exception as e:
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
        except Exception as e:
            report.findings.append(AuditFinding(AuditVerdict.UNCHECKABLE, f"OCR 复查失败：{e}"))

    def _ocr_check(self, image, terms: list[str], report: AuditReport, where: str) -> None:
        try:
            items = OCREngine.ocr(image)
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
                doc.close()
            elif ext == "docx" and zipfile.is_zipfile(out_path):
                with zipfile.ZipFile(out_path) as z:
                    names = z.namelist()
                    for n in names:
                        if "comments" in n.lower() or "docProps" in n.lower():
                            data = z.read(n).decode("utf-8", "ignore")
                            for t in terms:
                                if t in data:
                                    report.findings.append(AuditFinding(AuditVerdict.RESIDUAL_FOUND, f"DOCX 隐藏内容残留：{t} ({n})"))
            elif ext in ("xlsx", "xlsm") and zipfile.is_zipfile(out_path):
                with zipfile.ZipFile(out_path) as z:
                    if any(n.endswith("vbaProject.bin") for n in z.namelist()):
                        report.findings.append(AuditFinding(AuditVerdict.NEEDS_CONFIRM, "输出文件仍含宏"))
        except Exception as e:
            report.findings.append(AuditFinding(AuditVerdict.UNCHECKABLE, f"元数据/隐藏内容检查失败：{e}"))

    def _extract_text(self, out_path: str, ext: str, report: AuditReport) -> str:
        if ext == "pdf":
            doc = pymupdf.open(out_path)
            text = "\n".join(p.get_text() for p in doc)
            doc.close()
            return text
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
            # 文本框等
            for el in d.element.body.iter(qn("w:t")):
                if el.text:
                    parts.append(el.text)
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
