"""可终止的持久子进程守卫（FIX-06）。

每个任务上下文复用一个独立 spawn worker，依次执行 scan/process/audit 高风险
操作。阶段预算只从 worker 完成反序列化并返回 ready 后开始；超时或取消时只
terminate/kill 当前任务 worker，并 join 回收，其他并发任务 worker 不受影响。
"""
from __future__ import annotations

import atexit
import multiprocessing as mp
import multiprocessing.spawn as mp_spawn
import pickle
import sys
import threading
import time
import weakref
from typing import Any, Callable

from ..domain.exceptions import TaskCancelledError, TaskTimedOutError


class OperationGuardError(RuntimeError):
    """子进程执行守卫基础设施失败。"""


class UnpicklableOperationError(OperationGuardError):
    """操作无法跨 Windows spawn 进程边界传递。"""


class WorkerProcessError(OperationGuardError):
    """子进程异常退出且未返回可用结果。"""


def _worker_entry(conn) -> None:
    conn.send(("session_ready",))
    while True:
        try:
            payload = conn.recv_bytes()
        except (EOFError, OSError):
            break
        try:
            operation, args, kwargs = pickle.loads(payload)
            conn.send(("operation_ready",))
        except BaseException as exc:
            try:
                conn.send(("startup_error", type(exc).__name__))
            except BaseException:
                break
            continue

        try:
            command = conn.recv()
            if command != ("run",):
                raise WorkerProcessError("worker operation protocol invalid")
            result = operation(*args, **kwargs)
            try:
                conn.send(("ok", result))
            except BaseException as exc:
                conn.send((
                    "error",
                    type(exc).__name__,
                    "worker result serialization failed",
                ))
        except BaseException as exc:
            try:
                conn.send(("exception", exc))
            except BaseException:
                try:
                    conn.send((
                        "error",
                        type(exc).__name__,
                        "worker exception serialization failed",
                    ))
                except BaseException:
                    break
    conn.close()


def _stop_process(process: mp.Process) -> tuple[bool, bool, int | None]:
    terminated = False
    killed = False
    if process.is_alive():
        process.terminate()
        terminated = True
    process.join(1.0)
    if process.is_alive():
        kill = getattr(process, "kill", None)
        if kill is not None:
            kill()
            killed = True
        process.join(1.0)
    if process.is_alive():
        raise WorkerProcessError("卡死子进程无法终止或回收")
    exitcode = process.exitcode
    process.close()
    return terminated, killed, exitcode


def _attach_cleanup(exc: Exception, *, pid: int | None, terminated: bool,
                    killed: bool, joined: bool, exitcode: int | None) -> None:
    setattr(exc, "worker_pid", pid)
    setattr(exc, "worker_terminated", terminated or killed)
    setattr(exc, "worker_killed", killed)
    setattr(exc, "worker_joined", joined)
    setattr(exc, "worker_exitcode", exitcode)


_WORKER_START_LOCK = threading.Lock()
_ACTIVE_WORKERS = weakref.WeakSet()
_ACTIVE_WORKERS_LOCK = threading.Lock()
_ORIGINAL_PREPARATION_DATA = mp_spawn.get_preparation_data


def _worker_preparation_data(process_name):
    """非冻结 worker 不重跑调用方 main；避免无 main guard 脚本递归。"""
    data = _ORIGINAL_PREPARATION_DATA(process_name)
    if not getattr(sys, "frozen", False):
        data.pop("init_main_from_path", None)
        data.pop("init_main_from_name", None)
    return data


