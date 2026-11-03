# -*- coding: utf-8 -*-
"""端到端集成测试：组合根把识别/持久化/一致性映射真实接入主流水线。

覆盖（任务四）：
1. scan 后三档分流：泛指/白名单 → SUPPRESS；公开企业 → NEEDS_REVIEW；
   手机/合法身份证/邮箱/黑名单/领域词 → AUTO_CONFIRM。
2. 复核门禁：未裁决 NEEDS_REVIEW 时 finalize 阻断；用户确认后可成功导出。
3. 模式 B 批次：两文件含同一真实公司，跨文件映射为同一虚构值。
4. 原件 SHA-256 前后不变；输出可打开；失败文件隔离不污染成功结果。
5. 幂等：同一文件重复处理，repository 命中已成功产物。

所有数据为合成文件，写入临时目录，不触碰项目 data/。
"""
import os
from pathlib import Path

import pymupdf
import pytest
from docx import Document
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont

from localdocmask.application.composition import build_services
from localdocmask.application.pipeline import POLICY_VERSION
from localdocmask.domain.enums import EntityDecision, TaskStatus
from localdocmask.security.hashing import sha256_file

FONT = r"C:\Windows\Fonts\simhei.ttf"
VALID_ID = "11010519491231002X"


@pytest.fixture
def services(tmp_path, monkeypatch):
    # 把数据目录隔离到本测试临时目录（db 落在 tmp_path/tasks.db）
    monkeypatch.setenv("LOCALDOCMASK_HOME", str(tmp_path))
    svc = build_services()
    yield svc
    svc.close()


def _docx(path: Path, paragraphs: list[str]) -> Path:
    d = Document()
    for p in paragraphs:
        d.add_paragraph(p)
    d.save(str(path))
    return path


def _pdf(path: Path, lines: list[str]) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 80
    for line in lines:
        page.insert_text((72, y), line, fontsize=12, fontfile=FONT, fontname="china")
        y += 24
    doc.save(str(path))
    doc.close()
    return path


def _xlsx(path: Path, cells: dict[str, str]) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "数据"
    for addr, val in cells.items():
        ws[addr] = val
    wb.save(str(path))
    return path


def _png(path: Path, text: str, size=(900, 120)) -> Path:
    img = Image.new("RGB", size, "white")
    ImageDraw.Draw(img).text((20, 35), text, fill="black",
                             font=ImageFont.truetype(FONT, 36))
    img.save(str(path))
    return path


def _docx_text(path: Path) -> str:
    d = Document(str(path))
    return "\n".join(p.text for p in d.paragraphs)


# ---------------------------------------------------------------------------
# 1. 三档分流
# ---------------------------------------------------------------------------
def test_three_tier_adjudication(services, tmp_path):
    src = _docx(tmp_path / "three_tier.docx", [
        "本公司产品已上线，我们公司继续推进，本集团保持稳定，我们银行服务客户，我们大学欢迎报考。",
        "公开企业：中国石油股份有限公司。",
        f"手机13812345678 邮箱a@b.com 身份证{VALID_ID}",
        "内部供应商：杭州云启科技有限公司。项目代号-苍梧。",
    ])
    task = services.pipeline.prepare(src)
    scan = services.pipeline.scan(task)

    by_text = {c.text: c for c in scan.candidates}

    # SUPPRESS：泛指/白名单被排除
    suppressed = [c for c in scan.candidates if c.excluded]
    assert suppressed, "泛指/白名单应被排除(SUPPRESS)"
    assert any("本公司产品" in c.text or "我们公司" in c.text or
               "本集团" in c.text or "我们银行" in c.text or "我们大学" in c.text
               for c in suppressed)

    # NEEDS_REVIEW：一般语境公开企业
    review = [c for c in scan.candidates
              if c.decision == EntityDecision.NEEDS_REVIEW and not c.excluded]
    assert any("中国石油" in c.text for c in review), [
        (c.text, c.decision, c.excluded) for c in scan.candidates]

    # AUTO_CONFIRM：手机/身份证/邮箱/黑名单/领域词
    confirmed = {c.text: c for c in scan.candidates
                 if c.decision == EntityDecision.AUTO_CONFIRM}
    assert any("13812345678" in t for t in confirmed), list(confirmed)
    assert any(VALID_ID in t for t in confirmed)
    assert any("a@b.com" in t for t in confirmed)
    assert any("杭州云启科技有限公司" in t for t in confirmed)
    assert any("项目代号-苍梧" in t for t in confirmed)


