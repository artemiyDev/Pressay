[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "install-layout.ps1")
$layout = Get-PressayInstallLayout
$runtimeRoot = Get-PressayActiveRuntimeRoot -Layout $layout
$venvPython = Join-Path $runtimeRoot "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}

$env:PYTHONPATH = Join-Path $projectRoot "src"
$driver = Join-Path $PSScriptRoot "e2e_input.py"
& $venvPython $driver
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
