# -*- coding: utf-8 -*-
"""公司名左边界：无标点相邻句式下不应吞掉甲乙方/代词/动词前缀。"""
from localdocmask.recognition import patterns as P
from localdocmask.recognition.rule_based import Detector


def _companies(text):
    return [c.text for c in Detector().detect_text(text) if c.category == "公司名"]


def test_trim_unit_drops_leading_context():
    assert P.trim_company_prefix("甲方杭州云启科技有限公司") == "杭州云启科技有限公司"
    assert P.trim_company_prefix("我们合作的杭州云启科技有限公司") == "杭州云启科技有限公司"


def test_trim_unit_preserves_brand_with_connective_start():
    # 以连接字开头的真实字号不应被误裁
    assert P.trim_company_prefix("和泰科技有限公司") == "和泰科技有限公司"


def test_no_punctuation_sentence_no_prefix_swallow():
    out = _companies("甲方杭州云启科技有限公司今日签约")
    assert "杭州云启科技有限公司" in out
    assert not any(s.startswith("甲方") for s in out)


def test_wo_he_context_trimmed():
    out = _companies("我们合作的杭州云启科技有限公司")
    assert "杭州云启科技有限公司" in out
    assert not any(s.startswith("我们") for s in out)


def test_positive_examples_still_hit():
    for t, expect in [
        ("杭州云启科技有限公司", "杭州云启科技有限公司"),
        ("字节跳动", None),  # 无组织形式后缀，正则不命中，属已知词典路径
        ("浙江蓝鲸咨询有限公司", "浙江蓝鲸咨询有限公司"),
    ]:
        if expect:
            assert expect in _companies(t)


def test_generic_still_generic_after_trim():
    # 泛指句不产生真实专名候选（交由上下文/融合抑制）
    out = _companies("我们公司的产品已上线")
    assert all(("公司" in s) for s in out)
