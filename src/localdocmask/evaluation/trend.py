"""趋势追踪：评测结果按时间落盘，支持本次 vs 上次对比，发现"改 A 坏 B"。

结果默认写到 evaluation/results/（含文本候选，可能含敏感内容）：
- 默认不入库（建议加入 .gitignore），文件名带时间戳与数据集名。
- compare() 输出 P/R/F1、TP/FP/FN、复核率的 delta。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def save_result(report: dict, results_dir: str | Path,
                dataset_name: str = "labeled", note: str = "") -> Path:
    """把一次评测报告落盘为 JSON（带时间戳）。"""
    rdir = Path(results_dir)
    rdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": _ts(),
        "dataset_name": dataset_name,
        "note": note,
        "cases": report.get("cases"),
        "overall": report.get("overall"),
        "decision_counts": report.get("decision_counts"),
        "suppress": report.get("suppress"),
        "per_type": report.get("per_type"),
    }
    # 文件名带时间戳 + 序号，避免同微秒覆盖
    seq = len(list(rdir.glob(f"eval_{dataset_name}_*.json")))
    out = rdir / f"eval_{dataset_name}_{_ts()}_{seq:02d}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return out


def load_history(results_dir: str | Path) -> list[dict]:
    rdir = Path(results_dir)
    rows = []
    for fp in sorted(rdir.glob("eval_*.json")):
        try:
            rows.append(json.loads(fp.read_text(encoding="utf-8")))
        except Exception:
            continue
    return rows


def _overall(r: dict) -> dict:
    return (r or {}).get("overall") or {}


def compare(prev: dict | None, curr: dict) -> dict:
    """本次 vs 上次：返回关键指标 delta（curr - prev）。"""
    po, co = _overall(prev), _overall(curr)
    def d(key: str) -> float:
        return round(float(co.get(key, 0)) - float(po.get(key, 0)), 4)
    prev_dec = (prev or {}).get("decision_counts") or {}
    curr_dec = curr.get("decision_counts") or {}
    return {
        "prev_timestamp": (prev or {}).get("timestamp"),
        "curr_timestamp": curr.get("timestamp"),
        "delta": {k: d(k) for k in ("tp", "fp", "fn", "precision", "recall", "f1")},
        "review_rate": {
            "prev": _rate(prev_dec), "curr": _rate(curr_dec),
        },
        "regression_flag": (d("f1") < -0.001) or (d("fp") > 0) or (d("fn") > 0),
    }


def _rate(dec: dict) -> float:
    total = sum(dec.values())
    return round(dec.get("needs_review", 0) / total, 4) if total else 0.0


__all__ = ["save_result", "load_history", "compare"]
