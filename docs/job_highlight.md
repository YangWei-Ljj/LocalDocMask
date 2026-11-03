# 求职技术亮点（以代码/测试证据为基础）

> 本文每条亮点都标注对应代码与测试位置，可直接用于简历。
> **诚信红线**：不写"服务 X 用户""生产环境误报下降 Y%""处理延迟 Z ms"等未经实测的数字；
> 唯一可引用的量化结果是仓库内测试与 36 条合成标注集（见末节）。

---

## 一、项目一句话定位（中文）

LocalDocMask 是一个本地优先的文档隐私治理与安全脱敏桌面平台：对 PDF/DOCX/XLSX/图片中的
身份证、银行卡、手机号、公司名、人名等敏感信息做"规则识别 → 上下文判定 → 多源融合仲裁 →
人工复核 → 脱敏 → 独立安全审计 → 失败关闭导出"，原件只读、数据不出本机、审计不通过不导出。

## One-liner (EN)

LocalDocMask is a local-first document-privacy redaction desktop platform: it pipelines
rule-based detection → context analysis → multi-source fusion adjudication → human review →
redaction → independent security audit → fail-closed export across PDF/DOCX/XLSX/images, with
read-only originals, no outbound network, and no export unless the independent audit passes.

---

## 二、技术亮点条目（中英对照）

### 1. 分层六边形架构与组合根装配
- **做了什么**：把扁平单包重构为 domain / recognition / application / infrastructure /
  security / presentation / evaluation 七层；domain 纯逻辑零依赖，application 只声明端口(Protocol)，
  唯一组合根 `build_services()` 装配基础设施并注入，GUI 不再直接 `new` 业务对象。
- **证据**：`src/localdocmask/` 目录结构；`application/composition.py`；`application/ports.py`；ADR-001。
- **中文简历**：主导将扁平脚本重构为分层六边形架构，依赖倒置 + 组合根装配，业务核心与 UI/基础设施解耦，可独立替换 LLM/持久化实现。
- **EN resume** : Re-architected a flat script into a layered hexagonal architecture with a
  composition root and dependency inversion, decoupling the domain core from UI and infrastructure.

### 2. 规则 + 上下文 + 本地 LLM 的多源融合仲裁
- **做了什么**：高精度正则（身份证校验位、银行卡 Luhn）保召回；上下文窗口做泛指引用剥离；
  白/黑/公开/领域词库给强先验短路；多源证据合并并做置信度校准（多源一致 +0.05、仅模型来源 -0.15）；
  规则与模型冲突时高精度类型以确定性规则为准、上下文类降权转复核。本地 Ollama 仅出低权重候选，不参与最终安全判定。
- **证据**：`recognition/fusion.py`、`recognition/context.py`、`recognition/knowledge.py`、`recognition/pipeline.py`；ADR-003。
- **中文简历**：设计规则/上下文/词库/本地模型四层融合仲裁与置信度校准，模型只增强不独裁，关键判定回归确定性规则与独立审计。
- **EN resume** : Built a four-layer fusion engine (rules/context/lexicons/local LLM) with
  confidence calibration; the local LLM supplies low-weight candidates only, never self-approving safety verdicts.

### 3. 误报治理三档分流
- **做了什么**：候选不再一律确认，按类别+来源+校准置信度分流为 AUTO_CONFIRM / NEEDS_REVIEW / SUPPRESS；
  "我们公司""有限公司"等泛指、白名单表述直接抑制；公开企业名转人工复核，既不一律遮也不放行。
- **证据**：`domain/policy.py`、`recognition/fusion.py::_decide`；ADR-002；评测集负例 10/10 正确抑制。
- **中文简历**：从根上消除"公司名一律自动脱敏"，用泛指剥离 + 词库 + 上下文信号治理误报，每条判定带可解释理由。
- **EN resume** : Replaced blanket auto-redaction with an explainable three-way triage
  (auto-confirm / human review / suppress) to cut false positives on generic references and public entities.

### 4. 独立安全审计与失败关闭
- **做了什么**：审计器与处理模块分离，对脱敏产物重新提取正文、重新 OCR、复查 PDF 元数据/附件/表单字段、
  DOCX 脚注尾注/嵌入图像、XLSX 隐藏表与 zip 内 XML；残留即失败，逐条失败转删除并二次验证；
  finalize 用源/终态 SHA-256 绑定 + 未决项/复核门禁，任何不过即移入隔离目录，绝不把未验证内容当成功输出。
- **证据**：`security/auditor.py`、`application/pipeline.py::finalize`；威胁模型 `docs/security/threat_model.md`。
- **中文简历**：实现与处理解耦的独立审计闭环与 fail-closed 导出门禁，残留/不可读/原件被改即隔离。
- **EN resume** : Implemented an independent auditor (separate from redaction) with a fail-closed
  export gate: residual detection, source/output hash binding, and quarantine on any failure.

