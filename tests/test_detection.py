# -*- coding: utf-8 -*-
from conftest import VALID_ID, luhn_valid
from localdocmask.recognition.rule_based import Detector


def test_regex_rules():
    d = Detector()
    text = f"手机13812345678 身份证{VALID_ID} 邮箱 a@b.com 银行卡{luhn_valid('6222020200112233')}"
    cats = {c.category: c.text for c in d.detect_text(text)}
    assert cats["手机号"] == "13812345678"
    assert cats["身份证号"] == VALID_ID
    assert cats["邮箱"] == "a@b.com"
    assert "银行卡号" in cats


def test_invalid_id_ignored():
    d = Detector()
    # 校验位错误的身份证号不应被识别
    bad = "110105194912310021"
    assert not any(c.category == "身份证号" for c in d.detect_text(f"身份证 {bad}"))


def test_dict_entities():
    d = Detector(companies=["字节跳动"], persons=["张三"])
    cands = d.detect_text("北京字节跳动有限公司 张三 李四")
    texts = [c.text for c in cands]
    assert "北京字节跳动有限公司" in texts
    assert "张三" in texts


def test_longer_first_ordering():
    d = Detector(companies=["字节跳动"])
    cands = d.detect_text("北京字节跳动有限公司")
    # 更长的公司名应排在前，替换时先处理，避免破坏长词
    assert cands[0].text == "北京字节跳动有限公司"


def test_custom_words():
    d = Detector(custom_words=["机密数据"])
    assert any(c.category == "自定义敏感词" for c in d.detect_text("这是机密数据"))


def test_address_and_number():
    d = Detector()
    cands = d.detect_text("北京市朝阳区望京街10号 合同编号：HT-2024-0001")
    assert any(c.category == "详细地址" for c in cands)
    assert any(c.category == "编号" for c in cands)
