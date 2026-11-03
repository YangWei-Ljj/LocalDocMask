# LocalDocMask 独立验收审查报告（Acceptance Review）

- 审查对象：`<PROJECT_ROOT>`，分支 master，HEAD `1082c0f`
- 审查方式：只读源码、独立复跑测试、自建临时目录（`%TEMP%\lcam_audit*`）合成样本跑真实闭环、对产物与数据目录做原文回扫。未修改任何源码、未提交 git。
- 审查日期：2026-10-26
- 审查人角色：独立验收工程师（挑刺立场，不为实现辩护）

---

## 0. 总结论

**当前版本未达到"企业级可交付"标准，但核心闭环是真实、可信、可运行的。**

- 无 Blocker：真实闭环（prepare→scan→三档分流→人工复核→process→audit→finalize→成功/隔离）端到端跑通，失败关闭、原件只读、无外发网络、输出可打开，这些关键安全承诺**经我独立复现成立**。
- 但存在 **2 个 Major**（数据库落原文违反自述安全契约、崩溃恢复/操作日志未接入真实运行路径）和若干文档/依赖/评测过度解读问题，必须先修才能对外宣称"企业级交付"。
- 开发方"自我感觉良好但实际有问题"的点见第 5 节。

---

## 1. 独立复跑结果（我亲自执行）

| 项目 | 开发方声称 | 我复跑结果 | 结论 |
|---|---|---|---|
| 单元/集成测试 | 124 passed, 1 skipped | `pytest tests -q` → **124 passed, 1 skipped in 41.98s** | 属实 |
| 评测 F1 | F1=1.0 | 亲自跑 `EvaluationRunner`：36 条，tp=42/fp=0/fn=0，P/R/F1=1.0 | 数字真实，但样本见 Major/Minor |
| GUI 可启动 | （未量化） | offscreen 下构造 `MainWindow` 成功，17 个按钮均有槽函数，`ApplicationServices` 装配成功 | 通过 |

---

## 2. 问题清单（按严重程度）

### Blocker（阻断）

无。核心闭环与安全红线未发现"跑不通"或"直接泄密外发"级别的硬伤。

---

### Major（重要，交付前必须修）

#### M1. 数据库 `audit_findings.detail` 写入了未脱敏实体的**完整原文**，直接违反模块自述的安全契约

- **位置**：
  - `src/localdocmask/security/auditor.py:105,111,153,159,170,181,195` —— `AuditFinding(detail=f"输出文本中发现残留：{t}")` / `f"输出文本中发现高风险模式：{name} {v}"` / `f"隐藏部件残留：{t} ({n})"` 等，detail 直接拼入原始敏感值。
  - `src/localdocmask/infrastructure/persistence/sqlite_repo.py:320-333` —— `save_task` 把 finding.detail 原文落库。
  - 同文件 `sqlite_repo.py:8-9` 的头注释明确写着："**task_items 只存 mask_display 处理后的掩码值，绝不落原文；……审计 detail 为元信息，调用方不得放入敏感原文。**"
- **复现**：我合成了一个含"中国石油天然气集团有限公司 / 中国石油股份有限公司"的 DOCX，**故意不裁决其 NEEDS_REVIEW**（走失败关闭路径）。处理结束后查询 `tasks.db`：
  ```
  audit_findings 表：
  RESIDUAL_FOUND | 输出文本中发现残留：中国石油天然气集团有限公司
  RESIDUAL_FOUND | 输出文本中发现残留：中国石油股份有限公司
  RESIDUAL_FOUND | 隐藏部件残留：中国石油天然气集团有限公司 (word/document.xml)
  RESIDUAL_FOUND | 隐藏部件残留：中国石油股份有限公司 (word/document.xml)
  ```
  即：完整企业名以明文形式进入了 SQLite。若残留的是手机号/身份证/银行卡，`_check_text` 的高风险分支会把 `138xxxx/110xxxx/622xxxx` 整串写入同一字段。
