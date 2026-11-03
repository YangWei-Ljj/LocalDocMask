# DevFlow-AI / RepoMind 借鉴映射（LocalDocMask 企业化）

> 目的：参考 DevFlow-AI 的设计思路与 RepoMind 的工程化水平，升级现有 LocalDocMask，而不是另起一个新项目。
> 角色约定：ChatGPT Work 负责分析、规划、验收；Codex 负责读代码、开发、测试、修复。
> 本文是 Codex 侧的技术映射与实施建议，等待 ChatGPT Work 的任务单/验收标准后执行。
> 日期：2026-10-29

## 1. 已阅读的项目与证据

### 1.1 DevFlow-AI（桌面/AI学习课程/DevFlow-AI）

- 项目形态：GitHub PR/Issue 研发协作 AI Agent，Next.js + FastAPI + PostgreSQL/pgvector + Milvus + LangChain + Docker Compose。
- 关键文档：README、docs/architecture/system_design.md、agent_workflow.md、database_schema.md、security.md。
- 关键代码：
  - ActionDraft 模型与 schema：backend/app/db/models.py、backend/app/schemas/action_drafts.py。
  - RBAC 与审计：backend/app/services/permissions.py（ROLE_PERMISSIONS、write_audit_log）。
  - Skill Runtime：backend/app/services/skills/registry.py（SkillManifest、SkillActivation、版本、指令摘要、工具白名单、trace）。
  - 评测：backend/app/services/evals/runner.py、suites.py（固定评测集、哈希、ready/missing_sources、执行状态与质量状态分离）。
  - Agent 工作流：Planner → 专用 Agent → Observer → Synthesis；ActionDraft 只生成草稿，不直接写回。
- 明确不读取：API Key 文件、.env、真实上传文件。

### 1.2 RepoMind（桌面/RepoMind）

- 与 DevFlow-AI 高度同源（研发协作 Agent），工程化更完整。
- 运行手册包含：启动/停止、健康检查 /health、生产模式（uvicorn workers、Next build/start）、验证命令、降级模式（无向量时明确 degraded）、Ollama 启动与排障。
- README 强调：RAG 只做语义召回，实时事实/当前代码回到权威源；评测区分“执行状态”和“质量状态”；评测失败可重试异常评分、回归对比、生成 ActionDraft 而不直接写回。
- 工程资产：分层后端、tests 覆盖、scripts、运行手册、截图、演示数据。

## 2. 可借鉴 / 需改造 / 不可照搬

| DevFlow-AI/RepoMind 模式 | 对 LocalDocMask 的处理 | 理由 |
|---|---|---|
| ActionDraft（pending_confirmation → approved → executed/failed，人工确认后执行） | **改造为 RedactionDraft / ExportDraft**：检测→复核草稿→人工确认→脱敏→审计→导出 | 与脱敏“人工复核 + 失败关闭”天然同构；比直接导出更安全 |
| 每次执行写 AuditLog（user/repo/action/target/status/request/result） | **改造为 task_events + audit_events**（task_id、actor、event_code、from/to、artifact_hash） | 满足企业审计追溯；不复制 repo 概念 |
| RBAC（owner/maintainer/developer/viewer） | **改造为 Authorizer + owner_id + 角色**：本地模式=当前 Windows 用户；服务化再启用角色 | 单机单用户不引入假登录；服务化前先落授权边界 |
| Skill Runtime（manifest/version/tools/trace/instruction_digest） | **改造为 RulePack / FormatHandler 注册表**：规则包/格式处理器有 manifest、version、适用类型、输出契约、版本写入 policy_hash | 复用“版本化 + 可追溯 + 工具白名单”的思想，避免动态脚本执行 |
| 固定评测集 + suite hash + ready/missing_sources | **改造为 PII 检测评测集**：按类别/文件/位置统计，固定集 hash，缺失来源明确 | 现有 evaluation/ 已有 P/R/F1；补发布门禁与回归对比 |
| 执行状态 vs 质量状态分离 | **直接采用**：任务执行结果（成功/失败/取消/超时）与审计/覆盖率结论分开 | 防止“流程跑完”被误认为“质量合格” |
| AgentRun/WorkflowRun trace | **改造为 TaskEvent / StageTrace**：阶段耗时、输入哈希、输出哈希、失败码 | 桌面端不需要多 Agent，但需要可复盘 |
| RAG/向量库/Milvus | **不采用**；用规则/词库/本地 NER/OCR | 脱敏实体匹配以精确规则与词库为主；向量召回无明确收益（ADR-007） |
| 多 Agent 编排（Planner/Observer/Synthesis） | **不采用**；保留确定性流水线 + 可选本地 LLM 辅助 | 脱敏流程确定、可验证；多 Agent 增加不确定性与成本 |
| PostgreSQL + SQLAlchemy + LangChain | **不采用**；沿用 SQLite + 标准库 sqlite3 | 单机单用户、零运维；SQLite 足够且已有迁移机制 |
| Next.js 前端 / Docker Compose | **不采用**；沿用 PySide6 桌面 | 本地优先；若未来要“浏览器上传”，加可选本地 Web 适配器而非重写 |
| 演示用户 ensure_demo_user / 伪造角色 | **禁止照搬** | 这是安全反模式；本地用 OS 身份，服务化用真实认证 |

