# -*- coding: utf-8 -*-
"""生成可重复构建版本清单、精简 SBOM 和 SHA-256 文件清单。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from localdocmask import __version__  # noqa: E402
from localdocmask.application.pipeline import POLICY_VERSION  # noqa: E402
from localdocmask.infrastructure.config.settings import CONFIG_SCHEMA_VERSION  # noqa: E402
from localdocmask.infrastructure.persistence.sqlite_repo import SCHEMA_VERSION  # noqa: E402


# PySide6 的多个发行包共享同一个顶层命名空间。这里显式记录，
# 避免仅按 distribution 名称推导时误删实际可导入的运行时包。
_SHARED_IMPORT_NAMES = {
    "pyside6": ("PySide6",),
    "pyside6-addons": ("PySide6",),
    "pyside6-essentials": ("PySide6",),
    "pyside6-pdf": ("PySide6",),
    "pyside6-webengine": ("PySide6",),
    "shiboken6": ("shiboken6", "Shiboken"),
    "protobuf": ("google.protobuf",),
}


def _distribution_import_names(dist: metadata.Distribution) -> tuple[str, ...]:
    names: set[str] = set()
    top_level = dist.read_text("top_level.txt") or ""
    for line in top_level.splitlines():
        module = line.strip().split("/", 1)[0]
        if module:
            names.add(module)
    canonical = canonicalize_name(dist.metadata.get("Name") or "")
    names.update(_SHARED_IMPORT_NAMES.get(canonical, ()))
    if canonical:
        names.add(canonical.replace("-", "_"))
    return tuple(sorted(names))


def _distribution_has_importable_module(dist: metadata.Distribution) -> bool:
    for module in _distribution_import_names(dist):
        try:
            if importlib.util.find_spec(module) is not None:
                return True
        except (ImportError, ModuleNotFoundError, AttributeError, ValueError):
            continue
    return False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_hash(root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


_SOURCE_ROOTS = frozenset({"src", "resources", "config", "scripts"})
_SOURCE_ROOT_FILES = frozenset({
    "main.py", "LocalDocMask.spec", "pyproject.toml",
    "requirements.txt", "uv.lock",
})
IDENTITY_KEYS = (
    "identity_schema_version",
    "application",
    "application_version",
    "git_commit",
    "python_version",
    "pyinstaller_version",
    "database_schema_version",
    "config_schema_version",
    "policy_version",
    "source_tree_sha256",
    "resource_tree_sha256",
    "dependency_inventory_sha256",
    "platform",
)


def check_clean_worktree(repo_root: str | Path = ROOT) -> dict:
    """拒绝 tracked 修改、暂存修改和任何未跟踪文件。"""
    root = Path(repo_root).resolve()
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False,
        )
    except Exception as exc:
        return {
            "passed": False,
            "reason_codes": ["GIT_STATUS_FAILED"],
            "detail": type(exc).__name__,
        }
    if result.returncode != 0:
        return {
            "passed": False,
            "reason_codes": ["GIT_STATUS_FAILED"],
            "detail": result.stderr.strip(),
        }
    dirty = bool(result.stdout.strip())
    return {
        "passed": not dirty,
        "reason_codes": ["GIT_WORKTREE_DIRTY"] if dirty else [],
        "detail": result.stdout.strip(),
    }


def _source_files(repo_root: str | Path = ROOT) -> list[Path]:
    """只返回显式源码根/根文件中的 Git 跟踪文件。"""
    root = Path(repo_root).resolve()
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "git ls-files failed: "
            + result.stderr.decode("utf-8", errors="replace").strip())
    files: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative_text = raw.decode("utf-8", errors="strict")
        relative = Path(relative_text)
        parts = relative.parts
        if not parts:
            continue
        if not (parts[0] in _SOURCE_ROOTS or relative.as_posix() in _SOURCE_ROOT_FILES):
            continue
        if "__pycache__" in parts or relative.suffix in {".pyc", ".pyo"}:
            continue
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"tracked source file missing: {path}")
        files.append(path)
    return files


def _resource_files(bundle: Path) -> list[Path]:
    roots = [bundle / "resources", bundle / "config"]
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(path for path in root.rglob("*") if path.is_file())
    return files


def dependency_inventory(repo_root: Path = ROOT) -> list[dict]:
    """解析 pyproject 直接依赖及其已安装传递依赖闭包。"""
    root = Path(repo_root).resolve()
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    direct_raw = pyproject.get("project", {}).get("dependencies")
    if not isinstance(direct_raw, list) or not direct_raw:
        raise RuntimeError("pyproject project.dependencies 缺失或为空")

    installed = {}
    for dist in metadata.distributions():
        display_name = dist.metadata.get("Name") or ""
        if display_name and dist.version:
            installed[canonicalize_name(display_name)] = dist

    resolved: dict[str, dict] = {}
    expanded: set[tuple[str, frozenset[str]]] = set()
    marker_env = default_environment()

    def applies(requirement: Requirement, extras: tuple[str, ...]) -> bool:
        if requirement.marker is None:
            return True
        return any(requirement.marker.evaluate({
            **marker_env, "extra": extra,
        }) for extra in extras)

    def resolve(requirement: Requirement) -> None:
        extras = tuple(sorted(requirement.extras))
        variants = ("", *extras)
        if not applies(requirement, variants):
            return
        key = canonicalize_name(requirement.name)
        dist = installed.get(key)
        if dist is None:
            raise RuntimeError(f"运行时依赖缺少已安装元数据：{requirement.name}")
        version = str(dist.version or "")
        if not version or not requirement.specifier.contains(version, prereleases=True):
            raise RuntimeError(
                f"运行时依赖版本不匹配：{requirement.name}{requirement.specifier}，实际 {version}")
        resolved[key] = {"name": key, "version": version}
        expand_key = (key, frozenset(extras))
        if expand_key in expanded:
            return
        expanded.add(expand_key)
        for dependency_text in (dist.requires or []):
            dependency = Requirement(dependency_text)
            if applies(dependency, variants):
                resolve(dependency)

    direct_names = {
        canonicalize_name(Requirement(str(raw)).name) for raw in direct_raw
    }
    for raw in direct_raw:
        resolve(Requirement(str(raw)))

    filtered: list[dict] = []
    ordered_rows = sorted(
        resolved.items(), key=lambda item: (item[1]["name"], item[1]["version"]))
    for key, row in ordered_rows:
        dist = installed[key]
        if _distribution_has_importable_module(dist):
            filtered.append(row)
            continue
        if key in direct_names:
            raise RuntimeError(f"直接运行时依赖没有可导入模块：{row['name']}")
        # 传递依赖中的无代码元包不应进入实际 bundle 的 SBOM。
    return filtered


def _dependency_hash(rows: list[dict]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")
    os.replace(temp, path)


def write_metadata(bundle: Path, *, build_time: str, commit: str,
                   dirty: bool) -> dict:
    if not bundle.is_dir():
        raise FileNotFoundError(f"bundle directory not found: {bundle}")
    source_hash = _tree_hash(ROOT, _source_files())
    resource_hash = _tree_hash(bundle, _resource_files(bundle))
    deps = dependency_inventory()
    dep_hash = _dependency_hash(deps)
    pyinstaller_version = metadata.version("PyInstaller")
    identity = {
        "identity_schema_version": 1,
        "application": "LocalDocMask",
        "application_version": __version__,
        "git_commit": commit,
        "python_version": platform.python_version(),
        "pyinstaller_version": pyinstaller_version,
        "database_schema_version": int(SCHEMA_VERSION),
        "config_schema_version": int(CONFIG_SCHEMA_VERSION),
        "policy_version": POLICY_VERSION,
        "source_tree_sha256": source_hash,
        "resource_tree_sha256": resource_hash,
        "dependency_inventory_sha256": dep_hash,
        "platform": platform.platform(),
    }
    executable = bundle / "LocalDocMask.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"bundle executable not found: {executable}")
    build_info = dict(identity)
    build_info.update({
        "build_time_utc": build_time,
        "git_dirty": bool(dirty),
        "executable_sha256": sha256_file(executable),
    })
    _write_json(bundle / "bundle-identity.json", identity)
    _write_json(bundle / "build-info.json", build_info)

    namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"localdocmask:{source_hash}:{__version__}")
    components = [{
        "type": "application",
        "name": "LocalDocMask",
        "version": __version__,
        "bom-ref": f"pkg:generic/localdocmask@{__version__}",
    }]
    for row in deps:
        safe_name = row["name"].lower().replace("_", "-")
        components.append({
            "type": "library",
            "name": row["name"],
            "version": row["version"],
            "bom-ref": f"pkg:pypi/{safe_name}@{row['version']}",
        })
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{namespace}",
        "version": 1,
        "metadata": {
            "component": components[0],
            "properties": [
                {"name": "localdocmask:git_commit", "value": commit},
                {"name": "localdocmask:source_tree_sha256", "value": source_hash},
            ],
        },
        "components": components[1:],
    }
    _write_json(bundle / "SBOM.json", sbom)
    write_checksums(bundle)
    return identity


def write_checksums(bundle: Path) -> Path:
    target = bundle / "checksums.sha256"
    lines = []
    for path in sorted(bundle.rglob("*"), key=lambda p: p.relative_to(bundle).as_posix()):
        if not path.is_file() or path == target:
            continue
        relative = path.relative_to(bundle).as_posix()
        lines.append(f"{sha256_file(path)} *{relative}")
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temp, target)
    return target


def compare_bundles(first: Path, second: Path) -> dict:
    try:
        a = json.loads((first / "bundle-identity.json").read_text(encoding="utf-8"))
    except Exception:
        a = None
    try:
        b = json.loads((second / "bundle-identity.json").read_text(encoding="utf-8"))
    except Exception:
        b = None
    first_obj = a if isinstance(a, dict) else {}
    second_obj = b if isinstance(b, dict) else {}
    missing_first = [
        key for key in IDENTITY_KEYS
        if key not in first_obj or first_obj.get(key) is None
    ]
    missing_second = [
        key for key in IDENTITY_KEYS
        if key not in second_obj or second_obj.get(key) is None
    ]
    identity_identical = (
        isinstance(a, dict)
        and isinstance(b, dict)
        and not missing_first
        and not missing_second
        and all(first_obj.get(key) == second_obj.get(key)
                for key in IDENTITY_KEYS)
    )
    first_exe = first / "LocalDocMask.exe"
    second_exe = second / "LocalDocMask.exe"
    first_checksums = first / "checksums.sha256"
    second_checksums = second / "checksums.sha256"
    exe_first = sha256_file(first_exe) if first_exe.is_file() else ""
    exe_second = sha256_file(second_exe) if second_exe.is_file() else ""
    checksums_first = (
        sha256_file(first_checksums) if first_checksums.is_file() else "")
    checksums_second = (
        sha256_file(second_checksums) if second_checksums.is_file() else "")
    executable_identical = bool(exe_first and exe_first == exe_second)
    checksums_identical = bool(
        checksums_first and checksums_first == checksums_second)
    return {
        "identity_required_fields": list(IDENTITY_KEYS),
        "identity_missing_fields_first": missing_first,
        "identity_missing_fields_second": missing_second,
        "identity_identical": identity_identical,
        "executable_first_sha256": exe_first,
        "executable_second_sha256": exe_second,
        "executable_identical": executable_identical,
        "checksums_first_sha256": checksums_first,
        "checksums_second_sha256": checksums_second,
        "checksums_identical": checksums_identical,
        "identical": (
            identity_identical
            and executable_identical
            and checksums_identical
        ),
        "compared_fields": list(IDENTITY_KEYS),
        "first": {key: first_obj.get(key) for key in IDENTITY_KEYS},
        "second": {key: second_obj.get(key) for key in IDENTITY_KEYS},
    }


def _load_json_file(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _parse_utc(value) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _safe_evidence_path(project_root: Path, value) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    relative = Path(value)
    if relative.is_absolute():
        return None
    candidate = (project_root / relative).resolve()
    try:
        candidate.relative_to(project_root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _all_repro_flags_true(build_evidence: dict) -> bool:
    repro = build_evidence.get("reproducibility") or {}
    if isinstance(repro, dict) and isinstance(repro.get("reproducibility"), dict):
        repro = repro["reproducibility"]
    if not isinstance(repro, dict):
        return False
    return all(repro.get(name) is True for name in (
        "identical", "identity_identical", "executable_identical",
        "checksums_identical",
    ))


def validate_release_gate(*, bundle_dir: str | Path,
                          build_evidence_path: str | Path,
                          external_status_path: str | Path,
                          expected_commit: str,
                          project_root: str | Path = ROOT) -> dict:
    """校验构建证据、实际 EXE 和真实外部验证状态。"""
    bundle = Path(bundle_dir).resolve()
    evidence_path = Path(build_evidence_path).resolve()
    external_path = Path(external_status_path).resolve()
    root = Path(project_root).resolve()
    reasons: list[str] = []

    build_info_path = bundle / "build-info.json"
    exe_path = bundle / "LocalDocMask.exe"
    actual_exe_hash = sha256_file(exe_path) if exe_path.is_file() else ""
    if not actual_exe_hash:
        reasons.append("BUNDLE_EXECUTABLE_MISSING")

    build_info = _load_json_file(build_info_path)
    if not build_info_path.is_file():
        reasons.append("BUILD_INFO_MISSING")
    elif build_info is None:
        reasons.append("BUILD_INFO_INVALID")
    else:
        if build_info.get("git_dirty") is not False:
            reasons.append("BUILD_INFO_GIT_DIRTY")
        if build_info.get("git_commit") != expected_commit:
            reasons.append("BUILD_INFO_COMMIT_MISMATCH")
        recorded_exe = build_info.get("executable_sha256")
        if not isinstance(recorded_exe, str) or not recorded_exe:
            reasons.append("BUILD_INFO_EXECUTABLE_MISSING")
        elif not re.fullmatch(r"[0-9a-fA-F]{64}", recorded_exe):
            reasons.append("BUILD_INFO_EXECUTABLE_INVALID")
        elif recorded_exe.lower() != actual_exe_hash:
            reasons.append("BUILD_INFO_EXECUTABLE_MISMATCH")

    build_evidence = _load_json_file(evidence_path)
    if not evidence_path.is_file():
        reasons.append("BUILD_EVIDENCE_MISSING")
    elif build_evidence is None:
        reasons.append("BUILD_EVIDENCE_INVALID")
    else:
        if build_evidence.get("status") != "passed":
            reasons.append("BUILD_EVIDENCE_NOT_PASSED")
        if build_evidence.get("git_dirty") is not False:
            reasons.append("BUILD_GIT_DIRTY")
        if build_evidence.get("git_commit") != expected_commit:
            reasons.append("BUILD_COMMIT_MISMATCH")
        if build_evidence.get("executable_sha256") != actual_exe_hash:
            reasons.append("BUILD_EVIDENCE_EXECUTABLE_MISMATCH")
        repro = build_evidence.get("reproducibility") or {}
        if isinstance(repro, dict) and isinstance(repro.get("reproducibility"), dict):
            repro = repro["reproducibility"]
        repro_fields = (
            "identical",
            "identity_identical",
            "executable_identical",
            "checksums_identical",
        )
        if not isinstance(repro, dict):
            repro = {}
        missing_repro = [name for name in repro_fields if name not in repro]
        if missing_repro:
            reasons.append("BUILD_REPRO_FIELDS_MISSING")
        if repro.get("identical") is not True:
            reasons.append("BUILD_REPRO_IDENTICAL_NOT_TRUE")
        if repro.get("identity_identical") is not True:
            reasons.append("BUILD_REPRO_IDENTITY_MISMATCH")
        if repro.get("executable_identical") is not True:
            reasons.append("BUILD_REPRO_EXECUTABLE_MISMATCH")
        if repro.get("checksums_identical") is not True:
            reasons.append("BUILD_REPRO_CHECKSUMS_MISMATCH")

    external = _load_json_file(external_path)
    if not external_path.is_file():
        reasons.append("EXTERNAL_STATUS_MISSING")
        external = None
    elif external is None:
        reasons.append("EXTERNAL_STATUS_INVALID")

    checks = {
        "clean_windows_vm_verified": False,
        "other_user_logon_verified": False,
        "old_binary_rollback_verified": False,
    }
    external_view = {
        "path": str(external_path),
        "release_candidate": None,
        "executable_sha256": None,
        "candidate_matches": False,
        "executable_matches": False,
        "checks": checks,
    }
    if external is not None:
        valid_shape = True
        for name in ("release_candidate", "executable_sha256",
                     "updated_at_utc", "clean_windows_vm_evidence",
                     "other_user_logon_evidence", "old_binary_rollback_evidence"):
            if not isinstance(external.get(name), str):
                valid_shape = False
        if not _parse_utc(external.get("updated_at_utc")):
            valid_shape = False
        candidate_value = external.get("release_candidate")
        executable_value = external.get("executable_sha256")
        if isinstance(executable_value, str) and not re.fullmatch(
                r"[0-9a-fA-F]{64}", executable_value):
            valid_shape = False
        if not valid_shape:
            reasons.append("EXTERNAL_STATUS_INVALID")

        for name in checks:
            checks[name] = external.get(name) is True
        if not all(checks.values()):
            reasons.append("EXTERNAL_REQUIRED_CHECKS_NOT_TRUE")

        candidate_matches = (
            isinstance(candidate_value, str) and candidate_value == expected_commit)
        executable_matches = (
            isinstance(executable_value, str)
            and executable_value.lower() == actual_exe_hash)
        external_view.update({
            "release_candidate": candidate_value,
            "executable_sha256": executable_value,
            "candidate_matches": candidate_matches,
            "executable_matches": executable_matches,
        })
        if not candidate_matches:
            reasons.append("EXTERNAL_STATUS_CANDIDATE_MISMATCH")
        if not executable_matches:
            reasons.append("EXTERNAL_STATUS_EXECUTABLE_MISMATCH")

        evidence_fields = (
            "clean_windows_vm_evidence",
            "other_user_logon_evidence",
            "old_binary_rollback_evidence",
        )
        paths = [_safe_evidence_path(root, external.get(name))
                 for name in evidence_fields]
        if any(path is None for path in paths):
            reasons.append("EXTERNAL_EVIDENCE_PATH_MISSING")

    reasons = list(dict.fromkeys(reasons))
    return {
        "passed": not reasons,
        "reason_codes": reasons,
        "build": {
            "git_commit": build_info.get("git_commit") if build_info else None,
            "git_dirty": build_info.get("git_dirty") if build_info else None,
            "executable_sha256": actual_exe_hash,
            "reproducibility_identical": bool(
                build_evidence
                and _all_repro_flags_true(build_evidence)
            ),
        },
        "external_checks": checks,
        "external_validation": external_view,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-dir", default="")
    parser.add_argument("--build-time", default="")
    parser.add_argument("--commit", default="unknown")
    parser.add_argument("--dirty", action="store_true")
    parser.add_argument("--compare", default="")
    parser.add_argument("--json-output", default="")
    parser.add_argument("--check-clean-worktree", action="store_true")
    parser.add_argument("--repo-root", default=str(ROOT))
    parser.add_argument("--validate-release-gate", action="store_true")
    parser.add_argument("--build-evidence", default="")
    parser.add_argument("--external-status", default="")
    parser.add_argument("--expected-commit", default="")
    args = parser.parse_args()

    if args.check_clean_worktree:
        result = check_clean_worktree(args.repo_root)
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
        if args.json_output:
            Path(args.json_output).write_text(payload + "\n", encoding="utf-8")
        print(payload)
        return 0 if result["passed"] else 1

    if args.validate_release_gate:
        missing = [
            name for name, value in (
                ("--bundle-dir", args.bundle_dir),
                ("--build-evidence", args.build_evidence),
                ("--external-status", args.external_status),
                ("--expected-commit", args.expected_commit),
            ) if not value
        ]
        if missing:
            parser.error("validate-release-gate 缺少参数：" + ", ".join(missing))
        result = validate_release_gate(
            bundle_dir=args.bundle_dir,
            build_evidence_path=args.build_evidence,
            external_status_path=args.external_status,
            expected_commit=args.expected_commit,
            project_root=ROOT,
        )
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
        if args.json_output:
            Path(args.json_output).write_text(payload + "\n", encoding="utf-8")
        print(payload)
        return 0 if result["passed"] else 1

    if not args.bundle_dir:
        parser.error("--bundle-dir 必填")
    bundle = Path(args.bundle_dir).resolve()
    if args.compare:
        result = {
            "reproducibility": compare_bundles(
                bundle, Path(args.compare).resolve())
        }
    else:
        build_time = args.build_time or datetime.now(timezone.utc).isoformat(timespec="seconds")
        identity = write_metadata(bundle, build_time=build_time,
                                  commit=args.commit, dirty=args.dirty)
        result = {"identity": identity, "checksums": str(bundle / "checksums.sha256")}
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
    if args.json_output:
        Path(args.json_output).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    reproducibility = result.get("reproducibility")
    return 0 if not reproducibility or reproducibility["identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
