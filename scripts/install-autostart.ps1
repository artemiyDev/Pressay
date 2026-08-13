[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
. (Join-Path $PSScriptRoot "shortcut-utils.ps1")

$spec = Get-PressayLauncherSpec -ProjectRoot $projectRoot
$startupDirectory = [Environment]::GetFolderPath("Startup")
if ([string]::IsNullOrWhiteSpace($startupDirectory)) {
    throw "Windows Startup directory is unavailable."
}
$shortcutPath = Join-Path $startupDirectory "Pressay.lnk"
New-PressayShortcut -ShortcutPath $shortcutPath -Spec $spec
Write-Host "Autostart is enabled for the current Windows user."
