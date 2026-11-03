"""本地中文 NER 端口与基础设施（可插拔、默认关闭、失败关闭）。

设计对应 ADR-009：
- 端口 NERPort 只声明 ``extract_entities(text) -> list[(类别, 文本)]``；
  类别归一为 LocalDocMask 内部口径：PER→姓名、ORG→公司名、LOC→详细地址。
- 真实实现 OnnxChineseNER 基于 onnxruntime（与现有 OCR 同一推理栈），
  运行时不依赖 torch / transformers / tokenizers。
- 未启用、未安装依赖或模型缺失时，由组合根与调用方捕获异常并降级，
  绝不阻断主识别流程。
"""
from .base import NERPort, NerUnavailable
from .onnx_ner import OnnxChineseNER

__all__ = ["NERPort", "NerUnavailable", "OnnxChineseNER"]
