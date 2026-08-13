[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $env:LOCALAPPDATA "Pressay\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}

$env:PYTHONPATH = Join-Path $projectRoot "src"
$driver = Join-Path $PSScriptRoot "e2e_input.py"
& $venvPython $driver
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
