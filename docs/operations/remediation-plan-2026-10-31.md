# LocalDocMask 生产发布修复方案与验收单

> 审查基线：`08f3b37b166604a683afdcfcfefa7aa5e5964a35`
> 基线构建 SHA-256：`c9b3ff4192f3f82ab4302a3d3431741ffd4940b518a3c432d3877ce461d212b1`
> 角色：ChatGPT Work 负责计划与独立验收；Codex 负责代码、测试、构建和修复。
> 执行规则：一次只交办一个批次。未通过当前批次独立验收，不进入下一批。
> 禁止项：不得部署、推送、进入 T10；不得修改验收证据冒充通过；不得扩大批次文件范围。

## 0. 当前结论

当前功能链路完整，但存在可让未检查内容进入成功产物的 P0 漏洞，并且发布门禁无法稳定证明“干净提交 + 可重复二进制 + 三项外部验证”。

生产发布必须满足：

- 无未解决的 P0/P1；
- 所有安全修复有“修复前失败、修复后通过”的回归测试；
- 测试、评测、构建、打包后 E2E 全部可重复；
- 发布证据与候选提交/EXE SHA 一致；
- 正式分发包具备签名、版本资源和受控分发方式。

## 1. 全局验收规则

每个 Codex 批次必须返回：

1. 基线提交和目标提交；
2. `git status --short`，必须为空；
3. `git diff --stat` 和完整变更文件列表；
4. 新增/修改测试名称；
5. 修复前失败证据、修复后通过证据；
6. 全量测试原始输出；
7. 适用的构建、评测和证据哈希；
8. 未解决风险与明确声明“本轮未执行后续批次”。

每个批次独立复验：

```powershell
git status --short
git rev-parse HEAD
git diff --check
.venv\Scripts\python.exe -m pytest -o addopts= -p no:cacheprovider --basetemp build\acceptance-pytest -q tests
```

任何命令非零退出、测试通过数下降、候选哈希不匹配、出现未授权文件变更，均判 `REJECTED`。

---

## 第一批：FIX-01 审计失败关闭与 DOCX 嵌入对象

优先级：P0
目标：禁止任何未检查或不可检查的 DOCX/XLSX 容器内容进入成功目录。

### Codex 范围

允许修改：

- `src/localdocmask/infrastructure/formats/docx.py`
- `src/localdocmask/security/admission.py`
- `src/localdocmask/security/auditor.py`
- `tests/test_security.py`
- `tests/test_t4_admission_atomic.py`

### 必须实现的修复

1. DOCX 中出现以下部件时默认阻止导出：
   - `word/embeddings/*`
   - `word/activeX/*`
   - 其他无法确认安全的二进制嵌入对象
2. 对 DOCX 和 XLSX 审计时，任一 XML/REL 部件读取、CRC 校验或解码失败，必须生成 `UNCHECKABLE`，不得 `continue` 后仍判通过。
3. 扫描、处理、审计三个阶段都必须保持一致：扫描给 warning/review flag，处理失败关闭，审计再次兜底。
4. 不新增依赖，不做格式处理器重构。

### 必须新增的测试

1. 构造含 `word/embeddings/oleObject1.bin` 的 DOCX：
   - 准入或处理必须拒绝；
   - 不得产生 success 文件；
   - 原件哈希不变。
2. 构造含损坏 CRC 的内部 XML 的 XLSX：
   - `Auditor.audit()` 必须 `passed=False`；
   - findings 中必须有 `UNCHECKABLE`。
3. 普通 DOCX/XLSX 回归必须继续通过。

### 独立验收标准

```text
正常 DOCX：通过
正常 XLSX：通过
含 OLE 的 DOCX：失败关闭且无 success
XML CRC 损坏的 XLSX：UNCHECKABLE 且无 success
全量测试：不减少、无失败
```

未通过任一项，不进入第二批。

---

## 第二批：FIX-02 文件名与审计记录隐私

优先级：P0
目标：正文、文件名、审计记录均不泄漏敏感姓名或公司名。

### Codex 范围

允许修改：

