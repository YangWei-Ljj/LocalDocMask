# LocalDocMask 项目验收矩阵（A–E 最终验收）

> 来源：用户正式《项目验收追踪清单》（2026-10-26）
> 三态定义：
> - ✅ **已实现并验证**：真实实现 + 测试/运行证据
> - 🔶 **已实现但未充分验证**：有实现，验证不充分（注明缺口）
> - ⬜ **尚未实现**：未做；"必要时/按需"项且已论证放弃，标注 **已论证放弃（见 ADR）**
>
> 每项附证据：文件路径 / 测试名 / 命令输出。
>
> **最终更新：2026-10-29（全量 157 passed / 1 skipped，共 158 用例；本地 LLM 真实调用验证；本地中文 NER 可插拔；600 条合成语料两路径评测；打包 exe 构建成功）**
>
> 说明：清单原文标题为"30 项"，实际逐项枚举为 29 个复选框（A=6、B=5、C=6、D=6、E=6），本表按实际枚举项逐条验收。

## A. 核心业务闭环

| # | 验收项 | 状态 | 证据 |
|---|---|---|---|
| A1 | 多格式文件导入、格式检测和批量任务 | ✅ | 批量 `application/batch.py`（BatchService）；格式检测 `infrastructure/formats/registry.py`（handler_for/supported_extensions）；`tests/test_batch.py`（8 例）、`test_integration.py`；`demo/run_demo.py` 实跑 5 文件 |
| A2 | PDF、DOCX、XLSX、图片及扫描件处理 | ✅ | `infrastructure/formats/{pdf,docx,xlsx,image}.py`；扫描件 OCR `infrastructure/ocr/engine.py`；`tests/test_pipeline.py`、`test_integration.py`（4 格式 PASSED） |
| A3 | 敏感信息识别、上下文判定与规则配置 | ✅ | `recognition/{context,knowledge,fusion,rule_based}.py`；规则文件 `resources/*.txt`；`tests/test_context.py`、`test_fusion.py`、`test_integration.py`；GUI 左侧配置执行前生效 |
| A4 | 星号遮盖和一致性虚构信息两种模式 | ✅ | `security/redaction/{masker,mapping}.py`；`tests/test_mapping.py`、`test_masker.py`、`test_integration.py` |
| A5 | 脱敏预览、人工复核和重新处理 | ✅ | 前后并排预览、复核门禁、可改后重处理；`tests/test_gui.py`（6 例）、`test_integration.py`；截图 02/03 |
| A6 | 独立残留检查、安全导出与失败隔离 | ✅ | `security/auditor.py`（独立复查）；finalize 门禁；`tests/test_gate.py`、`test_integration.py`、`test_batch.py` |

## B. 工程化基础

| # | 验收项 | 状态 | 证据 |
|---|---|---|---|
| B1 | 清晰的业务分层和模块边界 | ✅ | domain/recognition/application/infrastructure/security/presentation/evaluation；ADR-001；`faf7479` 行为保持迁移 |
| B2 | 统一配置、异常、日志与类型约束 | ✅ | `infrastructure/config/settings.py`（YAML+校验）；`domain/exceptions.py`（错误码层级）；`infrastructure/logging/logger.py`；全量类型注解 |
| B3 | 任务状态、进度、取消与失败恢复 | ✅ | CancelToken + progress_cb（`batch.py`）；GUI 取消按钮；`RecoveryService` 启动时真实接线（`composition.py`）；`tests/test_major_fixes.py`、`test_batch.py` |
| B4 | 本地持久化、幂等控制和资源限制 | ✅ | `sqlite_repo.py`（4 表+迁移）；find_by_source 幂等、BatchService 自动跳过（`test_batch_idempotent_skip_and_force`）；`security/limits.py`（大小/线程/超时）；`tests/test_persistence.py`、`test_security_hardening.py` |
| B5 | 依赖锁定、环境诊断与可重复启动 | ✅ | `pyproject.toml`、`uv.lock`（41 包）；`scripts/diagnose_env.py` 实跑 22 OK/1 WARN/0 FAIL；`scripts/run_headless.ps1`；PyInstaller 构建离屏冒烟通过 |

## C. 质量与安全

