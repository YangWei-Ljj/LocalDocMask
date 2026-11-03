"""崩溃恢复与幂等复用（ADR-005 / T3）。

- PROCESSING 无输出 -> PENDING，记录恢复事件并释放 claim；
- PROCESSING 有输出 -> INTERRUPTED，不自动进入成功目录；
- PENDING claim（claim 后、首次扫描前崩溃）-> 释放 claim，保持 PENDING 可重试；
- 所有恢复写入失败显式抛出，由组合根失败关闭。
"""
from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path

from ...domain.entities import Task, TaskEvent
from ...domain.enums import TaskStatus
from ...domain.exceptions import ErrorCode, LocalDocMaskError
from ...domain.state_machine import current_actor, transition_task
from ...security.path_safety import safe_join
from .sqlite_repo import SqliteTaskRepository


def _phase_event(code: str, from_status: str, to_status: str,
                 detail: str = "", duration_ms: int = 0) -> TaskEvent:
    return TaskEvent(event_code=code, stage="recover",
                     from_status=from_status, to_status=to_status,
                     actor=current_actor(), detail=detail,
                     duration_ms=max(0, int(duration_ms)))


class RecoveryService:
    """扫描持久化任务与磁盘残留，把中断任务重新归类。"""

    def recover(self, repository: SqliteTaskRepository,
                processing_dir: str | Path,
                success_dir: str | Path | None = None,
                quarantine_dir: str | Path | None = None) -> list[Task]:
        processing_path = Path(processing_dir)
        processing_path.mkdir(parents=True, exist_ok=True)
        affected: list[Task] = []

        # 1) PROCESSING 任务：按是否存在产物决定重排或中断
        for task in repository.list_tasks(TaskStatus.PROCESSING):
            started = time.monotonic()
            try:
                repository.save_task(task, events=[_phase_event(
                    "phase.recover.start", TaskStatus.PROCESSING.name,
                    TaskStatus.PROCESSING.name)])
                has_output = bool(task.output_path and Path(task.output_path).exists())
                if has_output:
                    note = "恢复：检测到残留输出文件，未自动认定安全，标记为已中断"
                    target = TaskStatus.INTERRUPTED
                    event_code = "recovery.interrupted"
                else:
                    note = "恢复：崩溃时未产出输出，已重新入队等待处理"
                    target = TaskStatus.PENDING
                    event_code = "recovery.requeue"
                task.error = (task.error + "；" if task.error else "") + note
                transition_task(task, target, reason=event_code)
                repository.save_task(
                    task,
                    events=[_phase_event(
                        event_code, TaskStatus.PROCESSING.name, target.name,
                        detail=f"has_output={has_output}",
                        duration_ms=int((time.monotonic() - started) * 1000))],
                    release_claim=not has_output,
                )
                repository.append_operation(task.task_id, "recover", note)
                affected.append(task)
            except Exception as exc:
                raise LocalDocMaskError(
                    f"恢复任务 {task.task_id} 失败：{type(exc).__name__}: {exc}",
                    code=ErrorCode.PERSISTENCE_ERROR,
                ) from exc

        # 2) PENDING claim：释放声明，避免永久 in_progress；有产物则安全中断
        for claim in repository.list_claims():
            task = repository.load_task(claim["task_id"])
            if task is None or task.status != TaskStatus.PENDING:
                continue
            started = time.monotonic()
            try:
                has_output = bool(task.output_path and Path(task.output_path).exists())
                if has_output:
                    task.error = (task.error + "；" if task.error else "") + \
                        "恢复：PENDING claim 指向已有产物，标记为中断"
                    transition_task(task, TaskStatus.INTERRUPTED,
                                    reason="recovery_pending_claim_interrupted")
                    event_code = "recovery.pending_claim_interrupted"
                    to_status = TaskStatus.INTERRUPTED.name
                else:
                    task.error = (task.error + "；" if task.error else "") + \
                        "恢复：释放 PENDING claim，任务可安全重试"
                    event_code = "recovery.pending_claim_released"
                    to_status = TaskStatus.PENDING.name
                repository.save_task(
                    task,
                    events=[
                        _phase_event("phase.recover.start",
                                     TaskStatus.PENDING.name,
                                     TaskStatus.PENDING.name),
                        _phase_event(event_code, TaskStatus.PENDING.name,
                                     to_status, detail=f"has_output={has_output}",
                                     duration_ms=int((time.monotonic() - started) * 1000)),
                    ],
                    release_claim=True,
                )
                repository.append_operation(task.task_id, "recover",
                                            "释放 PENDING 幂等声明")
                affected.append(task)
            except Exception as exc:
                raise LocalDocMaskError(
                    f"恢复 PENDING claim 任务 {task.task_id} 失败："
                    f"{type(exc).__name__}: {exc}",
                    code=ErrorCode.PERSISTENCE_ERROR,
                ) from exc
        if success_dir is not None and quarantine_dir is not None:
            affected.extend(self._reconcile_success_dir(
                repository, Path(success_dir), Path(quarantine_dir)))
        return affected

    def _reconcile_success_dir(self, repository: SqliteTaskRepository,
                               success_dir: Path, quarantine_dir: Path) -> list[Task]:
        """启动对账：success 中未由 PASSED+有效 hash 证明的产物移入 quarantine。"""
        if not success_dir.exists():
            return []
        affected: list[Task] = []
        for artifact in sorted(success_dir.iterdir()):
            if not artifact.is_file():
                continue
            task = self._task_for_artifact(repository, artifact)
            if artifact.name.startswith(".") and artifact.name.endswith(".tmp"):
                kind = "orphan_temp"
            elif (task is not None and task.status == TaskStatus.PASSED
                  and repository.is_reusable_task(task)
                  and self._confirmed_artifact(task, artifact)):
                continue
            else:
                kind = "orphan_success"
            try:
                dest = self._quarantine_artifact(artifact, quarantine_dir, kind)
                if task is not None:
                    event = _phase_event(
                        "recovery." + kind, task.status.name, task.status.name,
                        detail=kind)
                    repository.save_task(task, events=[event])
                    affected.append(task)
                else:
                    repository.append_operation(
                        "*", "recovery." + kind,
                        "未关联任务的产物已隔离")
            except Exception as exc:
                raise LocalDocMaskError(
                    f"启动对账失败（{artifact.name}）：{type(exc).__name__}: {exc}",
                    code=ErrorCode.PERSISTENCE_ERROR,
                ) from exc
        return affected

    @staticmethod
    def _confirmed_artifact(task: Task, artifact: Path) -> bool:
        """PASSED 任务只允许其主产物或内容绑定 final_hash 的审计报告留在 success。"""
        try:
            resolved = artifact.resolve()
        except OSError:
            return False
        if task.output_path is not None and Path(task.output_path).resolve() == resolved:
            return True
        report_pattern = (
            rf"(?:audit_{re.escape(task.task_id)}|.*_审计记录_{re.escape(task.task_id)})\.json"
        )
        if not task.task_id or re.fullmatch(report_pattern, artifact.name) is None:
            return False
        try:
            payload = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        return (payload.get("task_id") == task.task_id
                and payload.get("final_sha256") == task.final_hash
                and payload.get("source_sha256") == task.source_hash)

    @staticmethod
    def _task_for_artifact(repository: SqliteTaskRepository, artifact: Path) -> Task | None:
        try:
            resolved = artifact.resolve()
        except OSError:
            resolved = artifact
        tasks = repository.list_tasks()
        for task in tasks:
            for candidate in (task.output_path, task.report_path):
                if candidate is None:
                    continue
                try:
                    if Path(candidate).resolve() == resolved:
                        return task
                except OSError:
                    pass
        for task in tasks:
            if task.task_id and task.task_id in artifact.name:
                return task
        return None

    @staticmethod
    def _quarantine_artifact(artifact: Path, quarantine_dir: Path, kind: str) -> Path:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        dest = safe_join(
            quarantine_dir,
            f"{artifact.stem}.{kind}.{uuid.uuid4().hex[:8]}{artifact.suffix}")
        try:
            shutil.move(str(artifact), str(dest))
            return dest
        except Exception:
            try:
                artifact.unlink()
                return dest
            except Exception as exc:
                raise LocalDocMaskError(
                    f"无法隔离产物：{type(exc).__name__}: {exc}",
                    code=ErrorCode.PERSISTENCE_ERROR,
                ) from exc


def existing_success(repository: SqliteTaskRepository, source_hash: str,
                     mode: str, policy_version: str,
                     config_hash: str = "", tool_version: str = "") -> dict | None:
    """返回已验证成功且产物仍在位的任务信息；无则返回 None。"""
    if config_hash or tool_version:
        finder = getattr(repository, "find_reusable", None)
        if finder is not None:
            task = finder(source_hash, mode, policy_version,
                          config_hash, tool_version)
            if task is not None:
                return {
                    "task_id": task.task_id,
                    "output_path": str(task.output_path),
                    "final_hash": task.final_hash,
                }
            return None
    for task in repository.list_tasks():
        if (task.source_hash != source_hash
                or task.mode != mode
                or task.policy_version != policy_version):
            continue
        checker = getattr(repository, "is_reusable_task", None)
        reusable = checker(task) if checker is not None else (
            task.status == TaskStatus.PASSED
            and task.output_path
            and Path(task.output_path).exists()
        )
        if reusable:
            return {
                "task_id": task.task_id,
                "output_path": str(task.output_path),
                "final_hash": task.final_hash,
            }
    return None
