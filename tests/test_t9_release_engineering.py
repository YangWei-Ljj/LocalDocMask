# -*- coding: utf-8 -*-
"""T9 发布工程：构建元数据、健康检查、数据库维护与 ACL 入口测试。"""
from __future__ import annotations

import hashlib
import importlib.metadata as importlib_metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def _run(args: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def test_build_metadata_is_deterministic_and_checksums_are_valid(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    for bundle in (first, second):
        (bundle / "resources").mkdir(parents=True)
        (bundle / "config").mkdir()
        (bundle / "resources" / "blacklist.txt").write_text("合成\n", encoding="utf-8")
        (bundle / "config" / "config.example.yaml").write_text("schema_version: 1\n", encoding="utf-8")
        (bundle / "LocalDocMask.exe").write_bytes(b"synthetic-executable")
    script = ROOT / "scripts" / "build_metadata.py"
    for bundle in (first, second):
        result = _run([PY, str(script), "--bundle-dir", str(bundle),
                       "--build-time", "2026-10-30T00:00:00Z",
                       "--commit", "test-commit"])
        assert result.returncode == 0, result.stderr
        assert (bundle / "build-info.json").is_file()
        assert (bundle / "bundle-identity.json").is_file()
        assert (bundle / "SBOM.json").is_file()
    compare = tmp_path / "compare.json"
    result = _run([PY, str(script), "--bundle-dir", str(first),
                   "--compare", str(second), "--json-output", str(compare)])
    assert result.returncode == 0, result.stderr
    data = json.loads(compare.read_text(encoding="utf-8"))
    repro = data["reproducibility"]
    assert repro["identical"] is True
    assert repro["identity_identical"] is True
    assert repro["executable_identical"] is True
    assert repro["checksums_identical"] is True
    assert repro["executable_first_sha256"] == repro["executable_second_sha256"]
    assert repro["checksums_first_sha256"] == repro["checksums_second_sha256"]

    repeat = _run([PY, str(script), "--bundle-dir", str(first),
                   "--compare", str(second)])
    assert repeat.returncode == 0, repeat.stderr
    assert json.loads(repeat.stdout)["reproducibility"] == repro

    checksums = (first / "checksums.sha256").read_text(encoding="utf-8").splitlines()
    assert checksums
    for line in checksums:
        digest, relative = line.split(" *", 1)
        actual = hashlib.sha256((first / relative).read_bytes()).hexdigest()
        assert actual == digest


def test_health_check_isolated_environment(tmp_path: Path):
    data_root = tmp_path / "health-data"
    output = tmp_path / "health.json"
    result = _run([PY, str(ROOT / "scripts" / "health_check.py"),
                   "--data-dir", str(data_root), "--json-output", str(output),
                   "--create-diagnostic"], timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "healthy"
    assert payload["database_integrity"] == "ok"
    assert payload["event_chain_valid"] is True
    assert payload["external_network_used"] is False
    assert payload["diagnostic"]["created"] is True


def test_database_cli_backup_migrate_restore_and_failure_safety(tmp_path: Path):
    from localdocmask.infrastructure.persistence.sqlite_repo import initialize_database
    db = tmp_path / "tasks.db"
    initialize_database(db, schema_version=3)
    backup = tmp_path / "v3-backup.db"
    script = ROOT / "scripts" / "db_maintenance.py"

    result = _run([PY, str(script), "backup", "--db", str(db), "--output", str(backup)])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["verification"]["integrity"] == "ok"

    result = _run([PY, str(script), "migrate", "--db", str(db), "--to", "4"])
    assert result.returncode == 0, result.stderr
    migrated = json.loads(result.stdout)
    assert migrated["after"]["schema_version"] == 4
    assert migrated["migration_backup"]["exists"] is True

    rollback_db = tmp_path / "rollback.db"
    result = _run([PY, str(script), "restore", "--backup", str(backup),
                   "--db", str(rollback_db)])
    assert result.returncode == 0, result.stderr
    restored = json.loads(result.stdout)
    assert restored["verification"]["schema_version"] == 3

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not-sqlite")
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    result = _run([PY, str(script), "restore", "--backup", str(corrupt), "--db", str(db)])
    assert result.returncode != 0
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    assert before == after


def test_acl_dry_run_and_apply_requires_backup(tmp_path: Path):
    assert POWERSHELL
    script = ROOT / "scripts" / "harden_data_acl.ps1"
    data = tmp_path / "acl-data"
    dry = _run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script), "-DataDir", str(data), "-DryRun"])
    assert dry.returncode == 0, dry.stderr
    assert "DRY_RUN_OK" in dry.stdout
    assert "icacls.exe" in dry.stdout
    assert data.exists() is False

    applied_without_backup = _run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                                   "-File", str(script), "-DataDir", str(data)])
    assert applied_without_backup.returncode != 0


