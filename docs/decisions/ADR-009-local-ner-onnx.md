# ADR-009：本地中文 NER 适配器（ONNX-BERT，默认关闭、可插拔、失败关闭）

- 状态：Accepted
- 日期：2026-10-28
- 决策者：架构组

## 背景与问题

无标点连续文本下，规则/词典对公司名边界（前导动词、吞字）与词典外裸人名召回存在盲区。需要一个**真正可在本机离线运行**的中文 NER（PER/ORG/LOC）作为低权重补充候选，且必须满足：
- 默认关闭（`enabled=False`）、可插拔（端口化）、失败关闭（任何异常/缺依赖/缺模型都不阻断主流程）；
- 与现有 `onnxruntime` 推理栈协调（OCR 已用 rapidocr-onnxruntime）；
- 不污染默认 PyInstaller 包体积，未安装模型时系统照常运行；
- NER 输出只作候选（`source="ner"`，与 ollama 同口径），**不**参与"是否残留/可否导出"的最终判定，仅模型来源的上下文类实体转人工复核。

## 候选方案与评估

| 方案 | 运行时依赖 | 离线可用 | 体积 | 结论 |
|---|---|---|---|---|
| 百度 LAC | paddlepaddle（~1GB+） | 是 | 重 | **不采用**：引入 paddle 重依赖，打包体积与维护成本高。 |
| 哈工大 LTP v4 | transformers + torch | 是 | 重 | **不采用**：默认要 torch，与轻量目标相悖。 |
| HanLP | torch / tensorflow | 是 | 重 | **不采用**：同上。 |
| transformers 中文 NER（torch 运行时） | torch CPU（~800MB 安装）+ transformers | 是 | 重 | **不采用为运行时**：torch 进默认包会让 exe 体积暴涨。 |
| 复用本地 Ollama 做结构化 NER | 本地 ollama 服务 + 模型 | 依赖 ollama 常驻 | 中 | **不单独采用**：现有 `llm` 槽位已是 ollama 自由抽取；自由 JSON 抽取对 span 边界弱，再做一个近似重复。 |
| **ONNX-BERT 中文 NER（本方案）** | onnxruntime（已在栈内）+ numpy（已在栈内）+ 手写字符分词；**运行时不 import torch/transformers/tokenizers** | 是，推理零网络 | 运行时 0 新增 Python 依赖；模型制品 ~388MB | **采用**。 |

## 选定实现

- 模型：`shibing624/bert4ner-base-chinese`（人民日报 NER 微调，BIO 标签，标签集含 B/I-PER、B/I-ORG、B/I-LOC、B/I-TIME、O）。
- 一次性导出：本机用 CPU `torch` + `transformers` 把该模型导出为 ONNX（`scripts/export_ner_onnx.py`），制品落在 `models/ner_bert4ner/`：
  - `model.onnx`（计算图，~1.5MB，外部权重引用）
  - `model.onnx.data`（权重，~387.9MB）
  - `vocab.txt`（BERT 词表）、`id2label.json`（运行时标签表，避免依赖 transformers）。
- 运行时适配器：`src/localdocmask/infrastructure/ner/onnx_ner.py`
  - 手写 BERT 字符级分词（中文逐字入词表，未知字 `[UNK]`，忽略空白），与 bert-base-chinese 分词行为对齐；
  - `onnxruntime.InferenceSession` 跑 `[input_ids, attention_mask, token_type_ids]` → `logits`；
  - BIO 解码出 PER/ORG/LOC；标签映射 **PER→姓名、ORG→公司名、LOC→详细地址**，TIME/O 忽略；
  - ORG 前导动词保守剥离（仅去除前导通用动词/主语，且要求剩余串仍以公司后缀结尾，绝不发明内容），缓解无标点连续文本吞字。

## 依赖体积与打包影响（如实）

- **运行时新增 Python 依赖：0**。onnxruntime 1.31 与 numpy 2.4 均为项目既有栈；手写分词不引入 `tokenizers`。
- **模型制品 ~388MB**，目录 `models/` 已加入 `.gitignore`，**不入库、不打入默认 exe**。
- **一次性导出工具链**（torch CPU、transformers、onnx、onnxscript）仅用于本机构建 `model.onnx`；**不写入 requirements，不进 PyInstaller spec**，与交付运行时隔离。
- 默认 `ner.enabled=False`；组合根 `composition.py` 仅在启用时惰性构造，构造失败（缺 onnxruntime/缺模型文件/会话初始化失败）一律捕获并降级 `ner=None`，系统行为与未接 NER 完全一致。

## 离线可用性

- 模型权重本机离线加载，推理全程无任何网络请求（与 ollama 需常驻服务不同）。已在本机断网语义下跑通真实句子（见 quality_report NER 节）。

## 已知误差与残余边界（如实，不粉饰）

1. **LOC 碎片**：人民日报模型对"北京市朝阳区望京街"倾向切成 `北京市/朝阳区/望京街` 多个独立 LOC，不自动粘合为一个地址 span。评测中该类真值无法精确命中，转人工复核。
2. **ORG 残余吞字**：前导动词剥离仅覆盖常见动词白名单（对接/聘请/签约/合作/采购…），罕见前导成分仍可能被模型吞入；因 NER 来源为低权重候选，残余错误只产生 NEEDS_REVIEW 候选，不会自动脱敏、不决定导出。
3. **长文本分块**：超过 `max_length` 按块处理，跨块边界的实体可能被截断（本应用文档短文本，影响有限）。
4. NER 模型自身误识别（如把普通词误判为人名）一律经融合降权转人工复核，不直接进入"可导出/残留"判定。

## 后果

- 正面：以零运行时新依赖获得确定性、离线的实体边界补充；默认关闭、失败关闭，攻击面与包体积可控；与 ollama 同口径进融合仲裁，符合"候选而非裁判"。
- 负面：模型制品体积大（~388MB），按需本机下载；LOC 粘合等模型级误差需人工复核兜底。
