# 版本化评测与发布门禁

LocalDocMask 的评测门禁全部在本机运行，不访问网络、不上传语料或报告。当前固定套件为
`synthetic-recognition-v1`，全部样本均为合成文本。

> 合成语料结果只用于回归基线，不代表真实生产精度。

## 目录与完整性

```text
evaluation/
  suites/synthetic-recognition-v1/
    suite.json
    cases.yaml
  baselines/synthetic-recognition-v1.baseline.json
  reports/
```

`suite.json` 固定记录套件 ID、版本、样本相对路径、样本 SHA-256、样本及类别计数、来源说明、
`synthetic` 标记、兼容版本和 `suite_hash`。运行时重新计算样本哈希与套件哈希，任一项不一致
都会失败关闭。

## 状态语义

- `execution_status=completed`：完整性和识别执行均完成。
- `execution_status=failed`：完整性、环境或执行失败。
- `quality_status=passed/failed`：执行完成并与基线比较后的质量结论。
- `quality_status=not_evaluated`：尚未执行、执行失败或只做 dry-run。
- `gate_status=passed/failed`：发布门禁最终结论。

执行失败时不得出现 `quality_status=passed`。

## 命令

列出现有套件：

```powershell
.venv\Scripts\python.exe scripts\eval_gate.py --list-suites
```

只检查套件完整性，不加载识别模型：

```powershell
.venv\Scripts\python.exe scripts\eval_gate.py `
  --suite synthetic-recognition-v1 `
  --report evaluation\reports\dry-run.json `
  --dry-run
```

执行评测与发布门禁：

```powershell
.venv\Scripts\python.exe scripts\eval_gate.py `
  --suite synthetic-recognition-v1 `
  --baseline evaluation\baselines\synthetic-recognition-v1.baseline.json `
  --report evaluation\reports\synthetic-recognition-v1.json `
  --markdown-report evaluation\reports\synthetic-recognition-v1.md
```

退出码：`0` 表示门禁通过；`1` 表示完整性、执行或质量门禁失败；`2` 表示参数、基线、
报告写入等运行错误。

## 回归门禁

默认直接失败条件：

- 套件或样本 SHA-256 不一致。
- `missing_sources` 非空。
- 评测执行异常或未完成。
- 安全关键样本失败。
- 总 Recall、F1 或抑制准确率绝对下降超过 `0.01`。
- 基线声明的 P0 类别 F1 下降超过容忍度。
- 工具版本或策略版本不在套件兼容范围内。

## 基线更新

基线不会自动更新。必须显式给出原因：

```powershell
.venv\Scripts\python.exe scripts\eval_gate.py `
  --suite synthetic-recognition-v1 `
  --baseline evaluation\baselines\synthetic-recognition-v1.baseline.json `
  --report evaluation\reports\baseline-update.json `
  --update-baseline `
  --reason "说明指标变化原因和评审结论"
```

更新前必须已完整执行并满足套件完整性要求且不存在安全关键失败。旧基线会保存在
`evaluation/baselines/history/`，同时生成更新审计记录。该机制不自动容忍安全关键失败。

## 报告脱敏边界

JSON/Markdown 报告只包含聚合指标、类别、文件格式、识别路径、样本 ID 和失败码；不包含
文档正文、候选原文、真实姓名、手机号、映射表或密钥。操作者不得把合成语料指标宣传为
真实生产精度。
