# LocalDocMask 重构路线图

> 版本：v1.0 日期：2026-10-26
> 执行日志：`docs/roadmap/execution_log.md`（按时间记录命令、结果、证据）

## 阶段总览

| 阶段 | 目标 | 关键交付 | 状态 |
|---|---|---|---|
| 0 | 工作区与安全准备 | 路径/Git/环境核实、基线提交 | ✅ 完成 |
| 1 | RepoMind 基准 | `docs/project_audit/repomind_baseline.md` | ✅ 完成 |
| 2 | LocalDocMask 现状 | `docs/project_audit/localdocmask_current_state.md` | ✅ 完成 |
| 3 | 差距与目标设计 | architecture/decisions/roadmap | ✅ 完成 |
| 4 | 核心安全底座 | domain/配置/日志/异常/端口/持久化 | ⏳ 进行 |
| 5 | 核心功能重构 | 分层识别/上下文/双脱敏/独立验证 | ⏳ |
| 6 | 高级能力（按需） | 论证结论（默认不引入） | 待定 |
| 7 | UI 闭环 | 真实联调、复核/进度/失败/结果一致 | ⏳ |
| 8 | 自动验收回归 | 全量测试、安全/异常/回归、独立审查 | ⏳ |
| 9 | 部署文档交付 | README/手册/报告/演示/求职亮点 | ⏳ |

## 阶段4：核心安全底座（任务拆分）

1. 建立 `domain`：enums/entities/exceptions/policy（迁入并扩展 models）。
2. 建立 `infrastructure/config`：YAML + dataclass 校验 + 安全默认；产出 `config/config.example.yaml`。
3. 建立 `infrastructure/logging`：结构化日志 + 敏感值过滤。
4. 建立 `application/ports`：定义 Protocol。
5. 建立 `infrastructure/persistence`：SQLite + schema 版本 + 恢复/幂等。
- 每步单元测试，保持 `pytest` 绿。

## 阶段5：核心功能重构

1. 迁移 `formats/` → `infrastructure/formats`，保留全部隐藏内容检查。
2. `recognition`：patterns/rule_based/context/knowledge/fusion（误报治理核心）。
3. 词库资源：`resources/{whitelist,blacklist,public_entities,domain_terms}.txt`。
4. `security/redaction`：双模式 + ConsistencyMapper（批次一致、冲突检测、保护）。
5. 迁移 `security/auditor`，保持独立；接入 `security/tempfiles`。
6. `application/pipeline` + `batch`：编排、并发/取消/超时/失败隔离。
- 每模块单元 + 集成测试。

## 阶段6：高级能力

- 依据 ADR-007：默认不引入多 Agent/RAG/MCP 等；仅在出现真实业务问题时重走论证。

## 阶段7：UI 闭环

- 迁移 GUI → `presentation/gui`；接入 pipeline/batch；复核交互、进度、取消、失败隔离、审计/结果一致；无假按钮、无虚构进度。

## 阶段8：自动验收与回归

- 全量 pytest；误报/漏报回归集；安全测试（路径越权/日志泄露/外传/残留/临时文件/失败关闭）；稳定性（取消/损坏/并发/恢复）；独立验收审查与修复闭环。

## 阶段9：部署与文档

- README、架构/流程图、ADR、开发/部署/运行指南、用户手册、安全威胁分析、测试与质量报告、可重复演示与合成数据、功能清单与已知限制、以证据为基础的求职亮点。

## 里程碑与提交纪律

- 每个有意义的里程碑做一次 git 提交（不 reset/覆盖）。
- 提交信息按 `feat/fix/refactor/test/docs/chore` 前缀。
- 安全、正确性、核心业务优先于扩展与美化。
