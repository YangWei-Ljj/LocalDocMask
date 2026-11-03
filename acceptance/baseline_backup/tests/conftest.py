"""测试公共配置。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

VALID_ID = "11010519491231002X"


def luhn_valid(base: str) -> str:
    digits = [int(c) for c in base]
    for i in range(len(digits) - 1, -1, -2):
        digits[i] *= 2
        if digits[i] > 9:
            digits[i] -= 9
    return base + str((10 - sum(digits) % 10) % 10)
