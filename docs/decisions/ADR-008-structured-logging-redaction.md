# ADR-008：结构化日志与敏感值过滤

- 状态：Accepted
- 日期：2026-10-26
- 决策者：架构组

## 背景

现状：`src/` 下 0 处 `logging` 调用；唯一的"记录"是写单任务 JSON，且异常被 `except: pass` 吞掉（`task_manager.py:242-243`，P1-4）。总控指令要求结构化日志，且日志不得保存完整敏感原文。

## 决策

- 建立统一日志设施 `infrastructure/logging/logger.py`：
  - 默认输出结构化 JSON（时间、级别、logger、task_id 关联字段、事件、消息），同时保留控制台可读格式。
  - 日志文件位于 `data/logs/`（gitignored），按大小/天数轮转与保留。
- **敏感值过滤（强制）**：
  - 提供日志 RedactionFilter，对消息中的高风险模式（手机/身份证/邮箱/银行卡）与已知候选值做掩码，复用 `mask_display`。
  - 约定只记录"类别 + 掩码值 + 位置/阶段"，不记录完整原文、完整提示词、完整实体。
- 统一异常体系（`domain/exceptions`）携带错误码；禁止裸 `except: pass`，捕获后必须以适当级别记录并做安全处理（失败关闭）。
- 关键操作（导入、解析、识别、复核、脱敏、审计、导出、隔离）均记录带 task_id 的可追踪事件。

## 后果

- 正面：可观测、可审计、可排障，且不泄露敏感原文。
- 负面：需在各模块统一接入；过滤器需随规则维护。

## 实现状态（2026-10-26 对账，不重写决策）

- 落地于 `infrastructure/logging/logger.py`：JSON 文件 + 可读控制台，按大小轮转。
- `RedactionFilter` 除 `msg/args` 外，对 `record.__dict__` 中所有非保留 `extra` 字符串属性统一掩码（修复了 `extra={"event": phone}` 直落盘的历史缺陷）。
- 扫描后经 `register_sensitive_values` 登记当次候选原文，日志输出前统一替换为掩码；该登记为内存态、不落盘。
- 关键操作（prepare/scan/process/finalize）经 `operation_log` 记录，detail 仅阶段/计数/状态，不含原文。
