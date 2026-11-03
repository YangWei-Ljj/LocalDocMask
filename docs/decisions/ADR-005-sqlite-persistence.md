# ADR-005：任务与审计状态持久化采用标准库 SQLite

- 状态：Accepted
- 日期：2026-10-26
- 决策者：架构组

## 背景

现状：`Task` 对象全在内存，GUI 关闭后任务丢失；无失败恢复、无幂等；崩溃后 `data/processing/` 残留文件只能手动清理（P1-5）。RepoMind 使用 PostgreSQL + 29 张表，但其规模与服务化形态与本桌面应用不同，不宜照搬。

## 决策

- 使用 Python 标准库 `sqlite3`（零新增依赖、单文件、随本地数据目录存放）做轻量持久化，位于 `infrastructure/persistence/`。
- 核心表：`tasks`（任务状态、源/输出路径、哈希、模式、时间戳）、`task_items`（条目级处置、风险、状态、失败原因）、`audit_findings`（审计结论）、`operation_log`（关键操作与任务关联）。
- 提供显式的 schema 版本号与轻量迁移函数（建表 + 幂等 `ALTER`/重建），不引入 ORM 迁移框架。
- **失败恢复**：启动时扫描持久化表与 `data/processing/`，把"处理中但进程已不存在"的任务标记为 INTERRUPTED，不自动认定其安全；可安全重试的（未产生输出）允许重新入队。
- **幂等**：以（源文件 SHA-256 + 处理模式 + 策略版本）为键，若已存在验证通过的成功产物，提示并可复用/跳过，避免重复导出。

## 安全约束

- 数据库中不保存完整敏感原文；条目值以 `mask_display` 存储，原文仅以哈希/掩码形式出现。
- 数据库文件位于 `data/`（gitignored），权限受限。

## 后果

- 正面：任务可追踪、可恢复、可幂等，且无额外运维负担。
- 负面：需要自行维护轻量 schema 演进；不适合多用户并发写入（本应用为单机单用户，可接受）。

## 实现状态（2026-10-26 对账，不重写决策）

- 落地于 `infrastructure/persistence/sqlite_repo.py` 与 `recovery.py`；schema 版本经 `PRAGMA user_version` + 显式 `_MIGRATIONS`，拒绝库版本高于应用。
- 启动恢复已在 `application/composition.py::build_services()` 接线（`RecoveryService.recover`）；关键节点经 `pipeline.py::_op` 写 `operation_log`。
- 当前恢复以"任务 output_path 是否落盘"判定：无输出回 PENDING，有残留输出标 INTERRUPTED；扫描 `data/processing/` 临时残留文件为预留能力，尚未实现。
- 安全约束已强化：`task_items` 只存 `mask_display` 值，`audit_findings.detail` 在落库前经 `sanitize_detail` 强制掩码。
