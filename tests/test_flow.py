# -*- coding: utf-8 -*-
"""场景 A~H：逐项处理、失败删除、隔离、定位、哈希绑定。"""
from pathlib import Path

import pymupdf
from docx import Document
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont

from conftest import VALID_ID
from localdocmask.infrastructure.config.settings import SUCCESS_DIR
from localdocmask.recognition.rule_based import Detector
from localdocmask.security.hashing import sha256_file
from localdocmask.security.admission import AdmissionError
from localdocmask.domain.enums import TaskStatus
from localdocmask.application.pipeline import TaskManager

FONT = r"C:\Windows\Fonts\simhei.ttf"


def _det():
    return Detector(companies=["字节跳动"], persons=["张三", "李四", "王五"])


def _mk_image(path, text, size=(600, 100)):
    img = Image.new("RGB", size, "white")
    ImageDraw.Draw(img).text((20, 30), text, fill="black", font=ImageFont.truetype(FONT, 40))
    img.save(str(path))


def _run(tm, src, mode="a"):
    task = tm.prepare(src)
    tm.scan(task)
    tm.process(task, mode, "****", task.scan.candidates)
    tm.finalize(task, task.audit)
    return task


def test_A_all_redact_success(tmp_path):
    src = tmp_path / "a.docx"
    d = Document(); d.add_paragraph(f"张三 {VALID_ID} 13812345678"); d.save(str(src))
    task = _run(TaskManager(_det()), src)
    assert task.status == TaskStatus.PASSED
    assert task.output_path.parent == SUCCESS_DIR
    assert task.deleted_count == 0
    assert all(r["final_disposition"] == "success" for r in task.records)


def test_B_partial_redact_and_delete(tmp_path):
    src = tmp_path / "b.docx"
    d = Document()
    d.add_paragraph(f"联系人 张三 电话 13812345678 身份证 {VALID_ID}")
    png = tmp_path / "s.png"
    _mk_image(png, "李四 13912345678")
    d.add_picture(str(png))
    d.save(str(src))
    task = _run(TaskManager(_det()), src)
    assert task.status == TaskStatus.PASSED
    assert task.redacted_count == 3, task.records
    assert task.deleted_count == 2, task.records
    deleted = [r for r in task.records if r["final_disposition"] == "deleted"]
    assert len(deleted) == 2
    assert all("嵌入图像" in r["source_location"] for r in deleted)
    assert all(r["deletion_verified"] for r in deleted)


def test_C_deletion_fails_quarantine(tmp_path):
    src = tmp_path / "c.docx"
    d = Document(); d.add_paragraph("联系人 张三")
    png = tmp_path / "c.png"
    _mk_image(png, "李四")
    d.add_picture(str(png)); d.save(str(src))
    tm = TaskManager(_det())
    task = tm.prepare(src)
    tm.scan(task)
    for c in task.scan.candidates:
        if c.image_part:
            c.image_part = "/word/media/nonexistent.png"  # 使删除定位失败
    tm.process(task, "a", "****", task.scan.candidates)
    tm.finalize(task, task.audit)
    assert task.status != TaskStatus.PASSED
    assert any(r["final_disposition"] == "unresolved" for r in task.records)


def test_D_residual_after_delete_quarantine(tmp_path):
    png = tmp_path / "d.png"
    _mk_image(png, "张三", size=(500, 100))
    tm = TaskManager(_det())
    task = tm.prepare(png)
    tm.scan(task)
    for c in task.scan.candidates:
        c.box = [400, 80, 420, 95]  # 错误区域，删除后仍残留
    tm.process(task, "a", "****", task.scan.candidates)
    tm.finalize(task, task.audit)
    assert task.status != TaskStatus.PASSED
    assert any(r["final_disposition"] == "unresolved" for r in task.records)


