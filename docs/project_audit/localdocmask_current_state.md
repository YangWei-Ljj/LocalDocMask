# LocalDocMask 现状审计报告

> 审计日期：2026-10-26（Asia/Shanghai）
> 审计基线提交：`d39eb1e`（"chore: establish LocalDocMask pre-refactor baseline"）
> 审计方式：基于实际运行（pytest、模块导入、offscreen GUI 构造、正则/检测器实测、现有产物 JSON 阅读）+ 源码静态走查
> 证据标记：【运行】= 已实际执行验证；【源码】= 仅静态阅读确认；【未验】= 本次未能验证

---

## 0. 总体结论

LocalDocMask 是一个**功能可跑、安全门禁基本闭环、但工程化程度很低**的本地文档脱敏桌面工具。它已经具备：

- 四种格式（PDF / DOCX / XLSX / 图片）的端到端脱敏 + 独立审计 + 失败关闭；
- 原件 SHA-256 前后绑定、输出哈希绑定、Ollama 强制回环 + 禁代理；
- 逐项处理（redact → 失败 → delete → 验证）的状态机；
- 42 项自动化测试全部通过（实测 29.62 s）。

但它同时存在明显的工程债：扁平单包结构、无日志/无持久化/无配置文件、置信度字段形同虚设、无白/黑名单文件、无标注回归集与量化评测、GUI 与业务逻辑强耦合。**这些正是后续重构要解决的问题，而非阻断当前可用的 P0 安全漏洞**——上一轮已修复的 SEC-001~008 在代码中确实存在且有测试覆盖。

---

## 1. 真实目录结构与模块清单

### 1.1 顶层目录（实测）

| 路径 | 状态 | 说明 |
|---|---|---|
| `main.py` | 209 B | 启动入口，把 `src/` 加入 sys.path 后调用 `localdocmask.gui.run()`【源码】 |
| `启动.bat` | 102 B | `chcp 65001` + 调用 `.venv\Scripts\python.exe main.py`【源码】 |
| `运行测试.bat` | 97 B | `chcp 65001` + `python -m pytest tests -v`【源码】 |
| `requirements.txt` | 209 B | 仅 9 个直接依赖（见 §9）【源码】 |
| `README.md` | 5.5 KB | 中文使用说明【源码】 |
| `.gitignore` | 172 B | 忽略 `.venv/`、`data/`、`__pycache__`、`_*.py/png/pdf/txt`【源码】 |
| `src/localdocmask/` | **扁平单包** | 见 §1.2 |
| `tests/` | 6 个测试文件 | 见 §10 |
| `acceptance/` | 报告 + 基线备份 | 见 §1.3 |
| `config/` | **空目录** | 【运行】`Get-ChildItem config` 无任何文件 |
| `scripts/` | **空目录** | 【运行】同上 |
| `resources/` | **空目录** | 【运行】同上 |
| `docs/` | 2 个 md | `未完成安全功能清单.md`、`测试报告.md`（本次新增 `project_audit/`、`roadmap/`） |
| `data/` | 用户数据 | `input/`、`output/success/`、`output/quarantine/`、`quarantine/`、`logs/`、`processing/`（后两者为空）【运行】 |

### 1.2 `src/localdocmask/` 实际模块清单（与任务设想的"子目录"对照）

任务设想的 `models/config/patterns/detection/masker/ocr/ollama/auditor/task_manager/gui` 子目录**全部不存在**；它们都是扁平 `.py` 模块：

