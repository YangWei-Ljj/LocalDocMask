"""NER 端口：领域/识别层只依赖该 Protocol，不感知具体模型。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


class NerUnavailable(RuntimeError):
    """NER 不可用（缺依赖 / 缺模型 / 推理失败）。调用方应捕获并降级。"""


@runtime_checkable
class NERPort(Protocol):
    """本地中文 NER 端口。

    extract_entities 返回 ``(类别, 文本)`` 列表；类别统一为：
    姓名 / 公司名 / 详细地址。找不到时返回空列表，不得抛给主流程。
    """

    def extract_entities(self, text: str) -> list[tuple[str, str]]:  # pragma: no cover
        ...