def test_E_batch_isolation(tmp_path):
    tm = TaskManager(_det())
    # A 完全成功
    a = tmp_path / "a.docx"
    d = Document(); d.add_paragraph("张三 13812345678"); d.save(str(a))
    ta = tm.prepare(a); tm.scan(ta); tm.process(ta, "a", "****", ta.scan.candidates); tm.finalize(ta, ta.audit)
    assert ta.status == TaskStatus.PASSED
    # B 部分成功、部分删除验证通过
    b = tmp_path / "b.docx"
    d = Document(); d.add_paragraph("联系人 张三")
    png = tmp_path / "b.png"; _mk_image(png, "李四")
    d.add_picture(str(png)); d.save(str(b))
    tb = tm.prepare(b); tm.scan(tb); tm.process(tb, "a", "****", tb.scan.candidates); tm.finalize(tb, tb.audit)
    assert tb.status == TaskStatus.PASSED
    assert tb.deleted_count >= 1
    # C 无法安全删除 → 隔离
    c = tmp_path / "c.docx"
    d = Document(); d.add_paragraph("联系人 张三")
    png2 = tmp_path / "c.png"; _mk_image(png2, "王五")
    d.add_picture(str(png2)); d.save(str(c))
    tc = tm.prepare(c); tm.scan(tc)
    for cand in tc.scan.candidates:
        if cand.image_part:
            cand.image_part = "/word/media/nonexistent.png"
    tm.process(tc, "a", "****", tc.scan.candidates); tm.finalize(tc, tc.audit)
    assert tc.status != TaskStatus.PASSED
    # D 格式不支持
    bad = tmp_path / "d.txt"; bad.write_text("张三", encoding="utf-8")
    try:
        tm.prepare(bad); bad_status = None
    except AdmissionError as exc:
        assert exc.reason_code == "unsupported_extension"
        bad_status = "格式不支持"
    assert bad_status == "格式不支持"


def test_F_source_locations(tmp_path):
    tm = TaskManager(_det())
    src = tmp_path / "f.xlsx"
    wb = Workbook(); ws = wb.active; ws.title = "客户"; ws["D18"] = "张三"; wb.save(str(src))
    t = tm.prepare(src); tm.scan(t)
    assert any("客户!D18" in c.location for c in t.scan.candidates)
    src2 = tmp_path / "f.pdf"
    doc = pymupdf.open(); page = doc.new_page()
    page.insert_text((72, 100), "张三", fontsize=12, fontfile=FONT, fontname="china")
    doc.save(str(src2)); doc.close()
    t2 = tm.prepare(src2); tm.scan(t2)
    assert any("第 1 页" in c.location for c in t2.scan.candidates)
    src3 = tmp_path / "f.docx"
    d = Document(); d.add_paragraph("张三"); d.save(str(src3))
    t3 = tm.prepare(src3); tm.scan(t3)
    assert any("第 1 段" in c.location for c in t3.scan.candidates)
    src4 = tmp_path / "f.png"
    _mk_image(src4, "张三", size=(400, 100))
    t4 = tm.prepare(src4); tm.scan(t4)
    assert any("OCR 区域" in c.location for c in t4.scan.candidates)


def test_G_source_hash_unchanged(tmp_path):
    src = tmp_path / "g.docx"
    d = Document(); d.add_paragraph("张三 13812345678"); d.save(str(src))
    h0 = sha256_file(src)
    task = _run(TaskManager(_det()), src)
    assert task.status == TaskStatus.PASSED
    assert sha256_file(src) == h0


def test_H_final_hash_binding(tmp_path):
    src = tmp_path / "h.docx"
    d = Document(); d.add_paragraph("张三"); d.save(str(src))
    tm = TaskManager(_det())
    task = tm.prepare(src)
    tm.scan(task)
    tm.process(task, "a", "****", task.scan.candidates)
    task.output_path.write_bytes(b"tampered")
    tm.finalize(task, task.audit)
    assert task.status == TaskStatus.UNSAFE
