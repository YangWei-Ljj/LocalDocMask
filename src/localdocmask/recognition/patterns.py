"""内置敏感信息正则规则与校验器。"""
import re
from datetime import date

MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LANDLINE_RE = re.compile(r"(?<!\d)(?:0\d{2,3}-\d{7,8})(?!\d)")
ID_CARD_RE = re.compile(
    r"(?<!\d)([1-9]\d{5})(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?!\d)"
)
PASSPORT_RE = re.compile(r"(?<![A-Z0-9])(?:[EG]\d{8}|[A-Z]{2}\d{6,8})(?![A-Z0-9])")
BANK_CARD_RE = re.compile(r"(?<!\d)\d{13,19}(?!\d)")
ADDRESS_RE = re.compile(
    r"[\u4e00-\u9fa5]{2,8}(?:省|自治区|市|区|县)[\u4e00-\u9fa5]{2,20}(?:路|街|道|号|栋|室|村|镇)"
)
ID_NO_RE = re.compile(
    r"(?:客户|员工|供应商|合同|项目|订单|工单|编号)(?:编号|号)?[\s:：]?[A-Za-z0-9_-]{3,24}"
)
#: 负向先行：仅排除明显的分句/虚词起点（与/和/及等连接字可能同时是真实字号
#: 首字，如“和泰科技”，故不排除，交由 trim_company_prefix 做左边界裁剪）。
COMPANY_RE = re.compile(
    r"(?![在的是于从将把被至向为])[\u4e00-\u9fa5A-Za-z0-9]{2,24}(?:有限公司|股份|集团|公司|银行|医院|大学|研究院|事务所)"
)

#: 公司名左边界裁剪词：紧跟组织形式之前、但不属于真实字号的语境成分。
#: 仅在“后缀前的核心”左侧按多字词 + 结构助词“的”剥离，避免误伤以
#: “和/本”等开头的真实字号（如“和泰科技有限公司”）。
COMPANY_LEADING_WORDS = (
    "我们合作", "我们联系", "合作方", "合同方", "签约方", "供应方",
    "甲方", "乙方", "丙方", "我方", "你方", "对方", "供方", "需方",
    "本公司", "贵公司", "该公司", "我司", "贵司", "本司", "该司",
    "我们", "咱们", "你们", "他们", "供应商", "联系人",
    "合作", "联系", "提供", "对接", "签约", "聘请", "委托", "邀请",
    "拜访", "会见", "洽谈", "中标", "投标", "客户",
)
#: 组织形式后缀（与 context.py 保持一致的主后缀）
_COMPANY_SUFFIXES = (
    "股份有限公司", "有限责任公司", "集团有限公司", "有限公司",
    "股份", "集团", "公司", "银行", "医院", "大学", "研究院", "事务所",
)


def trim_company_prefix(full: str) -> str:
    """剥离开头语境词后返回真实公司名。

    先按后缀切出核心，再从核心左侧循环剥离已知语境多字词与结构助词“的”；
    若剥离后核心短于 2 字（泛指）则放弃裁剪，保留原文交给上下文/融合判定。
    """
    core = full
    suffix = ""
    for suf in _COMPANY_SUFFIXES:
        if full.endswith(suf) and len(full) > len(suf):
            core = full[: -len(suf)]
            suffix = suf
            break
    changed = True
    while changed and core:
        changed = False
        for w in COMPANY_LEADING_WORDS:
            if core.startswith(w):
                core = core[len(w):]
                changed = True
                break
        if core.startswith("的"):
            core = core[1:]
            changed = True
    if len(core.strip()) < 2:
        return full
    return core + suffix

_ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID_CHECK = "10X98765432"


def is_valid_id(s: str) -> bool:
    if len(s) != 18:
        return False
    try:
        total = sum(int(s[i]) * _ID_WEIGHTS[i] for i in range(17))
        return _ID_CHECK[total % 11].upper() == s[-1].upper()
    except ValueError:
        return False


def is_valid_birth(s: str) -> bool:
    try:
        y, m, d = int(s[6:10]), int(s[10:12]), int(s[12:14])
        date(y, m, d)
        return 1900 <= y <= 2100
    except Exception:
        return False


def luhn_ok(s: str) -> bool:
    if not s.isdigit() or len(s) < 13:
        return False
    total = 0
    for i, ch in enumerate(reversed(s)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0
