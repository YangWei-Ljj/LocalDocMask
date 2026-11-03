# -*- coding: utf-8 -*-
"""针对安全缺陷的回归测试（SEC-001 ~ SEC-007）。"""
import shutil
import zipfile

import pymupdf
from docx import Document
from PIL import Image, ImageDraw, ImageFont
from openpyxl import Workbook

from conftest import VALID_ID
from localdocmask.recognition.rule_based import Detector
from localdocmask.infrastructure.llm.ollama import OllamaClient, OllamaError
from localdocmask.application.pipeline import TaskManager
from localdocmask.domain.entities import AuditFinding, AuditReport
from localdocmask.domain.enums import AuditVerdict, TaskStatus

FONT = r"C:\Windows\Fonts\simhei.ttf"


def _detector():
    return Detector(companies=["字节跳动"], persons=["张三", "李四"])


# ---------- SEC-001 本地网络边界 ----------
def test_ollama_rejects_non_loopback():
    try:
        OllamaClient("http://evil.example.com:11434")
        assert False, "应当拒绝非本机地址"
    except OllamaError:
        pass


def test_ollama_session_disables_proxy():
    c = OllamaClient("http://127.0.0.1:11434")
    assert c.session.trust_env is False
    assert c.session.proxies.get("http") is None
    assert c.session.proxies.get("https") is None


# ---------- SEC-002 导出门禁 ----------
def _mk_docx(tmp_path, text="张三"):
    src = tmp_path / "s.docx"
    d = Document()
    d.add_paragraph(text)
    d.save(str(src))
    return src


def test_finalize_rejects_fabricated_report(tmp_path):
    src = _mk_docx(tmp_path)
    tm = TaskManager(_detector())
    task = tm.prepare(src)
    tm.scan(task)
    tm.process(task, "a", "****", task.scan.candidates)
    fake = AuditReport(passed=True, findings=[AuditFinding(AuditVerdict.NO_RESIDUAL, "伪造")])
    task = tm.finalize(task, fake)
    assert task.status == TaskStatus.UNSAFE


def test_finalize_rejects_none_report(tmp_path):
    src = _mk_docx(tmp_path)
    tm = TaskManager(_detector())
    task = tm.prepare(src)
    tm.scan(task)
    tm.process(task, "a", "****", task.scan.candidates)
    task = tm.finalize(task, None)
    assert task.status == TaskStatus.UNSAFE


def test_finalize_rejects_empty_report(tmp_path):
    src = _mk_docx(tmp_path)
    tm = TaskManager(_detector())
    task = tm.prepare(src)
    tm.scan(task)
    tm.process(task, "a", "****", task.scan.candidates)
    report = AuditReport(passed=True, findings=[])
    task.audit = report
    task = tm.finalize(task, report)
    assert task.status == TaskStatus.UNSAFE


# ---------- SEC-003 DOCX 脚注 ----------
def _inject_footnote(docx_path, text):
    """向 docx 注入一个含敏感文本的脚注部件。"""
    import re
    tmp = docx_path.with_suffix(".tmp.docx")
    with zipfile.ZipFile(docx_path, "r") as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "[Content_Types].xml":
                s = data.decode("utf-8")
                s = s.replace("</Types>", '<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/></Types>')
                data = s.encode("utf-8")
            if item.filename == "word/_rels/document.xml.rels":
                s = data.decode("utf-8")
                s = s.replace("</Relationships>", '<Relationship Id="rIdFtn" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/></Relationships>')
                data = s.encode("utf-8")
            zout.writestr(item, data)
        foot = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:footnote w:id="1"><w:p><w:r><w:t>' + text + '</w:t></w:r></w:p></w:footnote>'
                '</w:footnotes>').encode("utf-8")
        zout.writestr("word/footnotes.xml", foot)
    shutil.move(str(tmp), str(docx_path))


def test_docx_footnote_detected_and_redacted(tmp_path):
    src = _mk_docx(tmp_path, "正文内容")
    _inject_footnote(src, "脚注里的张三和身份证" + VALID_ID)
    tm = TaskManager(_detector())
    task = tm.prepare(src)
    scan = tm.scan(task)
    # 脚注内容应被提取并检测到
    assert any("张三" == c.text for c in scan.candidates), "脚注中的姓名未被检测"
    tm.process(task, "a", "****", scan.candidates)
    report = tm.audit(task, scan.candidates)
    task = tm.finalize(task, report)
    assert report.passed, report.summary()
    # 输出脚注中不应再有张三/身份证
    with zipfile.ZipFile(str(task.output_path)) as z:
        foot = z.read("word/footnotes.xml").decode("utf-8")
    assert "张三" not in foot
    assert VALID_ID not in foot


