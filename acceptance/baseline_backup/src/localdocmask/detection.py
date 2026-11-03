"""敏感信息检测器：正则规则 + 自定义词库 + 实体名单。"""
from __future__ import annotations

from . import patterns
from .models import Candidate

_RULES = [
    ("身份证号", patterns.ID_CARD_RE,
     lambda s: patterns.is_valid_id(s) and patterns.is_valid_birth(s)),
    ("手机号", patterns.MOBILE_RE, None),
    ("固定电话", patterns.LANDLINE_RE, None),
    ("邮箱", patterns.EMAIL_RE, None),
    ("护照号", patterns.PASSPORT_RE, None),
    ("银行卡号", patterns.BANK_CARD_RE, patterns.luhn_ok),
    ("详细地址", patterns.ADDRESS_RE, None),
    ("编号", patterns.ID_NO_RE, None),
]


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
                                   confidence=conf, source=src))

        for cat, regex, validator in _RULES:
            for m in regex.finditer(text):
                val = m.group(0)
                if validator is not None and not validator(val):
                    continue
                add(cat, val, 1.0, "regex")

        if self.enable_company_regex:
            for m in patterns.COMPANY_RE.finditer(text):
                add("公司名", m.group(0), 0.85, "regex")

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
            add(cat, word, 1.0, "dict")
            start = i + len(word)
