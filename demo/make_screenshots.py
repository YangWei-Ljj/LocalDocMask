# -*- coding: utf-8 -*-
"""offscreen 截取 GUI 演示界面。

- QT_QPA_PLATFORM=offscreen + 临时 LOCALDOCMASK_HOME；
- 第一阶段：载入 good+review 两个文件并扫描，展示三档候选表与脱敏预览；
- 第二阶段：不裁决待复核项直接处理 → 复核门禁阻断（独立审计报告）；
- 第三阶段：清空后载入 good+review+损坏样本，跑 BatchService → 成功/复核/隔离/任务中心；
- 用 QWidget.grab() 保存到 docs/screenshots/。

运行：.venv\\Scripts\\python.exe demo\\make_screenshots.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

HOME = ROOT / "demo" / "_shot_home"
if HOME.exists():
    shutil.rmtree(HOME, ignore_errors=True)
HOME.mkdir(parents=True, exist_ok=True)
os.environ["LOCALDOCMASK_HOME"] = str(HOME)
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QThreadPool  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QTabWidget  # noqa: E402
from PySide6.QtGui import QFont, QFontDatabase  # noqa: E402

import localdocmask.presentation.gui.main_window as mw  # noqa: E402

OUT = ROOT / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)
DEMO = ROOT / "demo" / "data"


def _drain(app: QApplication) -> None:
    QThreadPool.globalInstance().waitForDone(60000)
    for _ in range(20):
        app.processEvents()
    time.sleep(0.3)
    app.processEvents()


def _save(widget, app, name: str) -> None:
    widget.resize(1360, 860)
    widget.show()
    app.processEvents()
    pix = widget.grab()
    pix.save(str(OUT / name))
    print(f"[shot] {name}  {pix.width()}x{pix.height()}")


app = QApplication.instance() or QApplication(sys.argv)
for fp in [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]:
    if Path(fp).exists():
        fid = QFontDatabase.addApplicationFont(fp)
        if fid >= 0:
            fams = QFontDatabase.applicationFontFamilies(fid)
            if fams:
                app.setFont(QFont(fams[0], 10))
                print(f"[font] registered {fp} -> {fams[0]}")
                break

QMessageBox.information = staticmethod(lambda *a, **k: None)  # type: ignore
QMessageBox.warning = staticmethod(lambda *a, **k: None)  # type: ignore
QMessageBox.critical = staticmethod(lambda *a, **k: None)  # type: ignore
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)  # type: ignore

win = mw.MainWindow()
win.resize(1360, 860)
tabs = win.findChild(QTabWidget)

good = DEMO / "业务说明.docx"
review = DEMO / "合同材料.pdf"
bad = DEMO / "损坏样本.docx"

# ============ 阶段 1：扫描两个干净文件，展示三档候选表 ============
win._add_files([str(good), str(review)])
win._scan()
_drain(app)
for t in win.tasks:
    print(f"[after scan] {t.source_path.name}: status={t.status.value} "
          f"cands={len(t.scan.candidates) if t.scan else 0}")
print(f"[after scan] cand_table rows={win.cand_table.rowCount()}")

# 1) 主界面：候选表（三档分流）
_save(win, app, "01_main_window_candidates.png")

# 2) 脱敏前后对比（模式 B）
win.rb_b.setChecked(True)
win._refresh_preview()
_drain(app)
tabs.setCurrentIndex(0)
_save(win, app, "02_preview_before_after.png")

# ============ 阶段 2：选中待复核文件，不裁决 → 复核门禁阻断 ============
win.file_list.selectRow(1)  # 选中 合同材料.pdf（含 needs_review 公开企业 + 图文混排）
_drain(app)
win._process_active()
_drain(app)
tabs.setCurrentIndex(1)  # 独立审计报告
_save(win, app, "03_audit_report_gate_blocked.png")

# ============ 阶段 3：批量跑（含损坏文件）展示分组 ============
win._clear_files()
win._add_files([str(good), str(review), str(bad)])
win._batch()
_drain(app)
for t in win.tasks:
    print(f"[after batch] {t.source_path.name}: status={t.status.value}")

tabs.setCurrentIndex(2)  # 成功结果
_save(win, app, "04_success_table.png")
tabs.setCurrentIndex(3)  # 复核/失败
_save(win, app, "05_review_failed_table.png")
tabs.setCurrentIndex(4)  # 隔离文件
_save(win, app, "06_quarantine_table.png")
win._refresh_history()
_drain(app)
tabs.setCurrentIndex(5)  # 任务中心/历史
_save(win, app, "07_task_center_history.png")

# 回到主界面
tabs.setCurrentIndex(0)
_save(win, app, "08_main_window_final.png")

win.shutdown()
win.close()
app.processEvents()
print("done")
