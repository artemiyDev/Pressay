[CmdletBinding()]
param(
    [string]$Python = "py",
    [switch]$SkipModel,
    [string]$Model = "turbo"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvRoot = Join-Path $env:LOCALAPPDATA "Pressay\venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $venvRoot) -Force | Out-Null
    if ($Python -eq "py") {
        & py -3.11 -m venv $venvRoot
    }
    else {
        & $Python -m venv $venvRoot
    }
}

& $venvPython -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade packaging tools." }

# This workspace may be exposed through a read-mostly project mount. Install
# dependencies normally and expose sources with PYTHONPATH in the launchers;
# that avoids pip attempting to create editable-build metadata beside sources.
& $venvPython -m pip install `
    "faster-whisper>=1.2.1,<2" `
    "numpy>=1.26,<3" `
    "PySide6>=6.8,<7" `
    "pywin32>=306" `
    "sounddevice>=0.5.1,<1" `
    "nvidia-cublas-cu12>=12,<13" `
    "nvidia-cudnn-cu12>=9,<10" `
    "uiautomation>=2.0,<3" `
    "pytest>=8.3,<10" `
    "pytest-cov>=6,<8"
if ($LASTEXITCODE -ne 0) { throw "Failed to install Pressay dependencies." }

if (-not $SkipModel) {
    $env:PYTHONPATH = Join-Path $projectRoot "src"
    $sitePackages = Join-Path $env:LOCALAPPDATA "Pressay\venv\Lib\site-packages"
    $cudaBins = @(
        (Join-Path $sitePackages "nvidia\cublas\bin"),
        (Join-Path $sitePackages "nvidia\cudnn\bin"),
        (Join-Path $sitePackages "ctranslate2")
    ) | Where-Object { Test-Path -LiteralPath $_ }
    if ($cudaBins.Count -gt 0) {
        $env:PATH = (($cudaBins -join ';') + ';' + $env:PATH)
    }
    & $venvPython -m pressay.model_setup --model $Model
    if ($LASTEXITCODE -ne 0) { throw "Model setup failed." }
}

Write-Host "Pressay is ready. Run .\scripts\run.ps1"