- `src/localdocmask/application/pipeline.py`
- `src/localdocmask/presentation/gui/main_window.py`（仅在默认导出名需要调整时）
- `tests/test_t7_review_export_identity.py`
- `tests/test_gui.py`

### 必须实现的修复

1. success 目录默认文件名不得复用含敏感内容的原 stem，改为中性格式：
   - `masked_<task_id>.<ext>`
   - `audit_<task_id>.json`
2. 审计 JSON 的 `source_file` 只保存掩码文件名或稳定哈希，不保存原始文件名。
3. GUI 导出默认名也必须使用中性名。
4. SQLite 可在受控本机保留原路径，但不得进入成功目录审计文件。

### 必须新增的测试

使用 `张三_云启公司_合同.docx` 完成真实闭环：

- success 文件名不含“张三”“云启公司”；
- report 文件名不含上述文本；
- report 内容不含原文件全名；
- 正文脱敏、原件哈希、审计门禁保持通过。

### 独立验收标准

```text
敏感原文件名：不得出现在 success 路径或审计 JSON
正文脱敏：继续通过
GUI 默认导出名：中性且可追溯
全量测试：通过
```

---

## 第三批：FIX-03 人名识别能力收口

优先级：P0（产品核心能力）
目标：默认安装状态下，普通中文人名必须能被识别并进入复核/脱敏，而不是静默漏检。

### 推荐方案

随包提供经验证、许可证允许分发的本地 NER 模型：

- 模型文件、词表、标签映射、版本、SHA-256、许可证全部固定；
- PyInstaller 包内可加载；
- 启动健康检查能判断模型缺失/损坏；
- 不得静默降级为“健康但无法识别人名”。

### 外部资源门

Codex 不得自行下载或打包许可证不明的模型。若模型许可证或体积方案未批准，本批状态保持 `BLOCKED_EXTERNAL_RESOURCE`，不得用文档说明代替功能修复。

### Codex 范围

允许修改：

- `src/localdocmask/infrastructure/ner/onnx_ner.py`
- `src/localdocmask/application/composition.py`
- `src/localdocmask/infrastructure/config/settings.py`
- `config/config.example.yaml`
- `LocalDocMask.spec`
- `resources/` 下新增模型清单文件
- `tests/test_ner.py`
- `tests/test_t9_release_engineering.py`

### 必须实现的修复

1. 模型加载必须校验清单和 SHA-256。
2. 启用 NER 后，模型缺失、文件篡改或推理初始化失败必须使健康状态 `degraded/unhealthy`，不得显示 `healthy`。
3. 推理运行时异常不得静默返回空结果并冒充成功；至少持久化 degraded 指标/告警。
4. 默认配置和发布包行为必须一致。

### 必须通过的样例

```text
请联系赵启明处理这份合同。
项目负责人：李思远，联系电话13812345678。
```

要求：

- “赵启明”“李思远”被识别为姓名候选；
- 至少进入 NEEDS_REVIEW，不得静默消失；
- 用户确认后可脱敏且独立审计通过。

### 独立验收标准

```text
包内模型存在且哈希匹配
未知姓名样例识别通过
模型损坏时健康检查失败/降级
普通文档无新增 P0/P1
600 条合成语料报告保留
真实脱敏样本集验收另行通过后方可生产发布
```

---

## 第四批：FIX-04 发布门禁可信化

优先级：P0
目标：脏工作区、二进制不一致和过期外部证据都不能通过正式发布验收。

### Codex 范围

允许修改：

- `scripts/build_package.ps1`
- `scripts/build_metadata.py`
- `scripts/release_acceptance.ps1`
- `scripts/release_db_drill.py`
- `tests/test_t9_release_engineering.py`
- 新增 `tests/test_release_gate.py`（如需要）

### 必须实现的修复

1. 测试和构建前执行 `git status --porcelain`，非空立即失败。
2. `build-info.json.git_dirty=true` 必须阻止正式发布验收。
3. `compare_bundles.identical` 必须同时包含：
   - 元数据身份字段；
   - EXE SHA-256；
   - 完整 `checksums.sha256` 内容哈希。
