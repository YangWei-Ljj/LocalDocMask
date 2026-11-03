"""DOCX 处理器：文本替换 + 嵌入图像像素删除 + 批注/修订/脚注尾注/元数据清除。"""
from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path

import numpy as np
from docx import Document
from docx.oxml.ns import qn
from lxml import etree
from PIL import Image

from ...domain.entities import ScanResult
from ...security.admission import find_unsafe_docx_parts, is_docx_ocr_media_part
from ..ocr.engine import OCREngine
from .base import FormatHandler

_W_T = qn("w:t")
_W_P = qn("w:p")
_W_DELTEXT = qn("w:delText")
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"




def _unsafe_docx_parts(path: str) -> list[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            return find_unsafe_docx_parts(zf.namelist())
    except zipfile.BadZipFile as exc:
        raise ValueError("DOCX 文件损坏或不是有效的 Word 文档") from exc


def _is_footnote_name(name: str) -> bool:
    low = name.lower()
    return low.endswith("footnotes.xml") or low.endswith("endnotes.xml")


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
        repl = cand.replacement if cand.replacement is not None else "****"
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

    def __init__(self, detector, ocr_engine=None):
        super().__init__(detector)
        self._ocr_engine = ocr_engine or OCREngine()

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
        unsafe_parts = _unsafe_docx_parts(path)
        if unsafe_parts:
            scan.warnings.append(
                f"文档含无法检查的嵌入对象：{unsafe_parts[0]}，将禁止导出")
            scan.review_flags.append("docx_embedded_object_uncheckable")
        doc = Document(path)
        texts: list[str] = []
        # 正文段落（逐段定位）
        for i, p in enumerate(doc.paragraphs):
            if p.text.strip():
                texts.append(p.text)
                scan.candidates.extend(self.detector.detect_text(p.text, location=f"第 {i + 1} 段"))
        # 表格（逐单元格定位）
        for ti, table in enumerate(doc.tables):
            for ri, row in enumerate(table.rows):
                for ci, cell in enumerate(row.cells):
                    if cell.text.strip():
                        texts.append(cell.text)
                        scan.candidates.extend(self.detector.detect_text(cell.text, location=f"表格{ti + 1} 第{ri + 1}行第{ci + 1}列"))
        # 页眉页脚
        for section in doc.sections:
            for p in section.header.paragraphs:
                if p.text.strip():
                    texts.append(p.text)
                    scan.candidates.extend(self.detector.detect_text(p.text, location="页眉"))
            for p in section.footer.paragraphs:
                if p.text.strip():
                    texts.append(p.text)
                    scan.candidates.extend(self.detector.detect_text(p.text, location="页脚"))
        # 文本框
        try:
            for txbx in doc.element.body.iter(qn("w:txbxContent")):
                for t in txbx.iter(_W_T):
                    if t.text:
                        texts.append(t.text)
                        scan.candidates.extend(self.detector.detect_text(t.text, location="文本框"))
        except Exception:
            pass
        # 批注
        try:
            for part in doc.part.package.parts:
                if "comments" in str(part.partname).lower():
                    for t in part.element.iter(_W_T):
                        if t.text:
                            texts.append(t.text)
                            scan.candidates.extend(self.detector.detect_text(t.text, location="批注"))
        except Exception:
            pass
        # 脚注 / 尾注
        try:
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if _is_footnote_name(n):
                        try:
                            root = etree.fromstring(z.read(n))
                            for t in root.iter("{%s}t" % _W_NS):
                                if t.text:
                                    texts.append(t.text)
                                    scan.candidates.extend(self.detector.detect_text(t.text, location="脚注/尾注"))
                        except Exception:
                            pass
        except Exception:
            pass
        scan.text = "\n".join(texts)
        try:
            has_del = any(root.find(".//" + qn("w:delText")) is not None for root in self._roots(doc))
            if has_del:
                scan.warnings.append("文档包含修订记录(隐藏删除文本)，处理时将一并清除")
        except Exception:
            pass
        # 嵌入图像 OCR：记录像素框与所属部件，供“删除”阶段像素级处理
        try:
            for part in doc.part.package.parts:
                pn = str(part.partname)
                if is_docx_ocr_media_part(pn, part.blob):
                    try:
                        items = self._ocr_engine.ocr(part.blob)
                        for it in items:
                            text = it["text"].strip()
                            if not text:
                                continue
                            xs = [p[0] for p in it["box"]]
                            ys = [p[1] for p in it["box"]]
                            box = [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]
                            loc = f"嵌入图像({Path(pn).name})"
                            for cand in self.detector.detect_text(text, location=loc):
                                cand.source = "ocr"
                                cand.box = box
                                cand.image_part = pn
                                scan.candidates.append(cand)
                        if items:
                            scan.warnings.append("文档含嵌入图像，已 OCR 检测；图像内敏感文字将进入删除流程")
                    except Exception as e:
                        scan.warnings.append(f"嵌入图像 OCR 失败：{e}")
                        scan.review_flags.append("docx_embedded_image_uncheckable")
        except Exception as e:
            scan.warnings.append(f"嵌入图像检查失败：{e}")
            scan.review_flags.append("docx_embedded_image_uncheckable")
        return scan

    def process(self, path: str, out_path: str, candidates: list) -> list[str]:
        warnings: list[str] = []
        unsafe_parts = _unsafe_docx_parts(path)
        if unsafe_parts:
            raise ValueError(
                f"DOCX 含无法检查的嵌入对象(OLE)，已阻止导出：{unsafe_parts[0]}")
        doc = Document(path)
        # 文本候选：段落/表格/页眉页脚/文本框
        text_cands = [c for c in candidates if not c.image_part]
        for root in self._roots(doc):
            for p in list(_iter_paragraphs(root)):
                _replace_in_paragraph(p, text_cands)
            for dt in root.findall(".//" + _W_DELTEXT):
                dt.text = ""
        try:
            for part in doc.part.package.parts:
                if "comments" in str(part.partname).lower():
                    for t in part.element.iter(_W_T):
                        t.text = ""
        except Exception:
            pass
        # 嵌入图像候选：delete 时对图像做像素级删除
        for c in candidates:
            if c.image_part and c.delete:
                if not self._redact_image_part(doc, c.image_part, c.box):
                    warnings.append(f"嵌入图像 {Path(c.image_part).name} 删除失败：{c.category}")
        self._clear_metadata(doc)
        doc.save(out_path)
        self._redact_footnotes(out_path, text_cands)
        return warnings

    def _redact_image_part(self, doc, partname: str, box) -> bool:
        try:
            for part in doc.part.package.parts:
                if str(part.partname) == partname:
                    img = Image.open(io.BytesIO(part.blob))
                    arr = np.asarray(img.convert("RGB")).copy()
                    if box and len(box) == 4:
                        x0, y0, x1, y1 = box
                        pad = 4
                        x0 = max(0, int(x0) - pad)
                        y0 = max(0, int(y0) - pad)
                        x1 = min(arr.shape[1], int(x1) + pad)
                        y1 = min(arr.shape[0], int(y1) + pad)
                        arr[y0:y1, x0:x1] = (0, 0, 0)
                    else:
                        arr[:] = (0, 0, 0)
                    fmt = img.format or "PNG"
                    buf = io.BytesIO()
                    Image.fromarray(arr).save(buf, "PNG" if fmt == "PNG" else "JPEG", quality=95)
                    part._blob = buf.getvalue()
                    return True
            return False
        except Exception:
            return False

    def _redact_footnotes(self, out_path, candidates):
        replacements = [(c.text, c.replacement if c.replacement is not None else "****") for c in candidates if c.text]
        try:
            with zipfile.ZipFile(out_path, "r") as zin:
                if not any(_is_footnote_name(n) for n in zin.namelist()):
                    return
            tmp = str(out_path) + ".tmp"
            with zipfile.ZipFile(out_path, "r") as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if _is_footnote_name(item.filename):
                        data = self._redact_footnote_xml(data, replacements)
                    zout.writestr(item, data)
            shutil.move(tmp, str(out_path))
        except Exception:
            pass

    @staticmethod
    def _redact_footnote_xml(data: bytes, replacements):
        try:
            root = etree.fromstring(data)
            for t in root.iter("{%s}t" % _W_NS):
                if t.text:
                    for old, new in replacements:
                        if old in t.text:
                            t.text = t.text.replace(old, new)
            return etree.tostring(root, encoding="utf-8", xml_declaration=True)
        except Exception:
            return data

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
                pn = str(part.partname).lower()
                if pn.endswith("/app.xml"):
                    for el in part.element.iter():
                        if el.tag.split("}")[-1] in ("Company", "Manager", "Application"):
                            el.text = ""
                elif "custom" in pn and pn.endswith(".xml"):
                    for el in part.element.iter():
                        if el.tag.split("}")[-1] in ("lpwstr", "lpstr", "i4", "r8", "bool", "filetime"):
                            el.text = ""
        except Exception:
            pass
