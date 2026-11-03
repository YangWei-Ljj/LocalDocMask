"""测试公共配置。"""
import gc
import os
import sys
import tempfile
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtWidgets import QApplication

# 将测试运行时数据隔离到临时目录，避免污染项目 data/ 目录
_TEST_DATA = tempfile.mkdtemp(prefix="localdocmask_test_")
os.environ["LOCALDOCMASK_HOME"] = _TEST_DATA
# 旧测试会注入局部、不可跨 spawn 序列化的 handler；生产代码不设置此开关。
os.environ["LOCALDOCMASK_TEST_ALLOW_INPROCESS_HANDLERS"] = "1"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

VALID_ID = "11010519491231002X"


def luhn_valid(base: str) -> str:
    digits = [int(c) for c in base]
    for i in range(len(digits) - 1, -1, -2):
        digits[i] *= 2
        if digits[i] > 9:
            digits[i] -= 9
    return base + str((10 - sum(digits) % 10) % 10)


@pytest.fixture(scope="session")
def qapp():
    """单进程测试共享唯一 QApplication，并显式清理 Qt 原生对象。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app

    QThreadPool.globalInstance().waitForDone(60000)
    app.closeAllWindows()
    app.quit()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    app.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()

    try:
        from localdocmask.infrastructure.ocr.engine import OCREngine
        OCREngine._engine = None
    except Exception:
        pass
    gc.collect()
