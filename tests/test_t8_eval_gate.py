# -*- coding: utf-8 -*-
"""T8 版本化评测套件、发布门禁、报告和基线审计测试。"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import yaml

from localdocmask.evaluation.gate import (
    DEFAULT_TOLERANCES,
    EvalGateError,
    render_markdown,
    run_eval_gate,
    update_baseline_from_report,
    write_json_report,
)
from localdocmask.evaluation.suite import compute_suite_hash, inspect_suite


def _manifest(base: dict, *, sample_count: int = 1,
              category_counts: dict | None = None,
              format_counts: dict | None = None) -> dict:
    data = {
        "schema_version": 1,
        "suite_id": "test-suite",
        "suite_version": "1.0.0",
        "samples": [{
            "path": "cases.yaml",
            "sha256": hashlib.sha256((base / "cases.yaml").read_bytes()).hexdigest(),
            "kind": "labeled_cases",
            "sample_count": sample_count,
        }],
        "sample_count": sample_count,
        "category_counts": category_counts or {"姓名": sample_count},
        "file_format_counts": format_counts or {"text": sample_count},
        "synthetic": True,
        "provenance": "synthetic test fixture",
        "created_at": "2026-10-30T00:00:00Z",
        "compatible_policy_versions": ["recognition-v1"],
        "compatible_tool_versions": ["0.1.0"],
        "baseline": {"path": "", "sha256": ""},
        "ready": True,
        "missing_sources": [],
        "suite_hash": "",
    }
    data["suite_hash"] = compute_suite_hash(data)
    return data


def _make_suite(tmp_path: Path, *, text: str = "联系人测试甲。",
                file_type: str = "text", safety: bool = False,
                sample_count: int = 1, category_counts: dict | None = None,
                format_counts: dict | None = None) -> tuple[Path, dict]:
    suite_dir = tmp_path / "suites" / "test-suite"
    suite_dir.mkdir(parents=True)
    cases = [{
        "case_id": "case-0001",
        "text": text,
        "true_entities": [["姓名", "测试甲"]],
        "file_type": file_type,
        "safety_critical": safety,
        "note": "synthetic",
    }]
    for i in range(2, sample_count + 1):
        cases.append({
            "case_id": f"case-{i:04d}",
            "text": f"联系人测试{i}。",
            "true_entities": [["姓名", f"测试{i}"]],
            "file_type": file_type,
            "safety_critical": safety,
            "note": "synthetic",
        })
    (suite_dir / "cases.yaml").write_text(yaml.safe_dump({
        "meta": {
            "suite_id": "test-suite",
            "suite_version": "1.0.0",
            "synthetic": True,
            "description": "synthetic test fixture",
        },
        "cases": cases,
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")
    manifest = _manifest(suite_dir, sample_count=sample_count,
                         category_counts=category_counts,
                         format_counts=format_counts or {file_type: sample_count})
    (suite_dir / "suite.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    return suite_dir, manifest


def _result(**overrides) -> dict:
    data = {
        "execution_status": "completed",
        "quality_status": "not_evaluated",
        "overall": {"tp": 10, "fp": 0, "fn": 0, "precision": 1.0,
                    "recall": 1.0, "f1": 1.0},
        "per_category": {"姓名": {"tp": 10, "fp": 0, "fn": 0,
                                  "precision": 1.0, "recall": 1.0, "f1": 1.0}},
        "per_file_format": {"text": {"tp": 10, "fp": 0, "fn": 0,
                                     "precision": 1.0, "recall": 1.0, "f1": 1.0}},
        "per_recognition_source": {"regex": {"tp": 10, "fp": 0, "fn": 0,
                                             "precision": 1.0, "recall": 1.0, "f1": 1.0}},
        "suppress": {"correctly_suppressed": 5, "should_suppress_active": 0,
                     "suppress_accuracy": 1.0},
        "suppress_accuracy": 1.0,
        "review_rate": 0.0,
        "decision_counts": {"auto_confirm": 10, "needs_review": 0, "suppress": 5},
        "failed_samples": [],
        "safety_failures": [],
        "tool_version": "0.1.0",
        "policy_version": "recognition-v1",
        "config_hash": "test-config",
    }
    data.update(overrides)
    return data


class FakeRunner:
    def __init__(self, result: dict | None = None, *, explode: bool = False):
        self.result = copy.deepcopy(result or _result())
        self.explode = explode
        self.called = False

    def run(self, dataset):
        self.called = True
        if self.explode:
            raise RuntimeError("must not expose fixture text")
        return copy.deepcopy(self.result)


def _baseline(manifest: dict, *, overall: dict | None = None,
              suppress: float = 1.0,
              per_category: dict | None = None) -> dict:
    status = inspect_suite
    return {
        "schema_version": 1,
        "suite_id": manifest["suite_id"],
        "suite_version": manifest["suite_version"],
        "suite_hash": manifest["suite_hash"],
        "metrics": {
            "overall": overall or {"tp": 10, "fp": 0, "fn": 0,
                                   "precision": 1.0, "recall": 1.0, "f1": 1.0},
            "suppress_accuracy": suppress,
            "per_category": per_category or {
                "姓名": {"tp": 10, "fp": 0, "fn": 0,
                         "precision": 1.0, "recall": 1.0, "f1": 1.0}},
        },
        "p0_categories": ["姓名"],
        "tolerances": dict(DEFAULT_TOLERANCES),
        "update_reason": "fixture",
        "updated_at": "2026-10-30T00:00:00Z",
    }


def _write_baseline(path: Path, baseline: dict) -> Path:
    path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_fixed_suite_hash_is_deterministic_and_ready(tmp_path: Path):
    suite_dir, manifest = _make_suite(tmp_path)
    first = inspect_suite(suite_dir)
    second = inspect_suite(suite_dir)
    assert first["ready"] is True
    assert first["computed_suite_hash"] == manifest["suite_hash"] == second["computed_suite_hash"]


def test_fixed_suite_runs_when_hash_matches(tmp_path: Path):
    suite_dir, manifest = _make_suite(tmp_path)
    baseline = _write_baseline(tmp_path / "baseline.json", _baseline(manifest))
    report = run_eval_gate(suite=suite_dir, baseline_path=baseline,
                           runner=FakeRunner())
    assert report["execution_status"] == "completed"
    assert report["quality_status"] == "passed"
    assert report["gate_status"] == "passed"
    assert report["suite_hash"] == manifest["suite_hash"]


def test_missing_sample_is_reported_and_blocks_gate(tmp_path: Path):
    suite_dir, manifest = _make_suite(tmp_path)
    (suite_dir / "cases.yaml").unlink()
    baseline = _write_baseline(tmp_path / "baseline.json", _baseline(manifest))
    report = run_eval_gate(suite=suite_dir, baseline_path=baseline,
                           runner=FakeRunner())
    assert report["missing_sources"] == ["cases.yaml"]
    assert "missing_sources" in report["failure_reasons"]
    assert report["gate_status"] == "failed"
    assert report["quality_status"] == "not_evaluated"


def test_modified_sample_hash_blocks_gate(tmp_path: Path):
    suite_dir, manifest = _make_suite(tmp_path)
    with (suite_dir / "cases.yaml").open("a", encoding="utf-8") as stream:
        stream.write("\n# tampered\n")
    baseline = _write_baseline(tmp_path / "baseline.json", _baseline(manifest))
    report = run_eval_gate(suite=suite_dir, baseline_path=baseline,
                           runner=FakeRunner())
    assert report["hash_mismatches"]
    assert "sample_hash_mismatch" in report["failure_reasons"]
    assert report["gate_status"] == "failed"


def test_manifest_suite_hash_tamper_blocks_gate(tmp_path: Path):
    suite_dir, manifest = _make_suite(tmp_path)
    manifest["suite_hash"] = "0" * 64
    (suite_dir / "suite.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    baseline = _write_baseline(tmp_path / "baseline.json", _baseline(manifest))
    report = run_eval_gate(suite=suite_dir, baseline_path=baseline,
                           runner=FakeRunner())
    assert "suite_hash_mismatch" in report["integrity_errors"]
    assert report["gate_status"] == "failed"


def test_expected_baseline_hash_mismatch_blocks_gate(tmp_path: Path):
    suite_dir, manifest = _make_suite(tmp_path)
    baseline_path = _write_baseline(tmp_path / "baseline.json", _baseline(manifest))
    manifest["baseline"]["sha256"] = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    (suite_dir / "suite.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    baseline_path.write_text(baseline_path.read_text(encoding="utf-8") + "\n",
                             encoding="utf-8")
    report = run_eval_gate(suite=suite_dir, baseline_path=baseline_path,
                           runner=FakeRunner())
    assert report["failure_reasons"] == ["baseline_hash_mismatch"]
    assert report["quality_status"] == "not_evaluated"


def test_execution_exception_is_not_evaluated(tmp_path: Path):
    suite_dir, manifest = _make_suite(tmp_path)
    baseline = _write_baseline(tmp_path / "baseline.json", _baseline(manifest))
    report = run_eval_gate(suite=suite_dir, baseline_path=baseline,
                           runner=FakeRunner(explode=True))
    assert report["execution_status"] == "failed"
    assert report["quality_status"] == "not_evaluated"
    assert report["gate_status"] == "failed"
    assert report["failure_reasons"] == ["evaluation_execution_failed"]


def test_matching_baseline_passes(tmp_path: Path):
    suite_dir, manifest = _make_suite(tmp_path)
    baseline = _write_baseline(tmp_path / "baseline.json", _baseline(manifest))
    report = run_eval_gate(suite=suite_dir, baseline_path=baseline,
                           runner=FakeRunner())
    assert report["quality_status"] == "passed"
    assert report["baseline_delta"]["f1"] == 0.0
    assert report["regression_flag"] is False


def test_recall_regression_fails(tmp_path: Path):
    suite_dir, manifest = _make_suite(tmp_path)
    baseline = _write_baseline(tmp_path / "baseline.json", _baseline(
        manifest, overall={"tp": 10, "fp": 0, "fn": 0,
                           "precision": 1.0, "recall": 0.95, "f1": 0.97}))
    current = _result(overall={"tp": 10, "fp": 0, "fn": 2,
                               "precision": 1.0, "recall": 0.93, "f1": 0.96})
    report = run_eval_gate(suite=suite_dir, baseline_path=baseline,
                           runner=FakeRunner(current))
    assert "recall_regression" in report["failure_reasons"]
    assert report["quality_status"] == "failed"


def test_f1_regression_fails(tmp_path: Path):
    suite_dir, manifest = _make_suite(tmp_path)
    baseline = _write_baseline(tmp_path / "baseline.json", _baseline(
        manifest, overall={"tp": 10, "fp": 0, "fn": 0,
                           "precision": 1.0, "recall": 1.0, "f1": 0.97}))
    current = _result(overall={"tp": 10, "fp": 0, "fn": 0,
                               "precision": 1.0, "recall": 1.0, "f1": 0.95})
    report = run_eval_gate(suite=suite_dir, baseline_path=baseline,
                           runner=FakeRunner(current))
    assert "f1_regression" in report["failure_reasons"]


def test_suppress_accuracy_regression_fails(tmp_path: Path):
    suite_dir, manifest = _make_suite(tmp_path)
    baseline = _write_baseline(tmp_path / "baseline.json", _baseline(manifest, suppress=0.98))
    report = run_eval_gate(suite=suite_dir, baseline_path=baseline,
                           runner=FakeRunner(_result(suppress_accuracy=0.96)))
    assert "suppress_accuracy_regression" in report["failure_reasons"]


def test_safety_critical_failure_blocks_even_with_perfect_metrics(tmp_path: Path):
    suite_dir, manifest = _make_suite(tmp_path, safety=True)
    baseline = _write_baseline(tmp_path / "baseline.json", _baseline(manifest))
    current = _result(safety_failures=[{
        "case_id": "case-0001", "file_format": "text", "failure_codes": ["FP"]
    }])
    report = run_eval_gate(suite=suite_dir, baseline_path=baseline,
                           runner=FakeRunner(current))
    assert "safety_critical_failure" in report["failure_reasons"]
    assert report["gate_status"] == "failed"


def test_p0_category_regression_fails(tmp_path: Path):
    suite_dir, manifest = _make_suite(tmp_path)
    baseline_data = _baseline(manifest)
    baseline = _write_baseline(tmp_path / "baseline.json", baseline_data)
    current = _result(per_category={
        "姓名": {"tp": 10, "fp": 2, "fn": 0,
                 "precision": 0.995, "recall": 1.0, "f1": 0.995}
    })
    # 让低于 P0 阈值的类别变化可观察；总指标保持一致以隔离检查项。
    report = run_eval_gate(suite=suite_dir, baseline_path=baseline,
                           runner=FakeRunner(current))
    assert "p0_category_regression" not in report["failure_reasons"]
    baseline_data["metrics"]["per_category"]["姓名"]["f1"] = 1.0
    baseline_data["tolerances"]["p0_category_f1_abs"] = 0.001
    _write_baseline(tmp_path / "baseline.json", baseline_data)
    report = run_eval_gate(suite=suite_dir, baseline_path=tmp_path / "baseline.json",
                           runner=FakeRunner(current))
    assert "p0_category_regression" in report["failure_reasons"]


def test_report_outputs_do_not_contain_fixture_original_value(tmp_path: Path):
    secret = "绝密姓名赵测试"
    suite_dir, manifest = _make_suite(tmp_path, text=f"联系人{secret}。")
    baseline = _write_baseline(tmp_path / "baseline.json", _baseline(manifest))
    report = run_eval_gate(suite=suite_dir, baseline_path=baseline,
                           runner=FakeRunner())
    report_path = write_json_report(report, tmp_path / "report.json")
    markdown = render_markdown(report)
    assert secret not in report_path.read_text(encoding="utf-8")
    assert secret not in markdown
    assert "不代表真实生产精度" in markdown


def test_dry_run_does_not_execute_runner(tmp_path: Path):
    suite_dir, _ = _make_suite(tmp_path)
    runner = FakeRunner(explode=True)
    report = run_eval_gate(suite=suite_dir, dry_run=True, runner=runner)
    assert runner.called is False
    assert report["execution_status"] == "completed"
    assert report["quality_status"] == "not_evaluated"
    assert report["gate_status"] == "passed"


def test_baseline_update_requires_reason_and_preserves_history(tmp_path: Path):
    suite_dir, manifest = _make_suite(tmp_path)
    baseline_path = _write_baseline(tmp_path / "baseline.json", _baseline(manifest))
    report = run_eval_gate(suite=suite_dir, baseline_path=baseline_path,
                           runner=FakeRunner())
    with __import__("pytest").raises(EvalGateError, match="baseline_update_reason_required"):
        update_baseline_from_report(report=report, baseline_path=baseline_path, reason="")
    result = update_baseline_from_report(
        report=report, baseline_path=baseline_path, reason="升级测试基线",
        suite_manifest_path=suite_dir / "suite.json")
    assert result["backup_preserved"] is True
    assert result["previous_baseline_sha256"]
    assert Path(result["audit_path"]).is_file()
    assert list((tmp_path / "history").glob("baseline.*.json"))
    updated = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert updated["update_reason"] == "升级测试基线"
    manifest_after = json.loads((suite_dir / "suite.json").read_text(encoding="utf-8"))
    assert manifest_after["baseline"]["sha256"] == result["new_baseline_sha256"]
    assert inspect_suite(suite_dir)["ready"] is True


def test_report_write_failure_is_explicit(tmp_path: Path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    with __import__("pytest").raises(EvalGateError, match="report_write_failed"):
        write_json_report({"ok": True}, blocker / "report.json")


def test_eval_gate_cli_exit_codes(tmp_path: Path):
    suite_dir, _ = _make_suite(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "eval_gate.py"
    ok = subprocess.run([
        sys.executable, str(script), "--suite", str(suite_dir),
        "--report", str(tmp_path / "dry.json"), "--dry-run",
    ], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert ok.returncode == 0, ok.stderr
    bad = subprocess.run([
        sys.executable, str(script), "--suite", str(tmp_path / "missing-suite"),
        "--report", str(tmp_path / "bad.json"), "--dry-run",
    ], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert bad.returncode != 0


def test_versioned_project_suite_is_ready_and_passes_real_runner(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    suite_dir = root / "evaluation" / "suites" / "synthetic-recognition-v1"
    baseline = root / "evaluation" / "baselines" / "synthetic-recognition-v1.baseline.json"
    report = run_eval_gate(suite=suite_dir, baseline_path=baseline)
    assert report["sample_count"] == 101
    assert report["suite_id"] == "synthetic-recognition-v1"
    assert report["execution_status"] == "completed"
    assert report["quality_status"] == "passed"
    assert report["gate_status"] == "passed"
