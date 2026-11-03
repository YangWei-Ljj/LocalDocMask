# RepoMind 工程基准分析报告

> 分析对象：`<REPO_MIND_ROOT>`（只读参考项目，main 分支，HEAD=`e0595cf`）
> 分析方式：纯只读（源码阅读 + `git log/status/diff`），未修改、未写入、未删除其中任何文件
> 分析日期：2026-10-26

---

## 1. 项目概览

RepoMind 是一个**面向 GitHub PR / Issue 研发协作的全栈 AI Agent 应用**（自述：「AI-powered GitHub PR/Issue collaboration platform」，见 `git log` 提交信息与 `README.md:3`）。

产品实际做什么（据 `README.md` 第 5–17 行）：

- 连接并同步 GitHub 仓库的 Issue、Pull Request、PR 文件、Review 评论、Workflow Run/Job/日志；
- 对 Issue 做分类/优先级/复杂度/推荐负责人/重复候选分析；
- 对 PR 做摘要、风险分级（P1/P2/P3）、检查清单、测试建议；
- 对失败 CI 日志做排障归因；
- 通过一个对话式 ChatAgent（原生工具调用）串联工作区代码、长期记忆、RAG 知识库、Issue/PR/CI/周报/安全工具；
- 多 Agent 工作流（Planner → 专用 Agent → Observer → Synthesis）处理跨领域工程决策（PR 就绪度、阻塞项、优先级）；
- 内置完整 RAG 知识问答系统 + RAGAS 在线评测体系；
- **只生成草稿、不写回 GitHub**（MVP 边界，`docs/architecture/security.md`）。

规模度量（实测）：

| 指标 | 数值 | 证据 |
|---|---|---|
| Git 跟踪文件数 | 196 | `git ls-files` |
| 后端 Python 文件 / 总行数 | 123 个 / 约 27,295 行 | `backend/app` 递归统计 |
| 后端测试文件 / 行数 | 27 个 `test_*.py` / 约 5,110 行 | `backend/app/tests/` |
| API 路由模块 | 18 个 | `backend/app/main.py:63–80` |
| Pydantic Schema 模块 | 18 个 | `backend/app/schemas/` |
| Agent 模块 | 13 个 | `backend/app/services/agents/` |
| RAG 子模块 | 13 个 | `backend/app/services/rag/` |
| 数据库表 | 29 张 | `backend/app/db/models.py` |
| 前端页面路由 | 15 个 | `frontend/app/*/page.tsx` |
| Git 提交 | 仅 2 个 | `git log --oneline` |

---

## 2. 目录结构（实际观察）

