# Windows 数据目录 ACL 手册

## 目标权限

- 当前 Windows 用户：完全控制，继承到子目录和文件。
- `NT AUTHORITY\SYSTEM`：完全控制。
- `BUILTIN\Administrators`：完全控制。
- 移除 `Everyone`、`BUILTIN\Users`、`Authenticated Users` 的允许访问。

## 预演

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts\harden_data_acl.ps1 `
  -DataDir C:\Controlled\LocalDocMask `
  -BackupAclFile C:\Controlled\acl-backup.acl `
  -DryRun
```

Dry-run 只打印命令，不修改 ACL。

## 实际应用

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts\harden_data_acl.ps1 `
  -DataDir C:\Controlled\LocalDocMask `
  -BackupAclFile C:\Controlled\acl-backup.acl `
  -EvidenceFile dist\t9-evidence\acl-apply.json
```

脚本先递归授予当前用户、SYSTEM、管理员显式权限，再移除继承和宽泛组，避免处理中途锁定当前用户。随后验证根目录、既有子项和新文件继承，并用 JSON 保存证据。

## 回滚

若当前会话具备恢复备份所需特权：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts\harden_data_acl.ps1 `
  -DataDir C:\Controlled\LocalDocMask `
  -Rollback `
  -BackupAclFile C:\Controlled\acl-backup.acl
```

如果 `icacls /restore` 因未提升特权失败，而原目录权限完全来自父目录继承，可显式使用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts\harden_data_acl.ps1 `
  -DataDir C:\Controlled\LocalDocMask `
  -Rollback -ResetToInherited `
  -BackupAclFile C:\Controlled\acl-backup.acl
```

该模式会移除显式加固 ACE 并恢复父目录继承。若原目录包含无法重建的独立显式 ACL，必须提升权限后从备份恢复。

## 验证边界

ACL 脚本可验证 DACL 主体、根目录保护标志、既有子项和新文件继承。真实“其他普通用户登录后访问被拒绝”仍需要独立测试账号和凭据；缺失时证据必须标记 `other_user_logon_verified=false`，不得写成已验证。
