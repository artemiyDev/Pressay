$ErrorActionPreference = "Stop"
# Windows PowerShell 5 otherwise decodes native UTF-8 output using the active
# legacy console code page, corrupting Cyrillic device names and JSON values.
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8WithoutBom
$OutputEncoding = $utf8WithoutBom
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
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