```
RepoMind/
├── backend/                    # FastAPI 后端（Python 3.11）
│   ├── app/
│   │   ├── main.py             # 应用工厂 + 路由注册 + 启动期建表/迁移 + 后台同步任务
│   │   ├── api/routes/         # 18 个路由模块（见下）
│   │   ├── schemas/            # 18 个请求/响应 Schema
│   │   ├── core/               # config.py（pydantic-settings）、logging.py、security.py
│   │   ├── db/                 # models.py（29 表）、session.py
│   │   ├── services/
│   │   │   ├── agents/         # 13 个 Agent：chat/planner/observer/synthesis/issue/pr_review/
│   │   │   │                   #   ci_debug/report/safety/recommendation/github_retrieval/workflow_orchestrator
│   │   │   ├── rag/            # 完整 RAG 管线：embeddings/chunking/indexing/retrieval/rerank/
│   │   │   │                   #   vector_store(Milvus)/qa/answer_gate/source_policy/knowledge_base...
│   │   │   ├── llm/            # client.py（裸 httpx OpenAI 兼容）、prompts.py、structured_outputs.py
│   │   │   ├── github/         # client/issues/pull_requests/actions（REST 封装）
│   │   │   ├── evals/          # runner.py、ragas_runner.py、suites.py
│   │   │   ├── skills/registry.py   # SKILL.md 技能注册表
│   │   │   ├── chat_memory.py / memory_hub.py / context_compression.py
│   │   │   ├── worktree_manager.py / project_indexing.py / code_search.py / code_analysis.py
│   │   │   ├── scheduler.py / mcp_client.py / feedback_notifications.py / permissions.py
│   │   ├── skills/             # 6 个内置技能（SKILL.md）：pr-review / issue-triage /
│   │   │                       #   ci-debug / project-investigation / safety-draft / weekly-report
│   │   ├── mcp/memory_server.py    # stdio MCP 记忆服务（独立进程）
│   │   └── tests/              # 27 个 pytest 文件
│   ├── requirements.txt / requirements-local-embedding.txt
│   └── .data/                  # 运行时数据（gitignored）：克隆的仓库、git worktree、上传文件
├── frontend/                   # Next.js 15 + React 19 + TS + Tailwind + React Query
│   ├── app/                    # 15 个页面：chat / repos / pull-requests / issues / ci /
│   │                           #   knowledge / rag / code-graph / reports / evals / workflow-runs /
│   │                           #   action-drafts / workspaces 等
│   ├── components/  lib/（api.ts / rag.ts / types.ts）
│   └── package.json + package-lock.json
├── docs/                       # 架构与文档（见第 10 节）
├── scripts/                    # Windows 运维脚本群（PowerShell 为主）
├── docker-compose.yml          # postgres(pgvector) / etcd / milvus / redis(optional) / minio(legacy)
├── .env.example / .env.deepseek / .env.ollama / .env   # LLM 后端切换的三套配置
├── switch-llm.ps1               # 一键切换 DeepSeek 云端 / Ollama 本地
├── README.md / 运行手册.md      # 项目说明 + 本机部署运行手册
├── 演示_*.png（4 张截图）       # 前端首页 / 知识库 / RAG 工作室 / RAG 问答
└── 运行时目录：chroma_db/、chat_history/、sessions/、.data/（均为运行产物）
```

---

## 3. 核心业务流程

主链路（用户视角，据 `README.md` 与 `backend/app/api/routes/chat.py`）：

1. **接入仓库**：`POST /api/repos` 连接 GitHub 仓库 → 后端 `services/repos_sync.py` 通过 `services/github/client.py`（httpx + GitHub Token）拉取 Issue/PR/Review/CI 日志，落 PostgreSQL；同时把仓库 clone 到 `.data/repos/<uuid>-<owner>__<repo>/`，PR 用 `worktree_manager.py` 建 git worktree 隔离。
2. **自动同步**：启动时 `asyncio.create_task(periodic_repo_sync_loop())`（`services/scheduler.py`），每 300s 遍历已连接仓库增量同步（`main.py:155-156`）。
3. **知识入库**：上传文档 / 同步 Issue 描述 / PR 说明 / CI 日志 → `rag/document_extraction.py`（TXT/MD/PDF/DOCX/JSON/CSV）→ `chunking.py` 段落感知切片（SHA-256 去重）→ `embeddings.py` 向量化 → `vector_store.py` 写入 Milvus；元数据与文档清单落 PostgreSQL。
4. **对话问答**：`POST /api/chat/...`（SSE 流式）→ `ContextAssembler` 先按原始问题预检索注入 `[Related Evidence]` → `ChatAgent`（LangGraph StateGraph 工具调用循环）可继续调用 `rag.search_similar_documents` 改写查询补查 → 模型生成带 `[n]` 编号引用的答案 → 证据落 `evidence_items` 表。
5. **工程决策**：跨领域问题必须走 `workflow.run_engineering_review` → `WorkflowOrchestrator`：Planner 产出 `WorkflowSpec`（任务 DAG）→ 按依赖/并发批量跑专用 Agent → Observer 评估置信度与证据缺口 → 不达标可 replan（最多 2 轮）→ Synthesis 汇总输出（`workflow_orchestrator.py`）。
6. **写操作边界**：任何 GitHub 写意图先经 `safety_agent.classify_action_risk`，只产出 `action_drafts`（草稿），**不直接写回 GitHub**（`docs/architecture/security.md`）。
7. **反馈闭环**：用户点赞/点踩 → `chat_feedback` 表；负反馈可按 webhook 推飞书机器人（`FEISHU_FEEDBACK_WEBHOOK_URL`），并可一键生成 `create_issue` 草稿。
8. **评测回归**：`POST /api/evals/rag/run` 立即返回 `queued` + `eval_id`，FastAPI `BackgroundTasks` 在隔离会话里真实跑生产 ChatAgent + RAGAS 0.4 五指标打分，硬规则 + 阈值双门槛判定 passed/failed（`routes/evaluations.py`）。

