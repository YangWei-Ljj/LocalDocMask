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
    """脱敏替换器。

    mode=a：所有类别统一返回掩码。
    mode=b：实体类别（公司/姓名）返回一致性虚构值。若注入了一个共享的
    ``mapper``（如批次级 ``ConsistencyMapper``），实体替换委托给它，从而
    跨文件保持一致；否则退化为实例自含映射（等同旧行为，映射表仅内存）。
    """

    def __init__(self, mode: str = "a", mask: str = "****", mapper=None):
        self.mode = mode.lower()
        self.mask = mask
        self._mapper = mapper
        self._mapping: dict[tuple[str, str], str] = {}
        self._ci = 0
        self._pi = 0

    def is_entity(self, category: str) -> bool:
        return category in _ENTITY_CATEGORIES

    def replacement(self, category: str, original: str) -> str:
        if self.mode == "b" and self.is_entity(category):
            if self._mapper is not None:
                mapped = self._mapper.replacement(category, original)
                # mapper 仅负责实体类别；返回空串视为未接管，兜底掩码
                if mapped:
                    return mapped
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


def mask_display(value: str, category: str = "") -> str:
    """用于界面/日志/报告的安全显示，不泄露完整敏感值。"""
    if not value:
        return ""
    if category == "身份证号" and len(value) >= 8:
        return value[:4] + "*" * (len(value) - 8) + value[-4:]
    if category == "手机号" and len(value) >= 7:
        return value[:3] + "****" + value[-4:]
    if category in ("银行卡号", "邮箱") and len(value) >= 5:
        return value[:2] + "***" + value[-2:]
    if category in ("姓名", "公司名", "企业名", "人名") and len(value) >= 2:
        return value[0] + "*" * (len(value) - 1)
    if len(value) >= 4:
        return value[:2] + "*" * (len(value) - 4) + value[-2:]
    return "*" * len(value)
