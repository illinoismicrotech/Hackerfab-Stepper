# HackerFab Stepper – Windows launcher (PowerShell)
# Run from any directory; script locates the project root automatically.
#
# Use -SetupOnly to install and validate dependencies without opening the GUI.

param(
    [switch]$SetupOnly
)
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

Write-Host ""
Write-Host "  HackerFab Stepper" -ForegroundColor Cyan
Write-Host "  -----------------" -ForegroundColor Cyan
Write-Host ""

# ── 1. Find a compatible system Python ───────────────────────────────────
# Prefer Python 3.13 because Pillow publishes macOS and Windows wheels for it.
$PythonExe = $null
$PythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.13 --version *> $null
    if ($LASTEXITCODE -eq 0) {
        $PythonExe = "py"
        $PythonArgs = @("-3.13")
    }
}
if (-not $PythonExe -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $PythonExe = "python"
}
if (-not $PythonExe) {
    throw "Python 3.10-3.13 is required. Install Python 3.13 from python.org, then run this script again."
}

$PythonVersion = & $PythonExe @PythonArgs -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($PythonVersion -notin @("3.10", "3.11", "3.12", "3.13")) {
    throw "Python 3.10-3.13 is required (found $PythonVersion). Install Python 3.13, then run this script again."
}

# ── 2. Create the project environment and install dependencies ───────────
# Version the directory so a failed Python 3.14 environment is never reused.
$VenvDir = Join-Path $ProjectRoot ".venv-$PythonVersion"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "  Creating Python environment..." -ForegroundColor Yellow
    & $PythonExe @PythonArgs -m venv $VenvDir
}

# First run downloads PyTorch and its dependencies (~1-2 GB). This is normal.
Write-Host "  Checking dependencies..." -ForegroundColor Yellow
& $VenvPython -m pip install --prefer-binary --requirement requirements.txt
Write-Host "  Dependencies OK" -ForegroundColor Green
Write-Host ""

if ($SetupOnly) {
    Write-Host "  Setup-only check complete." -ForegroundColor Green
    exit 0
}

# ── 3. Launch ────────────────────────────────────────────────────────────
Write-Host "  Starting..." -ForegroundColor Green
Write-Host ""
& $VenvPython src/gui.py
exit $LASTEXITCODE