| # | 验收项 | 状态 | 证据 |
|---|---|---|---|
| C1 | 原件不变的自动化验证 | ✅ | source_hash 前后比对；`tests/test_gate.py`、`test_integration.py`；demo 5/5 一致 |
| C2 | 普通描述和公开企业名称误报回归集 | ✅ | `tests/test_context.py`、`test_integration.py`、`test_gui.py`、`test_company_boundary.py`（本公司产品/我们公司/有限公司→抑制；公开企业→复核；无标点相邻公司名不吞前导成分） |
| C3 | 不同敏感实体类型的精确率与召回率评测 | ✅ | `evaluation/{runner,metrics}.py`；`evaluation/data/labeled_cases.yaml`（**101 条**合成）；`tests/test_evaluation.py`；真实 P/R/F1 见 `docs/quality_report.md` |
| C4 | 隐藏内容、元数据和 OCR 结果复查 | ✅ | `security/auditor.py`（隐藏表/行列/脚注/批注/元数据/嵌入对象/OCR）；`tests/test_security.py`、`test_integration.py`；demo 隐藏表内容被检出 |
| C5 | 日志泄露、外部网络访问和路径权限检查 | ✅ | `tests/test_security.py`（回环/禁代理）、`tests/test_security_hardening.py`（test_sensitive_values_redacted_in_json_log、test_dotdot_escape_blocked、test_absolute_escape_blocked） |
| C6 | 失败关闭与独立导出验证 | ✅ | finalize 失败关闭+隔离；`tests/test_gate.py`、`test_integration.py`；损坏文件隔离不污染成功文件（demo 实测） |

## D. AI 高级能力

| # | 验收项 | 状态 | 证据 |
|---|---|---|---|
| D1 | 规则引擎与本地 LLM 协同 | ✅ | `RecognitionPipeline` llm 插槽；生产 scan 调 `_llm_supplement`（`pipeline.py:111`）；**真实调用 qwen3.5:9b 验证**：返回（公司名=杭州云启科技有限公司、姓名=张伟），模型漏的手机由规则补齐；模型候选低权重、不参与最终安全判定 |
| D2 | 识别、复核和质量审计工作流 | ✅ | `application/pipeline.py`（识别→复核门禁→独立审计→门禁导出）；`tests/test_integration.py` |
| D3 | 受控工具注册、参数验证和调用追踪 | ✅ | `formats/registry.py` 受控注册；Settings 参数校验；`operation_log` 已接线并在真实运行落四阶段记录（`test_operation_log_populated_after_real_run`） |
| D4 | 必要时增加多 Agent 协作 | ⬜ 已论证放弃 | ADR-007：当前规模单流水线 + 确定性规则即可闭环，多 Agent 重复调模型无质量收益 |
| D5 | 必要时增加本地 RAG 策略知识库 | ⬜ 已论证放弃 | ADR-007：文件型 `resources/*.txt` 已满足策略/术语管理，无需向量库与运维成本 |
| D6 | 按需增加安全受控的 MCP 工具接口 | ⬜ 已论证放弃 | ADR-007：无外部消费方与真实使用场景，不为此增加攻击面 |

> D4/D5/D6 为"必要时/按需"项：按 ADR-007 论证，当前业务无充分收益，默认不实现；若后续出现真实业务问题再重走论证。**关键安全判定不依赖模型自我确认，由确定性规则 + 独立审计 + 必要人工审批形成闭环。**

## E. 产品与交付

| # | 验收项 | 状态 | 证据 |
|---|---|---|---|
| E1 | 任务中心、历史记录与质量报告 | ✅ | GUI"任务中心/历史"Tab；`repository.list_tasks()`；`docs/quality_report.md`；截图 07 |
| E2 | 专业的文件预览和脱敏前后对比 | ✅ | 预览 Tab 左右并排原文/脱敏后；模式 B 用共享 mapper 稳定虚构值；截图 02 |
| E3 | 完整 README、架构图和操作手册 | ✅ | `README.md`（重写）；`docs/architecture/diagrams.md`（5 张 Mermaid）；`docs/guides/user_manual.md` |
| E4 | 安装、配置、打包和故障排查 | ✅ | `docs/guides/development_guide.md`；`config/config.example.yaml`；`scripts/build_package.ps1`（onedir exe 约 390MB，离屏冒烟 25s）；故障排查章节 |
| E5 | 合成演示数据、演示流程和截图 | ✅ | `demo/data/`（5 合成文件）、`demo/run_demo.py`、`demo/demo_output.txt`；`docs/demo_flow.md`；`docs/screenshots/01..08.png` |
| E6 | 求职技术亮点与真实项目成果总结 | ✅ | `docs/job_highlight.md`（中英双语，每条挂代码/测试位置）；不含虚构性能/用户规模/企业落地 |

## 最终统计

- ✅ **已实现并验证：26 / 29**
- ⬜ **尚未实现（均已论证放弃，见 ADR-007）：3 / 29**（D4 多 Agent、D5 RAG、D6 MCP）
- 🔶 已实现但未充分验证：0
- 安全红线（原件不改、默认不外发、失败关闭、映射保护、路径/日志收口）全部经独立测试与独立审查验证。

## 仍需如实说明的边界（不影响勾选，见 docs/feature_list.md）

