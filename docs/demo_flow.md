# LocalDocMask 演示流程（照着走）

本脚本对应 `docs/screenshots/` 下 8 张 offscreen 截图。每条步骤给出操作、预期看到、讲解要点。
前置：在项目根目录 `<PROJECT_ROOT>` 下，已运行
`.venv\Scripts\python.exe demo\make_demo_data.py` 生成 `demo/data/`。

## 0. 两种入口

- **headless（推荐先跑，验证数字）**：
  ```powershell
  .venv\Scripts\python.exe demo\run_demo.py
  ```
  完整输出同时写入 `demo/demo_output.txt`。预期：3 个 PASSED、1 个 NEEDS_REVIEW、1 个 AUDIT_FAILED、原件哈希全一致、模式 B 跨文件一致。
- **GUI（演示用）**：
  ```powershell
  .venv\Scripts\python.exe main.py
  ```
  或重放 offscreen 截图：`.venv\Scripts\python.exe demo\make_screenshots.py`。

---

## 1. 主界面：三档候选表（对应 `01_main_window_candidates.png`）

**操作**：添加 `demo/data/业务说明.docx`，点「① 只读扫描（三档分流）」。

**预期看到**：
- 右上「敏感候选」表格 8 行：
  - 绿色行（自动确认）：邮箱、身份证号、银行卡号、手机号×2、公司名（杭州云启…）、自定义敏感词（项目代号-苍梧）；
  - 灰色行（已抑制）：公司名「我们公司」（泛指引用）。
- 列：包含勾选框 / 决策 / 风险 / 类别 / 原文（脱敏显示）/ 替换值 / 位置（第 N 段、表格第 X 行 Y 列）/ 来源 / 置信度。

**讲解要点**：
- 高置信规则实体（手机/身份证/银行卡/邮箱）→ 自动勾选、自动确认；
- 黑名单公司 + 领域词 → 自动确认；
- 泛指「我们公司」「本公司产品」→ 已抑制，不脱敏；
- 这是"误报治理"第一关：白名单 + 上下文 + 词库在源头把通用业务表述压掉。

---

## 2. 脱敏前后对比（对应 `02_preview_before_after.png`）

**操作**：切到「模式 B：一致性虚构替换（公司/人名）」，点「刷新预览」。

**预期看到**：底部「脱敏预览（原文 | 脱敏后）」左右两栏：
- 左栏原文保留：`杭州云启科技有限公司`、`张伟`、`13812345678`；
- 右栏脱敏后：`杭州云启科技有限公司` → `云帆科技有限责任公司`；`张伟` → 虚构人名；手机/身份证/银行卡 → `****`。

**讲解要点**：
- 模式 A：所有类别统一 `****`；
- 模式 B：公司/人名替换为稳定虚构值，数字类仍走掩码；
- 同一实体在同一批次的不同文件中替换为同一虚构值（见步骤 5）。

---

## 3. 失败关闭：复核门禁阻断（对应 `03_audit_report_gate_blocked.png`）

**操作**：
1. 添加 `demo/data/合同材料.pdf`；
2. 扫描；
3. **不要**勾选「中国石油股份有限公司」那一行（它默认是黄色待复核、不勾选）；
4. 直接点「② 处理 + 独立审计（复核门禁）」。

**预期看到**：
- 候选表第二行仍为黄色「待复核」；
- 切到「独立审计报告」tab：
  - 审计结论：**不通过**；
  - 发现残留：输出文本中发现残留：中国石油股份有限公司；
  - 输出文件落在 `…\output\quarantine\合同材料_脱敏.pdf`（隔离目录）；
  - 提示：「未发现残留」不等于绝对不存在敏感信息，请人工复核。

**讲解要点**：
- 这就是**失败关闭**：只要有任何一条待复核项未裁决，整文件不导出到成功目录，
  哪怕其他敏感项已经脱敏；
- PDF 第 2 页图文混排也会触发 `review_flag`（headless 日志里可见
  「第 2 页为图文混排，嵌入图像内容需人工复核」），同样会阻断导出。

---

## 4. 批量自动安全处理（对应 `04_success_table.png`、`05_review_failed_table.png`）

**操作**：清空列表，一次性添加 5 个 demo 文件，点「③ 批量自动安全处理（BatchService）」。

**预期看到**（headless 与 GUI 一致）：
- 「成功结果」tab：`业务说明.docx` — 7 识别 / 7 脱敏 / 0 删除 / 审计通过 / 哈希前 8 位；
- 「复核/失败」tab 两行：
  - `合同材料.pdf` — 等待人工复核 — 1 项待复核；
  - `损坏样本.docx` — 审计失败 — `ValueError: DOCX 文件损坏…`；
- 「隔离文件」tab：本批为空（损坏样本在 scan 阶段即抛错，没有产物需要隔离；
  步骤 3 中被门禁拦下的 PDF 在上一轮交互中已落入隔离目录）。

**讲解要点**：
- **失败隔离**：损坏样本抛异常，不影响同批次 docx/xlsx/png 成功导出；
- **自动失败关闭**：含待复核项的 PDF 不导出；
- **原件不变**：headless 日志打印 5/5 源文件 SHA-256 一致。

---

## 5. 模式 B 跨文件一致性（headless 验证）

**操作**：看 `demo/demo_output.txt` 阶段 4。

**预期看到**：
- 原文 `杭州云启科技有限公司` 在 DOCX 输出与 XLSX 输出中均无残留；
- 同一虚构值 `云帆科技有限责任公司` 同时出现在 DOCX 与 XLSX 输出中；
- 跨文件一致性：一致 ✓。

**讲解要点**：批次共享同一个 `ConsistencyMapper`，同一真实实体在该批次所有文件中
映射为同一虚构值；`mapping_persistence=memory` 表示进程结束映射即销毁，不落盘。

---

## 6. 任务中心/历史（对应 `07_task_center_history.png`）

**操作**：切到「任务中心/历史」tab，点「刷新历史」。

**预期看到**：多行历史，含时间、文件名、状态、模式、识别/脱敏/删除计数、输出路径。
`prepare` 阶段就失败的损坏文件不入库（避免污染审计链）；扫描过但未处理的文件也会留痕。

---

## 7. 收尾

- 点「打开成功目录」查看 PASSED 产物；
- 点「清理临时文件」清理 `data/processing/`；
- headless 用户直接看 `demo/demo_output.txt` 即可复现全部数字。

## 截图清单（`docs/screenshots/`）

| 文件 | 内容 |
| --- | --- |
| `01_main_window_candidates.png` | 主界面三档候选表（8 行，绿/灰） |
| `02_preview_before_after.png` | 模式 B 脱敏前后左右对比 |
| `03_audit_report_gate_blocked.png` | 复核门禁阻断：审计不通过 + 残留 + 隔离路径 |
| `04_success_table.png` | 成功结果表（业务说明.docx，7/7/0） |
| `05_review_failed_table.png` | 复核/失败表（PDF 待复核 + 损坏样本审计失败） |
| `06_quarantine_table.png` | 隔离文件表（本批为空，如实呈现） |
| `07_task_center_history.png` | 任务中心历史（4 行落库记录） |
| `08_main_window_final.png` | 回到主界面最终态 |

> 全部截图均为 `QT_QPA_PLATFORM=offscreen` 渲染，已通过
> `QFontDatabase.addApplicationFont("C:\Windows\Fonts\msyh.ttc")` 注册微软雅黑，
> 中文正常显示；offscreen 不合成 GPU 加速效果，但表格/文本/颜色/勾选态均与真实界面一致。
