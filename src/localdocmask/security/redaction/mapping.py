"""模式 B 一致性虚构映射器（ADR-004）。

职责：
- 为公司名/企业名/姓名/人名等实体类提供稳定、内存内一致的虚构替换值；
- 明确作用域（batch/task/global）、冲突处理（占用集合 + 确定性扩展，
  绝不静默复用）和安全表示；
- 非实体类别不由本类处理，由 Masker 兜底返回掩码。

安全约束：
- 当前版本只支持 persistence="memory"，进程结束即失效；
- 禁止以明文 JSON 保存“真实值→虚构值”映射；
- disk/protected_file 明文映射已禁用，必须经单独批准的 DPAPI 方案才能恢复；
- 映射表详情严禁写入日志、报告或诊断包。
"""
from __future__ import annotations

from pathlib import Path

from ...domain.exceptions import ConfigError, MappingCollisionError
from .masker import _COMPANIES, _GIVEN, _SURNAMES

_ENTITY_CATEGORIES = {"公司名", "企业名", "姓名", "人名"}
_COMPANY_KINDS = {"公司名", "企业名"}
_VALID_SCOPES = {"batch", "task", "global"}
_VALID_PERSISTENCE = {"memory"}
_PERSISTENCE_DISABLED_MESSAGE = (
    "mapping_persistence 当前仅支持 memory；disk/protected_file "
    "明文映射已禁用，需单独批准 DPAPI 加密方案后才能恢复"
)

# 分配安全阀：正常情况下确定性候选序列无限可枚举；仅在候选发生器被外部
# 改写（或极端病态输入）导致反复撞库时才触发，防止死循环。
_MAX_ALLOC_ATTEMPTS = 1_000_000


class ConsistencyMapper:
    """模式 B 一致性虚构映射器（当前仅内存持久化）。

    Parameters
    ----------
    scope:
        - ``batch``（默认）：一个批次共享同一实例，保证该批次内跨文件一致；
        - ``task``：仅单文件内一致；
        - ``global``：跨批次/跨会话一致，需要持久化；当前版本明确不可用。
    persistence:
        仅支持 ``memory``。``disk``、``protected_file`` 及未知值均拒绝。
    store_dir:
        仅为旧调用兼容保留；当前版本完全忽略，不创建目录或文件。
    """

    def __init__(self, scope: str = "batch", persistence: str = "memory",
                 store_dir: Path | None = None) -> None:
        if scope not in _VALID_SCOPES:
            raise ConfigError(f"未知映射作用域：{scope}（可选：{sorted(_VALID_SCOPES)}）")
        if persistence not in _VALID_PERSISTENCE:
            raise ConfigError(_PERSISTENCE_DISABLED_MESSAGE)
        if scope == "global":
            raise ConfigError(
                "scope=global 需要持久化；当前版本仅支持 memory，"
                "明文磁盘映射已禁用，需单独批准 DPAPI 加密方案"
            )
        self.scope = scope
        self.persistence = persistence

        self._mapping: dict[str, str] = {}   # "c\x1f真实公司" -> 虚构值
        self._used: set[str] = set()          # 已占用的虚构值（冲突检测）
        self._counters: dict[str, int] = {"c": 0, "p": 0}

    # ------------------------------------------------------------------
    # MapperPort
    # ------------------------------------------------------------------
    def is_entity(self, category: str) -> bool:
        return category in _ENTITY_CATEGORIES

    def replacement(self, category: str, original: str) -> str:
        """实体类返回稳定虚构值；非实体类别由调用方兜底掩码。"""
        if not self.is_entity(category):
            return ""
        kind = "c" if category in _COMPANY_KINDS else "p"
        key = kind + "\x1f" + original
        cached = self._mapping.get(key)
        if cached is not None:
            return cached
        value = self._allocate(kind)
        self._mapping[key] = value
        return value

    # ------------------------------------------------------------------
    # 分配与冲突处理
    # ------------------------------------------------------------------
    def _allocate(self, kind: str) -> str:
        """确定性顺序分配；撞已占用值则取下一个未占用值，绝不静默复用。"""
        idx = self._counters[kind]
        tried = 0
        while True:
            candidate = self._candidate(kind, idx)
            idx += 1
            tried += 1
            if candidate not in self._used:
                self._used.add(candidate)
                self._counters[kind] = idx
                return candidate
            if tried >= _MAX_ALLOC_ATTEMPTS:
                raise MappingCollisionError(
                    f"虚构值池耗尽，无法为 {kind} 类实体分配唯一值（共尝试 {tried} 次）")

    @staticmethod
    def _candidate(kind: str, n: int) -> str:
        """第 n 个确定性候选虚构值（注入式，永不重复使用）。"""
        if kind == "c":
            base = _COMPANIES[n % len(_COMPANIES)]
            block = n // len(_COMPANIES)
            if block == 0:
                return base
            return f"{base}（{block}）"
        ns = len(_SURNAMES)
        ng = len(_GIVEN)
        surname = _SURNAMES[n % ns]
        given1 = _GIVEN[(n // ns) % ng]
        given2 = _GIVEN[(n // (ns * ng)) % ng]
        beyond = n // (ns * ng * ng)
        name = surname + given1 + given2
        if beyond:
            name = f"{name}{beyond}"
        return name

    # ------------------------------------------------------------------
    # 保护：调试表示不得泄露真实映射
    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._mapping)

    def __repr__(self) -> str:  # pragma: no cover - 安全表示
        return (f"ConsistencyMapper(scope={self.scope!r}, persistence={self.persistence!r}, "
                f"entries={len(self._mapping)})")
