"""评测运行器：用 RecognitionPipeline 跑标注集，与真值比对出结构化聚合指标。

匹配口径：以 (类别, 归一化文本) 精确匹配。报告只包含聚合指标、样本 ID 和
失败码，不包含待评测正文或候选原文。
"""
from __future__ import annotations

from collections import defaultdict

from .. import __version__
from ..infrastructure.config.settings import Settings, compute_config_hash
from ..recognition.pipeline import RecognitionPipeline
from ..recognition.rule_based import Detector
from .dataset import Dataset
from .metrics import prf1, suppress_metrics

# 模拟运维侧提供的人员名单（词典命中路径，source=dict）
_DEMO_PERSONS = ["张伟", "王芳"]


def _new_counts() -> list[int]:
    return [0, 0, 0]


def _metrics(groups: dict[str, list[int]]) -> dict:
    return {key: prf1(*counts) for key, counts in sorted(groups.items())}


class EvaluationRunner:
    def __init__(self, pipeline: RecognitionPipeline | None = None):
        if pipeline is None:
            pipeline = RecognitionPipeline(
                detector=Detector(persons=list(_DEMO_PERSONS)))
        self.pipeline = pipeline

    def run(self, dataset: Dataset) -> dict:
        total_tp = total_fp = total_fn = 0
        by_category: dict[str, list[int]] = defaultdict(_new_counts)
        by_format: dict[str, list[int]] = defaultdict(_new_counts)
        by_source: dict[str, list[int]] = defaultdict(_new_counts)
        correct_suppress = 0
        should_suppress_active = 0
        reviewed = 0
        auto_confirmed = 0
        suppressed = 0
        active_count = 0
        failed_samples: list[dict] = []
        safety_failures: list[dict] = []

        for case in dataset:
            out = self.pipeline.analyze(case.text, location="eval")
            active = [c for c in out if not c.excluded]
            active_count += len(active)
            truth = {(cat, txt.strip()) for cat, txt in case.true_entities}
            remaining = set(truth)

            for c in out:
                if c.decision and c.decision.value == "auto_confirm":
                    auto_confirmed += 1
                elif c.decision and c.decision.value == "suppress":
                    suppressed += 1
                elif c.decision and c.decision.value == "needs_review":
                    reviewed += 1

            case_tp = case_fp = case_fn = 0
            for c in active:
                key = (c.category, c.text.strip())
                source = c.source or "unknown"
                if key in remaining:
                    total_tp += 1
                    case_tp += 1
                    by_category[c.category][0] += 1
                    by_source[source][0] += 1
                    remaining.remove(key)
                else:
                    total_fp += 1
                    case_fp += 1
                    by_category[c.category][1] += 1
                    by_source[source][1] += 1
            for category, _ in remaining:
                total_fn += 1
                case_fn += 1
                by_category[category][2] += 1
                by_source["unmatched_truth"][2] += 1

            format_name = case.file_type or "unknown"
            by_format[format_name][0] += case_tp
            by_format[format_name][1] += case_fp
            by_format[format_name][2] += case_fn

            # 负例（真值为空）：不应有活跃输出
            if not truth:
                if active:
                    should_suppress_active += 1
                else:
                    correct_suppress += 1

            failure_codes = []
            if case_fn:
                failure_codes.append("FN")
            if case_fp:
                failure_codes.append("FP")
            if not truth and active:
                failure_codes.append("SUPPRESSION_FP")
            if failure_codes:
                failed = {
                    "case_id": case.case_id,
                    "file_format": format_name,
                    "failure_codes": failure_codes,
                    "tp": case_tp,
                    "fp": case_fp,
                    "fn": case_fn,
                }
                failed_samples.append(failed)
                if case.safety_critical:
                    safety_failures.append({
                        "case_id": case.case_id,
                        "file_format": format_name,
                        "failure_codes": failure_codes,
                    })

        overall = prf1(total_tp, total_fp, total_fn)
        suppress = suppress_metrics(correct_suppress, should_suppress_active)
        decision_counts = {
            "auto_confirm": auto_confirmed,
            "needs_review": reviewed,
            "suppress": suppressed,
        }
        per_category = _metrics(by_category)
        report = {
            "execution_status": "completed",
            "quality_status": "not_evaluated",
            "suite_id": dataset.suite_id,
            "suite_version": dataset.suite_version,
            "suite_hash": "",
            "tool_version": __version__,
            "policy_version": "",
            "config_hash": compute_config_hash(Settings()),
            "synthetic": bool(dataset.synthetic),
            "provenance": dataset.source_note,
            "cases": len(dataset),
            "sample_count": len(dataset),
            "overall": overall,
            "per_type": per_category,
            "per_category": per_category,
            "per_file_format": _metrics(by_format),
            "per_recognition_source": _metrics(by_source),
            "suppress": suppress,
            "suppress_accuracy": suppress["suppress_accuracy"],
            "review_rate": round(reviewed / active_count, 4) if active_count else 0.0,
            "decision_counts": decision_counts,
            "missing_sources": [],
            "failed_samples": failed_samples,
            "safety_failures": safety_failures,
            "failure_reasons": [],
            "baseline_delta": {},
            "regression_flag": False,
        }
        try:
            from ..application.pipeline import POLICY_VERSION
            report["policy_version"] = POLICY_VERSION
        except Exception:
            report["policy_version"] = "unknown"
        return report


__all__ = ["EvaluationRunner"]
