"""敏感信息检测器：正则规则 + 自定义词库 + 实体名单。

保持 Detector 类与既有行为（detect_text 返回按长度降序的候选列表）。
按类别设置合理的原始置信度（ADR-003）：
校验通过的身份证/银行卡高；手机/邮箱/固定电话/护照较高；地址/编号中等；
公司名正则为上下文相关、偏低，交由上下文判定与融合仲裁精过滤。
"""
from __future__ import annotations

from . import patterns
from ..domain.entities import Candidate

# (类别, 正则, 校验器, 原始置信度)
_RULES = [
    ("身份证号", patterns.ID_CARD_RE,
     lambda s: patterns.is_valid_id(s) and patterns.is_valid_birth(s), 0.99),
    ("手机号", patterns.MOBILE_RE, None, 0.95),
    ("固定电话", patterns.LANDLINE_RE, None, 0.92),
    ("邮箱", patterns.EMAIL_RE, None, 0.95),
    ("护照号", patterns.PASSPORT_RE, None, 0.90),
    ("银行卡号", patterns.BANK_CARD_RE, patterns.luhn_ok, 0.97),
    ("详细地址", patterns.ADDRESS_RE, None, 0.70),
    ("编号", patterns.ID_NO_RE, None, 0.65),
]

# 公司名正则命中：上下文相关，原始置信度偏低，交由上下文/词库/融合裁决
_COMPANY_REGEX_CONF = 0.60
# 词表（dict）命中：人工维护名单，置信度较高但仍按类别走策略
_DICT_CONF = 0.90


class Detector:
    def __init__(self, custom_words=None, companies=None, persons=None,
                 enable_company_regex: bool = True):
        self.custom_words = [w for w in (custom_words or []) if w and w.strip()]
        self.companies = [w for w in (companies or []) if w and w.strip()]
        self.persons = [w for w in (persons or []) if w and w.strip()]
        self.enable_company_regex = enable_company_regex

    def detect_text(self, text: str, location: str = "") -> list[Candidate]:
        if not text:
            return []
        cands: list[Candidate] = []
        seen: set[tuple[str, str]] = set()

        def add(cat: str, val: str, conf: float, src: str):
            key = (cat, val)
            if key in seen:
                return
            seen.add(key)
            cands.append(Candidate(text=val, category=cat, location=location,
                                   confidence=conf, raw_confidence=conf, source=src))

        for cat, regex, validator, conf in _RULES:
            for m in regex.finditer(text):
                val = m.group(0)
                if validator is not None and not validator(val):
                    continue
                add(cat, val, conf, "regex")

        if self.enable_company_regex:
            for m in patterns.COMPANY_RE.finditer(text):
                add("公司名", patterns.trim_company_prefix(m.group(0)),
                    _COMPANY_REGEX_CONF, "regex")

        for word in self.companies:
            self._find_word(text, word, "公司名", location, add)
        for word in self.persons:
            self._find_word(text, word, "姓名", location, add)
        for word in self.custom_words:
            self._find_word(text, word, "自定义敏感词", location, add)

        # 按长度降序：替换时先处理更长的实体，避免短词破坏长词
        return sorted(cands, key=lambda c: len(c.text), reverse=True)

    def _find_word(self, text, word, cat, location, add):
        start = 0
        while True:
            i = text.find(word, start)
            if i < 0:
                break
            add(cat, word, _DICT_CONF, "dict")
            start = i + len(word)
