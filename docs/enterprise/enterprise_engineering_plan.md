# LocalDocMask 企业级工程化改造方案（待批准）

> 状态：设计稿。本轮只输出方案与验收矩阵，不修改代码、不拆分开发任务。
> 日期：2026-10-29（Asia/Shanghai）
> 依据：ADR-001 ~ ADR-009、docs/architecture/module_contracts.md、target_architecture.md、docs/security/threat_model.md、当前代码与实测测试（158 收集 / 157 通过 / 1 跳过）。

## 0. 结论摘要

1. **继续采用模块化单体（modular monolith）**，不引入 Kubernetes、消息队列、Redis、微服务或 Docker。
   - 依据：单机单用户桌面、批次顺序处理、SQLite 单文件、GUI 2 个工作线程、单文件默认上限 50MB；当前规模下分布式组件只会增加运维面和故障点（ADR-007）。
   - 重新评估条件：出现远程上传/多人并发/跨机批处理/7x24 服务化需求，且单体无法通过背压与队列满足时。
2. 现有架构已具备企业级骨架：分层包、端口/组合根、SQLite 持久化、显式迁移、结构化日志与脱敏、路径遏制、独立审计、失败关闭、158 项测试。
3. 主要缺口不在“功能”，而在**一致性、可恢复性和可运维性**：
   - 状态机是隐式的，未知状态会回退为 PENDING（危险）。
   - 幂等键只有普通索引，不是唯一约束；并发下可能重复。
   - 超时是阶段边界协作式检查，无硬超时；无重试。
   - SQLite 单连接跨线程、无 WAL/busy_timeout/写锁；事务边界不显式。
   - 内容准入只看扩展名 + 各处理器自行校验；无魔数嗅探、压缩比/页数/像素上限。
   - 临时文件保留天数配置存在但未执行；成功目录可能残留崩溃时的半成品。
   - 身份认证/资源授权在单机单用户下不引入“假登录”，以 Windows 会话 + ACL 为边界；定义 Principal/Authorizer 端口，服务化时再实现。
4. 安全红线保持不变：原件只读、失败关闭、无外发、独立审计、未验证不导出。

## 1. 范围、前提与判定依据

### 1.1 产品形态（现状）

- Windows 10/11 桌面应用，PyInstaller onedir（dist/LocalDocMask/LocalDocMask.exe 已存在），单用户交互式使用。
- 本地文件选择 → 扫描 → 人工复核 → 处理 → 独立审计 → 成功/隔离分目录。
- 可选本地 Ollama（默认关闭，仅回环）与本地 ONNX NER（默认关闭）。
- 数据目录 data/：input、processing、output/success、output/quarantine、logs。

### 1.2 规模假设（用于架构判断，不是虚构指标）

- 单批文件数：当前顺序处理；建议目标上限 200 个/批（可配置）。
- 单文件：默认 ≤50MB（已实现入口拒绝）；页数/像素/解压比上限待补。
- 并发：GUI 线程池默认 max_workers=2；批处理顺序。
- 存储：SQLite 单文件 + 文件系统；无外部数据库/对象存储。
- 结论：**不需要**消息队列、分布式锁、服务注册、容器编排；需要的是单体内的状态机、事务、幂等、背压与恢复。

### 1.3 已批准架构约束（必须遵守）

- 依赖方向：presentation → application → domain；recognition → domain；security → domain；infrastructure 实现端口；domain 不依赖业务包（ADR-001）。
- 失败关闭：任何异常、超时、未知状态、无法检查 → 不得导出（threat_model / module_contracts）。
- 审计独立：审计器重新读取实际输出文件，不信任处理模块内存标志。
- 数据最小化：数据库/日志/审计记录只存掩码值、哈希与元信息；映射默认仅内存。
- 网络边界：唯一出站是本地 Ollama，强制回环 + 禁代理 + 默认关闭。

## 2. 现状实现与证据