- 101 条为**合成样本**，F1=1.0 是受控句式回归基线，不代表真实文档分布；需真实语料持续校准。
- 公司正则贪婪吞并已通过左边界回退裁剪修复（常见甲乙方/代词/动词前缀不再被吞）；并已接入**可选本地中文 NER（默认关闭，ADR-009）**改善连续文本边界与词典外人名召回；**残余风险**：NER 模型自身误差（LOC 碎片、罕见 ORG 前导成分）、单字连接字、长文跨块截断。
- 打包 exe 冒烟验证"进程存活+服务装配"，未在 exe 内逐文件跑完整处理；干净第三方机器安装未单独实测（已由 uv.lock + 诊断 + 本机构建覆盖大部分风险）。

---

## 2026-10-27 证据更新

- C2（公司名边界）：✅ 已实现并验证——`patterns.trim_company_prefix` 剥离甲乙方/代词/动词前缀；`tests/test_company_boundary.py` 6 例；残余风险（单字连接字/无标点动词串）见 quality_report §7。
- C3（分层识别 + 评测）：✅ 已实现并验证——标注集 101 条合成；真实评测 TP=69/FP=0/FN=0，F1=1.0；抑制 46/46；决策 AC=51/NEEDS_REVIEW=18/SUPPRESS=22。
- 全量测试：138 passed / 1 skipped。

---

## 2026-10-28 证据更新：真实语料闭环工具 + 本地中文 NER

- **任务一（真实语料闭环工具，f0097e8）：✅ 已实现并验证**
  - 标注导入器 `evaluation/annotation.py`（真实文件文本提取+按句切分+待标注草稿，provenance）；坏例导出/合并/词库回灌 `evaluation/badcase.py`（默认 dry-run，--apply 才写）；趋势落盘与对比 `evaluation/trend.py`；高拟真合成语料 `evaluation/synthetic_corpus.py`（显著标注 synthetic）；统一 CLI `scripts/corpus_cli.py`；手册 `docs/guides/corpus_annotation_manual.md`；`tests/test_corpus_tooling.py` 6 例。
  - 真实跑通（合成 120 条）：P=0.9554/R=0.7364/F1=0.8317，坏例 78 条，trend regression_flag=false。
- **任务二（本地中文 NER，2824b17，ADR-009）：✅ 已实现并验证（默认关闭、可插拔、失败关闭）**
  - ONNX-BERT 中文 NER，运行时仅 onnxruntime+numpy（零新增运行时依赖）；source=ner 低权重、仅模型来源上下文类转 NEEDS_REVIEW，不参与残留/导出判定；`tests/test_ner.py` 12 例。
  - 真实 A/B：101 集 off F1=1.0 → on F1=0.9787（+3 FP 为 LOC 碎片，召回不变）；新增 4 反例召回 0.25→0.75，词典外裸人名 0/2→2/2。
- **全量测试：155 passed / 1 skipped，共 156 用例**（138 基线 + 6 语料工具 + 12 NER；口径：passed=总数-skipped）。
- 仍需用户完成：真实语料采集与人工真值标注（合成不可替代）；NER 是否默认开启待真实语料 A/B；干净第三方机器安装未实测。

---

## 2026-10-29 证据更新：LOC 粘合 + 合成语料扩到 600 条

- **NER 后处理（8b60020）：✅ 已实现并验证**——相邻 LOC 碎片（中间仅允许标点/空白）贪心粘合为完整地址，并向后吸收门牌号（号/室/栋/层/楼等）；跨动词/ORG/PER 即停，保守不发明文本。真实离线：`北京市朝阳区望京街8号` 由 3 碎片变为**单个完整 span**；`…北京市朝阳区，与杭州云启…洽谈` 中地址到逗号即止、不吞公司名。新增 `test_real_loc_fragments_merged_into_one_address`、`test_real_loc_merge_does_not_cross_boundary`。
- **合成语料扩量（e32a6f2）：✅ 已实现并验证**——`synthetic_corpus.py` 产出 **600 条**多体裁合成（合同/邮件/表格/扫描 OCR/泛指负例/公开机构），全部 provenance.synthetic=true。
  - 两路径真实评测（Ollama 关闭）：

| 路径 | TP | FP | FN | P | R | F1 | 复核率 |
|---|---|---|---|---|---|---|---|
| NER 关 | 1331 | 57 | 420 | 0.9589 | 0.7601 | 0.8480 | 29.2% |
| NER 开 | 1707 | 133 | 44 | 0.9277 | 0.9749 | 0.9507 | 44.2% |

  - 关键变化：NER 使召回 0.760→0.975（人名 0.39→1.0、公司名 0.63→1.0），代价是 FP 上升（主要为地址类 NER 重复识别，转 NEEDS_REVIEW）；抑制准确率两路径均 1.0。
  - 确定性修复：领域词/编号/地址真值与 resources、检测器对齐；trend 已落盘并对比（regression_flag 由 FP 上升触发，属预期召回-精确权衡，非质量回退）。
- **全量测试：157 passed / 1 skipped，共 158 用例**（156 + 2 个 LOC 粘合用例）。
- 根本边界：600 条为高拟真合成，**不能替代真实文档**；真实精度仍需用户后续采集真实语料并人工标注。
