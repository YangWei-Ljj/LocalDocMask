# 真实语料标注与评测闭环操作手册

> 目标：在**本地、不外发**的前提下，建立"真实文件 → 标注 → 评测 → 坏例回灌 → 趋势回归"的可重复闭环。
> 适用版本：master（corpus tooling）。所有命令在项目根目录、`.venv` 激活后执行。

## 0. 安全与隐私红线（先读）

1. **本地优先、不外发**：解析/OCR/LLM 全部走本机；Ollama 仅监听 `127.0.0.1`，禁代理。不要把真实文件上传到任何外部服务。
2. **环境隔离**：真实文件处理建议用 `LOCALDOCMASK_HOME=<临时目录>` 指向隔离工作区，避免误写默认库。
   测试用例全部在 `tmp_path` 隔离，不触碰 `data/`、`resources/`、真实用户文件。
3. **结果可能含敏感文本**：`evaluation/results/*.json` 与 `badcases.yaml` 可能包含原文片段，**默认不入库**，请自行加入 `.gitignore` 或存放于本地数据目录。
4. **合成 ≠ 真实**：`gen` 产出的语料全部为虚构实体，标注 `synthetic: true`，只能用于回归，**不能替代真实语料**，也不能据此对外宣称真实精度。
5. **词库回灌默认 dry-run**：`feedback`/`merge` 只预览，必须显式 `--apply` 才追加写盘，且只追加不覆盖。回灌前人工确认 `root_cause`。

## 1. 标注格式（两种）

### 1.1 已标注用例（`labeled_cases.yaml` 结构）
```yaml
cases:
  - text: "供应商：星轨数据服务（示例）有限公司。"
    true_entities: [["公司名", "星轨数据服务（示例）有限公司"]]
    note: "黑名单公司"
```
- 负例（应抑制）：`true_entities: []`。

### 1.2 待标注草稿（draft-v1，面向真实文件，带 provenance）
```yaml
cases:
  - text: "甲方杭州云启科技有限公司今日签约。"
    expected: [["公司名", "杭州云启科技有限公司"]]   # 人工填
    expected_decision: "auto_confirm"              # auto_confirm/needs_review/suppress，可空
    provenance: {source_file: contract.docx, synthetic: false, file_type: docx,
                 sentence_index: 3, span: [120, 150]}
```
- `provenance.synthetic` 区分合成/真实；`source_file` 只存文件名，不存本机绝对路径。

## 2. 三档真值口径

| 真值 | 含义 | 说明 |
| --- | --- | --- |
| `auto_confirm` | 系统可自动脱敏的高置信敏感实体 | 身份证/银行卡/Luhn 校验通过、黑名单命中 |
| `needs_review` | 不确定安全、必须人工复核 | 公开机构名、上下文分歧、低置信 |
| `suppress` | 泛指/白名单/普通业务描述，不脱敏 | 我们公司/相关产品/有限公司/银行医院 |

> 原则：**不能确定安全就转人工复核，不自动导出**（ADR-002）。

## 3. 命令一览（统一 CLI：`scripts/corpus_cli.py`）

| 步骤 | 命令 |
| --- | --- |
| 从真实文件生成待标注草稿 | `python scripts/corpus_cli.py draft --file contract.docx --out draft.yaml --real` |
| 批量生成高拟真合成语料 | `python scripts/corpus_cli.py gen --n 120 --out eval_cases.json` |
| 评测并落盘结果 | `python scripts/corpus_cli.py eval --data eval_cases.json --results evaluation/results --note "说明"` |
| 导出失败用例 | `python scripts/corpus_cli.py badcases --data eval_cases.json --out badcases.yaml` |
| 合并坏例进回归集（先预览） | `python scripts/corpus_cli.py merge --bad badcases.yaml --target regression.yaml` |
| 合并坏例（确认后写盘） | 加 `--apply` |
| 词库回灌建议（先预览） | `python scripts/corpus_cli.py feedback --bad badcases.yaml --resources resources` |
| 词库回灌（确认后写盘） | 加 `--apply` |
| 趋势对比（本次 vs 上次） | `python scripts/corpus_cli.py trend --results evaluation/results` |

## 4. 坏例类型与处理

- `FP`：活跃候选未命中真值 → 误报。检查是否应进 whitelist 或为泛指。
- `FN`：真值未被识别 → 漏报。检查正则/词库/上下文；确定性可修的修，否则转复核。
- `SUPPRESS_FAIL`：应抑制负例却输出活跃 → 上下文泛指判定缺口。

## 5. 推荐工作流

1. `draft --real` 从真实文件出草稿 → 人工填 `expected`/`expected_decision`。
2. `eval` 跑指标，`badcases` 导出失败。
3. 人工在 `badcases.yaml` 填 `root_cause`。
4. `feedback`（预览）→ 确认 → `--apply` 回灌词库；`merge`（预览）→ `--apply` 合并坏例。
5. 再跑 `eval`，用 `trend` 对比，`regression_flag=true` 即"改 A 坏 B"，必须解释。

## 6. 残余

- 真实语料仍需用户在本地采集；本工具不替代人工标注。
- 词库回灌的 whitelist 建议是启发式（FP 候选全列），必须人工甄别，不要直接 `--apply` 批量放行号码。