4. `release_acceptance.ps1` 必须读取并校验 `external-validation-status.json`：
   - release candidate 必须一致；
   - executable SHA 必须一致；
   - 三项外部检查必须为 true。
5. `release_db_drill.py` 只负责数据库演练，不得伪造外部旧版回滚结果。
6. 外部证据缺失、过期或候选不匹配时，验收必须失败，不能写成“通过但未验证”。

### 必须新增的测试

1. 相同身份、不同 EXE 的两个 bundle：`identical=false`。
2. 相同身份、不同内部资源清单：`identical=false`。
3. 脏工作区：发布验收在构建前失败。
4. 过期外部证据：验收失败并给出 reason code。
5. 当前候选匹配证据：验收通过且摘要三项为 true。

### 独立验收标准

```text
dirty worktree -> fail
EXE mismatch -> fail
checksums mismatch -> fail
stale external evidence -> fail
clean matching candidate -> pass
重复运行验收结果稳定，不手工修改 JSON
```

---

## 第五批：FIX-05 打包后完整 E2E

优先级：P0（发布证据）
目标：证明 PyInstaller EXE 内实际包含 OCR、格式处理、审计和导出依赖。

### Codex 范围

允许修改：

- `main.py`
- `LocalDocMask.spec`
- `scripts/build_package.ps1`
- 新增 `scripts/package_e2e_check.py`
- `tests/test_t9_release_engineering.py`

### 最小实现建议

为发布包增加仅用于自动验收的自检入口，例如严格受控的 `--release-self-check <temp-dir>`：

- 不开放任意文件输入；
- 只生成合成 DOCX/XLSX/PNG；
- 运行 prepare→scan→process→audit→finalize；
- 输出 JSON 后退出；
- 不在正常 GUI 启动路径暴露。

### 独立验收标准

```text
打包 EXE 能处理 DOCX：通过
打包 EXE 能处理 XLSX：通过
打包 EXE 能 OCR PNG：通过
原件哈希不变、输出哈希匹配、事件链有效
失败文件进入隔离且无 success
自检结果绑定 EXE SHA
```

---

## 第六批：FIX-06 真正可终止的阶段超时

优先级：P1
目标：PDF/OCR/XLSX 解析卡死时，任务能在预算内终止，不永久占用工作线程。

### Codex 范围

允许修改：

- `src/localdocmask/application/pipeline.py`
- `src/localdocmask/application/batch.py`
- `src/localdocmask/security/limits.py`
- `LocalDocMask.spec`
- `tests/test_t5_cancel_timeout_retry.py`
- 新增 `src/localdocmask/application/process_guard.py`（仅在必要时）

### 必须实现的修复

1. 高风险解析/OCR/处理进入受控子进程。
2. 超时后父进程能 terminate/kill 子进程。
3. 超时任务进入 `TIMED_OUT`，无 success 文件，临时文件进入 quarantine。
4. PyInstaller 下支持 `freeze_support`，自检覆盖实际打包环境。
5. 不新增第三方依赖。

### 必须新增的测试

使用会永久阻塞的假 handler：

- 主测试进程在超时后 3 秒内返回；
- 子进程已被终止；
- 数据库状态为 TIMED_OUT；
- 无 success 产物；
- 原件哈希不变。

### 独立验收标准

```text
协作式正常取消：继续通过
真正卡死 handler：硬超时通过
并发两个任务：互不串扰
PyInstaller 打包后硬超时：通过
```

---

## 第七批：FIX-07 映射持久化安全收口

优先级：P1
目标：不再留下未加固的明文映射文件。

### 推荐方案（最简）

当前 GUI 不依赖磁盘映射，直接移除或禁用 `mapping_persistence=disk`：

- 配置加载时明确拒绝 `disk`；
- 文档标记为暂不支持；
- 保持默认 `memory`。

若必须保留磁盘映射，先单独审批 DPAPI 加密方案；不得仅靠可失败的 `icacls`。

### Codex 范围

允许修改：

