# LocalDocMask 架构与业务流程图

> 本文件所有图均以 `master` 分支当前代码（`src/localdocmask/`）为依据绘制。
> 包结构对应 ADR-001，识别融合对应 ADR-002/003，模式 B 映射对应 ADR-004，
> 持久化对应 ADR-005，日志对应 ADR-008。图与代码不一致时以代码为准。

---

## 1. 分层架构与依赖方向

外层依赖内层；`domain` 不依赖任何业务包；应用层只依赖自身定义的端口（Protocol），
具体基础设施实现由唯一的组合根 `application/composition.py::build_services()` 装配并注入。

```mermaid
flowchart TB
    subgraph PRES["presentation 表现层 (PySide6 GUI)"]
        GUI["gui/main_window.py<br/>MainWindow · QThreadPool · 候选表/结果页"]
    end

    subgraph APP["application 应用层 (编排)"]
        COMP["composition.py<br/>build_services() 组合根"]
        PIPE["pipeline.py<br/>TaskManager: prepare→scan→process→finalize"]
        BATCH["batch.py<br/>BatchService: 批次/取消/失败隔离"]
        PORTS["ports.py<br/>TaskRepository Protocol"]
    end

    subgraph REC["recognition 识别层"]
        RULE["rule_based.py 正则+校验"]
        CTX["context.py 上下文/泛指判定"]
        KB["knowledge.py 白/黑/公开/领域词"]
        FUS["fusion.py 多源融合仲裁"]
        RPIPE["pipeline.py RecognitionPipeline"]
    end

    subgraph SEC["security 安全层"]
        AUD["auditor.py 独立审计"]
        MASK["redaction/masker.py A/B 替换"]
        MAP["redaction/mapping.py 一致性映射"]
        PS["path_safety.py 路径越权防护"]
        HASH["hashing.py SHA-256"]
        LIM["limits.py 资源上限"]
    end

    subgraph INFRA["infrastructure 基础设施层"]
        CFG["config/settings.py YAML+校验"]
        LOG["logging/logger.py 结构化+RedactionFilter"]
        DB["persistence/sqlite_repo.py + recovery.py"]
        OCR["ocr/engine.py RapidOCR 离线"]
        LLM["llm/ollama.py 回环客户端"]
        FMT["formats/ pdf·docx·xlsx·image"]
    end

    subgraph DOM["domain 领域层 (纯逻辑)"]
        ENT["entities.py Candidate/Task/AuditReport"]
        ENUM["enums.py TaskStatus/EntityDecision/RiskLevel"]
        POL["policy.py ReviewPolicy/risk_for"]
        EXC["exceptions.py 错误码"]
    end

    subgraph EVAL["evaluation 评测层"]
        RUN["runner.py / metrics.py / dataset.py"]
    end

    GUI --> COMP
    GUI --> PIPE
    GUI --> BATCH
    PIPE --> REC
    PIPE --> SEC
    BATCH --> SEC
    COMP --> INFRA
    COMP --> REC
    COMP --> SEC
    REC --> DOM
    SEC --> DOM
    APP --> DOM
    INFRA --> DOM
    INFRA -.实现.-> PORTS
    EVAL --> REC
```

依赖规则（与 ADR-001 一致）：

- `domain` 不 import 任何其他业务包或重框架，只做纯数据结构与策略。
- `application` 通过构造注入拿到 `RecognitionPipeline / Auditor / repository / settings`，
  不直接 `new` 具体基础设施；`composition.py` 是唯一允许 `new` 出基础设施实现的地方。
- `presentation` 只调 `build_services()` 返回的服务集合，不直接 `new Detector/TaskManager/OllamaClient`。

---

## 2. 端到端业务流程（含任务状态机与失败关闭/隔离分支）

实线为正常成功路径；红色分支为失败关闭路径（一律进隔离目录 `data/output/quarantine/`，绝不导出）。

```mermaid
flowchart TD
    Start([选择文件]) --> Prep["prepare<br/>哈希源文件·大小/格式守卫·幂等检测"]
    Prep --> Scan["scan 只读解析+识别<br/>规则→上下文→词库→融合三档裁决"]
    Scan --> HasReview{存在 NEEDS_REVIEW<br/>或 review_flags?}

    HasReview -- 是 --> WaitReview[/"NEEDS_REVIEW 等待人工复核"/]
    WaitReview --> UserDecide["用户逐条裁决<br/>确认 / 排除放行 / 改替换值"]
    UserDecide --> Process
    HasReview -- 否 --> Process

    Process["process 仅处理已确认候选<br/>阶段1 脱敏 → 逐项残留审计"]
    Process --> Resid{有残留或无法验证?}
    Resid -- 是 --> Delete["阶段2 对失败条目执行删除"]
    Delete --> DeleteVer{删除后验证?}
    DeleteVer -- 仍残留/不可读 --> Unresolved[/"UNRESOLVED 未决条目"/]
    DeleteVer -- 已验证 --> Stage3[阶段3 产出中间文件]
    Resid -- 否 --> Stage3

    Stage3 --> FinalAudit["阶段5 独立整体审计<br/>重提取文本/OCR/元数据/隐藏内容"]
    FinalAudit --> Finalize{"finalize 导出门禁<br/>(全部 AND)"}

    Finalize -- 源文件被改动 --> Block["block: 原件哈希变化"]
    Finalize -- 审计无结论/输出缺失 --> Block
    Finalize -- 输出在审计后被改动 --> Block["block: 终态哈希绑定"]
    Finalize -- 存在未决条目 --> Block["block: 未决敏感项"]
    Finalize -- 存在未裁决 NEEDS_REVIEW --> Block["block: 复核门禁"]
    Finalize -- 审计发现残留 --> Block["block: 残留"]

    Block --> Quarantine[/"移动到 quarantine/<br/>状态 UNSAFE/AUDIT_FAILED/NEEDS_REVIEW"/]

    Finalize -- 全部通过 --> Success[/"PASSED 复制到 success/<br/>可导出到用户指定位置"/]
    Success --> Done((结束))
    Quarantine --> Done
    Unresolved --> Quarantine
```

