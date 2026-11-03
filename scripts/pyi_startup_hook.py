# -*- coding: utf-8 -*-
"""PyInstaller runtime hook: persist startup tracebacks for windowed builds."""
from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path


def _write(exc_type, value, tb) -> None:
    try:
        root = Path(os.environ.get("LOCALDOCMASK_HOME") or os.environ.get("TEMP") or ".")
        root.mkdir(parents=True, exist_ok=True)
        target = root / "startup_crash.log"
        with target.open("a", encoding="utf-8") as stream:
            stream.write(f"[{datetime.now(timezone.utc).isoformat()}] ")
            stream.write("".join(traceback.format_exception(exc_type, value, tb)))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        pass


def _excepthook(exc_type, value, tb) -> None:
    _write(exc_type, value, tb)


def _thread_hook(args) -> None:
    _write(args.exc_type, args.exc_value, args.exc_traceback)


sys.excepthook = _excepthook
threading.excepthook = _thread_hook
