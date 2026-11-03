# -*- coding: utf-8 -*-
"""GUI 自动化冒烟测试：MainWindow 通过组合根接入真实后端闭环。

- offscreen 平台 + 临时 LOCALDOCMASK_HOME，不触碰项目 data/；
- 合成 DOCX/PDF/XLSX/PNG，覆盖三档决策、复核门禁、确认后导出、
  原件哈希不变、失败隔离、批量分组与取消；
- QMessageBox 弹窗一律 patch 为 no-op，避免模态阻塞。
"""
import os
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from docx import Document
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from PySide6.QtCore import QEvent, Qt, QThreadPool
from PySide6.QtWidgets import QApplication, QMessageBox

import localdocmask.presentation.gui.main_window as mw
from localdocmask.domain.entities import Task
from localdocmask.domain.enums import EntityDecision, TaskStatus
from localdocmask.infrastructure.config.settings import Settings
from localdocmask.security.hashing import sha256_file

FONT = r"C:\Windows\Fonts\simhei.ttf"
VALID_ID = "11010519491231002X"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def win(qapp, tmp_path, monkeypatch):
    """全新 MainWindow：数据目录隔离到 tmp_path，弹窗静默。"""
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setenv("LOCALDOCMASK_HOME", str(tmp_path))
    window = mw.MainWindow()
    yield window
    window.shutdown()
    window.close()
    window.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()


def _drain():
    QThreadPool.globalInstance().waitForDone(60000)
    QApplication.sendPostedEvents(None, 0)
    QApplication.processEvents()


# ---------------------------------------------------------------------------
# 合成文件
# ---------------------------------------------------------------------------
def _docx(path: Path, paragraphs: list[str]) -> Path:
    d = Document()
    for p in paragraphs:
        d.add_paragraph(p)
    d.save(str(path))
    return path


def _pdf(path: Path, lines: list[str]) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 80
    for line in lines:
        page.insert_text((72, y), line, fontsize=12, fontfile=FONT, fontname="china")
        y += 24
    doc.save(str(path))
    doc.close()
    return path


def _xlsx(path: Path, cells: dict[str, str]) -> Path:
    wb = Workbook()
    ws = wb.active
    for addr, val in cells.items():
        ws[addr] = val
    wb.save(str(path))
    return path


def _png(path: Path, text: str) -> Path:
    img = Image.new("RGB", (900, 120), "white")
    ImageDraw.Draw(img).text((20, 35), text, fill="black",
                             font=ImageFont.truetype(FONT, 36))
    img.save(str(path))
    return path


MIXED_PARAGRAPHS = [
    "本公司产品已上线，我们公司继续推进，本集团保持稳定，我们银行服务客户。",
    "公开企业：中国石油股份有限公司。",
    f"手机13812345678 邮箱a@b.com 身份证{VALID_ID}",
    "内部供应商：杭州云启科技有限公司。项目代号-苍梧。",
]


def _row_by_text(window, needle: str) -> int:
    for r in range(window.cand_table.rowCount()):
        candidate = window._row_candidate(r)
        if candidate is not None and needle in candidate.text:
            return r
        it = window.cand_table.item(r, 4)
        if it is not None and needle in it.text():
            return r
    return -1


def test_add_files_button_opens_dialog_without_checked_flag(win, monkeypatch):
    home = Path(os.environ["LOCALDOCMASK_HOME"])
    src = _docx(home / "button-add.docx", ["手机13812345678"])
    monkeypatch.setattr(
        mw.QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(src)], ""))
    win.btn_add.click()
    QApplication.processEvents()
    assert win.file_list.rowCount() == 1
    assert [task.source_path for task in win.tasks] == [src]


