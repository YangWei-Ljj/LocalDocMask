"""知识词库：白名单 / 黑名单 / 公开实体 / 领域术语。

对应 module_contracts §2 / ADR-003 第 3 步。从 resources/*.txt 加载，
给出强先验，供融合仲裁使用。
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

_FILES = {
    "whitelist": "whitelist.txt",
    "blacklist": "blacklist.txt",
    "public": "public_entities.txt",
    "domain": "domain_terms.txt",
}


def _load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


@dataclass
class KnowledgeBase:
    whitelist: set[str] = field(default_factory=set)
    blacklist: set[str] = field(default_factory=set)
    public_entities: set[str] = field(default_factory=set)
    domain_terms: set[str] = field(default_factory=set)
    # 带通配符的词（以 * 或 ? 出现）单独存为模式
    _wild: dict[str, list[str]] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, resources_dir: str | Path) -> "KnowledgeBase":
        d = Path(resources_dir)
        kb = cls()
        buckets = {
            "whitelist": kb.whitelist,
            "blacklist": kb.blacklist,
            "public": kb.public_entities,
            "domain": kb.domain_terms,
        }
        for label, fname in _FILES.items():
            for line in _load_lines(d / fname):
                if "*" in line or "?" in line:
                    kb._wild.setdefault(label, []).append(line)
                else:
                    buckets[label].add(line)
        return kb

    @staticmethod
    def _hit(text: str, exact: set[str], patterns: list[str]) -> bool:
        if text in exact:
            return True
        # 子串包含：白名单短语（如"本公司产品"）或短词命中
        for term in exact:
            if term and term in text:
                return True
        for pat in patterns:
            if fnmatch.fnmatch(text, pat) or fnmatch.fnmatchcase(text, pat):
                return True
        return False

    def classify(self, text: str) -> set[str]:
        """返回命中的词库标签子集：whitelist/blacklist/public/domain。"""
        hits: set[str] = set()
        if self._hit(text, self.blacklist, self._wild.get("blacklist", [])):
            hits.add("blacklist")
        if self._hit(text, self.public_entities, self._wild.get("public", [])):
            hits.add("public")
        if self._hit(text, self.domain_terms, self._wild.get("domain", [])):
            hits.add("domain")
        if self._hit(text, self.whitelist, self._wild.get("whitelist", [])):
            hits.add("whitelist")
        return hits
