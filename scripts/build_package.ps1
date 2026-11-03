# build_package.ps1 - Reproducible LocalDocMask Windows onedir build.
param(
    [switch]$SkipSmoke,
    [switch]$VerifyReproducible,
    [string]$BuildTime = ""
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
Set-Location $root

function Assert-Success([string]$Name, [int]$Code) {
    if ($Code -ne 0) { throw "$Name failed with exit code $Code" }
}

function Assert-PathUnderProject([string]$PathValue) {
    $full = [System.IO.Path]::GetFullPath($PathValue)
    $prefix = $root.TrimEnd('\') + '\'
    if (-not ($full + '\').StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝操作项目目录之外的构建路径: $full"
    }
    return $full
}

function Reset-BuildPath([string]$PathValue) {
    $full = Assert-PathUnderProject $PathValue
    if (Test-Path -LiteralPath $full) {
        Remove-Item -LiteralPath $full -Recurse -Force
    }
    New-Item -ItemType Directory -Path $full -Force | Out-Null
    return $full
}

function Copy-PackageRuntime([string]$BundleDir) {
    Copy-Item -Recurse -Force (Join-Path $root "resources") (Join-Path $BundleDir "resources")
    New-Item -ItemType Directory -Force (Join-Path $BundleDir "config") | Out-Null
    Copy-Item -Force (Join-Path $root "config\config.example.yaml") (Join-Path $BundleDir "config\config.example.yaml")
}

function Invoke-PyInstaller([string]$DistRoot, [string]$WorkRoot) {
    $env:PYTHONHASHSEED = "0"
    $env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
    & $py -m PyInstaller --noconfirm --clean --distpath $DistRoot --workpath $WorkRoot (Join-Path $root "LocalDocMask.spec") | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败，退出码 $LASTEXITCODE" }
    $bundle = Join-Path $DistRoot "LocalDocMask"
    if (-not (Test-Path -LiteralPath (Join-Path $bundle "LocalDocMask.exe"))) {
        throw "未生成 LocalDocMask.exe: $bundle"
    }
    Copy-PackageRuntime $bundle
    return $bundle
}

function Assert-PackageContents([string]$BundleDir) {
    $topForbidden = @("data", "tests", "evaluation", "demo", ".env", "tasks.db")
    foreach ($name in $topForbidden) {
        if (Test-Path -LiteralPath (Join-Path $BundleDir $name)) {
            throw "发布包包含禁止内容: $name"
        }
    }
    $secretLike = Get-ChildItem -LiteralPath $BundleDir -Recurse -File | Where-Object {
        $_.Name -eq ".env" -or $_.Name -like "*.key" -or $_.Name -like "*.p12" -or
        $_.Name -like "*.pfx" -or $_.Name -match '^tasks\.db($|-wal$|-shm$)'
    }
    if ($secretLike) {
        throw "发布包包含疑似秘密/运行数据库文件: $($secretLike[0].FullName)"
    }
    $privateKeyLike = Get-ChildItem -LiteralPath $BundleDir -Recurse -File -Filter "*.pem" | Where-Object {
        $head = Get-Content -LiteralPath $_.FullName -TotalCount 20 -ErrorAction SilentlyContinue
        [bool]($head -match "PRIVATE KEY")
    }
    if ($privateKeyLike) {
        throw "发布包包含私钥材料: $($privateKeyLike[0].FullName)"
    }
}

function Wait-ForStartup([System.Diagnostics.Process]$Process, [string]$DbPath, [int]$TimeoutSeconds = 60) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $Process.Refresh()
        if ($Process.HasExited) { return $false }
        if (Test-Path -LiteralPath $DbPath) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

function Invoke-CleanStateSmoke([string]$BundleDir) {
    $exe = Join-Path $BundleDir "LocalDocMask.exe"
    $smokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("localdocmask_t9_smoke_" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $smokeRoot -Force | Out-Null
    $oldHome = $env:LOCALDOCMASK_HOME
    $oldQt = $env:QT_QPA_PLATFORM
    $process = $null
    try {
        $env:LOCALDOCMASK_HOME = $smokeRoot
        $env:QT_QPA_PLATFORM = "offscreen"
        $db = Join-Path $smokeRoot "tasks.db"
        $process = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden
        if (-not (Wait-ForStartup $process $db 60)) {
            $process.Refresh()
            throw "首次启动未建立 tasks.db 或进程提前退出"
        }
        Start-Sleep -Seconds 2
        $process.Refresh()
        if ($process.HasExited) { throw "首次启动在初始化后提前退出，退出码 $($process.ExitCode)" }
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        $process.WaitForExit(10000) | Out-Null

        & $py (Join-Path $PSScriptRoot "package_db_check.py") --db $db --min-startups 0 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "首次启动数据库完整性检查失败" }

        $process = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden
        Start-Sleep -Seconds 5
        $process.Refresh()
        if ($process.HasExited) { throw "第二次启动提前退出，退出码 $($process.ExitCode)" }
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        $process.WaitForExit(10000) | Out-Null
        & $py (Join-Path $PSScriptRoot "package_db_check.py") --db $db --min-startups 2 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "第二次启动后数据库/历史记录验证失败" }

        $required = @("input", "processing", "output\success", "output\quarantine", "logs", "state\metrics", "output\diagnostics")
        foreach ($relative in $required) {
            if (-not (Test-Path -LiteralPath (Join-Path $smokeRoot $relative) -PathType Container)) {
                throw "首次启动未建立目录: $relative"
            }
        }
        return [ordered]@{
            status = "passed"
            mode = "isolated-clean-state-on-current-windows"
            first_start_created_database = $true
            second_start_reused_database = $true
            database_integrity = "ok"
            no_bundle_runtime_data = $true
        }
    }
    finally {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        $env:LOCALDOCMASK_HOME = $oldHome
        $env:QT_QPA_PLATFORM = $oldQt
        $fullSmoke = [System.IO.Path]::GetFullPath($smokeRoot)
        $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        if ($fullSmoke.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $fullSmoke)) {
            Remove-Item -LiteralPath $fullSmoke -Recurse -Force
        }
    }
}

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $py)) { throw "venv interpreter not found: $py" }
$pyVersion = & $py -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
if ($LASTEXITCODE -ne 0 -or $pyVersion -notlike "3.11.*") { throw "要求 Python 3.11.x，实际 $pyVersion" }
$piVersion = & $py -c "import PyInstaller; print(PyInstaller.__version__)"
if ($LASTEXITCODE -ne 0 -or $piVersion -ne "6.22.3") { throw "要求 PyInstaller 6.22.3，实际 $piVersion" }

