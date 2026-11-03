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
COMPANY_RE = re.compile(
    r"(?![在的与和是于对从将把被及至向为])[\u4e00-\u9fa5A-Za-z0-9]{2,24}(?:有限公司|股份|集团|公司|银行|医院|大学|研究院|事务所)"
)

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
