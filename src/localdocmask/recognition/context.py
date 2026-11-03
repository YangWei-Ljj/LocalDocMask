"""上下文判定：泛指引用识别 + 上下文信号提取。

对应 module_contracts §2 / data_flow §4 / ADR-003 第 2 步。
规则命中公司名等上下文相关实体时先保召回，再用上下文窗口做精过滤：
去掉组织后缀后的"核心"若为空、仅由泛指词或组织类词构成（剩余专名 < 2 字），
判为泛指引用（我们公司 / 有限公司 / 集团公司 / 银行医院大学…）→ SUPPRESS。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 组织后缀（从长到短，依次剥离）
ORG_SUFFIXES = (
    "股份有限公司", "有限责任公司", "集团有限公司", "集团股份有限公司",
    "有限公司", "公司", "集团", "股份", "银行", "医院", "大学",
    "研究院", "研究所", "事务所", "学院", "学校", "中心", "分社",
)

# 泛指前缀/代词
GENERIC_WORDS = (
    "我们", "咱们", "你们", "他们", "我司", "本公司", "贵司", "该公司",
    "我", "你", "他", "本", "该", "贵", "此", "这", "那", "某", "总", "分",
    "子", "母", "其他", "所有", "各", "相关", "有限", "股份", "集团", "公司",
)

# 组织类通名（剥离后缀后若仅剩这些词，视为非专名）
ORG_CLASS_WORDS = (
    "银行", "医院", "大学", "学院", "学校", "研究院", "研究所",
    "事务所", "公司", "企业", "机构", "中心", "集团", "分社", "报社",
)

# 上下文信号：中性/公开 vs 内部/敏感
_NEUTRAL_SIGNALS = ("发布公告", "官网", "新闻", "上市", "年报", "招股", "招标公告",
                    "简介", "官网显示", "官方")
_SENSITIVE_SIGNALS = ("联系", "合作", "供应商", "员工", "合同", "客户", "甲方",
                      "乙方", "经办", "经办", "签约", "入职", "离职", "薪资",
                      "报销", "客户名单", "通讯录", "往来", "付款", "发票")


@dataclass
class ContextFeatures:
    is_generic_reference: bool = False
    preceding: str = ""
    proper_chars: int = 0
    context_signals: list[str] = field(default_factory=list)


def _strip_suffixes(text: str) -> str:
    core = text.strip()
    changed = True
    while changed and core:
        changed = False
        for suf in ORG_SUFFIXES:
            if core.endswith(suf) and len(core) > len(suf):
                core = core[: -len(suf)]
                changed = True
                break
    return core.strip()


def _proper_char_count(core: str) -> int:
    """剥离泛指词与组织通名后，剩余专名字符数。"""
    rest = core
    # 按长度从长到短去除泛指/组织类词，重复直到不再变化
    tokens = sorted(set(GENERIC_WORDS) | set(ORG_CLASS_WORDS), key=len, reverse=True)
    changed = True
    while changed and rest:
        changed = False
        for tok in tokens:
            if tok and tok in rest:
                rest = rest.replace(tok, "")
                changed = True
    # 统计剩余 CJK / 字母数字字符
    return sum(1 for ch in rest if ch.strip())


class ContextAnalyzer:
    """对单个候选跨度做上下文判定。"""

    WINDOW = 20  # 前后各取约 12-20 字

    def analyze(self, text: str, match_span: tuple[int, int], category: str = "") -> ContextFeatures:
        start, end = match_span
        span_text = text[start:end]
        win_start = max(0, start - self.WINDOW)
        win_end = min(len(text), end + self.WINDOW)
        preceding = text[win_start:start]
        following = text[end:win_end]
        window = preceding + following

        core = _strip_suffixes(span_text)
        proper = _proper_char_count(core)
        generic = proper < 2

        signals: list[str] = []
        for sig in _NEUTRAL_SIGNALS:
            if sig in window:
                signals.append("neutral:" + sig)
        for sig in _SENSITIVE_SIGNALS:
            if sig in window:
                signals.append("sensitive:" + sig)

        return ContextFeatures(
            is_generic_reference=generic,
            preceding=preceding,
            proper_chars=proper,
            context_signals=signals,
        )
