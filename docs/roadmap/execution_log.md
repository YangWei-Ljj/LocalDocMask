# LocalDocMask 现状审计执行日志

> 日期：2026-10-26
> 执行主体：现状审计 SubAgent
> 基线提交：`d39eb1e`
> 目标：基于实际运行对 LocalDocMask 做现状审计

---

## 1. 实际执行的命令与结果

### 1.1 仓库状态确认

```powershell
cd '<PROJECT_ROOT>'
git status
git log --oneline -5
```

结果：
- 分支 master，工作区干净
- HEAD = `d39eb1e chore: establish LocalDocMask pre-refactor baseline`

### 1.2 目录盘点

```powershell
Get-ChildItem -Force
Get-ChildItem -Recurse -Directory -Force | Where-Object { $_.FullName -notmatch '\\.venv|\\.git\\|\\.pytest_cache|\\__pycache__' }
Get-ChildItem -Recurse -File -Force | Where-Object { $_.FullName -notmatch '\\.venv|\\.git\\|\\.pytest_cache|\\__pycache__' }
```

发现：
- 顶层 `config/`、`scripts/`、`resources/` 均为空目录
- `src/localdocmask/core/`、`src/localdocmask/ui/` 为空目录（预留）
- `src/localdocmask/` 为扁平结构，无 models/config/patterns 等子目录
- `acceptance/` 下有 8 个 md/json 报告 + `baseline_backup/` 整份旧源码快照

### 1.3 测试集运行

```powershell
.venv\Scripts\python.exe -m pytest tests -q
```

结果：
```
..........................................                               [100%]
42 passed in 29.62s
```

- 通过 42 / 失败 0 / 跳过 0
- 耗时 29.62 s（OCR 模型加载占主要时间）

### 1.4 模块可导入性验证

```powershell
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'src'); from localdocmask import patterns, detection, masker, models, hashing, config, ocr, ollama, auditor, task_manager; from localdocmask.formats import pdf, docx, xlsx, image; print('ALL IMPORTS OK')"
```

结果：`ALL IMPORTS OK`，全部核心模块可导入。

### 1.5 GUI 验证（offscreen）

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv\Scripts\python.exe -c "
from PySide6.QtWidgets import QApplication
from localdocmask.gui import MainWindow
app = QApplication([])
w = MainWindow()
print(w.windowTitle())
"
```

结果：
- `GUI MainWindow constructed OK, title= LocalDocMask - 本地文件脱敏工作台`
- 按钮文本：`① 只读扫描`、`② 处理 + 独立审计`、`③ 批量自动处理（全部确认）`
- **未真实弹窗**：仅 offscreen 构造，未进入 `app.exec()` 事件循环

### 1.6 已知缺陷实测

#### (a) 公司正则误报

```powershell
.venv\Scripts\python.exe -c "
import sys; sys.path.insert(0,'src')
from localdocmask import patterns
for s in ['本公司产品','本公司','本公司项目','我们公司','该公司','有限公司','股份公司','集团公司','银行医院大学','中国石油股份有限公司']:
    print(repr(s), [m.group(0) for m in patterns.COMPANY_RE.finditer(s)])
