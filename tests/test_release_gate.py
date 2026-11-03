# -*- coding: utf-8 -*-
"""FIX-04：发布门禁可信化测试。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "build_metadata.py"


def _load_build_metadata():
    spec = importlib.util.spec_from_file_location(
        "localdocmask_build_metadata_fix04", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    assert _git(path, "init").returncode == 0
    assert _git(path, "config", "user.name", "Fix04 Tests").returncode == 0
    assert _git(path, "config", "user.email", "fix04@example.invalid").returncode == 0
    assert _git(path, "config", "core.autocrlf", "false").returncode == 0
    (path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    assert _git(path, "add", "tracked.txt").returncode == 0
    commit = _git(path, "commit", "-m", "initial")
    assert commit.returncode == 0, commit.stderr
    return path


def test_clean_worktree_passes_in_clean_temp_repo(tmp_path):
    module = _load_build_metadata()
    repo = _init_git_repo(tmp_path / "repo")
    result = module.check_clean_worktree(repo)
    assert result["passed"] is True
    assert result["reason_codes"] == []


def test_clean_worktree_rejects_tracked_modification(tmp_path):
    module = _load_build_metadata()
    repo = _init_git_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
    result = module.check_clean_worktree(repo)
    assert result["passed"] is False
    assert "GIT_WORKTREE_DIRTY" in result["reason_codes"]


def test_clean_worktree_rejects_untracked_file(tmp_path):
    module = _load_build_metadata()
    repo = _init_git_repo(tmp_path / "repo")
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    result = module.check_clean_worktree(repo)
    assert result["passed"] is False
    assert "GIT_WORKTREE_DIRTY" in result["reason_codes"]


def _release_fixture(tmp_path: Path) -> dict:
    module = _load_build_metadata()
    project = tmp_path / "project"
    bundle = project / "dist" / "LocalDocMask"
    evidence_dir = project / "dist" / "t9-evidence"
    external_dir = project / "docs" / "operations" / "t9-evidence"
    bundle.mkdir(parents=True)
    evidence_dir.mkdir(parents=True)
    external_dir.mkdir(parents=True)

    exe = bundle / "LocalDocMask.exe"
    exe.write_bytes(b"synthetic-exe")
    exe_hash = hashlib.sha256(exe.read_bytes()).hexdigest()
    commit = "a" * 40
    evidence_paths = []
    for name in ("sandbox-verification.json", "acl-other-user-verification.json",
                 "old-binary-rollback.json"):
        path = external_dir / name
        path.write_text(json.dumps({"synthetic": True}), encoding="utf-8")
        evidence_paths.append(path.relative_to(project).as_posix())

    build_info = bundle / "build-info.json"
    build_info.write_text(json.dumps({
        "git_commit": commit,
        "git_dirty": False,
        "executable_sha256": exe_hash,
    }), encoding="utf-8")
    build_evidence = evidence_dir / "build.json"
    build_evidence.write_text(json.dumps({
        "status": "passed",
        "git_commit": commit,
        "git_dirty": False,
        "executable_sha256": exe_hash,
        "reproducibility": {
            "reproducibility": {
                "identical": True,
                "identity_identical": True,
                "executable_identical": True,
                "checksums_identical": True,
            }
        },
    }), encoding="utf-8")
    external = external_dir / "external-validation-status.json"
    external.write_text(json.dumps({
        "updated_at_utc": "2026-11-02T00:00:00Z",
        "release_candidate": commit,
        "executable_sha256": exe_hash,
        "clean_windows_vm_verified": True,
        "other_user_logon_verified": True,
        "old_binary_rollback_verified": True,
        "clean_windows_vm_evidence": evidence_paths[0],
        "other_user_logon_evidence": evidence_paths[1],
        "old_binary_rollback_evidence": evidence_paths[2],
    }), encoding="utf-8")
    return {
        "module": module,
        "project": project,
        "bundle": bundle,
        "build_info": build_info,
        "build_evidence": build_evidence,
        "external": external,
        "commit": commit,
        "exe_hash": exe_hash,
    }


def _validate(fixture: dict) -> dict:
    return fixture["module"].validate_release_gate(
        bundle_dir=fixture["bundle"],
        build_evidence_path=fixture["build_evidence"],
        external_status_path=fixture["external"],
        expected_commit=fixture["commit"],
        project_root=fixture["project"],
    )


def test_release_gate_matching_synthetic_fixture_passes(tmp_path):
    fixture = _release_fixture(tmp_path)
    result = _validate(fixture)
    assert result["passed"] is True
    assert result["reason_codes"] == []
    assert result["external_checks"] == {
        "clean_windows_vm_verified": True,
        "other_user_logon_verified": True,
        "old_binary_rollback_verified": True,
    }


def test_release_gate_missing_external_status_fails(tmp_path):
    fixture = _release_fixture(tmp_path)
    fixture["external"].unlink()
    result = _validate(fixture)
    assert result["passed"] is False
    assert "EXTERNAL_STATUS_MISSING" in result["reason_codes"]


def test_release_gate_invalid_json_fails(tmp_path):
    fixture = _release_fixture(tmp_path)
    fixture["external"].write_text("{not-json", encoding="utf-8")
    result = _validate(fixture)
    assert result["passed"] is False
    assert "EXTERNAL_STATUS_INVALID" in result["reason_codes"]


def test_release_gate_candidate_mismatch_fails(tmp_path):
    fixture = _release_fixture(tmp_path)
    payload = json.loads(fixture["external"].read_text(encoding="utf-8"))
    payload["release_candidate"] = "b" * 40
    fixture["external"].write_text(json.dumps(payload), encoding="utf-8")
    result = _validate(fixture)
    assert result["passed"] is False
    assert "EXTERNAL_STATUS_CANDIDATE_MISMATCH" in result["reason_codes"]


def test_release_gate_executable_mismatch_fails(tmp_path):
    fixture = _release_fixture(tmp_path)
    payload = json.loads(fixture["external"].read_text(encoding="utf-8"))
    payload["executable_sha256"] = "0" * 64
    fixture["external"].write_text(json.dumps(payload), encoding="utf-8")
    result = _validate(fixture)
    assert result["passed"] is False
    assert "EXTERNAL_STATUS_EXECUTABLE_MISMATCH" in result["reason_codes"]


def test_release_gate_non_boolean_check_fails(tmp_path):
    fixture = _release_fixture(tmp_path)
    payload = json.loads(fixture["external"].read_text(encoding="utf-8"))
    payload["clean_windows_vm_verified"] = "true"
    fixture["external"].write_text(json.dumps(payload), encoding="utf-8")
    result = _validate(fixture)
    assert result["passed"] is False
    assert "EXTERNAL_REQUIRED_CHECKS_NOT_TRUE" in result["reason_codes"]


def test_release_gate_missing_evidence_path_fails(tmp_path):
    fixture = _release_fixture(tmp_path)
    payload = json.loads(fixture["external"].read_text(encoding="utf-8"))
    payload["clean_windows_vm_evidence"] = ""
    fixture["external"].write_text(json.dumps(payload), encoding="utf-8")
    result = _validate(fixture)
    assert result["passed"] is False
    assert "EXTERNAL_EVIDENCE_PATH_MISSING" in result["reason_codes"]


def test_release_gate_build_dirty_fails(tmp_path):
    fixture = _release_fixture(tmp_path)
    payload = json.loads(fixture["build_info"].read_text(encoding="utf-8"))
    payload["git_dirty"] = True
    fixture["build_info"].write_text(json.dumps(payload), encoding="utf-8")
    result = _validate(fixture)
    assert result["passed"] is False
    assert "BUILD_INFO_GIT_DIRTY" in result["reason_codes"]


def test_release_gate_build_commit_mismatch_fails(tmp_path):
    fixture = _release_fixture(tmp_path)
    payload = json.loads(fixture["build_info"].read_text(encoding="utf-8"))
    payload["git_commit"] = "b" * 40
    fixture["build_info"].write_text(json.dumps(payload), encoding="utf-8")
    result = _validate(fixture)
    assert result["passed"] is False
    assert "BUILD_INFO_COMMIT_MISMATCH" in result["reason_codes"]


def test_release_gate_build_executable_hash_mismatch_fails(tmp_path):
    fixture = _release_fixture(tmp_path)
    payload = json.loads(fixture["build_evidence"].read_text(encoding="utf-8"))
    payload["executable_sha256"] = "0" * 64
    fixture["build_evidence"].write_text(json.dumps(payload), encoding="utf-8")
    result = _validate(fixture)
    assert result["passed"] is False
    assert "BUILD_EVIDENCE_EXECUTABLE_MISMATCH" in result["reason_codes"]


def test_release_gate_repeat_is_stable(tmp_path):
    fixture = _release_fixture(tmp_path)
    payload = json.loads(fixture["external"].read_text(encoding="utf-8"))
    payload["release_candidate"] = "b" * 40
    fixture["external"].write_text(json.dumps(payload), encoding="utf-8")
    first = _validate(fixture)
    second = _validate(fixture)
    assert first["passed"] == second["passed"] is False
    assert first["reason_codes"] == second["reason_codes"]


def test_release_gate_accepts_utf8_bom_build_evidence(tmp_path):
    fixture = _release_fixture(tmp_path)
    payload = fixture["build_evidence"].read_text(encoding="utf-8")
    fixture["build_evidence"].write_text(payload, encoding="utf-8-sig")
    result = _validate(fixture)
    assert result["passed"] is True


def test_release_gate_build_info_executable_missing_fails(tmp_path):
    fixture = _release_fixture(tmp_path)
    payload = json.loads(fixture["build_info"].read_text(encoding="utf-8"))
    payload.pop("executable_sha256")
    fixture["build_info"].write_text(json.dumps(payload), encoding="utf-8")
    result = _validate(fixture)
    assert result["passed"] is False
    assert "BUILD_INFO_EXECUTABLE_MISSING" in result["reason_codes"]


def test_release_gate_build_info_executable_invalid_fails(tmp_path):
    fixture = _release_fixture(tmp_path)
    payload = json.loads(fixture["build_info"].read_text(encoding="utf-8"))
    payload["executable_sha256"] = "not-a-64-char-hex-digest"
    fixture["build_info"].write_text(json.dumps(payload), encoding="utf-8")
    result = _validate(fixture)
    assert result["passed"] is False
    assert "BUILD_INFO_EXECUTABLE_INVALID" in result["reason_codes"]


def _set_repro_flags(fixture: dict, **flags) -> None:
    payload = json.loads(fixture["build_evidence"].read_text(encoding="utf-8"))
    repro = payload["reproducibility"]["reproducibility"]
    repro.update(flags)
    fixture["build_evidence"].write_text(json.dumps(payload), encoding="utf-8")


def test_release_gate_requires_all_reproducibility_child_flags(tmp_path):
    cases = (
        ("identity_identical", "BUILD_REPRO_IDENTITY_MISMATCH"),
        ("executable_identical", "BUILD_REPRO_EXECUTABLE_MISMATCH"),
        ("checksums_identical", "BUILD_REPRO_CHECKSUMS_MISMATCH"),
    )
    for field, reason in cases:
        fixture = _release_fixture(tmp_path / field)
        _set_repro_flags(fixture, **{field: False})
        result = _validate(fixture)
        assert result["passed"] is False
        assert reason in result["reason_codes"]


def test_release_gate_requires_reproducibility_child_fields_present(tmp_path):
    for field in ("identity_identical", "executable_identical",
                  "checksums_identical"):
        fixture = _release_fixture(tmp_path / field)
        payload = json.loads(fixture["build_evidence"].read_text(encoding="utf-8"))
        del payload["reproducibility"]["reproducibility"][field]
        fixture["build_evidence"].write_text(json.dumps(payload), encoding="utf-8")
        result = _validate(fixture)
        assert result["passed"] is False
        assert "BUILD_REPRO_FIELDS_MISSING" in result["reason_codes"]


def test_release_gate_matching_reproducibility_still_passes(tmp_path):
    fixture = _release_fixture(tmp_path)
    _set_repro_flags(
        fixture,
        identical=True,
        identity_identical=True,
        executable_identical=True,
        checksums_identical=True,
    )
    result = _validate(fixture)
    assert result["passed"] is True