# ---------------------------------------------------------------------------
# 1. 添加 + 扫描：三档决策与默认勾选
# ---------------------------------------------------------------------------
def test_scan_three_tier_table(win):
    home = Path(os.environ["LOCALDOCMASK_HOME"])
    src = _docx(home / "mixed.docx", MIXED_PARAGRAPHS)
    win._add_files([str(src)])
    assert win.file_list.rowCount() == 1

    win._scan()
    _drain()

    task = win._active_task()
    assert task is not None and task.scan is not None
    assert win.cand_table.rowCount() >= 5

    # AUTO_CONFIRM：手机默认勾选
    r_phone = _row_by_text(win, "13812345678")
    assert r_phone >= 0
    assert win.cand_table.item(r_phone, 0).checkState() == Qt.Checked
    assert "当前:自动确认" in win.cand_table.item(r_phone, 1).text()

    # NEEDS_REVIEW：公开企业默认不勾选、待复核
    r_oil = _row_by_text(win, "中国石油股份有限公司")
    assert r_oil >= 0
    assert win.cand_table.item(r_oil, 0).checkState() == Qt.Unchecked
    assert "当前:待复核" in win.cand_table.item(r_oil, 1).text()

    # SUPPRESS：泛指引用默认不勾选、已抑制
    r_supp = _row_by_text(win, "我们公司")
    assert r_supp >= 0
    assert win.cand_table.item(r_supp, 0).checkState() == Qt.Unchecked
    assert "当前:已抑制" in win.cand_table.item(r_supp, 1).text()

    # 候选对象上的三档标记与表格一致
    cands = {c.text: c for c in task.scan.candidates}
    assert cands["13812345678"].decision == EntityDecision.AUTO_CONFIRM
    assert cands["中国石油股份有限公司"].decision == EntityDecision.NEEDS_REVIEW
    assert cands["中国石油股份有限公司"].confirmed is False
    assert cands["中国石油股份有限公司"].excluded is False
    assert cands["我们公司"].excluded is True

    # 模式 B 预览使用稳定虚构值：黑名单公司被替换、原文消失
    win.rb_b.setChecked(True)
    win._refresh_preview()
    assert "杭州云启科技有限公司" not in win.preview_new.toPlainText()
    assert "本公司产品" in win.preview_orig.toPlainText()


# ---------------------------------------------------------------------------
# 2. 未裁决即处理 → 复核门禁阻断；显式确认后成功导出
# ---------------------------------------------------------------------------
def test_process_gate_blocks_then_passes(win):
    home = Path(os.environ["LOCALDOCMASK_HOME"])
    src = _docx(home / "gate.docx", MIXED_PARAGRAPHS)
    h_before = sha256_file(src)
    win._add_files([str(src)])
    win._scan()
    _drain()

    # 不裁决任何待复核项直接处理 → 门禁阻断
    win._process_active()
    _drain()
    task = win._active_task()
    assert task.status == TaskStatus.NEEDS_REVIEW, task.error
    assert win.success_table.rowCount() == 0
    assert win.review_table.rowCount() >= 1

    # 显式确认待复核行后处理 → PASSED
    r_oil = _row_by_text(win, "中国石油股份有限公司")
    assert r_oil >= 0
    win._set_candidates_decision([r_oil], confirmed=True)
    win._process_active()
    _drain()

    task = win._active_task()
    assert task.status == TaskStatus.PASSED, task.error
    assert task.output_path and Path(task.output_path).exists()
    # 原件哈希不变、输出可打开
    assert sha256_file(src) == h_before
    Document(str(task.output_path))
    # 成功表有行、复核表不再含该文件的待复核行
    assert win.success_table.rowCount() >= 1
    assert "中国石油" not in "\n".join(
        p.text for p in Document(str(task.output_path)).paragraphs)


# ---------------------------------------------------------------------------
# 3. 排除/放行语义
# ---------------------------------------------------------------------------
def test_exclude_and_confirm_buttons(win):
    home = Path(os.environ["LOCALDOCMASK_HOME"])
    src = _docx(home / "decide.docx", MIXED_PARAGRAPHS)
    win._add_files([str(src)])
    win._scan()
    _drain()

    # 把公开企业排除/放行
    r_oil = _row_by_text(win, "中国石油股份有限公司")
    win.cand_table.selectRow(r_oil)
    win._cand_exclude()
    c_oil = win._row_candidate(r_oil)
    assert c_oil.excluded is True and c_oil.confirmed is False
    assert "当前:已放行" in win.cand_table.item(r_oil, 1).text()

    # 处理：无未裁决项 → PASSED（公开企业被放行、未脱敏）
    win._process_active()
    _drain()
    task = win._active_task()
    assert task.status == TaskStatus.PASSED, task.error
    # 放行的原文仍在输出中
    assert "中国石油股份有限公司" in "\n".join(
        p.text for p in Document(str(task.output_path)).paragraphs)


