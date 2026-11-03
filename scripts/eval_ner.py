# -*- coding: utf-8 -*-
"""NER 开/关真实评测（ADR-009）。

用 EvaluationRunner 跑同一标注集（101 条），对比：
- off: RecognitionPipeline(detector=Detector(persons=[张伟,王芳]))（基线，无 NER）
- on : 同上 + 真实 OnnxChineseNER

另跑新增反例（无标点连续动词串、词典外裸人名、LOC）。
所有数字来自真实运行，不伪造；无法可靠命中的如实记录为漏报/复核。
"""
from __future__ import annotations

import json
from pathlib import Path

from localdocmask.evaluation.dataset import Dataset, LabeledCase
from localdocmask.evaluation.runner import EvaluationRunner
from localdocmask.recognition.pipeline import RecognitionPipeline
from localdocmask.recognition.rule_based import Detector

PROJECT = Path(__file__).resolve().parents[1]
DATASET = PROJECT / "src" / "localdocmask" / "evaluation" / "data" / "labeled_cases.yaml"


def _base_pipeline(ner=None) -> RecognitionPipeline:
    return RecognitionPipeline(
        detector=Detector(persons=["张伟", "王芳"]), ner=ner)


def main():
    ds = Dataset.load(DATASET)

    off = EvaluationRunner(pipeline=_base_pipeline(ner=None))
    res_off = off.run(ds)

    from localdocmask.infrastructure.ner.onnx_ner import OnnxChineseNER
    ner = OnnxChineseNER()
    on = EvaluationRunner(pipeline=_base_pipeline(ner=ner))
    res_on = on.run(ds)

    # 新增反例
    adv = Dataset(cases=[
        LabeledCase(text="张伟对接杭州云启科技有限公司提供服务",
                    true_entities=[("公司名", "杭州云启科技有限公司")],
                    note="无标点连续动词串公司边界"),
        LabeledCase(text="陈晓峰将于周五前往上海洽谈合作",
                    true_entities=[("姓名", "陈晓峰")],
                    note="词典外裸人名召回"),
        LabeledCase(text="经办人陈晓峰确认本周付款",
                    true_entities=[("姓名", "陈晓峰")],
                    note="词典外裸人名端到端"),
        LabeledCase(text="公司位于北京市朝阳区望京街8号",
                    true_entities=[("详细地址", "北京市朝阳区望京街")],
                    note="LOC 粘合（模型已知碎片局限）"),
    ])
    adv_off = off.run(adv)
    adv_on = on.run(adv)

    report = {
        "dataset_cases": len(ds),
        "baseline_off": res_off,
        "ner_on": res_on,
        "adversarial_off": adv_off,
        "adversarial_on": adv_on,
    }
    out = PROJECT / "scripts" / "_ner_eval_result.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
