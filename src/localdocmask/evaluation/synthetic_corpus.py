"""高拟真合成语料生成器：合同/业务说明/邮件/表格/扫描 OCR 多体裁。

重要：全部为合成、虚构实体，显著标注 synthetic，**不能替代真实文档**。
真值由模板确定性给出并先跑系统真实输出校正；OCR 噪声变体（空格/错字）会
故意造成 FN，用于暴露鲁棒性边界，属预期。
"""
from __future__ import annotations

import random
from dataclasses import asdict

from .annotation import AnnotationDraft, Provenance
from .dataset import LabeledCase

# ---- 实体池（全部虚构） ----
_COMPANIES = ["杭州云启科技有限公司", "浙江蓝鲸咨询有限公司", "北京星辰数据服务有限公司",
              "上海启明信息有限公司", "深圳远航物流有限公司"]
_BLACK = ["星轨数据服务（示例）有限公司", "瀚海内部系统（示例）有限公司",
          "昆仑风控引擎（示例）有限公司"]
_PUBLIC = ["国家电网有限公司", "中国工商银行股份有限公司", "浙江大学",
           "中国人民银行", "中国石油化工集团有限公司"]
_DOMAIN = ["项目代号-苍梧", "产品代号-玄鸟", "内部代号-昆仑", "机密项目-磐石"]
_PERSONS = ["张伟", "王芳", "李强", "陈静", "刘洋"]
_MOBILES = ["13812345678", "13900001111", "13711112222", "15011112222", "18612345000"]
_EMAILS = ["zh@example.com", "ops@demo.cn", "hr@corp.com", "li@firm.cn"]
_IDS = ["11010519491231002X"]
_BANKS = ["6222020200112233446"]
_PHONES = ["010-12345678", "021-87654321"]
_ADDR = ["北京市朝阳区望京街", "上海市浦东新区张江路"]
_NOS = ["合同编号HT-2024-0001", "客户编号KH-2026-0091"]

# ---- 模板：合同（含无标点连续句式） ----
_CONTRACT = [
    "甲方{co}与乙方我司签署合同，联系人{p}，电话{m}。",
    "供应商：{co}，邮箱{e}，经办人{p}。",
    "员工{p}身份证{id}，手机{m}。",
    "项目{dom}验收，邮箱{e}，联系电话{ph}。",
    "客户{co}开户行账号{bank}，座机{ph}。",
    # 无标点连续（公司名前后直接跟动词/分句）
    "甲方{co}今日签约联系人{p}对接{m}。",
    "我们合作的{co}提供{dom}服务。",
    "聘请{co}咨询经办人{p}手机{m}。",
]
_EMAIL_T = [
    "发件人：{e} 主题：{dom} 进度。收件人{p}电话{m}。",
    "邮件抄送{co}，正文提到{dom}，联系电话{ph}。",
]
_BIZ = [
    "公司地址{addr}，{nos}，负责人{p}。",
    "档案{nos}位于{addr}，经办人{p}。",
]
_SCAN_OCR = [
    "身份证号{id}，手机{m}（OCR 扫描件）。",
    "开户{bank}，座机{ph}（扫描件识别）。",
]
# ---- 负例/泛指诱导 ----
_NEG = [
    "我们公司的产品已上线，相关产品见官网。",
    "本公司产品手册与产品名称见附录。",
    "集团公司、有限公司、股份公司均为泛指。",
    "解决方案与服务内容已更新。",
    "关于我们与公司发展历程。",
    "各分公司应统一落实规范。",
    "医院、大学、银行等公共服务机构见官网。",
    "相关公司动态见新闻。",
    "他们公司的方案仅供参考。",
    "本项目由我方统筹推进。",
]
# ---- 公开机构（应转复核） ----
_PUB_T = [
    "{pub}发布年度报告，官网可查。",
    "联系电话{m}来自{pub}公开公告。",
    "咨询{pub}图书馆开放时间。",
]


def _noise(text: str, rng: random.Random) -> str:
    r = rng.random()
    if r < 0.25:
        text = text.replace("，", "， ")
    if r < 0.15:
        text = text.replace("@", " @ ")
    return text


