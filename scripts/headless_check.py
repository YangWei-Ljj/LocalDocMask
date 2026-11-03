# -*- coding: utf-8 -*-
"""Headless GUI smoke check: build MainWindow offscreen, then exit.

Used by scripts/run_headless.ps1. Does not show a real window and does not
process any business file. The launcher isolates the data dir to a temp path
via LOCALDOCMASK_HOME.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QThreadPool  # noqa: E402

import localdocmask.presentation.gui.main_window as mw  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    win = mw.MainWindow()
    QThreadPool.globalInstance().waitForDone(60000)
    app.processEvents()
    title = win.windowTitle()
    services_ok = win.services is not None
    win.shutdown()
    win.close()
    QThreadPool.globalInstance().waitForDone(10000)
    print("HEADLESS_OK title=%r services_built=%s" % (title, services_ok))
    return 0 if services_ok else 1


if __name__ == "__main__":
    sys.exit(main())