### 5. SQLite 持久化、幂等与崩溃恢复
- **做了什么**：标准库 sqlite3 零新增依赖；显式 schema 版本号 + 轻量迁移函数（不用无版本 create_all）；
  以 (源文件 SHA-256 + 模式 + 策略版本) 为幂等键复用已验证产物；启动时把崩溃停在 PROCESSING 的任务重归类——
  无输出可安全重试，已有残留输出标记 INTERRUPTED 不自动认定安全。库中条目只存掩码值，审计 detail 落库前强制掩码。
- **证据**：`infrastructure/persistence/sqlite_repo.py`、`recovery.py`；ADR-005。
- **中文简历**：设计带版本迁移的 SQLite 任务库、幂等键与崩溃恢复重归类，崩溃后不自动信任半成品产物。
- **EN resume** : Designed a versioned SQLite store with idempotency keys and crash recovery that
  reclassifies interrupted tasks instead of trusting half-finished outputs.

### 6. 双脱敏模式与一致性虚构映射
- **做了什么**：模式 A 统一掩码；模式 B 把公司/人名映射为稳定虚构值，批次内跨文件一致；
  确定性序列分配 + "已占用值"集合做冲突顺延（绝不静默复用），池耗尽抛领域异常使文件失败关闭；
  映射表默认仅内存不落盘，落盘需显式 opt-in 并用 icacls 收紧为仅当前用户，调试表示不泄露明文。
- **证据**：`security/redaction/mapping.py`、`masker.py`；`application/batch.py`；ADR-004。
- **中文简历**：实现跨文件一致的虚构替换映射，带冲突检测与最小权限保护，映射表全生命周期可控。
- **EN resume** : Implemented batch-consistent fictional-mapping replacement with deterministic
  allocation, collision detection, and protected persistence (off by default).

### 7. PySide6 桌面工程化与安全工程基线
- **做了什么**：QThreadPool 后台执行耗时任务、真实进度回调、协作式取消；候选表按决策着色；
  结构化 JSON 日志 + RedactionFilter（含 extra 属性）防原文落盘；路径越权防护 safe_join 拒绝 `..`/绝对路径/符号链接；
  Ollama 双回环校验 + 禁代理；资源上限（文件大小/并发/单任务超时）入口拒绝。
- **证据**：`presentation/gui/main_window.py`、`infrastructure/logging/logger.py`、`security/path_safety.py`、
  `llm/ollama.py`、`security/limits.py`；ADR-006/008。
- **中文简历**：落地桌面端异步 UI、日志脱敏、路径 containment、回环网络边界与资源守卫等安全工程基线。
- **EN resume** : Delivered async PySide6 UX plus security baseline: log redaction, path containment,
  loopback-only egress, and resource limits.

---

## 三、可诚实引用的量化结果

- 测试：`pytest tests` → **157 passed, 1 skipped**（共 158 用例，约 50s）。
- 识别评测（`evaluation/`，600 条**合成**标注、NER 开启）：tp=1707 / fp=133 / fn=44，
  P=0.9277 / R=0.9749 / F1=0.9507；NER 关闭时 P=0.9589 / R=0.7601 / F1=0.8480；
  应抑制负例全部正确抑制；指标均为受控合成句、不代表真实文档。
- 支持格式族：PDF、DOCX、XLSX/XLSM、位图图片（PNG/JPG/JPEG/TIFF/BMP）。

> 必须同时声明的边界：上述 F1=1.0 来自 36 条手工合成句，**是回归基线而非生产精度**；
> 真实用户文件端到端、真实 LLM 精度、跨机器干净安装尚未验证。面试时主动说明这些边界，比夸大数字更可信。

## 四、可直接放进简历的项目段落（中文，精简版）

> **LocalDocMask｜本地优先文档脱敏与隐私治理平台（Python / PySide6）**
> - 主导从扁平脚本重构为七层分层架构（domain/recognition/application/infrastructure/security/presentation/evaluation），组合根装配、依赖倒置。
> - 设计规则+上下文+词库+本地 LLM 的融合仲裁与置信度校准，三档分流治理误报，模型仅出低权重候选。
> - 实现与处理解耦的独立安全审计与 fail-closed 导出门禁（残留复查、源/终态哈希绑定、隔离目录）。
> - 落地 SQLite 版本化持久化、幂等键、崩溃恢复，以及批次级一致性虚构映射与最小权限保护。
> - 配套 131 项 pytest 与 36 条合成标注评测（F1=1.0），完整 ADR 文档与威胁模型。

## Project bullet (EN, concise)

> **LocalDocMask — local-first document redaction & privacy platform (Python / PySide6)**
> - Re-architected a flat script into a seven-layer architecture with a composition root and dependency inversion.
> - Built a rules/context/lexicon/local-LLM fusion engine with confidence calibration and explainable three-way triage.
> - Implemented an independent auditor with fail-closed export gating, hash binding, and quarantine.
> - Delivered versioned SQLite persistence, idempotency, crash recovery, and protected batch-consistent fictional mapping.
> - Shipped 131 passing pytest cases and a 36-case synthetic evaluation set (F1=1.0), with full ADRs and a threat model.
