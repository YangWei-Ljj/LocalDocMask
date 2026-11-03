"""路径越权防护（path containment）：所有落盘路径必须收敛在受控根目录内。

背景：输出/临时/隔离/映射文件的文件名可能来自用户文件名、任务对象或
外部输入；若直接 ``root / untrusted_name``，``..`` 穿越、绝对路径或符号链接
都可能把产物写到数据目录之外（覆盖系统/用户文件）。本模块提供唯一收口：

- ``resolve_within(root, untrusted_path)``：把任意不可信路径解析到 root 内，
  逃逸即抛 ``SecurityViolation``（失败关闭，绝不放行）；
- ``safe_join(root, *parts)``：逐段拼接并拒绝含 ``..``/绝对路径的段；
- ``is_within(path, roots)``：只读判定，不抛异常。

实现要点：``Path.resolve()`` 同时规范大小写与符号链接，因此“解析后仍在
root 之内”是最强判据；Windows 大小写不敏感，比较前统一 normcase。
"""
from __future__ import annotations

import os
from pathlib import Path

from ..domain.exceptions import SecurityViolation


def _norm(path: Path) -> Path:
    return Path(os.path.normcase(os.path.realpath(str(path))))


def is_within(path: str | Path, roots) -> bool:
    """只读判定：path（解析后）是否落在 roots 中任一根目录内。"""
    roots = list(roots or [])
    if not roots:
        return False
    resolved = _norm(Path(path).resolve())
    for root in roots:
        root_res = _norm(Path(root).resolve())
        if resolved == root_res or root_res in resolved.parents:
            return True
    return False


def resolve_within(root: str | Path, untrusted_path: str | Path) -> Path:
    """把 untrusted_path 解析为 root 内的绝对路径；逃逸抛 SecurityViolation。

    - untrusted_path 为相对路径时按 root 解析；为绝对路径时直接使用；
    - 解析后（含符号链接解析）不在 root 之内 => 失败关闭。
    """
    root_res = Path(root).resolve()
    p = Path(untrusted_path)
    candidate = p if p.is_absolute() else root_res / p
    resolved = candidate.resolve()
    if not is_within(resolved, [root_res]):
        raise SecurityViolation(
            f"路径越权：{untrusted_path!r} 解析后逃逸出受控目录 {root_res}")
    return resolved


def safe_join(root: str | Path, *parts: str | Path) -> Path:
    """逐段拼接不可信文件名到 root 内。

    任一段是绝对路径、或含 ``..`` 父级穿越段，直接抛 ``SecurityViolation``；
    最终再做一次 resolve 判定（兜住符号链接逃逸）。
    """
    root_res = Path(root).resolve()
    p = root_res
    for part in parts:
        seg = Path(part)
        if seg.is_absolute():
            raise SecurityViolation(
                f"路径越权：拒绝绝对路径段 {part!r}（必须落在 {root_res} 内）")
        if any(comp == ".." for comp in seg.parts):
            raise SecurityViolation(
                f"路径越权：拒绝包含 '..' 的路径段 {part!r}")
        p = p / seg
    return resolve_within(root_res, p)
