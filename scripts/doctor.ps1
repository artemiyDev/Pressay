$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $env:LOCALAPPDATA "Pressay\venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}

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
& $venvPython -m pressay.doctor @args