---

## 4. 架构分层与扩展机制

### 4.1 分层

```
Next.js 前端（页面 + React Query）
   │  HTTP/SSE（NEXT_PUBLIC_API_BASE_URL）
FastAPI 路由层（api/routes/*.py，18 个 router）
   │
Schema 层（schemas/*.py，Pydantic v2 请求/响应模型）
   │
服务层 services/：
   ├─ agents/        多 Agent（聊天控制器 + 专用分析 Agent + 编排器）
   ├─ rag/           检索管线（纯函数式：extraction→chunking→embedding→vector→rerank→qa）
   ├─ github/        GitHub REST 封装
   ├─ llm/           LLM 调用封装
   ├─ skills/        技能注册表
   └─ chat_memory / memory_hub / context_compression  记忆与上下文预算
   │
数据层：SQLAlchemy 2.0（PostgreSQL/pgvector）+ pymilvus（Milvus）+ 本地文件系统（.data/repos 工作区）
   │
外部：GitHub REST API / OpenAI 兼容 LLM（DeepSeek 云 或 Ollama 本地）/ DashScope Embedding&Rerank
```

### 4.2 扩展机制（值得重点借鉴）

| 机制 | 实现位置 | 要点 |
|---|---|---|
| **Skill Runtime（SKILL.md 技能体系）** | `services/skills/registry.py` + `app/skills/*/SKILL.md` | 每个技能一个带 YAML frontmatter 的目录：声明 name/version/entrypoint/允许工具/triggers/safety_level；注册表解析 frontmatter，运行时按「用户显式指定」或「触发条件」激活，只把完整指令注入当前任务的 system prompt；版本、激活原因、指令摘要（hash）、执行校验全部写进 trace。与 Claude Agent Skills 同构。 |
| **Workflow DAG 编排器** | `agents/workflow_orchestrator.py` | Planner 输出任务图（任务+依赖+并发上限）→ 拓扑执行、依赖失败自动 skip → Observer 打分 → 可 replan（最多 2 轮，有「图签名比对」防死循环）→ Synthesis 出报告；全链路 metrics（duration/task_count/replan_count/success/error）。 |
| **工具注册表（raw_tools 注入）** | `chat_agent.py` 约 L90–L103、L1312–L1325 | 工具以 `{name: async callable}` 字典注入运行时，ChatAgent 用 LangChain `StructuredTool` 包装后交给模型原生 function-calling；工具名即「命名空间.动作」（`workspace.read_file`、`rag.search_similar_documents`、`workflow.run_engineering_review`…）。 |
| **MCP 记忆服务** | `mcp/memory_server.py` + `services/mcp_client.py` | 独立 stdio JSON-RPC 进程暴露 `repomind_search_evidence` / `repomind_get_thread_context` / `repomind_read_session_events` 三个记忆工具，主进程以子进程方式拉起。 |
| **Embedding 供应商契约** | `services/rag/embeddings.py` | provider 归一化别名表（local/openai_compatible/dashscope/deterministic），启动校验 (provider, model, dims) 三元组契约，维度不符直接报错而非静默换向量空间。 |

---

## 5. AI 能力的使用方式与边界