# ---------------------------------------------------------------------------
# 4. 批量路径：passed / review / failed 分组 + 失败隔离
# ---------------------------------------------------------------------------
def test_batch_grouping_and_isolation(win):
    home = Path(os.environ["LOCALDOCMASK_HOME"])
    good = _docx(home / "good.docx", [f"手机13811112222 邮箱ok@x.com"])
    review = _docx(home / "review.docx",
                   ["公开企业：中国石油股份有限公司。", f"手机13833334444"])
    bad = home / "bad.docx"
    bad.write_bytes(b"this is not a real docx")
    win._add_files([str(good), str(review)])
    # 当前产品会在选择时拒绝伪造 DOCX；此用例仍验证批处理对已排队坏任务的隔离。
    win.tasks.append(Task(source_path=bad))
    win._refresh_file_list()

    win._batch()
    _drain()

    names = {t.source_path.name: t for t in win.tasks}
    assert set(names) == {"good.docx", "review.docx", "bad.docx"}
    assert names["good.docx"].status == TaskStatus.PASSED, names["good.docx"].error
    assert names["review.docx"].status == TaskStatus.NEEDS_REVIEW
    assert names["bad.docx"].status == TaskStatus.AUDIT_FAILED
    # 成功产物真实存在；review/failed 互不污染
    assert Path(names["good.docx"].output_path).exists()
    # 任务中心/历史已落库；坏文件以失败终态进入批次结果与历史。
    assert win.history_table.rowCount() >= 2


# ---------------------------------------------------------------------------
# 5. 取消：预取消 token 立即终止批次，已完成文件保留
# ---------------------------------------------------------------------------
def test_batch_cancel(win, monkeypatch):
    home = Path(os.environ["LOCALDOCMASK_HOME"])
    f1 = _docx(home / "c1.docx", [f"手机13800000001"])
    f2 = _docx(home / "c2.docx", [f"手机13800000002"])

    class PreCanceled(mw.CancelToken):
        def __init__(self):
            super().__init__()
            self.cancel()

    monkeypatch.setattr(mw, "CancelToken", PreCanceled)
    win._add_files([str(f1), str(f2)])
    win._batch()
    _drain()

    # 预取消：不静默删除已添加任务，任务保持未处理并记录取消原因
    assert len(win.tasks) == 2
    assert all(t.status == TaskStatus.PENDING for t in win.tasks)
    assert all("批次已取消" in t.error for t in win.tasks)
    assert "取消" in win.audit_view.toPlainText()
    assert win.services is not None


# ---------------------------------------------------------------------------
# 6. 多格式接入（DOCX/PDF/XLSX/PNG）走通扫描
# ---------------------------------------------------------------------------
def test_multi_format_scan(win):
    home = Path(os.environ["LOCALDOCMASK_HOME"])
    files = [
        _docx(home / "m.docx", [f"手机13812345678 邮箱d@e.com"]),
        _pdf(home / "m.pdf", [f"手机13812345678 身份证{VALID_ID}"]),
        _xlsx(home / "m.xlsx", {"A1": f"手机13812345678 身份证{VALID_ID}"}),
        _png(home / "m.png", "13812345678"),
    ]
    win._add_files([str(f) for f in files])
    assert win.file_list.rowCount() == 4
    win._scan()
    _drain()
    # 每个任务至少识别出一个候选（PNG 走 OCR）
    for t in win.tasks:
        assert t.scan is not None and t.scan.candidates, t.source_path.name


def test_gui_default_export_name_is_neutral_and_user_choice_wins(
        win, tmp_path, monkeypatch):
    neutral_output = tmp_path / "masked_task42.docx"
    neutral_output.write_bytes(b"masked")
    task = Task(
        source_path=tmp_path / "张三_云启公司_合同.docx",
        task_id="task42", file_type="docx", status=TaskStatus.PASSED,
        output_path=neutral_output, final_hash=sha256_file(neutral_output),
    )
    win.tasks = [task]
    win._refresh_file_list()

    captured = []
    user_target = tmp_path / "用户自定义名称.docx"
    responses = iter([("", ""), (str(user_target), "")])

    def fake_dialog(parent, title, default, filt):
        captured.append(default)
        return next(responses)

    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName", fake_dialog)

    class FakePipeline:
        def export_gate_reasons(self, selected):
            assert selected is task
            return []

        def export_to_location(self, selected, destination, **kwargs):
            assert selected is task
            captured.append(Path(destination))
            return Path(destination)

    win.services = SimpleNamespace(
        pipeline=FakePipeline(), settings=Settings(), close=lambda: None)
    win._export_to_location()
    assert captured[0] == str(Path.home() / "masked_task42.docx")
    assert "张三" not in captured[0] and "云启公司" not in captured[0]

    win._export_to_location()
    _drain()
    assert captured[-1] == user_target
