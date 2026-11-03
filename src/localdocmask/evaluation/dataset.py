"""标注数据集：加载合成标注用例。

对应 module_contracts §7。LabeledCase 描述一段文本中应被识别为敏感的真实实体。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class LabeledCase:
    text: str
    true_entities: list[tuple[str, str]] = field(default_factory=list)  # (类别, 文本)
    file_type: str = "text"
    note: str = ""
    case_id: str = ""
    safety_critical: bool = False


class Dataset:
    def __init__(self, cases: list[LabeledCase] | None = None,
                 *, synthetic: bool = True, source_note: str = "",
                 suite_id: str = "", suite_version: str = ""):
        self.cases: list[LabeledCase] = cases or []
        self.synthetic = bool(synthetic)
        self.source_note = source_note
        self.suite_id = suite_id
        self.suite_version = suite_version

    @classmethod
    def load(cls, path: str | Path) -> "Dataset":
        p = Path(path)
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        meta = data.get("meta") or {}
        raw_cases = data.get("cases", [])
        cases: list[LabeledCase] = []
        for index, item in enumerate(raw_cases, 1):
            ents = [(str(c[0]), str(c[1])) for c in (item.get("true_entities") or [])]
            cases.append(LabeledCase(
                text=item["text"],
                true_entities=ents,
                file_type=item.get("file_type", "text"),
                note=item.get("note", ""),
                case_id=str(item.get("case_id") or f"case-{index:04d}"),
                safety_critical=bool(item.get("safety_critical", False)),
            ))
        return cls(
            cases,
            synthetic=bool(meta.get("synthetic", True)),
            source_note=str(meta.get("description") or ""),
            suite_id=str(meta.get("suite_id") or ""),
            suite_version=str(meta.get("suite_version") or ""),
        )

    def __len__(self) -> int:
        return len(self.cases)

    def __iter__(self):
        return iter(self.cases)
