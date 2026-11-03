# release_acceptance.ps1 - Local-only release verification. No deploy/upload.
param(
    [switch]$SkipFullTests,
    [switch]$SkipBuild
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
$evidence = Join-Path $root "dist\t9-evidence"
$externalStatus = Join-Path $root "docs\operations\t9-evidence\external-validation-status.json"

function Reset-ProjectPath([string]$PathValue) {
    $full = [System.IO.Path]::GetFullPath($PathValue)
    $prefix = $root.TrimEnd('\') + '\'
    if (-not ($full + '\').StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝操作项目目录外路径: $full"
    }
    if (Test-Path -LiteralPath $full) { Remove-Item -LiteralPath $full -Recurse -Force }
    New-Item -ItemType Directory -Path $full -Force | Out-Null
    return $full
}

function Assert-Success([string]$Name, [int]$Code) {
    if ($Code -ne 0) { throw "$Name failed with exit code $Code" }
}

function Read-JsonFile([string]$PathValue) {
    if (-not (Test-Path -LiteralPath $PathValue -PathType Leaf)) { return $null }
    try { return Get-Content -Raw -Encoding UTF8 -LiteralPath $PathValue | ConvertFrom-Json }
    catch { return $null }
}

$cleanJson = & $py (Join-Path $PSScriptRoot "build_metadata.py") --check-clean-worktree --repo-root $root
$cleanCode = $LASTEXITCODE
if ($cleanCode -ne 0) {
    Write-Error "CLEAN_WORKTREE_REQUIRED: $cleanJson"
    exit 1
}
$cleanCheck = $cleanJson | ConvertFrom-Json
if (-not $cleanCheck.passed) { Write-Error "CLEAN_WORKTREE_REQUIRED"; exit 1 }

New-Item -ItemType Directory -Path $evidence -Force | Out-Null
$startedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$diffLog = Join-Path $evidence "git-diff-check.txt"
& git diff --check 2>&1 | Set-Content -LiteralPath $diffLog -Encoding utf8
Assert-Success "git diff --check" $LASTEXITCODE

$testSummary = "skipped"
if (-not $SkipFullTests) {
    $testLog = Join-Path $evidence "pytest-full.txt"
    & $py -m pytest -o "addopts=" -q tests 2>&1 | Tee-Object -FilePath $testLog | Out-Host
    Assert-Success "pytest" $LASTEXITCODE
    $summaryLine = Get-Content -LiteralPath $testLog | Select-Object -Last 1
    $testSummary = [string]$summaryLine
}

$gateReport = Join-Path $root "evaluation\reports\t9-release-gate.json"
& $py (Join-Path $PSScriptRoot "eval_gate.py") `
    --suite synthetic-recognition-v1 `
    --baseline (Join-Path $root "evaluation\baselines\synthetic-recognition-v1.baseline.json") `
    --report $gateReport 2>&1 | Set-Content -LiteralPath (Join-Path $evidence "eval-gate.txt") -Encoding utf8
Assert-Success "T8 eval gate" $LASTEXITCODE

$securityLog = Join-Path $evidence "security-regression.txt"
& $py -m pytest -o "addopts=" -q tests/test_security.py tests/test_security_hardening.py tests/test_t4_admission_atomic.py 2>&1 |
    Tee-Object -FilePath $securityLog | Out-Host
Assert-Success "security regression" $LASTEXITCODE

if (-not $SkipBuild) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build_package.ps1") -VerifyReproducible |
        Tee-Object -FilePath (Join-Path $evidence "build-console.txt") | Out-Host
    Assert-Success "reproducible package build" $LASTEXITCODE
}

$healthRoot = Reset-ProjectPath (Join-Path $root "build\t9-release-health")
$healthPath = Join-Path $evidence "health.json"
& $py (Join-Path $PSScriptRoot "health_check.py") --data-dir $healthRoot `
    --json-output $healthPath --create-diagnostic 2>&1 | Set-Content -LiteralPath (Join-Path $evidence "health-console.txt") -Encoding utf8
Assert-Success "health check" $LASTEXITCODE

$aclRoot = Reset-ProjectPath (Join-Path $root "build\t9-release-acl")
New-Item -ItemType Directory -Path (Join-Path $aclRoot "output\success") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $aclRoot "output\quarantine") -Force | Out-Null
Set-Content -LiteralPath (Join-Path $aclRoot "synthetic.txt") -Value "synthetic-acl-only" -Encoding ascii
$healthDb = Join-Path $healthRoot "tasks.db"
if (-not (Test-Path -LiteralPath $healthDb)) { throw "健康检查未生成 tasks.db" }
Copy-Item -LiteralPath $healthDb -Destination (Join-Path $aclRoot "tasks.db")
$aclBackup = Join-Path $root "build\t9-release-acl-backup.acl"
$aclDryRun = Join-Path $evidence "acl-dryrun.json"
$aclApply = Join-Path $evidence "acl-apply.json"
$aclRollback = Join-Path $evidence "acl-rollback.json"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "harden_data_acl.ps1") `
    -DataDir $aclRoot -BackupAclFile $aclBackup -DryRun -EvidenceFile $aclDryRun 2>&1 |
    Set-Content -LiteralPath (Join-Path $evidence "acl-dryrun-console.txt") -Encoding utf8
Assert-Success "ACL dry-run" $LASTEXITCODE
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "harden_data_acl.ps1") `
    -DataDir $aclRoot -BackupAclFile $aclBackup -EvidenceFile $aclApply 2>&1 |
    Tee-Object -FilePath (Join-Path $evidence "acl-apply-console.txt") | Out-Host
Assert-Success "ACL apply" $LASTEXITCODE
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "harden_data_acl.ps1") `
    -DataDir $aclRoot -Rollback -ResetToInherited -BackupAclFile $aclBackup -EvidenceFile $aclRollback 2>&1 |
    Tee-Object -FilePath (Join-Path $evidence "acl-rollback-console.txt") | Out-Host
Assert-Success "ACL rollback" $LASTEXITCODE

& $py (Join-Path $PSScriptRoot "release_db_drill.py") --output (Join-Path $evidence "db-drill.json") |
    Set-Content -LiteralPath (Join-Path $evidence "db-drill-console.txt") -Encoding utf8
Assert-Success "database drill" $LASTEXITCODE

$ErrorActionPreference = "Stop"
$healthEvidence = Read-JsonFile $healthPath
$aclDry = Read-JsonFile $aclDryRun
$aclApplied = Read-JsonFile $aclApply
$aclRolledBack = Read-JsonFile $aclRollback
$dbDrill = Read-JsonFile (Join-Path $evidence "db-drill.json")
$gateEvidence = Read-JsonFile $gateReport
$buildEvidencePath = Join-Path $evidence "build.json"
$buildEvidence = Read-JsonFile $buildEvidencePath
if ($null -eq $healthEvidence -or $null -eq $aclDry -or $null -eq $aclApplied -or
    $null -eq $aclRolledBack -or $null -eq $dbDrill -or $null -eq $gateEvidence) {
    throw "发布验收证据缺失或 JSON 无效"
}
if ($gateEvidence.execution_status -ne "completed" -or $gateEvidence.quality_status -ne "passed") {
    throw "T8 评测门禁未通过"
}
if ($healthEvidence.status -notin @("healthy", "degraded")) { throw "健康检查未通过" }
if ($aclDry.status -ne "dry_run" -or $aclApplied.status -ne "passed" -or $aclRolledBack.status -ne "rolled_back") {
    throw "ACL 演练未通过"
}
if ($dbDrill.status -ne "passed" -or $dbDrill.scope -ne "database-only") {
    throw "数据库演练未通过"
}

$head = (& git rev-parse HEAD).Trim()
Assert-Success "git rev-parse HEAD" $LASTEXITCODE
$bundle = Join-Path $root "dist\LocalDocMask"
$releaseGatePath = Join-Path $evidence "release-gate.json"
$releaseGateConsole = Join-Path $evidence "release-gate-console.txt"
& $py (Join-Path $PSScriptRoot "build_metadata.py") --validate-release-gate --bundle-dir $bundle --build-evidence $buildEvidencePath --external-status $externalStatus --expected-commit $head --json-output $releaseGatePath 2>&1 | Tee-Object -FilePath $releaseGateConsole | Out-Host
$releaseGateExit = $LASTEXITCODE
$releaseGate = Read-JsonFile $releaseGatePath
if ($null -eq $releaseGate) {
    $releaseGate = [ordered]@{
        passed = $false
        reason_codes = @("RELEASE_GATE_OUTPUT_MISSING")
        build = [ordered]@{ git_commit = $null; git_dirty = $null; executable_sha256 = $null; reproducibility_identical = $false }
        external_checks = [ordered]@{ clean_windows_vm_verified = $false; other_user_logon_verified = $false; old_binary_rollback_verified = $false }
        external_validation = [ordered]@{ path = $externalStatus; release_candidate = $null; executable_sha256 = $null; candidate_matches = $false; executable_matches = $false; checks = [ordered]@{} }
    }
}

$bundleBytes = 0
if (Test-Path -LiteralPath $bundle -PathType Container) {
    $bundleBytes = (Get-ChildItem -LiteralPath $bundle -Recurse -File | Measure-Object -Property Length -Sum).Sum
}
$failureReasons = @($releaseGate.reason_codes)
$releaseModeComplete = -not ($SkipFullTests -or $SkipBuild)
if (-not $releaseModeComplete) { $failureReasons += "RELEASE_MODE_INCOMPLETE" }
$failureReasons = @($failureReasons | Where-Object { $_ } | Select-Object -Unique)
$status = "failed"
if ($releaseModeComplete -and $releaseGateExit -eq 0 -and $releaseGate.passed -and $failureReasons.Count -eq 0) {
    $status = "passed"
}

$summary = [ordered]@{
    status = $status
    failure_reason_codes = $failureReasons
    generated_at_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    started_at_utc = $startedAt
    application_version = if ($buildEvidence) { $buildEvidence.application_version } else { $null }
    git_commit = $releaseGate.build.git_commit
    git_dirty = $releaseGate.build.git_dirty
    build_time_utc = if ($buildEvidence) { $buildEvidence.build_time_utc } else { $null }
    config_schema_version = if ($buildEvidence) { $buildEvidence.config_schema_version } else { $null }
    database_schema_version = if ($buildEvidence) { $buildEvidence.database_schema_version } else { $null }
    tests = $testSummary
    eval_gate = [ordered]@{
        suite_id = $gateEvidence.suite_id
        suite_version = $gateEvidence.suite_version
        suite_hash = $gateEvidence.suite_hash
        execution_status = $gateEvidence.execution_status
        quality_status = $gateEvidence.quality_status
    }
    build = [ordered]@{
        bundle = "dist/LocalDocMask"
        size_bytes = [int64]$bundleBytes
        executable_sha256 = $releaseGate.build.executable_sha256
        git_commit = $releaseGate.build.git_commit
        git_dirty = $releaseGate.build.git_dirty
        reproducibility_identical = $releaseGate.build.reproducibility_identical
        smoke = if ($buildEvidence) { $buildEvidence.smoke } else { $null }
    }
    health = [ordered]@{
        status = $healthEvidence.status
        database_integrity = $healthEvidence.database_integrity
        event_chain_valid = $healthEvidence.event_chain_valid
        external_network_used = $healthEvidence.external_network_used
    }
    acl = [ordered]@{
        dry_run_status = $aclDry.status
        apply_status = $aclApplied.status
        rollback_status = $aclRolledBack.status
        backup_restored = $aclRolledBack.backup_restored
        fallback_reset_to_inherited = $aclRolledBack.fallback_reset_to_inherited
        inheritance_enabled_after_rollback = $aclRolledBack.inheritance_enabled
    }
    database_drill = [ordered]@{
        scope = $dbDrill.scope
        status = $dbDrill.status
        upgrade = $dbDrill.upgrade
        database_restore = $dbDrill.database_restore
        restore_failure_safety = $dbDrill.restore_failure_safety
        success_quarantine_preserved = $dbDrill.success_quarantine_preserved
    }
    external_checks = $releaseGate.external_checks
    external_validation = $releaseGate.external_validation
    release_gate = [ordered]@{
        passed = [bool]$releaseGate.passed
        reason_codes = @($releaseGate.reason_codes)
        exit_code = $releaseGateExit
        path = "dist/t9-evidence/release-gate.json"
    }
    evidence_directory = "dist/t9-evidence"
}
$summary | ConvertTo-Json -Depth 14 | Set-Content -LiteralPath (Join-Path $evidence "release-summary.json") -Encoding utf8

$reasonsText = if ($failureReasons.Count) { $failureReasons -join ", " } else { "(none)" }
$md = @"
# LocalDocMask 发布验收报告

- 状态：$($summary.status)
- 失败原因码：$reasonsText
- Git 提交：$($summary.git_commit)
- 工作区有未提交修改：$($summary.git_dirty)
- 可执行文件 SHA-256：$($summary.build.executable_sha256)
- 外部验证候选匹配：$($summary.external_validation.candidate_matches)
- 外部验证 EXE 匹配：$($summary.external_validation.executable_matches)
- 干净 Windows VM：$($summary.external_validation.checks.clean_windows_vm_verified)
- 其他用户登录验证：$($summary.external_validation.checks.other_user_logon_verified)
- 旧版二进制回滚验证：$($summary.external_validation.checks.old_binary_rollback_verified)
- T8 门禁：$($summary.eval_gate.quality_status)
- 健康检查：$($summary.health.status)
- ACL：$($summary.acl.dry_run_status) / $($summary.acl.apply_status) / $($summary.acl.rollback_status)
- 数据库演练：$($summary.database_drill.status)，scope=$($summary.database_drill.scope)

外部验证只接受经结构化校验的 external-validation-status.json；缺失、过期、
候选或 EXE 不匹配、检查值非布尔 true 时，正式发布状态为 failed。
"@
Set-Content -LiteralPath (Join-Path $evidence "release-summary.md") -Value $md -Encoding utf8

Write-Host "RELEASE_EVIDENCE_OK -> $evidence"
Write-Host "STATUS -> $status"
if ($status -eq "passed") { exit 0 }
exit 1
