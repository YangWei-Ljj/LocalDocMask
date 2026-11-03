"""LocalDocMask 桌面界面（PySide6）。

本界面只通过组合根 ``build_services()`` 获取应用服务，不直接 new
Detector / TaskManager / OllamaClient；所有耗时操作走 QThreadPool，
进度来自真实 ``progress_cb``；三档分流（AUTO_CONFIRM / NEEDS_REVIEW /
SUPPRESS）在候选表中显式呈现，未裁决项由 finalize 复核门禁阻断导出。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Qt
from PySide6.QtGui import QBrush, QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QRadioButton, QButtonGroup, QCheckBox,
    QTextEdit, QTableWidget, QTableWidgetItem, QTabWidget,
    QFileDialog, QMessageBox, QProgressBar, QGroupBox, QHeaderView,
    QAbstractItemView,
)

from ...application.batch import BatchService, CancelToken
from ...application.pipeline import TaskExecutionContext
from ...application.composition import ApplicationServices, build_services
from ...domain.entities import Candidate, Task
from ...domain.enums import EntityDecision, TaskStatus
from ...domain.exceptions import (
    InvalidTaskTransition, SecurityViolation, TaskCancelledError, TaskTimedOutError,
)
from ...infrastructure.config.settings import (
    PROCESSING_DIR, QUARANTINE_DIR, SUCCESS_DIR, ensure_dirs,
)
from ...security.limits import ResourceLimits
from ...security.redaction.masker import Masker, mask_display
from ...security.redaction.mapping import ConsistencyMapper
from ...domain.state_machine import current_actor

#: 候选表列定义
COL_CHECK, COL_DECISION, COL_RISK, COL_CATEGORY, COL_TEXT, COL_REPL, \
    COL_LOC, COL_SOURCE, COL_CONF, COL_REASON = range(10)

_DECISION_LABEL = {
    EntityDecision.AUTO_CONFIRM: "自动确认",
    EntityDecision.NEEDS_REVIEW: "待复核",
    EntityDecision.SUPPRESS: "已抑制",
    EntityDecision.CONFIRMED: "已确认",
    EntityDecision.DISMISSED: "已放行",
}
_RISK_LABEL = {"low": "低", "medium": "中", "high": "高", "critical": "严重"}

_ROLE_CAND = Qt.UserRole
_ROLE_TASK = Qt.UserRole + 1


def _coerce_decision_label(value) -> EntityDecision:
    try:
        return EntityDecision(str(getattr(value, "value", value)))
    except ValueError:
        return EntityDecision.NEEDS_REVIEW


def _dec(c: Candidate) -> EntityDecision:
    d = c.decision
    if isinstance(d, str):
        try:
            return EntityDecision(d)
        except ValueError:
            return EntityDecision.NEEDS_REVIEW
    return d


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


class _BatchSignals(QObject):
    progress = Signal(int, int, str, str)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LocalDocMask - 本地文件脱敏工作台")
        self.resize(1320, 860)
        self.pool = QThreadPool.globalInstance()
        self._workers: list[_Worker] = []

        #: 当前文件任务列表（prepare 结果）
        self.tasks: list[Task] = []
        #: 组合根装配的应用服务
        self.services: ApplicationServices | None = None
        self._config_sig: str | None = None
        #: 会话级共享 mapper（模式 B 预览与真实处理使用同一实例，保证稳定值）
        self._session_mapper: ConsistencyMapper | None = None
        self._cancel_token: CancelToken | None = None
        self._batch_origins: list[Task] = []
        self._syncing = False

        self._build_ui()
        ensure_dirs()
        try:
            self.services = build_services()
            self._config_sig = self._current_config_signature()
            # 并发上限：按配置把全局线程池工作数收敛到安全上限
            from ...security.limits import ResourceLimits
            limits = ResourceLimits.from_settings(self.services.settings)
            self.pool.setMaxThreadCount(max(1, limits.max_workers))
        except Exception as e:  # 启动期装配失败不阻塞界面，执行时会再次报错
            self.stage_label.setText(f"服务装配失败：{e}")
        self._refresh_ollama()
        self._refresh_history()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        left = QVBoxLayout()
        left.setSpacing(8)

        g_files = QGroupBox("1. 选择文件")
        vf = QVBoxLayout(g_files)
        row = QHBoxLayout()
        self.btn_add = QPushButton("添加文件")
        self.btn_add.clicked.connect(lambda _checked=False: self._add_files())
        self.btn_clear = QPushButton("清空")
        self.btn_clear.clicked.connect(self._clear_files)
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_clear)
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
        self.rb_a = QRadioButton("模式 A：统一掩码")
        self.rb_b = QRadioButton("模式 B：一致性虚构替换（公司/人名）")
        self.rb_a.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.rb_a)
        self._mode_group.addButton(self.rb_b)
        vm.addWidget(self.rb_a)
        vm.addWidget(self.rb_b)
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("掩码字符："))
        self.mask_edit = QLineEdit("****")
        mrow.addWidget(self.mask_edit)
        vm.addLayout(mrow)
        left.addWidget(g_mode)

        g_cat = QGroupBox("3. 敏感类别与词库（执行前生效）")
        vc = QVBoxLayout(g_cat)
        self.chk_company = QCheckBox("公司/企业名（正则）")
        self.chk_company.setChecked(True)
        vc.addWidget(self.chk_company)
        vc.addWidget(QLabel("自定义敏感词（每行一个）："))
        self.custom_words = QTextEdit()
        self.custom_words.setMaximumHeight(56)
        vc.addWidget(self.custom_words)
        vc.addWidget(QLabel("企业名单（每行一个）："))
        self.company_list = QTextEdit()
        self.company_list.setMaximumHeight(56)
        vc.addWidget(self.company_list)
        vc.addWidget(QLabel("人员名单（每行一个）："))
        self.person_list = QTextEdit()
        self.person_list.setMaximumHeight(56)
        vc.addWidget(self.person_list)
        left.addWidget(g_cat)

        g_ai = QGroupBox("4. 本地模型（Ollama，回环）")
        va = QVBoxLayout(g_ai)
        self.ollama_label = QLabel("状态：检测中…")
        va.addWidget(self.ollama_label)
        self.chk_ollama = QCheckBox("启用本地模型辅助识别（执行前生效）")
        self.chk_ollama.setChecked(False)
        va.addWidget(self.chk_ollama)
        brow = QHBoxLayout()
        self.btn_ollama = QPushButton("刷新状态")
        self.btn_ollama.clicked.connect(self._refresh_ollama)
        brow.addWidget(self.btn_ollama)
        va.addLayout(brow)
        left.addWidget(g_ai)

        g_run = QGroupBox("5. 执行")
        vr = QVBoxLayout(g_run)
        self.btn_scan = QPushButton("① 只读扫描（三档分流）")
        self.btn_scan.clicked.connect(self._scan)
        self.btn_process = QPushButton("② 处理 + 独立审计（复核门禁）")
        self.btn_process.clicked.connect(self._process_active)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_cancel.setEnabled(False)
        self.btn_batch = QPushButton("③ 批量自动安全处理（BatchService）")
        self.btn_batch.clicked.connect(self._batch)
        self.btn_export = QPushButton("④ 导出脱敏文件到指定位置…")
        self.btn_export.clicked.connect(self._export_to_location)
        self.btn_export.setEnabled(False)
        self.btn_open = QPushButton("打开成功目录")
        self.btn_open.clicked.connect(self._open_output)
        self.btn_clean = QPushButton("清理临时文件")
        self.btn_clean.clicked.connect(self._clean_temp)
        for b in (self.btn_scan, self.btn_process, self.btn_cancel,
                  self.btn_batch, self.btn_export, self.btn_open, self.btn_clean):
            vr.addWidget(b)
        left.addWidget(g_run)
        left.addStretch(1)

        # 右侧
        right = QVBoxLayout()
        top = QHBoxLayout()
        self.stage_label = QLabel("就绪")
        top.addWidget(self.stage_label)
        self.actor_label = QLabel(f"本地操作者：{current_actor()}")
        top.addWidget(self.actor_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        top.addWidget(self.progress)
        right.addLayout(top)

        g_cand = QGroupBox("6. 敏感候选（三档分流；勾选=确认脱敏）")
        vcd = QVBoxLayout(g_cand)
        self.cand_table = QTableWidget(0, 10)
        self.cand_table.setHorizontalHeaderLabels(
            ["包含", "建议/当前", "风险", "类别", "脱敏显示值", "替换值",
             "位置", "来源", "置信度", "复核信息"])
        self.cand_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.cand_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.cand_table.itemChanged.connect(self._on_cand_changed)
        vcd.addWidget(self.cand_table)
        crow = QHBoxLayout()
        for label, fn in (("确认脱敏（选中）", self._cand_confirm),
                          ("排除/放行（选中）", self._cand_exclude),
                          ("全选", self._cand_all),
                          ("全不选", self._cand_none),
                          ("刷新预览", self._refresh_preview)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            crow.addWidget(b)
        vcd.addLayout(crow)
        right.addWidget(g_cand, 3)

        tabs = QTabWidget()
        # 脱敏预览：原文 vs 脱敏后
        prev_widget = QWidget()
        pw = QHBoxLayout(prev_widget)
        pw.setContentsMargins(0, 0, 0, 0)
        self.preview_orig = QTextEdit()
        self.preview_orig.setReadOnly(True)
        self.preview_new = QTextEdit()
        self.preview_new.setReadOnly(True)
        pw.addWidget(self.preview_orig)
        pw.addWidget(self.preview_new)
        tabs.addTab(prev_widget, "脱敏预览（原文 | 脱敏后）")

        self.audit_view = QTextEdit()
        self.audit_view.setReadOnly(True)
        tabs.addTab(self.audit_view, "独立审计报告")

        self.success_table = self._make_table(
            ["文件", "输出路径", "类型", "识别", "脱敏", "删除", "状态", "哈希(前8)"])
        tabs.addTab(self.success_table, "成功结果")

        self.review_table = self._make_table(
            ["文件", "状态", "原因/未裁决数", "类别", "失败原因", "输出路径"])
        tabs.addTab(self.review_table, "复核/失败")

        self.quarantine_table = self._make_table(
            ["文件", "状态", "原因", "隔离路径"])
        tabs.addTab(self.quarantine_table, "隔离文件")

        self.history_table = self._make_table(
            ["时间", "文件", "状态", "模式", "识别", "脱敏", "删除", "输出路径"])
        self.history_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hrow = QHBoxLayout()
        self.btn_open_dir = QPushButton("打开选中产物所在目录")
        self.btn_open_dir.clicked.connect(self._open_selected_history)
        self.btn_refresh_hist = QPushButton("刷新历史")
        self.btn_refresh_hist.clicked.connect(self._refresh_history)
        hrow.addWidget(self.btn_open_dir)
        hrow.addWidget(self.btn_refresh_hist)
        hist_wrap = QWidget()
        hw = QVBoxLayout(hist_wrap)
        hw.setContentsMargins(0, 0, 0, 0)
        hw.addWidget(self.history_table)
        hw.addLayout(hrow)
        tabs.addTab(hist_wrap, "任务中心/历史")

        right.addWidget(tabs, 2)

        root.addLayout(left, 1)
        root.addLayout(right, 2)

    # ------------------------------------------------------------------
    # 服务装配
    # ------------------------------------------------------------------
    def _current_config_signature(self) -> str:
        return "|".join([
            "1" if self.chk_company.isChecked() else "0",
            self.custom_words.toPlainText(),
            self.company_list.toPlainText(),
            self.person_list.toPlainText(),
            "1" if self.chk_ollama.isChecked() else "0",
        ])

    def _ensure_services(self) -> ApplicationServices:
        """配置变化时按界面值重建服务；否则复用既有实例。"""
        sig = self._current_config_signature()
        if self.services is not None and sig == self._config_sig:
            return self.services
        if self.services is not None:
            self.services.close()
        self.services = build_services(
            extra_companies=self.company_list.toPlainText().splitlines(),
            extra_custom_words=self.custom_words.toPlainText().splitlines(),
            extra_persons=self.person_list.toPlainText().splitlines(),
            enable_company_regex=self.chk_company.isChecked(),
            ollama_enabled=self.chk_ollama.isChecked(),
        )
        self._config_sig = sig
        self._session_mapper = None
        return self.services

    def _mapper_for(self, mode: str):
        """模式 B 复用会话级共享 mapper，保证预览与真实处理稳定一致。

        mapper 由组合根按 settings 构建（memory/disk 落盘口径生效，m5 修复）。
        """
        if mode != "b":
            return None
        if self._session_mapper is None:
            self._session_mapper = self._ensure_services().make_mapper()
        return self._session_mapper

    def _mode(self) -> str:
        return "b" if self.rb_b.isChecked() else "a"

    def _mask(self) -> str:
        return self.mask_edit.text() or "****"

    # ------------------------------------------------------------------
    # 文件
    # ------------------------------------------------------------------
    def _add_files(self, paths=None):
        if paths is None:
            exts = " ".join("*" + e for e in self._supported())
            paths, _ = QFileDialog.getOpenFileNames(
                self, "选择文件", "", f"支持的文件 ({exts});;所有文件 (*.*)")
        svc = self._ensure_services()
        for p in paths:
            try:
                task = svc.pipeline.prepare(p)
                self.tasks.append(task)
            except Exception as e:
                QMessageBox.warning(self, "无法添加", f"{os.path.basename(str(p))}：{e}")
        self._refresh_file_list()

    def _supported(self):
        try:
            return self._ensure_services().pipeline.supported()
        except Exception:
            return [".docx", ".pdf", ".xlsx", ".png"]

    def _clear_files(self):
        self.tasks.clear()
        self.cand_table.setRowCount(0)
        self.preview_orig.clear()
        self.preview_new.clear()
        self.audit_view.clear()
        self._populate_results()
        self._refresh_file_list()

    def _refresh_file_list(self):
        self._syncing = True
        self.file_list.setRowCount(len(self.tasks))
        for i, t in enumerate(self.tasks):
            self.file_list.setItem(i, 0, QTableWidgetItem(t.source_path.name))
            status_text = t.status.value
            if t.status == TaskStatus.PENDING and t.error.startswith("批次已取消"):
                status_text += "（批次已取消，未处理）"
            st = QTableWidgetItem(status_text)
            st.setData(_ROLE_TASK, t)
            self.file_list.setItem(i, 1, st)
        self._syncing = False
        if self.tasks:
            self.file_list.selectRow(0)

    def _active_task(self) -> Task | None:
        rows = self.file_list.selectionModel().selectedRows()
        idx = rows[0].row() if rows else 0
        return self.tasks[idx] if self.tasks else None

    def _on_file_selected(self):
        task = self._active_task()
        if task:
            self._show_candidates(task)
            can_export = False
            if task.status == TaskStatus.PASSED and self.services is not None:
                try:
                    can_export = not self.services.pipeline.export_gate_reasons(task)
                except Exception:
                    can_export = False
            self.btn_export.setEnabled(can_export)

    # ------------------------------------------------------------------
    # Ollama（异步健康检查，不阻塞）
    # ------------------------------------------------------------------
    def _refresh_ollama(self):
        self.ollama_label.setText("状态：检测中…")

        def check():
            from ...infrastructure.llm.ollama import OllamaClient
            return OllamaClient().tags()

        w = _Worker(check)
        w.signals.done.connect(
            lambda tags: self.ollama_label.setText(
                "状态：已连接，模型 " + "、".join(tags) if tags else
                "状态：未连接（仅用规则检测）"))
        w.signals.error.connect(lambda e: self.ollama_label.setText("状态：未连接"))
        self._workers.append(w)
        self.pool.start(w)

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------
    def _scan(self):
        if not self.tasks:
            QMessageBox.information(self, "提示", "请先添加文件")
            return
        svc = self._ensure_services()
        token = CancelToken()
        self._cancel_token = token
        self.btn_cancel.setEnabled(True)
        self._set_busy(True, "正在只读扫描…")
        task_snapshot = list(self.tasks)
        limits = ResourceLimits.from_settings(svc.settings)

        def run():
            for t in task_snapshot:
                if token.cancelled:
                    break
                if t.status not in (TaskStatus.PENDING, TaskStatus.PROCESSING,
                                    TaskStatus.NEEDS_REVIEW,
                                    TaskStatus.INTERRUPTED):
                    continue
                context = TaskExecutionContext.from_limits(limits, token)
                try:
                    svc.pipeline.scan(t, execution_context=context)
                except (TaskCancelledError, TaskTimedOutError):
                    break
            return task_snapshot

        w = _Worker(run)
        w.signals.done.connect(self._on_scan_done)
        w.signals.error.connect(self._on_error)
        self._workers.append(w)
        self.pool.start(w)

    def _on_scan_done(self, tasks):
        cancelled = bool(self._cancel_token and self._cancel_token.cancelled)
        self._set_busy(False, "扫描已取消" if cancelled else "扫描完成")
        self.btn_cancel.setEnabled(False)
        self._cancel_token = None
        self.tasks = list(tasks)
        self._refresh_file_list()
        task = self._active_task()
        if task:
            self._show_candidates(task)

    # ------------------------------------------------------------------
    # 候选表（三档分流）
    # ------------------------------------------------------------------
    def _show_candidates(self, task: Task):
        scan = task.scan
        self._syncing = True
        self.cand_table.setRowCount(0)
        if not scan:
            self.preview_orig.setPlainText("（尚未扫描）")
            self.preview_new.clear()
            self.audit_view.clear()
            self._syncing = False
            return
        for c in scan.candidates:
            r = self.cand_table.rowCount()
            self.cand_table.insertRow(r)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if c.confirmed else Qt.Unchecked)
            chk.setData(_ROLE_CAND, c)
            self.cand_table.setItem(r, COL_CHECK, chk)
            self.cand_table.setItem(r, COL_DECISION, self._ro_item(self._decision_label(c)))
            self.cand_table.setItem(r, COL_RISK, self._ro_item(
                _RISK_LABEL.get(getattr(c.risk_level, "value", c.risk_level), str(c.risk_level))))
            self.cand_table.setItem(r, COL_CATEGORY, self._ro_item(c.category))
            display_value = c.masked_value or mask_display(c.text, c.category)
            self.cand_table.setItem(r, COL_TEXT, self._ro_item(display_value))
            rep = QTableWidgetItem(c.replacement or "")
            rep.setData(_ROLE_CAND, c)
            self.cand_table.setItem(r, COL_REPL, rep)
            self.cand_table.setItem(r, COL_LOC, self._ro_item(c.location or ""))
            self.cand_table.setItem(r, COL_SOURCE, self._ro_item(c.source or ""))
            self.cand_table.setItem(r, COL_CONF, self._ro_item(f"{c.confidence:.2f}"))
            review_info = (c.decision_reason or "").replace(c.text, display_value)
            if c.review_actor or c.reviewed_at or c.review_reason_code:
                review_info = (f"{review_info} | 复核人={c.review_actor or '-'}"
                               f" 时间={c.reviewed_at or '-'}"
                               f" 原因={c.review_reason_code or '-'}")
            self.cand_table.setItem(r, COL_REASON, self._ro_item(review_info))
            self._paint_row(r, c)
        self._syncing = False
        self._refresh_preview()

    @staticmethod
    def _ro_item(text: str) -> QTableWidgetItem:
        it = QTableWidgetItem(str(text))
        it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        return it

    @staticmethod
    def _decision_label(c: Candidate) -> str:
        current = _DECISION_LABEL.get(_dec(c), _dec(c).value)
        suggested_raw = c.suggested_action or (c.decision.value if hasattr(c.decision, "value") else c.decision)
        suggested = _DECISION_LABEL.get(
            _coerce_decision_label(suggested_raw), str(suggested_raw) or "待复核")
        return f"建议:{suggested} / 当前:{current}"

    def _paint_row(self, row: int, c: Candidate):
        d = _dec(c)
        if d == EntityDecision.SUPPRESS or c.excluded:
            brush = QBrush(QColor(225, 225, 225))
        elif d == EntityDecision.NEEDS_REVIEW and not c.confirmed:
            brush = QBrush(QColor(255, 240, 160))
        elif c.confirmed:
            brush = QBrush(QColor(215, 240, 215))
        else:
            brush = QBrush(QColor(255, 255, 255))
        for col in range(self.cand_table.columnCount()):
            it = self.cand_table.item(row, col)
            if it is not None:
                it.setBackground(brush)

    def _row_candidate(self, row: int) -> Candidate | None:
        it = self.cand_table.item(row, COL_CHECK)
        return it.data(_ROLE_CAND) if it else None

    def _reload_task(self, task: Task) -> None:
        try:
            stored = self._ensure_services().repository.load_task(task.task_id)
        except Exception:
            return
        if stored is None:
            return
        for index, current in enumerate(self.tasks):
            if current.task_id == task.task_id:
                self.tasks[index] = stored
                break

    def _persist_candidate_change(self, task: Task, candidate: Candidate, *,
                                  confirmed: bool, excluded: bool,
                                  replacement: str, reason_code: str) -> Candidate | None:
        try:
            return self._ensure_services().pipeline.update_candidate_review(
                task, candidate.candidate_id, confirmed=confirmed,
                excluded=excluded, replacement=replacement,
                reason_code=reason_code)
        except Exception as exc:
            self._reload_task(task)
            self._refresh_file_list()
            current = self._active_task()
            if current is not None:
                self._show_candidates(current)
            QMessageBox.warning(self, "复核未保存", f"{type(exc).__name__}: {exc}")
            return None

    def _on_cand_changed(self, item: QTableWidgetItem):
        if self._syncing:
            return
        row = item.row()
        candidate = self._row_candidate(row)
        task = self._active_task()
        if candidate is None or task is None:
            return
        confirmed = candidate.confirmed
        excluded = candidate.excluded
        replacement = candidate.replacement
        reason = "review_checkbox"
        if item.column() == COL_CHECK:
            confirmed = item.checkState() == Qt.Checked
            excluded = False
            reason = "review_confirmed" if confirmed else "review_unconfirmed"
        elif item.column() == COL_REPL:
            replacement = item.text().strip()
            reason = "review_replacement"
        else:
            return
        updated = self._persist_candidate_change(
            task, candidate, confirmed=confirmed, excluded=excluded,
            replacement=replacement, reason_code=reason)
        if updated is None:
            return
        self._syncing = True
        self.cand_table.item(row, COL_CHECK).setCheckState(
            Qt.Checked if updated.confirmed else Qt.Unchecked)
        self.cand_table.setItem(row, COL_DECISION,
                                self._ro_item(self._decision_label(updated)))
        review_info = ""
        if updated.review_actor or updated.reviewed_at or updated.review_reason_code:
            review_info = (f"复核人={updated.review_actor or '-'}"
                           f" 时间={updated.reviewed_at or '-'}"
                           f" 原因={updated.review_reason_code or '-'}")
        self.cand_table.setItem(row, COL_REASON, self._ro_item(review_info))
        self._paint_row(row, updated)
        self._syncing = False
        self._refresh_preview()

    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.cand_table.selectedItems()})

    def _cand_confirm(self):
        self._set_candidates_decision(self._selected_rows(), confirmed=True)

    def _cand_exclude(self):
        self._set_candidates_decision(
            self._selected_rows(), confirmed=False, excluded=True)

    def _apply_candidate_result_to_row(self, row: int, candidate: Candidate) -> None:
        self._syncing = True
        self.cand_table.item(row, COL_CHECK).setCheckState(
            Qt.Checked if candidate.confirmed else Qt.Unchecked)
        self.cand_table.setItem(
            row, COL_DECISION, self._ro_item(self._decision_label(candidate)))
        review_info = ""
        if candidate.review_actor or candidate.reviewed_at or candidate.review_reason_code:
            review_info = (f"复核人={candidate.review_actor or '-'}"
                           f" 时间={candidate.reviewed_at or '-'}"
                           f" 原因={candidate.review_reason_code or '-'}")
        self.cand_table.setItem(row, COL_REASON, self._ro_item(review_info))
        self._paint_row(row, candidate)
        self._syncing = False

    def _set_candidates_decision(self, rows: list[int], confirmed: bool,
                                 *, excluded: bool = False):
        if not rows:
            return
        task = self._active_task()
        if task is None:
            return
        skipped = 0
        for row in rows:
            candidate = self._row_candidate(row)
            if candidate is None:
                continue
            # 多条批量操作不得自动裁决 NEEDS_REVIEW；单条选择视为明确逐项操作。
            if (len(rows) > 1 and candidate.decision == EntityDecision.NEEDS_REVIEW
                    and not candidate.confirmed and not candidate.excluded):
                skipped += 1
                continue
            updated = self._persist_candidate_change(
                task, candidate, confirmed=confirmed, excluded=excluded,
                replacement=candidate.replacement,
                reason_code=("bulk_confirm" if confirmed
                             else "bulk_exclude" if excluded
                             else "bulk_pending"))
            if updated is not None:
                self._apply_candidate_result_to_row(row, updated)
        if skipped:
            QMessageBox.information(
                self, "需要逐项复核",
                f"{skipped} 条待复核项未被批量处理，请逐项确认或排除。")
        self._refresh_preview()

    def _cand_all(self):
        task = self._active_task()
        if task is None or task.scan is None:
            return
        rows = []
        skipped = 0
        for row, candidate in enumerate(task.scan.candidates):
            if candidate.excluded:
                continue
            if candidate.decision == EntityDecision.NEEDS_REVIEW:
                skipped += 1
                continue
            rows.append(row)
        self._set_candidates_decision(rows, confirmed=True)
        if skipped:
            QMessageBox.information(
                self, "需要逐项复核",
                f"{skipped} 条待复核项未被自动确认。")

    def _cand_none(self):
        task = self._active_task()
        if task is None or task.scan is None:
            return
        for row, candidate in enumerate(task.scan.candidates):
            if candidate.excluded:
                continue
            updated = self._persist_candidate_change(
                task, candidate, confirmed=False, excluded=False,
                replacement=candidate.replacement, reason_code="bulk_pending")
            if updated is not None:
                self._apply_candidate_result_to_row(row, updated)
        self._refresh_preview()

    def _sync_replacements(self) -> bool:
        task = self._active_task()
        if task is None:
            return True
        ok = True
        for row in range(self.cand_table.rowCount()):
            candidate = self._row_candidate(row)
            item = self.cand_table.item(row, COL_REPL)
            if candidate is None or item is None:
                continue
            replacement = item.text().strip()
            if replacement == (candidate.replacement or ""):
                continue
            updated = self._persist_candidate_change(
                task, candidate, confirmed=candidate.confirmed,
                excluded=candidate.excluded, replacement=replacement,
                reason_code="review_replacement")
            if updated is None:
                ok = False
                item.setText(candidate.replacement or "")
                continue
            candidate.replacement = updated.replacement
        return ok

    def _refresh_preview(self):
        task = self._active_task()
        if not task or not task.scan:
            self.preview_orig.clear()
            self.preview_new.clear()
            return
        self._sync_replacements()
        text = task.scan.text or ""
        mode = self._mode()
        masker = Masker(mode, self._mask(), mapper=self._mapper_for(mode))
        active = [c for c in task.scan.candidates if not c.excluded and c.confirmed]
        active.sort(key=lambda c: len(c.text), reverse=True)
        out = text
        for c in active:
            repl = c.replacement or masker.replacement(c.category, c.text)
            out = out.replace(c.text, repl)
        self.preview_orig.setPlainText(text)
        self.preview_new.setPlainText(out)

    # ------------------------------------------------------------------
    # 结果表格
    # ------------------------------------------------------------------
    def _make_table(self, headers):
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        return t

    @staticmethod
    def _set_row(table, row, values):
        for col, val in enumerate(values):
            table.setItem(row, col, QTableWidgetItem(str(val)))

    def _populate_results(self):
        self.success_table.setRowCount(0)
        self.review_table.setRowCount(0)
        self.quarantine_table.setRowCount(0)
        for task in self.tasks:
            if task.status == TaskStatus.PASSED:
                r = self.success_table.rowCount()
                self.success_table.insertRow(r)
                self._set_row(self.success_table, r, [
                    task.source_path.name, str(task.output_path or ""), task.file_type,
                    task.recognized_count, task.redacted_count, task.deleted_count,
                    task.status.value, (task.final_hash or "")[:8],
                ])
            if task.status in (TaskStatus.NEEDS_REVIEW, TaskStatus.AUDIT_FAILED,
                               TaskStatus.UNSAFE):
                r = self.review_table.rowCount()
                self.review_table.insertRow(r)
                pending = sum(
                    1 for c in (task.scan.candidates if task.scan else [])
                    if _dec(c) == EntityDecision.NEEDS_REVIEW
                    and not c.confirmed and not c.excluded)
                self._set_row(self.review_table, r, [
                    task.source_path.name, task.status.value,
                    task.error or (f"{pending} 项待复核" if pending else ""),
                    "-", "-", str(task.output_path or ""),
                ])
                # 未解决条目明细
                for c in (task.scan.candidates if task.scan else []):
                    if c.failure_reason:
                        rr = self.review_table.rowCount()
                        self.review_table.insertRow(rr)
                        self._set_row(self.review_table, rr, [
                            task.source_path.name, c.status, "-",
                            c.category, c.failure_reason, "-",
                        ])
            out = task.output_path
            if (task.status in (TaskStatus.AUDIT_FAILED, TaskStatus.UNSAFE,
                                TaskStatus.NEEDS_REVIEW) and out
                    and str(out).lower().startswith(str(QUARANTINE_DIR).lower())):
                r = self.quarantine_table.rowCount()
                self.quarantine_table.insertRow(r)
                self._set_row(self.quarantine_table, r, [
                    task.source_path.name, task.status.value, task.error or "",
                    str(out),
                ])

    def _refresh_history(self):
        if self.services is None:
            return
        try:
            rows = self.services.repository.list_tasks()
        except Exception:
            return
        self.history_table.setRowCount(0)
        for t in rows:
            r = self.history_table.rowCount()
            self.history_table.insertRow(r)
            self._set_row(self.history_table, r, [
                str(t.updated_at or "")[:19].replace("T", " "),
                t.source_path.name, t.status.value, t.mode or "-",
                t.recognized_count, t.redacted_count, t.deleted_count,
                str(t.output_path or ""),
            ])
            self.history_table.item(r, 0).setData(_ROLE_TASK, t)

    def _open_selected_history(self):
        rows = self.history_table.selectionModel().selectedRows()
        if not rows:
            return
        t = self.history_table.item(rows[0].row(), 0).data(_ROLE_TASK)
        target = None
        if t is not None and t.output_path and Path(t.output_path).exists():
            target = str(Path(t.output_path).parent)
        elif t is not None and t.source_path.exists():
            target = str(t.source_path.parent)
        if target:
            os.startfile(target)

    # ------------------------------------------------------------------
    # 单文件处理（含复核门禁）
    # ------------------------------------------------------------------
    def _process_active(self):
        task = self._active_task()
        if not task:
            QMessageBox.information(self, "提示", "请先选择文件")
            return
        if not task.scan:
            QMessageBox.information(self, "提示", "请先扫描")
            return
        svc = self._ensure_services()
        if not self._sync_replacements():
            return
        mode, mask = self._mode(), self._mask()
        mapper = self._mapper_for(mode)
        cands = list(task.scan.candidates)
        token = CancelToken()
        self._cancel_token = token
        self.btn_cancel.setEnabled(True)
        limits = ResourceLimits.from_settings(svc.settings)
        context = TaskExecutionContext.from_limits(limits, token)
        self._set_busy(True, "正在处理 + 独立审计…")

        def run():
            try:
                svc.pipeline.process(
                    task, mode, mask, cands, mapper=mapper,
                    execution_context=context)
                report = svc.pipeline.audit(
                    task, cands, execution_context=context)
                return svc.pipeline.finalize(
                    task, report, execution_context=context)
            except (TaskCancelledError, TaskTimedOutError):
                return task

        w = _Worker(run)
        w.signals.done.connect(self._on_process_done)
        w.signals.error.connect(self._on_error)
        self._workers.append(w)
        self.pool.start(w)

    def _on_process_done(self, task: Task):
        self._set_busy(
            False, "处理已取消" if task.status == TaskStatus.CANCELLED
            else "处理超时" if task.status == TaskStatus.TIMED_OUT else "处理完成")
        self.btn_cancel.setEnabled(False)
        self._cancel_token = None
        self._refresh_file_list()
        self._populate_results()
        self._refresh_history()
        self._show_candidates(task)
        self.audit_view.setPlainText(self._report_text(task))
        can_export = False
        if task.status == TaskStatus.PASSED:
            try:
                can_export = not self.services.pipeline.export_gate_reasons(task)
            except Exception:
                can_export = False
        self.btn_export.setEnabled(can_export)
        if task.status == TaskStatus.PASSED:
            QMessageBox.information(
                self, "完成", f"{task.source_path.name}：{task.status.value}\n已导出到成功目录。")
        elif task.status in (TaskStatus.CANCELLED, TaskStatus.TIMED_OUT):
            QMessageBox.warning(
                self, "任务未完成",
                f"{task.source_path.name}：{task.status.value}\n{task.error}")
        elif task.status == TaskStatus.NEEDS_REVIEW:
            QMessageBox.warning(
                self, "待复核阻断",
                f"{task.source_path.name}：还有未裁决项，未导出。\n{task.error}")
        else:
            QMessageBox.warning(
                self, "结果",
                f"{task.source_path.name}：{task.status.value}\n{task.error}")

    def _report_text(self, task: Task) -> str:
        lines = []
        if task.audit:
            lines.append(f"审计结论：{'通过' if task.audit.passed else '不通过'}")
            for f in task.audit.findings:
                lines.append(f"{f.verdict.value}：{f.detail}")
        else:
            lines.append("（无审计报告）")
        if task.output_path:
            lines.append(f"输出文件：{task.output_path}")
        if task.status == TaskStatus.PASSED and task.source_hash:
            lines.append(f"原件保留：SHA-256 前后一致（{task.source_hash[:16]}…），原件未修改。")
        lines.append("")
        lines.append("提示：『未发现残留』不等于绝对不存在敏感信息，请人工复核。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 批量处理（BatchService + 真实进度 + 取消）
    # ------------------------------------------------------------------
    def _batch(self):
        if not self.tasks:
            QMessageBox.information(self, "提示", "请先添加文件")
            return
        svc = self._ensure_services()
        mode, mask = self._mode(), self._mask()
        token = CancelToken()
        self._cancel_token = token
        self._batch_origins = list(self.tasks)
        sig = _BatchSignals()
        sig.progress.connect(self._on_batch_progress)
        self._set_busy(True, "正在批量处理…")
        self.btn_cancel.setEnabled(True)

        def run():
            return svc.batch.run(
                [str(t.source_path) for t in self.tasks],
                mode=mode, mask=mask,
                progress_cb=lambda i, total, name, stage: sig.progress.emit(i, total, name, stage),
                cancel_token=token)

        w = _Worker(run)
        w.signals.done.connect(self._on_batch_done)
        w.signals.error.connect(self._on_error)
        self._workers.append(w)
        self.pool.start(w)

    def _on_batch_progress(self, index: int, total: int, name: str, stage: str):
        if total:
            self.progress.setValue(int(index / total * 100))
        self.stage_label.setText(f"{name} · {stage}")

    def _cancel(self):
        if self._cancel_token is not None:
            self._cancel_token.cancel()
            self.stage_label.setText("正在取消…")

    def _on_batch_done(self, result):
        cancelled = bool(self._cancel_token and self._cancel_token.cancelled)
        self._set_busy(False, "批量已取消" if cancelled else "批量处理完成")
        self.btn_cancel.setEnabled(False)
        self._cancel_token = None
        processed = (list(result.passed) + list(result.review)
                     + list(result.failed) + list(result.skipped))
        by_source = {str(t.source_path.resolve()): t for t in processed}
        merged = []
        for original in self._batch_origins:
            key = str(original.source_path.resolve())
            if key in by_source:
                merged.append(by_source.pop(key))
            else:
                if cancelled:
                    original.error = "批次已取消：该任务未处理"
                merged.append(original)
        merged.extend(by_source.values())
        self.tasks = merged
        self._batch_origins = []
        self._refresh_file_list()
        self._populate_results()
        self._refresh_history()
        task = self._active_task()
        if task:
            self._show_candidates(task)
        lines = [
            f"通过 {len(result.passed)} · 待复核 {len(result.review)} · "
            f"失败/隔离 {len(result.failed)}",
            "",
        ] + list(result.warnings)
        self.audit_view.setPlainText("批量处理结果：\n" + "\n".join(lines))

    # ------------------------------------------------------------------
    # 导出 / 目录 / 清理
    # ------------------------------------------------------------------
    def _export_to_location(self):
        task = self._active_task()
        if task is None:
            return
        svc = self._ensure_services()
        reasons = svc.pipeline.export_gate_reasons(task)
        if reasons:
            QMessageBox.warning(
                self, "无法导出",
                "当前文件未通过统一导出安全门禁：\n" + "\n".join(reasons))
            return
        default = str(Path.home() / task.output_path.name)
        path, _ = QFileDialog.getSaveFileName(
            self, "导出脱敏文件（原件不会被改动）", default, "所有文件 (*.*)")
        if not path:
            return
        destination = Path(path)
        overwrite = False
        if destination.exists():
            answer = QMessageBox.question(
                self, "确认覆盖",
                "目标文件已存在。是否明确覆盖？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            overwrite = True
        token = CancelToken()
        self._cancel_token = token
        self.btn_cancel.setEnabled(True)
        limits = ResourceLimits.from_settings(svc.settings)
        context = TaskExecutionContext.from_limits(limits, token)
        self._set_busy(True, "正在原子导出…")

        def run():
            try:
                return svc.pipeline.export_to_location(
                    task, destination, overwrite=overwrite,
                    execution_context=context)
            except (TaskCancelledError, TaskTimedOutError):
                return None

        w = _Worker(run)
        w.signals.done.connect(self._on_export_done)
        w.signals.error.connect(self._on_export_error)
        self._workers.append(w)
        self.pool.start(w)

    def _on_export_done(self, path):
        self._set_busy(False, "导出完成" if path else "导出已取消")
        self.btn_cancel.setEnabled(False)
        self._cancel_token = None
        self._refresh_history()
        task = self._active_task()
        if task is not None and task.status == TaskStatus.PASSED and self.services is not None:
            try:
                self.btn_export.setEnabled(
                    not self.services.pipeline.export_gate_reasons(task))
            except Exception:
                self.btn_export.setEnabled(False)
        if path is not None:
            QMessageBox.information(
                self, "导出成功",
                f"已原子导出脱敏文件：\n{path}\n\n原件未做任何修改。")

    def _on_export_error(self, message: str):
        self._set_busy(False, "导出失败")
        self.btn_cancel.setEnabled(False)
        self._cancel_token = None
        task = self._active_task()
        if task is not None and task.status == TaskStatus.PASSED and self.services is not None:
            try:
                self.btn_export.setEnabled(
                    not self.services.pipeline.export_gate_reasons(task))
            except Exception:
                self.btn_export.setEnabled(False)
        QMessageBox.critical(self, "导出失败", message)

    def _open_output(self):
        ensure_dirs()
        os.startfile(str(SUCCESS_DIR))

    def _clean_temp(self):
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

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool, msg: str):
        self.stage_label.setText(msg)
        self.progress.setVisible(True)
        if not busy:
            self.progress.setValue(100)
        self.btn_scan.setEnabled(not busy)
        self.btn_process.setEnabled(not busy)
        self.btn_batch.setEnabled(not busy)
        self.btn_add.setEnabled(not busy)
        self.btn_export.setEnabled(False)

    def _on_error(self, msg: str):
        self._set_busy(False, "出错")
        self.btn_cancel.setEnabled(False)
        self._cancel_token = None
        task = self._active_task()
        if task is not None:
            services = self.services
            if services is not None:
                try:
                    services.pipeline.mark_task_unsafe(
                        task, reason=msg, error_code="gui_error")
                except InvalidTaskTransition:
                    # 终态不可回退：保留原终态
                    pass
                except Exception as persist_exc:
                    task.error = msg
                    msg = (f"{msg}\n\n无法持久化失败状态："
                           f"{type(persist_exc).__name__}: {persist_exc}")
            else:
                task.error = msg
            self._refresh_file_list()
        QMessageBox.critical(self, "错误", msg)

    def shutdown(self):
        if self.services is not None:
            self.services.close()
            self.services = None

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)


def run():
    try:
        from ...infrastructure.config.settings import get_settings
        from ...infrastructure.logging.logger import configure_logging
        configure_logging(get_settings().logging)
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei", 10))
    ensure_dirs()
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