| 实际路径 | 行数/大小 | 职责 |
|---|---|---|
| `__init__.py` | 74 B | 仅 `__version__ = "0.1.0"` |
| `__main__.py` | 59 B | `from .gui import run; run()` |
| `config.py` | 822 B | 硬编码目录、Ollama URL、默认掩码（见 §8） |
| `models.py` | 2.6 KB | `TaskStatus` / `ItemStatus` / `AuditVerdict` 枚举 + `Candidate` / `ScanResult` / `AuditFinding` / `AuditReport` / `Task` dataclass |
| `patterns.py` | 1.9 KB | 8 个正则 + 身份证/银行卡校验器 |
| `detection.py` | 2.7 KB | `Detector`：正则 + 自定义词 + 企业/人员名单 |
| `masker.py` | 2.8 KB | `Masker`：模式 A/B + `mask_display` |
| `ocr.py` | 892 B | `OCREngine` 单例封装 RapidOCR |
| `ollama.py` | 3.3 KB | `OllamaClient`：回环校验、禁代理、实体抽取 |
| `hashing.py` | 279 B | `sha256_file` |
| `auditor.py` | 12.0 KB | `Auditor`：独立审计、OCR 复查、元数据/隐藏部件检查 |
| `task_manager.py` | 12.4 KB | `TaskManager`：prepare/scan/process/audit/finalize/run |
| `gui.py` | 24.4 KB | PySide6 主窗口 + 工作线程 |
| `formats/__init__.py` | 843 B | 格式注册 `_EXT_MAP` |
| `formats/base.py` | 530 B | `FormatHandler` 抽象基类 |
| `formats/pdf.py` | 7.6 KB | PyMuPDF 红action |
| `formats/docx.py` | 12.5 KB | python-docx + zip XML 直改 |
| `formats/xlsx.py` | 4.1 KB | openpyxl |
| `formats/image.py` | 2.8 KB | PIL + OpenCV + OCR |
| `core/` | **空目录** | 预留，无任何 `.py`【运行】 |
| `ui/` | **空目录** | 预留，无任何 `.py`【运行】 |

### 1.3 `acceptance/` 实际内容

| 路径 | 说明 |
|---|---|
| `acceptance_checklist.json` (5.1 KB) | 验收检查项清单 |
| `baseline_manifest.json` (8.3 KB) | 基线文件 SHA-256 清单（53 项，含 `__pycache__`） |
| `baseline_report.md` / `test_report.md` / `final_acceptance_report.md` | 上一轮验收报告 |
| `defect_log.md` | SEC-001~008 缺陷日志（全部标记已修复） |
| `feature_per_item_report.md` | 逐项功能报告 |
| `security_review.md` | 安全评审 |
| `test_manifest.json` | 测试清单 |
| `baseline_backup/` | **整份基线源码快照**（含一份旧版 `src/localdocmask/`，与当前源码存在差异，例如 `auditor.py` 旧版 7.4 KB vs 当前 12.0 KB） |

注意：`acceptance/` 下**没有可执行的验收测试脚本**，全部是文档/JSON 报告。

---

## 2. 当前功能清单（已实现 vs 占位）