| 项 | 实际实现 | 证据 |
|---|---|---|
| 对话/推理 LLM | OpenAI 兼容 `/chat/completions`；**双栈并存**：裸 httpx 封装（`services/llm/client.py`，支持 json_object 模式、流式 SSE）+ LangChain `ChatOpenAI`/LangGraph StateGraph（`chat_agent.py:6-9`） | `llm/client.py`、`chat_agent.py` |
| 默认模型 | `deepseek-v4-pro`（云端，`LLM_BASE_URL=https://api.deepseek.com`）；可切 Ollama 本地 `qwen3.5:9b`（端口 11434） | `core/config.py:16-18`、`运行手册.md` 第八节 |
| 切换方式 | `switch-llm.ps1 deepseek|ollama`：把 `.env.<mode>` 整体覆盖为 `.env`，重启后端生效 | `switch-llm.ps1` |
| Embedding | `Qwen3-Embedding-0.6B` 本地 GPU（sentence-transformers，1024 维）；或 DashScope `text-embedding-v4`；或 deterministic hash 兜底（仅演示/测试） | `rag/embeddings.py`、`.env.example:19-28` |
| Rerank | `qwen3-rerank`（DashScope），不可用时自动回退启发式重排 | `config.py:30-32`、`运行手册.md` 第七节 |
| Judge 模型 | RAGAS 评测 judge 复用 LLM_* 或独立 `RAGAS_JUDGE_*` 三件套 | `config.py:40-54` |
| Prompt 组织 | 系统提示 = **稳定缓存前缀**（「你是 RepoMind 对话控制器…」，`prompt_cache_enabled`）+ 每轮动态拼接（可用工具清单、已注册技能摘要、按需激活的 Skill 完整指令、团队上下文、服务端预检索证据 `[Related Evidence]`、最近消息、工具结果索引、用户消息） | `chat_agent.py:953-1025` |
| 无 Key 兜底 | `LLM_API_KEY` 为空时 `LLMClient` 直接返回 `{}`/`""`，系统走启发式「无模型兜底回答」，不会崩 | `llm/client.py:18-19,44-45`、`运行手册.md` 第七节 |
| 模型输出如何被使用 | 结构化 JSON（`response_format=json_object` + Pydantic Schema 校验）；工具调用结果即观察证据，进入下一轮；最终答案必须带编号引用 `[1][2]`，引用来自 `evidence_items`；不允许编造工具结果（系统提示硬约束） | `llm/client.py:27`、`structured_outputs.py`、`chat_agent.py:959` |
| 上下文工程 | `context_compression.py`：24K token 输入预算、动态模型窗口、超预算自动压缩（保留最近 8 条、摘要 1800 token）、system/memory/evidence/recent/tool 五段预算比例（0.12/0.22/0.34/0.22/0.10） | `config.py:55-73` |
| 安全边界 | 写操作一律先过 safety agent，只出草稿不执行；推荐负责人只能来自 `team_members` 表，禁止从文档/记忆片段「脑补」人 | `chat_agent.py:963-968` |

---

## 6. API / 对外接口设计

- **风格**：FastAPI 自动 OpenAPI 文档（`/docs`），路由按域挂载，`main.py:63-80`。
- **路由前缀一览**：`/api/repos`、`/api/workspaces`、`/api/issues`、`/api/pull-requests`、`/api/ci`、`/api/chat`、`/api/chat/feedback`、`/api/action-drafts`、`/api/reports`、`/api/search`、`/api/skills`、`/api/rag`、`/api/code-graph`、`/api/evals`、`/api/webhooks`，外加 `/health`。
- **流式接口**：聊天走 SSE（`StreamingResponse` + `encode_sse`，`chat_agent.py:2068`），事件类型含 `delta`/工具调用/最终答案。
- **请求/响应**：全部经 `app/schemas/` 的 Pydantic v2 模型；RAG API 示例（`README.md:99-109`）：`POST /api/rag/knowledge-bases`、`POST /api/rag/{repo_id}/ask`、`POST /api/rag/{repo_id}/reindex` 等。
- **鉴权**：**未观察到真实鉴权**——`permissions.py` 提供 `ensure_demo_user`（演示模式默认用户），无登录/无 token 中间件；CORS 按环境区分（生产关 localhost 正则放行，`main.py:39-42`）。GitHub Token 持久化时用 Fernet 加密（`core/security.py`）。
- **错误处理**：自定义 `MilvusUnavailableError` → 503 `{status: degraded}`（`main.py:48-53`）；LLM 错误翻译为中文友好提示（Ollama 未起 502 / Key 失效 401 / 超时，`chat.py:39-56`）。
- **Webhook**：`webhooks.py` 路由存在（GitHub webhook secret 可配）。

