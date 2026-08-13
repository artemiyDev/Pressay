$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $env:LOCALAPPDATA "Pressay\venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}

Push-Location $projectRoot
try {
    $env:PYTHONPATH = Join-Path $projectRoot "src"
    $testDataDirectory = Join-Path $env:LOCALAPPDATA "Pressay\test-data"
    New-Item -ItemType Directory -Path $testDataDirectory -Force | Out-Null
    $env:COVERAGE_FILE = Join-Path $testDataDirectory ".coverage"
    & $venvPython -m pytest @args
}
finally {
    Pop-Location
}
