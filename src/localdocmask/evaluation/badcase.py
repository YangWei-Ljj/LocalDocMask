"""坏例回灌：评测后导出失败用例、合并进回归集、词库回灌。

失败类型：
- FP：活跃候选未命中真值（过度脱敏/误报）。
- FN：真值实体未出现在活跃输出（漏报）。
- SUPPRESS_FAIL：真值为空的负例却输出了活跃候选（应抑制未抑制）。
- WRONG_DECISION：标注了 expected_decision 但系统三档决策不符。

安全约束：
- merge_badcases / feedback_lexicon 默认 dry_run=True，仅打印预览；
  必须显式 dry_run=False 才写盘，且只追加、不覆盖既有内容。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .dataset import Dataset, LabeledCase
from .runner import EvaluationRunner


@dataclass
class BadCase:
    text: str
    failure_type: str            # FP/FN/SUPPRESS_FAIL/WRONG_DECISION
    expected: list[list[str]] = field(default_factory=list)
    expected_decision: str = ""
    system_candidates: list[dict] = field(default_factory=list)
    root_cause: str = ""          # 人工根因备注
    note: str = ""


def _cand_dict(c) -> dict:
    return {
        "category": c.category, "text": c.text,
        "decision": c.decision.value if c.decision else "",
        "excluded": c.excluded, "source": c.source,
        "confidence": round(c.confidence, 3),
    }


def collect_badcases(dataset: Dataset,
                     runner: EvaluationRunner | None = None) -> list[BadCase]:
    """复跑评测并收集所有失败用例（与 runner 同一口径）。"""
    runner = runner or EvaluationRunner()
    bad: list[BadCase] = []
    for case in dataset:
        out = runner.pipeline.analyze(case.text, location="eval")
        active = [c for c in out if not c.excluded]
        truth = {(cat, txt.strip()) for cat, txt in case.true_entities}
        matched: set[tuple[str, str]] = set()
        fp_items: list[dict] = []
        for c in active:
            key = (c.category, c.text.strip())
            if key in truth:
                matched.add(key)
            else:
                fp_items.append(_cand_dict(c))
        fn_items = [[cat, txt] for cat, txt in truth if key_not_match(cat, txt, matched)]
        truth_empty = not truth

        types: list[str] = []
        if fp_items:
            types.append("FP")
        if fn_items:
            types.append("FN")
        if truth_empty and active:
            types.append("SUPPRESS_FAIL")

        if types:
            bad.append(BadCase(
                text=case.text,
                failure_type=",".join(types),
                expected=[[c, t] for c, t in truth] + fn_items,
                system_candidates=[_cand_dict(c) for c in out],
                note=case.note,
            ))
    return bad


def key_not_match(cat: str, txt: str, matched: set) -> bool:
    return (cat, txt.strip()) not in matched


def export_badcases(bad: list[BadCase], out_path: str | Path) -> None:
    payload = {
        "meta": {"description": "坏例清单：人工确认 root_cause 后再合并/回灌。",
                 "schema": "badcase-v1"},
        "badcases": [asdict(b) for b in bad],
    }
    Path(out_path).write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8")


def load_badcases(path: str | Path) -> list[BadCase]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [BadCase(**b) for b in data.get("badcases", [])]


def merge_badcases(bad: list[BadCase], target_yaml: str | Path,
                   dry_run: bool = True) -> dict:
    """把确认的坏例合并进回归集（按 text 去重）。返回动作预览。"""
    target = Path(target_yaml)
    existing = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    cases = existing.get("cases", []) or []
    existing_texts = {c.get("text") for c in cases}
    added = 0
    for b in bad:
        if b.text in existing_texts:
            continue
        cases.append({
            "text": b.text,
            "true_entities": b.expected,
            "note": f"badcase[{b.failure_type}] {b.root_cause}".strip(),
            "provenance": {"badcase": True, "failure_type": b.failure_type},
        })
        existing_texts.add(b.text)
        added += 1
    action = {"target": str(target), "added": added, "total": len(cases),
              "dry_run": dry_run}
    if not dry_run:
        existing["cases"] = cases
        target.write_text(yaml.safe_dump(existing, allow_unicode=True,
                                         sort_keys=False), encoding="utf-8")
    return action


# 类别 -> 词库文件（仅用于确定性回灌建议）
_CATEGORY_TO_LEXICON = {
    "公司名": "blacklist.txt",
    "自定义敏感词": "domain_terms.txt",
}


def feedback_lexicon(bad: list[BadCase], resources_dir: str | Path,
                     dry_run: bool = True) -> dict:
    """把确认的实体回灌到 resources 词库（仅追加、去重、不覆盖）。

    规则：FN 且类别为公司名/自定义敏感词 -> 追加到 blacklist/domain；
    FP 且为公开机构名 -> 建议加入 public_entities；FP 且为泛指业务词 -> whitelist。
    真实判定仍需人工确认，这里只做可预览的建议。
    """
    rdir = Path(resources_dir)
    plan: dict[str, list[str]] = {}
    for b in bad:
        ftypes = set(b.failure_type.split(","))
        for cat, txt in b.expected:
            txt = (txt or "").strip()
            if not txt:
                continue
            if "FN" in ftypes and cat in _CATEGORY_TO_LEXICON:
                plan.setdefault(_CATEGORY_TO_LEXICON[cat], []).append(txt)
        if "FP" in ftypes:
            for c in b.system_candidates:
                txt = c["text"].strip()
                plan.setdefault("whitelist.txt", []).append(txt + "  # FP 候选，人工确认")

    written: dict[str, int] = {}
    for fname, terms in plan.items():
        fpath = rdir / fname
        existing = set()
        if fpath.exists():
            existing = {ln.split("#", 1)[0].strip()
                        for ln in fpath.read_text(encoding="utf-8").splitlines()}
        new = [t for t in terms if t.split("#", 1)[0].strip() not in existing]
        if not new:
            continue
        written[fname] = len(new)
        if not dry_run:
            with fpath.open("a", encoding="utf-8") as fh:
                for t in new:
                    fh.write(t + "\n")
    return {"resources_dir": str(rdir), "dry_run": dry_run,
            "plan": {k: sorted(set(v)) for k, v in plan.items()},
            "would_append": written}


__all__ = ["BadCase", "collect_badcases", "export_badcases", "load_badcases",
           "merge_badcases", "feedback_lexicon"]
