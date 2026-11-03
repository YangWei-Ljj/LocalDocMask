# -*- coding: utf-8 -*-
"""BatchService：分组、失败隔离、共享 mapper、进度、取消、失败关闭。"""
from pathlib import Path

import pytest

from localdocmask.application.batch import BatchService, CancelToken
from localdocmask.domain.entities import (
    AuditFinding,
    AuditReport,
    Candidate,
    ScanResult,
    Task,
)
from localdocmask.domain.enums import AuditVerdict, EntityDecision, TaskStatus
from localdocmask.domain.state_machine import transition_task


class FakePipeline:
    """桩流水线：按文件名配置每个文件的结局。

    outcome:
      passed           —— 成功并 finalize 为 PASSED
      review           —— scan 返回 NEEDS_REVIEW 条目（不导出）
      fail             —— process 抛异常（失败隔离）
      fail_at_prepare  —— prepare 抛异常
    """

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.processed = []      # 实际进入 process 的文件名
        self.mappers_seen = []   # 注入的 mapper 实例（验共享）

    def prepare(self, source):
        name = Path(str(source)).name
        if self.outcomes.get(name) == "fail_at_prepare":
            raise FileNotFoundError(f"找不到 {name}")
        return Task(source_path=Path(str(source)))

    def scan(self, task):
        name = task.source_path.name
        if self.outcomes.get(name) == "review":
            c = Candidate(text="敏感原文-不应出现在报告", category="自定义敏感词")
            c.decision = EntityDecision.NEEDS_REVIEW.value
            return ScanResult(candidates=[c])
        return ScanResult(candidates=[])

    def process(self, task, mode, mask, candidates, mapper=None):
        name = task.source_path.name
        self.mappers_seen.append(mapper)
        self.processed.append(name)
        if self.outcomes.get(name) == "fail":
            raise RuntimeError("写入失败")
        task.output_path = Path("out") / name
        task.audit = AuditReport(passed=True,
                                 findings=[AuditFinding(AuditVerdict.NO_RESIDUAL, "ok")])
        return task.output_path

    def finalize(self, task, report):
        transition_task(task, TaskStatus.PROCESSED_UNAUDITED, reason="fake_finalize")
        transition_task(task, TaskStatus.PASSED, reason="fake_finalize")
        return task


def _make_sources(tmp_path, names):
    srcs = []
    for n in names:
        p = tmp_path / n
        p.write_text("x", encoding="utf-8")
        srcs.append(p)
    return srcs


def test_all_passed_grouped(tmp_path):
    srcs = _make_sources(tmp_path, ["a.docx", "b.docx"])
    pipe = FakePipeline({"a.docx": "passed", "b.docx": "passed"})
    res = BatchService(pipe).run(srcs, "b", "****")
    assert len(res.passed) == 2
    assert res.review == []
    assert res.failed == []


def test_review_files_not_exported(tmp_path):
    srcs = _make_sources(tmp_path, ["ok.docx", "review.docx"])
    pipe = FakePipeline({"ok.docx": "passed", "review.docx": "review"})
    res = BatchService(pipe).run(srcs, "b", "****")
    assert len(res.passed) == 1
    assert len(res.review) == 1
    assert res.review[0].status == TaskStatus.NEEDS_REVIEW
    # 含待复核的文件不得进入 process（不导出）
    assert "review.docx" not in pipe.processed
    assert "ok.docx" in pipe.processed


def test_failure_isolation(tmp_path):
    srcs = _make_sources(tmp_path, ["good1.docx", "boom.docx", "good2.docx"])
    pipe = FakePipeline({"good1.docx": "passed", "boom.docx": "fail",
                         "good2.docx": "passed"})
    res = BatchService(pipe).run(srcs, "b", "****")
    assert len(res.passed) == 2
    assert len(res.failed) == 1
    assert res.failed[0].source_path.name == "boom.docx"
    # 失败文件之后的文件仍被处理
    assert "good2.docx" in pipe.processed


def test_prepare_failure_is_isolated(tmp_path):
    srcs = _make_sources(tmp_path, ["bad.docx", "fine.docx"])
    pipe = FakePipeline({"bad.docx": "fail_at_prepare", "fine.docx": "passed"})
    res = BatchService(pipe).run(srcs, "b", "****")
    assert len(res.failed) == 1
    assert len(res.passed) == 1


def test_progress_callback_invoked(tmp_path):
    srcs = _make_sources(tmp_path, ["a.docx"])
    pipe = FakePipeline({"a.docx": "passed"})
    calls = []
    BatchService(pipe).run(srcs, "b", "****",
                           progress_cb=lambda i, t, n, s: calls.append((i, t, n, s)))
    stages = [c[3] for c in calls]
    assert stages == ["prepare", "scan", "process", "finalize"]
    assert calls[0][1] == 1 and calls[0][2] == "a.docx"


def test_cancel_token_stops_further_files(tmp_path):
    srcs = _make_sources(tmp_path, ["a.docx", "b.docx", "c.docx"])
    pipe = FakePipeline({n: "passed" for n in ("a.docx", "b.docx", "c.docx")})
    token = CancelToken()

    def cb(i, t, n, s):
        if n == "a.docx" and s == "finalize":
            token.cancel()

    res = BatchService(pipe).run(srcs, "b", "****",
                                 progress_cb=cb, cancel_token=token)
    assert [t.source_path.name for t in res.passed] == ["a.docx"]
    assert "b.docx" not in pipe.processed
    assert "c.docx" not in pipe.processed
    assert any("取消" in w for w in res.warnings)


def test_batch_shares_one_mapper_across_files(tmp_path):
    srcs = _make_sources(tmp_path, ["a.docx", "b.docx"])
    pipe = FakePipeline({"a.docx": "passed", "b.docx": "passed"})
    BatchService(pipe).run(srcs, "b", "****")
    assert len(pipe.mappers_seen) == 2
    assert pipe.mappers_seen[1] is pipe.mappers_seen[0]


def test_warnings_contain_no_plaintext_original(tmp_path):
    srcs = _make_sources(tmp_path, ["review.docx", "boom.docx"])
    pipe = FakePipeline({"review.docx": "review", "boom.docx": "fail"})
    res = BatchService(pipe).run(srcs, "b", "****")
    blob = "\n".join(res.warnings)
    assert "敏感原文" not in blob
