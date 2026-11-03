"""LocalDocMask 桌面界面（PySide6）。"""
from __future__ import annotations

import os
import subprocess
import sys

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QLineEdit, QRadioButton, QButtonGroup, QCheckBox,
    QTextEdit, QListWidget, QTableWidget, QTableWidgetItem, QTabWidget,
    QFileDialog, QMessageBox, QProgressBar, QGroupBox, QHeaderView, QAbstractItemView,
)

from .config import OUTPUT_DIR, ensure_dirs
from .detection import Detector
from .masker import Masker
from .models import Candidate, Task, TaskStatus
from .ollama import OllamaClient
from .task_manager import TaskManager


class _Signals(QObject):
    done = Signal(object)
    error = Signal(str)


class _Worker(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = _Signals()

    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            try:
                self.signals.error.emit(str(e))
            except RuntimeError:
                pass
            return
        try:
            self.signals.done.emit(result)
        except RuntimeError:
            pass


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LocalDocMask - 本地文件脱敏工作台")
        self.resize(1280, 820)
        self.pool = QThreadPool.globalInstance()
        self._workers = []
        self.tasks: list[Task] = []
        self.detector: Detector | None = None
        self.tm: TaskManager | None = None
        self._build_ui()
        self._refresh_ollama()

    # ---------------- UI ----------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        # 左侧控制区
        left = QVBoxLayout()
        left.setSpacing(8)

        g_files = QGroupBox("1. 选择文件")
        vf = QVBoxLayout(g_files)
        row = QHBoxLayout()
        self.btn_add = QPushButton("添加文件")
        self.btn_add.clicked.connect(self._add_files)
        self.btn_clear_files = QPushButton("清空")
        self.btn_clear_files.clicked.connect(self._clear_files)
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_clear_files)
        vf.addLayout(row)
        self.file_list = QTableWidget(0, 2)
        self.file_list.setHorizontalHeaderLabels(["文件", "状态"])
        self.file_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.file_list.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.file_list.itemSelectionChanged.connect(self._on_file_selected)
        vf.addWidget(self.file_list)
        left.addWidget(g_files)

        g_mode = QGroupBox("2. 脱敏模式")
        vm = QVBoxLayout(g_mode)
        self.rb_a = QRadioButton("模式 A：统一掩码（默认彻底删除）")
        self.rb_b = QRadioButton("模式 B：一致性虚构替换（公司/人名）")
        self.rb_a.setChecked(True)
        vm.addWidget(self.rb_a)
        vm.addWidget(self.rb_b)
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("掩码字符："))
        self.mask_edit = QLineEdit("****")
        mrow.addWidget(self.mask_edit)
        vm.addLayout(mrow)
        left.addWidget(g_mode)

        g_cat = QGroupBox("3. 敏感类别与词库")
        vc = QVBoxLayout(g_cat)
        self.chk_company = QCheckBox("公司/企业名（正则）")
        self.chk_company.setChecked(True)
        vc.addWidget(self.chk_company)
        vc.addWidget(QLabel("自定义敏感词（每行一个）："))
        self.custom_words = QTextEdit()
        self.custom_words.setMaximumHeight(60)
        vc.addWidget(self.custom_words)
        vc.addWidget(QLabel("企业名单（每行一个）："))
        self.company_list = QTextEdit()
        self.company_list.setMaximumHeight(60)
        vc.addWidget(self.company_list)
        vc.addWidget(QLabel("人员名单（每行一个）："))
        self.person_list = QTextEdit()
        self.person_list.setMaximumHeight(60)
        vc.addWidget(self.person_list)
        left.addWidget(g_cat)

        g_ai = QGroupBox("4. 本地模型（Ollama）")
        va = QVBoxLayout(g_ai)
        self.ollama_label = QLabel("状态：检测中…")
        va.addWidget(self.ollama_label)
        self.chk_ollama = QCheckBox("使用本地模型辅助识别实体")
        self.chk_ollama.setChecked(True)
        va.addWidget(self.chk_ollama)
        brow = QHBoxLayout()
        self.btn_ollama = QPushButton("刷新状态")
        self.btn_ollama.clicked.connect(self._refresh_ollama)
        brow.addWidget(self.btn_ollama)
        va.addLayout(brow)
        left.addWidget(g_ai)

        g_run = QGroupBox("5. 执行")
        vr = QVBoxLayout(g_run)
        self.btn_scan = QPushButton("① 只读扫描")
        self.btn_scan.clicked.connect(self._scan)
        self.btn_process = QPushButton("② 处理 + 独立审计")
        self.btn_process.clicked.connect(self._process_active)
        self.btn_batch = QPushButton("③ 批量自动处理（全部确认）")
        self.btn_batch.clicked.connect(self._batch)
        self.btn_open = QPushButton("打开通过审计的输出目录")
        self.btn_open.clicked.connect(self._open_output)
        self.btn_clean = QPushButton("清理临时文件")
        self.btn_clean.clicked.connect(self._clean_temp)
        for b in (self.btn_scan, self.btn_process, self.btn_batch, self.btn_open, self.btn_clean):
            vr.addWidget(b)
        left.addWidget(g_run)
        left.addStretch(1)

        # 右侧结果区
        right = QVBoxLayout()
        top = QHBoxLayout()
        self.stage_label = QLabel("就绪")
        top.addWidget(self.stage_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        top.addWidget(self.progress)
        right.addLayout(top)

        g_cand = QGroupBox("6. 敏感候选（可确认 / 排除 / 修正替换值）")
        vcd = QVBoxLayout(g_cand)
        self.cand_table = QTableWidget(0, 7)
        self.cand_table.setHorizontalHeaderLabels(["包含", "类别", "原文", "替换值", "位置", "来源", "置信度"])
        self.cand_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.cand_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        vcd.addWidget(self.cand_table)
        crow = QHBoxLayout()
        for label, fn in (("全选", self._cand_all), ("全不选", self._cand_none),
                          ("排除选中", self._cand_exclude), ("刷新预览", self._refresh_preview)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            crow.addWidget(b)
        vcd.addLayout(crow)
        right.addWidget(g_cand, 3)

        tabs = QTabWidget()
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.audit_view = QTextEdit()
        self.audit_view.setReadOnly(True)
        tabs.addTab(self.preview, "脱敏预览")
        tabs.addTab(self.audit_view, "独立审计报告")
        right.addWidget(tabs, 2)

        root.addLayout(left, 1)
        root.addLayout(right, 2)

    # ---------------- 文件 ----------------
    def _add_files(self):
        exts = " ".join("*" + e for e in TaskManager(Detector()).supported())
        paths, _ = QFileDialog.getOpenFileNames(self, "选择文件", "", f"支持的文件 ({exts});;所有文件 (*.*)")
        for p in paths:
            try:
                task = self._build_tm().prepare(p)
                self.tasks.append(task)
            except Exception as e:
                QMessageBox.warning(self, "无法添加", f"{os.path.basename(p)}：{e}")
        self._refresh_file_list()

    def _clear_files(self):
        self.tasks.clear()
        self.cand_table.setRowCount(0)
        self.preview.clear()
        self.audit_view.clear()
        self._refresh_file_list()

    def _refresh_file_list(self):
        self.file_list.setRowCount(len(self.tasks))
        for i, t in enumerate(self.tasks):
            name = QTableWidgetItem(t.source_path.name)
            st = QTableWidgetItem(t.status.value)
            self.file_list.setItem(i, 0, name)
            self.file_list.setItem(i, 1, st)
        if self.tasks:
            self.file_list.selectRow(0)

    def _active_task(self):
        rows = self.file_list.selectionModel().selectedRows()
        idx = rows[0].row() if rows else 0
        return self.tasks[idx] if self.tasks else None

    def _on_file_selected(self):
        task = self._active_task()
        if task:
            self._show_candidates(task)

    # ---------------- 配置 ----------------
    def _build_detector(self):
        return Detector(
            custom_words=self.custom_words.toPlainText().splitlines(),
            companies=self.company_list.toPlainText().splitlines(),
            persons=self.person_list.toPlainText().splitlines(),
            enable_company_regex=self.chk_company.isChecked(),
        )

    def _build_tm(self):
        return TaskManager(self._build_detector(), use_ollama=self.chk_ollama.isChecked())

    def _mode(self):
        return "b" if self.rb_b.isChecked() else "a"

    # ---------------- Ollama ----------------
    def _refresh_ollama(self):
        self.ollama_label.setText("状态：检测中…")
        def check():
            c = OllamaClient()
            tags = c.tags()
            return tags
        w = _Worker(check)
        w.signals.done.connect(self._on_ollama_done)
        w.signals.error.connect(lambda e: self.ollama_label.setText("状态：未连接"))
        self._workers.append(w)
        self.pool.start(w)

    def _on_ollama_done(self, tags):
        if tags:
            self.ollama_label.setText("状态：已连接，模型 " + "、".join(tags))
        else:
            self.ollama_label.setText("状态：未连接（仅用规则检测）")

    # ---------------- 扫描 ----------------
    def _scan(self):
        if not self.tasks:
            QMessageBox.information(self, "提示", "请先添加文件")
            return
        self._set_busy(True, "正在只读扫描…")
        def run():
            tm = self._build_tm()
            for t in self.tasks:
                if t.status in (TaskStatus.PENDING, TaskStatus.PROCESSING):
                    tm.scan(t)
            return self.tasks
        w = _Worker(run)
        w.signals.done.connect(self._on_scan_done)
        w.signals.error.connect(self._on_error)
        self._workers.append(w)
        self.pool.start(w)

    def _on_scan_done(self, tasks):
        self._set_busy(False, "扫描完成")
        self._refresh_file_list()
        task = self._active_task()
        if task:
            self._show_candidates(task)

    # ---------------- 候选表格 ----------------
    def _show_candidates(self, task):
        scan = task.scan
        self.cand_table.setRowCount(0)
        if not scan:
            self.preview.setPlainText("（尚未扫描）")
            self.audit_view.clear()
            return
        cands = scan.candidates
        self.cand_table.setRowCount(len(cands))
        for i, c in enumerate(cands):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if c.confirmed else Qt.Unchecked)
            chk.setData(Qt.UserRole, c)
            self.cand_table.setItem(i, 0, chk)
            for col, val in ((1, c.category), (2, c.text), (4, c.location), (5, c.source), (6, f"{c.confidence:.2f}")):
                it = QTableWidgetItem(str(val))
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.cand_table.setItem(i, col, it)
            rep = QTableWidgetItem(c.replacement or "")
            rep.setData(Qt.UserRole, c)
            self.cand_table.setItem(i, 3, rep)
        self._refresh_preview()

    def _iter_cands(self):
        for i in range(self.cand_table.rowCount()):
            chk = self.cand_table.item(i, 0)
            c = chk.data(Qt.UserRole)
            c.confirmed = chk.checkState() == Qt.Checked
            c.excluded = not c.confirmed
            rep = self.cand_table.item(i, 3)
            c.replacement = rep.text().strip()
            yield c

    def _cand_all(self):
        for i in range(self.cand_table.rowCount()):
            self.cand_table.item(i, 0).setCheckState(Qt.Checked)

    def _cand_none(self):
        for i in range(self.cand_table.rowCount()):
            self.cand_table.item(i, 0).setCheckState(Qt.Unchecked)

    def _cand_exclude(self):
        for r in self.cand_table.selectionModel().selectedRows():
            self.cand_table.item(r.row(), 0).setCheckState(Qt.Unchecked)

    def _refresh_preview(self):
        task = self._active_task()
        if not task or not task.scan:
            self.preview.clear()
            return
        text = task.scan.text
        masker = Masker(self._mode(), self.mask_edit.text() or "****")
        for c in sorted(self._iter_cands(), key=lambda c: len(c.text), reverse=True):
            if c.excluded:
                continue
            repl = c.replacement or masker.replacement(c.category, c.text)
            text = text.replace(c.text, repl)
        self.preview.setPlainText("（预览为近似结果，最终以审计后输出为准）\n\n" + text)

    # ---------------- 处理 ----------------
    def _process_active(self):
        task = self._active_task()
        if not task:
            return
        if not task.scan:
            QMessageBox.information(self, "提示", "请先扫描")
            return
        cands = list(self._iter_cands())
        self._set_busy(True, "正在处理 + 独立审计…")
        def run():
            tm = self._build_tm()
            tm.process(task, self._mode(), self.mask_edit.text() or "****", cands)
            if self.chk_ollama.isChecked():
                pass  # 模型仅辅助识别，已在扫描阶段使用
            report = tm.audit(task, cands)
            tm.finalize(task, report)
            return task
        w = _Worker(run)
        w.signals.done.connect(self._on_process_done)
        w.signals.error.connect(self._on_error)
        self._workers.append(w)
        self.pool.start(w)

    def _batch(self):
        if not self.tasks:
            QMessageBox.information(self, "提示", "请先添加文件")
            return
        self._set_busy(True, "正在批量处理…")
        def run():
            tm = self._build_tm()
            results = []
            for t in self.tasks:
                try:
                    if not t.scan:
                        tm.scan(t)
                    cands = list(t.scan.candidates)
                    for c in cands:
                        c.confirmed = True
                        c.excluded = False
                    tm.process(t, self._mode(), self.mask_edit.text() or "****", cands)
                    report = tm.audit(t, cands)
                    tm.finalize(t, report)
                    results.append((t.source_path.name, t.status.value, report.summary()))
                except Exception as e:
                    t.status = TaskStatus.UNSAFE
                    t.error = str(e)
                    results.append((t.source_path.name, t.status.value, str(e)))
            return results
        w = _Worker(run)
        w.signals.done.connect(self._on_batch_done)
        w.signals.error.connect(self._on_error)
        self._workers.append(w)
        self.pool.start(w)

    def _on_process_done(self, task):
        self._set_busy(False, "处理完成")
        self._refresh_file_list()
        self.audit_view.setPlainText(self._report_text(task))
        if task.audit and task.audit.passed:
            QMessageBox.information(self, "完成", f"{task.source_path.name}：{task.status.value}\n已导出到输出目录。")
        else:
            QMessageBox.warning(self, "结果", f"{task.source_path.name}：{task.status.value}\n{self._report_text(task)}")

    def _on_batch_done(self, results):
        self._set_busy(False, "批量处理完成")
        self._refresh_file_list()
        text = "\n".join(f"{n} → {st} | {msg}" for n, st, msg in results)
        self.audit_view.setPlainText("批量处理结果：\n\n" + text)

    def _report_text(self, task):
        if not task.audit:
            return "（无审计报告）"
        lines = [f"审计结论：{'通过' if task.audit.passed else '不通过'}"]
        for f in task.audit.findings:
            lines.append(f"{f.verdict.value}：{f.detail}")
        lines.append("")
        lines.append("提示：『未发现残留』不等于绝对不存在敏感信息，请人工复核。")
        return "\n".join(lines)

    # ---------------- 工具 ----------------
    def _open_output(self):
        ensure_dirs()
        os.startfile(str(OUTPUT_DIR))

    def _clean_temp(self):
        import shutil
        from .config import PROCESSING_DIR
        if PROCESSING_DIR.exists():
            for p in PROCESSING_DIR.iterdir():
                try:
                    if p.is_file():
                        p.unlink()
                    elif p.is_dir():
                        shutil.rmtree(p)
                except Exception:
                    pass
        QMessageBox.information(self, "完成", "处理中间目录已清理。")

    # ---------------- 状态 ----------------
    def _set_busy(self, busy, msg):
        self.stage_label.setText(msg)
        self.progress.setVisible(busy)
        self.btn_scan.setEnabled(not busy)
        self.btn_process.setEnabled(not busy)
        self.btn_batch.setEnabled(not busy)

    def _on_error(self, msg):
        self._set_busy(False, "出错")
        QMessageBox.critical(self, "错误", msg)


def run():
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei", 10))
    ensure_dirs()
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
