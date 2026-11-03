# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

datas = []
binaries = []
hiddenimports = []

# OCR 的配置文件、字典和 ONNX 模型随包收集；运行时代码由依赖图自动分析。
datas += collect_data_files("rapidocr_onnxruntime")
datas += collect_data_files("pyclipper")
hiddenimports += [
    "onnxruntime.capi._pybind_state",
    "localdocmask.application.process_guard",
]

# 仅收集已批准、可追溯的本地 NER 模型；不得退回未批准副本。
_APPROVED_NER_DIR = Path(SPECPATH) / "models" / "ner_bert4ner-approved"
_NER_BUNDLE_FILES = (
    "model.onnx",
    "model.onnx.data",
    "model.safetensors",
    "vocab.txt",
    "id2label.json",
    "config.json",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "LICENSE",
    "NOTICE",
    "SOURCE.md",
    "PROVENANCE.json",
)
_missing_ner_files = [name for name in _NER_BUNDLE_FILES
                      if not (_APPROVED_NER_DIR / name).is_file()]
if _missing_ner_files:
    raise SystemExit(
        "approved NER model files missing: " + ", ".join(_missing_ner_files))
datas += [
    (str(_APPROVED_NER_DIR / name), "resources/ner_bert4ner")
    for name in _NER_BUNDLE_FILES
]

a = Analysis(
    ["main.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["scripts/pyi_startup_hook.py"],
    excludes=[
        "torch", "torchvision", "torchaudio", "transformers", "tensorflow",
        "pytest", "setuptools", "wheel", "IPython", "matplotlib", "pandas",
        "sklearn", "pymupdf.layout", "onnx.quantization", "onnxruntime.quantization",
        "onnxruntime.tools",
    ],
    noarchive=False,
    optimize=0,
)
# Do not ship development-machine poppler ICU DLLs. Qt6Core must resolve the
# Windows system icuuc.dll stable API instead of versioned ICU 78 symbols.
a.binaries = [entry for entry in a.binaries
              if not (entry[0].replace("\\", "/").split("/")[-1].lower() == "icuuc.dll"
                      or entry[0].replace("\\", "/").split("/")[-1].lower().startswith("icudt"))]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalDocMask",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LocalDocMask",
)
