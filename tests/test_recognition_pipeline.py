# -*- coding: utf-8 -*-
from localdocmask.domain.entities import Candidate
from localdocmask.domain.enums import EntityDecision
from localdocmask.recognition.pipeline import RecognitionPipeline


def test_analyze_high_precision():
    p = RecognitionPipeline()
    out = p.analyze("手机13812345678 邮箱a@b.com")
    by_cat = {c.category: c for c in out}
    assert by_cat["手机号"].decision == EntityDecision.AUTO_CONFIRM
    assert by_cat["邮箱"].decision == EntityDecision.AUTO_CONFIRM


def test_analyze_generic_suppressed():
    p = RecognitionPipeline()
    out = p.analyze("我们公司的产品已上线。")
    assert all(c.excluded for c in out)


def test_analyze_blacklist_confirmed():
    p = RecognitionPipeline()
    out = p.analyze("供应商：杭州云启科技有限公司。")
    assert any(c.text == "杭州云启科技有限公司" and
               c.decision == EntityDecision.AUTO_CONFIRM for c in out)


def test_adjudicate_does_not_relocate():
    p = RecognitionPipeline()
    existing = [Candidate(text="13812345678", category="手机号",
                          location="正文", confidence=0.95, raw_confidence=0.95,
                          source="regex", box=[1, 2, 3, 4])]
    out = p.adjudicate(existing, "手机13812345678")
    assert out[0].box == [1, 2, 3, 4]
    assert out[0].decision == EntityDecision.AUTO_CONFIRM


def test_empty_text():
    p = RecognitionPipeline()
    assert p.analyze("") == []
    assert p.adjudicate([], "") == []
