"""DOCX 处理器：段落/表格/页眉页脚/文本框文本替换，批注/修订/元数据清除，嵌入图像 OCR 检测。"""
from __future__ import annotations

import io
import zipfile

from docx import Document
from docx.oxml.ns import qn
from lxml import etree

from ..models import ScanResult
from ..ocr import OCREngine
from .base import FormatHandler

_W_T = qn("w:t")
_W_P = qn("w:p")
_W_DELTEXT = qn("w:delText")


def _iter_paragraphs(root):
    return root.iter(_W_P)


def _paragraph_texts(root):
    for t in root.iter(_W_T):
        if t.text:
            yield t.text


def _replace_in_paragraph(p, candidates):
    t_elems = list(p.iter(_W_T))
    if not t_elems:
        return
    combined = "".join(e.text or "" for e in t_elems)
    if not combined:
        return
    matches = []
    for cand in candidates:
        text = cand.text
        if not text:
            continue
        repl = cand.replacement or "****"
        start = 0
        while True:
            i = combined.find(text, start)
            if i < 0:
                break
            matches.append((i, i + len(text), repl))
            start = i + len(text)
    if not matches:
        return
    matches.sort(key=lambda m: m[0])
    merged = []
    for m in matches:
        if merged and m[0] < merged[-1][1]:
            continue
        merged.append(m)
    boundaries = []
    pos = 0
    for e in t_elems:
        ln = len(e.text or "")
        boundaries.append([pos, pos + ln, e])
        pos += ln
    for start, end, repl in reversed(merged):
        first = last = None
        for idx, (s, e, _el) in enumerate(boundaries):
            if s <= start < e:
                first = idx
            if s < end <= e:
                last = idx
        if first is None or last is None:
            continue
        a = start - boundaries[first][0]
        b = end - boundaries[last][0]
        if first == last:
            el = boundaries[first][2]
            t = el.text or ""
            el.text = t[:a] + repl + t[b:]
        else:
            first_el = boundaries[first][2]
            last_el = boundaries[last][2]
            first_el.text = (first_el.text or "")[:a] + repl
            for k in range(first + 1, last):
                boundaries[k][2].text = ""
            last_el.text = (last_el.text or "")[b:]


class DocxHandler(FormatHandler):
    name = "docx"
    extensions = [".docx"]

    def _roots(self, doc):
        roots = [doc.part.element]
        for rel in doc.part.rels.values():
            if rel.reltype.endswith("/header") or rel.reltype.endswith("/footer"):
                try:
                    roots.append(rel.target_part.element)
                except Exception:
                    pass
        return roots

    def extract(self, path: str) -> ScanResult:
        scan = ScanResult()
        if not zipfile.is_zipfile(path):
            raise ValueError("DOCX 文件损坏或不是有效的 Word 文档")
        doc = Document(path)
        texts: list[str] = []
        for root in self._roots(doc):
            texts.extend(_paragraph_texts(root))
        # 批注文本
        try:
            for part in doc.part.package.parts:
                if "comments" in str(part.partname).lower():
                    for t in part.element.iter(_W_T):
                        if t.text:
                            texts.append(t.text)
        except Exception:
            pass
        scan.text = "\n".join(texts)
        scan.candidates = self.detector.detect_text(scan.text, location="DOCX 正文")
        # 修订记录检查
        try:
            has_del = any(root.find(".//" + qn("w:delText")) is not None for root in self._roots(doc))
            if has_del:
                scan.warnings.append("文档包含修订记录(隐藏删除文本)，处理时将一并清除")
        except Exception:
            pass
        # 嵌入图像 OCR
        try:
            for part in doc.part.package.parts:
                name = str(part.partname).lower()
                if name.startswith("/word/media/"):
                    try:
                        img = part.blob
                        items = OCREngine.ocr(img)
                        for it in items:
                            text = it["text"].strip()
                            if not text:
                                continue
                            for cand in self.detector.detect_text(text, location="嵌入图像(OCR)"):
                                cand.source = "ocr"
                                scan.candidates.append(cand)
                        if items:
                            scan.warnings.append("文档含嵌入图像，已 OCR 检测；印章/签名等图形需人工复核")
                    except Exception as e:
                        scan.warnings.append(f"嵌入图像 OCR 失败：{e}")
        except Exception as e:
            scan.warnings.append(f"嵌入图像检查失败：{e}")
        return scan

    def process(self, path: str, out_path: str, candidates: list) -> list[str]:
        warnings: list[str] = []
        doc = Document(path)
        # 文本替换
        for root in self._roots(doc):
            for p in list(_iter_paragraphs(root)):
                _replace_in_paragraph(p, candidates)
            # 清除隐藏删除文本
            for dt in root.findall(".//" + _W_DELTEXT):
                dt.text = ""
        # 清除批注
        try:
            for part in doc.part.package.parts:
                if "comments" in str(part.partname).lower():
                    for t in part.element.iter(_W_T):
                        t.text = ""
        except Exception:
            pass
        # 清除元数据
        self._clear_metadata(doc)
        doc.save(out_path)
        return warnings

    def _clear_metadata(self, doc):
        cp = doc.core_properties
        for attr in ("author", "last_modified_by", "comments", "category", "content_status",
                     "identifier", "keywords", "language", "revision", "subject", "title", "version"):
            try:
                setattr(cp, attr, "")
            except Exception:
                pass
        try:
            for part in doc.part.package.parts:
                if str(part.partname).endswith("/app.xml"):
                    for el in part.element.iter():
                        if el.tag.split("}")[-1] in ("Company", "Manager", "Application"):
                            el.text = ""
                    break
        except Exception:
            pass