---

## 7. 数据 / 存储 / 任务机制

| 类别 | 技术 | 实际用法 | 证据 |
|---|---|---|---|
| 关系库 | PostgreSQL 16 + pgvector（`pgvector/pgvector:pg16` 镜像） | SQLAlchemy 2.0 ORM，29 张表：repositories/conversations/issues/pull_requests/pr_files/workflow_runs/chat_messages/chat_sessions/thread_memories/conversation_memories/evidence_items/code_symbols/code_versions/agent_runs/eval_runs/action_drafts/audit_logs… | `db/models.py`、`docker-compose.yml:4-21` |
| 向量库 | Milvus v2.5.4 standalone（local 存储模式，etcd 作元数据） | collection `repomind_rag_qwen1024`（1024 维）；pymilvus 为**可选依赖**，缺失时整体降级为「degraded」而非崩溃；维度契约不符会拒绝启动 | `rag/vector_store.py`、`config.py:35-38` |
| 文件存储 | 本地文件系统 | clone 仓库 `.data/repos/`、PR worktree `.data/repos/worktrees/`、上传文档 `.data/uploads/`；均 gitignored | `config.py:78-79` |
| 缓存 | Redis 7-alpine **仅在 compose 里以 `profiles: optional` 声明** | 代码中**未观察到**任何 Redis 使用（无 redis 客户端依赖、无调用） | `docker-compose.yml:23-31` |
| 任务/队列 | **无独立消息队列** | 后台任务 = ① FastAPI `BackgroundTasks`（RAGAS 评测）；② `asyncio.create_task` 常驻协程（仓库周期同步，300s） | `evaluations.py:92,146-157`、`scheduler.py` |
| 迁移 | **无 Alembic 迁移目录**（requirements 里列了 alembic 但未使用） | 启动时 `Base.metadata.create_all` + 手工 `ALTER TABLE` 补丁式演进（`main.py:86-140`，注释自承「生产风格流程会在后续加入 Alembic 迁移」） | `main.py:87` |
| 对象存储 | MinIO 已退役 | 镜像停更，Milvus 改 local 卷；minio 服务定义留在 `profiles: legacy-minio` | `docker-compose.yml:45-59`、`运行手册.md` 第二节 |

---

## 8. 测试体系

- **框架**：pytest 8.3.4 + pytest-asyncio（`requirements.txt:24-25`）。
- **规模**：`backend/app/tests/` 下 27 个测试文件、约 5,110 行。
- **覆盖点**（按文件名）：RAG 管线（chunking/qa/rerank/retrieval_pipeline/source_policy/project_isolation/vector_store）、各 Agent（chat/pr_review/issue/ci_debug/recommendation）、工作流编排器、技能注册表、代码索引与搜索、上下文压缩与记忆投影、会话增删、仓库连接/同步/删除、eval runner。
- **在线评测（产品功能级测试）**：内置 RAGAS 0.4 五指标（Context Precision/Recall、Faithfulness、Answer Relevancy、Agent Goal Accuracy）+ 硬规则 + 阈值回归，失败 case 可重跑、可两版对比、可转 ActionDraft。
- **如何运行**：`pytest`（未观察到项目级 pytest.ini/pyproject 配置，也未观察到根目录 CI 配置）。
- **前端测试**：**未观察到**任何前端测试框架（package.json 无 vitest/jest）。
- **本项目自身的 CI/CD**：**未观察到**根目录 `.github/workflows`（看到的 workflow yml 都在 `.data/repos/` 下被克隆进来的演示目标仓库里，不是 RepoMind 自己的）。

---

## 9. 启动 / 安装 / 部署 / 运行