- **为何是问题**：
  1. 违反代码自身头注释承诺，也与 README/ADR-008 "绝不写完整原文"的对外口径冲突；
  2. 影响边界虽被隔离模型限制（隔离目录里的输出文件本来就含原文），但 **DB 比隔离产物活得久**——用户清理隔离文件后，明文实体仍残留在 `tasks.db` 里；
  3. 一旦后续有人把 `tasks.db` 作为"审计证据包"导出/归档/上传，就等于外发原文。
- **建议**：detail 中对 `t`/`v` 统一过 `mask_display()`（或只写类别+位置，不写值）；为 audit_findings 增加回归测试，断言库内不出现任一登记候选的原文子串。

#### M2. 崩溃恢复、幂等复用、操作日志、敏感值登记**从未接入真实运行路径**，只在测试里被调用

- **位置**：
  - `infrastructure/persistence/recovery.py` 整个 `RecoveryService.recover()` 与 `existing_success()`；
  - `infrastructure/logging/logger.py:36 register_sensitive_values()`；
  - `sqlite_repo.append_operation()`；
  - `infrastructure/limits.py` 的 `task_timeout_seconds`。
- **证据**：全仓 grep `RecoveryService|existing_success|register_sensitive_values|append_operation` —— 生产代码（`application/composition.py`、`presentation/gui/main_window.py`、`application/batch.py`、`application/pipeline.py`）**没有任何一处调用**；调用方全部位于 `tests/test_persistence.py`、`tests/test_security_hardening.py`。
- **后果**：
  1. 启动时不做 `recover`：上次崩溃后停在 `PROCESSING` 的任务永远不会被重新归类/重新入队——B3 验收项"任务状态、进度、取消与失败恢复"在真实 App 里只剩"取消"是活的；
  2. `operation_log` 表在生产中**永远为空**，D3 "调用追踪"实际未发生；
  3. `register_sensitive_values` 无人调用，意味着除正则可识别的五类（手机/身份证/邮箱/银行卡/固话）外，其它候选原文（公司名、人名、自定义敏感词）即使被 log 也不会被日志过滤器主动掩码（本次实测日志未泄露，是因为代码路径本来就不打印原文；但这是巧合，不是机制）；
  4. `task_timeout_seconds` 只做了范围校验，没有任何看门狗执行它。
- **为何是 Major**：这些是 acceptance_matrix 里 🔶/✅ 项的"已实现"证据，但实现只在单元测试里闭环，运行时根本不走。属于"测试绿了但功能没接线"。
- **建议**：在 `build_services()` 末尾或 GUI `MainWindow.__init__` 里真正调用一次 `RecoveryService().recover(repository, PROCESSING_DIR)`；在 pipeline 关键节点 `append_operation`；或在矩阵/文档中诚实标注"未接线"。

---

### Minor（一般，建议修）

#### m1. README 严重过时，与真实代码结构脱节
- `README.md:84` 声称"当前测试套件 42 项全部通过"——实际 124。
- `README.md:64-78` 的目录结构描述的是旧扁平模块（`config.py/models.py/detection.py/masker.py/ocr.py/ollama.py/auditor.py/task_manager.py/gui.py`），这些文件已不存在；真实结构是 `src/localdocmask/{domain,recognition,application,infrastructure,security,presentation,evaluation}` 分层。
- `docs/roadmap/acceptance_matrix.md:10` 头部仍写"95 passed"。
- 影响：新人/面试官照 README 找不到代码，且会高估/误解项目状态。

#### m2. 评测集自我描述与实际不符，F1=1.0 有"自证循环"风险
- `evaluation/data/labeled_cases.yaml:11` meta 写"38 条"，实际 **36 条**。
- 36 条全为合成句，且注释自承"公司正则在无标点相邻时会贪婪吞并，故例句中公司名前用冒号分隔"——**例句是为了绕开系统已知缺陷而手工构造的**。在此集合上 0 FP / 0 FN / F1=1.0 不能外推到真实文档；`_DEMO_PERSONS = ["张伟","王芳"]` 还硬编码在 runner 里。
- 建议：报告中明确写"合成小样本回归测试，不代表真实环境精度"，并尽快引入真实/脱敏语料复测。

