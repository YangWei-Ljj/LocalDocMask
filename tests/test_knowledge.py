# -*- coding: utf-8 -*-
from pathlib import Path

from localdocmask.recognition.knowledge import KnowledgeBase

_RES = Path(__file__).resolve().parents[1] / "resources"


def test_load_four_buckets():
    kb = KnowledgeBase.load(_RES)
    assert kb.whitelist and kb.blacklist and kb.public_entities and kb.domain_terms


def test_classify_hits():
    kb = KnowledgeBase.load(_RES)
    assert "blacklist" in kb.classify("杭州云启科技有限公司")
    assert "whitelist" in kb.classify("本公司产品")
    assert "public" in kb.classify("中国石油天然气集团有限公司")
    assert "domain" in kb.classify("项目代号-苍梧")


def test_classify_no_hit():
    kb = KnowledgeBase.load(_RES)
    assert kb.classify("张三") == set()
