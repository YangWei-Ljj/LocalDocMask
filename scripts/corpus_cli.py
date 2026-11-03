# -*- coding: utf-8 -*-
"""语料闭环统一 CLI：draft / eval / badcases / merge / feedback / trend / gen。

全部本地运行，不外发。合并与词库回灌默认 dry-run 预览，显式 --apply 才写盘。

用法（项目根目录，.venv 激活后）：
  python scripts/corpus_cli.py gen --n 100 --out eval_cases.yaml
  python scripts/corpus_cli.py eval --data src/localdocmask/evaluation/data/labeled_cases.yaml --results evaluation/results
  python scripts/corpus_cli.py badcases --data <yaml> --out badcases.yaml
  python scripts/corpus_cli.py merge --bad badcases.yaml --target regression.yaml [--apply]
  python scripts/corpus_cli.py feedback --bad badcases.yaml --resources resources [--apply]
  python scripts/corpus_cli.py trend --results evaluation/results
  python scripts/corpus_cli.py draft --file contract.docx --out draft.yaml [--real]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localdocmask.evaluation import annotation, badcase, trend, synthetic_corpus  # noqa: E402
from localdocmask.evaluation.dataset import Dataset, LabeledCase  # noqa: E402
from localdocmask.evaluation.runner import EvaluationRunner  # noqa: E402


def _load(data: str) -> Dataset:
    return Dataset.load(data)


def cmd_gen(a):
    cases = synthetic_corpus.generate(a.n)
    doc = {"meta": {"synthetic": True,
                    "warning": "合成样本，不能替代真实语料"},
           "cases": [{"text": c.text, "true_entities": [list(t) for t in c.true_entities],
                      "note": c.note, "file_type": "synthetic"} for c in cases]}
    Path(a.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"generated {len(cases)} synthetic cases -> {a.out}")


def cmd_eval(a):
    ds = _load(a.data)
    r = EvaluationRunner().run(ds)
    p = trend.save_result(r, a.results,
                          dataset_name=Path(a.data).stem, note=a.note or "")
    print(json.dumps({"saved": str(p), "cases": r["cases"],
                     "overall": r["overall"], "decision_counts": r["decision_counts"]},
                    ensure_ascii=False, indent=2))


def cmd_bad(a):
    ds = _load(a.data)
    bcs = badcase.collect_badcases(ds)
    badcase.export_badcases(bcs, a.out)
    print(f"exported {len(bcs)} badcases -> {a.out}")


def cmd_merge(a):
    bcs = badcase.load_badcases(a.bad)
    res = badcase.merge_badcases(bcs, a.target, dry_run=not a.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_feedback(a):
    bcs = badcase.load_badcases(a.bad)
    res = badcase.feedback_lexicon(bcs, a.resources, dry_run=not a.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_trend(a):
    hist = trend.load_history(a.results)
    print(f"history rows: {len(hist)}")
    if len(hist) >= 2:
        cmp = trend.compare(hist[-2], hist[-1])
        print(json.dumps(cmp, ensure_ascii=False, indent=2))
    elif hist:
        print(json.dumps(hist[-1].get("overall"), ensure_ascii=False, indent=2))


def cmd_draft(a):
    drafts = annotation.build_draft(a.file, synthetic=not a.real)
    annotation.write_draft(drafts, a.out)
    print(f"drafted {len(drafts)} sentences -> {a.out}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen"); g.add_argument("--n", type=int, default=100)
    g.add_argument("--out", default="eval_cases.json"); g.set_defaults(fn=cmd_gen)

    e = sub.add_parser("eval"); e.add_argument("--data", required=True)
    e.add_argument("--results", default="evaluation/results"); e.add_argument("--note", default="")
    e.set_defaults(fn=cmd_eval)

    b = sub.add_parser("badcases"); b.add_argument("--data", required=True)
    b.add_argument("--out", default="badcases.yaml"); b.set_defaults(fn=cmd_bad)

    m = sub.add_parser("merge"); m.add_argument("--bad", required=True)
    m.add_argument("--target", required=True); m.add_argument("--apply", action="store_true")
    m.set_defaults(fn=cmd_merge)

    f = sub.add_parser("feedback"); f.add_argument("--bad", required=True)
    f.add_argument("--resources", default="resources"); f.add_argument("--apply", action="store_true")
    f.set_defaults(fn=cmd_feedback)

    t = sub.add_parser("trend"); t.add_argument("--results", default="evaluation/results")
    t.set_defaults(fn=cmd_trend)

    d = sub.add_parser("draft"); d.add_argument("--file", required=True)
    d.add_argument("--out", default="draft.yaml"); d.add_argument("--real", action="store_true")
    d.set_defaults(fn=cmd_draft)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
