"""全局配置：目录、Ollama 地址、默认掩码。"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _data_dir() -> Path:
    env = os.environ.get("LOCALDOCMASK_HOME")
    if env:
        return Path(env)
    return PROJECT_ROOT / "data"


DATA_DIR = _data_dir()
INPUT_DIR = DATA_DIR / "input"
PROCESSING_DIR = DATA_DIR / "processing"
OUTPUT_DIR = DATA_DIR / "output"
QUARANTINE_DIR = DATA_DIR / "quarantine"
LOG_DIR = DATA_DIR / "logs"

DEFAULT_MASK = "****"
OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.5:9b"


def ensure_dirs() -> None:
    for d in (INPUT_DIR, PROCESSING_DIR, OUTPUT_DIR, QUARANTINE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
