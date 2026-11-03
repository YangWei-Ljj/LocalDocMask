"""PDF 处理器：文本/扫描页区域删除 + 元数据/附件/批注清除。"""
from __future__ import annotations

import os

import pymupdf
from PIL import Image

from ..ocr import OCREngine
from ..models import ScanResult
from .base import FormatHandler

_CJK_FONT = r"C:\Windows\Fonts\simhei.ttf"


def _strict_contains(outer: pymupdf.Rect, inner: pymupdf.Rect) -> bool:
    if not (outer.x0 <= inner.x0 + 0.5 and outer.x1 >= inner.x1 - 0.5
            and outer.y0 <= inner.y0 + 0.5 and outer.y1 >= inner.y1 - 0.5):
        return False
    return (outer.x0 < inner.x0 - 0.5 or outer.x1 > inner.x1 + 0.5
            or outer.y0 < inner.y0 - 0.5 or outer.y1 > inner.y1 + 0.5)


class PdfHandler(FormatHandler):
    name = "pdf"
    extensions = [".pdf"]

    def extract(self, path: str) -> ScanResult:
        scan = ScanResult()
        doc = pymupdf.open(path)
        if doc.needs_pass:
            raise ValueError("PDF 已加密，无法读取（需要密码）")
        full_text: list[str] = []
        for pno in range(doc.page_count):
            page = doc[pno]
            t = page.get_text()
            full_text.append(t)
            scan.candidates.extend(self.detector.detect_text(t, location=f"第 {pno + 1} 页"))
            n_images = len(page.get_images(full=True))
            if n_images > 0 and len(t.strip()) < 40:
                self._ocr_page(doc, pno, scan)
            elif n_images > 0:
                scan.warnings.append(f"第 {pno + 1} 页为图文混排，嵌入图像内容需人工复核")
        scan.text = "\n".join(full_text)
        if doc.page_count == 0:
            scan.warnings.append("PDF 无页面内容")
        doc.close()
        return scan

    def _ocr_page(self, doc, pno: int, scan: ScanResult) -> None:
        page = doc[pno]
        scale = 200 / 72.0
        try:
            pix = page.get_pixmap(dpi=200)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            for it in OCREngine.ocr(img):
                text = it["text"].strip()
                if not text:
                    continue
                xs = [p[0] for p in it["box"]]
                ys = [p[1] for p in it["box"]]
                box_pdf = [min(xs) / scale, min(ys) / scale, max(xs) / scale, max(ys) / scale]
                for cand in self.detector.detect_text(text, location=f"第 {pno + 1} 页(OCR)"):
                    cand.source = "ocr"
                    cand.page = pno
                    cand.box = box_pdf
                    scan.candidates.append(cand)
        except Exception as e:
            scan.warnings.append(f"第 {pno + 1} 页 OCR 失败：{e}")

    def process(self, path: str, out_path: str, candidates: list) -> list[str]:
        warnings: list[str] = []
        doc = pymupdf.open(path)
        if doc.needs_pass:
            raise ValueError("PDF 已加密，无法处理")
        use_cjk = os.path.exists(_CJK_FONT)
        try:
            items: dict[int, list] = {}
            for cand in candidates:
                if cand.excluded:
                    continue
                repl = cand.replacement or "****"
                is_cjk = any(ord(ch) > 127 for ch in repl)
                rects: list[tuple[int, pymupdf.Rect]] = []
                if cand.box is not None and cand.page >= 0:
                    x0, y0, x1, y1 = cand.box
                    rects.append((cand.page, pymupdf.Rect(x0, y0, x1, y1)))
                else:
                    for pno in range(doc.page_count):
                        for r in doc[pno].search_for(cand.text):
                            rects.append((pno, r))
                if not rects:
                    warnings.append(f"未能在 PDF 中定位：{cand.category}「{cand.text}」，需人工复核")
                    continue
                for pno, r in rects:
                    items.setdefault(pno, []).append((r, repl, is_cjk))
            # 去重：精确重复 + 包含关系
            for pno in items:
                lst = items[pno]
                seen = set()
                uniq = []
                for (r, repl, is_cjk) in lst:
                    key = (round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1), repl)
                    if key in seen:
                        continue
                    seen.add(key)
                    uniq.append((r, repl, is_cjk))
                lst = [it for it in uniq
                       if not any(_strict_contains(o[0], it[0]) for o in uniq)]
                items[pno] = lst
            for pno in range(doc.page_count):
                page = doc[pno]
                page_items = items.get(pno, [])
                if page_items:
                    for r, _repl, _is_cjk in page_items:
                        page.add_redact_annot(r, cross_out=True, fill=(1, 1, 1))
                    try:
                        page.apply_redactions()
                    except Exception as e:
                        warnings.append(f"第 {pno + 1} 页应用删除失败：{e}")
                for r, repl, is_cjk in page_items:
                    fs = max(6.0, min(r.height * 0.8, 12.0))
                    try:
                        if is_cjk and use_cjk:
                            page.insert_text((r.x0, r.y1 - 1), repl, fontsize=fs,
                                            fontfile=_CJK_FONT, fontname="china")
                        else:
                            page.insert_text((r.x0, r.y1 - 1), repl, fontsize=fs, fontname="helv")
                    except Exception as e:
                        warnings.append(f"第 {pno + 1} 页插入替换文本失败：{e}")
            try:
                doc.set_metadata({})
            except Exception:
                pass
            for i in range(len(doc.embfile_names()) - 1, -1, -1):
                try:
                    doc.embfile_del(i)
                except Exception:
                    pass
            doc.save(out_path, garbage=4, deflate=True, clean=True)
            return warnings
        finally:
            doc.close()
