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
$sitePackages = Join-Path $runtimeRoot "Lib\site-packages"
$cudaBins = @(
    (Join-Path $sitePackages "nvidia\cublas\bin"),
    (Join-Path $sitePackages "nvidia\cudnn\bin"),
    (Join-Path $sitePackages "ctranslate2")
) | Where-Object { Test-Path -LiteralPath $_ }
if ($cudaBins.Count -gt 0) {
    $env:PATH = (($cudaBins -join ';') + ';' + $env:PATH)
}
& $venvPython -m pressay.doctor @args
