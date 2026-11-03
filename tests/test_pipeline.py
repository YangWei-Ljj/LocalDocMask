# -*- coding: utf-8 -*-
from pathlib import Path

import pymupdf
from docx import Document
from openpyxl import load_workbook
from openpyxl.comments import Comment
from PIL import Image, ImageDraw, ImageFont

from conftest import VALID_ID, luhn_valid
from localdocmask.recognition.rule_based import Detector
from localdocmask.security.hashing import sha256_file
from localdocmask.security.admission import AdmissionError
from localdocmask.application.pipeline import TaskManager

FONT = r"C:\Windows\Fonts\simhei.ttf"
BANK = luhn_valid("6222020200112233")


def _detector():
    return Detector(companies=["字节跳动"], persons=["张三", "李四"])


def _run(tm, src, mode="b"):
    task = tm.prepare(src)
    h0 = task.source_hash
    scan = tm.scan(task)
    tm.process(task, mode, "****", scan.candidates)
    report = tm.audit(task, scan.candidates)
    task = tm.finalize(task, report)
    return task, h0, report


def test_docx_pipeline(tmp_path):
    src = tmp_path / "s.docx"
    d = Document()
    d.add_paragraph(f"甲方：北京字节跳动有限公司 张三 身份证{VALID_ID} 手机13812345678")
    d.add_paragraph(f"银行卡 {BANK}")
    t = d.add_table(rows=1, cols=1)
    t.rows[0].cells[0].text = "邮箱 a@b.com"
    d.sections[0].header.paragraphs[0].text = "张三（经办）"
    d.save(str(src))
    task, h0, report = _run(TaskManager(_detector()), src)
    assert report.passed
    assert task.status.value == "审计通过，可以导出"
    assert sha256_file(src) == h0
    out_text = "\n".join(p.text for p in Document(str(task.output_path)).paragraphs)
    assert "张三" not in out_text
    assert "13812345678" not in out_text
    assert "字节跳动" not in out_text


def test_pdf_pipeline(tmp_path):
    src = tmp_path / "s.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "北京字节跳动有限公司 法人张三", fontsize=12,
                     fontfile=FONT, fontname="china")
    page.insert_text((72, 140), "手机13812345678", fontsize=12, fontfile=FONT, fontname="china")
    doc.save(str(src))
    doc.close()
    task, h0, report = _run(TaskManager(_detector()), src)
    assert report.passed
    assert sha256_file(src) == h0
    d = pymupdf.open(str(task.output_path))
    out = "\n".join(p.get_text() for p in d)
    d.close()
    assert "13812345678" not in out
    assert "张三" not in out


def test_xlsx_pipeline_hidden_sheet(tmp_path):
    import openpyxl
    src = tmp_path / "s.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = f"客户 张三 身份证{VALID_ID}"
    ws["A2"].comment = Comment("备注：手机13812345678", "审")
    ws2 = wb.create_sheet("隐藏")
    ws2.sheet_state = "hidden"
    ws2["A1"] = "机密 李四"
    wb.save(str(src))
    task, h0, report = _run(TaskManager(_detector()), src)
    assert report.passed
    out = openpyxl.load_workbook(str(task.output_path))
    vals = []
    for wso in out.worksheets:
        for row in wso.iter_rows():
            for c in row:
                if c.value is not None:
                    vals.append(str(c.value))
    assert all("张三" not in v and "李四" not in v and "13812345678" not in v for v in vals)


def test_image_pipeline(tmp_path):
    src = tmp_path / "s.png"
    img = Image.new("RGB", (900, 120), "white")
    ImageDraw.Draw(img).text((20, 35), "张三 13812345678", fill="black",
                             font=ImageFont.truetype(FONT, 40))
    img.save(str(src))
    task, h0, report = _run(TaskManager(_detector()), src)
    assert report.passed
    assert sha256_file(src) == h0


def test_audit_catches_residual(tmp_path):
    from localdocmask.security.auditor import Auditor
    out = tmp_path / "residual.docx"
    d = Document()
    d.add_paragraph("这里仍然包含 张三")
    d.save(str(out))
    report = Auditor().audit(str(out), ["张三"])
    assert not report.passed
    assert any(f.verdict.value == "发现残留" for f in report.findings)


def test_source_hash_change_blocks_export(tmp_path):
    src = tmp_path / "s.docx"
    d = Document()
    d.add_paragraph("张三")
    d.save(str(src))
    tm = TaskManager(_detector())
    task = tm.prepare(src)
    tm.scan(task)
    tm.process(task, "a", "****", task.scan.candidates)
    report = tm.audit(task, task.scan.candidates)
    # 人为修改源文件
    d2 = Document()
    d2.add_paragraph("张三 被篡改")
    d2.save(str(src))
    task = tm.finalize(task, report)
    assert task.status.value == "无法确认安全"


def test_unsupported_format(tmp_path):
    src = tmp_path / "s.txt"
    src.write_text("hello", encoding="utf-8")
    tm = TaskManager(_detector())
    try:
        tm.prepare(src)
        assert False, "应当抛出异常"
    except AdmissionError as exc:
        assert exc.reason_code == "unsupported_extension"