| 功能 | 状态 | 证据 |
|---|---|---|
| PDF 文本脱敏（redact annotation） | 已实现 | `formats/pdf.py:144-165` |
| PDF 扫描页 OCR | 已实现 | `formats/pdf.py:51-70`（仅当页文本<40 字且含图时触发） |
| PDF 元数据/附件清除 | 已实现 | `formats/pdf.py:167-174` |
| PDF 表单字段检测 → 阻止导出 | 已实现 | `formats/pdf.py:96-108`，测试 `test_pdf_form_field_blocks` |
| 加密 PDF 拒绝 | 已实现 | `formats/pdf.py:31-32`，测试 `test_encrypted_pdf_rejected` |
| 图文混排页 OCR | **部分实现**：仅警告，不 OCR 嵌入图 | `formats/pdf.py:42-43` |
| DOCX 正文/表格/页眉页脚/文本框/批注/脚注尾注 | 已实现 | `formats/docx.py:112-173` |
| DOCX 嵌入图像 OCR + 像素级删除 | 已实现 | `formats/docx.py:182-206, 236-259` |
| DOCX 修订隐藏文本（w:delText）清除 | 已实现 | `formats/docx.py:217-218` |
| DOCX 元数据（core/app/custom）清除 | 已实现 | `formats/docx.py:291-311` |
| XLSX/XLSM 单元格（含隐藏表） | 已实现 | `formats/xlsx.py:43-63` |
| XLSX 批注/超链接/图片清除 | 已实现 | `formats/xlsx.py:86-97` |
| XLSX 宏/外部链接/OLE → 阻止导出 | 已实现 | `formats/xlsx.py:68-73` |
| 图片 OCR + 像素红化 | 已实现 | `formats/image.py:34-49, 61-72` |
| 图片印章红色区域启发式 | 已实现（仅警告，不删除） | `formats/image.py:25-33` |
| 模式 A 统一掩码 | 已实现 | `masker.py:31-45` |
| 模式 B 一致性虚构替换（内存映射） | 已实现 | `masker.py:18-45` |
| Ollama 实体抽取辅助 | 已实现（候选，不参与判定） | `ollama.py:55-83`、`task_manager.py:245-257` |
| 独立审计（文本/OCR/元数据/隐藏部件） | 已实现 | `auditor.py:31-48, 102-202` |
| 逐项处理（redact → 失败 → delete → 验证） | 已实现 | `task_manager.py:56-122` |
| 最终导出安全门禁 | 已实现 | `task_manager.py:142-180` |
| 审计记录 JSON 落盘 | 已实现（仅单任务报告） | `task_manager.py:227-243` |
| GUI 桌面界面 | 已实现（offscreen 构造通过） | `gui.py:54-584` |
| `core/`、`ui/` 分层目录 | **空壳占位** | 【运行】两目录无 `.py` |
| `config/`、`scripts/`、`resources/` 顶层目录 | **空目录** | 【运行】 |

---

## 3. 端到端数据流与状态流转

### 3.1 处理流水线（`TaskManager`）

【源码】+【运行：pipeline 测试通过】

```
prepare(source_path)
  └─ 计算 source_hash = sha256_file(p)，TaskStatus.PENDING
scan(task)
  └─ handler.extract(p) → ScanResult(candidates, text, warnings)
  └─ 若 use_ollama：_ollama_assist(scan) 追加 confidence=0.6 候选
  └─ 有候选 → NEEDS_REVIEW；无候选 → PROCESSED_UNAUDITED
process(task, mode, mask, candidates)
  ├─ 过滤 excluded，按长度降序
  ├─ 阶段1：handler.process(...) → out1 = processing/<stem>_脱敏.<ext>
  ├─ 阶段2：auditor.residual_terms(out1, [c.text]) 逐项判定
  │     ├─ 残留/不可查 → 进入 deletion 列表
  │     └─ 否则 → ItemStatus.AUDIT_PASSED
  ├─ 阶段3：若有 deletion，handler.process(out1 → out2, deletion)
  ├─ 阶段4：auditor.residual_terms(out2) 验证删除
  │     ├─ 仍残留/不可查 → UNRESOLVED（final_disposition=unresolved）
  │     └─ 否则 → DELETION_VERIFIED
  └─ 阶段5：auditor.audit(final, [c.text]) 整体审计
finalize(task, report)
  └─ 门禁：
     1. sha256(source) == task.source_hash？否则 → UNSAFE + quarantine
     2. report is task.audit 且 findings 非空？否则 → UNSAFE
     3. output_path 存在？否则 → UNSAFE
     4. sha256(output) == task.final_hash？否则 → UNSAFE（审计后被改）
     5. 无 unresolved 条目？否则 → AUDIT_FAILED + quarantine
     6. report.passed？否则 → AUDIT_FAILED/UNSAFE + quarantine
     └─ 全过 → 拷贝到 SUCCESS_DIR，TaskStatus.PASSED
```

### 3.2 关键枚举（`models.py`）

- `TaskStatus`：PENDING / PROCESSING / NEEDS_REVIEW / PROCESSED_UNAUDITED / AUDIT_FAILED / UNSAFE / PASSED
- `ItemStatus`：DETECTED / REDACTED / AUDIT_PASSED / AUDIT_FAILED / DELETION_PENDING / DELETED / DELETION_VERIFIED / FAILED / UNRESOLVED
- `AuditVerdict`：RESIDUAL_FOUND / NO_RESIDUAL / UNCHECKABLE / INSUFFICIENT / NEEDS_CONFIRM

