#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LocalDocMask 环境自检脚本（只读诊断，不发送任何业务数据）。

检查项：
  1. Python 版本（要求 3.11.x）
  2. 项目虚拟环境 .venv 是否存在
  3. 关键依赖能否 import 及其版本
  4. Ollama 是否安装 / 运行，模型 qwen3.5:9b 是否存在
  5. 配置文件 config/config.yaml 是否就位
  6. 词库 resources/*.txt 是否就位
  7. 数据目录 data/ 及子目录是否存在
  8. 数据目录磁盘可写性

输出每项 OK / WARN / FAIL 与修复建议；存在 FAIL 时以非零码退出。
本脚本只做本地检查（含一次本地 `ollama list`），不上传任何文件或数据。

用法：
    .venv\\Scripts\\python.exe scripts\\diagnose_env.py
"""
from __future__ import annotations

import importlib
import importlib.metadata as md
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_PY = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

#: (import 名, 分发包名, 期望最低说明)
DEPS = [
    ("PySide6.QtCore", "PySide6", "6.12.0"),
    ("fitz", "PyMuPDF", "2.0"),
    ("docx", "python-docx", "1.2.0"),
    ("openpyxl", "openpyxl", "3.1.5"),
    ("PIL", "Pillow", "13.0.0"),
    ("cv2", "opencv-python", "5.0.0.93"),
    ("rapidocr_onnxruntime", "rapidocr-onnxruntime", "1.4.4"),
    ("onnxruntime", "onnxruntime", "1.31.0"),
    ("requests", "requests", "2.34.2"),
    ("yaml", "pyyaml", "6.0.3"),
    ("numpy", "numpy", "2.4.6"),
    ("lxml", "lxml", "6.1.3"),
]

REQUIRED_MODEL = "qwen3.5:9b"

#: 数据子目录（与 infrastructure/config/settings.py 的 ensure_dirs 一致）
EXPECTED_DATA_DIRS = [
    "input",
    "processing",
    os.path.join("output", "success"),
    os.path.join("output", "quarantine"),
    "logs",
]

REQUIRED_RESOURCES = [
    "whitelist.txt",
    "blacklist.txt",
    "public_entities.txt",
    "domain_terms.txt",
]

results: list[tuple[str, str, str]] = []  # (level, item, detail+fix)


def record(level: str, item: str, detail: str) -> None:
    results.append((level, item, detail))


def check_python() -> None:
    info = sys.version_info
    if info.major == 3 and info.minor == 11:
        record("OK", "Python 版本", f"Python {info.major}.{info.minor}.{info.micro}（要求 3.11.x）")
    else:
        record(
            "FAIL",
            "Python 版本",
            f"当前 {info.major}.{info.minor}.{info.micro}，项目要求 3.11.x。"
            "修复：安装 Python 3.11 后重建 .venv。",
        )


def check_venv() -> None:
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if VENV_PY.exists():
        record("OK", "虚拟环境", f".venv 存在（{VENV_PY.name}）；" + ("当前解释器位于 venv 内" if in_venv else "WARN：当前未使用 .venv 解释器运行本脚本"))
        if not in_venv:
            record("WARN", "运行解释器", "请用 .venv\\Scripts\\python.exe 运行，避免与系统环境混淆。")
    else:
        record("FAIL", "虚拟环境", "未找到 .venv。修复：uv venv .venv && uv pip install -r requirements.txt")


def check_deps() -> None:
    for mod_name, dist, expect in DEPS:
        try:
            mod = importlib.import_module(mod_name)
            ver = getattr(mod, "__version__", None)
            if not ver:
                try:
                    ver = md.version(dist)
                except md.PackageNotFoundError:
                    ver = "未知版本"
            record("OK", f"依赖 {dist}", f"import {mod_name} 成功，版本 {ver}（期望 {expect}）")
        except Exception as e:  # noqa: BLE001 - 诊断脚本要兜住一切
            record(
                "FAIL",
                f"依赖 {dist}",
                f"import {mod_name} 失败：{type(e).__name__}: {e}。"
                f"修复：uv pip install \"{dist}=={expect}\"",
            )


def check_ollama() -> None:
    exe = shutil.which("ollama")
    if not exe:
        record(
            "WARN",
            "Ollama",
            "未找到 ollama 可执行文件。本项目 LLM 增强默认关闭（ollama.enabled=false），"
            "无 Ollama 也可运行；如需启用：安装 Ollama（https://ollama.com）并 `ollama pull "
            f"{REQUIRED_MODEL}`。",
        )
        return
    record("OK", "Ollama", f"已安装：{exe}")
    try:
        proc = subprocess.run(
            [exe, "list"], capture_output=True, text=True, timeout=15, encoding="utf-8",
            errors="replace",
        )
    except Exception as e:  # noqa: BLE001
        record("WARN", "Ollama 运行状态", f"`ollama list` 执行失败：{e}。可能 Ollama 服务未启动。修复：启动 Ollama 后重试。")
        return
    if proc.returncode != 0:
        record(
            "WARN",
            "Ollama 运行状态",
            f"`ollama list` 退出码 {proc.returncode}：{(proc.stderr or '').strip()[:200]}。"
            "通常表示 Ollama 服务未运行。修复：启动 Ollama 后重试。",
        )
        return
    record("OK", "Ollama 运行状态", "`ollama list` 正常返回（服务可达）")
    models = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if parts:
            models.append(parts[0])
    if REQUIRED_MODEL in models:
        record("OK", "Ollama 模型", f"已存在 {REQUIRED_MODEL}（共 {len(models)} 个本地模型）")
    else:
        record(
            "WARN",
            "Ollama 模型",
            f"未找到模型 {REQUIRED_MODEL}（现有：{', '.join(models) or '无'}）。"
            f"修复：`ollama pull {REQUIRED_MODEL}`。注意：不启用 LLM 增强时此项非必需。",
        )


def check_config() -> None:
    example = PROJECT_ROOT / "config" / "config.example.yaml"
    cfg = PROJECT_ROOT / "config" / "config.yaml"
    if not example.exists():
        record("FAIL", "配置模板", f"缺少 config/config.example.yaml（仓库应包含）。")
        return
    record("OK", "配置模板", "config/config.example.yaml 存在")
    if cfg.exists():
        record("OK", "本机配置", "config/config.yaml 已就位（已 gitignore，不会误提交）")
    else:
        record(
            "WARN",
            "本机配置",
            "config/config.yaml 不存在，将全部使用安全默认值。"
            "修复：copy config\\config.example.yaml config\\config.yaml 后按需修改。",
        )


def check_resources() -> None:
    res_dir = PROJECT_ROOT / "resources"
    missing = [f for f in REQUIRED_RESOURCES if not (res_dir / f).exists()]
    if missing:
        record("FAIL", "词库资源", f"resources/ 缺少：{', '.join(missing)}（识别功能将不完整）。")
    else:
        record("OK", "词库资源", f"resources/ 下 {len(REQUIRED_RESOURCES)} 个词库文件齐全")


def _resolve_data_dir() -> Path:
    env = os.environ.get("LOCALDOCMASK_HOME")
    if env:
        return Path(env)
    # 与 settings.py 一致：config.yaml 中的 paths.data_dir（仅做存在性判断）
    return PROJECT_ROOT / "data"


def check_data_dir() -> None:
    data_dir = _resolve_data_dir()
    src = "LOCALDOCMASK_HOME" if os.environ.get("LOCALDOCMASK_HOME") else "默认 <项目根>/data"
    if not data_dir.exists():
        record("WARN", "数据目录", f"{data_dir} 不存在（来源：{src}）。首次启动会自动创建。")
        return
    record("OK", "数据目录", f"{data_dir}（来源：{src}）")
    missing = [d for d in EXPECTED_DATA_DIRS if not (data_dir / d).is_dir()]
    if missing:
        record("WARN", "数据子目录", f"缺少子目录：{', '.join(missing)}。首次启动 ensure_dirs() 会自动补齐。")
    else:
        record("OK", "数据子目录", "input/ processing/ output/success/ output/quarantine/ logs/ 均存在")


def check_writable() -> None:
    root = _resolve_data_dir()
    target = root if root.exists() else Path(tempfile.gettempdir())
    try:
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target, prefix="lm_diag_", delete=True) as f:
            f.write(b"probe")
            f.flush()
        record("OK", "磁盘可写", f"可在 {target} 写入临时文件")
    except Exception as e:  # noqa: BLE001
        record("FAIL", "磁盘可写", f"无法在 {target} 写入：{e}。修复：检查目录权限/磁盘空间。")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    print("=" * 72)
    print(f"LocalDocMask 环境诊断  项目根: {PROJECT_ROOT}")
    print("=" * 72)
    check_python()
    check_venv()
    check_deps()
    check_ollama()
    check_config()
    check_resources()
    check_data_dir()
    check_writable()

    width = max(len(i) for _, i, _ in results)
    for level, item, detail in results:
        print(f"[{level:<4}] {item:<{width}}  {detail}")

    fails = [r for r in results if r[0] == "FAIL"]
    warns = [r for r in results if r[0] == "WARN"]
    print("-" * 72)
    print(f"汇总：{sum(1 for r in results if r[0] == 'OK')} OK / {len(warns)} WARN / {len(fails)} FAIL")
    if fails:
        print("存在 FAIL 项，请按上述修复建议处理后重试。")
        return 1
    if warns:
        print("无致命问题；WARN 项均为可选项或首次启动自愈项，可正常启动。")
    else:
        print("环境全部就绪。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