- `src/localdocmask/security/redaction/mapping.py`
- `src/localdocmask/infrastructure/config/settings.py`
- `config/config.example.yaml`
- `tests/test_mapping.py`

### 独立验收标准

```text
memory 模式：通过
disk 配置：启动时明确拒绝
不得生成 mapping.protected.json
不得吞掉权限加固失败
```

---

## 第八批：FIX-08 SBOM、文档与审计报告一致性

优先级：P1/P2
目标：发布材料反映真实包内容，不出现多套状态。

### Codex 范围

允许修改：

- `scripts/build_metadata.py`
- `docs/operations/release_runbook.md`
- `README.md`
- `docs/feature_list.md`
- `docs/quality_report.md`
- `acceptance/enterprise_acceptance_matrix.json`
- `acceptance/final_acceptance_report.md`
- `tests/test_t9_release_engineering.py`

### 必须完成的修复

1. SBOM 只列实际打入 bundle 的运行时组件；不得包含未发布的 torch/transformers/pytest。
2. README、验收矩阵和最终报告只保留一个当前状态来源。
3. 旧版报告标为历史归档，不参与当前候选验收。
4. 导出的审计 JSON 必须包含：
   - `audit_passed`
   - findings/coverage
   - policy_version
   - config_hash
   - tool_version

### 独立验收标准

```text
SBOM 与 bundle dist-info/二进制清单一致
未发布包不出现在 SBOM
README/验收矩阵/报告无冲突
审计 JSON 可单独证明审计结论
```

---

## 第九批：FIX-09 正式分发门禁（外部资源）

优先级：生产发布门禁
目标：从“本地绿色目录”升级为可受控企业分发制品。

### 前提资源

- 企业代码签名证书及安全签名环境；
- 企业批准的安装器/分发方式；
- Windows 版本支持矩阵。

### 必须完成

1. EXE 写入 FileVersion/ProductVersion/CompanyName 等版本资源。
2. Authenticode SHA-256 签名并时间戳。
3. 安装器或企业 ZIP 签名。
4. 验证签名状态为 `Valid`。
5. 保留上一篇旧版正式制品及 SHA，完成真实升级/回滚。
6. 版本号、发布说明、撤回流程和升级说明。

### 独立验收标准

```text
Get-AuthenticodeSignature -> Valid
版本资源非空
安装/卸载/升级/回滚均通过
候选 SHA 与发布清单一致
三项外部验证仍为 true
```

---

## 2. 执行顺序与发布门槛

必须按以下顺序：

1. FIX-01 审计失败关闭与 DOCX 嵌入对象
2. FIX-02 文件名与审计记录隐私
3. FIX-03 人名识别能力
4. FIX-06 硬超时
5. FIX-07 映射持久化安全收口
6. FIX-04 发布门禁可信化
7. FIX-05 打包后完整 E2E
8. FIX-08 SBOM、文档与审计报告一致性
9. FIX-09 正式分发门禁

FIX-04 和 FIX-05 是最终候选发布门禁。任何后续代码修改都必须重新执行这两批，旧候选的通过证据自动失效。

允许在同一批次内合并强耦合修复，但禁止跨批次顺手重构。

## 3. 最终验收输出格式

Codex 每批完成后必须按以下格式提交：

```text
BATCH: FIX-0X
BASE_COMMIT:
HEAD_COMMIT:
CHANGED_FILES:
TESTS_ADDED:
BEFORE_FIX_REPRODUCTION:
AFTER_FIX_RESULT:
FULL_TEST_RESULT:
ARTIFACT_SHA256:
EVIDENCE_PATHS:
UNRESOLVED_RISKS:
NEXT_BATCH_STARTED: false
DEPLOYED: false
PUSHED: false
T10_STARTED: false
```

ChatGPT Work 只输出：

- `ACCEPTED`：本批次满足验收，允许进入下一批；
- `REJECTED`：列出 P0/P1、证据路径和复现命令；Codex 只修当前批次。

最终全部批次通过后，才允许更新发布候选并再次执行全量独立验收。仍不得自动部署、推送或进入 T10。
