"""版本化评测套件：清单加载、样本完整性校验和确定性哈希。"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

from .dataset import Dataset

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SUITES_DIR = PROJECT_ROOT / "evaluation" / "suites"
SUITE_SCHEMA_VERSION = 1


class SuiteError(ValueError):
    """评测套件格式、路径或完整性错误。"""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def manifest_path_for(suite_ref: str | Path,
                      suites_dir: str | Path = DEFAULT_SUITES_DIR) -> Path:
    ref = Path(suite_ref)
    if ref.is_file():
        return ref.resolve()
    if ref.is_dir() and (ref / "suite.json").is_file():
        return (ref / "suite.json").resolve()
    candidate = (Path(suites_dir) / str(suite_ref) / "suite.json").resolve()
    if candidate.is_file():
        return candidate
    raise SuiteError(f"unknown_suite:{suite_ref}")


def _safe_sample_path(suite_dir: Path, relative: str) -> Path:
    rel = PurePosixPath(relative.replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise SuiteError("sample_path_unsafe")
    if rel.drive:
        raise SuiteError("sample_path_unsafe")
    base = suite_dir.resolve()
    target = base.joinpath(*rel.parts).resolve()
    if target != base and base not in target.parents:
        raise SuiteError("sample_path_unsafe")
    return target


def stable_suite_payload(manifest: dict) -> dict:
    """返回参与 suite_hash 的稳定字段，排除运行期状态和哈希自身。"""
    samples = []
    for item in manifest.get("samples") or []:
        samples.append({
            "path": str(item.get("path") or "").replace("\\", "/"),
            "sha256": str(item.get("sha256") or "").lower(),
            "kind": str(item.get("kind") or "labeled_cases"),
            "sample_count": int(item.get("sample_count") or 0),
        })
    samples.sort(key=lambda item: item["path"])
    return {
        "suite_id": str(manifest.get("suite_id") or ""),
        "suite_version": str(manifest.get("suite_version") or ""),
        "samples": samples,
        "sample_count": int(manifest.get("sample_count") or 0),
        "category_counts": dict(sorted((manifest.get("category_counts") or {}).items())),
        "file_format_counts": dict(sorted((manifest.get("file_format_counts") or {}).items())),
        "synthetic": bool(manifest.get("synthetic", False)),
        "provenance": str(manifest.get("provenance") or ""),
        "created_at": str(manifest.get("created_at") or ""),
        "compatible_policy_versions": sorted(
            str(v) for v in (manifest.get("compatible_policy_versions") or [])),
        "compatible_tool_versions": sorted(
            str(v) for v in (manifest.get("compatible_tool_versions") or [])),
    }


def compute_suite_hash(manifest: dict) -> str:
    return hashlib.sha256(_canonical_json(stable_suite_payload(manifest))).hexdigest()


def _load_manifest(manifest_path: Path) -> dict:
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SuiteError("suite_manifest_unreadable") from exc
    if not isinstance(data, dict):
        raise SuiteError("suite_manifest_invalid")
    if int(data.get("schema_version") or 0) != SUITE_SCHEMA_VERSION:
        raise SuiteError("suite_schema_unsupported")
    for key in ("suite_id", "suite_version", "samples", "sample_count",
                "category_counts", "file_format_counts", "synthetic",
                "provenance", "created_at", "suite_hash"):
        if key not in data:
            raise SuiteError(f"suite_field_missing:{key}")
    if not data["samples"]:
        raise SuiteError("suite_samples_empty")
    return data


def _load_sample_dataset(path: Path) -> Dataset:
    try:
        return Dataset.load(path)
    except Exception as exc:
        raise SuiteError("suite_sample_unreadable") from exc


def inspect_suite(suite_ref: str | Path,
                  suites_dir: str | Path = DEFAULT_SUITES_DIR) -> dict:
    """校验套件并返回可序列化状态；不调用识别管线。"""
    mpath = manifest_path_for(suite_ref, suites_dir)
    manifest = _load_manifest(mpath)
    suite_dir = mpath.parent
    result = deepcopy(manifest)
    result["manifest_path"] = str(mpath.relative_to(PROJECT_ROOT)) if mpath.is_relative_to(PROJECT_ROOT) else str(mpath.name)
    result["missing_sources"] = []
    result["hash_mismatches"] = []
    result["integrity_errors"] = []
    result["sample_checks"] = []

    samples_ok = True
    resolved_samples: list[tuple[dict, Path]] = []
    for item in manifest["samples"]:
        relative = str(item.get("path") or "")
        try:
            sample_path = _safe_sample_path(suite_dir, relative)
        except SuiteError:
            result["integrity_errors"].append("sample_path_unsafe")
            result["sample_checks"].append({"path": relative, "status": "unsafe"})
            samples_ok = False
            continue
        resolved_samples.append((item, sample_path))
        check = {"path": relative, "expected_sha256": str(item.get("sha256") or "").lower()}
        if not sample_path.is_file():
            check["status"] = "missing"
            result["missing_sources"].append(relative)
            samples_ok = False
        else:
            actual = _sha256_file(sample_path)
            check["actual_sha256"] = actual
            if actual != check["expected_sha256"]:
                check["status"] = "hash_mismatch"
                result["hash_mismatches"].append({
                    "path": relative,
                    "expected_sha256": check["expected_sha256"],
                    "actual_sha256": actual,
                })
                samples_ok = False
            else:
                check["status"] = "ok"
        result["sample_checks"].append(check)

    actual_suite_hash = compute_suite_hash(manifest)
    result["computed_suite_hash"] = actual_suite_hash
    if str(manifest.get("suite_hash") or "").lower() != actual_suite_hash:
        result["integrity_errors"].append("suite_hash_mismatch")
        samples_ok = False

    if result["missing_sources"]:
        result["integrity_errors"].append("missing_sources")
    if result["hash_mismatches"]:
        result["integrity_errors"].append("sample_hash_mismatch")

    if samples_ok:
        cases = []
        for item, sample_path in resolved_samples:
            if str(item.get("kind") or "labeled_cases") != "labeled_cases":
                result["integrity_errors"].append("unsupported_sample_kind")
                samples_ok = False
                continue
            try:
                dataset = _load_sample_dataset(sample_path)
            except SuiteError:
                result["integrity_errors"].append("suite_sample_unreadable")
                samples_ok = False
                continue
            declared = int(item.get("sample_count") or 0)
            if declared != len(dataset):
                result["integrity_errors"].append("sample_count_mismatch")
                samples_ok = False
            cases.extend(dataset.cases)

        category_counts: dict[str, int] = {}
        format_counts: dict[str, int] = {}
        for case in cases:
            fmt = case.file_type or "unknown"
            format_counts[fmt] = format_counts.get(fmt, 0) + 1
            for category, _ in case.true_entities:
                category_counts[category] = category_counts.get(category, 0) + 1
        if len(cases) != int(manifest.get("sample_count") or 0):
            result["integrity_errors"].append("sample_count_mismatch")
            samples_ok = False
        if dict(sorted(category_counts.items())) != dict(sorted((manifest.get("category_counts") or {}).items())):
            result["integrity_errors"].append("category_counts_mismatch")
            samples_ok = False
        if dict(sorted(format_counts.items())) != dict(sorted((manifest.get("file_format_counts") or {}).items())):
            result["integrity_errors"].append("file_format_counts_mismatch")
            samples_ok = False
        result["actual_sample_count"] = len(cases)
        result["actual_category_counts"] = dict(sorted(category_counts.items()))
        result["actual_file_format_counts"] = dict(sorted(format_counts.items()))
    else:
        result["actual_sample_count"] = 0

    result["integrity_errors"] = sorted(set(result["integrity_errors"]))
    result["ready"] = not result["integrity_errors"]
    return result


def load_suite_dataset(suite_ref: str | Path,
                       suites_dir: str | Path = DEFAULT_SUITES_DIR) -> Dataset:
    status = inspect_suite(suite_ref, suites_dir)
    if not status["ready"]:
        raise SuiteError("suite_not_ready")
    mpath = manifest_path_for(suite_ref, suites_dir)
    cases = []
    for item in status["samples"]:
        path = _safe_sample_path(mpath.parent, str(item["path"]))
        cases.extend(_load_sample_dataset(path).cases)
    return Dataset(
        cases,
        synthetic=bool(status["synthetic"]),
        source_note=str(status["provenance"]),
        suite_id=str(status["suite_id"]),
        suite_version=str(status["suite_version"]),
    )


def list_suites(suites_dir: str | Path = DEFAULT_SUITES_DIR) -> list[dict]:
    root = Path(suites_dir)
    rows = []
    if not root.is_dir():
        return rows
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not (child / "suite.json").is_file():
            continue
        try:
            info = inspect_suite(child, root)
            rows.append({
                "suite_id": info.get("suite_id"),
                "suite_version": info.get("suite_version"),
                "suite_hash": info.get("computed_suite_hash"),
                "ready": info.get("ready"),
                "missing_sources": info.get("missing_sources"),
                "integrity_errors": info.get("integrity_errors"),
            })
        except SuiteError as exc:
            rows.append({"suite_id": child.name, "ready": False,
                         "integrity_errors": [str(exc)]})
    return rows


__all__ = [
    "DEFAULT_SUITES_DIR", "SUITE_SCHEMA_VERSION", "SuiteError",
    "compute_suite_hash", "inspect_suite", "list_suites",
    "load_suite_dataset", "manifest_path_for", "stable_suite_payload",
]
