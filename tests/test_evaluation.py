# -*- coding: utf-8 -*-
from pathlib import Path

from localdocmask.evaluation.dataset import Dataset
from localdocmask.evaluation.metrics import prf1
from localdocmask.evaluation.runner import EvaluationRunner

_DATA = (Path(__file__).resolve().parents[1] / "src" / "localdocmask" /
         "evaluation" / "data" / "labeled_cases.yaml")


def test_prf1_basic():
    m = prf1(8, 2, 2)
    assert m["precision"] == 0.8
    assert m["recall"] == 0.8
    assert 0.7 < m["f1"] < 0.9


def test_dataset_loads():
    ds = Dataset.load(_DATA)
    assert 80 <= len(ds) <= 200


def test_runner_reports_real_metrics():
    ds = Dataset.load(_DATA)
    report = EvaluationRunner().run(ds)
    assert report["cases"] == len(ds)
    overall = report["overall"]
    # 真实比对：合成集上 F1 应落在合理区间，捕获明显回归
    assert overall["tp"] > 0
    assert 0.8 <= overall["f1"] <= 1.0
    assert "per_type" in report and report["per_type"]
    assert "suppress" in report