## 3. “上传文件”的产品形态判断（关键决策）

用户提到“上传文件”，要先明确三种含义：

### A. 桌面本地选择文件（当前形态）
- 用户通过文件选择器选本机文件；不经过网络。
- 安全边界：OS 会话 + ACL + path containment。
- 不需要认证/租户；最适合当前单机单用户。
- **建议默认保持 A**。

### B. 本机浏览器上传（可选本地 Web 适配器）
- 浏览器 → 127.0.0.1 本地服务 → 同一个 application 用例。
- 需要额外：本机 token/会话、上传目录 containment、CSRF、临时文件生命周期、IDOR 测试。
- 不引入多用户/数据库服务化；仍用 SQLite。
- 仅在“希望网页界面/局域网演示”时启用；必须绑定回环地址，默认关闭。

### C. 多用户/远程服务化
- 需要 OIDC/Windows Integrated Auth、RBAC/ABAC、对象存储、队列/并发控制、审计归档。
- 只有在明确多用户/远程场景才做；不是当前默认。
- 若确定走 C，再评估 FastAPI + PostgreSQL + 对象存储；仍不默认 K8s/MQ。

**建议**：先按 A 完成企业化安全与可靠性；若你需要 B，再做一个“本地 Web 适配器”薄层，复用同一 application 服务，不重写核心。

## 4. 目标产品流程（脱敏工具）

1. **选择/上传文件**（本地或可选本地 Web）→ admission（类型/大小/魔数/哈希/路径）。
2. **识别**：规则 + 词库（人名/公司名/关联企业）+ 本地 NER + 可选本地 LLM 候选；融合与风险分级。
3. **生成复核草稿**（RedactionDraft）：每条候选的类别、位置、掩码值、风险、建议动作；默认不写回、不导出。
4. **人工复核**：确认/排除/修正；高风险必须显式确认；LLM-only 永不自动确认。
5. **脱敏执行**：逐项 redact → 失败 delete → 删除验证；原件只读。
6. **独立审计**：重新提取文本/OCR/元数据/隐藏内容；执行状态与质量状态分离。
7. **导出审批**（ExportDraft）：审计通过 + 无未决项 + 产物 hash 绑定 → 原子写入 success；否则 quarantine。
8. **审计与可观测**：task_events、audit_events、metrics、诊断包；可导出审计报告。

## 5. 目标模块边界（在现有分层内增强）

- domain/
  - state_machine.py：显式任务/条目状态转移。
  - review.py：RedactionDraft/ExportDraft 的状态与规则。