# ---------- SEC-004 DOCX 嵌入图像（删除后导出） ----------
def test_docx_embedded_image_sensitive_deleted(tmp_path):
    import io
    import numpy as np
    from localdocmask.infrastructure.ocr.engine import OCREngine
    src = tmp_path / "img.docx"
    png = tmp_path / "sensitive.png"
    img = Image.new("RGB", (700, 100), "white")
    ImageDraw.Draw(img).text((20, 30), "李四 13812345678", fill="black",
                             font=ImageFont.truetype(FONT, 40))
    img.save(str(png))
    d = Document()
    d.add_paragraph("联系人 张三")
    d.add_picture(str(png))
    d.save(str(src))
    tm = TaskManager(_detector())
    task = tm.prepare(src)
    scan = tm.scan(task)
    img_cands = [c for c in scan.candidates if c.image_part]
    assert img_cands, "嵌入图像敏感内容未被检测"
    tm.process(task, "a", "****", scan.candidates)
    report = tm.audit(task, scan.candidates)
    task = tm.finalize(task, report)
    assert report.passed, report.summary()
    assert task.status == TaskStatus.PASSED
    out = Document(str(task.output_path))
    for part in out.part.package.parts:
        if str(part.partname).lower().startswith("/word/media/"):
            texts = "".join(it["text"] for it in OCREngine.ocr(part.blob))
            assert "李四" not in texts
            assert "13812345678" not in texts


# ---------- SEC-005 XLSX 嵌入对象 ----------
def test_xlsx_embeddings_block(tmp_path):
    src = tmp_path / "s.xlsx"
    wb = Workbook()
    wb.active["A1"] = "张三"
    wb.save(str(src))
    with zipfile.ZipFile(str(src), "a") as z:
        z.writestr("xl/embeddings/oleObject1.bin", b"dummy-ole")
    tm = TaskManager(_detector())
    task = tm.prepare(src)
    tm.scan(task)
    try:
        tm.process(task, "a", "****", task.scan.candidates)
        assert False, "应当阻止导出"
    except ValueError:
        pass


# ---------- SEC-006 PDF 表单字段 ----------
def test_pdf_form_field_blocks(tmp_path):
    src = tmp_path / "s.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    w = pymupdf.Widget()
    w.rect = pymupdf.Rect(72, 72, 200, 92)
    w.field_name = "f1"
    w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    w.field_value = "张三"
    page.add_widget(w)
    doc.save(str(src))
    doc.close()
    tm = TaskManager(_detector())
    task = tm.prepare(src)
    tm.scan(task)
    try:
        tm.process(task, "a", "****", task.scan.candidates)
        assert False, "应当阻止导出"
    except ValueError:
        pass


# ---------- SEC-007 高风险规则补全 ----------
def test_audit_catches_passport_and_landline(tmp_path):
    from localdocmask.security.auditor import Auditor
    out = tmp_path / "r.docx"
    d = Document()
    d.add_paragraph("护照 E12345678 电话 010-12345678")
    d.save(str(out))
    report = Auditor().audit(str(out), [])
    assert not report.passed
    kinds = [f.verdict.value for f in report.findings]
    assert "发现残留" in kinds


# ---------- FIX-01 XLSX XML CRC 审计失败关闭 ----------
def _corrupt_zip_entry_crc(path, member: str):
    data = bytearray(path.read_bytes())
    needle = member.encode("utf-8")
    pos = 0
    found = False
    while True:
        pos = data.find(b"PK\x01\x02", pos)
        if pos < 0:
            break
        name_len = int.from_bytes(data[pos + 28:pos + 30], "little")
        extra_len = int.from_bytes(data[pos + 30:pos + 32], "little")
        comment_len = int.from_bytes(data[pos + 32:pos + 34], "little")
        name_start = pos + 46
        if data[name_start:name_start + name_len] == needle:
            data[pos + 16] ^= 0xFF
            found = True
            break
        pos += 46 + name_len + extra_len + comment_len
    assert found, f"未找到 ZIP 条目：{member}"
    path.write_bytes(data)


def test_auditor_xlsx_corrupt_xml_crc_is_uncheckable(tmp_path):
    from localdocmask.security.auditor import Auditor

    path = tmp_path / "crc.xlsx"
    wb = Workbook()
    wb.active["A1"] = "普通内容"
    wb.save(str(path))
    with zipfile.ZipFile(path) as zf:
        assert "docProps/app.xml" in zf.namelist()
    _corrupt_zip_entry_crc(path, "docProps/app.xml")

    report = Auditor().audit(str(path), [])
    assert report.passed is False
    assert any(f.verdict == AuditVerdict.UNCHECKABLE for f in report.findings), report.summary()


def test_auditor_xlsx_corrupt_xml_residual_terms_uncheckable(tmp_path):
    from localdocmask.security.auditor import Auditor

    path = tmp_path / "crc-residual.xlsx"
    wb = Workbook()
    wb.active["A1"] = "普通内容"
    wb.save(str(path))
    _corrupt_zip_entry_crc(path, "docProps/app.xml")

    residual, uncheckable = Auditor().residual_terms(str(path), ["普通内容"])
    assert residual == []
    assert uncheckable == ["普通内容"]


def test_residual_terms_ocr_failure_is_uncheckable(tmp_path, monkeypatch):
    from localdocmask.security.auditor import Auditor

    image_path = tmp_path / "ocr.png"
    Image.new("RGB", (320, 80), "white").save(image_path, format="PNG")
    path = tmp_path / "ocr.docx"
    d = Document()
    d.add_picture(str(image_path))
    d.save(str(path))

    class FailingEngine:
        def ocr(self, *args, **kwargs):
            raise RuntimeError("synthetic OCR failure")

    auditor = Auditor()
    monkeypatch.setattr(auditor, "_ocr_engine", FailingEngine())
    residual, uncheckable = auditor.residual_terms(str(path), ["敏感词"])
    assert residual == []
    assert uncheckable == ["敏感词"]
