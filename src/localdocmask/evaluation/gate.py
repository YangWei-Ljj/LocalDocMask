"""本地评测发布门禁：完整性检查、质量回归比较、报告和基线审计。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runner import EvaluationRunner
from .suite import DEFAULT_SUITES_DIR, SuiteError, inspect_suite, load_suite_dataset

REPORT_SCHEMA_VERSION = 1
BASELINE_SCHEMA_VERSION = 1
DEFAULT_TOLERANCES = {
    "recall_abs": 0.01,
    "f1_abs": 0.01,
    "suppress_accuracy_abs": 0.01,
    "p0_category_f1_abs": 0.01,
}


class EvalGateError(RuntimeError):
    """门禁执行、报告或基线更新失败。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
    except OSError as exc:
        raise EvalGateError("report_write_failed") from exc


def write_json_report(report: dict, path: str | Path) -> Path:
    target = Path(path)
    _atomic_write(target, _json_bytes(report))
    return target


def render_markdown(report: dict) -> str:
    overall = report.get("overall") or {}
    lines = [
        "# LocalDocMask 评测门禁报告",
        "",
        "> 回归基线，不代表真实生产精度。合成语料结果不得对外表述为真实生产精度。",
        "",
        f"- suite: `{report.get('suite_id', '')}`",
        f"- suite_version: `{report.get('suite_version', '')}`",
        f"- suite_hash: `{report.get('suite_hash', '')}`",
        f"- execution_status: `{report.get('execution_status', '')}`",
        f"- quality_status: `{report.get('quality_status', '')}`",
        f"- gate_status: `{report.get('gate_status', '')}`",
        f"- sample_count: `{report.get('sample_count', 0)}`",
        f"- synthetic: `{str(bool(report.get('synthetic'))).lower()}`",
        "",
        "## 总体指标",
        "",
        "| Precision | Recall | F1 | TP | FP | FN |",
        "|---:|---:|---:|---:|---:|---:|",
        f"| {overall.get('precision', 0)} | {overall.get('recall', 0)} | "
        f"{overall.get('f1', 0)} | {overall.get('tp', 0)} | "
        f"{overall.get('fp', 0)} | {overall.get('fn', 0)} |",
        "",
        f"- suppress_accuracy: `{report.get('suppress_accuracy', 0)}`",
        f"- review_rate: `{report.get('review_rate', 0)}`",
        "",
        "## 门禁结论",
        "",
    ]
    reasons = report.get("failure_reasons") or []
    if reasons:
        lines.extend(f"- `{reason}`" for reason in reasons)
    else:
        lines.append("- 无质量回归或完整性失败。")
    lines.extend(["", "## 失败样本", ""])
    failed = report.get("failed_samples") or []
    if failed:
        lines.extend(["| case_id | file_format | failure_codes |", "|---|---|---|"])
        for item in failed:
            codes = ", ".join(str(code) for code in item.get("failure_codes") or [])
            lines.append(f"| `{item.get('case_id', '')}` | `{item.get('file_format', '')}` | `{codes}` |")
    else:
        lines.append("- 无。")
    lines.extend([
        "",
        "## 脱敏边界",
        "",
        "本报告仅包含聚合指标、样本 ID、格式和失败码；不包含文档正文、候选原文、真实姓名、手机号、映射表或密钥。",
        "",
    ])
    return "\n".join(lines)


def write_markdown_report(report: dict, path: str | Path) -> Path:
    target = Path(path)
    _atomic_write(target, render_markdown(report).encode("utf-8"))
    return target


