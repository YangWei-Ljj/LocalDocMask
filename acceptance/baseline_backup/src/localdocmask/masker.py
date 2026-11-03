"""脱敏替换逻辑：模式 A 统一掩码 / 模式 B 一致性虚构替换。"""
from __future__ import annotations

_COMPANIES = [
    "云帆科技有限责任公司", "山海实业股份有限公司", "星野贸易有限公司",
    "北辰信息技术有限公司", "青禾供应链有限公司", "望舒咨询服务有限公司",
    "沧澜文化传媒有限公司", "青柠食品有限公司", "竹石物流有限公司",
    "萤火数字科技有限公司",
]
_SURNAMES = ["赵", "钱", "孙", "李", "周", "吴", "郑", "王", "冯", "陈",
             "沈", "韩", "杨", "朱", "秦", "许", "何", "吕", "施", "张"]
_GIVEN = ["晨", "宇", "欣", "雨", "浩", "婷", "凯", "雪", "明", "悦",
          "磊", "静", "杰", "慧", "博", "雯", "强", "瑶", "峰", "琳"]

_ENTITY_CATEGORIES = {"公司名", "企业名", "姓名", "人名"}


class Masker:
    """同一任务内保持一致的映射；映射表仅存内存，绝不落盘。"""

    def __init__(self, mode: str = "a", mask: str = "****"):
        self.mode = mode.lower()
        self.mask = mask
        self._mapping: dict[tuple[str, str], str] = {}
        self._ci = 0
        self._pi = 0

    def is_entity(self, category: str) -> bool:
        return category in _ENTITY_CATEGORIES

    def replacement(self, category: str, original: str) -> str:
        if self.mode == "b" and self.is_entity(category):
            key = ("c" if category in ("公司名", "企业名") else "p", original)
            if key not in self._mapping:
                if key[0] == "c":
                    self._mapping[key] = _COMPANIES[self._ci % len(_COMPANIES)]
                    self._ci += 1
                else:
                    surname = _SURNAMES[self._pi % len(_SURNAMES)]
                    given = _GIVEN[(self._pi // len(_SURNAMES)) % len(_GIVEN)]
                    given2 = _GIVEN[(self._pi * 7 + 3) % len(_GIVEN)]
                    self._mapping[key] = surname + given + given2
                    self._pi += 1
            return self._mapping[key]
        return self.mask