- application/
  - desensitization_service.py：prepare/scan/review/process/audit/export 用例。
  - review_service.py：草稿确认/排除/修正、权限校验。
  - batch.py：批次编排、取消、幂等、失败隔离。
  - ports.py：ContentHandler、Detector、LLM、OCR、Auditor、Repository、Authorizer、Metrics、Clock、CancelToken。
- recognition/
  - 规则/词库/上下文/融合；RulePack manifest（版本、适用类型、输出契约）。
- security/
  - admission.py（魔数/容器/资源上限）、auditor.py、path_safety.py、tempfiles.py、redaction/。
- infrastructure/
  - formats/、ocr/、llm/、persistence/（SQLite v2）、config/、logging/、observability/。
- presentation/
  - PySide6 主界面；可选本地 Web 适配器（FastAPI）只做薄层。
- evaluation/
  - PII 检测评测集、指标、回归对比、发布门禁。

## 6. 与现有 17 项能力 + 6 项安全专项的映射

- ActionDraft → 强化 E-01（状态机）、E-12（审计）、E-15（恢复/导出草稿）。
- RBAC/AuditLog → 强化 E-09（认证边界）、E-10（授权）、E-12（审计）。
- Skill Runtime → 强化 E-13（配置/RulePack 版本）、E-02（policy/config_hash 幂等）。
- Eval/RAGAS → 强化 E-14（可观测/质量门禁）、C 类检测评测。
- Trace/AgentRun → 强化 E-14（阶段 trace）、E-15（恢复）。
- 运行手册/health/degraded → 强化 E-14、E-16（部署/运维）。
- RAG 项目隔离 → 强化 E-10（任务/owner 隔离）、S-03（IDOR）。
- 安全文档“不写回、只草稿” → 强化 S-05（提示注入）、S-06（模型边界）。

## 7. 实施阶段建议（等待 ChatGPT Work 的任务单）

> 以下只是阶段边界，不是可执行任务拆分；具体任务由 ChatGPT Work 规划与验收。

- **阶段 0（基线加固，不改业务行为）**：契约测试、健康检查/runbook、评测基线、迁移备份。
- **阶段 1（P0 安全/一致性）**：状态机、幂等唯一键、DB 事务/WAL、admission、原子导出/恢复。
- **阶段 2（P1 可靠性/可运维）**：超时/重试/取消、临时文件 TTL、审计事件链、metrics、配置版本。
- **阶段 3（企业 UX/治理）**：RedactionDraft/ExportDraft GUI、评测门禁、Authorizer/owner（按部署形态）、打包/签名/回滚。
- **阶段 4（可选服务化）**：只有在明确 B/C 形态后，才评估本地 Web/FastAPI/多用户；不默认引入 Docker/MQ/Milvus。

## 8. 需要 ChatGPT Work / 用户确认的事项

1. “上传文件”是 A（桌面选择）、B（本机浏览器）还是 C（多用户/远程）？
2. 是否要求“复核草稿”成为独立持久化实体（RedactionDraft/ExportDraft），还是复用现有 Task 状态即可？
3. 企业审计是否需要防篡改链/HMAC/外部归档？
4. 是否允许为硬超时引入受控子进程（OCR/解析）？
5. 是否允许新增依赖（推荐零新增；内置魔数/WAL/备份）。
6. 分发：是否需要签名/安装器/企业分发？

## 9. 结论

DevFlow-AI 值得借鉴的是**工程骨架与治理模式**：草稿化人工确认、RBAC/审计、Skill/规则版本化、固定评测集、执行/质量状态分离、trace 与健康检查。  
不值得照搬的是**多 Agent、RAG/向量库、PostgreSQL/ Milvus、Docker、Next.js**——这些与当前单机脱敏工具的真实收益不匹配。  
LocalDocMask 应继续保持**模块化单体 + SQLite + PySide6 + 本地模型**，在此基础上补齐企业级安全、可靠性与可运维能力。