| 能力 | 现有实现 | 证据（文件/测试） | 状态 |
|---|---|---|---|
| 分层与端口 | domain/recognition/application/infrastructure/security/presentation/evaluation | ADR-001；src/localdocmask 各包；module_contracts.md | ✅ |
| 任务状态 | TaskStatus 8 态 + ItemStatus 9 态 | domain/enums.py；pipeline.py 状态赋值 | 🔶 隐式转移 |
| 幂等 | key=(source_hash, mode, policy_version)，普通索引 + find_by_source + 批次跳过 | pipeline.py POLICY_VERSION；sqlite_repo.py idx_tasks_idem；batch.py skip；recovery.existing_success | 🔶 非唯一 |
| 超时 | 单任务 deadline，阶段边界 _check_deadline | security/limits.py；pipeline.py 第 55/100/145 行；settings 默认 1800s | 🔶 协作式 |
| 重试 | 无 | 代码检索无 retry/backoff；batch.py 异常即 failed | ⬜ |
| 取消 | CancelToken 批次间检查 + GUI 取消按钮 | application/batch.py；presentation/gui/main_window.py 第 211-213/823-850 行 | 🔶 文件间 |
| DB 事务 | 单连接 check_same_thread=False；save_task 多语句后 commit；operation_log 单独 commit | sqlite_repo.py 第 264、290-373、375-385 行 | 🔶 无显式事务/锁 |
| 并发限制 | settings.max_workers=2；GUI setMaxThreadCount；BatchService 顺序 | main_window.py 第 119-120 行；settings.py | 🔶 共享连接无锁 |
| 文件安全 | 扩展名白名单、大小上限、路径 containment、哈希绑定；处理器自行校验 zip/PDF/图片 | pipeline.py 第 78-82/289/312/385 行；security/path_safety.py；limits.py；formats/* | 🔶 无魔数嗅探/资源上限 |
| 身份认证 | 无应用登录，依赖 Windows 用户会话 | threat_model 信任边界；无 auth 模块 | ⬜ 单机不适用 |
| 资源授权 | 无应用级 owner/tenant，依赖 OS ACL + 文件选择器 | 同上；无 HTTP 下载接口 | ⬜ 单机不适用 |
| 临时文件 | processing/success/quarantine 分目录；GUI 手动清理 | main_window.py 第 221-222/900-910 行；settings.tempfile_retention_days 未被使用 | 🔶 无自动 TTL |
| 审计日志 | JSON 日志 + RedactionFilter + 候选值登记；SQLite operation_log；审计记录 JSON；detail 落库前 sanitize | infrastructure/logging/logger.py；sqlite_repo.sanitize_detail；tests/test_security_hardening.py | 🔶 无防篡改/保留执行 |
| 配置管理 | dataclass Settings + YAML + LOCALDOCMASK_HOME + 校验 + 示例 | infrastructure/config/settings.py；config/config.example.yaml；ADR-006 | 🔶 无 schema 版本/热加载 |
| 可观测性 | 结构化日志、operation_log、任务记录、评测指标、环境诊断、headless 冒烟 | logger.py；sqlite_repo；evaluation/；scripts/diagnose_env.py；scripts/headless_check.py | 🔶 无 metrics/tracing |
| 异常恢复 | RecoveryService 启动时把 PROCESSING 重新归类 | infrastructure/persistence/recovery.py；composition.py 接线；ADR-005 | 🔶 无孤儿文件对账 |
| 部署 | PyInstaller onedir、构建脚本、dist 产物、离屏冒烟 | LocalDocMask.spec；scripts/build_package.ps1；dist/LocalDocMask/LocalDocMask.exe | 🔶 无签名/安装器/CI |
| 数据迁移 | PRAGMA user_version + 显式 _MIGRATIONS；拒绝高版本 | sqlite_repo.py SCHEMA_VERSION=1、initialize_database | 🔶 无备份/回滚工具 |

## 3. 目标架构（在已批准分层内增强）

### 3.1 模块边界

- domain/
  - 新增 state_machine.py：显式状态转移表 + transition() 守卫；禁止业务代码直接赋值 status。
  - entities 扩展：Task 增加 attempt、cancel_requested、timeout_at、owner_id(可空)、config_hash、batch_id；Candidate 增加 source_scope。
- application/
  - pipeline.py 保留“单文件用例编排”；新增 task_service.py（或等价 use-case 服务）统一 claim/transition/finalize，避免 GUI/批次各自改状态。
  - ports.py 扩展：TaskRepository（claim/transition/find_by_key/list/append_event）、Clock、IdGenerator、CancellationToken、MetricsSink、AuditSink、ContentInspector。
  - batch.py：取消/超时/重试策略下沉到用例服务；批次只做编排与聚合。
- security/
  - admission.py：内容准入（魔数、扩展名一致性、zip 安全、页数/像素/解压比、哈希、只读句柄）。
  - tempfiles.py：临时目录生命周期、TTL 清理、原子落盘、孤儿对账。
  - auditor.py 保持独立；新增 audit_event 结构化事件（哈希链可选）。
- infrastructure/
  - persistence：SQLite schema v2（唯一幂等键、task_events、attempt/timeout/cancel 字段）；单写队列或线程本地连接；WAL + busy_timeout。
  - observability：metrics（计数器/时延）、diagnostics bundle（脱敏）、health 自检。
  - config：schema_version、last-known-good、启动校验。
- presentation/
  - 只通过 application 用例服务；所有耗时操作 QThreadPool + CancelToken；状态展示以领域状态为准。
- evaluation/
  - 增加注入/恶意文档/取消/超时/恢复场景的回归集。

### 3.2 数据流（目标）

选择文件 → admission（魔数/大小/结构/哈希）→ 创建/认领任务（幂等）→ 扫描（识别+融合+三档）→ 人工复核门禁 → 处理（逐项脱敏→失败删除→验证）→ 独立最终审计 → 原子提交（临时文件 + fsync + os.replace + DB 事务）→ success/quarantine → 结构化审计事件 + 指标。

### 3.3 明确不引入

- Kubernetes、消息队列、Redis、微服务、Docker（ADR-007）。
- 多 Agent 编排、向量库、MCP（ADR-007）。
- 云端模型默认路径（仅本地 Ollama；如未来引入云端，必须单独评审数据出境/留存/授权）。

## 4. 能力域设计（E-01 ~ E-17）

> 每项固定回答：现有实现与证据 / 风险与业务影响 / 目标行为与边界 / 最小可行实现（MVP） / 测试与验收 / 迁移依赖兼容 / 上线回滚恢复。

### E-01 任务状态机

**现有实现与证据**
- domain/enums.py 定义 TaskStatus 8 态（PENDING/PROCESSING/NEEDS_REVIEW/PROCESSED_UNAUDITED/AUDIT_FAILED/UNSAFE/PASSED/INTERRUPTED）与 ItemStatus 9 态。
- application/pipeline.py 通过直接赋值切换状态；application/batch.py 在异常分支直接写 TaskStatus.AUDIT_FAILED。
- sqlite_repo._coerce_status 对未知状态回退 PENDING。

**风险与真实业务影响**
- 非法转移（如 PASSED → PROCESSING、UNSAFE → PASSED）无守卫；调用方可直接改状态，审计轨迹不可信。
- 未知/未来状态被当成 PENDING，可能把“未知是否安全”的任务重新处理或误导出。
- 缺少 CANCELLED/TIMED_OUT/RETRYING，取消/超时只能借用 UNSAFE，报表不可区分。
- 业务影响：合规审计无法回答“任务为何终态”；崩溃恢复可能把不确定任务当待处理。

**目标行为与模块边界**
- domain/state_machine.py 定义 allowed_transitions 与 transition(task, to, reason)；终态不可再转移（除人工显式 reopen）。
- Task 只通过用例服务转移；GUI/批次不得直接赋值。
- 每次转移写 task_events（from/to/reason/actor/ts），与任务状态同事务。
- 新增 CANCELLED、TIMED_OUT、RETRY_SCHEDULED；未知状态加载为 INTERRUPTED（失败关闭），不得回退 PENDING。

**最小可行实现（MVP）与技术理由**
- 纯 Python 转移表 + 一个 transition 方法；SQLite 增加 task_events 表（v2）。
- 不引入状态机框架：转移数量有限，显式表更易审计与测试。
- 保留现有枚举 name 存储，新增值向后兼容。

**测试 / 验收 / 失败处理**
- 全转移矩阵测试：合法通过、非法拒绝、终态不可变、未知状态→INTERRUPTED。
- 取消/超时/重试路径的状态与事件断言；GUI 显示与 DB 一致。
- 失败处理：非法转移抛 SecurityViolation，任务保持原状态并记录拒绝事件。

**迁移 / 依赖 / 兼容**
- DB v2：新增 task_events、attempt、timeout_at、cancel_requested、config_hash、batch_id；旧行补默认值。
- 无新依赖；旧版读 v2 库应拒绝（版本高于应用）。
- 回滚需恢复迁移前备份（v1 无法理解 v2 字段）。

**上线 / 回滚 / 数据恢复**
- 上线前备份 tasks.db；先迁移再启动；灰度几天观察事件表。
- 回滚：停应用 → 恢复备份 → 回退版本；事件表丢失但原始任务数据完整。

### E-02 幂等性

**现有实现与证据**
- pipeline.POLICY_VERSION = recognition-v1；prepare 计算 (source_hash, mode, policy_version) 并调用 find_by_source。
- sqlite_repo 建 idx_tasks_idem 普通索引；batch.py 命中 PASSED 且产物存在则 skipped；recovery.existing_success 提供查询。
- 单文件 GUI 路径设置 task._reusable，但未见统一消费点（需补）。

**风险与真实业务影响**
- 普通索引不保证唯一；并发/重入可生成重复任务与重复导出。
- 只按“存在 PASSED 且路径存在”复用，未校验 final_hash，产物被替换/损坏仍可能复用。
- policy_version 变更会让旧产物不可复用（正确），但缺少 tool_version/config_hash，策略相同而配置不同会误复用。
- 业务影响：同一文件重复导出、映射不一致、审计记录与产物对不上。

**目标行为与模块边界**
- 幂等键扩展为 (source_hash, mode, policy_version, config_hash, tool_version)。
- TaskRepository.claim_or_get(key) 在 BEGIN IMMEDIATE 中创建或返回已有任务；唯一约束防并发重复。
- 复用条件：status=PASSED 且 output_path 存在且 sha256(output)==final_hash；否则标记 stale 并重跑。
- force 重跑必须记录操作者与原因，旧产物保留可追溯。

**最小可行实现（MVP）与技术理由**
- SQLite 新增 idempotency 表（key TEXT PRIMARY KEY, task_id, final_hash, output_path, updated_at）或 partial unique index；事务内 claim。
- 不引入分布式锁：单机 SQLite 的 BEGIN IMMEDIATE 已足够。
- 复用校验增加 hash 比对（已有 sha256_file）。

**测试 / 验收 / 失败处理**
- 两线程同时处理同一文件 → 只产生一个 PASSED 任务；第二个 reuse/等待。
- 产物被删/被改 → 不复用，标记 stale 并重跑。
- force=true 产生新尝试且旧记录保留；策略/配置变更不复用。
- 失败处理：claim 失败/锁超时 → 任务 RETRY_SCHEDULED 或明确错误，不导出。

**迁移 / 依赖 / 兼容**
- DB v2 新增 idempotency 表 + 回填（从 tasks 去重，保留最新 PASSED）；无新依赖。
- 旧版本忽略新表但会被版本检查拒绝。

**上线 / 回滚 / 数据恢复**
- 迁移前备份；回填脚本记录冲突数；回滚恢复备份。

### E-03 超时

**现有实现与证据**
- security/limits.py：task_timeout_seconds（默认 1800），ResourceLimits.check_file_size。
- pipeline._check_deadline 在 scan/process 入口与阶段间检查；settings 可配。
- 无硬超时；OCR/解析库调用没有统一超时；无 TIMED_OUT 状态。

**风险与真实业务影响**
- 大文件/异常文档可能长时间占用 worker，GUI 只能等待；临时文件累积。
- 仅有协作式检查：单个解析/OCR 调用卡住时永远到不了下一个检查点。
- 业务影响：用户以为卡死、重复点击、产生并发；无法给出明确失败原因。

**目标行为与模块边界**
- 每阶段预算（admission/scan/process/audit）与任务总预算；deadline 作为上下文传递。
- 看门狗线程：超时标记 TIMED_OUT、请求取消、清理受控临时文件、隔离产物。
- 对不可信解析/OCR 提供进程隔离选项（硬超时）；线程无法被安全强杀，进程隔离是唯一可靠硬超时。
- 所有超时失败关闭，不得把半成品放入 success。

**最小可行实现（MVP）与技术理由**
- 阶段 1：协作式检查 + 每调用可设超时（Ollama 已有 timeout；OCR/解析逐步加参数/子进程）+ watchdog 标记。
- 阶段 2：OCR/PDF/DOCX/XLSX 解析放入受控子进程（multiprocessing spawn），父进程 wait(timeout) 后 terminate；仅在测量到真实挂起后启用，避免过早复杂化。
- 理由：先用低成本方案覆盖绝大多数；硬超时需要进程边界，不能靠线程。

**测试 / 验收 / 失败处理**
- 注入慢处理器：超时后状态 TIMED_OUT、无 success 文件、临时目录清理、原文件哈希不变。
- 超时与取消竞争、超时后立即重试、审计仍可用。
- 失败处理：超时=失败关闭；记录阶段与耗时；不重试安全类超时（除非配置允许一次）。

**迁移 / 依赖 / 兼容**
- DB v2 增加 timeout_at/last_stage；无新依赖（multiprocessing 标准库）。
- 子进程方案需评估 PyInstaller 打包（spawn/freeze_support）。

**上线 / 回滚 / 数据恢复**
- 配置开关控制 watchdog/子进程；回滚关闭子进程路径，保留协作式检查。
- 超时任务进入 quarantine，人工可重跑。

### E-04 重试

**现有实现与证据**
- 无重试代码；batch.py 捕获异常后直接 failed；pipeline 无 attempt 概念。
- 现有错误码 domain/exceptions.py（LLM_TIMEOUT、PERSISTENCE_ERROR 等）可复用。

**风险与真实业务影响**
- 瞬时 SQLite busy、Ollama 超时、OCR 偶发失败会让文件直接失败，用户只能手动重跑。
- 无 attempt 记录，无法区分“首次失败”和“重试耗尽”。
- 业务影响：批量成功率下降、运维成本上升。

**目标行为与模块边界**
- 仅对可重试错误（DB busy、模型超时、OCR 瞬时、临时 IO）重试；安全违规/残留/不支持格式/配置错误绝不重试。
- 指数退避 + 抖动；最大 2 次；attempt 与 last_error_code 持久化；每次重试写 task_events。
- 重试幂等：复用同一 task_id 与工作目录，或清理后重建，绝不重复导出。

**最小可行实现（MVP）与技术理由**
- application/retry.py：is_retryable(error_code) + backoff；pipeline/batch 在阶段边界包装。
- 不引入 tenacity 等依赖：重试策略很小，标准库足够。
- 复核门禁/审计失败不重试（避免掩盖安全问题）。

**测试 / 验收 / 失败处理**
- 注入两次 transient 后成功；三次耗尽→FAILED 且 attempt=3；安全错误零重试。
- 重试期间取消；重试后输出 hash 与审计绑定；DB 不重复。
- 失败处理：重试耗尽 → INTERRUPTED/FAILED + 明确错误码；不进入 success。

**迁移 / 依赖 / 兼容**
- DB v2 attempt/last_error_code；无新依赖。
- 旧任务 attempt 默认 0。

**上线 / 回滚 / 数据恢复**
- 配置 max_retries（默认 2，可设 0）；回滚设 0。
- 重试记录保留，便于事后分析。

### E-05 任务取消

**现有实现与证据**
- batch.CancelToken 与 run() 文件间检查；GUI btn_cancel 设置 token。
- pipeline 没有 cancel token；扫描/处理中的单文件不可中断。

**风险与真实业务影响**
- 大文件/批量中用户无法及时停止；关闭窗口可能留下工作线程/临时文件。
- 取消与完成竞争时，可能已写出 success 文件但 UI 认为取消。
- 业务影响：用户体验差、磁盘残留、误以为失败/成功。

**目标行为与模块边界**
- CancelToken 传入 pipeline；在阶段边界与长循环（页/单元格/图片）检查。
- 取消后状态 CANCELLED；清理该任务 processing 目录；不复制到 success；已存在 quarantine 保留。
- GUI 显示“已取消”与已完成数量；关闭窗口时请求取消并等待有限时间。

**最小可行实现（MVP）与技术理由**
- 复用现有 CancelToken；在格式处理器循环中加 check_cancel 回调；异常 TaskCancelled 由 pipeline/batch 捕获。
- 不做线程强杀；协作式取消 + 有限等待。

**测试 / 验收 / 失败处理**
- 扫描/处理/审计各阶段取消；success 目录无新增；原文件不变；临时清理；状态 CANCELLED。
- 取消与完成同时发生：以 DB 事务为准，只有一个终态。
- 失败处理：取消不是失败，也不得被当成 PASSED。

**迁移 / 依赖 / 兼容**
- DB v2 cancel_requested/cancelled_at；无新依赖。
- GUI 现有按钮复用。

**上线 / 回滚 / 数据恢复**
- 回滚保留按钮但退化为文件间取消；已取消任务可重跑。

### E-06 数据库事务

**现有实现与证据**
- sqlite_repo 单连接 sqlite3.connect(check_same_thread=False)；无 WAL、busy_timeout、显式写锁。
- save_task 执行 tasks upsert + task_items 删除/插入 + audit_findings 删除/插入后一次 commit。
- append_operation 单独 commit，与 save_task 不是同一事务。

**风险与真实业务影响**
- 多线程共享连接：语句可能交错；一条连接上的未提交事务被另一线程 commit。
- 中途异常无显式 rollback，部分语句可能留在事务中，后续提交造成半更新。
- 操作日志与任务状态可能不一致，审计链断裂；并发下 database is locked。
- 业务影响：任务状态与条目/审计结论不一致，恢复判断错误。

**目标行为与模块边界**
- 单写者模型：所有写操作经一个写队列/锁；读可用线程本地连接。
- 每个状态变更一个显式事务：tasks + task_items + audit_findings + task_events + operation_log 原子提交。
- PRAGMA journal_mode=WAL、busy_timeout=5000、synchronous=NORMAL、foreign_keys=ON。
- 乐观并发：updated_at/row_version 条件更新；冲突则重读。

**最小可行实现（MVP）与技术理由**
- 保留 sqlite3；增加 threading.Lock 或单写队列；用 with self._conn: 包裹；操作日志并入同一事务。
- 不引入 ORM：表少、SQL 明确、迁移可控。
- WAL/busy_timeout 是 SQLite 原生能力，零依赖。

**测试 / 验收 / 失败处理**
- 并发写 100 次无 lock 错误；注入中间异常→无半更新；task/event 原子；integrity_check=ok。
- 读在写事务中看到旧版本（快照隔离）；写冲突重试一次。
- 失败处理：事务失败→rollback + PERSISTENCE_ERROR；任务保持原状态。

**迁移 / 依赖 / 兼容**
- DB v2；WAL 是数据库级设置；备份 .db（含 -wal/-shm 的处理）。
- 无新依赖。

**上线 / 回滚 / 数据恢复**
- 迁移前 checkpoint + 备份；回滚恢复备份并删除 -wal/-shm。
- 提供 integrity_check 与恢复手册。

### E-07 并发限制

**现有实现与证据**
- settings.security.max_workers=2；GUI setMaxThreadCount(max(1, max_workers))。
- BatchService 顺序 for 循环；pipeline 无全局内存/像素预算；仓储共享连接。

**风险与真实业务影响**
- 2 个 OCR/大图任务可能同时吃满内存；SQLite 写竞争；同一文件并发处理。
- 无队列深度/背压，用户可连续点击。
- 业务影响：OOM、卡顿、重复任务、DB 锁。

**目标行为与模块边界**
- 全局并发预算：worker 数、OCR/解析信号量、内存/像素估算、队列深度。
- 同一幂等键串行（per-key lock）；DB 单写。
- 超限时快速失败/排队并提示，不无限堆积。

**最小可行实现（MVP）与技术理由**
- threading.Semaphore 控制重任务；per-key lock dict；queue.Queue(maxsize) 做背压；复用 max_workers。
- 桌面规模无需分布式限流。

**测试 / 验收 / 失败处理**
- 并发 2 批、同文件并发、队列满、内存上限模拟；无死锁/重复导出。
- 失败处理：队列满→RESOURCE_LIMIT，明确提示；不丢已接受任务。

**迁移 / 依赖 / 兼容**
- settings 增加 queue_depth/heavy_task_limit；无 DB 迁移（可放 config）。
- 无新依赖。

**上线 / 回滚 / 数据恢复**
- 配置可回退到 max_workers=1；重启清空内存队列，任务状态仍可恢复。

### E-08 文件安全（准入与访问控制）

**现有实现与证据**
- pipeline.prepare：扩展名白名单 + ResourceLimits.check_file_size。
- security/path_safety：safe_join/resolve_within，输出路径 containment（pipeline 多处使用）。
- 处理器自行校验：docx/xlsx zipfile.is_zipfile；pdf pymupdf.open；image PIL.Image.open。
- threat_model T1/T2/T3 已覆盖路径越权、外发、日志泄漏。

**风险与真实业务影响**
- 扩展名可伪造：.png 实为 PDF/压缩包；处理器报错但不统一。
- 无 zip 解压比/条目数限制 → zip bomb；无页数/像素上限 → 内存/CPU 耗尽。
- 输入路径未限制受控根（桌面场景用户自选，合理）；符号链接/重解析点可绕过预期目录。
- 哈希与读取之间文件可被替换（TOCTOU）。
- 业务影响：解析器崩溃、OOM、处理到非预期内容，甚至读取用户未预期文件。

**目标行为与模块边界**
- security/admission.py：文件大小 → 魔数/真实类型 → 扩展名一致性 → 容器安全（zip 条目/解压比/总大小）→ 页数/像素/尺寸 → SHA-256 → 只读打开/句柄 → 处理后再哈希。
- 不信任扩展名：路由以真实类型为准，扩展名仅作提示；不一致进入 quarantine/明确错误。
- 所有写入经 path_safety；临时目录拒绝重解析点。
- 输入为本地用户显式选择时可允许任意可读路径，但必须记录 realpath；服务化模式必须限制在受控上传根。

**最小可行实现（MVP）与技术理由**
- 内置签名嗅探（%PDF、PK、PNG/JPEG/TIFF/BMP 魔数），不引入 python-magic（避免 DLL/部署复杂度）。
- zipfile 预检：条目数、总解压大小、压缩比、路径穿越条目；pymupdf 页数；PIL 像素数。
- 处理前后哈希比对已存在，补充“打开后立即哈希 + 关闭再哈希”。
- 理由：零新依赖、可测试、与 PyInstaller 兼容。

**测试 / 验收 / 失败处理**
- 伪造扩展名、zip bomb、超大页数/像素、重解析点、文件名 ../、处理中替换文件。
- 失败处理：admission 失败→明确错误 + quarantine（若已产生文件）；绝不进入解析链路。

**迁移 / 依赖 / 兼容**
- 无 DB 迁移；settings 增加 limit 字段；无新依赖。
- 严格模式可能导致历史“能用”的文件被拒，需要发布说明。

**上线 / 回滚 / 数据恢复**
- 配置 strict/warn 两档（默认 strict）；回滚到 warn 仅限内部调试，不得作为默认。
- 被拒文件保留在 input/或 quarantine，不删除用户原件。

### E-09 身份认证

**现有实现与证据**
- 无应用级登录/认证模块；信任边界是 Windows 交互式用户会话（docs/security/threat_model.md 第 1 节）。
- 数据目录与文件访问依赖操作系统 ACL；代码中无 auth/login/session 相关实现。

**风险与真实业务影响**
- 共享工作站或同一 Windows 账户被多人使用时，无法区分操作者；审计 actor 只能是 OS 用户。
- 若未来引入本地 HTTP API/服务化，没有认证边界，任何本机进程都可调用。
- 业务影响：无法满足“谁在何时导出了哪个文件”的强审计；共享电脑上隔离不足。

**目标行为与模块边界**
- 正式定义产品为“单机单用户本地模式”：不实现假登录，认证由 Windows 会话承担。
- application/ports.py 增加 Principal（用户 SID/名称/来源）与 Authenticator 端口；当前实现为 LocalWindowsPrincipal。
- 每个任务/事件记录 actor；导出操作记录 actor + 来源。
- 可选“应用锁”（DPAPI/Windows Hello）仅当出现共享工作站需求时启用，不作为默认。
- 若将来服务化：必须接入 OIDC/Windows Integrated Auth，禁止匿名。

**最小可行实现（MVP）与技术理由**
- 安装/启动时用 icacls 将 data/ 收紧为当前用户（及 Administrators 只读或按策略）；记录 OS 用户到任务/事件。
- 不引入登录框：单机单用户下登录是安全剧场，且增加密码管理风险。
- Principal 端口先定义，避免未来把认证逻辑散落在 GUI。

**测试 / 验收 / 失败处理**
- 另一本地标准用户无法读取 data/（ACL 测试）；任务事件含 actor。
- 若启用应用锁：错误密码/锁定/恢复测试。
- 失败处理：无法确定 Principal → 拒绝导出（失败关闭）。

**迁移 / 依赖 / 兼容**
- DB v2 tasks/events 增加 actor/owner_id（可空）；无新依赖。
- 服务化认证不在本轮范围。

**上线 / 回滚 / 数据恢复**
- 安装器设置 ACL；回滚恢复原 ACL 策略。
- 审计记录保留 OS 用户，便于追溯。

### E-10 资源授权

**现有实现与证据**
- 无应用级 owner/tenant/role；文件访问由用户选择 + OS ACL 决定；无下载 API。
- path_safety 只保证写入不越出受控目录，不等于“用户有权访问该任务”。

**风险与真实业务影响**
- 当前单机单用户下 IDOR 不适用；但一旦增加本地 API/多用户/共享数据目录，按 task_id 猜测即可越权读取。
- quarantine 可能含原文，若目录权限过宽会泄漏。
- 业务影响：未来服务化时的高危越权风险；现在若不定义边界，后续会补丁式加权限。

**目标行为与模块边界**
- application 层 Authorizer 端口：can_read(principal, task)、can_export(principal, task)、can_view_quarantine(principal, task)。
- 本地模式：任务 owner = 当前 OS 用户，默认允许本人；Administrators 按组织策略。
- 服务化模式：owner_id/tenant_id + RBAC（viewer/operator/admin），默认拒绝；每个下载/预览/导出入口强制授权。
- quarantine 默认仅本人 + 管理员可访问。

**最小可行实现（MVP）与技术理由**
- Task 增加 owner_id（OS SID）；在 use-case 入口与所有未来 API 路由统一调用 Authorizer。
- 不实现角色系统（单机无多角色收益）；预留策略接口。
- 理由：先把授权收口到一个端口，避免未来散落。

**测试 / 验收 / 失败处理**
- 跨用户读取/导出拒绝；猜测 task_id 拒绝；quarantine 越权拒绝。
- 失败处理：授权失败→403/拒绝 + 审计事件；不得泄露任务是否存在（可选）。

**迁移 / 依赖 / 兼容**
- DB v2 owner_id（可空，旧任务归当前用户）；无新依赖。
- 服务化 API 不在本轮范围。

**上线 / 回滚 / 数据恢复**
- 回滚忽略 owner_id；不删除已有任务。
- 若未来 API 上线，必须先完成授权测试再开放。

### E-11 临时文件生命周期

**现有实现与证据**
- 数据目录：processing/success/quarantine；GUI 提供“清理临时文件”手动入口（main_window.py 第 221-222/900-910 行）。
- settings.security.tempfile_retention_days=1 已定义但未被任何代码使用（检索无引用）。
- RecoveryService 只重分类 PROCESSING，不扫描/清理 processing 残留（ADR-005 明确为预留）。

**风险与真实业务影响**
- 中间文件可能含原文，长期堆积；磁盘耗尽导致后续任务失败。
- 配置项存在但不生效，运维误以为已自动清理。
- quarantine 若自动清理，可能破坏取证；若不清理，可能长期保留原文。
- 业务影响：隐私暴露窗口扩大、磁盘故障、合规留存不可控。

**目标行为与模块边界**
- security/tempfiles.py：受控根、per-task 工作目录、manifest（task_id/创建时间/状态/哈希，不含原文）、TTL 清理、孤儿对账。
- 启动时清理：只清理“非活动任务 + 超过 TTL + 无 success/quarantine 引用”的 processing 文件。
- quarantine 默认不自动删除；保留期由策略配置，删除需审计与人工确认（或明确合规策略）。
- 清理严格限制在受控根内；拒绝重解析点；记录清理事件。

**最小可行实现（MVP）与技术理由**
- 启动 TTL sweep + 批次结束 sweep；per-task 目录 data/processing/<task_id>/；manifest JSON。
- 不引入定时服务：桌面应用启动/批次结束足够。
- 理由：低成本、可审计、避免后台常驻。

**测试 / 验收 / 失败处理**
- TTL 到期清理、活动任务不清理、路径穿越拒绝、清理失败记录、崩溃残留对账。
- 失败处理：清理失败→告警 + 保留，不删除不确定文件；绝不影响原件。

**迁移 / 依赖 / 兼容**
- 无 DB 迁移（manifest 为文件）；settings 已有 retention 字段。
- 无新依赖。

**上线 / 回滚 / 数据恢复**
- 先 dry-run 统计再启用；回滚关闭 sweep。
- 清理事件可追溯；误删只能靠备份（默认不备份临时文件，需说明）。

### E-12 审计日志

**现有实现与证据**
- 结构化 JSON 日志 + RedactionFilter（msg/args/extra 全掩码）；register_sensitive_values 登记候选原文；SQLite operation_log；审计记录 JSON 只含掩码值/哈希；sanitize_detail 落库前强制掩码。
- tests/test_security_hardening.py 覆盖日志脱敏；threat_model T3。
- 无哈希链/防篡改；operation_log 与 save_task 分事务；无保留执行；异常堆栈未统一记录（JSON formatter 不含 exc_info）。

**风险与真实业务影响**
- 日志/审计记录可被本地用户修改，无法证明未被篡改。
- operation_log 与任务状态可能不一致；日志轮转/保留策略未执行。
- 日志注入（文件名含换行/控制字符）可伪造日志行。
- 业务影响：合规审计证据不足；事故复盘困难；敏感值可能经异常消息泄漏。

**目标行为与模块边界**
- 追加式审计事件：event_id、task_id、batch_id、actor、event_code、from/to、artifact_hash、ts、prev_hash。
- 任务内哈希链（可选 HMAC/签名）；每次状态变更与事件同事务。
- 日志/JSON 只允许白名单字段；控制字符/换行转义；异常只记录类型/错误码/脱敏消息，不记录原文。
- 保留策略按配置执行；支持导出 JSONL 供审计。

**最小可行实现（MVP）与技术理由**
- task_events + operation_log 合并/同事务；prev_hash 链；log injection 过滤；保留 sweep。
- 不做外部 SIEM/WORM（桌面场景过度）；如企业要求可后续导出到受控存储。
- 理由：防篡改的“检测能力”比“绝对不可改”更现实；哈希链可发现篡改。

**测试 / 验收 / 失败处理**
- 篡改事件后链校验失败；日志/DB/JSON 无完整敏感值；换行/控制字符注入被转义；保留策略生效。
- 失败处理：审计写失败→任务不得进入 PASSED（失败关闭）；链断裂→标记审计异常并隔离。

**迁移 / 依赖 / 兼容**
- DB v2 task_events（含 prev_hash）；无新依赖。
- 旧 operation_log 可迁移/保留，不回填哈希链（标记 legacy）。

**上线 / 回滚 / 数据恢复**
- 迁移前备份；回滚保留旧 operation_log；新事件表可丢弃（不影响产物）。

### E-13 配置管理

**现有实现与证据**
- infrastructure/config/settings.py：dataclass Settings + YAML 加载 + LOCALDOCMASK_HOME + 范围/回环校验；config/config.example.yaml；ADR-006。
- 无配置 schema_version、无 last-known-good、无运行期热加载、无 config_hash 进入任务。

**风险与真实业务影响**
- 配置变更后无法复现历史任务（不知道当时阈值/模型/掩码）。
- 非法配置启动失败（好），但无回滚到上次可用配置。
- 多来源（默认/文件/环境）优先级需文档化；否则运维误配。

**目标行为与模块边界**
- 配置 schema_version；优先级：默认 < config.yaml < 环境变量 < CLI（如有）；启动校验失败即拒绝启动并给出字段级错误。
- 每次任务记录 config_hash（不含密钥）；配置快照只读保存（脱敏）。
- last-known-good：校验通过后原子替换；启动失败可回退。
- 不把密钥写入配置；如需密钥用 DPAPI/凭据管理器（当前无）。

**最小可行实现（MVP）与技术理由**
- 增加 schema_version + config_hash；启动时写 config_snapshot.json（脱敏）；保留上一份 last_known_good。
- 不做热加载：桌面应用重启应用配置，简单可靠。
- 理由：可复现性优先于动态性。

**测试 / 验收 / 失败处理**
- 非法 YAML/越界/非回环 Ollama → 启动失败且不覆盖 last-known-good。
- config_hash 变化 → 幂等键变化，不复用旧产物。
- 失败处理：配置错误→ConfigError，明确字段；不进入处理。

**迁移 / 依赖 / 兼容**
- 配置文件新增字段可缺省；无 DB 迁移（config_hash 在 v2 tasks）。
- 无新依赖（已有 pyyaml）。

**上线 / 回滚 / 数据恢复**
- 发布新配置示例；回滚恢复上一份配置快照。
- 数据目录不变。

### E-14 可观测性

**现有实现与证据**
- 结构化日志（轮转）、operation_log、任务记录、评测指标（P/R/F1）、diagnose_env、headless_check。
- 无 metrics/tracing/health 端点；无阶段耗时/失败码统计的持久化指标。

**风险与真实业务影响**
- 无法量化成功率、P95 耗时、失败原因分布；支持只能靠日志人工排查。
- 大批量时无法判断瓶颈（OCR/解析/审计/DB）。
- 业务影响：运维效率低、回归不可量化、容量规划无依据。

**目标行为与模块边界**
- MetricsSink 端口：计数器（任务/条目/失败码）、时延（阶段/总）、并发/队列深度；本地 JSONL 或 SQLite 聚合表。
- 诊断包（diagnostics bundle）：版本、配置摘要、最近事件、脱敏日志；不自动上传。
- health/self-check：数据目录可写、DB 可打开、模型/OCR 可用性（degraded 明确标注）。

**最小可行实现（MVP）与技术理由**
- 轻量 MetricsSink 写 JSONL；pipeline 阶段计时；diagnose_env 扩展为 health 输出。
- 不引入 Prometheus/OpenTelemetry（桌面无采集端；ADR-007 精神）。
- 理由：本地可读、零运维、可导出。

**测试 / 验收 / 失败处理**
- 指标记录完整且无敏感值；诊断包脱敏；health degraded 不误报 healthy。
- 失败处理：指标写失败不影响主流程（best-effort），但审计事件失败仍失败关闭。

**迁移 / 依赖 / 兼容**
- 可无 DB 迁移（JSONL）或 v2 metrics 表；无新依赖。
- 回滚关闭 metrics。

**上线 / 回滚 / 数据恢复**
- 配置开关；回滚停止写指标。
- 指标文件按保留策略清理。

### E-15 异常恢复

**现有实现与证据**
- RecoveryService.recover：启动时把 PROCESSING 任务按 output_path 是否存在重分类为 PENDING/INTERRUPTED；composition.py 接线。
- finalize 用 shutil.copy2 直接写 success 目录；无临时文件 + os.replace 的原子提交。
- 无孤儿文件对账；无重试；无恢复报告 UI。

**风险与真实业务影响**
- 崩溃发生在 copy2 中途：success 目录可能出现半文件，DB 未标记 PASSED；重启后可能被误认为有效产物。
- 崩溃发生在 DB commit 前：文件已写成功但任务状态未提交，审计记录与产物不一致。
- 业务影响：误导出损坏/未审计文件，或用户找不到已完成结果。

**目标行为与模块边界**
- 原子提交：写 processing 临时文件 → fsync → os.replace 到 success → DB 事务标记 PASSED；或先 DB 记录 staging 状态再原子替换。
- 启动对账：DB vs processing/success/quarantine；孤儿文件标记 orphaned，不自动 promote。
- 恢复报告：哪些任务重排、哪些中断、哪些孤儿，GUI/CLI 可见。
- 断点续跑：仅在阶段幂等且产物可验证时允许；否则重跑。

**最小可行实现（MVP）与技术理由**
- 新增 security/atomic_io.py（write-temp + fsync + os.replace）；finalize 改用它；RecoveryService 增加目录对账。
- 不引入事务文件系统/数据库 BLOB：文件系统 + DB 状态足够。
- 理由：崩溃一致性靠“原子替换 + 对账”，不靠祈祷。

**测试 / 验收 / 失败处理**
- 模拟崩溃点：copy 前/中/后、DB commit 前/后；重启后无未审计文件进入 success；原文件不变；孤儿可见。
- 失败处理：对账不确定→隔离 + 人工；绝不自动认定安全。

**迁移 / 依赖 / 兼容**
- DB v2 task_events/staging 字段；无新依赖。
- 旧 success 文件迁移时做一次 hash/审计对账（只读）。

**上线 / 回滚 / 数据恢复**
- 上线前备份；首次启动执行只读对账报告，不自动删除。
- 回滚保留旧产物，按报告人工处理。

### E-16 部署

**现有实现与证据**
- PyInstaller onedir（LocalDocMask.spec），dist/LocalDocMask/LocalDocMask.exe 已构建；scripts/build_package.ps1；headless_check 离屏冒烟。
- pyproject + uv.lock 固定依赖；无代码签名/安装器/CI/SBOM/自动更新。

**风险与真实业务影响**
- 分发包可能被篡改；DLL/依赖漂移；升级失败无回滚；用户数据目录处理不当可能丢失。
- 业务影响：企业分发合规性不足；支持成本高。

**目标行为与模块边界**
- 可重复构建（锁定依赖 + 哈希清单）；版本清单/SBOM；可选代码签名；安装器（per-user）；升级保留 data/；回滚到上一版本。
- 不自动更新（除非签名 + 明确同意）；不引入 Docker/K8s。
- 发布物含 config.example、resources、README、校验和。

**最小可行实现（MVP）与技术理由**
- 构建脚本产出 zip + SHA-256 + version.json + SBOM（pip freeze/uv export）；headless smoke 作为门禁。
- 代码签名如无证书则记录为未签名（不伪造）。
- 理由：先把可验证分发做扎实，签名/安装器按企业需求增量。

**测试 / 验收 / 失败处理**
- 干净 VM 安装/启动/升级/回滚；data/ 保留；DB 迁移成功；哈希校验失败拒绝安装。
- 失败处理：校验失败/签名无效→拒绝运行并提示。

**迁移 / 依赖 / 兼容**
- 无 DB 迁移（打包层）；依赖锁定已有。
- 签名/安装器可能引入工具链依赖（需批准）。

**上线 / 回滚 / 数据恢复**
- 发布前备份用户 data/；回滚安装上一版本；DB 回滚用迁移备份。

### E-17 数据迁移

**现有实现与证据**
- SCHEMA_VERSION=1；PRAGMA user_version + 显式 _MIGRATIONS；initialize_database 幂等；库版本高于应用则拒绝（sqlite_repo.py）。
- 无迁移前自动备份、无 dry-run、无恢复 CLI、无版本矩阵文档。

**风险与真实业务影响**
- 迁移失败可能留下半迁移 DB；用户无备份无法回滚。
- 降级被拒绝（正确），但缺少“如何恢复”的操作路径。
- 业务影响：升级事故导致任务历史丢失/应用不可用。

**目标行为与模块边界**
- 迁移前自动备份 tasks.db（含校验和）；迁移在事务中执行；失败 rollback + 保留备份。
- 提供 migrate --dry-run、backup、restore、verify 命令；版本兼容矩阵；迁移后 integrity_check。
- 数据导出/导入（JSONL）用于跨版本/跨机迁移（不含原文，仅掩码/元数据）。

**最小可行实现（MVP）与技术理由**
- sqlite_repo 增加 backup_db()/restore_db()/verify_db()；启动迁移前调用 backup；CLI 脚本。
- 不引入 Alembic：表少、迁移显式、SQLite 单文件；Alembic 增加依赖与抽象成本。
- 理由：标准库即可完成备份/恢复/校验。

**测试 / 验收 / 失败处理**
- v1→v2 成功、注入迁移异常→回滚且备份可恢复、库版本过高→拒绝、损坏 DB→明确错误。
- 失败处理：迁移失败不启动应用；提示备份路径与恢复命令。

**迁移 / 依赖 / 兼容**
- v2 schema；无新依赖；旧版本无法读 v2（版本检查）。
- 回滚必须用备份，不能降级就地改库。

**上线 / 回滚 / 数据恢复**
- 发布前 dry-run + 备份；灰度；回滚恢复备份 + 旧版本。
- 保留最近 N 份备份（按配置/磁盘）。

## 5. 安全专项

### S-01 不可信扩展名 / 内容伪造

- **现有**：pipeline.prepare 只按扩展名白名单路由；docx/xlsx 用 zipfile.is_zipfile，pdf 用 pymupdf.open，image 用 PIL.Image.open 做局部校验；无统一魔数嗅探与扩展名一致性策略。
- **风险**：.png 实为 PDF/压缩包、.docx 实为 zip bomb、截断文件导致解析器异常；用户以为处理了 A，实际读取 B。
- **目标**：security/admission.py 统一“真实类型 + 扩展名一致性 + 容器安全 + 页数/像素 + 哈希”准入；不信任扩展名。
- **MVP**：内置魔数签名（%PDF、PK、PNG/JPEG/TIFF/BMP）、zip 条目/解压比/总大小预检、pymupdf 页数、PIL 像素上限；零新依赖。
- **测试**：伪造扩展名、zip bomb、超大页/像素、截断文件、MIME 不一致；预期明确错误 + 隔离，不进入解析。
- **失败处理**：admission 失败 = 失败关闭；不得尝试“尽力解析”。
- **迁移/依赖**：仅配置项；不引入 python-magic。
- **上线/回滚**：默认 strict；回滚仅限内部 warn 模式，不得作为生产默认。

### S-02 路径穿越 / 重解析点

- **现有**：security/path_safety.py 的 safe_join/resolve_within，拒绝绝对路径与 .. 段并做 realpath containment；pipeline 输出/隔离路径已使用；有测试（test_security_hardening）。
- **风险**：Windows junction/符号链接、8.3 短名、UNC、大小写差异可能绕过朴素检查；临时目录若被替换为 junction 可写到外部。
- **目标**：所有写入必须 path_safety；创建临时目录时拒绝 reparse point；对输入 realpath 做记录与一致性校验。
- **MVP**：增加 Windows reparse point 检测（os.lstat + FILE_ATTRIBUTE_REPARSE_POINT）；补充 UNC/8.3/大小写测试；零新依赖。
- **测试**：../、绝对路径、junction、符号链接、UNC、大小写变体；预期 SecurityViolation。
- **失败处理**：任何 containment 失败 → 拒绝 + 审计事件。
- **迁移/依赖**：无。
- **上线/回滚**：直接生效；回滚保留 safe_join，不降级为字符串前缀判断。

### S-03 越权下载 / IDOR

- **现有**：无 HTTP 下载接口；桌面 GUI 本地访问；无 owner/tenant 概念。
- **风险**：未来若加本地 API/多用户/共享目录，按 task_id 猜测可越权；quarantine 含原文，权限过宽会泄漏。
- **目标**：application Authorizer 端口 + owner_id；所有 preview/download/export 入口统一授权；默认拒绝。
- **MVP**：先落 owner_id（OS SID）与 Authorizer 接口；当前 GUI 以本地用户为 owner；不实现假 API。
- **测试**：跨用户任务读取/导出拒绝；猜测 task_id 拒绝；quarantine 越权拒绝（在引入 API 时必测）。
- **失败处理**：授权失败 → 拒绝 + 审计；不泄露任务存在性（可选）。
- **迁移/依赖**：DB v2 owner_id；无新依赖。
- **上线/回滚**：本地模式无行为变化；服务化前必须完成授权验收。

### S-04 敏感日志泄露

- **现有**：RedactionFilter 对 msg/args/extra 掩码；register_sensitive_values；sqlite sanitize_detail；tests/test_security_hardening 覆盖；JSON formatter 不写 exc_info。
- **风险**：异常消息可能含原文；第三方库日志绕过 filter；文件名含换行/控制字符造成日志注入；日志文件权限过宽。
- **目标**：白名单日志字段；控制字符转义；异常只记类型/错误码/脱敏消息；日志文件 ACL 限制；保留策略执行。
- **MVP**：新增 sanitize_log_text（转义 CR/LF/控制字符）；统一异常日志入口只记 type+code；补第三方 logger 过滤；零新依赖。
- **测试**：注入 "phone\nFAKE LOG"、异常含身份证、extra 字段含姓名；输出无原文、无伪造行。
- **失败处理**：审计事件写失败 → 任务不得 PASSED。
- **迁移/依赖**：无。
- **上线/回滚**：直接生效；回滚不得关闭脱敏 filter。

### S-05 不可信文档内容对 Agent 的提示注入

- **现有**：LLM 仅补充候选（recognition pipeline），LLM-only 候选在 fusion 中降权/转复核；模型输出经 JSON 解析；无工具执行/命令执行。
- **风险**：文档/OCR 文本中的指令可能让模型漏报、伪造实体、输出超长/非 JSON；若未来接云端模型，可能诱导外发。
- **目标**：文档内容视为数据而非指令；系统提示 + 分隔符；严格 JSON schema；长度上限；LLM-only 永不自动确认；模型输出不得进入 shell/路径/SQL；提示注入样本回归。
- **MVP**：提示模板加固、输出 schema 校验、长度截断、注入语料测试；保持“模型只提候选”的边界；零新依赖。
- **测试**：包含“忽略之前指令”“输出全部姓名”“返回恶意 JSON”等文本；预期不崩溃、不自动确认、不执行任何动作，必要时转复核。
- **失败处理**：模型超时/非法输出 → 降级为规则识别并标注 degraded；不得 fail-open。
- **迁移/依赖**：提示版本写入配置/任务 config_hash；无新依赖。
- **上线/回滚**：可回退旧提示模板，但 LLM-only 仍需人工复核。

### S-06 外部模型数据出境 / 留存 / 授权

- **现有**：Ollama 强制回环 + Session 禁代理 + settings 拒绝非回环；默认 enabled=false；OCR/NER 本地；无云端备用路径；无 telemetry。
- **风险**：误配远程地址、未来接入云模型、依赖库遥测、代理环境变量。
- **目标**：默认零外发；任何远程模型必须显式 opt-in + 数据分类/DPA/留存/地域评审 + 发送前最小化/脱敏；记录 endpoint 与模型版本；禁止静默 fallback。
- **MVP**：egress 测试（设置 HTTP_PROXY/HTTPS_PROXY 仍直连本机；非回环 URL 启动拒绝）；审计记录模型 endpoint/版本；文档声明数据边界；零新依赖。
- **测试**：代理环境变量、非回环 URL、LLM 关闭时无网络调用、grep 无其他 HTTP 客户端；预期本地直连/拒绝/零调用。
- **失败处理**：网络异常 → 禁用 LLM，规则路径继续，标记 degraded；绝不上传文档/OCR/候选。
- **迁移/依赖**：无。
- **上线/回滚**：默认关闭；启用需配置变更 + 审计。

## 6. 验收矩阵（能力域）

> 状态图例：✅ 已实现并验证；🔶 部分实现/未充分验证；⬜ 未实现（含已论证不适用）。

| ID | 能力 | 优先级 | 现状 | 可测验收标准 | 关键测试/证据 | 迁移/依赖 |
|---|---|---|---|---|---|---|
| E-01 | 任务状态机 | P0 | 🔶 枚举+直接赋值，未知→PENDING | 非法转移 100% 拒绝；未知状态→INTERRUPTED；终态不可变；事件可追溯 | 转移矩阵测试、崩溃/取消/超时事件 | DB v2 task_events |
| E-02 | 幂等性 | P0 | 🔶 普通索引+批次跳过 | 并发同文件只产生 1 个 PASSED；产物 hash 不符不复用；force 可追溯 | 并发测试、产物篡改测试 | DB v2 idempotency |
| E-03 | 超时 | P1 | 🔶 阶段边界协作式 | 慢处理器超时→TIMED_OUT；无 success；原文件不变；临时清理 | 注入慢 handler、watchdog 测试 | DB v2 timeout_at |
| E-04 | 重试 | P1 | ⬜ 无 | transient 最多 2 次；安全错误 0 次；attempt 持久化；不重复导出 | 注入 transient/安全错误 | DB v2 attempt |
| E-05 | 任务取消 | P1 | 🔶 文件间取消 | 扫描/处理/审计中取消→CANCELLED；无 success；清理；原文件不变 | 各阶段取消测试 | DB v2 cancel 字段 |
| E-06 | DB 事务 | P0 | 🔶 单连接/无锁/分事务 | 每状态变更原子；并发 100 次无半更新；integrity_check=ok | 并发写、异常 rollback 测试 | DB v2 + WAL |
| E-07 | 并发限制 | P1 | 🔶 2 worker/共享连接 | 同键串行；队列满明确失败；无死锁/重复导出 | 并发批次、同文件、队列满 | settings 字段 |
| E-08 | 文件安全 | P0 | 🔶 扩展名+局部校验 | 伪造扩展名/zip bomb/超大页拒绝；路径 containment；哈希不变 | 恶意样本、reparse、TOCTOU | 配置项，无新依赖 |
| E-09 | 身份认证 | P2* | ⬜ 本地单用户 | 明确单机模式；data/ ACL 仅当前用户；actor 可追溯；服务化才接 OIDC | ACL/actor 测试 | DB v2 actor/owner |
| E-10 | 资源授权 | P2* | ⬜ OS ACL | Authorizer 统一入口；本地 owner 生效；未来 API IDOR 测试 | 跨用户/猜测 ID 测试 | DB v2 owner_id |
| E-11 | 临时文件生命周期 | P1 | 🔶 手动清理/配置未用 | TTL 清理仅限受控根；活动任务不删；清理事件可追溯 | TTL/孤儿/穿越测试 | manifest，无 DB |
| E-12 | 审计日志 | P1 | 🔶 脱敏强/无防篡改 | 事件哈希链可检测篡改；日志无原文；保留策略生效 | 篡改/注入/保留测试 | DB v2 task_events |
| E-13 | 配置管理 | P1 | 🔶 YAML+校验 | 非法配置拒绝启动；config_hash 进入幂等；last-known-good | 非法配置/哈希/回滚测试 | config 字段；DB v2 config_hash |
| E-14 | 可观测性 | P1 | 🔶 日志/无指标 | 阶段耗时/失败码指标可用；诊断包脱敏；health 不误报 | 指标/诊断/健康测试 | JSONL 或 v2 表 |
| E-15 | 异常恢复 | P0 | 🔶 仅重分类 | 原子提交；孤儿对账；崩溃点测试无未审计文件进 success | 多崩溃点注入、重启对账 | DB v2 + atomic_io |
| E-16 | 部署 | P2 | 🔶 PyInstaller | 可重复构建+校验和+版本清单；升级保留 data/；回滚可用 | 干净 VM 安装/升级/回滚 | 打包工具链 |
| E-17 | 数据迁移 | P1 | 🔶 版本化迁移 | 迁移前备份；失败回滚；dry-run/restore/verify | v1→v2、失败注入、恢复 | DB v2 |

\* E-09/E-10 在“单机单用户”范围内按 P2 处理；若批准服务化/多用户，立即升为 P1/P0 并纳入首发。

## 7. 安全专项验收矩阵

| ID | 专项 | 优先级 | 现状 | 可测验收标准 | 关键测试 |
|---|---|---|---|---|---|
| S-01 | 不可信扩展名 | P0 | 🔶 | 伪造扩展名/zip bomb/超大页拒绝；真实类型路由 | 恶意样本集 |
| S-02 | 路径穿越/重解析点 | P0 | ✅/🔶 | ../、绝对、junction、UNC 全部拒绝；写入 containment | 路径攻击测试 |
| S-03 | 越权下载/IDOR | P2* | ⬜ | 未来 API 默认拒绝；owner 生效；quarantine 隔离 | 跨用户/猜测 ID |
| S-04 | 敏感日志泄露 | P0 | ✅/🔶 | 日志/DB/JSON 无原文；换行注入被转义 | 日志注入/异常测试 |
| S-05 | 提示注入 | P1 | 🔶 | 注入文本不自动确认、不执行动作、不崩溃 | 注入语料回归 |
| S-06 | 外部模型数据出境 | P0 | ✅ | 代理环境仍直连本机；非回环拒绝；默认零外发 | egress/配置测试 |

## 8. 非目标（明确不做）

- Kubernetes、消息队列、Redis、微服务、Docker（ADR-007；当前规模无收益）。
- 多 Agent 编排、向量库、MCP（ADR-007）。
- 单机单用户下引入账号/密码登录（安全剧场）；以 OS 会话 + ACL 为边界。
- 默认云端模型；任何云模型都必须单独评审数据出境/留存/授权。
- 自动删除 quarantine 原文（除非明确合规策略与用户确认）。

## 9. 需要批准的产品/合规决策

1. **部署形态**：仅单机单用户，还是需要局域网/多用户服务模式？这决定 E-09/E-10 的优先级与是否实现 OIDC/RBAC。
2. **合规留存**：日志、审计事件、quarantine 的保留天数与删除审批流程；是否需要防篡改证明（哈希链/HMAC/外部归档）。
3. **硬超时方案**：是否接受为 OCR/解析引入受控子进程（真正可终止），以及 PyInstaller 打包影响。
4. **依赖策略**：是否坚持零新依赖（推荐内置魔数嗅探/WAL/备份），还是允许 python-magic 等库。
5. **分发方式**：是否需要代码签名、MSI/安装器、企业软件分发（Intune/WinGet）与自动更新。
6. **迁移窗口**：是否允许启动时自动备份 tasks.db，以及备份保留数量/位置。

## 10. 交付门槛与下一步

本方案建议的退出条件（批准后按能力域拆分实施）：

- 所有 P0 能力与安全专项通过，且无 P0/P1 未解决缺陷。
- 158 项现有测试保持通过；新增状态机/幂等/事务/取消/超时/准入/恢复/审计/注入测试全部通过。
- 迁移可备份/恢复；升级/回滚演练通过。
- 独立复审确认无“失败关闭”退化、无原件修改、无外发、无敏感日志。

**当前状态：设计待批准。批准后才会拆分可独立开发的任务，并遵循 P0 → P1 → P2 的顺序。**
