# -*- coding: utf-8 -*-
from localdocmask.security.redaction.masker import Masker


def test_mode_a_mask():
    m = Masker("a", "****")
    assert m.replacement("手机号", "13812345678") == "****"
    assert m.replacement("公司名", "字节跳动") == "****"


def test_mode_b_consistent_company():
    m = Masker("b")
    a = m.replacement("公司名", "字节跳动")
    b = m.replacement("公司名", "字节跳动")
    c = m.replacement("公司名", "腾讯")
    assert a == b
    assert a != c


def test_mode_b_consistent_person():
    m = Masker("b")
    a = m.replacement("姓名", "张三")
    b = m.replacement("姓名", "张三")
    assert a == b
    assert a != "张三"


def test_mode_b_numbers_still_masked():
    m = Masker("b")
    assert m.replacement("手机号", "13812345678") == "****"
