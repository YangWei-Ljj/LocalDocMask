# -*- coding: utf-8 -*-
"""T4：统一文件准入、资源限制、原子导出与启动对账。"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from docx import Document
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont

import localdocmask.application.pipeline as pipeline_mod
import localdocmask.infrastructure.persistence.sqlite_repo as sqlite_repo_mod
import localdocmask.security.atomic_io as atomic_io_mod
from localdocmask.application.batch import BatchService
from localdocmask.application.pipeline import TaskManager, compute_config_hash
from localdocmask.domain.entities import AuditFinding, AuditReport, ScanResult, Task
from localdocmask.domain.enums import AuditVerdict, EntityDecision, TaskStatus
from localdocmask.infrastructure.config.settings import Settings
from localdocmask.infrastructure.persistence.recovery import RecoveryService
from localdocmask.infrastructure.persistence.sqlite_repo import SqliteTaskRepository
from localdocmask.recognition.rule_based import Detector
from localdocmask.security.admission import AdmissionError, FileAdmission
from localdocmask.security.auditor import Auditor
from localdocmask.infrastructure.formats.docx import DocxHandler
from localdocmask.security.atomic_io import AtomicExportError, atomic_copy
from localdocmask.security.hashing import sha256_file
from localdocmask.security.limits import ResourceLimits


def _docx(path: Path, text: str = "普通测试内容") -> Path:
    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))
    return path


def _pdf(path: Path, pages: int = 1) -> Path:
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"test page {index + 1}")
    doc.save(str(path))
    doc.close()
    return path


def _xlsx(path: Path) -> Path:
    wb = Workbook()
    wb.active["A1"] = "普通测试内容"
    wb.save(str(path))
    return path


def _xlsm(path: Path) -> Path:
    _xlsx(path)
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("xl/vbaProject.bin", b"synthetic-vba-placeholder")
    return path


def _image(path: Path, fmt: str, size: tuple[int, int] = (32, 32)) -> Path:
    Image.new("RGB", size, "white").save(path, format=fmt)
    return path


def _reason(path: Path, reason: str, *, limits: ResourceLimits | None = None) -> None:
    with pytest.raises(AdmissionError) as caught:
        FileAdmission(limits=limits).admit(path)
    assert caught.value.reason_code == reason


def _mark_zip_encrypted(path: Path) -> None:
    data = bytearray(path.read_bytes())
    offset = data.find(b"PK\x01\x02")
    assert offset >= 0
    flags = int.from_bytes(data[offset + 8:offset + 10], "little")
    data[offset + 8:offset + 10] = (flags | 0x1).to_bytes(2, "little")
    path.write_bytes(data)


def _atomic_task(tmp_path: Path, task_id: str) -> Task:
    source = _docx(tmp_path / f"source_{task_id}.docx", "源文件普通内容")
    output = _docx(tmp_path / f"output_{task_id}.docx", "脱敏后的普通内容")
    final_hash = sha256_file(output)
    task = Task(
        source_path=source,
        task_id=task_id,
        file_type="docx",
        status=TaskStatus.PROCESSED_UNAUDITED,
        source_hash=sha256_file(source),
        output_path=output,
        final_hash=final_hash,
        mode="",
        policy_version="test-policy",
        config_hash="test-config",
        tool_version="test-tool",
        scan=ScanResult(),
        records=[],
    )
    task.audit = AuditReport(
        passed=True,
        findings=[AuditFinding(AuditVerdict.NO_RESIDUAL, "最终审计通过", "text")],
    )
    task.report_path = tmp_path / f"report_{task_id}.json"
    task.report_path.write_text(json.dumps({
        "task_id": task_id,
        "source_sha256": task.source_hash,
        "final_sha256": final_hash,
        "records": [],
    }, ensure_ascii=False), encoding="utf-8")
    return task


@pytest.fixture()
def output_dirs(tmp_path, monkeypatch):
    processing = tmp_path / "processing"
    success = tmp_path / "success"
    quarantine = tmp_path / "quarantine"
    for path in (processing, success, quarantine):
        path.mkdir()
    monkeypatch.setattr(pipeline_mod, "PROCESSING_DIR", processing)
    monkeypatch.setattr(pipeline_mod, "SUCCESS_DIR", success)
    monkeypatch.setattr(pipeline_mod, "QUARANTINE_DIR", quarantine)
    return processing, success, quarantine


def test_admission_rejects_missing_directory_unreadable_and_oversize(tmp_path, monkeypatch):
    _reason(tmp_path / "missing.docx", "file_not_found")

    directory = tmp_path / "directory.docx"
    directory.mkdir()
    _reason(directory, "not_a_file")

    source = _docx(tmp_path / "unreadable.docx")
    import builtins
    real_open = builtins.open

    def denied_open(file, *args, **kwargs):
        if Path(file) == source:
            raise PermissionError("synthetic permission denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied_open)
    _reason(source, "file_unreadable")
    monkeypatch.setattr(builtins, "open", real_open)

    limits = replace(ResourceLimits.from_settings(Settings()), max_file_size_bytes=1)
    _reason(source, "file_too_large", limits=limits)


def test_admission_accepts_all_enabled_real_types(tmp_path):
    paths = [
        _pdf(tmp_path / "ok.pdf"),
        _docx(tmp_path / "ok.docx"),
        _xlsx(tmp_path / "ok.xlsx"),
        _xlsm(tmp_path / "ok.xlsm"),
        _image(tmp_path / "ok.png", "PNG"),
        _image(tmp_path / "ok.jpg", "JPEG"),
        _image(tmp_path / "ok.tiff", "TIFF"),
        _image(tmp_path / "ok.bmp", "BMP"),
    ]
    types = [FileAdmission().admit(path).real_type for path in paths]
    assert types == ["pdf", "docx", "xlsx", "xlsm", "png", "jpeg", "tiff", "bmp"]


def test_extension_and_magic_mismatches_are_rejected(tmp_path):
    pdf_as_docx = _pdf(tmp_path / "fake.docx")
    _reason(pdf_as_docx, "extension_mismatch")

    invalid_pdf = tmp_path / "invalid.pdf"
    invalid_pdf.write_bytes(b"%PDF-1.7\nnot a complete pdf")
    _reason(invalid_pdf, "pdf_corrupt")

    png_as_jpeg = tmp_path / "fake.jpg"
    Image.new("RGB", (8, 8), "white").save(png_as_jpeg, format="PNG")
    _reason(png_as_jpeg, "extension_mismatch")


@pytest.mark.parametrize("entry_name", ["../escape.xml", "/absolute.xml", "C:/drive.xml"])
def test_zip_path_escape_entries_are_rejected(tmp_path, entry_name):
    path = _docx(tmp_path / "path-attack.docx")
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr(entry_name, b"malicious")
    _reason(path, "zip_path_escape")


def test_zip_duplicate_entry_is_rejected(tmp_path):
    path = _docx(tmp_path / "duplicate.docx")
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("word/document.xml", b"duplicate")
    _reason(path, "zip_duplicate_entry")


def test_zip_encrypted_entry_is_rejected(tmp_path):
    path = _docx(tmp_path / "encrypted.docx")
    _mark_zip_encrypted(path)
    _reason(path, "zip_encrypted")


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("max_zip_entries", 1, "zip_entries_exceeded"),
        ("max_zip_uncompressed_bytes", 10, "zip_total_too_large"),
        ("max_zip_compression_ratio", 1.0, "zip_ratio_exceeded"),
        ("max_zip_entry_bytes", 10, "zip_entry_too_large"),
    ],
)
def test_zip_resource_limits_are_enforced(tmp_path, field, value, reason):
    path = _docx(tmp_path / f"{field}.docx")
    limits = replace(ResourceLimits.from_settings(Settings()), **{field: value})
    _reason(path, reason, limits=limits)


def test_source_change_during_admission_is_rejected(tmp_path, monkeypatch):
    path = _pdf(tmp_path / "changing.pdf", pages=1)
    original_check = FileAdmission._check_pdf

    def mutate_during_check(self, candidate):
        result = original_check(self, candidate)
        candidate.write_bytes(candidate.read_bytes() + b"\nchanged")
        return result

    monkeypatch.setattr(FileAdmission, "_check_pdf", mutate_during_check)
    _reason(path, "source_changed_during_admission")


def test_pdf_page_and_image_pixel_limits_are_enforced(tmp_path):
    pdf = _pdf(tmp_path / "pages.pdf", pages=2)
    pdf_limits = replace(ResourceLimits.from_settings(Settings()), max_pdf_pages=1)
    _reason(pdf, "pdf_pages_exceeded", limits=pdf_limits)

    image = _image(tmp_path / "pixels.png", "PNG", size=(10, 10))
    image_limits = replace(ResourceLimits.from_settings(Settings()), max_image_pixels=99)
    _reason(image, "image_pixels_exceeded", limits=image_limits)


def test_unc_traversal_symlink_and_reparse_paths_are_rejected(tmp_path, monkeypatch):
    with pytest.raises(AdmissionError) as unc:
        FileAdmission().admit(r"\\server\share\secret.docx")
    assert unc.value.reason_code == "unc_path_rejected"

    with pytest.raises(AdmissionError) as traversal:
        FileAdmission().admit(tmp_path / ".." / "secret.docx")
    assert traversal.value.reason_code == "path_traversal_rejected"

    with pytest.raises(AdmissionError) as device:
        FileAdmission().admit(r"\Device\HarddiskVolume1\secret.docx")
    assert device.value.reason_code == "unc_path_rejected"

    with pytest.raises(AdmissionError) as drive_relative:
        FileAdmission().admit(r"C:relative.docx")
    assert drive_relative.value.reason_code == "drive_relative_path"

    target = _docx(tmp_path / "target.docx")
    real_is_symlink = Path.is_symlink

    def fake_is_symlink(self):
        return self == target or real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    with pytest.raises(AdmissionError) as symlink:
        FileAdmission().admit(target)
    assert symlink.value.reason_code == "symlink_rejected"
    monkeypatch.setattr(Path, "is_symlink", real_is_symlink)

    real_lstat = os.lstat

    def fake_lstat(path):
        if Path(path) == target:
            return SimpleNamespace(st_file_attributes=0x400)
        return real_lstat(path)

    monkeypatch.setattr(os, "lstat", fake_lstat)
    with pytest.raises(AdmissionError) as reparse:
        FileAdmission().admit(target)
    assert reparse.value.reason_code == "reparse_point_rejected"


def test_admission_rejection_persists_fail_closed_event(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "admission.db")
    try:
        tm = TaskManager(Detector(), repository=repo, settings=Settings())
        bad = tmp_path / "bad.docx"
        bad.write_bytes(b"not an ooxml document")
        result = BatchService(tm).run([bad], mode="a")
        assert len(result.failed) == 1
        task = result.failed[0]
        stored = repo.load_task(task.task_id)
        assert stored is not None and stored.status == TaskStatus.AUDIT_FAILED
        events = repo.list_events(task.task_id)
        failed = [e for e in events if e["event_code"] == "phase.admission.failed"]
        assert len(failed) == 1
        assert failed[0]["error_code"] == "unknown_type"
        assert repo.verify_event_chain().valid
    finally:
        repo.close()


def test_full_pipeline_preserves_source_and_records_admission_events(
        tmp_path, output_dirs):
    source = _docx(tmp_path / "source.docx", "张三 13812345678")
    before = sha256_file(source)
    repo = SqliteTaskRepository(tmp_path / "pipeline.db")
    try:
        tm = TaskManager(
            Detector(persons=["张三"]), repository=repo, settings=Settings())
        task = tm.prepare(source, mode="a")
        tm.scan(task)
        tm.process(task, "a", "****", task.scan.candidates)
        tm.finalize(task, task.audit)
        assert task.status == TaskStatus.PASSED
        assert sha256_file(source) == before == task.source_hash
        assert sha256_file(task.output_path) == task.final_hash
        assert not list(output_dirs[1].glob("*.tmp"))
        codes = {e["event_code"] for e in repo.list_events(task.task_id)}
        assert {"phase.admission.start", "phase.admission.end",
                "phase.export.hash", "phase.export.replace",
                "phase.export.db_confirm"} <= codes
        assert repo.verify_event_chain().valid
    finally:
        repo.close()


def test_atomic_export_never_exposes_partial_final_file(tmp_path, output_dirs, monkeypatch):
    success = output_dirs[1]
    repo = SqliteTaskRepository(tmp_path / "atomic.db")
    task = _atomic_task(tmp_path, "atomic001")
    repo.save_task(task)
    real_replace = atomic_io_mod.os.replace
    observed = []

    def checked_replace(src, dest):
        source_path = Path(src)
        dest_path = Path(dest)
        assert source_path.name.endswith(".tmp")
        assert not dest_path.exists()
        observed.append(dest_path.name)
        return real_replace(src, dest)

    monkeypatch.setattr(atomic_io_mod.os, "replace", checked_replace)
    try:
        tm = TaskManager(repository=repo, settings=Settings())
        tm.finalize(task, task.audit)
        assert task.status == TaskStatus.PASSED
        assert sha256_file(task.output_path) == task.final_hash
        assert observed and all("atomic001" in name for name in observed)
        assert not [p for p in success.iterdir() if p.name.startswith(".") or p.suffix == ".tmp"]
    finally:
        repo.close()


def test_same_stem_different_tasks_do_not_overwrite(tmp_path, output_dirs):
    first = _atomic_task(tmp_path, "same001")
    second = _atomic_task(tmp_path, "same002")
    second.source_path = first.source_path
    second.source_hash = first.source_hash
    tm = TaskManager(settings=Settings())
    tm.finalize(first, first.audit)
    tm.finalize(second, second.audit)
    outputs = sorted(p.name for p in output_dirs[1].glob("masked_*.docx"))
    assert len(outputs) == 2
    assert any("same001" in name for name in outputs)
    assert any("same002" in name for name in outputs)
    assert all(Path(output_dirs[1] / name).is_file() for name in outputs)


def test_atomic_copy_refuses_overwrite_and_hash_mismatch(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"expected-content")
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    existing = target_dir / "existing.bin"
    existing.write_bytes(b"keep-me")
    with pytest.raises(AtomicExportError):
        atomic_copy(source, target_dir, "existing.bin")
    assert existing.read_bytes() == b"keep-me"

    with pytest.raises(AtomicExportError, match="SHA-256"):
        atomic_copy(source, target_dir, "new.bin", expected_sha256="0" * 64)
    assert not (target_dir / "new.bin").exists()
    assert not list(target_dir.glob(".*.tmp"))


def test_event_write_failure_rolls_back_passed_and_compensates(
        tmp_path, output_dirs, monkeypatch):
    repo = SqliteTaskRepository(tmp_path / "event-fail.db")
    task = _atomic_task(tmp_path, "dbfail001")
    repo.save_task(task)
    original_insert = sqlite_repo_mod._insert_event_conn

    def fail_on_confirm(conn, *, event_code, **kwargs):
        if event_code == "phase.export.db_confirm":
            raise RuntimeError("injected event failure")
        return original_insert(conn, event_code=event_code, **kwargs)

    monkeypatch.setattr(sqlite_repo_mod, "_insert_event_conn", fail_on_confirm)
    try:
        tm = TaskManager(repository=repo, settings=Settings())
        with pytest.raises(RuntimeError, match="injected event failure"):
            tm.finalize(task, task.audit)
        assert task.status != TaskStatus.PASSED
        stored = repo.load_task(task.task_id)
        assert stored is not None and stored.status != TaskStatus.PASSED
        assert not list(output_dirs[1].iterdir())
        assert list(output_dirs[2].iterdir())
        codes = {e["event_code"] for e in repo.list_events(task.task_id)}
        assert "phase.export.failed" in codes
        assert "phase.export.db_confirm" not in codes
    finally:
        repo.close()


def test_crash_after_replace_is_quarantined_on_startup(
        tmp_path, output_dirs, monkeypatch):
    repo = SqliteTaskRepository(tmp_path / "crash.db")
    task = _atomic_task(tmp_path, "crash001")
    repo.save_task(task)
    success = output_dirs[1]
    quarantine = output_dirs[2]
    real_replace = atomic_io_mod.os.replace

    def crash_after_replace(src, dest):
        real_replace(src, dest)
        raise SystemExit("simulated process death after atomic replace")

    monkeypatch.setattr(atomic_io_mod.os, "replace", crash_after_replace)
    try:
        tm = TaskManager(repository=repo, settings=Settings())
        with pytest.raises(SystemExit):
            tm.finalize(task, task.audit)
        assert list(success.iterdir())
        assert repo.load_task(task.task_id).status == TaskStatus.PROCESSED_UNAUDITED

        RecoveryService().recover(
            repo, output_dirs[0], success_dir=success, quarantine_dir=quarantine)
        assert not list(success.iterdir())
        assert list(quarantine.iterdir())
        stored = repo.load_task(task.task_id)
        assert stored is not None and stored.status != TaskStatus.PASSED
        codes = {e["event_code"] for e in repo.list_events(task.task_id)}
        assert "recovery.orphan_success" in codes
    finally:
        repo.close()


def test_recovery_keeps_confirmed_artifacts_and_preserves_user_files(
        tmp_path, output_dirs):
    success = output_dirs[1]
    quarantine = output_dirs[2]
    repo = SqliteTaskRepository(tmp_path / "recovery.db")
    task = _atomic_task(tmp_path, "confirmed001")
    final_output = success / f"masked_{task.task_id}.docx"
    final_report = success / f"audit_{task.task_id}.json"
    final_output.write_bytes(task.output_path.read_bytes())
    final_report.write_text(json.dumps({
        "task_id": task.task_id,
        "source_sha256": task.source_hash,
        "final_sha256": task.final_hash,
    }), encoding="utf-8")
    task.output_path = final_output
    task.report_path = final_report
    task.status = TaskStatus.PASSED
    repo.save_task(task)
    source_before = sha256_file(task.source_path)
    preserved_quarantine = quarantine / "manual-review.docx"
    preserved_quarantine.write_bytes(b"user-controlled quarantine")
    orphan_temp = success / ".unfinished.tmp"
    orphan_temp.write_bytes(b"partial")
    try:
        RecoveryService().recover(
            repo, output_dirs[0], success_dir=success, quarantine_dir=quarantine)
        assert final_output.exists() and final_report.exists()
        assert not orphan_temp.exists()
        assert preserved_quarantine.exists()
        assert sha256_file(task.source_path) == source_before
    finally:
        repo.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_pdf_pages", 501),
        ("max_image_pixels", 100_000_001),
        ("max_zip_entries", 2001),
        ("max_zip_uncompressed_mb", 501.0),
        ("max_zip_compression_ratio", 101.0),
        ("max_zip_entry_mb", 201.0),
    ],
)
def test_t4_security_limits_change_config_hash(field, value):
    base = Settings()
    changed = Settings()
    setattr(changed.security, field, value)
    assert compute_config_hash(base) != compute_config_hash(changed)


def test_t4_pipeline_does_not_open_network_sockets(tmp_path, output_dirs, monkeypatch):
    class BlockedSocket:
        def __init__(self, *args, **kwargs):
            raise AssertionError("T4 pipeline unexpectedly opened a network socket")

    def blocked_create_connection(*args, **kwargs):
        raise AssertionError("T4 pipeline unexpectedly created a network connection")

    monkeypatch.setattr(socket, "socket", BlockedSocket)
    monkeypatch.setattr(socket, "create_connection", blocked_create_connection)
    source = _docx(tmp_path / "offline.docx", "张三 13812345678")
    tm = TaskManager(Detector(persons=["张三"]), settings=Settings())
    task = tm.prepare(source, mode="a")
    tm.scan(task)
    tm.process(task, "a", "****", task.scan.candidates)
    tm.finalize(task, task.audit)
    assert task.status == TaskStatus.PASSED


def test_compensation_cleanup_failure_is_explicit(
        tmp_path, output_dirs, monkeypatch):
    task = _atomic_task(tmp_path, "cleanup001")
    tm = TaskManager(settings=Settings())

    def fail_cleanup(*args, **kwargs):
        raise AtomicExportError("injected cleanup failure")

    original_record = tm._record_phase

    def fail_after_replace(task, event_code, stage, **kwargs):
        if event_code == "phase.export.replace":
            raise RuntimeError("injected failure after publish")
        return original_record(task, event_code, stage, **kwargs)

    monkeypatch.setattr(pipeline_mod, "quarantine_unconfirmed", fail_cleanup)
    monkeypatch.setattr(tm, "_record_phase", fail_after_replace)
    with pytest.raises(AtomicExportError, match="无法清理"):
        tm.finalize(task, task.audit)
    assert task.status != TaskStatus.PASSED


def _corrupt_zip_entry_crc(path: Path, member: str) -> None:
    data = bytearray(path.read_bytes())
    needle = member.encode("utf-8")
    pos = 0
    found = False
    while True:
        pos = data.find(b"PK\x01\x02", pos)
        if pos < 0:
            break
        name_len = int.from_bytes(data[pos + 28:pos + 30], "little")
        extra_len = int.from_bytes(data[pos + 30:pos + 32], "little")
        comment_len = int.from_bytes(data[pos + 32:pos + 34], "little")
        name_start = pos + 46
        if data[name_start:name_start + name_len] == needle:
            data[pos + 16] ^= 0xFF
            found = True
            break
        pos += 46 + name_len + extra_len + comment_len
    assert found, f"未找到 ZIP 条目：{member}"
    path.write_bytes(data)


def test_docx_embedded_ole_fails_closed_across_layers(tmp_path, output_dirs):
    source = _docx(tmp_path / "ole.docx", "普通测试内容")
    with zipfile.ZipFile(source, "a") as zf:
        zf.writestr("word/embeddings/oleObject1.bin", b"synthetic-ole")
    source_hash = sha256_file(source)

    with pytest.raises(AdmissionError) as caught:
        FileAdmission().admit(source)
    assert caught.value.reason_code == "docx_embedded_object"

    handler = DocxHandler(Detector())
    scan = handler.extract(str(source))
    assert "docx_embedded_object_uncheckable" in scan.review_flags
    assert any("嵌入对象" in warning for warning in scan.warnings)
    with pytest.raises(ValueError, match="嵌入对象"):
        handler.process(str(source), str(tmp_path / "ole-out.docx"), scan.candidates)

    report = Auditor().audit(str(source), [])
    assert report.passed is False
    assert any(f.verdict == AuditVerdict.UNCHECKABLE for f in report.findings)

    tm = TaskManager(Detector(), settings=Settings())
    result = BatchService(tm).run([source], mode="a")
    assert len(result.failed) == 1
    assert not list(output_dirs[1].iterdir())
    assert sha256_file(source) == source_hash


def test_xlsx_corrupt_xml_crc_blocks_batch_success(tmp_path, output_dirs):
    source = _xlsx(tmp_path / "crc.xlsx")
    _corrupt_zip_entry_crc(source, "docProps/app.xml")
    source_hash = sha256_file(source)

    with pytest.raises(AdmissionError) as caught:
        FileAdmission().admit(source)
    assert caught.value.reason_code == "ooxml_part_unreadable"

    tm = TaskManager(Detector(), settings=Settings())
    result = BatchService(tm).run([source], mode="a")
    assert len(result.failed) == 1
    assert not list(output_dirs[1].iterdir())
    assert sha256_file(source) == source_hash


def _inject_related_custom_xml_binary(path: Path) -> None:
    temp = path.with_name(path.name + ".customxml.tmp")
    with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(
            temp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "[Content_Types].xml":
                text = data.decode("utf-8")
                if 'Extension="bin"' not in text:
                    text = text.replace(
                        "</Types>",
                        '<Default Extension="bin" ContentType="application/octet-stream"/></Types>',
                    )
                data = text.encode("utf-8")
            elif item.filename == "word/_rels/document.xml.rels":
                text = data.decode("utf-8")
                text = text.replace(
                    "</Relationships>",
                    '<Relationship Id="rIdCustom1" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml" '
                    'Target="../customXml/item1.bin"/></Relationships>',
                )
                data = text.encode("utf-8")
            zout.writestr(item, data)
        zout.writestr("customXml/item1.bin", b"synthetic-custom-binary")
    os.replace(temp, path)


def test_docx_related_customxml_binary_fails_closed_across_layers(tmp_path, output_dirs):
    source = _docx(tmp_path / "custom-xml.docx", "普通测试内容")
    _inject_related_custom_xml_binary(source)
    with zipfile.ZipFile(source) as zf:
        names = set(zf.namelist())
        rels = zf.read("word/_rels/document.xml.rels").decode("utf-8")
    assert "customXml/item1.bin" in names
    assert "../customXml/item1.bin" in rels
    source_hash = sha256_file(source)

    with pytest.raises(AdmissionError) as caught:
        FileAdmission().admit(source)
    assert caught.value.reason_code == "docx_embedded_object"

    handler = DocxHandler(Detector())
    scan = handler.extract(str(source))
    assert "docx_embedded_object_uncheckable" in scan.review_flags
    assert any("嵌入对象" in warning for warning in scan.warnings)
    with pytest.raises(ValueError, match="嵌入对象"):
        handler.process(str(source), str(tmp_path / "custom-out.docx"), scan.candidates)

    report = Auditor().audit(str(source), [])
    assert report.passed is False
    assert any(f.verdict == AuditVerdict.UNCHECKABLE for f in report.findings)

    tm = TaskManager(Detector(), settings=Settings())
    result = BatchService(tm).run([source], mode="a")
    assert len(result.failed) == 1
    assert not list(output_dirs[1].iterdir())
    assert sha256_file(source) == source_hash


def test_docx_sensitive_thumbnail_is_redacted_and_never_exported_without_review(
        tmp_path, output_dirs):
    script = tmp_path / "thumbnail_flow.py"
    script.write_text(r"""
