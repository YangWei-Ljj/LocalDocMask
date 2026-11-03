# run_tests.ps1 - Unified test entrypoint: run pytest from the project root.
# Usage:
#   pwsh scripts/run_tests.ps1                    # full suite
#   pwsh scripts/run_tests.ps1 tests/test_masker.py -v
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $py)) {
    Write-Error "venv interpreter not found: $py (create .venv and install deps first)"
    exit 2
}

if ($args.Count -eq 0) {
    & $py -m pytest tests -q
} else {
    & $py -m pytest @args
}
exit $LASTEXITCODE