`AuditReport.passed` 的判定（`auditor.py:45-47`）：**所有 findings 都必须是 NO_RESIDUAL**，任何 RESIDUAL_FOUND / UNCHECKABLE / NEEDS_CONFIRM 都会导致 passed=False。

---

## 4. 双脱敏模式现状

| 维度 | 模式 A（统一掩码） | 模式 B（一致性虚构） |
|---|---|---|
| 实现位置 | `masker.py:31-45` | 同 |
| 非实体类别（手机/身份证/邮箱…） | 一律 `****` | 仍 `****`（`masker.py:45` 兜底） |
| 实体类别（公司名/企业名/姓名/人名） | `****` | 从硬编码池取（10 家虚构公司、20 姓 × 20 名） |
| 映射表作用域 | 无 | **仅当前 `Masker` 实例内存**（`masker.py:24` `self._mapping`） |
| 持久化 | 无 | **无 save/load**（实测 `hasattr(m,'save')=False`） |
| 跨任务一致性 | 否 | **否**——每次 `TaskManager.process` 都 new 一个 `Masker`（`task_manager.py:59`），批量处理时同一实体在不同文件间映射不同 |
| 冲突处理 | 无 | 无——池耗尽后按索引取模循环，不检测冲突 |
| 虚构名是否依赖真实信息 | N/A | 否，纯字典枚举（`masker.py:4-13`） |

---

## 5. 敏感信息识别现状

### 5.1 正则规则（`patterns.py`）

| 规则 | 正则 | 校验器 |
|---|---|---|
| 手机号 | `1[3-9]\d{9}` | 无 |
| 邮箱 | 标准 | 无 |
| 固定电话 | `0\d{2,3}-\d{7,8}` | 无 |
| 身份证 | 18 位 + 出生日期段 | `is_valid_id`（加权校验位）+ `is_valid_birth` |
| 护照 | `E/G\d{8}` 或 `[A-Z]{2}\d{6,8}` | 无 |
| 银行卡 | `\d{13,19}` | `luhn_ok` |
| 地址 | 省/市/区/县 + 路/街/号/… | 无 |
| 编号 | 客户/员工/合同/项目…编号 | 无 |
| 公司名 | `(?![在的与和是于对从将把被及至向为])[\u4e00-\u9fa5A-Za-z0-9]{2,24}(?:有限公司\|股份\|集团\|公司\|银行\|医院\|大学\|研究院\|事务所)` | 无 |

### 5.2 Ollama 辅助

- 入口：`task_manager.py:245-257`
- 仅取 `scan.text[:8000]`，调 `OllamaClient.extract_entities`
- 产出候选：`category=公司名/姓名`、`confidence=0.6`、`source="ollama"`、`location="本地模型辅助"`
- **不参与最终安全判定**：候选仍需用户在 GUI 勾选（默认 `confirmed=True`），或在批量时全部自动确认
- 异常时仅追加 `scan.warnings`，不阻断

### 5.3 置信度字段的实际用途

【运行 + 源码】`confidence` 字段：
- 正则规则命中一律 `1.0`（`detection.py:47`）
- 公司正则命中 `0.85`（`detection.py:51`）
- Ollama 候选 `0.6`（`task_manager.py:255`）
- OCR 引擎返回的分数被丢弃（`ocr.py:25` 存进 dict 但 `Detector.detect_text` 不读取）
- **仅在 GUI 第 6 列显示**（`gui.py:324`），不进入 process/finalize/auditor 任何判定逻辑

---

## 6. 已知缺陷核实（逐条）

### 6a. "本公司"被误判为公司名

**结论：本说法对当前代码不准确；但同类误报确实存在。**

【运行】实测 `COMPANY_RE.finditer(s)`：

