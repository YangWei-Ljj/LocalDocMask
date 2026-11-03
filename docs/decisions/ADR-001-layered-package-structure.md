# ADR-001：采用分层包结构替代扁平单包

- 状态：Accepted
- 日期：2026-10-26
- 决策者：架构组

## 背景

现状（见 `docs/project_audit/localdocmask_current_state.md` §1.2）：`src/localdocmask/` 为扁平单包，14 个 `.py` 平铺，数据模型、识别规则、业务编排、文件解析、OCR、LLM、审计、GUI 全部在同一层；GUI 直接 `new Detector/TaskManager/TaskManager/OllamaClient`（`gui.py:18-23`），核心业务逻辑被 UI 与模型调用代码耦合。空目录 `core/`、`ui/` 仅为占位。

RepoMind 虽业务更复杂，但其分层（`api/routes → schemas → services → db`）与依赖方向清晰（见 `docs/project_audit/repomind_baseline.md` §4）。

## 决策

将代码按职责重组为以下顶层包（位于 `src/localdocmask/` 下）：

| 包 | 职责 |
|---|---|
| `domain` | 业务实体、枚举、统一异常、领域策略（纯逻辑，不依赖其他层） |
| `recognition` | 高精度规则、上下文判定、知识词库、多源融合与置信度仲裁 |
| `application` | 端口（Protocol）、端到端流水线编排、任务/批次协调 |
| `infrastructure` | 配置、日志、SQLite 持久化、OCR、Ollama、文件格式处理器 |
| `security` | 哈希、网络边界、临时文件管理、独立审计、脱敏引擎与映射表 |
| `presentation` | PySide6 GUI、后台工作线程、视图组装 |
| `evaluation` | 标注集加载、指标计算、评测运行 |

## 依赖方向（强制）

```
presentation ──► application ──► domain
                     │              ▲
evaluation ─────────►│              │
                     ├─► recognition (依赖 domain)
                     └─► security (依赖 domain / infrastructure 接口)
infrastructure ──► domain （实现 application 定义的端口）
```

- `domain` 不允许 import 任何其他业务包或第三方重框架。
- `application` 只依赖 `domain` 与自身定义的 `ports`，不直接依赖具体实现；具体实现通过构造注入。
- 外层可依赖内层，内层不得反向依赖外层。
- 可用 `__init__.py` 做受控的门面导出，但不做循环依赖。

## 迁移策略

采用"移动 + 保持测试绿"的渐进迁移，而非一次性推翻：
1. 先建立新包与 `domain`（迁入 `models.py`、新建异常体系）。
2. 逐包迁移，每迁移一个模块立即更新 import 并跑 `pytest`。
3. 保留旧模块路径的轻量兼容 shim（仅在必要时），迁移完成后删除并记录。
4. 原始文件内容逻辑不随意改动，重构与行为修复分开提交，便于回溯。

## 后果

- 正面：职责清晰、可独立测试、可替换实现（如换 LLM/持久化）、作品集结构专业。
- 负面：一次性目录调整与 import 改写工作量较大；需要严格的迁移纪律。
- 约束：不为形式新增无实际调用的抽象层；包数量随真实职责收敛。