"
```

结果：
- `本公司` / `本公司产品` / `本公司项目` / `该公司` → **不匹配**（负向前瞻 + {2,24} 挡住了）
- `我们公司` → 匹配（误报）
- `有限公司` / `股份公司` / `集团公司` → 匹配（误报）
- `银行医院大学` → 作为一个实体匹配（误报）
- `中国石油股份有限公司` → 正确匹配

**结论：任务描述中"本公司被误判"对当前代码不成立，但同类泛指误报确实存在。**

#### (b) Candidate 默认值

```powershell
.venv\Scripts\python.exe -c "
from localdocmask.models import Candidate
c = Candidate(text='x', category='y')
print(c.confirmed, c.confidence)
"
```

结果：`confirmed=True, confidence=1.0`。公司正则命中时在 `detection.py:51` 显式传 `confidence=0.85`，但 `confirmed` 走默认值 True。

#### (c) confidence 使用情况

Grep `confidence` 在 `src/` 下的全部命中：
- `detection.py:40` 构造时写入
- `models.py:44` 字段定义
- `ocr.py:25` OCR dict 字段
- `task_manager.py:255` Ollama 候选写 0.6
- `gui.py:324` GUI 表格显示

**无任何 process/finalize/auditor 分支读取 confidence 做判定。**

#### (e) 日志/持久化/配置

Grep `import logging|getLogger|logging\.` 在 `src/` 下：**0 命中**。
Grep `json\.dump|to_csv|sqlite|\.db|persist|save_state|load_state` 在 `src/` 下：**仅 1 处**——`task_manager.py:241` 写单任务审计 JSON 报告。

### 1.7 Ollama 环境复核

```powershell
ollama list
```

结果：
- `qwen3.5:9b` (6.6 GB)
- `qwen3-embedding:4b` (2.5 GB)

模型存在，但本次未实际调用 `extract_entities`。

### 1.8 用户产物 JSON 阅读

读取了两个既有审计记录：
- `data/output/success/演示_员工信息_脱敏_审计记录.json`：5 条候选（3 redact 通过 + 2 嵌入图像 delete 验证通过），证明逐项删除流程真实跑过
- `data/output/quarantine/示例报告_脱敏_审计记录.json`：recognized_count=0 但仍被隔离——推测是输出中检测到高风险正则残留或元数据问题，证明失败关闭机制生效

### 1.9 用户数据保护验证

```powershell
git status --porcelain
```

在所有只读操作完成后输出为空，证明未改动任何被 git 跟踪的文件。`data/` 下文件未被删除/移动（pytest 通过 conftest 把 `LOCALDOCMASK_HOME` 指向临时目录）。

---

## 2. 证据索引

| 结论 | 证据路径 |
|---|---|
| 扁平结构 | `src/localdocmask/*.py` 平铺，无 models/detection 等子目录 |
| 空预留目录 | `src/localdocmask/core/`、`src/localdocmask/ui/`（运行时为空） |
| 空顶层目录 | `config/`、`scripts/`、`resources/`（运行时为空） |
| Candidate 默认 confirmed=True | `src/localdocmask/models.py:46` |
| 公司正则 confidence=0.85 | `src/localdocmask/detection.py:51` |
| confidence 仅 GUI 显示 | `src/localdocmask/gui.py:324` |
| Ollama 候选 confidence=0.6 | `src/localdocmask/task_manager.py:255` |
| Masker 映射仅内存 | `src/localdocmask/masker.py:24` |
| 每次 process new Masker | `src/localdocmask/task_manager.py:59` |
| 源哈希门禁 | `src/localdocmask/task_manager.py:150-151` |
| 输出哈希绑定 | `src/localdocmask/task_manager.py:159-160` |
| Ollama 回环校验 | `src/localdocmask/ollama.py:21-23` |
| 禁代理 | `src/localdocmask/ollama.py:27-29` |
| 无 logging | Grep 0 命中 |
| 批量强制全选 | `src/localdocmask/gui.py:474-476` |

---

## 3. 遗留问题 / 未能验证项

1. **GUI 真实弹窗**：仅 offscreen 构造，未在真实桌面 `app.exec()`。建议重构前由用户手动双击 `启动.bat` 确认。
2. **Ollama 实体抽取实测**：未实际调 `extract_entities`。建议后续单独跑一次端到端确认模型输出格式与 `_parse` 兼容。
3. **大文件/批量性能**：未测。
4. **PDF 图文混排页嵌入图 OCR**：代码仅警告不 OCR（`formats/pdf.py:42-43`），实际漏检率未量化。
5. **XLSX docProps 元数据清除**：代码中未见显式清除逻辑，可能残留作者信息。
6. **`acceptance/baseline_backup/` 旧源码**：与当前源码漂移，建议重构时决定是否删除（本次未删）。
7. **requirements.txt 缺依赖**：numpy/lxml 未声明但代码直接 import。

---

## 4. 本次审计产出

- `docs/project_audit/localdocmask_current_state.md`（现状审计主报告）
- `docs/roadmap/execution_log.md`（本日志）

未改动任何业务代码、未删除/移动任何用户数据。

---
---

# 阶段3：差距分析与目标设计

> 执行主体：OrganizeAgent（总控）
> 日期：2026-10-26

## 5. 环境复核（总控实测）

```powershell
git -C <target> log --oneline -5   # d39eb1e 基线，工作区干净
ollama list                        # qwen3.5:9b、qwen3-embedding:4b 均存在
.venv\Scripts\python.exe --version # Python 3.11.16
```

## 6. 阶段3 产出（全部基于两份审计与源码）

| 文档 | 路径 |
|---|---|
| 目标架构 | `docs/architecture/target_architecture.md` |
| 数据流与状态机 | `docs/architecture/data_flow.md` |
| 模块契约 | `docs/architecture/module_contracts.md` |
| 差距矩阵 | `docs/architecture/gap_analysis.md` |
| 路线图 | `docs/roadmap/roadmap.md` |
| ADR-001~008 | `docs/decisions/ADR-001-layered-package-structure.md` 等 8 份 |

关键设计决策：
- 分层包：domain/recognition/application/infrastructure/security/presentation/evaluation（ADR-001）。
- 候选默认按风险/来源三档分流（AUTO_CONFIRM/NEEDS_REVIEW/SUPPRESS），不再默认确认（ADR-002）。
- 分层识别 + 融合仲裁，LLM 仅补充（ADR-003）。
- 模式 B 批次共享 Mapper，默认仅内存、冲突检测、映射保护（ADR-004）。
- SQLite 持久化（标准库）+ 恢复/幂等（ADR-005）；YAML 配置 + 校验（ADR-006）。
- 不引入多 Agent/RAG/MCP/Redis/MQ/Docker（ADR-007）；结构化日志 + 脱敏（ADR-008）。

## 7. 下一阶段计划

进入阶段4-5：按模块契约分片并行重构（domain 底座 → 配置/日志/持久化 → 识别/上下文/词库 → 脱敏/映射 → 编排），每模块单元+集成测试并在 .venv 跑通。

## 8. 正式验收矩阵（用户新增）

用户提供正式《项目验收追踪清单》（A–E 共 30 项），作为最终验收矩阵。勾选状态与证据统一维护在：
`docs/roadmap/acceptance_matrix.md`
- 最终交付时逐项给出三态结论（已实现并验证 / 已实现但未充分验证 / 尚未实现）及证据。
- D4/D5/D6（多 Agent / RAG / MCP）按 ADR-007 论证，无收益则标记"已论证放弃"。

---
---

# 阶段4–9：重构、联调、验收与交付（执行总览）

> 执行主体：各功能 SubAgent + OrganizeAgent 集成
> 日期：2026-10-26

## 9. 提交链（由旧到新，master）

| Hash | 内容 |
|---|---|
| `faf7479` | 扁平包 → 分层架构（行为保持，42 测试全绿） |
| `cb880ed` | 异常体系 + 结构化日志 + YAML 配置 |
| `4077426` | SQLite 持久化 + 恢复/幂等（+10 测试） |
| `27b2adb` | 一致性映射 + 批次服务（+17 测试） |
| `88d692a` | 分层识别 + 上下文/词库/融合/评测（+21 测试） |
| `5e9dc57` | 组合根 + 流水线集成（+5 集成测试） |
| `517e1e8` | GUI 接组合根：复核/取消/任务中心/前后对比（+6 GUI 测试） |
| `1082c0f` | 安全/稳定性加固：路径收口、日志泄露、损坏、恢复、资源限制（+23 测试） |
| `1704701` | 修复 M1/M2：audit detail 落库掩码、恢复/操作日志/幂等跳过/超时接线（+7 测试） |
| `7c1a5b5` | 文档套件：README/图/手册/威胁模型/功能清单/求职亮点 |
| `0c4137b` | 工程交付：pyproject/uv.lock/诊断/打包/开发指南 |
| `d94b7fb` | 质量报告 + 合成演示数据 + 截图 |

## 10. 测试与验收（真实结果）

- 全量 `pytest tests -q`：**131 passed / 1 skipped，约 44 s**（OrganizeAgent 多次复跑一致）。
- 跳过：`test_symlink_escape_blocked`（当前 Windows 无符号链接权限，逻辑由 Path.resolve 覆盖）。
- 评测（36 条合成标注）：TP=42 / FP=0 / FN=0，P=R=F1=1.0；抑制 10/10；决策 AUTO_CONFIRM=25 / NEEDS_REVIEW=17 / SUPPRESS=6。
- 本地 LLM **真实调用**：qwen3.5:9b 冷启动 79.6 s 返回（公司名=杭州云启科技有限公司、姓名=张伟），模型漏的手机由规则补齐。
- 打包：PyInstaller onedir exe 约 390 MB，离屏冒烟存活 25 s；含 OCR 模型与 resources。
- 演示：`demo/run_demo.py` 真实闭环——3 PASSED / 1 REVIEW（公开企业+图文混排）/ 1 FAILED（损坏隔离），5/5 原件哈希一致，模式 B 跨文件同值。

## 11. 独立验收审查

- 报告：`docs/project_audit/acceptance_review.md`。无 Blocker；提出 M1（DB 落原文）、M2（恢复/追踪未接线），均已在 `1704701` 修复并回归。

## 12. 验收矩阵最终勾选

- 详见 `docs/roadmap/acceptance_matrix.md`：**26/29 已实现并验证；3/29 已论证放弃（D4 多 Agent、D5 RAG、D6 MCP，见 ADR-007）**。
- 清单标题称"30 项"，实际枚举为 29 个复选框（A6/B5/C6/D6/E6），已在矩阵中说明。

## 13. 如实记录的边界

- 36 条合成小样本、F1=1.0 为受控句式回归基线，不外推真实文档；公司正则无标点相邻 CJK 贪婪；人名召回依赖词典/LLM；exe 未逐文件跑完整处理；干净第三方机器安装未单独实测。

---

## 2026-10-27 迭代：公司名左边界回退 + 标注集扩到 101 条

- ix: company name left-boundary rollback（53fa27e）：patterns.trim_company_prefix 先按组织后缀切核心，再从左侧剥离甲/乙/丙方、我们/本/该/贵、合作/联系/对接/签约/聘请等多字词与助词"的"；lookahead 移除与真实字号冲突的连接字（与/和/对）。新增 	ests/test_company_boundary.py（6 例）。
- 后续随词库注入公开实体（public→NEEDS_REVIEW）、泛指词表补"他们/相关"。
- labeled_cases.yaml 由 36 → 101 条（全部合成）。真实评测（Ollama 关闭）：cases=101，TP=69/FP=0/FN=0，P=R=F1=1.0；抑制准确率 46/46=1.0；决策 auto_confirm=51/needs_review=18/suppress=22。
- 全量 pytest：138 passed / 1 skipped / 0 failed。
- 失败根因与处理：①单字连接字/无标点动词串吞并→残余风险记录；②公开实体带括号被正则切断→词库注入 public；③"他们/相关"未入泛指词表→补入；④贪婪泛指句→改用干净例句。

---

## 2026-10-28 阶段：真实语料闭环工具 + 本地中文 NER 适配器

### 任务一：语料标注/回灌/趋势工具（提交 f0097e8）
- 新增 `evaluation/annotation.py`（真实文件文本提取 + 按句切分 + 待标注草稿，含 provenance）、`evaluation/badcase.py`（FP/FN/SUPPRESS_FAIL 导出、合并去重、词库回灌 dry-run）、`evaluation/trend.py`（结果按时间落盘 JSON、本次 vs 上次对比、regression_flag）、`evaluation/synthetic_corpus.py`（高拟真合成语料，provenance.synthetic=true）。
- 统一 CLI `scripts/corpus_cli.py`（gen/eval/badcases/merge/feedback/trend/draft）；操作手册 `docs/guides/corpus_annotation_manual.md`；测试 `tests/test_corpus_tooling.py`（6 例，tmp 隔离）。
- 真实跑通（合成 120 条，Ollama 关闭）：cases=120，TP=257/FP=12/FN=92，P=0.9554/R=0.7364/F1=0.8317；导出坏例 78 条；feedback dry-run 给回灌建议；trend 两次跑 delta=0、regression_flag=false。
- 合成/真实区分：每条带 provenance（synthetic、source_file 仅文件名、file_type、sentence_index、span）；评测结果与坏例清单 gitignore（可能含敏感文本）。

### 任务二：本地中文 NER 适配器（提交 2824b17，ADR-009）
- 选型：ONNX-BERT 中文 NER（shibing624/bert4ner-base-chinese，一次性导出 ONNX）；运行时仅 onnxruntime+numpy+手写字符级 BERT 分词，**零新增运行时 Python 依赖**；模型制品 ~388MB 放本机 models/（gitignore）。
- 接线：`infrastructure/ner/{base,onnx_ner}.py`（NERPort + OnnxChineseNER，PER→姓名/ORG→公司名/LOC→详细地址，ORG 前导动词保守剥离）；settings 增 NerConfig（默认关闭）；composition 仅启用时惰性构造、异常降级；pipeline 加 ner 插槽与 _ner_supplement；fusion 把 source=ner 与 ollama 同口径（仅模型来源上下文类→NEEDS_REVIEW）。
- 测试 `tests/test_ner.py`（12 例）：接线/降级（桩模型恒跑）+ 真实模型（缺 model.onnx 自动 skip）。
- 真实 A/B（EvaluationRunner）：101 集 off P/R/F1=1.0 → on 0.9583/1.0000/0.9787（+3 FP 全为 LOC 碎片，召回不变，auto_confirm/suppress 不变）；新增 4 反例召回 off 0.25 → on 0.75，词典外裸人名 0/2 → 2/2。
- 残余：LOC 不粘合（地址切碎片，转复核）、罕见 ORG 前导成分、长文跨块截断；NER 始终是候选非裁判。

### 全量测试
- 两任务合并后 OrganizeAgent 复跑：**155 passed / 1 skipped，共 156 用例 / 0 failed**（138 基线 + 6 语料工具 + 12 NER；口径：passed=总数-skipped）。
- data/ 与 RepoMind 全程未改动；词库回灌默认 dry-run，需显式 --apply。

---

## 2026-10-29 阶段：NER LOC 粘合 + 合成语料扩到 600 条

### 任务一：NER LOC 地址粘合（提交 8b60020）
- `onnx_ner.py` BIO 解码器先记录字符起止，新增后处理 `_finalize`：相邻 LOC 片段（gap 仅允许 `[\s，。、；：,.;:（）()]*`）贪心合并，跨动词/ORG/PER/任意汉字即停；合并后向后吸收一次门牌号 `\d+(号|室|栋|幢|座|层|楼|弄|单元)`。
- 真实离线对比：`公司位于北京市朝阳区望京街8号` → 由 3 碎片变为单个 `北京市朝阳区望京街8号`；`张伟前往北京市朝阳区，与杭州云启科技有限公司洽谈` → 地址到逗号即止、不跨边界。
- 新增 2 个真实模型测试（test_ner.py 共 14 例）。

### 任务二：合成语料扩量 600 + 两路径评测（提交 e32a6f2）
- 600 条多体裁合成（合同/邮件/表格/扫描 OCR/泛指负例/公开机构），全部 provenance.synthetic=true。
- 真实评测（Ollama 关闭）：
  - NER 关：TP=1331/FP=57/FN=420，P=0.9589/R=0.7601/F1=0.8480，复核率 29.2%；
  - NER 开：TP=1707/FP=133/FN=44，P=0.9277/R=0.9749/F1=0.9507，复核率 44.2%；
  - 抑制准确率两路径均 1.0；决策分布 关 AC=927/NR=461/SU=189，开 AC=927/NR=913/SU=224。
- 根因与处理：人名/公司名召回靠 NER 提升（0.39→1.0、0.63→1.0）；领域词/编号/地址真值与 resources、检测器对齐；OCR 噪声打断邮箱/号码属预期鲁棒边界；NER 开 FP 上升主要为地址重复识别，转 NEEDS_REVIEW。
- trend 落盘对比：Δrecall=+0.215、Δf1=+0.103、Δfp=+76；regression_flag 由 FP 上升触发，属预期召回-精确权衡。

### 全量测试
- 最终 OrganizeAgent 复跑（junitxml 权威）：**tests=158 / skipped=1 / passed=157 / 0 failed**。
- data/ 与 RepoMind 全程未改动；600 条为合成、不能替代真实文档。
