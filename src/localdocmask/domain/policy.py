"""审查策略与风险定级（纯逻辑，无外部依赖）。

对应 module_contracts §1.4：
- ReviewPolicy：三档分流阈值与 decide()。
- risk_for()：按类别/置信度/上下文给出 RiskLevel。

设计原则（ADR-002）：候选不再一律确认；高精度规则/黑名单自动确认，
上下文相关/中等置信度/冲突转人工复核，泛指/白名单/低置信度抑制。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .enums import EntityDecision, RiskLevel

# 高精度、格式确定、可校验的类别：可自动确认
_HIGH_PRECISION_CATEGORIES = {
    "身份证号", "银行卡号", "手机号", "邮箱", "固定电话", "护照号",
}
# 上下文相关类别：即便置信度高也需人工复核，避免误脱敏真实企业/人名
_CONTEXTUAL_CATEGORIES = {"公司名", "姓名", "详细地址", "编号"}


@dataclass
class ReviewPolicy:
    review_mode: str = "interactive"          # interactive / auto_safe
    auto_confirm_threshold: float = 0.90      # 校准置信度 ≥ 此值且无歧义 → AUTO_CONFIRM
    review_threshold: float = 0.50             # ≥ 此值 → NEEDS_REVIEW；否则 SUPPRESS

    def decide(self, category: str, sources: set[str], confidence: float,
               knowledge_hits: set[str]) -> EntityDecision:
        """按 类别 + 来源 + 校准置信度 + 词库命中 输出三档处置。

        knowledge_hits 取值：whitelist / blacklist / public / domain。
        （黑名单/白名单/公开/领域词库的强先验在 fusion 中优先短路，本方法只做
        阈值与类别层面的兜底判定。）
        """
        if confidence < self.review_threshold:
            return EntityDecision.SUPPRESS
        if confidence >= self.auto_confirm_threshold:
            # 上下文相关类别即使置信度高，也默认转复核，避免误脱敏真实企业名
            if category in _CONTEXTUAL_CATEGORIES:
                return EntityDecision.NEEDS_REVIEW
            return EntityDecision.AUTO_CONFIRM
        return EntityDecision.NEEDS_REVIEW


def risk_for(category: str, confidence: float = 0.0, context: dict | None = None) -> RiskLevel:
    """按类别与上下文给出风险等级（纯逻辑）。"""
    context = context or {}
    sensitive_signals = bool(context.get("sensitive_signals"))
    if category in {"身份证号", "银行卡号", "护照号"}:
        return RiskLevel.CRITICAL
    if category in {"手机号", "邮箱", "固定电话", "详细地址"}:
        return RiskLevel.HIGH
    if category == "自定义敏感词":
        return RiskLevel.HIGH
    if category in {"公司名", "姓名", "编号"}:
        # 内部/敏感语境（合同、供应商、员工、客户）下的企业/人名风险上调
        return RiskLevel.HIGH if sensitive_signals else RiskLevel.MEDIUM
    if confidence < 0.5:
        return RiskLevel.LOW
    return RiskLevel.MEDIUM
