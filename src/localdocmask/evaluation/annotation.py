"""标注导入器：从真实/合成文件提取文本，按句子切分生成待标注草稿。

设计原则（见 docs/guides/corpus_annotation_manual.md）：
- 本地优先：仅读取本机文件，不外发；OCR/解析全部走本地 formats 处理器。
- 草稿含 provenance：来源文件、合成/真实标记、句子定位；人工填真值后可被
  ``Dataset.load`` 直接加载（``expected`` 即 ``true_entities``）。
- 不静默改动用户词库/回归集；合并与回灌由 badcase.py 显式 dry-run。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from ..infrastructure.formats.registry import handler_for, supported_extensions
from ..recognition.rule_based import Detector


@dataclass
class Provenance:
    source_file: str = ""          # 来源文件路径（仅文件名相对，避免泄露本机绝对路径）
    synthetic: bool = True          # 合成/真实标记
    file_type: str = "text"        # docx/pdf/xlsx/image/text
    sentence_index: int = -1       # 句序
    span: list[int] = field(default_factory=lambda: [0, 0])  # 在全文中的字符区间


@dataclass
class AnnotationDraft:
    text: str
    location: str = ""
    expected: list[list[str]] = field(default_factory=list)   # [[类别,文本],...] 人工填
    expected_decision: str = ""     # auto_confirm/needs_review/suppress，可空
    note: str = ""
    provenance: dict = field(default_factory=dict)


def extract_text(path: str | Path) -> tuple[str, list[str]]:
    """复用 formats 处理器提取纯文本（图片走本地 OCR）。返回 (text, warnings)。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    ext = p.suffix.lower()
    if ext in supported_extensions():
        handler = handler_for(p, Detector())
        result = handler.extract(str(p))
        return result.text, list(result.warnings)
    # 纯文本兜底
    return p.read_text(encoding="utf-8", errors="ignore"), []


_SENT_END = re.compile(r"(?<=[。！？；!?;\n])")


def split_sentences(text: str) -> list[tuple[str, int, int]]:
    """按句末标点/换行切分，返回 (句子, start, end)。空段丢弃。"""
    out: list[tuple[str, int, int]] = []
    pos = 0
    for chunk in _SENT_END.split(text or ""):
        if not chunk:
            continue
        start = pos
        end = pos + len(chunk)
        s = chunk.strip()
        if s:
            out.append((s, start, end))
        pos = end
    return out


def _file_type_of(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    return {"docx": "docx", "pdf": "pdf", "xlsx": "xlsx", "xlsm": "xlsx",
            "png": "image", "jpg": "image", "jpeg": "image",
            "tif": "image", "tiff": "image", "bmp": "image"}.get(ext, "text")


def build_draft(path: str | Path, synthetic: bool = True,
                location_prefix: str = "") -> list[AnnotationDraft]:
    """从单个文件生成待标注草稿列表。"""
    p = Path(path)
    text, warnings = extract_text(p)
    ftype = _file_type_of(p)
    drafts: list[AnnotationDraft] = []
    for i, (sent, start, end) in enumerate(split_sentences(text)):
        prov = Provenance(source_file=p.name, synthetic=synthetic,
                          file_type=ftype, sentence_index=i, span=[start, end])
        drafts.append(AnnotationDraft(
            text=sent,
            location=f"{location_prefix}{p.name}#{i}",
            provenance=asdict(prov),
            note=("合成样本，不能替代真实语料。" if synthetic else "真实文件，请本地标注。"),
        ))
    return drafts


def write_draft(drafts: list[AnnotationDraft], out_path: str | Path) -> None:
    """把草稿写成 YAML，人工在 expected 列填真值。"""
    payload = {
        "meta": {
            "description": "待标注草稿：请为每条填写 expected=[[类别,文本],...] 与 expected_decision。",
            "warning": "合成样本不能替代真实语料；真实文件请在本地标注，勿外发。",
            "schema": "draft-v1",
        },
        "cases": [asdict(d) for d in drafts],
    }
    Path(out_path).write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8")


def load_labeled(path: str | Path) -> list[AnnotationDraft]:
    """加载已标注草稿（expected 已填）。兼容 labeled_cases.yaml 的 cases 结构。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: list[AnnotationDraft] = []
    for item in data.get("cases", []):
        expected = item.get("expected")
        if expected is None:
            expected = [[c, t] for c, t in (item.get("true_entities") or [])]
        out.append(AnnotationDraft(
            text=item["text"],
            location=item.get("location", ""),
            expected=expected,
            expected_decision=item.get("expected_decision", ""),
            note=item.get("note", ""),
            provenance=item.get("provenance", {}),
        ))
    return out


__all__ = ["Provenance", "AnnotationDraft", "extract_text", "split_sentences",
           "build_draft", "write_draft", "load_labeled", "supported_extensions"]