def test_release_powershell_scripts_parse():
    assert POWERSHELL
    scripts = ["build_package.ps1", "harden_data_acl.ps1", "release_acceptance.ps1"]
    for name in scripts:
        path = ROOT / "scripts" / name
        command = (
            "$errors=$null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$null, [ref]$errors) | Out-Null; "
            "if ($errors) { $errors | Out-String | Write-Error; exit 1 }"
        )
        result = _run([POWERSHELL, "-NoProfile", "-Command", command])
        assert result.returncode == 0, result.stderr


def test_package_spec_excludes_development_icu_and_optional_layout():
    spec = (ROOT / "LocalDocMask.spec").read_text(encoding="utf-8")
    assert "icuuc.dll" in spec
    assert "icudt" in spec
    assert "pymupdf.layout" in spec
    assert "torch" in spec
    assert "runtime_hooks" in spec
    assert "pyi_startup_hook.py" in spec
    assert "upx=False" in spec


def test_package_spec_collects_only_approved_ner_model():
    spec = (ROOT / "LocalDocMask.spec").read_text(encoding="utf-8")
    assert "ner_bert4ner-approved" in spec
    assert '"models" / "ner_bert4ner"' not in spec
    assert '"resources/ner_bert4ner"' in spec or "'resources/ner_bert4ner'" in spec
    for name in (
        "model.onnx",
        "model.onnx.data",
        "model.safetensors",
        "vocab.txt",
        "id2label.json",
        "config.json",
        "special_tokens_map.json",
        "tokenizer_config.json",
        "LICENSE",
        "NOTICE",
        "SOURCE.md",
        "PROVENANCE.json",
    ):
        assert name in spec


def _synthetic_bundle(path: Path, exe_bytes: bytes = b"synthetic-executable") -> Path:
    (path / "resources").mkdir(parents=True)
    (path / "config").mkdir()
    (path / "resources" / "blacklist.txt").write_text("合成\n", encoding="utf-8")
    (path / "config" / "config.example.yaml").write_text(
        "schema_version: 1\n", encoding="utf-8")
    (path / "LocalDocMask.exe").write_bytes(exe_bytes)
    return path


def _write_bundle_metadata(bundle: Path, script: Path) -> None:
    result = _run([PY, str(script), "--bundle-dir", str(bundle),
                   "--build-time", "2026-10-30T00:00:00Z",
                   "--commit", "test-commit"])
    assert result.returncode == 0, result.stderr


def test_compare_bundles_detects_executable_mismatch(tmp_path: Path):
    first = _synthetic_bundle(tmp_path / "first", b"same-exe")
    second = _synthetic_bundle(tmp_path / "second", b"different-exe")
    script = ROOT / "scripts" / "build_metadata.py"
    _write_bundle_metadata(first, script)
    _write_bundle_metadata(second, script)
    result = _run([PY, str(script), "--bundle-dir", str(first),
                   "--compare", str(second)])
    assert result.returncode != 0
    repro = json.loads(result.stdout)["reproducibility"]
    assert repro["identity_identical"] is True
    assert repro["executable_identical"] is False
    assert repro["identical"] is False


def test_compare_bundles_detects_checksums_mismatch(tmp_path: Path):
    first = _synthetic_bundle(tmp_path / "first")
    second = _synthetic_bundle(tmp_path / "second")
    script = ROOT / "scripts" / "build_metadata.py"
    _write_bundle_metadata(first, script)
    _write_bundle_metadata(second, script)
    with (second / "checksums.sha256").open("a", encoding="utf-8") as stream:
        stream.write("synthetic-extra-line\n")
    result = _run([PY, str(script), "--bundle-dir", str(first),
                   "--compare", str(second)])
    assert result.returncode != 0
    repro = json.loads(result.stdout)["reproducibility"]
    assert repro["identity_identical"] is True
    assert repro["executable_identical"] is True
    assert repro["checksums_identical"] is False
    assert repro["identical"] is False