# ---------------------------------------------------------------------------
# 2. 复核门禁：未裁决阻断；确认后可导出
# ---------------------------------------------------------------------------
def test_review_gate_blocks_then_passes(services, tmp_path):
    src = _docx(tmp_path / "review.docx", [
        "公开企业：中国石油股份有限公司。",
        f"手机13812345678 身份证{VALID_ID}",
    ])

    # 第一次：不裁决 NEEDS_REVIEW，finalize 应阻断
    task = services.pipeline.prepare(src)
    services.pipeline.scan(task)
    services.pipeline.process(task, "a", "****", task.scan.candidates)
    report = services.pipeline.audit(task, task.scan.candidates)
    task = services.pipeline.finalize(task, report)
    assert task.status != TaskStatus.PASSED
    assert task.status == TaskStatus.NEEDS_REVIEW

    # 第二次：用户显式确认待复核项后，应成功导出
    task2 = services.pipeline.prepare(src)
    services.pipeline.scan(task2)
    for c in task2.scan.candidates:
        if c.decision == EntityDecision.NEEDS_REVIEW and not c.excluded:
            c.confirmed = True
            c.decision = EntityDecision.CONFIRMED
    services.pipeline.process(task2, "a", "****", task2.scan.candidates)
    report2 = services.pipeline.audit(task2, task2.scan.candidates)
    task2 = services.pipeline.finalize(task2, report2)
    assert task2.status == TaskStatus.PASSED, task2.error
    # 公开企业名已被脱敏，不在输出中
    assert "中国石油" not in _docx_text(task2.output_path)


# ---------------------------------------------------------------------------
# 3. 模式 B 批次跨文件一致映射
# ---------------------------------------------------------------------------
def test_mode_b_batch_cross_file_consistency(services, tmp_path):
    f1 = _docx(tmp_path / "b1.docx",
               ["供应商：杭州云启科技有限公司 手机13811111111"])
    f2 = _docx(tmp_path / "b2.docx",
               ["合作方：杭州云启科技有限公司 手机13822222222"])
    res = services.batch.run([f1, f2], mode="b", mask="****")
    assert len(res.passed) == 2, res.warnings

    fic = []
    for t in res.passed:
        txt = _docx_text(t.output_path)
        # 真实公司名不应出现；记录输出中的虚构公司名
        assert "杭州云启科技有限公司" not in txt
        fic.append(txt.strip())
    # 两文件映射为同一虚构公司值
    # 提取虚构公司词（以"公司"结尾的替换值），二者应一致
    import re
    pats = [set(re.findall(r"[一-龥]+(?:有限责任|股份|)?公司", t)) for t in fic]
    common = pats[0] & pats[1]
    assert common, f"两文件应有共同虚构公司名：{pats}"
    assert "杭州云启科技有限公司" not in (pats[0] | pats[1])


# ---------------------------------------------------------------------------
# 4. 原件哈希不变 + 输出可打开 + 各格式跑通
# ---------------------------------------------------------------------------
def test_formats_roundtrip_and_source_hash(services, tmp_path):
    h_before = {}
    files = []

    docx_src = _docx(tmp_path / "f.docx", [f"手机13812345678 邮箱x@y.com"])
    files.append(docx_src)

    pdf_src = _pdf(tmp_path / "f.pdf", [f"手机13812345678 身份证{VALID_ID}"])
    files.append(pdf_src)

    xlsx_src = _xlsx(tmp_path / "f.xlsx",
                      {"A1": f"客户 手机13812345678 身份证{VALID_ID}"})
    files.append(xlsx_src)

    png_src = _png(tmp_path / "f.png", "13812345678")
    files.append(png_src)

    for f in files:
        h_before[f] = sha256_file(f)
        task = services.pipeline.prepare(f)
        services.pipeline.scan(task)
        services.pipeline.process(task, "a", "****", task.scan.candidates)
        report = services.pipeline.audit(task, task.scan.candidates)
        task = services.pipeline.finalize(task, report)
        assert task.status == TaskStatus.PASSED, (f.name, task.error)
        # 原件 SHA-256 不变
        assert sha256_file(f) == h_before[f]
        # 输出可打开
        assert task.output_path.exists()
        if task.output_path.suffix == ".docx":
            assert Document(str(task.output_path)).paragraphs is not None
        elif task.output_path.suffix == ".pdf":
            d = pymupdf.open(str(task.output_path)); n = d.page_count; d.close()
            assert n >= 1
        elif task.output_path.suffix == ".xlsx":
            from openpyxl import load_workbook
            assert load_workbook(str(task.output_path)) is not None
        elif task.output_path.suffix == ".png":
            assert Image.open(str(task.output_path)) is not None


# ---------------------------------------------------------------------------
# 5. 幂等：重复处理命中已成功产物
# ---------------------------------------------------------------------------
def test_idempotent_reuse(services, tmp_path):
    src = _docx(tmp_path / "idem.docx",
                [f"手机13812345678 邮箱r@s.com"])
    task = services.pipeline.prepare(src, mode="b")
    services.pipeline.scan(task)
    services.pipeline.process(task, "b", "****", task.scan.candidates)
    report = services.pipeline.audit(task, task.scan.candidates)
    task = services.pipeline.finalize(task, report)
    assert task.status == TaskStatus.PASSED

    # 第二次：同文件同模式，prepare 应命中已成功产物
    again = services.pipeline.prepare(src, mode="b")
    assert again._reusable is not None
    assert again._reusable.status == TaskStatus.PASSED
    # repository 直接查询亦应命中
    found = services.repository.find_by_source(
        again.source_hash, "b", POLICY_VERSION)
    assert found is not None and found.status == TaskStatus.PASSED
