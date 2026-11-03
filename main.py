"""LocalDocMask startup entry and hidden packaged release self-check."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parent
if str(_SOURCE_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT / "src"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _prepare_output_root(value: str) -> Path:
    root = Path(value).resolve()
    executable_dir = Path(sys.executable).resolve().parent
    try:
        root.relative_to(executable_dir)
    except ValueError:
        pass
    else:
        raise ValueError("自检输出目录不得位于应用 bundle 内")
    if root.exists():
        if not root.is_dir():
            raise ValueError("自检输出路径必须是目录")
        if any(root.iterdir()):
            raise ValueError("自检输出目录必须为空")
    else:
        root.mkdir(parents=True)
    return root


def _create_docx(path: Path, text: str) -> None:
    from docx import Document

    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))


def _create_failure_docx(path: Path) -> None:
    from docx import Document

    doc = Document()
    paragraph = doc.add_paragraph("普通合成说明")
    run = paragraph.add_run("复核备注")
    doc.add_comment(runs=run, text="审计残留人", author="审计残留人",
                    initials="AR")
    doc.save(str(path))


def _create_xlsx(path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "合成数据"
    ws["A1"] = "李思远"
    ws["B1"] = "13812345678"
    wb.save(str(path))
    wb.close()


def _create_png(path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (900, 180), "white")
    font_path = Path(r"C:\Windows\Fonts\simhei.ttf")
    font = (ImageFont.truetype(str(font_path), 48)
            if font_path.is_file() else ImageFont.load_default())
    ImageDraw.Draw(image).text((30, 45), "13812345678", fill="black", font=font)
    image.save(path, "PNG")


def _confirm_all(task, tm) -> None:
    candidates = list(task.scan.candidates if task.scan else [])
    for candidate in candidates:
        tm.update_candidate_review(
            task, candidate.candidate_id, confirmed=True,
            replacement="****", reason_code="release_self_check",
        )


def _success_case(tm, source: Path, *, ocr_used: bool = False) -> dict:
    before = _sha256(source)
    task = tm.prepare(source, mode="a")
    scan = tm.scan(task)
    if not scan.candidates:
        raise RuntimeError(f"自检未生成候选：{source.suffix}")
    _confirm_all(task, tm)
    tm.process(task, "a", "****", task.scan.candidates)
    report = tm.audit(task, task.scan.candidates)
    task = tm.finalize(task, report)
    after = _sha256(source)
    if task.status.name != "PASSED" or not report.passed:
        raise RuntimeError(f"自检任务未通过：{source.suffix}:{task.status.name}")
    if task.output_path is None or not task.output_path.is_file():
        raise RuntimeError(f"自检输出不存在：{source.suffix}")
    output_hash = _sha256(task.output_path)
    return {
        "status": "passed",
        "candidate_count": len(scan.candidates),
        "original_path": str(source),
        "original_sha256_before": before,
        "original_sha256_after": after,
        "original_unchanged": before == after,
        "output_path": str(task.output_path),
        "output_sha256": output_hash,
        "event_chain_valid": bool(tm.repository.verify_event_chain().valid),
        "ocr_used": bool(ocr_used),
    }


def _failure_case(tm) -> dict:
    from localdocmask.domain.enums import TaskStatus
    from localdocmask.infrastructure.config.settings import SUCCESS_DIR, QUARANTINE_DIR

    inputs = Path(os.environ["LOCALDOCMASK_HOME"]) / ".." / "inputs"
    source = (inputs / "failure-comment.docx").resolve()
    _create_failure_docx(source)
    before = _sha256(source)
    task = tm.prepare(source, mode="a")
    scan = tm.scan(task)
    if not scan.candidates:
        raise RuntimeError("失败案例未生成审计候选")
    _confirm_all(task, tm)
    tm.process(task, "a", "****", task.scan.candidates)
    task = tm.finalize(task, task.audit)
    after = _sha256(source)
    success_count = len(list(Path(SUCCESS_DIR).glob(f"masked_{task.task_id}*")))
    quarantined = sorted(Path(QUARANTINE_DIR).glob(f"*{task.task_id}*"))
    if task.status == TaskStatus.PASSED or success_count != 0 or not quarantined:
        raise RuntimeError("失败案例未按失败关闭进入 quarantine")
    quarantine_hash = _sha256(quarantined[0]) if quarantined[0].is_file() else ""
    return {
        "status": "failed_closed",
        "task_status": task.status.name,
        "audit_passed": bool(task.audit and task.audit.passed),
        "success_artifact_count": success_count,
        "quarantine_artifact_count": len(quarantined),
        "quarantine_path": str(quarantined[0]),
        "output_hash": quarantine_hash,
        "original_path": str(source),
        "original_sha256_before": before,
        "original_sha256_after": after,
        "original_unchanged": before == after,
        "event_chain_valid": bool(tm.repository.verify_event_chain().valid),
    }


def _release_self_check(output_value: str) -> int:
    output_root = _prepare_output_root(output_value)
    result_path = output_root / "release-self-check.json"
    inputs = output_root / "inputs"
    inputs.mkdir(parents=True, exist_ok=False)
    runtime_root = output_root / "runtime-data"
    os.environ["LOCALDOCMASK_HOME"] = str(runtime_root)
    os.environ["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM", "offscreen")

    executable_path = Path(sys.executable).resolve()
    executable_hash_before = _sha256(executable_path)
    build_info_path = executable_path.parent / "build-info.json"
    build_info = {}
    if build_info_path.is_file():
        try:
            build_info = json.loads(build_info_path.read_text(encoding="utf-8-sig"))
        except Exception:
            build_info = {}
    if build_info:
        git_commit = str(build_info.get("git_commit") or "")
        build_info_exe = str(build_info.get("executable_sha256") or "")
    else:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, encoding="utf-8", check=False)
        git_commit = proc.stdout.strip() if proc.returncode == 0 else ""
        build_info_exe = executable_hash_before

    payload = {
        "schema_version": 1,
        "status": "failed",
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable_path": str(executable_path),
        "executable_sha256": executable_hash_before,
        "git_commit": git_commit,
        "build_info_executable_sha256": build_info_exe,
        "started_at_utc": _now_utc(),
        "finished_at_utc": "",
        "event_chain_valid": False,
        "bundle_runtime_data_absent": not (executable_path.parent / "data").exists(),
        "cases": {},
    }

    services = None
    try:
        from localdocmask.application.composition import build_services
        from localdocmask.infrastructure.config.settings import Settings

        settings = Settings()
        settings.ollama.enabled = False
        settings.ner.enabled = True
        services = build_services(
            settings=settings,
            extra_companies=["杭州云启科技有限公司"],
            extra_persons=["赵启明", "李思远", "审计残留人"],
        )
        tm = services.pipeline

        docx = inputs / "success.docx"
        xlsx = inputs / "success.xlsx"
        png = inputs / "success.png"
        _create_docx(docx, "赵启明 杭州云启科技有限公司 13812345678")
        _create_xlsx(xlsx)
        _create_png(png)

        payload["cases"]["docx"] = _success_case(tm, docx)
        payload["cases"]["xlsx"] = _success_case(tm, xlsx)
        payload["cases"]["png"] = _success_case(tm, png, ocr_used=True)
        payload["cases"]["failure"] = _failure_case(tm)
        payload["event_chain_valid"] = bool(
            services.repository.verify_event_chain().valid)
        payload["bundle_runtime_data_absent"] = not (
            executable_path.parent / "data").exists()
        payload["executable_sha256_after"] = _sha256(executable_path)
        if payload["executable_sha256_after"] != executable_hash_before:
            raise RuntimeError("自检期间 EXE SHA-256 发生变化")
        payload["finished_at_utc"] = _now_utc()
        payload["status"] = "passed"
        _write_json(result_path, payload)
        return 0
    except Exception as exc:
        payload["finished_at_utc"] = _now_utc()
        payload["error_type"] = type(exc).__name__
        payload["error_message"] = str(exc)
        payload["traceback"] = traceback.format_exc(limit=8)
        try:
            _write_json(result_path, payload)
        except Exception:
            pass
        return 1
    finally:
        if services is not None:
            services.close()


def main() -> int:
    multiprocessing.freeze_support()
    argv = sys.argv[1:]
    if argv and argv[0] == "--release-self-check":
        if len(argv) != 2:
            print("用法：LocalDocMask.exe --release-self-check <空的输出目录>",
                  file=sys.stderr)
            return 2
        try:
            return _release_self_check(argv[1])
        except Exception as exc:
            print(f"release self-check rejected: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 2
    if argv:
        print("未知命令行参数", file=sys.stderr)
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from localdocmask.presentation.gui.main_window import run
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
