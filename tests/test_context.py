# -*- coding: utf-8 -*-
from localdocmask.recognition.context import ContextAnalyzer


def _generic(text, span_text):
    start = text.index(span_text)
    return ContextAnalyzer().analyze(text, (start, start + len(span_text)), "公司名")


def test_generic_references():
    for s in ["我们公司", "有限公司", "集团公司", "银行", "医院", "大学",
              "本公司", "该集团", "贵公司"]:
        f = _generic("前缀" + s + "后缀", s)
        assert f.is_generic_reference is True, s


def test_real_entities_not_generic():
    for s in ["中国石油股份有限公司", "北京字节科技有限公司", "中国工商银行股份有限公司"]:
        f = _generic("前缀" + s + "后缀", s)
        assert f.is_generic_reference is False, s


def test_context_signals():
    text = "签订合同供应商杭州云启科技有限公司发布公告"
    start = text.index("杭州云启科技有限公司")
    f = ContextAnalyzer().analyze(text, (start, start + len("杭州云启科技有限公司")), "公司名")
    joined = " ".join(f.context_signals)
    assert "sensitive:合同" in joined or "sensitive:供应商" in joined