def _load_baseline(path: str | Path | None) -> dict | None:
    if path is None:
        return None
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalGateError("baseline_unreadable") from exc
    if not isinstance(data, dict) or int(data.get("schema_version") or 0) != BASELINE_SCHEMA_VERSION:
        raise EvalGateError("baseline_invalid")
    metrics = data.get("metrics")
    overall = metrics.get("overall") if isinstance(metrics, dict) else None
    if (not isinstance(overall, dict)
            or not {"recall", "f1"}.issubset(overall)
            or "suppress_accuracy" not in metrics):
        raise EvalGateError("baseline_invalid")
    return data


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _baseline_comparison(report: dict, baseline: dict, suite_status: dict) -> tuple[dict, list[str], list[dict]]:
    reasons: list[str] = []
    if str(baseline.get("suite_id") or "") != str(suite_status.get("suite_id") or ""):
        reasons.append("baseline_suite_id_mismatch")
    if str(baseline.get("suite_version") or "") != str(suite_status.get("suite_version") or ""):
        reasons.append("baseline_suite_version_mismatch")
    if str(baseline.get("suite_hash") or "").lower() != str(suite_status.get("computed_suite_hash") or "").lower():
        reasons.append("baseline_suite_hash_mismatch")

    base_metrics = baseline.get("metrics") or {}
    base_overall = base_metrics.get("overall") or {}
    current_overall = report.get("overall") or {}
    delta = {
        "precision": round(_number(current_overall.get("precision")) - _number(base_overall.get("precision")), 4),
        "recall": round(_number(current_overall.get("recall")) - _number(base_overall.get("recall")), 4),
        "f1": round(_number(current_overall.get("f1")) - _number(base_overall.get("f1")), 4),
        "suppress_accuracy": round(
            _number(report.get("suppress_accuracy")) - _number(base_metrics.get("suppress_accuracy")), 4),
    }
    tolerances = dict(DEFAULT_TOLERANCES)
    tolerances.update(baseline.get("tolerances") or {})
    if delta["recall"] < -_number(tolerances["recall_abs"]):
        reasons.append("recall_regression")
    if delta["f1"] < -_number(tolerances["f1_abs"]):
        reasons.append("f1_regression")
    if delta["suppress_accuracy"] < -_number(tolerances["suppress_accuracy_abs"]):
        reasons.append("suppress_accuracy_regression")

    p0_regressions: list[dict] = []
    current_categories = report.get("per_category") or {}
    base_categories = base_metrics.get("per_category") or {}
    for category in baseline.get("p0_categories") or []:
        current_f1 = _number((current_categories.get(category) or {}).get("f1"))
        baseline_f1 = _number((base_categories.get(category) or {}).get("f1"))
        category_delta = round(current_f1 - baseline_f1, 4)
        if category_delta < -_number(tolerances["p0_category_f1_abs"]):
            reasons.append("p0_category_regression")
            p0_regressions.append({
                "category": category,
                "baseline_f1": baseline_f1,
                "current_f1": current_f1,
                "delta": category_delta,
            })
    delta["p0_regressions"] = p0_regressions
    return delta, sorted(set(reasons)), p0_regressions


