# -*- coding: utf-8 -*-
"""安全与稳定性加固专项测试（path containment / log leakage / corrupt &
fail-closed / 临时目录受控+崩溃恢复 / 资源上限 / 重复运行幂等）。

所有断言均为真实行为断言：不放宽门槛、不删测试。数据目录由 conftest
的 LOCALDOCMASK_HOME 隔离到临时目录，绝不触碰项目 data/。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from docx import Document

from conftest import VALID_ID, luhn_valid
from localdocmask.application.batch import BatchService
from localdocmask.application.pipeline import POLICY_VERSION, TaskManager
from localdocmask.domain.entities import Task
from localdocmask.domain.enums import TaskStatus
from localdocmask.domain.exceptions import (
    ConfigError,
    ErrorCode,
    SecurityViolation,
)
from localdocmask.infrastructure.config.settings import (
    PROCESSING_DIR,
    Settings,
)
from localdocmask.infrastructure.config.settings import LoggingConfig
from localdocmask.infrastructure.logging import logger as logmod
from localdocmask.infrastructure.persistence.recovery import RecoveryService
from localdocmask.infrastructure.persistence.sqlite_repo import SqliteTaskRepository
from localdocmask.recognition.rule_based import Detector
from localdocmask.security.path_safety import (
    is_within,
    resolve_within,
    safe_join,
)
from localdocmask.security.limits import ResourceLimits

PHONE = "13812345678"
EMAIL = "zhangsan@example.com"
BANK = luhn_valid("622202020012345")  # 16 位 Luhn 合法银行卡
CUSTOM = "CUSTOMSECRET987654321"


def _detector():
    return Detector(companies=["字节跳动"], persons=["张三", "李四"])


def _clean_docx(path: Path) -> Path:
    """无任何敏感内容的合法 docx（用于整链路通过的对照组）。"""
    d = Document()
    d.add_paragraph("这是一段普通内容，用于稳定性对照。")
    d.save(str(path))
    return path


# ===========================================================================
# 1. 路径越权 / 目录包含
# ===========================================================================
class TestPathContainment:
    def test_dotdot_escape_blocked(self, tmp_path):
        with pytest.raises(SecurityViolation):
            resolve_within(tmp_path, "..\\..\\..\\Windows\\System32\\x.docx")

    def test_absolute_escape_blocked(self, tmp_path):
        with pytest.raises(SecurityViolation):
            resolve_within(tmp_path, "C:\\Windows\\System32\\evil.docx")

    def test_safe_join_rejects_traversal_segment(self, tmp_path):
        with pytest.raises(SecurityViolation):
            safe_join(tmp_path, "..\\evil.docx")
        with pytest.raises(SecurityViolation):
            safe_join(tmp_path, "sub", "..", "..", "outside.docx")

    def test_safe_join_rejects_absolute_segment(self, tmp_path):
        with pytest.raises(SecurityViolation):
            safe_join(tmp_path, Path("C:/evil.docx"))

    def test_legitimate_relative_path_allowed(self, tmp_path):
        out = resolve_within(tmp_path, "sub/dir/正常文件.docx")
        assert is_within(out, [tmp_path])
        assert out.name == "正常文件.docx"

    def test_is_within_negative(self, tmp_path):
        outside = tmp_path.parent / "somewhere_else"
        assert not is_within(outside, [tmp_path])
        assert is_within(tmp_path / "a.txt", [tmp_path])

    def test_symlink_escape_blocked(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        link = root / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("当前环境不允许创建符号链接（Windows 需开发者模式/管理员）")
        with pytest.raises(SecurityViolation):
            resolve_within(root, "link/evil.docx")

    # --- pipeline 集成：阶段产物路径必须收敛在受控目录 ---
    def test_pipeline_out_path_stays_in_processing_dir(self, tmp_path):
        src = _clean_docx(tmp_path / "src.docx")
        tm = TaskManager(_detector())
        task = Task(source_path=src)
        out = tm._out_path(task, "正常文件_脱敏")
        assert is_within(out, [PROCESSING_DIR]), f"输出路径逃逸：{out}"
        assert out.parent.resolve() == Path(PROCESSING_DIR).resolve()

    def test_pipeline_out_path_rejects_traversal_stem(self, tmp_path):
        src = _clean_docx(tmp_path / "src.docx")
        tm = TaskManager(_detector())
        task = Task(source_path=src)
        # 文件名/任务对象被构造为含穿越段：必须失败关闭，绝不写出去
        with pytest.raises(SecurityViolation):
            tm._out_path(task, "..\\..\\..\\Windows\\System32\\evil")


# ===========================================================================
# 2. 日志泄露：手机/身份证/邮箱/银行卡/自定义敏感值不得原文落盘
# ===========================================================================
class TestLogLeakage:
    def test_sensitive_values_redacted_in_json_log(self, tmp_path, monkeypatch):
        # 复位日志模块全局状态，避免被其他测试提前初始化
        monkeypatch.setattr(logmod, "_configured", False)
        monkeypatch.setattr(logmod, "_extra_sensitive", set())
        logging.getLogger("localdocmask").handlers.clear()

        log_dir = tmp_path / "logs"
        logmod.configure_logging(LoggingConfig(
            level="INFO", log_dir=str(log_dir),
            max_bytes=1024 * 1024, backup_count=1))
        logmod.register_sensitive_values([CUSTOM])

        logger = logmod.get_logger("hardening")
        logger.info("用户手机号 %s 邮箱 %s 银行卡 %s", PHONE, EMAIL, BANK)
        logger.info("身份证 %s 的办理记录", VALID_ID)
        logger.info("自定义敏感值：%s", CUSTOM)
        # extra 传递：历史缺陷路径（event 曾不经掩码直接落盘）
        logger.info("订单事件", extra={"event": f"下单{PHONE}完成", "task_id": f"T-{CUSTOM}"})

        # 冲刷 handler 后读回日志文件
        root = logging.getLogger("localdocmask")
        for h in root.handlers:
            h.flush()
        log_file = log_dir / "localdocmask.log"
        content = log_file.read_text(encoding="utf-8")

        # 每条日志都是合法 JSON，可逐行解析
        lines = [ln for ln in content.splitlines() if ln.strip()]
        assert lines, "日志文件为空"
        records = [json.loads(ln) for ln in lines]
        assert all("message" in r for r in records)

        # 完整原文一律不得出现在日志文件中
        for secret in (PHONE, VALID_ID, EMAIL, BANK, CUSTOM):
            assert secret not in content, f"敏感原文泄漏进日志：{secret!r}"

        # 掩码值必须出现（证明是掩码而非整条消息丢失）
        assert "138****5678" in content          # 手机掩码
        assert VALID_ID[:4] in content and VALID_ID[-4:] in content  # 身份证首尾保留
        assert EMAIL[:2] in content               # 邮箱首尾掩码

        # 复位，避免影响后续 build_services 的日志初始化
        monkeypatch.setattr(logmod, "_configured", False)
        logging.getLogger("localdocmask").handlers.clear()


# ===========================================================================
# 3. 损坏 / 不可解析文件：fail-closed，绝不标记成功
# ===========================================================================
class TestCorruptFailClosed:
    def _scan_expect_error(self, path: Path):
        tm = TaskManager(_detector())
        # T4：真实类型/容器校验必须在解析前完成，损坏文件在 prepare 即失败关闭。
        with pytest.raises(Exception):
            tm.prepare(path)

    def test_zero_byte_docx_rejected(self, tmp_path):
        p = tmp_path / "empty.docx"
        p.write_bytes(b"")
        self._scan_expect_error(p)

    def test_truncated_docx_rejected(self, tmp_path):
        # PK 魔数正确但内容被截断
        p = tmp_path / "trunc.docx"
        p.write_bytes(b"PK\x03\x04" + b"\x00" * 64 + "破损内容".encode("utf-8"))
        self._scan_expect_error(p)

    def test_truncated_pdf_rejected(self, tmp_path):
        p = tmp_path / "trunc.pdf"
        p.write_bytes(b"%PDF-1.4\n" + b"%%EOF broken garbage not a real pdf")
        self._scan_expect_error(p)

    def test_truncated_xlsx_rejected(self, tmp_path):
        p = tmp_path / "trunc.xlsx"
        p.write_bytes(b"PK\x03\x04" + b"\x01" * 40)
        self._scan_expect_error(p)

    def test_extension_content_mismatch_rejected(self, tmp_path):
        # 扩展名是 .docx，实际是纯文本
        p = tmp_path / "mismatch.docx"
        p.write_text("这根本不是 docx，只是文本。", encoding="utf-8")
        self._scan_expect_error(p)

    def test_batch_isolates_corrupt_without_breaking_good_file(self, tmp_path):
        bad = tmp_path / "bad.docx"
        bad.write_bytes(b"PK\x03\x04 broken")
        good = _clean_docx(tmp_path / "good.docx")

        batch = BatchService(TaskManager(_detector()))
        result = batch.run([str(bad), str(good)])

        # 损坏文件进 failed，绝不进 passed
        bad_names = {t.source_path.name for t in result.failed}
        assert "bad.docx" in bad_names
        passed_names = {t.source_path.name for t in result.passed}
        assert "bad.docx" not in passed_names
        # 同批正常文件不受影响，成功导出
        assert "good.docx" in passed_names
        good_task = next(t for t in result.passed if t.source_path.name == "good.docx")
        assert good_task.status == TaskStatus.PASSED
        assert Path(good_task.output_path).exists()


# ===========================================================================
# 4. 临时文件受控 + 崩溃恢复
# ===========================================================================
class TestRecovery:
    def test_intermediate_output_landed_in_processing_dir(self, tmp_path):
        src = _clean_docx(tmp_path / "c.docx")
        tm = TaskManager(_detector())
        task = tm.prepare(src, mode="a")
        tm.scan(task)
        tm.process(task, "a", "****", task.scan.candidates)
        # 阶段产物落在受控 processing 目录
        assert is_within(task.output_path, [PROCESSING_DIR]), task.output_path
        # 审计记录也在受控目录
        assert task.report_path is None or is_within(task.report_path, [PROCESSING_DIR])
        tm.finalize(task, task.audit)

    def test_recover_missing_output_back_to_pending(self, tmp_path):
        db = tmp_path / "rec.db"
        repo = SqliteTaskRepository(db)
        try:
            src = tmp_path / "s.docx"
            src.write_bytes(b"x")
            crash = Task(source_path=src, task_id="t-crash",
                         status=TaskStatus.PROCESSING, mode="a",
                         policy_version=POLICY_VERSION)
            crash.output_path = None  # 崩溃时未产出输出
            repo.save_task(crash)

            affected = RecoveryService().recover(repo, tmp_path / "processing")
            hit = next(t for t in affected if t.task_id == "t-crash")
            assert hit.status == TaskStatus.PENDING  # 可安全重试
            assert repo.load_task("t-crash").status == TaskStatus.PENDING
        finally:
            repo.close()

    def test_recover_residual_output_not_auto_deleted(self, tmp_path):
        db = tmp_path / "rec2.db"
        repo = SqliteTaskRepository(db)
        try:
            src = tmp_path / "s.docx"
            src.write_bytes(b"x")
            residual = tmp_path / "residual.docx"
            residual.write_bytes("半拉子产物".encode("utf-8"))
            task = Task(source_path=src, task_id="t-res",
                        status=TaskStatus.PROCESSING, mode="a",
                        policy_version=POLICY_VERSION)
            task.output_path = residual
            repo.save_task(task)

            affected = RecoveryService().recover(repo, tmp_path / "processing")
            hit = next(t for t in affected if t.task_id == "t-res")
            # 有残留产物 => 不自动认定安全，也绝不无条件删除临时产物
            assert hit.status == TaskStatus.INTERRUPTED
            assert residual.exists(), "残留产物被 RecoveryService 无条件删除"
        finally:
            repo.close()


# ===========================================================================
# 5. 并发 / 资源上限
# ===========================================================================
class TestResourceLimits:
    def test_settings_rejects_out_of_range_limits(self):
        s = Settings()
        s.security.max_workers = 0
        with pytest.raises(ConfigError):
            s._validate()
        s = Settings()
        s.security.max_file_size_mb = 0.0
        with pytest.raises(ConfigError):
            s._validate()
        s = Settings()
        s.security.task_timeout_seconds = 10
        with pytest.raises(ConfigError):
            s._validate()

    def test_default_limits_are_sane(self):
        limits = ResourceLimits.from_settings(Settings())
        assert 1 <= limits.max_workers <= 64
        assert limits.max_file_size_bytes > 0
        assert limits.task_timeout_seconds >= 30
        # settings 缺失时回退安全默认
        fallback = ResourceLimits.from_settings(None)
        assert fallback.max_workers == 2

    def test_oversized_file_rejected_at_prepare(self, tmp_path):
        src = _clean_docx(tmp_path / "big.docx")  # 约 200 字节
        s = Settings()
        s.security.max_file_size_mb = 0.0001  # ~105 KB 上限，该文件已超出
        tm = TaskManager(_detector(), settings=s)
        with pytest.raises(SecurityViolation) as ei:
            tm.prepare(src)
        assert ei.value.code == ErrorCode.RESOURCE_LIMIT

    def test_normal_size_passes_with_tight_limit(self, tmp_path):
        src = _clean_docx(tmp_path / "ok.docx")
        s = Settings()
        s.security.max_file_size_mb = 1.0  # 1MB 内正常放行
        tm = TaskManager(_detector(), settings=s)
        task = tm.prepare(src)
        assert task.source_path == src


# ===========================================================================
# 6. 重复运行幂等
# ===========================================================================
class TestIdempotency:
    def test_rerun_reuses_prior_success_no_duplicate(self, tmp_path):
        db = tmp_path / "idem.db"
        repo = SqliteTaskRepository(db)
        try:
            src = _clean_docx(tmp_path / "rpt.docx")
            tm = TaskManager(_detector(), repository=repo)

            task1 = tm.prepare(src, mode="a")
            tm.scan(task1)
            tm.process(task1, "a", "****", task1.scan.candidates)
            tm.finalize(task1, task1.audit)
            assert task1.status == TaskStatus.PASSED

            passed_after_first = [t for t in repo.list_tasks()
                                 if t.status == TaskStatus.PASSED]
            assert len(passed_after_first) == 1

            # 同一文件再次 prepare：命中已成功产物，标记可复用，不重复处理
            task2 = tm.prepare(src, mode="a")
            assert task2._reusable is not None
            assert task2._reusable.task_id == task1.task_id
            assert task2._reusable.status == TaskStatus.PASSED
            assert Path(task2._reusable.output_path).exists()

            # 幂等键查询命中同一条记录，不产生第二条成功结果
            hit = repo.find_by_source(task1.source_hash, "a", POLICY_VERSION)
            assert hit is not None and hit.task_id == task1.task_id
            assert len([t for t in repo.list_tasks()
                        if t.status == TaskStatus.PASSED]) == 1
        finally:
            repo.close()
