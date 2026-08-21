[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "shortcut-utils.ps1")

$layout = Get-PressayInstallLayout
if (-not (Test-Path -LiteralPath $layout.LauncherPath -PathType Leaf)) {
    throw "Pressay is not installed. Run .\scripts\install.ps1 first."
}
$spec = Get-PressayLauncherSpec
$startupDirectory = [Environment]::GetFolderPath("Startup")
if ([string]::IsNullOrWhiteSpace($startupDirectory)) {
    throw "Windows Startup directory is unavailable."
}
$shortcutPath = Join-Path $startupDirectory "Pressay.lnk"
New-PressayShortcut -ShortcutPath $shortcutPath -Spec $spec
Write-Host "Autostart is enabled for the current Windows user."
