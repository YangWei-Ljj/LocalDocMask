# LocalDocMask 当前候选验收报告

## 状态来源

- 当前候选构建证据：`dist/t9-evidence/build.json`
- **当前发布状态唯一来源：** `dist/t9-evidence/release-summary.json`

本报告不复制实时 pass/fail 状态。旧版“全部通过”报告仅保留在 Git 历史中，不参与当前验收。

## 工程门禁

- clean worktree：必须通过
- 可重复构建：identity、EXE、checksums 必须全部一致
- package E2E：DOCX、XLSX、PNG OCR、失败 quarantine 必须全部通过
- T8 固定评测门禁：必须通过
- 健康检查：healthy 或明确 degraded
- ACL 和数据库演练：必须通过
- SBOM：只允许 pyproject 运行时依赖闭包

## 外部验证

以下三项必须来自真实外部 evidence：

- 独立干净 Windows VM 验证
- 独立普通用户登录 ACL 验证
- 旧版二进制回滚验证

当前状态为 `pending_external_validation`，`production_release=false`。三项均为布尔 true、候选和 EXE SHA 匹配前，不得正式发布。

## 发布边界

- 不部署
- 不推送
- 不进入 T10
- 不使用合成评测冒充真实生产精度
