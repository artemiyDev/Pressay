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
$venvPythonw = Join-Path $env:LOCALAPPDATA "Pressay\venv\Scripts\pythonw.exe"
if ($args -contains "--background") {
    if (-not (Test-Path -LiteralPath $venvPythonw -PathType Leaf)) {
        [Console]::Error.WriteLine("Background Python launcher is missing: $venvPythonw")
        exit 1
    }

    try {
        $backgroundProcess = Start-Process `
            -FilePath $venvPythonw `
            -ArgumentList (@("-m", "pressay") + @($args)) `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -PassThru `
            -ErrorAction Stop
    }
    catch {
        [Console]::Error.WriteLine("Failed to launch Pressay in background: $($_.Exception.Message)")
        exit 1
    }

    # Catch import/configuration failures that terminate pythonw immediately.
    # A healthy tray process outlives this short launch handshake; the hidden
    # PowerShell launcher can then exit without waiting for the app lifetime.
    if ($backgroundProcess.WaitForExit(1500)) {
        $backgroundExitCode = [int]$backgroundProcess.ExitCode
        if ($backgroundExitCode -ne 0) {
            [Console]::Error.WriteLine("Pressay background process exited during startup (code $backgroundExitCode).")
            exit $backgroundExitCode
        }
    }
    exit 0
}

& $venvPython -m pressay @args
$foregroundExitCode = [int]$LASTEXITCODE
exit $foregroundExitCode
