# -*- coding: utf-8 -*-
"""External checker for the frozen LocalDocMask release self-check."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def validate_self_check_result(result: dict, *, expected_exe_sha256: str,
                               expected_git_commit: str,
                               output_root: Path) -> dict:
    reasons: list[str] = []
    root = Path(output_root).resolve()

    if not isinstance(result, dict):
        return {"passed": False, "reason_codes": ["SELF_CHECK_INVALID"]}
    if result.get("schema_version") != 1:
        reasons.append("SELF_CHECK_INVALID")
    if result.get("status") != "passed":
        reasons.append("SELF_CHECK_NOT_PASSED")
    if result.get("frozen") is not True:
        reasons.append("SELF_CHECK_NOT_FROZEN")
    if result.get("executable_sha256") != expected_exe_sha256:
        reasons.append("EXE_SHA_MISMATCH")
    if result.get("build_info_executable_sha256") != expected_exe_sha256:
        reasons.append("BUILD_INFO_EXE_MISMATCH")
    if result.get("git_commit") != expected_git_commit:
        reasons.append("GIT_COMMIT_MISMATCH")
    if result.get("event_chain_valid") is not True:
        reasons.append("EVENT_CHAIN_INVALID")
    if result.get("bundle_runtime_data_absent") is not True:
        reasons.append("BUNDLE_RUNTIME_DATA_PRESENT")

    cases = result.get("cases")
    if not isinstance(cases, dict):
        cases = {}
        reasons.append("SELF_CHECK_CASE_MISSING")

    for name in ("docx", "xlsx", "png"):
        case = cases.get(name)
        if not isinstance(case, dict):
            reasons.append("SELF_CHECK_CASE_MISSING")
            continue
        if case.get("status") != "passed":
            reasons.append(f"SELF_CHECK_CASE_FAILED_{name.upper()}")
        if case.get("event_chain_valid") is not True:
            reasons.append("EVENT_CHAIN_INVALID")
        if case.get("original_unchanged") is not True:
            reasons.append("ORIGINAL_HASH_CHANGED")
        before = case.get("original_sha256_before")
        after = case.get("original_sha256_after")
        if not isinstance(before, str) or before != after:
            reasons.append("ORIGINAL_HASH_CHANGED")
        original_path = Path(str(case.get("original_path") or ""))
        if not original_path.is_file() or not _is_within(original_path, root):
            reasons.append("ORIGINAL_PATH_INVALID")
        elif after and sha256_file(original_path) != after:
            reasons.append("ORIGINAL_HASH_CHANGED")
        output_path = Path(str(case.get("output_path") or ""))
        if not output_path.is_file() or not _is_within(output_path, root):
            reasons.append("OUTPUT_PATH_INVALID")
        else:
            recorded = case.get("output_sha256")
            if not isinstance(recorded, str) or sha256_file(output_path) != recorded:
                reasons.append("OUTPUT_HASH_MISMATCH")
        if name == "png" and case.get("ocr_used") is not True:
            reasons.append("PNG_OCR_NOT_USED")

    failure = cases.get("failure")
    if not isinstance(failure, dict):
        reasons.append("SELF_CHECK_CASE_MISSING")
    else:
        if failure.get("status") != "failed_closed":
            reasons.append("FAILURE_CASE_NOT_CLOSED")
        if failure.get("success_artifact_count") != 0:
            reasons.append("FAILURE_CASE_ENTERED_SUCCESS")
        quarantine_count = failure.get("quarantine_artifact_count")
        if not isinstance(quarantine_count, int) or quarantine_count <= 0:
            reasons.append("FAILURE_CASE_NOT_QUARANTINED")
        if failure.get("event_chain_valid") is not True:
            reasons.append("EVENT_CHAIN_INVALID")
        quarantine = Path(str(failure.get("quarantine_path") or ""))
        if not quarantine.is_file() or not _is_within(quarantine, root):
            reasons.append("QUARANTINE_PATH_INVALID")
        else:
            recorded = failure.get("output_hash")
            if not isinstance(recorded, str) or sha256_file(quarantine) != recorded:
                reasons.append("QUARANTINE_HASH_MISMATCH")

    reasons = list(dict.fromkeys(reasons))
    return {"passed": not reasons, "reason_codes": reasons}


def _write_evidence(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def _tail(value: str, limit: int = 4000) -> str:
    return (value or "")[-limit:]


def _run_self_check(exe: Path, output_root: Path,
                    timeout_seconds: float) -> tuple[int, str, str]:
    env = os.environ.copy()
    env.update({
        "QT_QPA_PLATFORM": "offscreen",
        "OMP_NUM_THREADS": "1",
        "PYTHONFAULTHANDLER": "1",
    })
    process = subprocess.Popen(
        [str(exe), "--release-self-check", str(output_root)],
        cwd=exe.parent, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return process.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, check=False,
            )
        stdout, stderr = process.communicate()
        stderr += f"\nSELF_CHECK_TIMEOUT={timeout_seconds}"
        return 124, stdout, stderr


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-dir", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    args = parser.parse_args()

    bundle = Path(args.bundle_dir).resolve()
    evidence_path = Path(args.evidence).resolve()
    exe = bundle / "LocalDocMask.exe"
    build_info_path = bundle / "build-info.json"
    base_evidence = {
        "schema_version": 1,
        "status": "failed",
        "bundle_dir": str(bundle),
        "executable_sha256": "",
        "git_commit": "",
        "self_check": "",
        "stdout_tail": "",
        "stderr_tail": "",
        "cases": {
            "docx": False,
            "xlsx": False,
            "png_ocr": False,
            "failure_quarantine": False,
        },
        "notes": [],
    }

    if not bundle.is_dir():
        base_evidence["notes"].append("BUNDLE_MISSING")
        _write_evidence(evidence_path, base_evidence)
        return 1
    if not exe.is_file():
        base_evidence["notes"].append("EXE_MISSING")
        _write_evidence(evidence_path, base_evidence)
        return 1
    if not build_info_path.is_file():
        base_evidence["notes"].append("BUILD_INFO_MISSING")
        _write_evidence(evidence_path, base_evidence)
        return 1
    try:
        build_info = json.loads(build_info_path.read_text(encoding="utf-8-sig"))
    except Exception:
        base_evidence["notes"].append("BUILD_INFO_INVALID")
        _write_evidence(evidence_path, base_evidence)
        return 1
    if not isinstance(build_info, dict):
        base_evidence["notes"].append("BUILD_INFO_INVALID")
        _write_evidence(evidence_path, base_evidence)
        return 1

    executable_hash_before = sha256_file(exe)
    build_commit = str(build_info.get("git_commit") or "")
    build_exe_hash = str(build_info.get("executable_sha256") or "")
    base_evidence["executable_sha256"] = executable_hash_before
    base_evidence["git_commit"] = build_commit
    if build_exe_hash != executable_hash_before:
        base_evidence["notes"].append("BUILD_INFO_EXE_MISMATCH")
        _write_evidence(evidence_path, base_evidence)
        return 1

    build_root = ROOT / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    output_root = Path(tempfile.mkdtemp(
        prefix=f"package-e2e-{uuid.uuid4().hex[:8]}-", dir=build_root)).resolve()
    self_check_path = output_root / "release-self-check.json"
    exit_code, stdout, stderr = _run_self_check(exe, output_root,
                                                args.timeout_seconds)
    base_evidence.update({
        "self_check": str(self_check_path),
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
    })

    if not self_check_path.is_file():
        base_evidence["notes"].append("SELF_CHECK_RESULT_MISSING")
        _write_evidence(evidence_path, base_evidence)
        return 1
    try:
        result = json.loads(self_check_path.read_text(encoding="utf-8-sig"))
    except Exception:
        base_evidence["notes"].append("SELF_CHECK_RESULT_INVALID")
        _write_evidence(evidence_path, base_evidence)
        return 1

    validation = validate_self_check_result(
        result, expected_exe_sha256=executable_hash_before,
        expected_git_commit=build_commit, output_root=output_root,
    )
    executable_hash_after = sha256_file(exe)
    if executable_hash_after != executable_hash_before:
        validation["passed"] = False
        validation["reason_codes"] = list(dict.fromkeys(
            [*validation["reason_codes"], "EXE_SHA_CHANGED_DURING_CHECK"]))
    if (bundle / "data").exists():
        validation["passed"] = False
        validation["reason_codes"] = list(dict.fromkeys(
            [*validation["reason_codes"], "BUNDLE_RUNTIME_DATA_PRESENT"]))
    if exit_code != 0:
        validation["passed"] = False
        validation["reason_codes"] = list(dict.fromkeys(
            [*validation["reason_codes"], "SELF_CHECK_EXIT_NONZERO"]))

    base_evidence["status"] = "passed" if validation["passed"] else "failed"
    base_evidence["reason_codes"] = validation["reason_codes"]
    base_evidence["executable_sha256_after"] = executable_hash_after
    cases = result.get("cases", {}) if isinstance(result, dict) else {}
    base_evidence["cases"] = {
        "docx": bool(isinstance(cases.get("docx"), dict)
                     and cases["docx"].get("status") == "passed"),
        "xlsx": bool(isinstance(cases.get("xlsx"), dict)
                     and cases["xlsx"].get("status") == "passed"),
        "png_ocr": bool(isinstance(cases.get("png"), dict)
                        and cases["png"].get("status") == "passed"
                        and cases["png"].get("ocr_used") is True),
        "failure_quarantine": bool(
            isinstance(cases.get("failure"), dict)
            and cases["failure"].get("status") == "failed_closed"
            and cases["failure"].get("success_artifact_count") == 0
            and int(cases["failure"].get("quarantine_artifact_count", 0)) > 0),
    }
    _write_evidence(evidence_path, base_evidence)
    return 0 if validation["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
