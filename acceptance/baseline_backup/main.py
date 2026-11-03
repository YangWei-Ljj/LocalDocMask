"""LocalDocMask 启动入口。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from localdocmask.gui import run

if __name__ == "__main__":
    run()