import io
import os
import sys
import zipfile
from pathlib import Path

from docx import Document
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(sys.argv[1])
WORK = Path(sys.argv[2])
SUCCESS = Path(sys.argv[3])
sys.path.insert(0, str(ROOT / "src"))
os.environ["LOCALDOCMASK_HOME"] = str(WORK / "home")

from localdocmask.application.batch import BatchService
from localdocmask.application.pipeline import TaskManager
from localdocmask.infrastructure.config.settings import Settings
from localdocmask.infrastructure.formats.docx import DocxHandler
from localdocmask.recognition.rule_based import Detector
import localdocmask.application.pipeline as pipeline_mod
from localdocmask.security.admission import FileAdmission
from localdocmask.security.auditor import Auditor
from localdocmask.security.hashing import sha256_file


def replace_thumbnail(path: Path, png_path: Path) -> None:
    image_bytes = png_path.read_bytes()
    temp = path.with_name(path.name + ".thumbnail.tmp")
    with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(
            temp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == "docProps/thumbnail.jpeg":
                continue
            data = zin.read(item.filename)
            if item.filename == "[Content_Types].xml":
                text = data.decode("utf-8").replace(
                    '<Default Extension="jpeg" ContentType="image/jpeg"/>',
                    '<Default Extension="jpeg" ContentType="image/jpeg"/>'
                    '<Default Extension="png" ContentType="image/png"/>',
                )
                data = text.encode("utf-8")
            elif item.filename == "_rels/.rels":
                text = data.decode("utf-8").replace(
                    "docProps/thumbnail.jpeg", "docProps/thumbnail.png")
                data = text.encode("utf-8")
            zout.writestr(item, data)
        zout.writestr("docProps/thumbnail.png", image_bytes)
    os.replace(temp, path)


source = WORK / "thumbnail.docx"
doc = Document()
doc.add_paragraph("普通测试内容")
doc.save(source)
thumbnail = WORK / "sensitive-thumbnail.png"
image = Image.new("RGB", (760, 180), "white")
ImageDraw.Draw(image).text(
    (20, 45), "李四 13812345678", fill="black",
    font=ImageFont.truetype(r"C:\Windows\Fonts\simhei.ttf", 48))
image.save(thumbnail, format="PNG")
replace_thumbnail(source, thumbnail)
sensitive_thumbnail = thumbnail.read_bytes()
source_hash = sha256_file(source)


class ThumbnailOCR:
    def ocr(self, image_bytes):
        if bytes(image_bytes) == sensitive_thumbnail:
            return [{
                "box": [[20, 45], [520, 45], [520, 110], [20, 110]],
                "text": "李四 13812345678",
                "confidence": 0.99,
            }]
        return []


ocr = ThumbnailOCR()
admitted = FileAdmission().admit(source)
assert admitted.real_type == "docx"
handler = DocxHandler(Detector(persons=["李四"]), ocr_engine=ocr)
scan = handler.extract(str(source))
thumb_candidates = [
    c for c in scan.candidates
    if c.image_part.lower() == "/docprops/thumbnail.png"
]
assert thumb_candidates
for candidate in thumb_candidates:
    candidate.delete = True
    candidate.replacement = ""

redacted = WORK / "thumbnail-redacted.docx"
handler.process(str(source), str(redacted), thumb_candidates)
with zipfile.ZipFile(redacted) as zf:
    output_image = zf.read("docProps/thumbnail.png")
assert ocr.ocr(output_image) == []
with Image.open(io.BytesIO(output_image)) as opened:
    redacted_pixels = opened.convert("RGB")
    x0, y0, x1, y1 = thumb_candidates[0].box
    region = list(redacted_pixels.crop((x0, y0, x1, y1)).get_flattened_data())
assert region and all(pixel == (0, 0, 0) for pixel in region)
report = Auditor(ocr_engine=ocr).audit(
    str(redacted), [c.text for c in thumb_candidates])
assert report.passed, report.summary()
assert sha256_file(source) == source_hash

pipeline_mod.handler_for = lambda *args, **kwargs: handler
tm = TaskManager(Detector(persons=["李四"]), settings=Settings())
result = BatchService(tm).run([source], mode="a")
assert len(result.review) == 1
assert not result.passed
assert not list(SUCCESS.iterdir())
assert sha256_file(source) == source_hash
print("THUMBNAIL_OK")
""", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [sys.executable, str(script), str(Path(__file__).resolve().parents[1]),
         str(tmp_path), str(output_dirs[1])],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "THUMBNAIL_OK" in result.stdout
