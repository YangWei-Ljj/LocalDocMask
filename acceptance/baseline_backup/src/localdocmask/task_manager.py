"""任务管理器：扫描 → 处理 → 审计 → 导出/隔离 的编排，源文件哈希校验。"""
from __future__ import annotations

import shutil
from pathlib import Path

from .auditor import Auditor
from .config import INPUT_DIR, PROCESSING_DIR, OUTPUT_DIR, QUARANTINE_DIR, ensure_dirs
from .formats import handler_for, supported_extensions
from .hashing import sha256_file
from .masker import Masker
from .models import AuditReport, ScanResult, Task, TaskStatus


class TaskManager:
    def __init__(self, detector, use_ollama: bool = False, ollama_model: str = ""):
        self.detector = detector
        self.auditor = Auditor()
        self.use_ollama = use_ollama
        self.ollama_model = ollama_model

    def supported(self) -> list[str]:
        return supported_extensions()

    def prepare(self, source_path: str | Path) -> Task:
        ensure_dirs()
        p = Path(source_path)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在：{p}")
        if p.suffix.lower() not in supported_extensions():
            raise ValueError(f"不支持的文件格式：{p.suffix}（支持：{', '.join(supported_extensions())}）")
        task = Task(source_path=p)
        task.source_hash = sha256_file(p)
        return task

    def scan(self, task: Task) -> ScanResult:
        task.status = TaskStatus.PROCESSING
        handler = handler_for(task.source_path, self.detector)
        scan = handler.extract(str(task.source_path))
        if self.use_ollama and scan.text:
            self._ollama_assist(scan)
        task.scan = scan
        if scan.candidates:
            task.status = TaskStatus.NEEDS_REVIEW
        else:
            task.status = TaskStatus.PROCESSED_UNAUDITED
        return scan

    def process(self, task: Task, mode: str, mask: str, candidates: list) -> Path:
        ensure_dirs()
        handler = handler_for(task.source_path, self.detector)
        masker = Masker(mode, mask)
        candidates = sorted(candidates, key=lambda c: len(c.text), reverse=True)
        for cand in candidates:
            if cand.excluded:
                continue
            cand.replacement = cand.replacement or masker.replacement(cand.category, cand.text)
        stem = task.source_path.stem + "_脱敏"
        if handler.name == "image":
            out_path = PROCESSING_DIR / (stem + ".png")
        elif task.source_path.suffix.lower() == ".xlsm":
            out_path = PROCESSING_DIR / (stem + ".xlsx")
        else:
            out_path = PROCESSING_DIR / (stem + task.source_path.suffix.lower())
        warnings = handler.process(str(task.source_path), str(out_path), candidates)
        task.output_path = out_path
        task.status = TaskStatus.PROCESSED_UNAUDITED
        task.error = ""
        for w in warnings:
            pass  # 警告保留在 scan.warnings 之外，由调用方处理
        return out_path

    def audit(self, task: Task, candidates: list) -> AuditReport:
        if task.output_path is None:
            raise RuntimeError("尚未处理，无法审计")
        terms = [c.text for c in candidates if not c.excluded and len(c.text) >= 2]
        report = self.auditor.audit(str(task.output_path), terms)
        task.audit = report
        return report

    def finalize(self, task: Task, report: AuditReport) -> Task:
        # 校验源文件未被修改
        if sha256_file(task.source_path) != task.source_hash:
            task.status = TaskStatus.UNSAFE
            task.error = "源文件哈希发生变化，原件可能被修改，已停止导出"
            self._move_to_quarantine(task)
            return task
        if report.passed:
            dest = OUTPUT_DIR / task.output_path.name
            shutil.copy2(str(task.output_path), str(dest))
            task.output_path = dest
            task.status = TaskStatus.PASSED
        elif any(f.verdict.value == "发现残留" for f in report.findings):
            task.status = TaskStatus.AUDIT_FAILED
            self._move_to_quarantine(task)
        else:
            task.status = TaskStatus.UNSAFE
            self._move_to_quarantine(task)
        return task

    def _ollama_assist(self, scan):
        from .models import Candidate
        from .ollama import OllamaClient
        try:
            cli = OllamaClient(model=self.ollama_model) if self.ollama_model else OllamaClient()
            existing = {(c.category, c.text) for c in scan.candidates}
            for cat, text in cli.extract_entities(scan.text[:8000]):
                if (cat, text) in existing:
                    continue
                scan.candidates.append(Candidate(text=text, category=cat, location="本地模型辅助",
                                                 confidence=0.6, source="ollama"))
        except Exception as e:
            scan.warnings.append(f"本地模型辅助识别失败：{e}")

    def _move_to_quarantine(self, task: Task):
        if task.output_path is not None and task.output_path.exists():
            dest = QUARANTINE_DIR / task.output_path.name
            shutil.move(str(task.output_path), str(dest))
            task.output_path = dest