**任务状态机**（`domain/enums.py::TaskStatus`）：

```mermaid
stateDiagram-v2
    [*] --> PENDING: prepare
    PENDING --> PROCESSING: scan 开始
    PROCESSING --> NEEDS_REVIEW: 有候选待裁决
    PROCESSING --> PROCESSED_UNAUDITED: 无候选
    NEEDS_REVIEW --> PROCESSING: 用户裁决后处理
    PROCESSED_UNAUDITED --> PASSED: finalize 全部门禁通过
    NEEDS_REVIEW --> PASSED: 复核后门禁通过
    PASSED --> [*]
    PROCESSING --> INTERRUPTED: 崩溃恢复·已有残留输出
    PENDING --> INTERRUPTED: 崩溃恢复·已有残留输出
    INTERRUPTED --> PENDING: 恢复判定可安全重试
    PROCESSING --> UNSAFE: 失败关闭·隔离
    PROCESSING --> AUDIT_FAILED: 残留/未决·隔离
    NEEDS_REVIEW --> UNSAFE: 复核门禁未过·隔离
    UNSAFE --> [*]
    AUDIT_FAILED --> [*]
```

失败关闭原则：任何一条门禁不通过，产物被 `shutil.move` 移入隔离目录并记录原因，
**不会**被当作成功输出；崩溃后已产出但未审计的文件标记为 `INTERRUPTED`，不自动认定安全。

---

## 3. 识别三档分流（规则 → 上下文 → 词库 → 融合 → 三档）

对应 `recognition/` 包与 `domain/policy.py`。先保召回（规则命中即入候选），
再用上下文/词库/融合精过滤，最后按类别+来源+校准置信度分流。

```mermaid
flowchart TD
    Text[("待识别文本 / OCR 文本")] --> Rule["① 规则检测 rule_based<br/>身份证·手机·固话·邮箱·护照·银行卡·地址·编号·公司正则"]
    Text --> Dict["词典命中<br/>企业名单·人员名单·自定义敏感词"]

    Rule --> Inject["② 词库主动注入<br/>黑名单→公司名 0.95<br/>领域词→自定义敏感词 0.92"]
    Dict --> Inject
    LLM[("可选 本地 Ollama<br/>仅候选·低权重 0.55")] --> Inject

    Inject --> Fuse["③ 多源融合 fusion<br/>按(类别,归一化文本)合并<br/>多源一致+0.05 / 仅ollama-0.15"]
    Fuse --> Ctx["上下文判定 context<br/>前后窗信号·泛指引用剥离"]
    Ctx --> Hit{"词库命中 / 上下文信号"}

    Hit -- 黑名单 / 领域词 --> Auto["AUTO_CONFIRM 自动确认"]
    Hit -- 白名单 且 无高风险来源 --> Supp["SUPPRESS 抑制放行"]
    Hit -- 泛指引用(我们公司/有限公司…) --> Supp
    Hit -- 公开企业名 --> Review["NEEDS_REVIEW 人工复核"]
    Hit -- 高精度格式 校准≥0.9 --> Auto
    Hit -- 高精度格式 校准中等 --> Review
    Hit -- 仅本地模型来源 --> Review
    Hit -- 校准<0.5 --> Supp
    Hit -- 公司/姓名/地址等上下文类 --> Review

    Auto --> Act1[进入脱敏处理]
    Review --> Act2[等待用户确认/排除]
    Supp --> Act3[不脱敏·不进处理]
```

判定理由写入 `Candidate.decision_reason`，在候选表"判定理由"列对用户可见，保证每条结论可解释。

---

## 4. 模式 B 一致性虚构映射示意

对应 `security/redaction/mapping.py::ConsistencyMapper`。真实实体 → 稳定虚构值；
同一批次内跨文件一致；映射表默认仅内存不落盘。

```mermaid
flowchart LR
    subgraph Batch["一个批次（BatchService.run）"]
        F1["文件1<br/>杭州云启科技有限公司"]
        F2["文件2<br/>杭州云启科技有限公司<br/>张伟"]
        F3["文件3<br/>张伟"]
    end

    Mapper["ConsistencyMapper(scope=batch)<br/>内存映射表 + 已占用集合"]
    F1 --> Mapper
    F2 --> Mapper
    F3 --> Mapper

    Mapper -->|"c\x1f杭州云启…" → 云帆科技有限责任公司| R1["虚构公司名(全批一致)"]
    Mapper -->|"p\x1f张伟" → 赵晨雪| R2["虚构人名(全批一致)"]

    Mapper -.非实体类别(手机/身份证…).-> Masker["Masker 兜底统一掩码 ****"]

    subgraph Persist["持久化(默认关闭)"]
        Note["persistence=memory: 进程结束即失效<br/>persistence=protected_file: 显式 opt-in<br/>写入 data/mapping/mapping.protected.json<br/>icacls 收紧为仅当前用户"]
    end
    Mapper -.opt-in.-> Note
```

冲突处理：分配采用确定性序列，维护"已占用虚构值"集合；若候选值已被占用则顺延下一个，
**绝不静默复用**；无法分配时抛 `MappingCollisionError` 使该文件失败关闭。
映射表严禁写入日志/报告/数据库，`__repr__` 只返回条目数。
