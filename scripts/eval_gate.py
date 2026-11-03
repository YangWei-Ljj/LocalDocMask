# -*- coding: utf-8 -*-
"""LocalDocMask 版本化评测与发布门禁 CLI。全部本地运行，不访问网络。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from localdocmask.evaluation.gate import (  # noqa: E402
    EvalGateError,
    run_eval_gate,
    update_baseline_from_report,
    write_json_report,
    write_markdown_report,
)
from localdocmask.evaluation.suite import (  # noqa: E402
    DEFAULT_SUITES_DIR,
    list_suites,
    manifest_path_for,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LocalDocMask 评测完整性检查与发布门禁")
    parser.add_argument("--suite", help="suite_id 或套件目录")
    parser.add_argument("--baseline", help="基线 JSON 路径")
    parser.add_argument("--report", help="JSON 报告输出路径")
    parser.add_argument("--markdown-report", help="可选 Markdown 报告输出路径")
    parser.add_argument("--suites-dir", default=str(DEFAULT_SUITES_DIR))
    parser.add_argument("--list-suites", action="store_true", help="列出套件及 ready 状态")
    parser.add_argument("--dry-run", action="store_true", help="只检查套件完整性和哈希")
    parser.add_argument("--update-baseline", action="store_true",
                        help="显式把本次通过完整性检查的结果更新为基线")
    parser.add_argument("--reason", default="", help="更新基线时必须说明原因")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list_suites:
        print(json.dumps(list_suites(args.suites_dir), ensure_ascii=False, indent=2))
        return 0
    if not args.suite or not args.report:
        print("error: --suite and --report are required unless --list-suites is used",
              file=sys.stderr)
        return 2
    if not args.dry_run and not args.baseline:
        print("error: --baseline is required unless --dry-run is used", file=sys.stderr)
        return 2
    if args.update_baseline and not args.baseline:
        print("error: --baseline is required with --update-baseline", file=sys.stderr)
        return 2
    if args.update_baseline and not args.reason.strip():
        print("error: --reason is required with --update-baseline", file=sys.stderr)
        return 2

    try:
        report = run_eval_gate(
            suite=args.suite,
            baseline_path=args.baseline,
            suites_dir=args.suites_dir,
            dry_run=args.dry_run,
        )
        update_result = None
        if args.update_baseline:
            update_result = update_baseline_from_report(
                report=report,
                baseline_path=args.baseline,
                reason=args.reason,
                suite_manifest_path=manifest_path_for(args.suite, args.suites_dir),
            )
        write_json_report(report, args.report)
        if args.markdown_report:
            write_markdown_report(report, args.markdown_report)
    except EvalGateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("error: eval_gate_internal_failure", file=sys.stderr)
        return 2

    summary = {
        "suite_id": report.get("suite_id"),
        "execution_status": report.get("execution_status"),
        "quality_status": report.get("quality_status"),
        "gate_status": report.get("gate_status"),
        "failure_reasons": report.get("failure_reasons") or [],
        "report": str(Path(args.report)),
    }
    if args.markdown_report:
        summary["markdown_report"] = str(Path(args.markdown_report))
    if update_result:
        summary["baseline_update"] = update_result
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report.get("gate_status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
