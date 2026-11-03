"""指标：P/R/F1、误报率/漏报率/复核率，按实体类型分组。"""
from __future__ import annotations

from collections import defaultdict


def prf1(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4)}


def suppress_metrics(tp_suppress: int, fp_suppress: int) -> dict:
    """抑制准确性：应为抑制却输出为活跃 = 误报（过度脱敏风险）。"""
    accuracy = tp_suppress / (tp_suppress + fp_suppress) if (tp_suppress + fp_suppress) > 0 else 0.0
    return {"correctly_suppressed": tp_suppress,
            "should_suppress_but_active": fp_suppress,
            "suppress_accuracy": round(accuracy, 4)}


def group_by_type(records: list[dict]) -> dict:
    """records: [{category, tp, fp, fn}] -> {category: prf1(...)}"""
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for r in records:
        a = agg[r["category"]]
        a[0] += r["tp"]; a[1] += r["fp"]; a[2] += r["fn"]
    return {cat: prf1(*vals) for cat, vals in agg.items()}