def test_write_metadata_with_dirty_true_is_recorded(tmp_path: Path):
    bundle = _synthetic_bundle(tmp_path / "bundle")
    script = ROOT / "scripts" / "build_metadata.py"
    result = _run([PY, str(script), "--bundle-dir", str(bundle),
                   "--build-time", "2026-10-30T00:00:00Z",
                   "--commit", "test-commit", "--dirty"])
    assert result.returncode == 0, result.stderr
    info = json.loads((bundle / "build-info.json").read_text(encoding="utf-8"))
    assert info["git_dirty"] is True


def test_release_db_drill_is_database_only(tmp_path: Path):
    output = tmp_path / "db-drill.json"
    result = _run([PY, str(ROOT / "scripts" / "release_db_drill.py"),
                   "--output", str(output)], timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["scope"] == "database-only"
    assert "old_binary_executed" not in json.dumps(payload)
    assert payload["status"] == "passed"


def test_source_file_inventory_uses_git_tracked_files_only(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "scripts" / "tracked.py").write_text("tracked = True\n", encoding="utf-8")
    (repo / "src" / "tracked.py").write_text("tracked = True\n", encoding="utf-8")

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    assert git("init").returncode == 0
    assert git("config", "user.name", "Fix04 Tests").returncode == 0
    assert git("config", "user.email", "fix04@example.invalid").returncode == 0
    assert git("add", "scripts/tracked.py", "src/tracked.py").returncode == 0
    assert git("commit", "-m", "initial").returncode == 0
    (repo / "scripts" / "_ignored_local.py").write_text("local = True\n", encoding="utf-8")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "build_metadata_source_test", ROOT / "scripts" / "build_metadata.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    relative = {path.relative_to(repo).as_posix()
                for path in module._source_files(repo)}
    assert relative == {"scripts/tracked.py", "src/tracked.py"}


def test_release_scripts_have_clean_gate_before_reset():
    build = (ROOT / "scripts" / "build_package.ps1").read_text(encoding="utf-8")
    accept = (ROOT / "scripts" / "release_acceptance.ps1").read_text(encoding="utf-8")
    assert "--check-clean-worktree" in build
    assert build.index("--check-clean-worktree") < build.index("$distRoot = Reset-BuildPath")
    assert "--dirty" not in build
    assert "--check-clean-worktree" in accept
    assert "passed_current_environment_with_unverified_external_items" not in accept
    assert "external-validation-status.json" in accept


def _identity_fixture() -> dict:
    return {
        "identity_schema_version": 1,
        "application": "LocalDocMask",
        "application_version": "0.1.0",
        "git_commit": "test-commit",
        "python_version": "3.11.16",
        "pyinstaller_version": "6.22.3",
        "database_schema_version": 4,
        "config_schema_version": 1,
        "policy_version": "recognition-v1",
        "source_tree_sha256": "a" * 64,
        "resource_tree_sha256": "b" * 64,
        "dependency_inventory_sha256": "c" * 64,
        "platform": "Windows-test",
    }


def _compare_identity_bundles(tmp_path: Path, first_identity: dict,
                              second_identity: dict) -> dict:
    first = _synthetic_bundle(tmp_path / "first")
    second = _synthetic_bundle(tmp_path / "second")
    for bundle, identity in ((first, first_identity), (second, second_identity)):
        (bundle / "bundle-identity.json").write_text(
            json.dumps(identity), encoding="utf-8")
        (bundle / "checksums.sha256").write_text(
            "same-checksums\n", encoding="utf-8")
    script = ROOT / "scripts" / "build_metadata.py"
    result = _run([PY, str(script), "--bundle-dir", str(first),
                   "--compare", str(second)])
    payload = json.loads(result.stdout)["reproducibility"]
    assert result.returncode != 0
    return payload


def test_compare_bundles_requires_identity_fields(tmp_path: Path):
    repro = _compare_identity_bundles(tmp_path, {}, {})
    assert repro["identity_identical"] is False
    assert repro["identical"] is False


def test_compare_bundles_detects_application_identity_mismatch(tmp_path: Path):
    first = _identity_fixture()
    second = _identity_fixture()
    second["application"] = "OtherApplication"
    repro = _compare_identity_bundles(tmp_path, first, second)
    assert repro["identity_identical"] is False
    assert repro["identical"] is False