| 输入 | 匹配结果 |
|---|---|
| `本公司产品` | `[]`（不匹配） |
| `本公司` | `[]`（不匹配） |
| `本公司项目` | `[]`（不匹配） |
| `我们公司` | `['我们公司']` ← 误报 |
| `有限公司` | `['有限公司']` ← 误报 |
| `股份公司` | `['股份公司']` ← 误报 |
| `集团公司` | `['集团公司']` ← 误报 |
| `银行医院大学` | `['银行医院大学']` ← 误报（拼接） |
| `中国石油股份有限公司` | `['中国石油股份有限公司']`（正确命中） |

原因：当前正则已加负向前瞻 `(?![在的与和是于对从将把被及至向为])`，且 `{2,24}` 要求至少 2 个汉字前缀，所以"本公司"（仅 1 字前缀）不命中。但负向前瞻**没有覆盖**"我们/这是/以上/以下"等 2 字泛指，也没有阻止"有限公司/股份公司/集团公司"这种纯后缀组合命中。**任务描述中的具体案例"本公司"是旧版正则的问题，已被部分修复，但问题类别（无上下文判定）仍然存在。**

### 6b. 所有 Candidate 默认 confirmed=True，公司正则高置信度自动勾选

**结论：确认。**

- `models.py:46`：`confirmed: bool = True`
- `detection.py:51`：公司正则命中 `confidence=0.85`，但 `add()` 走的是 `Candidate(text=val, category=cat, ...)` 构造，`confirmed` 取默认值 `True`
- 【运行】实测：`'中国石油股份有限公司发布公告'` → 候选 `('中国石油股份有限公司', '公司名', 0.85, True, 'regex')`，confirmed=True
- 批量按钮 `gui.py:474-476` 还会强制把所有候选 `confirmed=True, excluded=False`

### 6c. confidence 未用于门禁/复核分流，规则与模型冲突无仲裁

**结论：确认。**

- `confidence` 仅在 `gui.py:324` 显示
- `process()`、`finalize()`、`Auditor.audit()` 全部不读 `confidence`
- 当同一实体同时被 regex 和 ollama 命中时，`detection.add()` 用 `(cat, val)` 去重（`detection.py:36-38`），保留**先入表**的那个（regex 先跑，所以 ollama 同文本被丢弃），不做置信度仲裁

### 6d. 无上下文判定、无可配置白/黑名单与领域术语词典文件

**结论：确认。**

- 上下文判定：无（无 NER、无句法分析、无窗口上下文）
- 白/黑名单：`Detector` 接受 `custom_words/companies/persons` 参数，但来源是 GUI `QTextEdit` 文本框（`gui.py:251-256`），**没有从文件加载的路径**
- `config/` 目录为空，`resources/` 目录为空
- 无领域术语词典文件

### 6e. 无结构化日志、无任务状态持久化/失败恢复/幂等、无配置文件与配置校验

**结论：确认。**

- **日志**：【Grep】`import logging|getLogger|logging\.` 在 `src/` 下 0 命中。唯一的"记录"是 `task_manager.py:241` 写单任务 JSON 报告，且异常被 `except Exception: pass` 吞掉（`task_manager.py:242-243`）
- **持久化**：`Task` 对象全在内存；GUI 关闭后任务列表丢失；无 sqlite/json 任务队列
- **失败恢复**：无——崩溃后未完成任务无法续跑；`data/processing/` 下残留文件只能由 GUI "清理临时文件"按钮手动删（`gui.py:545-557`）
- **幂等**：同一文件重复跑会生成新 `task_id`（`task_manager.py:36` `uuid.uuid4().hex[:12]`），不检测已存在的成功产物
- **配置文件**：`config.py` 全部硬编码；`config/` 空；无 pydantic/dataclass 校验

### 6f. 无分层架构，核心逻辑被 GUI/模型调用耦合

**结论：确认。**

