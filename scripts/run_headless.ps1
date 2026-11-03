# run_headless.ps1 - Offscreen GUI smoke check: build MainWindow and exit.
# Used for no-display environments and post-packaging smoke testing.
# No real window is shown; the data dir is isolated to TEMP.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:QT_QPA_PLATFORM = "offscreen"
$env:LOCALDOCMASK_HOME = Join-Path $env:TEMP ("ldm_headless_" + [Guid]::NewGuid().ToString("N"))

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $py)) {
    Write-Error "venv interpreter not found: $py (create .venv and install deps first)"
    exit 2
}

& $py (Join-Path $PSScriptRoot "headless_check.py")
exit $LASTEXITCODE
