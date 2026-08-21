[CmdletBinding()]
param(
    [string]$Python = "py",
    [switch]$SkipModel,
    [string]$Model = "turbo"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
. (Join-Path $PSScriptRoot "install-layout.ps1")

$installerGuard = $null
$appGuard = $null
try {
    # The installer guard serializes shared-runtime mutations. Holding the app
    # singleton at the same time closes the startup race after the preflight.
    $installerGuard = Enter-PressayInstallerGuard
    $appGuard = Enter-PressayAppMaintenanceGuard

    $layout = Get-PressayInstallLayout
    Assert-PressayInstallLayoutSafety -Layout $layout | Out-Null
    $version = Get-PressayProjectVersion -ProjectRoot $projectRoot
    # Publish or reject the immutable payload before touching the shared runtime.
    # A later failure may leave an inactive version, which is safe to reuse.
    $versionRoot = Install-PressayPayload `
        -ProjectRoot $projectRoot `
        -Layout $layout `
        -Version $version
    $installedSource = Join-Path $versionRoot "src"
    $venvRoot = $layout.RuntimeRoot
    $venvPython = Join-Path $venvRoot "Scripts\python.exe"
    $runtimeCreated = $false

    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $venvRoot) -Force | Out-Null
        if ($Python -eq "py") {
            & py -3.11 -m venv $venvRoot
        }
        else {
            & $Python -m venv $venvRoot
        }
        if ($LASTEXITCODE -ne 0) { throw "Failed to create the Pressay runtime." }
        $runtimeCreated = $true
    }
    Get-PressaySafeTreeFiles -Root $venvRoot | Out-Null
    Assert-PressayPathIsNotReparsePoint -Path $venvPython | Out-Null

    # Packaging tools are upgraded only for a newly created runtime. Reusing a
    # healthy runtime avoids an unrelated mutation on every application update.
    if ($runtimeCreated) {
        & $venvPython -m pip install --upgrade pip setuptools wheel
        if ($LASTEXITCODE -ne 0) { throw "Failed to prepare packaging tools." }
    }

    # Install dependencies into the stable per-user runtime. Application code is
    # published separately as an immutable versioned payload below.
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

    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Pressay runtime dependency validation failed." }

    & $venvPython -c "import ctranslate2, faster_whisper, numpy, PySide6, sounddevice, uiautomation, win32api"
    if ($LASTEXITCODE -ne 0) { throw "Pressay runtime validation failed." }

    $iconSource = Join-Path $installedSource "pressay\assets\app-icon.svg"
    $iconTarget = $layout.IconPath
    $iconCommand = "from PySide6.QtCore import Qt; from PySide6.QtGui import QImage; import sys; image=QImage(sys.argv[1]).scaled(256, 256, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation); assert not image.isNull() and image.save(sys.argv[2], 'ICO')"
    & $venvPython -c $iconCommand $iconSource $iconTarget
    if ($LASTEXITCODE -ne 0) { throw "Failed to prepare the Pressay application icon." }

    if (-not $SkipModel) {
        $env:PYTHONPATH = $installedSource
        $env:PYTHONDONTWRITEBYTECODE = "1"
        $sitePackages = Join-Path $layout.RuntimeRoot "Lib\site-packages"
        $cudaBins = @(
            @(
                (Join-Path $sitePackages "nvidia\cublas\bin"),
                (Join-Path $sitePackages "nvidia\cudnn\bin"),
                (Join-Path $sitePackages "ctranslate2")
            ) | Where-Object { Test-Path -LiteralPath $_ }
        )
        if ($cudaBins.Count -gt 0) {
            $env:PATH = (($cudaBins -join ';') + ';' + $env:PATH)
        }
        & $venvPython -m pressay.model_setup --model $Model
        if ($LASTEXITCODE -ne 0) { throw "Model setup failed." }
    }

    Complete-PressayActivation `
        -Layout $layout `
        -Version $version `
        -LauncherSource (Join-Path $PSScriptRoot "run.ps1") | Out-Null

    Write-Host "Pressay $version is ready at $versionRoot"
}
finally {
    Exit-PressayGuard -Guard $appGuard
    Exit-PressayGuard -Guard $installerGuard
}