#### m3. `requirements.txt` 不完整，依赖靠"传递依赖"侥幸可用
- 实际 import：`yaml`（`settings.py:99`、`evaluation/dataset.py:10`）、`numpy`（auditor/image/docx）、`lxml`（auditor/docx）、`cv2`、`fitz`。
- `requirements.txt` 只声明了 PySide6/PyMuPDF/python-docx/openpyxl/Pillow/opencv-python/rapidocr-onnxruntime/requests/pytest。`pyyaml`、`numpy`、`lxml` 全部是传递依赖。
- 我在当前 venv 实测均可用（yaml 6.0.3 / numpy 2.4.6 / lxml 6.1.3），但换一台干净机器、换 resolver 时可能因传递依赖版本漂移而装不上。矩阵 B5 已自承"待锁文件"。

#### m4. 公司名正则有误报，但保守地落到"待复核"
- 我在 XLSX A4 放了单元格文本"内部公司"（仅作标签），被 `COMPANY_RE` 命中为公司名 → `needs_review`。
- 行为安全（不会被自动脱敏），但说明正则在真实表格里会制造噪音，增加人工复核负担。

#### m5. 设置项与运行时存在隐性映射，且 GUI 完全不暴露
- `settings.masking.mapping_persistence` 合法值是 `memory/disk`（`settings.py:134`），但 `ConsistencyMapper` 只认 `memory/protected_file`（`mapping.py:27`）；靠 `batch.py:121-122` 把 `"disk"` 翻译成 `"protected_file"`。
- GUI 里模式 B 的 mapper 硬编码 `ConsistencyMapper(scope="batch", persistence="memory")`（`main_window.py:348`），用户没有任何入口切换到落盘映射；配置文件里写了 `mapping_persistence: disk` 在 GUI 路径下不生效。

#### m6. PDF 处理器对图文混排页只警告、不强制人工阻断
- `pdf.py:42-43`：图文混排页（图片多但文字≥40）只 append warning，不阻止导出。README 承诺"图文混排页中的印章/签名默认阻止或提示人工复核"——实际是"提示"，不是"阻止"。

---

### Nit（建议）

- `n1`. `fitz` 顶层 import 抛 `FutureWarning: The fitz API is deprecated`（`pdf.py:6`、`auditor.py:7` 已用 `pymupdf`，但 `fitz` 仍在 pdf.py 使用）。
- `n2`. `application/pipeline.py:324 _write_report` 用裸 `except Exception: pass` 吞掉审计 JSON 写入失败——文件写不出去时静默。
- `n3`. `evaluation/runner.py` 没有 CLI，评测只能写脚本调用；无报告落盘。
- `n4`. `acceptance/` 目录下保留了一份 `baseline_backup/`（旧扁平结构整包）和多份验收文档，容易与真实代码结构混淆，建议标注存档用途。

---

## 3. 我独立验证通过的关键项

为避免只报问题，以下项目均由我亲自跑通、与文档声称一致：

1. **测试套件真实通过**：124 passed / 1 skipped，41.98s，无 error。
2. **三档分流正确**（用自建 DOCX/PDF/XLSX/PNG 合成样本）：
   - "我们公司" → SUPPRESS（不脱敏）；"本公司产品/相关产品" 不进入候选；
   - "中国石油天然气集团有限公司"（公开词库内）→ NEEDS_REVIEW，**既不自动脱敏也不放行**；
   - "中国石油股份有限公司"（公开词库外）→ NEEDS_REVIEW（上下文相关兜底），符合验收口径；
   - 手机/身份证/邮箱/银行卡/固话/黑名单公司/领域词 → AUTO_CONFIRM。