$cleanJson = & $py (Join-Path $PSScriptRoot "build_metadata.py") --check-clean-worktree --repo-root $root
if ($LASTEXITCODE -ne 0) {
    throw "工作区不干净，拒绝构建；不会清理 dist/build。输出：$cleanJson"
}
$commit = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw "无法读取 Git 提交" }
$sourceDate = (& git show -s --format=%ct HEAD).Trim()
if (-not $sourceDate) { $sourceDate = "0" }
$env:SOURCE_DATE_EPOCH = $sourceDate
if ([string]::IsNullOrWhiteSpace($BuildTime)) { $BuildTime = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ") }

$distRoot = Reset-BuildPath (Join-Path $root "dist")
$workRoot = Reset-BuildPath (Join-Path $root "build\pyinstaller")
$bundle = Invoke-PyInstaller $distRoot $workRoot
Assert-PackageContents $bundle

$metadataArgs = @((Join-Path $PSScriptRoot "build_metadata.py"),
    "--bundle-dir", $bundle, "--build-time", $BuildTime, "--commit", $commit)
$metadataJson = & $py @metadataArgs
if ($LASTEXITCODE -ne 0) { throw "构建元数据生成失败" }
$metadata = $metadataJson | ConvertFrom-Json
$applicationVersion = [string]$metadata.identity.application_version

$smoke = [ordered]@{ status = "skipped" }
if (-not $SkipSmoke) { $smoke = Invoke-CleanStateSmoke $bundle }

$repro = [ordered]@{ requested = [bool]$VerifyReproducible; status = "not_run" }
if ($VerifyReproducible) {
    $verifyRoot = Join-Path $root "build\reproducibility"
    $verifyDist = Reset-BuildPath (Join-Path $verifyRoot "dist")
    $verifyWork = Reset-BuildPath (Join-Path $verifyRoot "work")
    $secondBundle = Invoke-PyInstaller $verifyDist $verifyWork
    $secondMetadataArgs = @((Join-Path $PSScriptRoot "build_metadata.py"),
        "--bundle-dir", $secondBundle, "--build-time", $BuildTime, "--commit", $commit)
    & $py @secondMetadataArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "第二次构建元数据生成失败" }
    $reproPath = Join-Path (Join-Path $root "build") "reproducibility.json"
    & $py (Join-Path $PSScriptRoot "build_metadata.py") `
        --bundle-dir $bundle --compare $secondBundle --json-output $reproPath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "重复构建的身份、提交或资源哈希不一致" }
    $repro = Get-Content -Raw -LiteralPath $reproPath | ConvertFrom-Json
    if (-not $repro.reproducibility.identical) { throw "重复构建关键身份/资源不一致" }
    Remove-Item -LiteralPath $verifyRoot -Recurse -Force
}

$evidenceDir = Join-Path $root "dist\t9-evidence"
New-Item -ItemType Directory -Path $evidenceDir -Force | Out-Null
$packageE2ePath = Join-Path $evidenceDir "package-e2e.json"
& $py (Join-Path $PSScriptRoot "package_e2e_check.py") --bundle-dir $bundle --evidence $packageE2ePath 2>&1 | Tee-Object -FilePath (Join-Path $evidenceDir "package-e2e-console.txt") | Out-Host
Assert-Success "package e2e" $LASTEXITCODE
$packageE2e = Get-Content -Raw -Encoding UTF8 -LiteralPath $packageE2ePath | ConvertFrom-Json
if ($packageE2e.status -ne "passed") { throw "package e2e failed" }
$bundleBytes = (Get-ChildItem -LiteralPath $bundle -Recurse -File | Measure-Object -Property Length -Sum).Sum
$exeHash = (Get-FileHash -LiteralPath (Join-Path $bundle "LocalDocMask.exe") -Algorithm SHA256).Hash.ToLowerInvariant()
[ordered]@{
    status = "passed"
    bundle = "dist/LocalDocMask"
    bundle_size_bytes = [int64]$bundleBytes
    executable_sha256 = $exeHash
    application_version = $applicationVersion
    database_schema_version = [int]$metadata.identity.database_schema_version
    config_schema_version = [int]$metadata.identity.config_schema_version
    policy_version = [string]$metadata.identity.policy_version
    source_tree_sha256 = [string]$metadata.identity.source_tree_sha256
    resource_tree_sha256 = [string]$metadata.identity.resource_tree_sha256
    build_time_utc = $BuildTime
    git_commit = $commit
    git_dirty = $false
    python_version = $pyVersion
    pyinstaller_version = $piVersion
    source_date_epoch = $sourceDate
    smoke = $smoke
    reproducibility = $repro
    package_e2e = [ordered]@{
        status = [string]$packageE2e.status
        evidence = "dist/t9-evidence/package-e2e.json"
        executable_sha256 = [string]$packageE2e.executable_sha256
        cases = $packageE2e.cases
    }
} | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $evidenceDir "build.json") -Encoding utf8

Write-Host "BUILD_OK -> $bundle"
Write-Host "CHECKSUMS -> $(Join-Path $bundle 'checksums.sha256')"
Write-Host "EVIDENCE -> $(Join-Path $evidenceDir 'build.json')"
exit 0
