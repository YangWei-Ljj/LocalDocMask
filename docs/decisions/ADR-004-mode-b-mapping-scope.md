# ADR-004：模式 B 一致性虚构映射的作用域、持久化、冲突处理与保护

- 状态：Accepted
- 日期：2026-10-26
- 决策者：架构组

## 背景

现状：模式 B（一致性虚构）映射表仅存于单个 `Masker` 实例内存（`masker.py:24`），无 save/load；`TaskManager.process` 每次都 new 一个 `Masker`（`task_manager.py:59`），导致批量处理时同一真实实体在不同文件中被映射为不同虚构值，跨文件/跨任务不一致（P1-3）。池耗尽后按索引取模循环，不检测冲突。

总控指令要求明确：映射作用域、持久化策略、冲突处理、映射表保护；映射表本身是敏感资产，不得写入普通日志或未经保护的报告。

## 决策

将映射能力收敛为 `security/redaction/mapping.py` 的 `ConsistencyMapper`，并规定：

1. **作用域（scope）**：
   - 默认 `batch`：一个批次（一次批量运行）共享同一个 Mapper，保证同一真实实体在该批次所有文件中映射为同一虚构值（修复 P1-3）。
   - 可选 `task`：仅单文件内一致（等同旧行为）。
   - 可选 `global`：跨批次/跨会话一致（需开启持久化，见下）。
2. **持久化策略（persistence）**：
   - 默认 `memory`：不落盘，进程结束即失效，不产生新的静态敏感资产。
   - 可选 `protected_file`：仅在用户显式开启时，写入 `data/mapping/`（gitignored），文件以最小权限（仅当前用户可读写）保存；该目录受保护、不进日志/报告。
   - 映射文件不与原始文档、审计报告、普通日志同放。
3. **冲突处理（conflict）**：
   - 分配采用确定性顺序；维护"已占用虚构值"集合，检测两个真实实体是否被映射到同一虚构值，命中则自动取下一个未占用值。
   - 池耗尽时确定性扩展，不静默复用造成碰撞；无法分配时抛领域异常并使该文件失败关闭。
4. **保护（protection）**：
   - 映射表严禁写入日志；日志/报告中只出现经 `mask_display` 处理的内容。
   - 访问器默认不返回明文映射；持久化文件权限受限并在安全文档中登记。

## 后果

- 正面：跨文件一致性得到保障；映射表全生命周期可控、可解释。
- 负面：持久化开启后会产生一个需保护的敏感文件，因此默认关闭并明确告知。
- 约束：默认不创建静态敏感资产；任何落盘均为显式 opt-in。

## 实现状态（2026-10-26 对账，不重写决策）

- 落地于 `security/redaction/mapping.py::ConsistencyMapper`；`scope ∈ {batch,task,global}`、`persistence ∈ {memory,protected_file}`，`global` 强制要求持久化。
- 配置层 `settings.masking.mapping_persistence` 取值为 `memory/disk`，由 `application/composition.py::build_mapper` 翻译为内部 `memory/protected_file`；GUI 模式 B 默认 `scope=batch, persistence=memory`。
- 冲突处理：确定性序列 + 已占用集合顺延，`_MAX_ALLOC_ATTEMPTS` 上限后抛 `MappingCollisionError` 使文件失败关闭；落盘文件用 `icacls` 收紧为仅当前用户（受限环境失败时降级不中断）。
