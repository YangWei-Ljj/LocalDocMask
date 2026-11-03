"""XLSX/XLSM 处理器：单元格/公式/批注/超链接/隐藏内容处理，宏与外部链接失败关闭。"""
from __future__ import annotations

import zipfile

import openpyxl

from ...domain.entities import ScanResult
from .base import FormatHandler


def _has_macro(path: str) -> bool:
    with zipfile.ZipFile(path) as z:
        return any(n.endswith("vbaProject.bin") for n in z.namelist())


def _has_external_links(path: str) -> bool:
    with zipfile.ZipFile(path) as z:
        return any("/externalLinks/" in n for n in z.namelist())


def _has_embeddings(path: str) -> bool:
    with zipfile.ZipFile(path) as z:
        return any("/embeddings/" in n.lower() for n in z.namelist())


class XlsxHandler(FormatHandler):
    name = "xlsx"
    extensions = [".xlsx", ".xlsm"]

    def extract(self, path: str) -> ScanResult:
        scan = ScanResult()
        if not zipfile.is_zipfile(path):
            raise ValueError("表格文件损坏或不是有效的 XLSX/XLSM 文档")
        if _has_macro(path):
            scan.warnings.append("文件包含宏(VBA)，宏代码无法可靠脱敏，将禁止导出")
        if _has_external_links(path):
            scan.warnings.append("文件包含外部链接，存在无法检查的内容，将禁止导出")
        if _has_embeddings(path):
            scan.warnings.append("文件包含嵌入对象(OLE)，内容无法检查，将禁止导出")
        wb = openpyxl.load_workbook(path, data_only=False, keep_vba=False)
        texts: list[str] = []
        for ws in wb.worksheets:
            state = ws.sheet_state
            hidden = "(隐藏)" if state != "visible" else ""
            for row in ws.iter_rows():
                for cell in row:
                    v = cell.value
                    if v is None:
                        continue
                    s = str(v)
                    texts.append(s)
                    loc = f"{ws.title}!{cell.coordinate}{hidden}"
                    for cand in self.detector.detect_text(s, location=loc):
                        scan.candidates.append(cand)
                    try:
                        if cell.comment is not None and cell.comment.text:
                            for cand in self.detector.detect_text(cell.comment.text, location=loc + "(批注)"):
                                scan.candidates.append(cand)
                    except Exception:
                        pass
        scan.text = "\n".join(texts)
        wb.close()
        return scan

    def process(self, path: str, out_path: str, candidates: list) -> list[str]:
        warnings: list[str] = []
        if _has_macro(path):
            raise ValueError("文件包含宏(VBA)，无法可靠脱敏宏代码，已阻止导出")
        if _has_external_links(path):
            raise ValueError("文件包含外部链接，存在无法检查的内容，已阻止导出")
        if _has_embeddings(path):
            raise ValueError("文件包含嵌入对象(OLE)，内容无法检查，已阻止导出")
        wb = openpyxl.load_workbook(path, data_only=False, keep_vba=False)
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    v = cell.value
                    if v is None:
                        continue
                    if isinstance(v, str):
                        for cand in candidates:
                            if cand.text and cand.text in v:
                                v = v.replace(cand.text, cand.replacement if cand.replacement is not None else "****")
                        cell.value = v
                    try:
                        cell.comment = None
                    except Exception:
                        pass
                    try:
                        cell.hyperlink = None
                    except Exception:
                        pass
            try:
                del ws._images[:]
            except Exception:
                pass
        wb.save(out_path)
        wb.close()
        return warnings
