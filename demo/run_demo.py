# -*- coding: utf-8 -*-
"""演示运行器：在临时 HOME 下对 demo/data 中的合成文件跑完整闭环。

- 不触碰项目根 data/（LOCALDOCMASK_HOME 指向 demo/_home）；
- 先逐文件 prepare+scan 打印三档裁决，再用 BatchService 跑模式 B 批量；
- 打印成功/复核/失败/跳过分组、原件哈希一致性、模式 B 跨文件一致性；
- 控制台输出同时写入 demo/demo_output.txt，可重复运行。

运行：.venv\\Scripts\\python.exe demo\\run_demo.py
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# ---- 隔离 HOME：必须在 import settings 之前设置（模块级常量在 import 时绑定）----
HOME = ROOT / "demo" / "_home"
if HOME.exists():
    shutil.rmtree(HOME, ignore_errors=True)
HOME.mkdir(parents=True, exist_ok=True)
os.environ["LOCALDOCMASK_HOME"] = str(HOME)

from localdocmask.application.composition import build_services  # noqa: E402
from localdocmask.security.hashing import sha256_file  # noqa: E402

DEMO_DIR = Path(__file__).resolve().parent / "data"
EXPECTED_FILES = [
    "业务说明.docx",
    "合同材料.pdf",
    "客户台账.xlsx",
    "扫描件-身份证.png",
    "损坏样本.docx",
]


def _hr(title: str = "") -> None:
    bar = "=" * 78
    if title:
        print(bar)
        print(title)
        print(bar)
    else:
        print(bar)


def _extract_docx_text(path: Path) -> str:
    from docx import Document
    d = Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def _extract_xlsx_text(path: Path) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(str(path), data_only=True)
    parts = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for v in row:
                if v is not None:
                    parts.append(str(v))
    wb.close()
    return "\n".join(parts)


def main() -> int:
    _hr("LocalDocMask 演示运行（模式 B，合成数据）")
    print(f"临时 HOME（不污染项目 data/）：{HOME}")
    print(f"演示数据目录：{DEMO_DIR}")
    print()

    services = build_services()
    pipeline = services.pipeline
    batch = services.batch

    sources = [DEMO_DIR / name for name in EXPECTED_FILES]
    sources = [p for p in sources if p.exists()]
    print(f"待处理文件 {len(sources)} 个：{[p.name for p in sources]}")
    print()

    # ---- 阶段 1：逐文件 prepare + scan（只读），打印三档裁决 ----
    _hr("阶段 1：只读扫描（三档分流）")
    hashes_before: dict[str, str] = {}
    scan_tasks = []
    for src in sources:
        print(f"\n● {src.name}")
        try:
            task = pipeline.prepare(src)
        except Exception as e:
            print(f"  prepare 失败：{type(e).__name__}: {e}")
            continue
        hashes_before[src.name] = task.source_hash
        try:
            scan = pipeline.scan(task)
        except Exception as e:
            print(f"  scan 失败：{type(e).__name__}: {e}")
            continue
        scan_tasks.append(task)
        print(f"  候选 {len(scan.candidates)} 条，review_flags={len(scan.review_flags)}")
        for c in scan.candidates:
            decision = getattr(c.decision, "value", str(c.decision))
            excluded = " [已抑制]" if c.excluded else ""
            confirmed = " [已勾选]" if c.confirmed else ""
            print(f"    - [{c.category}] {c.text!r}  conf={c.confidence:.2f}  "
                  f"决策={decision}{excluded}{confirmed}  @{c.location}")
        for w in scan.warnings:
            print(f"  ! {w}")

    # ---- 阶段 2：批量自动安全处理（模式 B）----
    print()
    _hr("阶段 2：BatchService 模式 B（一致性虚构替换）")
    result = batch.run([str(s) for s in sources], mode="b", mask="****")
    print(f"成功 passed: {[t.source_path.name for t in result.passed]}")
    print(f"复核 review : {[t.source_path.name for t in result.review]}")
    print(f"失败 failed : {[(t.source_path.name, t.status.value, (t.error or '')[:60]) for t in result.failed]}")
    print(f"跳过 skipped: {[t.source_path.name for t in result.skipped]}")
    for w in result.warnings:
        print(f"  · {w}")

    # ---- 阶段 3：原件哈希一致性 ----
    print()
    _hr("阶段 3：原件哈希一致性（失败关闭：源文件被改动即拒绝导出）")
    all_ok = True
    for src in sources:
        if src.name not in hashes_before:
            continue
        now = sha256_file(src)
        ok = now == hashes_before[src.name]
        all_ok &= ok
        print(f"  {src.name}: {'一致 ✓' if ok else '不一致 ✗'}  "
              f"sha256前8={now[:8]}")
    print(f"原件未被改动：{all_ok}")

    # ---- 阶段 4：模式 B 跨文件一致性 ----
    print()
    _hr("阶段 4：模式 B 跨文件一致性（同一实体在不同文件中替换为同一虚构值）")
    passed_names = {t.source_path.name: t for t in result.passed}
    docx_out = next((Path(t.output_path) for t in result.passed
                     if t.source_path.suffix.lower() == ".docx"), None)
    xlsx_out = next((Path(t.output_path) for t in result.passed
                     if t.source_path.suffix.lower() == ".xlsx"), None)
    ORIGINAL_COMPANY = "杭州云启科技有限公司"
    EXPECTED_FAKE = "云帆科技有限责任公司"  # _COMPANIES[0]，本批次首个公司实体分配

    if docx_out and xlsx_out:
        docx_text = _extract_docx_text(docx_out)
        xlsx_text = _extract_xlsx_text(xlsx_out)
        print(f"  DOCX 输出：{docx_out.name}")
        print(f"  XLSX 输出：{xlsx_out.name}")
        print(f"  原文公司名在 DOCX 输出中残留：{ORIGINAL_COMPANY in docx_text}（应 False）")
        print(f"  原文公司名在 XLSX 输出中残留：{ORIGINAL_COMPANY in xlsx_text}（应 False）")
        in_docx = EXPECTED_FAKE in docx_text
        in_xlsx = EXPECTED_FAKE in xlsx_text
        print(f"  同一虚构值「{EXPECTED_FAKE}」出现在 DOCX：{in_docx}；XLSX：{in_xlsx}")
        print(f"  跨文件一致性：{'一致 ✓' if (in_docx and in_xlsx) else '不一致 ✗'}")
    else:
        print("  （docx/xlsx 未全部进入成功组，跳过一致性校验）")

    # ---- 阶段 5：复核组与失败组详情 ----
    print()
    _hr("阶段 5：复核 / 失败组详情（失败关闭演示）")
    for t in result.review:
        print(f"  [REVIEW] {t.source_path.name} → {t.error or '含待复核/图文混排'}")
    for t in result.failed:
        print(f"  [FAILED] {t.source_path.name} → {t.status.value}: {t.error or ''}")

    print()
    _hr("完成")
    services.close()
    return 0


if __name__ == "__main__":
    buf = io.StringIO()
    code = 1
    try:
        with redirect_stdout(buf):
            code = main()
    finally:
        text = buf.getvalue()
        sys.stdout.write(text)
        out_file = Path(__file__).resolve().parent / "demo_output.txt"
        out_file.write_text(text, encoding="utf-8")
    sys.exit(code)
