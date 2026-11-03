# -*- coding: utf-8 -*-
"""一次性脚本：把 shibing624/bert4ner-base-chinese 导出为 ONNX（CPU）。

仅用于构建 models/ner_bert4ner/model.onnx 制品；运行时适配器不依赖 torch/transformers。
"""
import json
from pathlib import Path

import torch
from transformers import AutoModelForTokenClassification, AutoTokenizer

HERE = Path(__file__).resolve().parents[1]
OUT = HERE / "models" / "ner_bert4ner"


def main():
    tok = AutoTokenizer.from_pretrained(str(OUT))
    model = AutoModelForTokenClassification.from_pretrained(str(OUT))
    model.eval()

    text = "张伟对接杭州云启科技有限公司提供服务"
    enc = tok(text, return_tensors="pt", truncation=True, max_length=128)
    input_names = ["input_ids", "attention_mask", "token_type_ids"]
    dummy = tuple(enc[k] for k in input_names)

    dynamic_axes = {
        "input_ids": {0: "batch", 1: "seq"},
        "attention_mask": {0: "batch", 1: "seq"},
        "token_type_ids": {0: "batch", 1: "seq"},
        "logits": {0: "batch", 1: "seq"},
    }
    onnx_path = OUT / "model.onnx"
    torch.onnx.export(
        model,
        dummy,
        str(onnx_path),
        input_names=input_names,
        output_names=["logits"],
        dynamic_axes=dynamic_axes,
        opset_version=14,
        do_constant_folding=True,
    )
    print("ONNX exported:", onnx_path, onnx_path.stat().st_size)

    # 保存 id2label，运行时适配器读取，避免依赖 transformers
    with open(OUT / "id2label.json", "w", encoding="utf-8") as f:
        json.dump(model.config.id2label, f, ensure_ascii=False, indent=2)
    print("id2label:", model.config.id2label)


if __name__ == "__main__":
    main()