- **依赖管理**：后端 `backend/requirements.txt`（全部 pinned 或范围约束，26 项）+ 可选 `requirements-local-embedding.txt`；前端 `package.json` + `package-lock.json`（**有锁文件**）。
- **安装**：`运行手册.md` 给出 uv 创建 venv、pip 安装、GPU 版 torch（cu128）、`npm install` 全流程；模型首次从 HuggingFace 下载（国内走 `HF_ENDPOINT=https://hf-mirror.com` 镜像，另有 `scripts/download_model_parts.py` 手动分片下载）。
- **基础设施**：`docker compose up -d postgres etcd milvus`（三个容器，`restart: unless-stopped`，postgres 带 healthcheck 与 `scripts/init_pgvector.sql` 初始化）。
- **启动脚本群**（`scripts/`）：`start-all.ps1` / `start-backend.ps1`（自动检查并拉起 Ollama）/ `start-ollama.ps1` / `stop-all.ps1` / `repomind-controller.ps1` / `repomind-watchdog.ps1` / 纯演示模式 `start_rag.ps1`（关 Milvus、deterministic embedding、SQLite 兜底）。
- **运行形态**：开发 `uvicorn --reload`；生产 `uvicorn --workers 2` + `next build && next start`（`运行手册.md` 生产模式节）。后端本机端口 8001（避开被占的 8000）。
- **环境变量**：`.env.example` 全量注释（88 行）；`.env.deepseek` / `.env.ollama` 两套切换配置；pydantic-settings 读取 `../.env`。
- **容器化**：只有基础设施的 compose，**应用本身无 Dockerfile**。
- **运行时状态**：工作区存在用户未提交修改（`backend/app/api/routes/chat.py` +2 行、`frontend/app/workflow-runs/page.tsx` +62/-11 行）与未跟踪目录（`chat_history/`、`sessions/`、`frontend/public/`）——属本机使用痕迹，非交付代码。

---

## 10. 文档体系

| 文档 | 内容 |
|---|---|
| `README.md`（8.5KB） | 核心能力、Mermaid 架构图、快速开始、RAG 系统说明、RAGAS 评测使用方法 |
| `运行手册.md`（约 330 行） | 本机环境版本表、启动步骤、验证命令、.env 逐项说明、首次安装、FAQ（模型下载/端口占用/Docker 镜像/Milvus 端口冲突）、遗留事项、生产模式、Ollama 故障排查 |
| `docs/architecture/system_design.md` | 总体 Mermaid 架构（较简） |
| `docs/architecture/agent_workflow.md` | 多 Agent 工作流说明 |
| `docs/architecture/database_schema.md` | 数据库 schema 说明 |
| `docs/architecture/security.md` | 安全边界 5 条（Token 不进日志、不写回 GitHub、Token 加密等） |
| `docs/api/openapi.md` | API 文档 |
| `docs/examples/rag_employee_handbook.md` | RAG 示例知识库语料 |
| `docs/datas/evals/rag_cases.json` | 内置 RAGAS 正式评测基准集（README 引用） |
| 各 `app/skills/*/SKILL.md` | 6 个技能的操作手册（含 PR 审查 P1/P2/P3 分级标准、`references/risk-checklist.md`） |

---

## 11. 演示 / 示例数据 / 简历展示方式

- 根目录 4 张中文演示截图：`演示_前端首页.png`、`演示_知识库.png`、`演示_RAG问答.png`、`演示_RAG工作室2.png`（与 `运行手册.md` 互相印证）。
- `scripts/seed_demo_data.py` 种子演示数据；`backend/.data/repos/demo-sample-api/` 是一个自带的小型 FastAPI 演示目标仓库（含 tests、GitHub Actions），用于演示「连接真实仓库→分析→PR worktree」全链路。
- `.data/repos/` 下还沉淀了对作者自己另一个项目 `AI-smart-companion` 的真实同步结果（说明系统真跑通过 GitHub 同步链路）。
- 技术亮点（README 自述 + 代码印证）：混合 RAG（预检索注入 + Agent 工具内补查）、Skill Runtime、可 replan 的 DAG 工作流、上下文预算压缩、RAGAS 在线回归评测、写操作草稿化安全边界。

---

## 12. 技术栈清单（以清单文件为准）

