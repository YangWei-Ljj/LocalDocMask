# LocalDocMask 开发与部署指南

面向新开发者：从零准备环境到本地运行、测试、诊断与打包的完整流程。
项目为 src 布局（`src/localdocmask/...`），启动入口 `main.py`，Python 3.11.x。

---

## 1. 环境准备（一次性）

| 组件 | 要求 | 说明 |
| --- | --- | --- |
| 操作系统 | Windows 10/11（64 位） | GUI 基于 PySide6；脚本为 PowerShell |
| Python | **3.11.x**（如 3.11.16） | 不要用 3.12+，依赖未验证 |
| uv | ≥ 0.5（已验证 0.12.5） | 包/虚拟环境管理，加入 PATH |
| Ollama（可选） | 最新版 | 仅用于 LLM 增强；默认关闭，不装也能跑 |

安装 uv（PowerShell）：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

> 本项目不使用系统全局 Python 装包，全部依赖装进仓库内 `.venv`（已 gitignore）。

## 2. 创建虚拟环境与安装

在仓库根目录执行：

```powershell
# 1) 用项目要求的 Python 建 venv（uv 会自动下载 3.11）
uv venv .venv --python 3.11

# 2a) 推荐：按锁文件精确复现（与已验证环境逐版本一致），含 dev 依赖
uv sync

# 2b) 备选：按 requirements.txt 安装
uv pip install -r requirements.txt
```

两种方式二选一。`uv sync` 以 `uv.lock` 为准（可复现性最好）；`requirements.txt` 适合不使用 uv 的机器（`python -m pip install -r requirements.txt`，需先自行 ensurepip）。

> 如需对本包做 editable 安装（改代码即生效、生成 `localdocmask` 命令）：
> `uv pip install -e .`（pyproject.toml 已配置 src 布局）。日常开发直接用 `main.py` 启动即可，不必 editable。

## 3. 配置

```powershell
copy config\config.example.yaml config\config.yaml
```

`config/config.yaml` 已被 gitignore，不会误提交。缺省全部走安全默认：

- Ollama **默认关闭**（`ollama.enabled: false`），仅回环地址；
- 一致性映射默认仅内存不落盘；
- 数据目录默认 `<项目根>/data`，可用环境变量 `LOCALDOCMASK_HOME` 覆盖（优先级最高）。

修改 `config.yaml` 后重启生效；非法值（非回环 Ollama 地址、置信度越界等）会在启动时显式报错。

## 4. 启动

- 图形界面：双击 `启动.bat`，或
  ```powershell
  .\.venv\Scripts\python.exe main.py
  ```
- 模块方式：`.venv\Scripts\python.exe -m localdocmask`

## 5. 测试

```powershell
# 全量（约 50s，当前基线：157 passed, 1 skipped，共 158 用例）
.\scripts\run_tests.ps1
# 等价： .\.venv\Scripts\python.exe -m pytest tests -q

# 单个文件 / 加详细输出
.\scripts\run_tests.ps1 tests\test_masker.py -v
```

GUI 冒烟（离屏构造主窗口后自动退出，用于无显示环境/打包后自检）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_headless.ps1
# 成功输出： HEADLESS_OK title='LocalDocMask - 本地文件脱敏工作台' services_built=True
```

## 6. 环境诊断

```powershell
.\.venv\Scripts\python.exe scripts\diagnose_env.py
```

逐项输出 `OK / WARN / FAIL` 与修复建议，覆盖：Python 版本、.venv、12 个关键依赖的 import 与版本、Ollama 是否安装/运行、模型 `qwen3.5:9b` 是否存在、配置模板与本机配置、词库资源、数据目录及子目录、磁盘可写。存在 FAIL 时以退出码 1 结束。诊断只做本地检查，不上传任何业务数据。

当前本机实测：**22 OK / 1 WARN（缺 config.yaml，走默认值）/ 0 FAIL**。

## 7. 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `ModuleNotFoundError: localdocmask` | 未从仓库根启动 / 未加 src 到路径 | 用 `main.py` 或 `python -m localdocmask`；不要直接运行 src 内单文件 |
| `ModuleNotFoundError: No module named 'PySide6'` 等 | venv 未激活或依赖未装 | 确认用 `.venv\Scripts\python.exe`；`uv sync` 重装 |
| `QT_QPA_PLATFORM` 插件错误 | 无显示环境 | CI/服务器上先 `$env:QT_QPA_PLATFORM="offscreen"` |
| Ollama 相关 WARN | 未装/未启动/缺模型 | 可选：`ollama serve` 后 `ollama pull qwen3.5:9b`；或保持 `enabled: false` |
| 端口/网络报错 | 配置了非回环 Ollama 地址 | settings 强制白名单仅回环，改回 `http://127.0.0.1:11434` |
| 数据写不进 | data/ 权限或磁盘满 | 以管理员/检查磁盘；或设 `LOCALDOCMASK_HOME` 到可写目录 |
| fitz FutureWarning | PyMuPDF 2.x 弃用 `import fitz` | 仅警告，不影响运行 |

## 8. 打包（PyInstaller）

前置：`.venv` 已装好全部依赖；构建机会产出约 1–2 GB（PySide6 + onnxruntime + OpenCV + OCR 模型）。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_package.ps1
```

脚本做的事：

1. `uv pip install pyinstaller`（如未装）；
2. `pyinstaller --windowed --paths src --collect-all rapidocr_onnxruntime --collect-all onnxruntime --collect-all shapely --collect-data pyclipper main.py`；
3. 把 `resources/` 与 `config/config.example.yaml` 复制到 `dist\LocalDocMask\`（本项目用 `__file__` 推导项目根，打包后即 exe 所在目录）；
4. 离屏冒烟：启动 exe，25 秒内未崩溃即视为通过，随后结束进程。

产物：`dist\LocalDocMask\LocalDocMask.exe`（单目录分发，整个目录压缩拷贝即可，实测约 390 MB）。
首次运行会在 exe 旁自动创建 `data\`（input/processing/output/logs）。

> 备注：`dist/`、`build/`、`*.spec` 已加入 .gitignore，不入库。本环境已实际构建成功并通过离屏冒烟（exe 启动后稳定运行 25 秒无崩溃）。如需修改打包行为（单文件 exe、控制台调试版），见脚本内注释。
