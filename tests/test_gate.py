# -*- coding: utf-8 -*-
"""导出门禁与原件保护测试（A1/A3）。"""
import os
import stat
import zipfile

import pytest
from docx import Document
from openpyxl import Workbook

from conftest import VALID_ID
from localdocmask.recognition.rule_based import Detector
from localdocmask.security.hashing import sha256_file
from localdocmask.application.pipeline import TaskManager
from localdocmask.domain.enums import TaskStatus
from localdocmask.domain.exceptions import SecurityViolation


def _detector():
    return Detector(companies=["字节跳动"], persons=["张三"])


def _mk_docx(tmp_path):
    src = tmp_path / "s.docx"
    d = Document()
    d.add_paragraph(f"张三 身份证{VALID_ID} 13812345678")
    d.save(str(src))
    return src


def test_source_readonly_pipeline(tmp_path):
    """源文件设为只读仍可处理，且哈希不变（证明程序只读原件）。"""
    src = _mk_docx(tmp_path)
    os.chmod(str(src), stat.S_IREAD)
    h0 = sha256_file(src)
    tm = TaskManager(_detector())
    task = tm.prepare(src)
    tm.scan(task)
    tm.process(task, "a", "****", task.scan.candidates)
    report = tm.audit(task, task.scan.candidates)
    task = tm.finalize(task, report)
    assert sha256_file(src) == h0
    assert task.status == TaskStatus.PASSED
    os.chmod(str(src), stat.S_IWRITE)


def test_repeated_run_hash_stable(tmp_path):
    """同一源文件重复运行两次，哈希始终不变。"""
    src = _mk_docx(tmp_path)
    h0 = sha256_file(src)
    tm = TaskManager(_detector())
    for _ in range(2):
        task = tm.prepare(src)
        tm.scan(task)
        tm.process(task, "a", "****", task.scan.candidates)
        tm.audit(task, task.scan.candidates)
        tm.finalize(task, task.audit)
        assert sha256_file(src) == h0


def test_macro_file_no_export(tmp_path):
    """含宏文件处理失败，不得产生输出。"""
    src = tmp_path / "s.xlsm"
    wb = Workbook()
    wb.active["A1"] = "张三"
    wb.save(str(src))
    with zipfile.ZipFile(str(src), "a") as z:
        z.writestr("xl/vbaProject.bin", b"dummy-vba")
    tm = TaskManager(_detector())
    task = tm.prepare(src)
    tm.scan(task)
    try:
        tm.process(task, "a", "****", task.scan.candidates)
        assert False, "应当阻止导出"
    except ValueError:
        pass
    assert task.output_path is None or not task.output_path.exists()


def test_corrupted_output_blocks(tmp_path):
    """处理产物损坏时，审计判定无法检查，最终阻止导出。"""
    src = _mk_docx(tmp_path)
    tm = TaskManager(_detector())
    task = tm.prepare(src)
    tm.scan(task)
    tm.process(task, "a", "****", task.scan.candidates)
    # 人为破坏输出文件
    task.output_path.write_bytes(b"not a valid docx")
    report = tm.audit(task, task.scan.candidates)
    task = tm.finalize(task, report)
    assert task.status != TaskStatus.PASSED


def test_unsupported_format_no_export(tmp_path):
    src = tmp_path / "s.txt"
    src.write_text("张三", encoding="utf-8")
    tm = TaskManager(_detector())
    try:
        tm.prepare(src)
        assert False
    except SecurityViolation:
        pass


def test_pdf_raw_content_stream_no_residual(tmp_path):
    """PDF 内容流原始字节中不得残留敏感文本（不是仅覆盖）。"""
    import pymupdf
    src = tmp_path / "s.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "张三 13812345678", fontsize=12,
                     fontfile=r"C:\\Windows\\Fonts\\simhei.ttf", fontname="china")
    doc.save(str(src))
    doc.close()
    tm = TaskManager(_detector())
    task = tm.prepare(src)
    tm.scan(task)
    tm.process(task, "a", "****", task.scan.candidates)
    report = tm.audit(task, task.scan.candidates)
    assert report.passed
    out = pymupdf.open(str(task.output_path))
    raw = b""
    for p in out:
        try:
            raw += p.read_contents()
        except Exception:
            pass
    out.close()
    assert "13812345678".encode() not in raw
    assert "张三".encode() not in raw


def test_encrypted_pdf_rejected(tmp_path):
    import pymupdf
    src = tmp_path / "enc.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "secret")
    doc.save(str(src), encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    doc.close()
    tm = TaskManager(_detector())
    with pytest.raises(SecurityViolation, match="加密"):
        tm.prepare(src)