- `src/localdocmask/` 扁平 14 个 `.py`，无 presentation/application/domain/infrastructure/security/evaluation 分层
- `gui.py` 直接 `from .detection import Detector`、`from .task_manager import TaskManager`、`from .ollama import OllamaClient`（`gui.py:18-23`），并在槽函数里直接 new `Detector()`、new `TaskManager()`
- `task_manager.py` 直接 import `Auditor`、`Masker`、`hashing`、`formats`、`config`（`task_manager.py:10-15`），无接口抽象
- `core/`、`ui/` 空目录是预留但未落地

### 6g. 无标注回归测试集与量化评测

**结论：确认。**

- `tests/` 下全部是功能/安全回归，用例中的敏感词都是硬编码（`张三/李四/13812345678/VALID_ID`）
- 无 Precision/Recall/F1、误报率/漏报率/复核率/残留率/文件成功率的统计代码
- 无按实体类型/文件类型/识别路径（regex vs OCR vs ollama）分组的指标
- 无标注数据集（`data/` 下只有 2 个用户文件，非标注集）

---

## 7. 多格式解析与隐藏内容检查现状

| 检查项 | PDF | DOCX | XLSX/XLSM | 图片 |
|---|---|---|---|---|
| 正文文本 | ✅ `page.get_text()` | ✅ paragraphs/tables | ✅ 全部单元格 | N/A |
| 页眉页脚 | ✅（PyMuPDF 自动并入文本） | ✅ `formats/docx.py:131-139` | N/A | N/A |
| 脚注/尾注 | N/A | ✅ zip 直读 + 重写 | N/A | N/A |
| 文本框 | N/A | ✅ `w:txbxContent` 遍历 | N/A | N/A |
| 批注 | 🔶（附件清除但不查批注残留词） | ✅ 提取 + 清空 | ✅ 单元格批注清空 | N/A |
| 修订/隐藏文本 | N/A | ✅ `w:delText` 清空 | ✅ 隐藏工作表单元格仍处理 | N/A |
| 元数据 | ✅ `doc.set_metadata({})` | ✅ core/app/custom 全清 | 🔶（未显式清 docProps） | N/A |
| 附件/嵌入文件 | ✅ `embfile_del` 全删 | 🔶（媒体部件 OCR + 像素删） | ✅ OLE 检测即阻止 | N/A |
| 嵌入图像文字 | 🔶 图文混排仅警告不 OCR | ✅ OCR + 像素红化 | ✅ 删 `ws._images` | ✅ OCR |
| 宏/VBA | N/A | N/A | ✅ 检测即阻止 | N/A |
| 外部链接 | N/A | N/A | ✅ 检测即阻止 | N/A |
| 表单字段 | ✅ 含值即阻止 | N/A | N/A | N/A |
| 加密/密码 | ✅ 拒绝 | 🔶（损坏即拒绝，但不查加密） | 🔶（损坏即拒绝） | N/A |
| 隐藏行列 | N/A | N/A | 🔶（iter_rows 默认遍历所有单元格，含隐藏，但不专门标记） | N/A |
| 印章/签名/二维码 | 🔶 | 🔶 | N/A | 🔶 红色区域仅警告 |

---

## 8. 安全机制现状

| 机制 | 实现 | 证据 |
|---|---|---|
| 原件哈希校验 | `prepare` 记 source_hash，`finalize` 重算比对 | `task_manager.py:38, 150-151` |
| 输出哈希绑定 | `process` 末尾记 final_hash，`finalize` 重算比对 | `task_manager.py:123, 159-160` |
| 网络回环 | `OllamaClient.__init__` 校验 hostname ∈ {127.0.0.1, localhost, ::1} | `ollama.py:21-23` |
| 禁代理 | `session.trust_env=False` + `proxies={http:None,https:None}` | `ollama.py:27-29` |
| 审计独立性 | `Auditor` 独立类，重新提取输出文本/OCR/元数据，不读处理期内存状态 | `auditor.py:28-48` |
| 失败关闭 | 任何 UNSAFE/AUDIT_FAILED/UNRESOLVED → quarantine，不导出 | `task_manager.py:144-173` |
| 临时文件 | 处理中间产物在 `data/processing/`，最终成功后 move/copy 到 success | `task_manager.py:192-198, 120` |
| 敏感值落盘保护 | 审计 JSON 中用 `mask_display` 脱敏（`1101**********002X`） | `masker.py:48-62`、`task_manager.py:216` |
| 审计报告不可伪造 | `finalize` 校验 `report is task.audit` 且 findings 非空 | `task_manager.py:152-155` |

