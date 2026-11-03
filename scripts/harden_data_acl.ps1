<#
.SYNOPSIS
  Apply, verify, or roll back a restricted LocalDocMask data-directory ACL.

.DESCRIPTION
  Single-user Windows desktop hardening:
  - preserves SYSTEM and local Administrators;
  - grants the current interactive user full control;
  - removes broad Users / Everyone / Authenticated Users access;
  - verifies inherited ACLs on a newly created probe file;
  - records machine-readable evidence;
  - never deletes application data.

  Actual apply requires -BackupAclFile. Rollback restores the saved ACL when
  that file is supplied, then re-enables inheritance. A real second-user logon
  cannot be performed unless a separate test account credential is supplied.
#>
param(
    [string]$DataDir = "",
    [switch]$DryRun,
    [switch]$Rollback,
    [switch]$ResetToInherited,
    [string]$BackupAclFile = "",
    [string]$EvidenceFile = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$commands = [System.Collections.Generic.List[string]]::new()
$commandOutput = [System.Collections.Generic.List[string]]::new()

if ([string]::IsNullOrWhiteSpace($DataDir)) {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALDOCMASK_HOME)) {
        $DataDir = $env:LOCALDOCMASK_HOME
    } else {
        $DataDir = Join-Path $ProjectRoot "data"
    }
}
$raw = [string]$DataDir
if ($raw.StartsWith("\\") -or $raw.StartsWith("//") -or $raw -match '^\\\\\?\\') {
    throw "拒绝 UNC/设备数据目录：$DataDir"
}
$full = [System.IO.Path]::GetFullPath($raw)
$driveRoot = [System.IO.Path]::GetPathRoot($full)
if ($full.TrimEnd('\', '/') -eq $driveRoot.TrimEnd('\', '/')) {
    throw "拒绝将文件系统根目录作为数据目录：$full"
}
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$actorSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ([string]::IsNullOrWhiteSpace($identity) -or [string]::IsNullOrWhiteSpace($actorSid)) {
    throw "无法获取当前 Windows 用户身份"
}

function Invoke-Icacls {
    param([string[]]$Arguments, [switch]$AllowFailure)
    $display = "icacls.exe " + (($Arguments | ForEach-Object { '"' + $_ + '"' }) -join " ")
    $commands.Add($display)
    [Console]::WriteLine($display)
    if ($DryRun) { return 0 }
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & icacls.exe @Arguments 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    foreach ($line in $output) {
        $text = [string]$line
        $commandOutput.Add($text)
        Write-Host $text
    }
    if ($code -ne 0 -and -not $AllowFailure) { throw "icacls 执行失败，退出码 $code" }
    return $code
}

function Get-AclSnapshot {
    param([string]$Path)
    $acl = Get-Acl -LiteralPath $Path
    $rules = @()
    foreach ($rule in $acl.Access) {
        $sid = ""
        try { $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }
        catch { $sid = [string]$rule.IdentityReference }
        $rules += [ordered]@{
            identity = [string]$rule.IdentityReference
            sid = $sid
            access_type = [string]$rule.AccessControlType
            rights = [string]$rule.FileSystemRights
            inherited = [bool]$rule.IsInherited
            inheritance_flags = [string]$rule.InheritanceFlags
            propagation_flags = [string]$rule.PropagationFlags
        }
    }
    return [ordered]@{
        path = $Path
        owner = [string]$acl.Owner
        inheritance_protected = [bool]$acl.AreAccessRulesProtected
        rules = @($rules)
    }
}

function Test-HardenedAcl {
    param([object]$Snapshot, [switch]$RequireProtected)
    $allowRules = @($Snapshot.rules | Where-Object { $_.access_type -eq "Allow" })
    $allowed = @($actorSid, "S-1-5-18", "S-1-5-32-544")
    $forbidden = @("S-1-1-0", "S-1-5-32-545", "S-1-5-11")
    $allowSids = @($allowRules | ForEach-Object { $_.sid } | Sort-Object -Unique)
    $forbiddenPresent = @($allowSids | Where-Object { $_ -in $forbidden })
    $unexpected = @($allowSids | Where-Object { $_ -notin $allowed })
    $missing = @($allowed | Where-Object { $_ -notin $allowSids })
    $protectedOk = (-not $RequireProtected) -or [bool]$Snapshot.inheritance_protected
    return [ordered]@{
        passed = [bool]($protectedOk -and
            $forbiddenPresent.Count -eq 0 -and $unexpected.Count -eq 0 -and $missing.Count -eq 0)
        inheritance_protected = [bool]$Snapshot.inheritance_protected
        allowed_sids = $allowSids
        forbidden_sids_present = $forbiddenPresent
        unexpected_allow_sids = $unexpected
        missing_required_sids = $missing
    }
}

function Write-Evidence {
    param([hashtable]$Payload)
    if ([string]::IsNullOrWhiteSpace($EvidenceFile)) { return }
    $target = [System.IO.Path]::GetFullPath($EvidenceFile)
    $parent = Split-Path -Parent $target
    if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    $Payload["commands"] = @($commands)
    $Payload["icacls_output"] = @($commandOutput)
    $json = $Payload | ConvertTo-Json -Depth 12
    Set-Content -LiteralPath $target -Value $json -Encoding utf8
}

if (-not $Rollback -and -not $DryRun -and [string]::IsNullOrWhiteSpace($BackupAclFile)) {
    throw "实际应用 ACL 前必须提供 -BackupAclFile"
}
if ($Rollback -and -not (Test-Path -LiteralPath $full -PathType Container)) {
    throw "数据目录不存在，无法回滚：$full"
}

if ($Rollback) {
    $restoreCode = -1
    if (-not [string]::IsNullOrWhiteSpace($BackupAclFile)) {
        $backupFull = [System.IO.Path]::GetFullPath($BackupAclFile)
        if (-not (Test-Path -LiteralPath $backupFull -PathType Leaf)) {
            throw "ACL 备份文件不存在，拒绝回滚：$backupFull"
        }
        $restoreCode = Invoke-Icacls -Arguments @($full, "/restore", $backupFull, "/C") -AllowFailure
    }
    $resetApplied = $false
    if ($restoreCode -eq 0) {
        Invoke-Icacls -Arguments @($full, "/inheritance:e", "/T", "/C")
    } elseif ($ResetToInherited) {
        Invoke-Icacls -Arguments @($full, "/reset", "/T", "/C")
        $resetApplied = $true
    } else {
        Invoke-Icacls -Arguments @($full, "/inheritance:e", "/T", "/C")
    }
    $after = Get-AclSnapshot -Path $full
    $explicitAllowRules = @($after.rules | Where-Object {
        $_.access_type -eq "Allow" -and -not $_.inherited
    })
    $rollbackStatus = "failed"
    if (-not $after.inheritance_protected -and (-not $resetApplied -or $explicitAllowRules.Count -eq 0)) {
        $rollbackStatus = "rolled_back"
    }
    $payload = @{
        status = $rollbackStatus
        mode = "rollback"
        data_dir = $full
        actor = $identity
        actor_sid = $actorSid
        backup_acl_file = $BackupAclFile
        backup_restore_exit_code = $restoreCode
        backup_restored = ($restoreCode -eq 0)
        fallback_reset_to_inherited = $resetApplied
        explicit_allow_rules_after = $explicitAllowRules
        inheritance_enabled = -not $after.inheritance_protected
        effective_acl_restored_by_inheritance = (-not $after.inheritance_protected)
        acl_after = $after
    }
    Write-Evidence -Payload $payload
    if ($payload.status -ne "rolled_back") { throw "ACL 回滚后继承仍未恢复" }
    Write-Output "ROLLBACK_OK inheritance_enabled=$($payload.inheritance_enabled) backup_restored=$($payload.backup_restored) reset_to_inherited=$($payload.fallback_reset_to_inherited)"
    exit 0
}

if (-not (Test-Path -LiteralPath $full -PathType Container)) {
    if ($DryRun) { Write-Output "DRY_RUN_MKDIR $full" }
    else { New-Item -ItemType Directory -Path $full -Force | Out-Null }
}
$before = $null
if (Test-Path -LiteralPath $full -PathType Container) { $before = Get-AclSnapshot -Path $full }

$userGrant = "*${actorSid}:(OI)(CI)F"
$systemGrant = "*S-1-5-18:(OI)(CI)F"
$adminGrant = "*S-1-5-32-544:(OI)(CI)F"
$remove = @("*S-1-1-0", "*S-1-5-32-545", "*S-1-5-11")

if (-not [string]::IsNullOrWhiteSpace($BackupAclFile)) {
    $backupFull = [System.IO.Path]::GetFullPath($BackupAclFile)
    $backupParent = Split-Path -Parent $backupFull
    if ($backupParent -and -not $DryRun) { New-Item -ItemType Directory -Path $backupParent -Force | Out-Null }
    Invoke-Icacls -Arguments @($full, "/save", $backupFull, "/T", "/C")
}

# 先为所有对象写入显式权限，避免移除根继承后递归失去访问权。
Invoke-Icacls -Arguments (@($full, "/grant:r", "*${actorSid}:F", "*S-1-5-18:F", "*S-1-5-32-544:F") + @("/T", "/C"))
Invoke-Icacls -Arguments @($full, "/inheritance:r", "/T", "/C")
Invoke-Icacls -Arguments (@($full, "/remove:g") + $remove + @("/T", "/C"))
Invoke-Icacls -Arguments (@($full, "/grant:r", $userGrant, $systemGrant, $adminGrant, "/T", "/C"))

if ($DryRun) {
    Write-Output "ACTOR $identity ($actorSid)"
    Write-Output "DRY_RUN_OK no ACL changes applied"
    Write-Evidence -Payload @{
        status = "dry_run"; mode = "dry_run"; data_dir = $full
        actor = $identity; actor_sid = $actorSid
        backup_acl_file = $BackupAclFile; acl_before = $before
        other_user_logon_verified = $false
        other_user_verification_reason = "dry_run"
    }
    exit 0
}

$probe = Join-Path $full (".acl-inheritance-probe-" + [Guid]::NewGuid().ToString("N") + ".tmp")
try {
    Set-Content -LiteralPath $probe -Value "synthetic-acl-probe" -Encoding ascii
    $rootAcl = Get-AclSnapshot -Path $full
    $probeAcl = Get-AclSnapshot -Path $probe
    $rootCheck = Test-HardenedAcl -Snapshot $rootAcl -RequireProtected
    $probeCheck = Test-HardenedAcl -Snapshot $probeAcl
    $descendantChecks = @()
    $descendantFailures = @()
    foreach ($child in Get-ChildItem -LiteralPath $full -Recurse -Force) {
        $childAcl = Get-AclSnapshot -Path $child.FullName
        $childCheck = Test-HardenedAcl -Snapshot $childAcl
        $descendantChecks += [ordered]@{
            path = $child.FullName
            passed = [bool]$childCheck.passed
            forbidden_sids_present = @($childCheck.forbidden_sids_present)
        }
        if (-not $childCheck.passed) { $descendantFailures += $child.FullName }
    }
} catch {
    if (-not [string]::IsNullOrWhiteSpace($BackupAclFile)) {
        Invoke-Icacls -Arguments @($full, "/restore", $BackupAclFile, "/C") -AllowFailure | Out-Null
        Invoke-Icacls -Arguments @($full, "/inheritance:e", "/T", "/C") -AllowFailure | Out-Null
    }
    throw
} finally {
    if (Test-Path -LiteralPath $probe) { Remove-Item -LiteralPath $probe -Force }
}

$applyStatus = "failed"
if ($rootCheck.passed -and $probeCheck.passed -and $descendantFailures.Count -eq 0) {
    $applyStatus = "passed"
}
$payload = @{
    status = $applyStatus
    mode = "apply"
    data_dir = $full
    actor = $identity
    actor_sid = $actorSid
    backup_acl_file = $BackupAclFile
    acl_before = $before
    acl_after = $rootAcl
    inheritance_probe = $probeAcl
    verification = @{
        root = $rootCheck
        existing_descendants = $descendantChecks
        descendant_failures = $descendantFailures
        new_file_inheritance = $probeCheck
        other_user_logon_verified = $false
        other_user_verification_reason = "no_separate_standard_user_credential_available"
    }
}
Write-Evidence -Payload $payload
if ($payload.status -ne "passed") {
    if (-not [string]::IsNullOrWhiteSpace($BackupAclFile)) {
        Invoke-Icacls -Arguments @($full, "/restore", $BackupAclFile, "/C") -AllowFailure | Out-Null
    }
    Invoke-Icacls -Arguments @($full, "/inheritance:e", "/T", "/C") -AllowFailure | Out-Null
    throw "ACL 验证失败，已尝试回滚"
}
Write-Output "ACTOR $identity ($actorSid)"
Write-Output "ACL_APPLIED_AND_VERIFIED $full"
Write-Output "OTHER_USER_LOGON_VERIFIED false reason=no_separate_standard_user_credential_available"