**后端**（`backend/requirements.txt`）：
Python 3.11 · FastAPI 0.115.6 · Uvicorn 0.34 · Pydantic 2.10 / pydantic-settings · SQLAlchemy 2.0.36 · psycopg3 · pgvector 0.3.6 · pymilvus ≥2.5 · httpx 0.28 · LangChain 1.2.17 + langgraph 1.1.10 + langchain-openai 1.2.1 + langchain-community · ragas 0.4.3 · openai 2.53 · cryptography · python-multipart · pypdf / python-docx · pytest 8.3.4 + pytest-asyncio。
（声明但**未实际使用**：alembic 已列依赖但无迁移目录；langchain/ragas 版本号较新，为真实生产组合。）

**前端**（`frontend/package.json`）：Next.js 15.1.2 · React 19 · TypeScript 5.7 · Tailwind 3.4 · @tanstack/react-query 5.62 · react-markdown + remark-gfm · lucide-react。

**基础设施**：PostgreSQL 16(pgvector) · Milvus 2.5.4 standalone · etcd 3.5 ·（可选）Redis 7 ·（已退役）MinIO。

**模型**：LLM = DeepSeek `deepseek-v4-pro`（云）或 Ollama `qwen3.5:9b`（本地）；Embedding = Qwen3-Embedding-0.6B 本地 GPU（1024 维）/ DashScope text-embedding-v4；Rerank = qwen3-rerank。

---

## 13. 工程成熟度亮点（可借鉴）

1. **Skill Runtime（SKILL.md 注册表）**——技能即目录 + YAML frontmatter，声明式定义允许工具、触发词、安全级别，运行时按需注入提示词并留痕（版本/原因/hash），与现代 Agent Skill 生态同构，扩展新分析能力只需新增一个目录。
2. **可 replan 的 DAG 工作流编排**——Planner 出图、拓扑执行、依赖失败自动 skip、Observer 置信度驱动重规划（带图签名防死循环、最多 2 轮）、全链路 metrics 落库。
3. **混合 RAG 双路证据**——调用前 ContextAssembler 预检索 + 调用中模型可多次改写 query 补查交叉验证；向量召回明确限定「只对跨来源非结构化知识向量化」，源码走词法工作区、结构化数据直接查库，避免过度向量化。
4. **Embedding 契约与降级设计**——(provider, model, dims) 三元组启动校验、Milvus 不可用明确 503/degraded 而不是静默换向量空间、无 Key 时走启发式兜底且页面明确标注 degraded 状态。
5. **上下文工程成体系**——24K 预算五段比例分配、自动压缩/微压缩、prompt 缓存前缀设计、工具结果投影摘要（避免每轮把全部观察塞回 prompt）。
6. **评测即产品功能**——在线 RAGAS 五指标 + 硬规则双门槛、queued→轮询的异步模式、失败 case 可仅重试异常评分、可两版回归对比、可转人工草稿，把「评估 Agent 质量」做成了 UI 闭环。
7. **安全边界内建**——GitHub 写操作只出草稿不执行、推荐负责人只能来自团队表、工作区工具禁止读 `.env` 等密钥文件名、Token Fernet 加密落库。
8. **Windows 运维脚本群 + 运行手册**——一键启停、watchdog、Ollama 自愈、双 LLM 后端一键切换，文档与实际部署状态互相对账（含故障案例复盘）。

## 14. 不建议照搬的部分（含原因）

1. **启动期手工 `ALTER TABLE` 演进 schema**（`main.py:86-140`）——课程项目级做法，多环境/多人协作时极易状态漂移；已列 alembic 依赖却未用。生产应换真正的迁移工具。
2. **`Base.metadata.create_all` 自动建表**——无版本化 schema 管理，无法回滚、无法 review 变更。
3. **无真实鉴权**——`ensure_demo_user` 假用户、无登录态、无权限模型，多人使用即裸奔；只能作为本地演示形态。
4. **日志极简**——`core/logging.py` 仅 basicConfig INFO，无结构化日志、无请求 ID、无 trace 采集（虽然业务侧有 trace 落库，但运行日志不可观测）。
5. **任务队列缺位**——后台任务全靠 asyncio 协程 + BackgroundTasks，多 worker（已配 `--workers 2`）下后台任务不共享、重启即丢、无法水平扩展；RAGAS 重活在 worker 内跑有资源风险。
6. **LLM 客户端吞异常**——`chat_json` 把 `httpx/JSON` 异常统一吞成 `{}`（`llm/client.py:40-41`），排障时只能靠上层模糊提示；错误可观测性差。
7. **测试无 CI 兜底、前端零测试**——27 个后端测试不错，但没有任何 CI 自动跑；前端 15 个页面无测试框架。
8. **`requirements.txt` 无 lock 机制**（后端）——pinned 不全，重建环境可能拿到不同补丁版本；前端有 package-lock 而后端没有。
9. **敏感默认值**——`token_encryption_key` 默认 `change-me-in-local-dev`、Fernet 密钥用 sha256 派生（代码注释自承「不适合生产」）。
10. **仓库仅 2 个提交**——工程历史单薄，分支策略/PR 流程在本仓库自身未体现（虽然产品是做 PR 协作的）。