---

## 9. 依赖与环境清单

### 9.1 `requirements.txt`（声明）

```
PySide6==6.12.0
PyMuPDF==2.0.0
python-docx==1.2.0
openpyxl==3.1.5
Pillow==13.0.0
opencv-python==5.0.0.93
rapidocr-onnxruntime==1.4.4
requests==2.34.2
pytest==9.1.1
```

### 9.2 实际代码 import 但未在 requirements.txt 声明的包

【运行】以下包在源码中直接 import 但未列入 `requirements.txt`：

| 包 | 使用位置 |
|---|---|
| `numpy` | `auditor.py:6`、`formats/docx.py:9`、`formats/image.py:5` |
| `lxml` | `formats/docx.py:12`、`auditor.py:228` |
| `onnxruntime`（间接） | rapidocr 依赖 |
| `psutil` | （用户环境已装，代码未直接用） |
| `pyyaml` | （用户环境已装，代码未直接用） |

### 9.3 启动入口

- `main.py` → `localdocmask.gui.run()`（`gui.py:578-584`）
- `启动.bat` → 调用 `.venv\Scripts\python.exe main.py`
- `scripts/` 空，`resources/` 空

---

## 10. 测试体系现状

### 10.1 实测结果

【运行】`.venv\Scripts\python.exe -m pytest tests -q`：

```
..........................................                               [100%]
42 passed in 29.62s
```

- 通过：42
- 失败：0
- 跳过：0
- 耗时：29.62 s（OCR 模型加载慢，主要耗时在 `test_image_pipeline`、`test_docx_embedded_image_sensitive_deleted` 等用例）

### 10.2 测试文件覆盖

| 文件 | 用例数 | 覆盖点 |
|---|---|---|
| `tests/test_detection.py` | 6 | 正则规则、身份证校验、词典实体、长词优先、自定义词、地址/编号 |
| `tests/test_masker.py` | 4 | 模式 A/B、一致性、数字类仍掩码 |
| `tests/test_pipeline.py` | 8 | DOCX/PDF/XLSX/图片端到端、残留检测、源文件篡改、不支持格式 |
| `tests/test_gate.py` | 9 | 只读管线、重复运行哈希稳定、宏阻止、损坏输出阻止、PDF 内容流、加密 PDF 拒绝 |
| `tests/test_flow.py` | 8 | 场景 A~H：逐项 redact/delete/quarantine、来源定位、最终哈希绑定 |
| `tests/test_security.py` | 7 | SEC-001~007：回环/禁代理、伪造报告、脚注、嵌入图像、OLE、表单字段、高风险规则 |

### 10.3 测试缺口

- 无 GUI 自动化测试（offscreen 构造 MainWindow 仅本次审计手动验证）
- 无 Ollama 真实调用测试（`test_security.py` 仅测回环校验和禁代理，不实际调模型）
- 无性能/压力测试（大文件、批量、长文本）
- 无量化评测（Precision/Recall/F1）
- 无异常注入测试（磁盘满、权限拒绝、OCR 崩溃）
- 无并发/线程安全测试

---

## 11. 已知缺陷 / 安全风险 / 技术债务清单（分级）

### P0（安全相关，需优先处理）

| 编号 | 描述 | 证据 |
|---|---|---|
| P0-1 | 所有候选默认 `confirmed=True`，批量按钮强制全选，公司正则误报会被自动脱敏 | `models.py:46`、`gui.py:474-476` |
| P0-2 | confidence 字段不参与任何判定，低置信度 Ollama 候选（0.6）与高置信度正则候选（0.85）在批量时一视同仁 | `task_manager.py:255`、`gui.py:324` |
| P0-3 | 同一实体被 regex 和 ollama 同时命中时，按入表顺序去重，无仲裁 | `detection.py:36-38` |