def run_eval_gate(*, suite: str | Path,
                  baseline_path: str | Path | None = None,
                  suites_dir: str | Path = DEFAULT_SUITES_DIR,
                  dry_run: bool = False,
                  runner: EvaluationRunner | None = None) -> dict:
    """执行完整性检查及可选质量门禁；任何失败只返回失败报告，不伪报通过。"""
    try:
        suite_status = inspect_suite(suite, suites_dir)
    except SuiteError as exc:
        return {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": _utc_now(),
            "dry_run": bool(dry_run),
            "execution_status": "failed",
            "quality_status": "not_evaluated",
            "gate_status": "failed",
            "suite_id": str(suite),
            "suite_version": "",
            "suite_hash": "",
            "sample_count": 0,
            "synthetic": False,
            "overall": {"tp": 0, "fp": 0, "fn": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0},
            "per_category": {},
            "per_file_format": {},
            "per_recognition_source": {},
            "suppress_accuracy": 0.0,
            "review_rate": 0.0,
            "missing_sources": [],
            "failed_samples": [],
            "safety_failures": [],
            "failure_reasons": [str(exc)],
            "baseline_delta": {},
            "regression_flag": True,
        }

    base_report = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "dry_run": bool(dry_run),
        "execution_status": "completed",
        "quality_status": "not_evaluated",
        "gate_status": "failed",
        "suite_id": suite_status.get("suite_id", ""),
        "suite_version": suite_status.get("suite_version", ""),
        "suite_hash": suite_status.get("computed_suite_hash", ""),
        "tool_version": "",
        "policy_version": "",
        "config_hash": "",
        "synthetic": bool(suite_status.get("synthetic")),
        "provenance": suite_status.get("provenance", ""),
        "sample_count": int(suite_status.get("sample_count") or 0),
        "cases": int(suite_status.get("sample_count") or 0),
        "overall": {"tp": 0, "fp": 0, "fn": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0},
        "per_type": {},
        "per_category": {},
        "per_file_format": {},
        "per_recognition_source": {},
        "suppress": {"correctly_suppressed": 0, "should_suppress_but_active": 0, "suppress_accuracy": 0.0},
        "suppress_accuracy": 0.0,
        "review_rate": 0.0,
        "decision_counts": {"auto_confirm": 0, "needs_review": 0, "suppress": 0},
        "missing_sources": list(suite_status.get("missing_sources") or []),
        "hash_mismatches": list(suite_status.get("hash_mismatches") or []),
        "integrity_errors": list(suite_status.get("integrity_errors") or []),
        "failed_samples": [],
        "safety_failures": [],
        "failure_reasons": [],
        "baseline_delta": {},
        "regression_flag": False,
    }
    if not suite_status.get("ready"):
        base_report["execution_status"] = "failed"
        base_report["failure_reasons"] = sorted(set(base_report["integrity_errors"]))
        base_report["regression_flag"] = True
        return base_report
    if dry_run:
        base_report["gate_status"] = "passed"
        return base_report

    try:
        baseline = _load_baseline(baseline_path)
    except EvalGateError as exc:
        base_report["execution_status"] = "failed"
        base_report["failure_reasons"] = [str(exc)]
        base_report["regression_flag"] = True
        return base_report
    if baseline is None:
        base_report["failure_reasons"] = ["baseline_required"]
        base_report["regression_flag"] = True
        return base_report
    expected_baseline_hash = str((suite_status.get("baseline") or {}).get("sha256") or "").lower()
    if expected_baseline_hash:
        try:
            actual_baseline_hash = _sha256_bytes(Path(baseline_path).read_bytes())
        except OSError:
            actual_baseline_hash = ""
        if actual_baseline_hash != expected_baseline_hash:
            base_report["execution_status"] = "failed"
            base_report["failure_reasons"] = ["baseline_hash_mismatch"]
            base_report["regression_flag"] = True
            return base_report

    try:
        dataset = load_suite_dataset(suite, suites_dir)
        active_runner = runner or EvaluationRunner()
        result = active_runner.run(dataset)
        if not isinstance(result, dict):
            raise TypeError("runner_result_invalid")
        if result.get("execution_status", "completed") != "completed":
            raise RuntimeError("runner_reported_failure")
    except Exception:
        base_report["execution_status"] = "failed"
        base_report["failure_reasons"] = ["evaluation_execution_failed"]
        base_report["regression_flag"] = True
        return base_report

    for key in (
        "overall", "per_type", "per_category", "per_file_format",
        "per_recognition_source", "suppress", "suppress_accuracy",
        "review_rate", "decision_counts", "failed_samples", "safety_failures",
        "tool_version", "policy_version", "config_hash",
    ):
        if key in result:
            base_report[key] = result[key]
    base_report["suite_id"] = suite_status["suite_id"]
    base_report["suite_version"] = suite_status["suite_version"]
    base_report["suite_hash"] = suite_status["computed_suite_hash"]
    base_report["synthetic"] = bool(suite_status.get("synthetic"))
    base_report["sample_count"] = int(suite_status.get("sample_count") or 0)
    base_report["cases"] = base_report["sample_count"]
    base_report["execution_status"] = "completed"
    base_report["missing_sources"] = list(suite_status.get("missing_sources") or [])
    base_report["hash_mismatches"] = list(suite_status.get("hash_mismatches") or [])
    base_report["integrity_errors"] = list(suite_status.get("integrity_errors") or [])
    base_report["failed_samples"] = [
        {
            "case_id": str(item.get("case_id") or ""),
            "file_format": str(item.get("file_format") or ""),
            "failure_codes": [str(code) for code in (item.get("failure_codes") or [])],
        }
        for item in (base_report.get("failed_samples") or [])
        if isinstance(item, dict)
    ]
    base_report["safety_failures"] = [
        {
            "case_id": str(item.get("case_id") or ""),
            "file_format": str(item.get("file_format") or ""),
            "failure_codes": [str(code) for code in (item.get("failure_codes") or [])],
        }
        for item in (base_report.get("safety_failures") or [])
        if isinstance(item, dict)
    ]

    compatibility_reasons = []
    compatible_policies = suite_status.get("compatible_policy_versions") or []
    compatible_tools = suite_status.get("compatible_tool_versions") or []
    if compatible_policies and base_report.get("policy_version") not in compatible_policies:
        compatibility_reasons.append("policy_version_incompatible")
    if compatible_tools and base_report.get("tool_version") not in compatible_tools:
        compatibility_reasons.append("tool_version_incompatible")

    baseline_delta, baseline_reasons, _ = _baseline_comparison(
        base_report, baseline, suite_status)
    base_report["baseline_delta"] = baseline_delta
    reasons = set(baseline_reasons)
    reasons.update(compatibility_reasons)
    if base_report.get("safety_failures"):
        reasons.add("safety_critical_failure")
    if base_report["missing_sources"]:
        reasons.add("missing_sources")
    if base_report["integrity_errors"]:
        reasons.update(base_report["integrity_errors"])
    base_report["failure_reasons"] = sorted(reasons)
    base_report["regression_flag"] = bool(reasons)
    base_report["quality_status"] = "failed" if reasons else "passed"
    base_report["gate_status"] = "failed" if reasons else "passed"
    return base_report


