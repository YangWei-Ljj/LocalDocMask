"""原子文件写入与未确认产物隔离（T4）。"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from ..domain.exceptions import ErrorCode, LocalDocMaskError
from .hashing import sha256_file
from .path_safety import safe_join


class AtomicExportError(LocalDocMaskError):
    code = ErrorCode.PERSISTENCE_ERROR


def atomic_copy(source: str | Path, dest_dir: str | Path,
                final_name: str, expected_sha256: str = "") -> Path:
    """复制到同目录临时文件 → fsync/hash 校验 → os.replace 原子落位。

    目标已存在时拒绝覆盖（文件名应包含任务 ID）。
    """
    src = Path(source)
    root = Path(dest_dir)
    root.mkdir(parents=True, exist_ok=True)
    final = safe_join(root, final_name)
    if final.exists():
        raise AtomicExportError(f"目标文件已存在，拒绝覆盖：{final.name}")
    tmp = safe_join(root, f".{final_name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(src, "rb") as rf, open(tmp, "xb") as wf:
            while True:
                chunk = rf.read(1 << 20)
                if not chunk:
                    break
                wf.write(chunk)
            wf.flush()
            os.fsync(wf.fileno())
        actual = sha256_file(tmp)
        if expected_sha256 and actual != expected_sha256:
            raise AtomicExportError("临时文件 SHA-256 与 final_hash 不一致")
        os.replace(tmp, final)
        return final
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        raise


def atomic_write_bytes(dest_dir: str | Path, final_name: str, data: bytes,
                       expected_sha256: str = "") -> Path:
    root = Path(dest_dir)
    root.mkdir(parents=True, exist_ok=True)
    final = safe_join(root, final_name)
    if final.exists():
        raise AtomicExportError(f"目标文件已存在，拒绝覆盖：{final.name}")
    tmp = safe_join(root, f".{final_name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "xb") as wf:
            wf.write(data)
            wf.flush()
            os.fsync(wf.fileno())
        actual = sha256_file(tmp)
        if expected_sha256 and actual != expected_sha256:
            raise AtomicExportError("临时文件 SHA-256 不匹配")
        os.replace(tmp, final)
        return final
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        raise


def quarantine_unconfirmed(path: str | Path, quarantine_dir: str | Path,
                           suffix: str = "unconfirmed") -> Path | None:
    """把未确认产物移出 success；失败时删除它，绝不留成功目录。"""
    src = Path(path)
    if not src.exists():
        return None
    qroot = Path(quarantine_dir)
    qroot.mkdir(parents=True, exist_ok=True)
    dest = safe_join(qroot, f"{src.stem}.{suffix}.{uuid.uuid4().hex[:8]}{src.suffix}")
    try:
        shutil.move(str(src), str(dest))
        return dest
    except Exception as move_exc:
        try:
            src.unlink()
        except Exception as delete_exc:
            raise AtomicExportError(
                f"无法隔离或删除未确认产物：{src.name}"
            ) from delete_exc
        return None


def atomic_copy_user_file(source: str | Path, destination: str | Path,
                          expected_sha256: str = "",
                          *, overwrite: bool = False) -> Path:
    """原子导出到用户选择位置；默认拒绝覆盖已有文件。"""
    src = Path(source)
    final = Path(destination)
    parent = final.parent
    if final.exists() and final.is_dir():
        raise AtomicExportError("目标路径是目录，拒绝导出")
    if final.exists() and not overwrite:
        raise AtomicExportError(f"目标文件已存在，拒绝覆盖：{final.name}")
    if not parent.exists() or not parent.is_dir():
        raise AtomicExportError("目标目录不存在")
    tmp = parent / f".{final.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(src, "rb") as rf, open(tmp, "xb") as wf:
            while True:
                chunk = rf.read(1 << 20)
                if not chunk:
                    break
                wf.write(chunk)
            wf.flush()
            os.fsync(wf.fileno())
        actual = sha256_file(tmp)
        if expected_sha256 and actual != expected_sha256:
            raise AtomicExportError("导出临时文件 SHA-256 与 final_hash 不一致")
        if final.exists() and not overwrite:
            raise AtomicExportError(f"目标文件已存在，拒绝覆盖：{final.name}")
        os.replace(tmp, final)
        return final
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        raise