3. **失败关闭**：故意不裁决 NEEDS_REVIEW 项 → finalize 阻断，任务状态"等待人工复核"，产物被 `shutil.move` 到 `output/quarantine/`，**未进入 success 目录**。
4. **原件只读**：同一文件跑完整流水线后，源文件 SHA-256 前后一致（`ec88dea5…` == `ec88dea5…`）。
5. **输出无残留原文**：对成功产物解压 DOCX/XLSX 二进制扫描，全部敏感真值（手机/身份证/邮箱/银行卡/黑名单公司/领域词/公开企业）均不存在；输出文件可被 python-docx 正常打开。
6. **路径越权防护**：`safe_join(root, "../../evil")`、绝对路径 `C:\Windows\…`、`/etc/passwd` 全部抛 `SecurityViolation`。
7. **无外发网络路径**：全仓 grep 唯一出站调用是 `OllamaClient`，且 `trust_env=False`、`proxies={http:None,https:None}`、host 白名单硬编码为回环；`Settings._validate` 再次校验 Ollama URL 必须是回环。
8. **日志脱敏生效**：跑完一轮后回扫 `data/logs/localdocmask.log`，手机/身份证/邮箱/银行卡/公司名原文 **均未出现**。
9. **模式 B 一致性**：同一批次两个 DOCX 中的"杭州云启科技有限公司"都映射为"云帆科技有限责任公司"；映射表仅内存，`data/mapping/` 未落盘。
10. **GUI 真实可启动**：offscreen 下 `MainWindow()` 构造成功，17 个按钮全部 `clicked.connect` 到真实槽函数，`build_services()` 装配成功。
11. **PDF 处理器真实**：`add_redact_annot + apply_redactions`、`set_metadata({})`、`embfile_del`、含表单字段抛错阻断——非占位实现。
12. **评测 runner 真实运行**：不是写死数字，确实跑了 `RecognitionPipeline.analyze` 并按 (类别,文本) 比对。

---

## 4. 对照验收矩阵的抽查

| 矩阵项 | 标注 | 我抽查结论 |
|---|---|---|
| A2 多格式处理 | ✅ | 属实（4 种格式我都跑了） |
| A4 两种脱敏模式 | ✅ | 属实（模式 B 跨文件一致性验证） |
| A6 失败隔离 | ✅ | 属实（失败关闭+隔离目录） |
| C1 原件不变 | ✅ | 属实（SHA-256 比对） |
| C2 误报回归 | ✅ | 属实（泛指抑制、公开企业复核） |
| C3 P/R/F1 | ✅ | 数字真实，但样本见 m2 |
| C5 日志/网络/路径 | 🔶 | 部分属实：网络/日志我验证通过；路径越权有专门测试 |
| B3 恢复/取消 | 🔶 | **实际只有取消是活的**，恢复未接线（M2） |
| E1 任务中心/历史 | 🔶 | repository 查得到，但 recover 未接入 |
| B5/E3/E4/E5/E6 | ⬜ | 属实，确实未做 |

矩阵本身相对诚实（13 项 🔶、6 项 ⬜），没有把未完成项伪装成 ✅。

---

## 5. 开发方可能"自我感觉良好但实际有问题"的点

1. **"F1=1.0"被过度解读**：在自己手写、为绕开正则缺陷而调过标点的 36 条合成句上拿满分，不构成企业级精度证据；对外汇报时应明确这是回归基线，不是生产精度。
2. **"审计 detail 不泄露"是靠代码自觉，不是机制**：头注释写了"调用方不得放入敏感原文"，但 auditor 自己就这么干了，且没人 review 这条契约——建议改成"在落库函数里强制过 mask_display"而不是依赖调用方自觉。
3. **"崩溃恢复已实现"**：代码和测试都在，但 App 启动时根本不调用它。测试绿 ≠ 功能在用户路径上生效。
4. **README 停留在 42 测试/旧扁平结构**：给面试官/新同事看会直接误导。
5. **"图文混排默认阻止导出"**：README 说阻止，代码只 warning。
6. **requirements 没锁版本**：当前机器能跑，换环境可能即崩；"可安装/可启动"在干净虚拟机上尚未验证。

---

## 6. 必须先修才能交付的清单

1. **M1**：audit_findings.detail 强制掩码，补回归测试防原文落库。
2. **M2**：把 RecoveryService.recover 接到启动路径；在 pipeline 关键节点 append_operation；否则在文档/矩阵里诚实降级。
3. **m1**：README 更新到 124 测试、新分层结构。
4. **m3**：requirements 显式声明 pyyaml/numpy/lxml，并产出 lock 文件。
5. **m2**：在评测报告中显著标注"36 条合成小样本，F1=1.0 不代表真实精度"。

完成以上 5 项后，本项目可达到"内部可交付"标准；要达到"对外企业级交付"，还需补真实语料评测、干净环境安装验证、打包/安装文档（矩阵中的 E3/E4/E5）。
