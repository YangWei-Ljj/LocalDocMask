# -*- coding: utf-8 -*-
"""生成演示用合成数据（不含任何真实个人/企业信息）。

产出：
- 业务说明.docx   正文 + 表格 + 脚注 + 页脚；全部高置信/黑名单/领域词 → 预期 PASSED
- 合同材料.pdf    多页；第 2 页图文混排触发 review_flag；含公开企业 → 预期 NEEDS_REVIEW
- 客户台账.xlsx   含隐藏工作表 / 隐藏行列；高置信敏感项 → 预期 PASSED
- 扫描件.png      OCR 扫描件（大字）；身份证 → 预期 PASSED
- 损坏样本.docx   截断字节，用于演示失败隔离/隔离目录

运行：.venv\\Scripts\\python.exe demo\\make_demo_data.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pymupdf  # noqa: E402
from docx import Document  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from openpyxl import Workbook  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

OUT = Path(__file__).resolve().parent / "data"
FONT = r"C:\Windows\Fonts\simhei.ttf"

# 合成、校验位合法（与评测集同源，均为虚构）
MOBILE = "13812345678"
MOBILE2 = "13711112222"
EMAIL = "contact@demo-synth.cn"
ID_CARD = "11010519491231002X"
BANK = "62220202001122338"
BLACKLIST_COMPANY = "杭州云启科技有限公司"
DOMAIN_TERM = "项目代号-苍梧"
PUBLIC_COMPANY = "中国石油股份有限公司"
PERSON = "张伟"


def _ensure_out() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*"):
        if old.is_file():
            old.unlink()


def make_docx() -> Path:
    p = OUT / "业务说明.docx"
    doc = Document()
    # 正文：泛指（应抑制）+ 高置信 PII（应自动确认）
    doc.add_paragraph("本公司产品已上线，我们公司持续推进，相关产品见附录。")
    doc.add_paragraph(
        f"联系人：{PERSON}，手机{MOBILE}，邮箱{EMAIL}，"
        f"身份证{ID_CARD}，银行卡{BANK}。")
    doc.add_paragraph(f"内部供应商：{BLACKLIST_COMPANY}。{DOMAIN_TERM}的材料已归档。")
    # 表格
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "供应商"
    table.cell(0, 1).text = "联系人手机"
    table.cell(1, 0).text = BLACKLIST_COMPANY
    table.cell(1, 1).text = MOBILE2
    # 页脚
    section = doc.sections[0]
    footer_p = section.footer.paragraphs[0]
    footer_p.text = "页脚：本手册为通用业务说明，不涉真实客户。"
    # 脚注：直接写 footnotes.xml（python-docx 不原生支持脚注）
    doc.save(str(p))
    _inject_footnote(p, f"脚注来源：{DOMAIN_TERM}内部纪要。")
    return p


def _inject_footnote(path: Path, text: str) -> None:
    """向 docx 注入最小 footnotes.xml，让 extract/redact 路径都能跑到。"""
    import zipfile
    import shutil
    from lxml import etree

    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    nsmap = {"w": ns}
    fn_root = etree.Element(qn("w:footnotes"), nsmap=nsmap)
    for fid, label, body in [(0, "继续自动编号", ""), (1, "草稿", ""), (2, text, "")]:
        fn = etree.SubElement(fn_root, qn("w:footnote"),
                               {qn("w:id"): str(fid), qn("w:type"): "separator" if fid < 2 else ""})
        if fid >= 2:
            r = etree.SubElement(fn, qn("w:r"))
            t = etree.SubElement(r, qn("w:t"))
            t.text = body
    fn_xml = etree.tostring(fn_root, xml_declaration=True, encoding="UTF-8", standalone=True)

    tmp = path.with_suffix(".tmp.docx")
    with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            zout.writestr(item, data)
        # 写 footnotes.xml（已在包里则覆盖）
        names = zin.namelist()
        if "word/footnotes.xml" not in names:
            zout.writestr("word/footnotes.xml", fn_xml)
    shutil.move(str(tmp), str(path))


def make_pdf() -> Path:
    p = OUT / "合同材料.pdf"
    doc = pymupdf.open()
    # 第 1 页：公开企业（→ 待复核）+ 高置信手机
    page = doc.new_page()
    y = 80
    lines = [
        "合同材料（合成演示）",
        f"今日公告：{PUBLIC_COMPANY}发布年度报告。",
        f"联系电话：{MOBILE}，经办人{PERSON}。",
        "本公司产品条款见附件一。",
    ]
    for line in lines:
        page.insert_text((72, y), line, fontsize=12, fontfile=FONT, fontname="china")
        y += 24
    # 第 2 页：图文混排（嵌入小图 + 少量文本 <40 字触发 review_flag 路径）
    page2 = doc.new_page()
    img_path = OUT / "_synth_img.png"
    _save_tiny_png(img_path)
    page2.insert_image(pymupdf.Rect(80, 80, 300, 220), filename=str(img_path))
    page2.insert_text((72, 400),
                      "附图：现场照片（合成演示），本页为图文混排页，"
                      "嵌入图像内容需人工复核后方可导出。",
                      fontsize=11, fontfile=FONT, fontname="china")
    doc.save(str(p))
    doc.close()
    try:
        img_path.unlink()
    except OSError:
        pass
    return p


def _save_tiny_png(path: Path) -> None:
    img = Image.new("RGB", (200, 150), (200, 220, 240))
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, 190, 140], outline=(80, 80, 80), width=2)
    d.text((30, 70), "SYNTH", fill=(40, 40, 40),
           font=ImageFont.truetype(FONT, 24))
    img.save(str(path))


def make_xlsx() -> Path:
    p = OUT / "客户台账.xlsx"
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "可见台账"
    ws1["A1"] = "客户台账（合成）"
    ws1["A2"] = "本公司产品清单"
    ws1["B2"] = "相关产品介绍见官网"
    # 隐藏行：第 4 行藏 PII
    ws1["A4"] = f"隐藏行：手机{MOBILE2} 邮箱{EMAIL}"
    ws1.row_dimensions[4].hidden = True
    # 隐藏列：C 列藏 PII
    ws1["C2"] = f"隐藏列：身份证{ID_CARD}"
    ws1.column_dimensions["C"].hidden = True

    # 隐藏工作表
    ws2 = wb.create_sheet("内部备忘")
    ws2["A1"] = f"黑名单供应商：{BLACKLIST_COMPANY}"
    ws2["A2"] = f"领域词：{DOMAIN_TERM}"
    ws2["A3"] = f"经办人：{PERSON} 银行卡{BANK}"
    ws2.sheet_state = "hidden"

    wb.save(str(p))
    wb.close()
    return p


def make_png() -> Path:
    p = OUT / "扫描件-身份证.png"
    img = Image.new("RGB", (1100, 220), "white")
    d = ImageDraw.Draw(img)
    d.text((30, 40), f"扫描件  身份证{ID_CARD}", fill="black",
           font=ImageFont.truetype(FONT, 44))
    d.text((30, 130), f"手机{MOBILE}  合成演示", fill="black",
           font=ImageFont.truetype(FONT, 32))
    img.save(str(p))
    return p


def make_corrupt() -> Path:
    p = OUT / "损坏样本.docx"
    p.write_bytes(b"PK\x03\x04 this is a truncated, fake docx body ................")
    return p


def main() -> None:
    _ensure_out()
    files = [make_docx(), make_pdf(), make_xlsx(), make_png(), make_corrupt()]
    for f in files:
        print(f"[OK] {f.name:20s} {f.stat().st_size:>8d} bytes")


if __name__ == "__main__":
    main()