def update_baseline_from_report(*, report: dict, baseline_path: str | Path,
                                reason: str,
                                suite_manifest_path: str | Path | None = None) -> dict:
    """显式更新基线，保存旧基线副本和更新审计记录。"""
    reason = str(reason or "").strip()
    if not reason:
        raise EvalGateError("baseline_update_reason_required")
    if report.get("execution_status") != "completed":
        raise EvalGateError("baseline_update_requires_completed_execution")
    if report.get("integrity_errors") or report.get("missing_sources"):
        raise EvalGateError("baseline_update_integrity_failed")
    if report.get("safety_failures"):
        raise EvalGateError("baseline_update_safety_failure")

    target = Path(baseline_path)
    old_data = target.read_bytes() if target.is_file() else b""
    old_hash = _sha256_bytes(old_data) if old_data else ""
    old_baseline: dict = {}
    if old_data:
        try:
            old_baseline = json.loads(old_data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvalGateError("baseline_corrupt") from exc
    now = _utc_now()
    history_dir = target.parent / "history"
    audit_id = now.replace(":", "").replace("-", "").replace("+", "_")
    audit_record = {
        "schema_version": 1,
        "event": "baseline_updated",
        "updated_at": now,
        "reason": reason,
        "suite_id": report.get("suite_id", ""),
        "suite_version": report.get("suite_version", ""),
        "suite_hash": report.get("suite_hash", ""),
        "previous_baseline_sha256": old_hash,
    }
    if old_data:
        backup = history_dir / f"{target.stem}.{audit_id}.{old_hash[:12]}.json"
        _atomic_write(backup, old_data)
    audit_path = history_dir / f"{target.stem}.{audit_id}.audit.json"
    tolerances = dict(DEFAULT_TOLERANCES)
    tolerances.update(old_baseline.get("tolerances") or {})
    new_baseline = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "suite_id": report.get("suite_id", ""),
        "suite_version": report.get("suite_version", ""),
        "suite_hash": report.get("suite_hash", ""),
        "metrics": {
            "overall": report.get("overall") or {},
            "suppress_accuracy": report.get("suppress_accuracy", 0.0),
            "per_category": report.get("per_category") or {},
        },
        "p0_categories": list(old_baseline.get("p0_categories") or []),
        "tolerances": tolerances,
        "update_reason": reason,
        "updated_at": now,
        "previous_baseline_sha256": old_hash,
    }
    new_data = _json_bytes(new_baseline)
    audit_record["new_baseline_sha256"] = _sha256_bytes(new_data)

    manifest_target = Path(suite_manifest_path) if suite_manifest_path else None
    old_manifest_data = b""
    new_manifest_data = b""
    if manifest_target is not None:
        try:
            old_manifest_data = manifest_target.read_bytes()
            manifest = json.loads(old_manifest_data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvalGateError("suite_manifest_update_failed") from exc
        if str(manifest.get("suite_id") or "") != str(report.get("suite_id") or ""):
            raise EvalGateError("suite_manifest_update_failed")
        manifest.setdefault("baseline", {})["sha256"] = audit_record["new_baseline_sha256"]
        new_manifest_data = _json_bytes(manifest)
        audit_record["suite_manifest_sha256"] = _sha256_bytes(new_manifest_data)

    _atomic_write(audit_path, _json_bytes(audit_record))
    try:
        _atomic_write(target, new_data)
        if manifest_target is not None:
            _atomic_write(manifest_target, new_manifest_data)
    except EvalGateError:
        # 新基线或清单写入失败时，尽量恢复旧文件；失败细节仍向调用者抛出。
        try:
            if old_data:
                _atomic_write(target, old_data)
            elif target.exists():
                target.unlink()
            if manifest_target is not None and old_manifest_data:
                _atomic_write(manifest_target, old_manifest_data)
        except (OSError, EvalGateError) as restore_exc:
            raise EvalGateError("baseline_update_rollback_failed") from restore_exc
        raise
    return {
        "baseline_path": str(target),
        "audit_path": str(audit_path),
        "previous_baseline_sha256": old_hash,
        "new_baseline_sha256": audit_record["new_baseline_sha256"],
        "backup_preserved": bool(old_data),
        "suite_manifest_updated": bool(manifest_target),
    }


__all__ = [
    "BASELINE_SCHEMA_VERSION", "DEFAULT_TOLERANCES", "EvalGateError",
    "REPORT_SCHEMA_VERSION", "render_markdown", "run_eval_gate",
    "update_baseline_from_report", "write_json_report", "write_markdown_report",
]