def test_compare_bundles_detects_identity_schema_mismatch(tmp_path: Path):
    first = _identity_fixture()
    second = _identity_fixture()
    second["identity_schema_version"] = 2
    repro = _compare_identity_bundles(tmp_path, first, second)
    assert repro["identity_identical"] is False
    assert repro["identical"] is False


def _run_self_check(output_dir: Path, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(ROOT / "main.py"), "--release-self-check", str(output_dir)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )


def test_main_release_self_check_source_mode(tmp_path: Path):
    output = tmp_path / "self-check"
    result = _run_self_check(output)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(
        (output / "release-self-check.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["status"] == "passed"
    assert payload["frozen"] is False
    assert payload["event_chain_valid"] is True
    assert payload["bundle_runtime_data_absent"] is True
    for name in ("docx", "xlsx", "png"):
        case = payload["cases"][name]
        assert case["status"] == "passed"
        assert case["original_unchanged"] is True
        assert case["event_chain_valid"] is True
        original = Path(case["original_path"])
        output_file = Path(case["output_path"])
        assert hashlib.sha256(original.read_bytes()).hexdigest() == case["original_sha256_after"]
        assert output_file.is_file()
        assert hashlib.sha256(output_file.read_bytes()).hexdigest() == case["output_sha256"]
    assert payload["cases"]["png"]["ocr_used"] is True
    failure = payload["cases"]["failure"]
    assert failure["status"] == "failed_closed"
    assert failure["success_artifact_count"] == 0
    assert failure["quarantine_artifact_count"] > 0
    assert failure["event_chain_valid"] is True


def test_release_self_check_rejects_arbitrary_file_input(tmp_path: Path):
    user_file = tmp_path / "user-contract.docx"
    user_file.write_bytes(b"synthetic-user-file-must-not-be-read")
    result = subprocess.run(
        [PY, str(ROOT / "main.py"), "--release-self-check", str(user_file)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=10,
    )
    assert result.returncode != 0
    assert not (tmp_path / "user-contract.docx.release-self-check.json").exists()


def test_package_e2e_check_rejects_missing_bundle(tmp_path: Path):
    evidence = tmp_path / "missing-bundle.json"
    checker = ROOT / "scripts" / "package_e2e_check.py"
    result = _run([PY, str(checker), "--bundle-dir", str(tmp_path / "missing"),
                   "--evidence", str(evidence)], timeout=30)
    assert result.returncode != 0
    assert evidence.is_file()
    assert json.loads(evidence.read_text(encoding="utf-8"))["status"] == "failed"


def test_package_e2e_check_requires_exe_and_build_info(tmp_path: Path):
    checker = ROOT / "scripts" / "package_e2e_check.py"
    for missing in ("exe", "build-info"):
        bundle = tmp_path / missing
        bundle.mkdir()
        if missing != "exe":
            (bundle / "LocalDocMask.exe").write_bytes(b"synthetic-exe")
        if missing != "build-info":
            (bundle / "build-info.json").write_text(
                json.dumps({"git_commit": "a" * 40,
                            "executable_sha256": hashlib.sha256(
                                b"synthetic-exe").hexdigest()}),
                encoding="utf-8")
        evidence = tmp_path / f"{missing}.json"
        result = _run([PY, str(checker), "--bundle-dir", str(bundle),
                       "--evidence", str(evidence)], timeout=30)
        assert result.returncode != 0
        assert evidence.is_file()
        assert json.loads(evidence.read_text(encoding="utf-8"))["status"] == "failed"


def _self_check_result_fixture(tmp_path: Path) -> tuple[Path, dict, str, str]:
    root = tmp_path / "self-check"
    inputs = root / "inputs"
    outputs = root / "success"
    quarantine = root / "quarantine"
    inputs.mkdir(parents=True)
    outputs.mkdir()
    quarantine.mkdir()
    cases = {}
    for name, suffix in (("docx", ".docx"), ("xlsx", ".xlsx"), ("png", ".png")):
        original = inputs / f"{name}{suffix}"
        output = outputs / f"masked_{name}{suffix}"
        original.write_bytes(f"original-{name}".encode("ascii"))
        output.write_bytes(f"output-{name}".encode("ascii"))
        original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        cases[name] = {
            "status": "passed",
            "candidate_count": 3 if name == "docx" else 1,
            "original_path": str(original),
            "original_sha256_before": original_hash,
            "original_sha256_after": original_hash,
            "original_unchanged": True,
            "output_path": str(output),
            "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "event_chain_valid": True,
        }
    cases["png"]["ocr_used"] = True
    failure_output = quarantine / "failed.docx"
    failure_output.write_bytes(b"quarantine")
    cases["failure"] = {
        "status": "failed_closed",
        "success_artifact_count": 0,
        "quarantine_artifact_count": 1,
        "quarantine_path": str(failure_output),
        "output_hash": hashlib.sha256(failure_output.read_bytes()).hexdigest(),
        "event_chain_valid": True,
    }
    exe_hash = hashlib.sha256(b"synthetic-exe").hexdigest()
    commit = "a" * 40
    result = {
        "schema_version": 1,
        "status": "passed",
        "frozen": True,
        "executable_path": str(root / "LocalDocMask.exe"),
        "executable_sha256": exe_hash,
        "build_info_executable_sha256": exe_hash,
        "git_commit": commit,
        "started_at_utc": "2026-11-02T00:00:00Z",
        "finished_at_utc": "2026-11-02T00:01:00Z",
        "event_chain_valid": True,
        "bundle_runtime_data_absent": True,
        "cases": cases,
    }
    return root, result, exe_hash, commit


def _load_package_checker():
    import importlib.util
    path = ROOT / "scripts" / "package_e2e_check.py"
    spec = importlib.util.spec_from_file_location("package_e2e_check_fix05", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_package_e2e_check_validates_result_cases(tmp_path: Path):
    checker = _load_package_checker()
    root, result, exe_hash, commit = _self_check_result_fixture(tmp_path)
    valid = checker.validate_self_check_result(
        result, expected_exe_sha256=exe_hash,
        expected_git_commit=commit, output_root=root)
    assert valid["passed"] is True, valid["reason_codes"]

    mutations = []
    for missing in ("docx", "xlsx", "png"):
        case = json.loads(json.dumps(result))
        del case["cases"][missing]
        mutations.append((case, "SELF_CHECK_CASE_MISSING"))
    case = json.loads(json.dumps(result))
    case["cases"]["failure"]["success_artifact_count"] = 1
    mutations.append((case, "FAILURE_CASE_ENTERED_SUCCESS"))
    case = json.loads(json.dumps(result))
    case["cases"]["docx"]["original_sha256_after"] = "0" * 64
    mutations.append((case, "ORIGINAL_HASH_CHANGED"))
    case = json.loads(json.dumps(result))
    case["cases"]["xlsx"]["event_chain_valid"] = False
    mutations.append((case, "EVENT_CHAIN_INVALID"))
    case = json.loads(json.dumps(result))
    case["executable_sha256"] = "0" * 64
    mutations.append((case, "EXE_SHA_MISMATCH"))
    case = json.loads(json.dumps(result))
    case["git_commit"] = "b" * 40
    mutations.append((case, "GIT_COMMIT_MISMATCH"))

    for mutated, reason in mutations:
        checked = checker.validate_self_check_result(
            mutated, expected_exe_sha256=exe_hash,
            expected_git_commit=commit, output_root=root)
        assert checked["passed"] is False
        assert reason in checked["reason_codes"]


def test_build_package_runs_package_e2e_before_build_json():
    script = (ROOT / "scripts" / "build_package.ps1").read_text(encoding="utf-8")
    assert "package_e2e_check.py" in script
    e2e_pos = script.index("package_e2e_check.py")
    build_json_pos = script.index('(Join-Path $evidenceDir "build.json")')
    assert e2e_pos < build_json_pos
    assert "Assert-Success \"package e2e\"" in script or "package e2e failed" in script.lower()


def _load_build_metadata_for_sbom():
    import importlib.util
    path = ROOT / "scripts" / "build_metadata.py"
    spec = importlib.util.spec_from_file_location("build_metadata_fix08", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_sbom_only_contains_runtime_dependency_closure():
    module = _load_build_metadata_for_sbom()
    rows = module.dependency_inventory(ROOT)
    names = {row["name"].lower().replace("_", "-") for row in rows}
    required = {
        "pyside6", "pymupdf", "python-docx", "openpyxl", "pillow",
        "opencv-python", "rapidocr-onnxruntime", "onnxruntime",
        "requests", "pyyaml", "numpy", "lxml",
    }
    forbidden = {
        "pytest", "pyinstaller", "torch", "torchvision", "torchaudio",
        "transformers", "tensorflow",
    }
    assert required <= names, sorted(required - names)
    assert not (names & forbidden), sorted(names & forbidden)
    assert len(rows) > len(required)


def test_bundle_sbom_matches_runtime_inventory(tmp_path: Path):
    module = _load_build_metadata_for_sbom()
    bundle = _synthetic_bundle(tmp_path / "bundle")
    module.write_metadata(bundle, build_time="2026-11-02T00:00:00Z",
                          commit="test-commit", dirty=False)
    rows = module.dependency_inventory(ROOT)
    expected = {(row["name"].lower().replace("_", "-"), row["version"])
                for row in rows}
    sbom = json.loads((bundle / "SBOM.json").read_text(encoding="utf-8"))
    actual = {(component["name"].lower().replace("_", "-"),
               component["version"]) for component in sbom["components"]}
    assert actual == expected
    info = json.loads((bundle / "build-info.json").read_text(encoding="utf-8"))
    assert info["dependency_inventory_sha256"] == module._dependency_hash(rows)


def test_release_documents_have_single_status_source():
    files = (
        ROOT / "README.md",
        ROOT / "docs" / "operations" / "release_runbook.md",
        ROOT / "acceptance" / "final_acceptance_report.md",
        ROOT / "acceptance" / "enterprise_acceptance_matrix.json",
    )
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "dist/t9-evidence/release-summary.json" in text
        assert "唯一" in text or "status_source" in text


def test_release_documents_do_not_contain_stale_claims():
    files = (
        ROOT / "README.md",
        ROOT / "docs" / "feature_list.md",
        ROOT / "docs" / "quality_report.md",
        ROOT / "docs" / "operations" / "release_runbook.md",
        ROOT / "acceptance" / "final_acceptance_report.md",
        ROOT / "acceptance" / "enterprise_acceptance_matrix.json",
    )
    stale = (
        "157 passed",
        "158 tests",
        "用例 34 项全部通过",
        "映射落盘 opt-in",
        "NER ... 默认关闭",
        "审计/OCR 无超时",
        "GUI 无取消",
        "passed_current_environment_with_unverified_external_items",
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in files)
    for claim in stale:
        assert claim not in combined
    assert "ner.enabled=false" not in combined


def test_acceptance_matrix_current_status_is_evidence_backed():
    matrix = json.loads((ROOT / "acceptance" / "enterprise_acceptance_matrix.json")
                        .read_text(encoding="utf-8"))
    meta = matrix["meta"]
    assert meta["status_source"] == "dist/t9-evidence/release-summary.json"
    assert meta["current_candidate_status"] == "pending_external_validation"
    assert meta["production_release"] is False
    assert meta["status"] == "pending_external_validation"


def test_runtime_inventory_excludes_non_importable_metadata_package():
    module = _load_build_metadata_for_sbom()
    installed = {
        row.metadata.get("Name", "").lower().replace("_", "-")
        for row in importlib_metadata.distributions()
        if row.metadata.get("Name")
    }
    assert "pymupdf-layout" in installed
    rows = module.dependency_inventory(ROOT)
    names = {row["name"].lower().replace("_", "-") for row in rows}
    assert "pymupdf-layout" not in names


def test_sbom_excludes_spec_excluded_pymupdf_layout(tmp_path: Path):
    module = _load_build_metadata_for_sbom()
    bundle = _synthetic_bundle(tmp_path / "bundle")
    module.write_metadata(bundle, build_time="2026-11-02T00:00:00Z",
                          commit="test-commit", dirty=False)
    sbom = json.loads((bundle / "SBOM.json").read_text(encoding="utf-8"))
    names = {component["name"].lower().replace("_", "-")
             for component in sbom["components"]}
    refs = {component["bom-ref"] for component in sbom["components"]}
    assert "pymupdf-layout" not in names
    assert "pkg:pypi/pymupdf-layout@2.0" not in refs
    assert "pymupdf" in names
    assert "pymupdf4llm" in names
    for required in ("pyside6-addons", "pyside6-essentials",
                      "pyside6-pdf", "pyside6-webengine"):
        assert required in names, required


def test_runtime_inventory_components_have_importable_evidence():
    module = _load_build_metadata_for_sbom()
    rows = module.dependency_inventory(ROOT)
    installed = {}
    for distribution in importlib_metadata.distributions():
        name = distribution.metadata.get("Name") or ""
        if name:
            installed[name.lower().replace("_", "-")] = distribution
    for row in rows:
        distribution = installed[row["name"]]
        assert module._distribution_has_importable_module(distribution), row["name"]
