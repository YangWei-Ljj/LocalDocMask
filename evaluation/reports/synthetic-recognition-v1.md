# LocalDocMask 评测门禁报告

> 回归基线，不代表真实生产精度。合成语料结果不得对外表述为真实生产精度。

- suite: `synthetic-recognition-v1`
- suite_version: `1.0.0`
- suite_hash: `c953475daae86efe743cb4af8deb93617c5059417b5e329cd419366f13c3fa91`
- execution_status: `completed`
- quality_status: `passed`
- gate_status: `passed`
- sample_count: `101`
- synthetic: `true`

## 总体指标

| Precision | Recall | F1 | TP | FP | FN |
|---:|---:|---:|---:|---:|---:|
| 1.0 | 1.0 | 1.0 | 69 | 0 | 0 |

- suppress_accuracy: `1.0`
- review_rate: `0.2609`

## 门禁结论

- 无质量回归或完整性失败。

## 失败样本

- 无。

## 脱敏边界

本报告仅包含聚合指标、样本 ID、格式和失败码；不包含文档正文、候选原文、真实姓名、手机号、映射表或密钥。
