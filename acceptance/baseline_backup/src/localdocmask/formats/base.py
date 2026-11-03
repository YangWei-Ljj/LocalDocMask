"""格式处理器基类。"""
from __future__ import annotations

from ..models import ScanResult


class FormatHandler:
    name = "base"
    extensions: list[str] = []

    def __init__(self, detector):
        self.detector = detector

    def extract(self, path: str) -> ScanResult:
        raise NotImplementedError

    def process(self, path: str, out_path: str, candidates: list) -> list[str]:
        """将脱敏结果写入 out_path，返回警告列表；失败必须抛异常。"""
        raise NotImplementedError
