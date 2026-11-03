# ADR-006：配置采用 YAML 文件 + dataclass 校验与安全默认值

- 状态：Accepted
- 日期：2026-10-26
- 决策者：架构组

## 背景

现状：`config.py` 全部硬编码（目录、Ollama URL/模型、默认掩码），无配置文件、无校验；`config/` 为空目录（P1-6）。RepoMind 使用 `pydantic-settings` + `.env.example`，但本项目无需引入 pydantic 依赖（`pyyaml` 已在环境中）。

## 决策

- 配置文件：`config/config.example.yaml`（随仓库提交，含全部字段与注释）与 `config/config.yaml`（用户实际配置，可 gitignore）。
- 加载层 `infrastructure/config/settings.py`：用 dataclass 定义配置结构与类型，读取 YAML 后做类型/范围校验，缺失字段回退到安全默认值，非法值（如非回环 Ollama 地址、置信度越界）显式报错。
- 环境变量仍可覆盖关键路径（保持 `LOCALDOCMASK_HOME` 现有行为，便于测试隔离）。
- 配置分组：`paths`、`ollama`（url/model/enabled/timeout）、`recognition`（公司正则开关、置信度阈值、复核策略）、`masking`（默认模式、掩码字符、映射作用域/持久化）、`security`（网络白名单、临时文件保留策略）、`logging`（级别、格式、文件保留）。

## 安全默认

- Ollama 默认仅回环；默认不启用任何外部网络；默认失败关闭；默认映射不落盘。

## 后果

- 正面：配置可版本化、可校验、可复现；安全默认集中管理。
- 负面：需维护配置结构与示例的一致性。