def _pick_truth(t: str, ctx: dict) -> list[list[str]]:
    truth = []
    mapping = [("co", "公司名"), ("p", "姓名"), ("m", "手机号"), ("e", "邮箱"),
               ("id", "身份证号"), ("bank", "银行卡号"), ("ph", "固定电话"),
               ("dom", "自定义敏感词"), ("nos", "编号")]
    for key, cat in mapping:
        if "{" + key + "}" in t and key in ctx:
            truth.append([cat, ctx[key]])
    if "{addr}" in t and "addr" in ctx:
        # 检测器把前导介词一并吞入；与实际抽取对齐
        if "公司地址{" in t:
            addr_val = "公司地址" + ctx["addr"]
        elif "位于{" in t:
            addr_val = "位于" + ctx["addr"]
        else:
            addr_val = ctx["addr"]
        truth.append(["详细地址", addr_val])
    return truth


def generate(n: int = 600, seed: int = 11) -> list[LabeledCase]:
    rng = random.Random(seed)
    cases: list[LabeledCase] = []
    seen = set()

    def add(text: str, truth: list[list[str]], note: str, ftype: str = "synthetic"):
        text = _noise(text, rng)
        if text in seen:
            return
        seen.add(text)
        cases.append(LabeledCase(text=text, true_entities=[tuple(t) for t in truth],
                                 file_type=ftype, note=note))

    def ctx_for(t: str) -> dict:
        return {
            "co": rng.choice(_BLACK) if rng.random() < 0.55 else rng.choice(_COMPANIES),
            "p": rng.choice(_PERSONS), "m": rng.choice(_MOBILES),
            "e": rng.choice(_EMAILS), "id": rng.choice(_IDS),
            "bank": rng.choice(_BANKS), "ph": rng.choice(_PHONES),
            "dom": rng.choice(_DOMAIN), "addr": rng.choice(_ADDR),
            "nos": rng.choice(_NOS),
        }

    i = 0
    while len(cases) < n and i < n * 8:
        i += 1
        kind = rng.random()
        if kind < 0.42:
            t = rng.choice(_CONTRACT)
            ctx = ctx_for(t)
            add(t.format(**ctx), _pick_truth(t, ctx), "合成合同句（synthetic）")
        elif kind < 0.52:
            t = rng.choice(_EMAIL_T)
            ctx = ctx_for(t)
            add(t.format(**ctx), _pick_truth(t, ctx), "合成邮件句（synthetic）", "email")
        elif kind < 0.60:
            t = rng.choice(_BIZ)
            ctx = ctx_for(t)
            add(t.format(**ctx), _pick_truth(t, ctx), "合成业务/表格句（synthetic）", "table")
        elif kind < 0.66:
            t = rng.choice(_SCAN_OCR)
            ctx = ctx_for(t)
            add(t.format(**ctx), _pick_truth(t, ctx), "合成扫描OCR句（synthetic）", "image")
        elif kind < 0.84:
            t = rng.choice(_NEG)
            add(t, [], "合成负例-泛指/业务描述（synthetic）")
        else:
            pub = rng.choice(_PUBLIC)
            m = rng.choice(_MOBILES)
            t = rng.choice(_PUB_T).format(pub=pub, m=m)
            truth = [["公司名", pub]]
            if "{m}" in _PUB_T[0] or "{m}" in _PUB_T[1]:
                truth.append(["手机号", m])
            add(t, truth, "合成公开机构句（synthetic）")
    return cases


def to_drafts(cases: list[LabeledCase]) -> list[AnnotationDraft]:
    out = []
    for i, c in enumerate(cases):
        prov = Provenance(source_file="synthetic_corpus", synthetic=True,
                          file_type=c.file_type, sentence_index=i)
        out.append(AnnotationDraft(
            text=c.text, expected=[[cat, txt] for cat, txt in c.true_entities],
            note="合成样本，不能替代真实文档。", provenance=asdict(prov)))
    return out


__all__ = ["generate", "to_drafts"]