### P1（功能/工程化）

| 编号 | 描述 | 证据 |
|---|---|---|
| P1-1 | 公司正则无上下文判定，"我们公司/有限公司/股份公司/银行医院大学"等泛指被误判 | 【运行】§6a |
| P1-2 | 无白/黑名单文件，自定义词只能 GUI 输入，无法持久化/版本化 | `gui.py:251-256`、`config/` 空 |
| P1-3 | 模式 B 映射表仅内存，跨文件/跨任务不一致；批量时同一真实公司在不同输出中变成不同虚构名 | `task_manager.py:59` 每次 new Masker |
| P1-4 | 无结构化日志，异常被 `except: pass` 吞掉 | `task_manager.py:242-243` |
| P1-5 | 无任务状态持久化/失败恢复/幂等，崩溃后无法续跑 | 全代码无 sqlite/json 任务表 |
| P1-6 | 无配置文件与配置校验，Ollama URL/模型/掩码/目录全硬编码 | `config.py` |
| P1-7 | 扁平架构，GUI 直接耦合 Detector/TaskManager/OllamaClient，无接口抽象 | `gui.py:18-23` |
| P1-8 | requirements.txt 缺 numpy/lxml/onnxruntime | §9.2 |

### P2（评测/可维护性）

| 编号 | 描述 | 证据 |
|---|---|---|
| P2-1 | 无标注回归测试集 | `tests/` 全是硬编码用例 |
| P2-2 | 无 Precision/Recall/F1/误报率/漏报率/复核率/残留率/文件成功率指标 | 全代码无统计逻辑 |
| P2-3 | 无按实体类型/文件类型/识别路径分组的评测 | 同上 |
| P2-4 | `core/`、`ui/` 空目录占位，分层未落地 | 【运行】§1.2 |
| P2-5 | OCR 置信度被丢弃，不用于过滤低质量候选 | `ocr.py:25` |
| P2-6 | PDF 图文混排页仅警告不 OCR 嵌入图 | `formats/pdf.py:42-43` |
| P2-7 | XLSX 元数据（docProps）未显式清除 | `formats/xlsx.py` 无对应代码 |
| P2-8 | 图形印章/签名/二维码/人脸不识别 | `docs/未完成安全功能清单.md` LIMIT-04 |
| P2-9 | GUI 无取消按钮、无超时 | 同上 LIMIT-01/02 |
| P2-10 | `acceptance/baseline_backup/` 保留了一份旧版源码，与当前源码漂移，易混淆 | 【运行】§1.3 |

---

## 12. 本次审计未验证项

- **GUI 真实弹窗启动**：仅用 `QT_QPA_PLATFORM=offscreen` 构造了 `MainWindow` 并验证标题/按钮文本；未在真实桌面环境 `win.show()` + `app.exec()` 事件循环。
- **Ollama 实际抽取**：`ollama list` 确认 qwen3.5:9b / qwen3-embedding:4b 存在，但本次未实际调 `extract_entities`（避免耗时与外部依赖）。
- **大文件/批量性能**：未测。
- **真实用户文件端到端**：未对 `data/input/演示_员工信息.docx` 重新跑管线（避免污染 data/）；仅阅读了已有产物 JSON。
- **杀毒软件/权限边界**：未测。

---

## 13. 用户数据保护声明

本次审计**未删除、未修改、未移动**任何 `data/` 下文件：

- `git status --porcelain` 输出为空（工作区干净）
- `data/input/`、`data/output/success/`、`data/output/quarantine/`、`data/quarantine/` 下文件均为既有产物
- pytest 通过 `tests/conftest.py` 设置 `LOCALDOCMASK_HOME` 到 `tempfile.mkdtemp()`，不写入项目 `data/`
- 仅在 `docs/project_audit/` 和 `docs/roadmap/` 下新建了本文档
