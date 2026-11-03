# -*- coding: utf-8 -*-
"""语料闭环工具测试：导入草稿、坏例导出/合并、词库回灌(dry-run)、趋势落盘对比、合成语料。

全部在 tmp 目录隔离，不触碰真实 resources/ 与 data/。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from localdocmask.evaluation import annotation, badcase, trend, synthetic_corpus
from localdocmask.evaluation.dataset import Dataset, LabeledCase
from localdocmask.evaluation.runner import EvaluationRunner


def test_split_sentences():
    sents = annotation.split_sentences("甲方杭州云启科技有限公司签约。联系人张伟！")
    texts = [s[0] for s in sents]
    assert any("杭州云启科技有限公司" in t for t in texts)
    assert any("张伟" in t for t in texts)


def test_draft_roundtrip(tmp_path: Path):
    text = "供应商：星轨数据服务（示例）有限公司，手机13812345678。"
    drafts = [annotation.AnnotationDraft(
        text=text, expected=[["公司名", "星轨数据服务（示例）有限公司"],
                             ["手机号", "13812345678"]],
        provenance={"synthetic": True, "source_file": "x.txt"})]
    out = tmp_path / "draft.yaml"
    annotation.write_draft(drafts, out)
    loaded = annotation.load_labeled(out)
    assert loaded[0].text == text
    assert loaded[0].expected[0] == ["公司名", "星轨数据服务（示例）有限公司"]
    assert loaded[0].provenance["synthetic"] is True


def test_badcase_collect_export_merge(tmp_path: Path):
    # 一条应抑制负例被误判为活跃 -> SUPPRESS_FAIL/FP
    ds = Dataset([
        LabeledCase(text="我们公司的产品已上线。", true_entities=[]),
        LabeledCase(text="供应商：星轨数据服务（示例）有限公司。",
                    true_entities=[["公司名", "星轨数据服务（示例）有限公司"]]),
    ])
    runner = EvaluationRunner()
    bad = badcase.collect_badcases(ds, runner)
    out = tmp_path / "bad.yaml"
    badcase.export_badcases(bad, out)
    assert out.exists()

    # 合并到一个空回归集（dry-run 不写）
    target = tmp_path / "regression.yaml"
    target.write_text(yaml.safe_dump({"cases": []}, allow_unicode=True),
                      encoding="utf-8")
    preview = badcase.merge_badcases(bad, target, dry_run=True)
    assert preview["dry_run"] is True
    # dry-run 后文件仍为空
    after = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert len(after["cases"]) == 0
    # 真写
    written = badcase.merge_badcases(bad, target, dry_run=False)
    assert written["added"] == len(bad)


def test_feedback_lexicon_dry_run(tmp_path: Path):
    rdir = tmp_path / "resources"
    rdir.mkdir()
    (rdir / "blacklist.txt").write_text("# 示例\n", encoding="utf-8")
    bcs = [badcase.BadCase(text="x", failure_type="FN",
                           expected=[["公司名", "某内部公司（示例）有限公司"]])]
    preview = badcase.feedback_lexicon(bcs, rdir, dry_run=True)
    assert preview["dry_run"] is True
    assert "某内部公司（示例）有限公司" in preview["plan"]["blacklist.txt"]
    # dry-run 不写
    assert "某内部公司" not in (rdir / "blacklist.txt").read_text(encoding="utf-8")
    written = badcase.feedback_lexicon(bcs, rdir, dry_run=False)
    assert written["would_append"].get("blacklist.txt") == 1


def test_trend_save_compare(tmp_path: Path):
    rdir = tmp_path / "results"
    r1 = {"cases": 10, "overall": {"tp": 8, "fp": 1, "fn": 1,
          "precision": 0.8, "recall": 0.8, "f1": 0.8},
          "decision_counts": {"auto_confirm": 5, "needs_review": 3, "suppress": 2}}
    p1 = trend.save_result(r1, rdir, dataset_name="t")
    r2 = {"cases": 10, "overall": {"tp": 9, "fp": 0, "fn": 1,
          "precision": 1.0, "recall": 0.9, "f1": 0.947},
          "decision_counts": {"auto_confirm": 6, "needs_review": 2, "suppress": 2}}
    p2 = trend.save_result(r2, rdir, dataset_name="t")
    hist = trend.load_history(rdir)
    assert len(hist) == 2
    cmp = trend.compare(hist[0], hist[1])
    assert cmp["delta"]["f1"] > 0
    assert cmp["regression_flag"] in (True, False)


def test_synthetic_corpus_runs_and_evaluates():
    cases = synthetic_corpus.generate(60, seed=1)
    assert 50 <= len(cases) <= 60
    assert all("synthetic" in (c.note or "") or c.file_type == "synthetic"
               for c in cases)
    r = EvaluationRunner().run(Dataset(cases))
    assert r["cases"] == len(cases)
    assert 0.5 <= r["overall"]["f1"] <= 1.0
    drafts = synthetic_corpus.to_drafts(cases)
    assert drafts[0].provenance["synthetic"] is True
