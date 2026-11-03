# LocalDocMask 发布与运行手册

## 1. 当前状态唯一来源

**当前候选发布状态的唯一来源：** `dist/t9-evidence/release-summary.json`

README、验收矩阵和最终报告不得维护第二套实时状态。正式发布要求：

- 当前工作区干净；
- `build-info.git_dirty=false`；
- 构建提交等于当前 HEAD；
- EXE SHA-256 与 build evidence 和外部验证一致；
- 可重复构建 identity、EXE、checksums 全部一致；
- package E2E 通过；
- T8 评测门禁通过；
- 健康检查通过；
- ACL 和数据库演练通过；
- 三项外部检查均为布尔 true。

## 2. 固定环境

- Windows 10/11 x64
- Python 3.11.16
- PyInstaller 6.22.3
- 数据库 schema v4
- 配置 schema v1
- PyInstaller onedir，无安装器或代码签名

## 3. 构建

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_package.ps1 -VerifyReproducible
```

构建流程：

1. 在清理 dist/build 前执行 clean-worktree 检查。
2. 只清理 `dist/` 和 `build/pyinstaller`，不得递归清空整个 `build/`。
3. 保留 `build/release-baselines/**`、历史 evidence 和 `build/t9-old-binary-rollback/**`。
4. 执行两次 PyInstaller 构建并比较 identity、EXE SHA-256、checksums 内容哈希。
5. 执行隔离启动 smoke。
6. 执行打包后 E2E：DOCX、XLSX、PNG OCR、失败 quarantine。
7. 生成 `dist/t9-evidence/package-e2e.json`。
8. 仅 E2E 成功后生成 `status=passed` 的 `dist/t9-evidence/build.json`。
9. 生成运行时依赖闭包 SBOM 与 checksums。

任一构建、smoke、E2E 或可重复性检查失败，不得输出 `BUILD_OK`。

## 4. 外部验证

默认文件：

`docs/operations/t9-evidence/external-validation-status.json`

必须包含：

- 当前完整 Git commit；
- 当前 EXE SHA-256；
- `clean_windows_vm_verified=true`；
- `other_user_logon_verified=true`；
- `old_binary_rollback_verified=true`；
- 三项 evidence 相对路径且文件真实存在。

文件缺失、JSON 无效、候选或 EXE 不匹配、检查值非布尔 true、evidence 路径缺失时，release summary 必须为 `failed`。

## 5. 发布验收

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\release_acceptance.ps1
```

`-SkipFullTests` 和 `-SkipBuild` 只用于负向门禁测试，不得形成正式通过。

## 6. 健康、ACL 与数据库

- 健康检查接受 `healthy` 或 `degraded`，`unhealthy` 阻止发布。
- ACL 验证当前用户、SYSTEM、Administrators，并执行回滚。
- 数据库演练只报告 database backup/restore 事实，不代表旧版二进制回滚。
- 旧版二进制回滚只能由外部验证文件证明。

## 7. 运行

1. 复制整个 `dist/LocalDocMask` 目录。
2. 双击 `LocalDocMask.exe`。
3. 首次启动建立 data、数据库、日志、指标、success 和 quarantine。
4. 本地模型不可用时健康状态 degraded，但不得把自动人名识别称为正常可用。
5. 关闭后重启可读取已有数据库与历史任务。

## 8. 故障处置

- 工作区不干净：停止构建，不删除 dist/build。
- package E2E 失败：保留 `package-e2e.json`，禁止发布。
- 外部证据过期：停止正式发布，不得修改 evidence JSON 伪造通过。
- 事件链异常：健康状态 unhealthy，禁止导出。
- 清理失败：保持失败关闭并保留 quarantine，不得自动删除。
- 诊断与日志只保存在本地，不上传。

## 9. 明确未做

- 不部署、不推送、不进入 T10。
- 不包含代码签名、安装器、自动更新。
- 不包含多用户认证、RBAC、OIDC、局域网或云上传。
