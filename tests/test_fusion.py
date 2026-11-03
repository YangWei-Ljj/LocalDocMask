# -*- coding: utf-8 -*-
from pathlib import Path

from localdocmask.domain.entities import Candidate
from localdocmask.domain.enums import EntityDecision
from localdocmask.domain.policy import ReviewPolicy
from localdocmask.recognition.fusion import FusionEngine
from localdocmask.recognition.knowledge import KnowledgeBase

_RES = Path(__file__).resolve().parents[1] / "resources"


def _engine():
    return FusionEngine(ReviewPolicy(), KnowledgeBase.load(_RES))


def _cand(cat, text, conf=0.6, src="regex"):
    return Candidate(text=text, category=cat, confidence=conf,
                     raw_confidence=conf, source=src)


def test_merge_multisource_boosts():
    eng = _engine()
    out = eng.fuse([_cand("手机号", "13812345678", 0.95),
                    _cand("手机号", "13812345678", 0.55, "ollama")],
                   "手机13812345678")
    assert len(out) == 1
    assert out[0].confidence >= out[0].raw_confidence


def test_whitelist_suppress():
    eng = _engine()
    out = eng.fuse([_cand("公司名", "本公司产品", 0.6)], "本公司产品手册")
    assert out[0].decision == EntityDecision.SUPPRESS
    assert out[0].excluded is True


def test_blacklist_auto_confirm():
    eng = _engine()
    out = eng.fuse([_cand("公司名", "杭州云启科技有限公司", 0.6)],
                   "供应商杭州云启科技有限公司")
    assert out[0].decision == EntityDecision.AUTO_CONFIRM
    assert out[0].confirmed is True


def test_public_needs_review():
    eng = _engine()
    out = eng.fuse([_cand("公司名", "中国石油天然气集团有限公司", 0.6)],
                   "新闻中国石油天然气集团有限公司年报")
    assert out[0].decision == EntityDecision.NEEDS_REVIEW


def test_generic_company_suppress():
    eng = _engine()
    out = eng.fuse([_cand("公司名", "我们公司", 0.6)], "我们公司的产品")
    assert out[0].decision == EntityDecision.SUPPRESS


def test_ollama_only_conflicts_review():
    eng = _engine()
    out = eng.fuse([_cand("公司名", "某神秘公司", 0.55, "ollama")],
                   "某神秘公司出现")
    assert out[0].decision == EntityDecision.NEEDS_REVIEW


def test_high_precision_auto_confirm():
    eng = _engine()
    out = eng.fuse([_cand("手机号", "13812345678", 0.95)], "手机13812345678")
    assert out[0].decision == EntityDecision.AUTO_CONFIRM