class GuardedWorker:
    """单个任务独占的持久 worker；超时后自动进入关闭状态。"""

    def __init__(self) -> None:
        self._ctx = mp.get_context("spawn")
        self._process: mp.Process | None = None
        self._conn = None
        self.pid: int | None = None
        with _ACTIVE_WORKERS_LOCK:
            _ACTIVE_WORKERS.add(self)

    @property
    def closed(self) -> bool:
        return self._process is None

    def _start(self, cancel_token, startup_timeout_seconds: float) -> None:
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        process = self._ctx.Process(target=_worker_entry, args=(child_conn,))
        try:
            with _WORKER_START_LOCK:
                mp_spawn.get_preparation_data = _worker_preparation_data
                try:
                    process.start()
                finally:
                    mp_spawn.get_preparation_data = _ORIGINAL_PREPARATION_DATA
        except Exception:
            parent_conn.close()
            child_conn.close()
            raise
        child_conn.close()
        self._process = process
        self._conn = parent_conn
        self.pid = process.pid
        pid = self.pid
        deadline = time.monotonic() + max(0.1, startup_timeout_seconds)
        while True:
            if cancel_token is not None and cancel_token.cancelled:
                terminated, killed, exitcode = self.close_with_result()
                reason = getattr(cancel_token, "reason", "user_requested")
                exc = TaskCancelledError("startup", reason)
                _attach_cleanup(
                    exc, pid=pid, terminated=terminated, killed=killed,
                    joined=True, exitcode=exitcode)
                raise exc
            now = time.monotonic()
            if now >= deadline:
                terminated, killed, exitcode = self.close_with_result()
                exc = WorkerProcessError(
                    f"高风险 worker 启动超时：pid={pid}")
                _attach_cleanup(
                    exc, pid=pid, terminated=terminated, killed=killed,
                    joined=True, exitcode=exitcode)
                raise exc
            if parent_conn.poll(min(0.02, deadline - now)):
                message = parent_conn.recv()
                if message[0] == "session_ready":
                    return
                terminated, killed, exitcode = self.close_with_result()
                exc = WorkerProcessError("高风险 worker 启动协议失败")
                _attach_cleanup(
                    exc, pid=pid, terminated=terminated, killed=killed,
                    joined=True, exitcode=exitcode)
                raise exc
            if not process.is_alive():
                self.close()
                raise WorkerProcessError(
                    f"高风险 worker 启动即退出：pid={pid}")

    def run(
        self,
        operation: Callable[..., Any],
        args: tuple = (),
        kwargs: dict[str, Any] | None = None,
        *,
        timeout_seconds: float,
        stage: str,
        timeout_scope: str = "phase",
        timeout_budget: float | None = None,
        cancel_token=None,
        startup_timeout_seconds: float = 10.0,
        on_ready=None,
    ) -> Any:
        kwargs = kwargs or {}
        try:
            payload = pickle.dumps(
                (operation, args, kwargs), protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:
            raise UnpicklableOperationError(
                f"高风险操作无法跨进程传递：{type(operation).__name__}") from exc

        if self._process is None:
            self._start(cancel_token, startup_timeout_seconds)
        conn = self._conn
        process = self._process
        pid = self.pid
        if conn is None or process is None:
            raise WorkerProcessError("高风险 worker 未就绪")

        conn.send_bytes(payload)
        startup_deadline = (
            time.monotonic() + max(0.1, startup_timeout_seconds))
        while True:
            if cancel_token is not None and cancel_token.cancelled:
                terminated, killed, exitcode = self.close_with_result()
                reason = getattr(cancel_token, "reason", "user_requested")
                exc = TaskCancelledError(stage, reason)
                _attach_cleanup(
                    exc, pid=pid, terminated=terminated, killed=killed,
                    joined=True, exitcode=exitcode)
                raise exc
            now = time.monotonic()
            if now >= startup_deadline:
                terminated, killed, exitcode = self.close_with_result()
                exc = WorkerProcessError(
                    f"高风险操作反序列化超时：pid={pid}")
                _attach_cleanup(
                    exc, pid=pid, terminated=terminated, killed=killed,
                    joined=True, exitcode=exitcode)
                raise exc
            if conn.poll(min(0.02, startup_deadline - now)):
                message = conn.recv()
                if message[0] == "operation_ready":
                    break
                if message[0] == "startup_error":
                    self.close()
                    raise UnpicklableOperationError(
                        f"高风险操作反序列化失败：{message[1]}")
                self.close()
                raise WorkerProcessError("高风险 worker 操作协议失败")
            if not process.is_alive():
                self.close()
                raise WorkerProcessError(
                    f"高风险 worker 意外退出：pid={pid}")

        if on_ready is not None:
            on_ready()
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        conn.send(("run",))
        while True:
            if cancel_token is not None and cancel_token.cancelled:
                terminated, killed, exitcode = self.close_with_result()
                reason = getattr(cancel_token, "reason", "user_requested")
                exc = TaskCancelledError(stage, reason)
                _attach_cleanup(
                    exc, pid=pid, terminated=terminated, killed=killed,
                    joined=True, exitcode=exitcode)
                raise exc
            now = time.monotonic()
            if now >= deadline:
                terminated, killed, exitcode = self.close_with_result()
                exc = TaskTimedOutError(
                    stage, timeout_scope,
                    float(timeout_budget if timeout_budget is not None
                          else timeout_seconds))
                _attach_cleanup(
                    exc, pid=pid, terminated=terminated, killed=killed,
                    joined=True, exitcode=exitcode)
                raise exc
            if conn.poll(min(0.02, deadline - now)):
                message = conn.recv()
                break
            if not process.is_alive():
                self.close()
                raise WorkerProcessError(
                    f"高风险 worker 执行中退出：pid={pid}")

        status = message[0]
        if status == "ok":
            return message[1]
        if status == "exception":
            payload = message[1]
            if isinstance(payload, BaseException):
                raise payload
            raise WorkerProcessError("worker 返回了非法异常载荷")
        if status == "error":
            error_type, detail = message[1], message[2]
            raise WorkerProcessError(
                f"worker 执行失败：{error_type};{detail}")
        self.close()
        raise WorkerProcessError(f"未知 worker 返回状态：{status}")

    def close_with_result(self) -> tuple[bool, bool, int | None]:
        process = self._process
        conn = self._conn
        self._process = None
        self._conn = None
        pid = self.pid
        self.pid = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if process is None:
            return False, False, None
        return _stop_process(process)

    def close(self) -> None:
        try:
            self.close_with_result()
        except Exception:
            pass
        with _ACTIVE_WORKERS_LOCK:
            _ACTIVE_WORKERS.discard(self)


def close_all_workers() -> None:
    with _ACTIVE_WORKERS_LOCK:
        workers = list(_ACTIVE_WORKERS)
    for worker in workers:
        worker.close()


atexit.register(close_all_workers)


def run_in_subprocess(
    operation: Callable[..., Any],
    args: tuple = (),
    kwargs: dict[str, Any] | None = None,
    **kwargs_for_run,
) -> Any:
    """兼容一次性调用：创建 worker，执行一个操作后立即回收。"""
    worker = GuardedWorker()
    try:
        return worker.run(operation, args, kwargs, **kwargs_for_run)
    finally:
        worker.close()