---

## 15. 对 LocalDocMask 的可迁移建议清单

按「性价比从高到低」排序：

1. **照搬 Skill（SKILL.md）目录约定**：每个技能一个文件夹 + frontmatter（name/version/允许工具/触发词/安全级别），注册表启动时解析、运行时按需注入提示词并记录激活 trace——LocalDocMask 若有多类文档处理动作（解析/摘要/问答/脱敏），这是最干净的扩展方式。
2. **Embedding 三元组契约校验**：换 embedding 模型/供应商时显式报错并要求重建索引，杜绝「静默换向量空间」类事故。
3. **降级分级透明化**：向量库不可用 → 503+degraded 状态码；无 API Key → 启发式兜底但 UI 明确标注 degraded；不要让两种质量等级的输出混在一起。
4. **上下文预算五段分配**：system/记忆/证据/历史/工具结果按固定比例切预算，超预算自动压缩保留最近 N 条——长文档问答尤其需要。
5. **异步重活模式**：`POST 立即返回 queued + id → BackgroundTasks 后台跑 → 前端轮询进度`，比同步长请求健壮；注意多 worker 下的局限，规模大了再上队列。
6. **答案必须带编号引用 + evidence 表落库**：每个引用可点查原文位置，负面反馈可回溯到具体证据片段。
7. **写操作草稿化**：任何有副作用的动作先产 ActionDraft 人工确认，绝不自动执行。
8. **评测闭环 UI**：固定评测集 + 指标阈值 + 失败 case 可复跑/对比/转待办——LocalDocMask 做文档问答质量回归可直接套用这套思路。
9. **运行手册与实际部署对账**：把端口、依赖版本、模型路径、常见故障（如 502=Ollama 没起）写成带验证命令的 runbook。
10. **反向避坑**：一开始就引入 Alembic 迁移、结构化日志、真实鉴权；后端依赖也要有锁文件（uv pip compile / pip-tools）。

---

## 附：分析过程证据索引

- 通读：`README.md`、`运行手册.md`、`backend/app/main.py`、`core/config.py`、`core/security.py`、`core/logging.py`、`services/llm/client.py`、`services/agents/workflow_orchestrator.py`（全量）、`services/skills/registry.py`（前 120 行）、`docs/architecture/system_design.md`、`docs/architecture/security.md`、`services/scheduler.py`、`mcp/memory_server.py`（头部）、`switch-llm.ps1`、`app/skills/pr-review/SKILL.md`、`docker-compose.yml`、`.env.example`、`.gitignore`、`backend/requirements.txt`、`frontend/package.json`。
- 精读/抽样：`services/agents/chat_agent.py`（2069 行，按行号抽样系统提示词 L953–1025、工具表 L1312–1325、工具清单 L90–103）、`services/rag/vector_store.py`（前 80 行）、`services/rag/embeddings.py`（grep 提供者归一化逻辑）、`api/routes/chat.py`（前 100 行）、`api/routes/evaluations.py`（grep 异步任务机制）、`db/models.py`（grep 全部 29 张表）。
- 只读 git 命令：`git log --oneline -20`、`git status --short`、`git branch -vv`、`git diff --stat`、`git ls-files | Measure`（均只读，未做任何写操作）。
- 目录枚举：`backend/`（递归文件清单）、`frontend/`（递归文件清单）、`docs/`、`scripts/`、`assets/`；测试与代码行数统计。
