"""多源融合仲裁：合并、置信度校准、三档分流。

对应 module_contracts §2 / ADR-002 / ADR-003 第 5 步。
- 按（类别, 归一化文本）合并多来源证据；多源一致提升置信度。
- 白名单且无高风险来源 → SUPPRESS；黑名单 → AUTO_CONFIRM；公开实体 → NEEDS_REVIEW；
  领域词（内部敏感代号）→ AUTO_CONFIRM。
- 规则与模型冲突：高精度类型以确定性规则为准；上下文类型出现分歧 → 降置信度转复核。
- 校准得到 confidence（raw_confidence 保留原始值），按策略输出三档 EntityDecision。
"""
from __future__ import annotations

from ..domain.entities import Candidate
from ..domain.enums import EntityDecision, ItemStatus, RiskLevel
from ..domain.policy import ReviewPolicy, risk_for
from .context import ContextAnalyzer
from .knowledge import KnowledgeBase

# 高精度、格式确定、可校验类别：以确定性规则为准，不被模型来源推翻
_HIGH_PRECISION = {"身份证号", "银行卡号", "手机号", "邮箱", "固定电话", "护照号"}
_RELIABLE_SOURCES = {"regex", "dict", "blacklist", "domain"}
# 仅模型来源（本地 LLM / 本地 NER）：低权重、非可靠来源；与 ollama 同口径。
# 仅这类来源的上下文类实体转 NEEDS_REVIEW，不参与"是否残留/可否导出"最终判定。
_MODEL_ONLY_SOURCES = {"ollama", "ner"}


class FusionEngine:
    def __init__(self, policy: ReviewPolicy, knowledge: KnowledgeBase,
                 context_analyzer: ContextAnalyzer | None = None):
        self.policy = policy
        self.knowledge = knowledge
        self.context = context_analyzer or ContextAnalyzer()

    # ---------- 主流程 ----------
    def fuse(self, candidates: list[Candidate], text: str) -> list[Candidate]:
        groups: dict[tuple[str, str], list[Candidate]] = {}
        for c in candidates:
            key = (c.category, c.text.strip())
            groups.setdefault(key, []).append(c)

        out: list[Candidate] = []
        for (cat, norm), group in groups.items():
            merged = self._merge_one(cat, norm, group, text)
            out.append(merged)
        # 按长度降序，与既有替换顺序一致
        out.sort(key=lambda c: len(c.text), reverse=True)
        return out

    # ---------- 单组合并 ----------
    def _merge_one(self, category: str, norm: str,
                   group: list[Candidate], text: str) -> Candidate:
        base = group[0]
        # 保留首个候选的位置/box/page
        sources = {c.source for c in group}
        raw_conf = max((c.raw_confidence or c.confidence or 0.0) for c in group)

        # 上下文跨度
        span = self._find_span(text, norm)
        feats = self.context.analyze(text, span, category) if span else None
        sensitive = [s for s in (feats.context_signals if feats else [])
                     if s.startswith("sensitive:")]

        # 词库命中
        hits = self.knowledge.classify(norm)

        # ---- 置信度校准 ----
        reliable_hits = len({s for s in sources if s in _RELIABLE_SOURCES})
        calibrated = raw_conf
        if reliable_hits >= 2:
            calibrated += 0.05  # 多源一致：规则/名单互相印证
        # 仅模型来源的上下文类实体（ollama/ner，无可靠来源印证）：降权
        if sources and sources <= _MODEL_ONLY_SOURCES and category not in _HIGH_PRECISION:
            calibrated -= 0.15
        calibrated = max(0.0, min(0.99, calibrated))

        ctx = {"sensitive_signals": bool(sensitive)}
        risk = risk_for(category, calibrated, ctx)

        # ---- 三档判定（按优先级短路）----
        decision, reason = self._decide(category, sources, hits, feats, calibrated)

        # 写回
        base.raw_confidence = raw_conf
        base.confidence = calibrated
        base.risk_level = risk
        base.decision = decision
        base.decision_reason = reason
        base.source = "+".join(sorted(sources)) if len(sources) > 1 else (
            (next(iter(sources)) if sources else base.source))

        if decision == EntityDecision.AUTO_CONFIRM:
            base.confirmed = True
            base.excluded = False
        elif decision == EntityDecision.SUPPRESS:
            base.confirmed = False
            base.excluded = True
        else:  # NEEDS_REVIEW
            base.confirmed = False
            base.excluded = False
        return base

    def _decide(self, category: str, sources: set[str], hits: set[str],
                feats, calibrated: float) -> tuple[EntityDecision, str]:
        # 1) 黑名单：内部敏感实体，直接确认
        if "blacklist" in hits:
            return EntityDecision.AUTO_CONFIRM, "黑名单命中的内部敏感实体"

        # 2) 白名单且无高风险来源：抑制
        if "whitelist" in hits and "blacklist" not in hits and category not in _HIGH_PRECISION:
            return EntityDecision.SUPPRESS, "白名单通用业务表述"

        # 3) 领域词（内部敏感代号）：确认
        if "domain" in hits:
            return EntityDecision.AUTO_CONFIRM, "领域词库命中的内部敏感代号"

        # 4) 泛指引用（我们公司/有限公司/集团公司/银行医院大学…）：抑制
        if feats is not None and feats.is_generic_reference:
            return EntityDecision.SUPPRESS, "组织后缀剥离后为泛指引用（剩余专名<2字）"

        # 5) 公开实体：一般语境转复核（既不一律放行也不一律脱敏）
        if "public" in hits:
            return EntityDecision.NEEDS_REVIEW, "公开企业/机构名，需人工裁决是否脱敏"

        # 6) 高精度格式类型：确定性规则为准
        if category in _HIGH_PRECISION:
            if calibrated >= self.policy.auto_confirm_threshold:
                return EntityDecision.AUTO_CONFIRM, f"{category}通过格式/校验，高置信度规则命中"
            return EntityDecision.NEEDS_REVIEW, f"{category}置信度中等，需复核"

        # 7) 规则与模型冲突：仅模型来源（ollama/ner）→ 降置信度转复核
        if sources and sources <= _MODEL_ONLY_SOURCES:
            return EntityDecision.NEEDS_REVIEW, "仅本地模型来源（LLM/NER），低权重候选需复核"

        # 8) 阈值兜底
        d = self.policy.decide(category, sources, calibrated, hits)
        if d == EntityDecision.AUTO_CONFIRM:
            return d, f"{category}校准置信度{calibrated:.2f}达自动确认阈值"
        if d == EntityDecision.SUPPRESS:
            return d, f"{category}校准置信度{calibrated:.2f}低于复核阈值"
        return d, f"{category}上下文相关实体，校准置信度{calibrated:.2f}需复核"

    # ---------- 工具 ----------
    @staticmethod
    def _find_span(text: str, norm: str) -> tuple[int, int] | None:
        if not text or not norm:
            return None
        i = text.find(norm)
        if i < 0:
            return None
        return (i, i + len(norm))
