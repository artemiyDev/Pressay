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
    # The installer guard serializes release publication. Holding the app
    # singleton at the same time closes the startup race before activation.
    $installerGuard = Enter-PressayInstallerGuard
    $appGuard = Enter-PressayAppMaintenanceGuard

    $layout = Get-PressayInstallLayout
    Assert-PressayInstallLayoutSafety -Layout $layout | Out-Null
    $version = Get-PressayProjectVersion -ProjectRoot $projectRoot
    $runtimeVersion = Get-PressayRuntimeVersionForInstall `
        -Layout $layout `
        -ReleaseVersion $version
    # Publish or reject the immutable payload before constructing its inactive
    # versioned runtime. The current release is never mutated.
    $versionRoot = Install-PressayPayload `
        -ProjectRoot $projectRoot `
        -Layout $layout `
        -Version $version `
        -RuntimeVersion $runtimeVersion
    $installedSource = Join-Path $versionRoot "src"
    $runtimeBuild = Initialize-PressayRuntimeBuild `
        -Layout $layout `
        -Version $runtimeVersion
    $venvRoot = $runtimeBuild.VenvRoot
    $venvPython = Join-Path $venvRoot "Scripts\python.exe"
    $runtimeCreated = -not [bool]$runtimeBuild.Reused
    if ($runtimeCreated) {
        Write-Host "Preparing Pressay runtime $runtimeVersion"
    }
    else {
        Write-Host "Reusing validated Pressay runtime $runtimeVersion"
    }

    try {
        if ($runtimeCreated) {
            if ($Python -eq "py") {
                & py -3.11 -m venv $venvRoot
            }
            else {
                & $Python -m venv $venvRoot
            }
            if ($LASTEXITCODE -ne 0) { throw "Failed to create the Pressay runtime." }
        }
        Get-PressaySafeTreeFiles -Root $venvRoot | Out-Null
        Assert-PressayPathIsNotReparsePoint -Path $venvPython | Out-Null
        $env:PYTHONDONTWRITEBYTECODE = "1"

        if ($runtimeCreated) {
            & $venvPython -m pip install --upgrade pip setuptools wheel
            if ($LASTEXITCODE -ne 0) { throw "Failed to prepare packaging tools." }

            $runtimeDependencies = @(Get-PressayWindowsRuntimeDependencySpecs)
            & $venvPython -m pip install @runtimeDependencies
            if ($LASTEXITCODE -ne 0) { throw "Failed to install Pressay dependencies." }
        }

        & $venvPython -m pip check
        if ($LASTEXITCODE -ne 0) { throw "Pressay runtime dependency validation failed." }

        & $venvPython -c "import ctranslate2, faster_whisper, numpy, PySide6, sounddevice, uiautomation, win32api, struct, sys; assert sys.version_info[:2] == (3, 11) and struct.calcsize('P') * 8 == 64"
        if ($LASTEXITCODE -ne 0) { throw "Pressay runtime validation failed." }

        if ($runtimeCreated) {
            Complete-PressayRuntimeBuild `
                -Layout $layout `
                -Version $runtimeVersion | Out-Null
        }

        $iconSource = Join-Path $installedSource "pressay\assets\app-icon.svg"
        $iconTarget = $layout.IconPath
        $iconCommand = "from PySide6.QtCore import Qt; from PySide6.QtGui import QImage; import sys; image=QImage(sys.argv[1]).scaled(256, 256, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation); assert not image.isNull() and image.save(sys.argv[2], 'ICO')"
        & $venvPython -c $iconCommand $iconSource $iconTarget
        if ($LASTEXITCODE -ne 0) { throw "Failed to prepare the Pressay application icon." }

        if (-not $SkipModel) {
            $env:PYTHONPATH = $installedSource
            $sitePackages = Join-Path $venvRoot "Lib\site-packages"
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
            -LauncherSource (Join-Path $PSScriptRoot "run.ps1") `
            -UninstallerSource (Join-Path $PSScriptRoot "uninstall.ps1") | Out-Null

        Write-Host "Pressay $version is ready at $versionRoot"
    }
    catch {
        if ($runtimeCreated) {
            try {
                Remove-PressayIncompleteRuntimeBuild `
                    -Layout $layout `
                    -Version $runtimeVersion
            }
            catch {
                Write-Warning "Incomplete Pressay runtime cleanup was refused or failed."
            }
        }
        throw
    }
}
finally {
    Exit-PressayGuard -Guard $appGuard
    Exit-PressayGuard -Guard $installerGuard
}
