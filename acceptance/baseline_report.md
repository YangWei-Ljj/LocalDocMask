# LocalDocMask 基线报告

生成时间：2026-10-25（Asia/Shanghai）

## 环境实测

- 操作系统：Windows 10 专业版 10.0.19045，x64，内存 64GB
- Python：项目虚拟环境 .venv（Python 3.11.16，uv 创建），与系统 Python 3.14 / Conda 3.13 隔离
- Ollama：0.33.2，运行中；模型 qwen3.5:9b、qwen3-embedding:4b
- OCR：RapidOCR（rapidocr-onnxruntime 1.4.4，本地离线，模型内置）
- GUI：PySide6 6.12.0

## 项目初始状态

- 项目根：<PROJECT_ROOT>
- Git：仓库无提交（master 分支，所有源码未跟踪）
- 初始测试：17 个用例全部通过
- 备份：源码已复制到 acceptance/baseline_backup/（53 个文件），哈希记录于 acceptance/baseline_manifest.json

## 网络边界初查

- 唯一网络客户端为 ollama.py 中的 requests，默认指向 http://127.0.0.1:11434
- 未发现 telemetry、sentry、云端 API 调用、外部模板/字体/图片加载
- 初始缺陷：requests 默认 trust_env=True，存在经系统代理绕行本机的风险（SEC-001）

## 关键依赖版本（实测）

- PySide6 6.12.0 / PyMuPDF 2.0 / python-docx 1.2.0 / openpyxl 3.1.5 / Pillow 13.0.0
- opencv-python 5.0.0.93 / rapidocr-onnxruntime 1.4.4 / requests 2.34.2 / pytest 9.1.1
- numpy 2.4.6 / onnxruntime 1.31.0
