[CmdletBinding()]
param(
    [string]$Python = "py",
    [string]$Model = "turbo",
    [switch]$SkipModel,
    [switch]$DesktopShortcut,
    [switch]$EnableAutostart,
    [switch]$NoLaunch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$setupScript = Join-Path $PSScriptRoot "setup.ps1"
$shortcutUtilities = Join-Path $PSScriptRoot "shortcut-utils.ps1"
$autostartInstaller = Join-Path $PSScriptRoot "install-autostart.ps1"

. $shortcutUtilities

$setupParameters = @{
    Python = $Python
    Model  = $Model
}
if ($SkipModel) {
    $setupParameters.SkipModel = $true
}

Write-Host "Preparing Pressay..."
& $setupScript @setupParameters

$layout = Get-PressayInstallLayout
if (
    -not (Test-Path -LiteralPath $layout.LauncherPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $layout.CurrentFile -PathType Leaf)
) {
    throw "Pressay setup completed without an active installed launcher."
}
$spec = Get-PressayLauncherSpec
$programsDirectory = [Environment]::GetFolderPath("Programs")
if ([string]::IsNullOrWhiteSpace($programsDirectory)) {
    throw "Windows Start Menu directory is unavailable."
}
New-PressayShortcut `
    -ShortcutPath (Join-Path $programsDirectory "Pressay.lnk") `
    -Spec $spec

if ($DesktopShortcut) {
    $desktopDirectory = [Environment]::GetFolderPath("Desktop")
    if ([string]::IsNullOrWhiteSpace($desktopDirectory)) {
        throw "Windows Desktop directory is unavailable."
    }
    New-PressayShortcut `
        -ShortcutPath (Join-Path $desktopDirectory "Pressay.lnk") `
        -Spec $spec
}

if ($EnableAutostart) {
    & $autostartInstaller
}

if (-not $NoLaunch) {
    Start-Process `
        -FilePath $spec.TargetPath `
        -ArgumentList $spec.Arguments `
        -WorkingDirectory $spec.WorkingDirectory `
        -WindowStyle Hidden
    Write-Host "Pressay launched in the system tray."
}

Write-Host "Installation complete. No administrator rights or registry changes were used."
