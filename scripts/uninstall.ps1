[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$RemoveApp,
    [switch]$RemoveRuntime,
    [switch]$RemoveUserData
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$shortcutUtilities = Join-Path $PSScriptRoot "shortcut-utils.ps1"
. $shortcutUtilities
$layout = Get-PressayInstallLayout
$destructiveMode = $RemoveApp -or $RemoveRuntime -or $RemoveUserData
$appTargets = @()
$runtimeTargets = @()
$userFiles = @()

function Assert-PressayRemovalTarget {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedRelativePath,

        [Parameter(Mandatory = $true)]
        [string]$AppDataRoot
    )

    $resolved = [System.IO.Path]::GetFullPath($Path)
    $expected = [System.IO.Path]::GetFullPath((Join-Path $AppDataRoot $ExpectedRelativePath))
    if (-not [string]::Equals($resolved, $expected, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe removal target; refusing removal: $resolved"
    }
    Assert-PressayPathIsNotReparsePoint -Path $resolved | Out-Null
    return $resolved
}

# Resolve and validate every requested destructive target before shortcuts or
# installed files are changed. The guards are then held through all mutations.
if ($destructiveMode) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "LOCALAPPDATA is unavailable; refusing to remove Pressay files."
    }

    $localAppData = [System.IO.Path]::GetFullPath($env:LOCALAPPDATA)
    $localAppDataRoot = [System.IO.Path]::GetPathRoot($localAppData)
    if ([string]::Equals($localAppData, $localAppDataRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "LOCALAPPDATA resolves to a filesystem root; refusing removal: $localAppData"
    }
    $localAppData = $localAppData.TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $appDataRoot = [System.IO.Path]::GetFullPath($layout.Root)
    $expectedPrefix = $localAppData + [System.IO.Path]::DirectorySeparatorChar
    if (
        -not $appDataRoot.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $appDataRoot) -cne "Pressay"
    ) {
        throw "Unsafe Pressay data path; refusing removal: $appDataRoot"
    }
    Assert-PressayPathIsNotReparsePoint -Path $appDataRoot | Out-Null

    if ($RemoveApp) {
        $appTargets = @(
            [pscustomobject]@{
                Path = Assert-PressayRemovalTarget -Path $layout.VersionsRoot -ExpectedRelativePath "app" -AppDataRoot $appDataRoot
                Recursive = $true
                Description = "Permanently remove installed Pressay versions"
            },
            [pscustomobject]@{
                Path = Assert-PressayRemovalTarget -Path $layout.CurrentFile -ExpectedRelativePath "current" -AppDataRoot $appDataRoot
                Recursive = $false
                Description = "Permanently remove the Pressay current-version pointer"
            },
            [pscustomobject]@{
                Path = Assert-PressayRemovalTarget -Path $layout.LauncherPath -ExpectedRelativePath "Pressay.ps1" -AppDataRoot $appDataRoot
                Recursive = $false
                Description = "Permanently remove the Pressay launcher"
            },
            [pscustomobject]@{
                Path = Assert-PressayRemovalTarget -Path $layout.IconPath -ExpectedRelativePath "pressay.ico" -AppDataRoot $appDataRoot
                Recursive = $false
                Description = "Permanently remove the Pressay icon"
            }
        )
    }
    if ($RemoveRuntime) {
        $runtimeTargets = @(
            [pscustomobject]@{
                Path = Assert-PressayRemovalTarget -Path $layout.RuntimeRoot -ExpectedRelativePath "venv" -AppDataRoot $appDataRoot
                Recursive = $true
                Description = "Permanently remove Pressay runtime"
            }
        )
    }
    if ($RemoveUserData) {
        $userFiles = @(
            (Assert-PressayRemovalTarget -Path (Join-Path $appDataRoot "config.json") -ExpectedRelativePath "config.json" -AppDataRoot $appDataRoot),
            (Assert-PressayRemovalTarget -Path (Join-Path $appDataRoot "pressay.log") -ExpectedRelativePath "pressay.log" -AppDataRoot $appDataRoot),
            (Assert-PressayRemovalTarget -Path (Join-Path $appDataRoot "pressay.log.1") -ExpectedRelativePath "pressay.log.1" -AppDataRoot $appDataRoot),
            (Assert-PressayRemovalTarget -Path (Join-Path $appDataRoot "pressay.log.2") -ExpectedRelativePath "pressay.log.2" -AppDataRoot $appDataRoot),
            (Assert-PressayRemovalTarget -Path (Join-Path $appDataRoot "pressay.log.3") -ExpectedRelativePath "pressay.log.3" -AppDataRoot $appDataRoot)
        )
    }
    foreach ($target in @($appTargets + $runtimeTargets)) {
        if ($target.Recursive -and (Test-Path -LiteralPath $target.Path -PathType Container)) {
            Get-PressaySafeTreeFiles -Root $target.Path | Out-Null
        }
    }
}

$installerGuard = $null
$appGuard = $null
try {
    if ($destructiveMode -and -not $WhatIfPreference) {
        $installerGuard = Enter-PressayInstallerGuard
        $appGuard = Enter-PressayAppMaintenanceGuard
    }

    $spec = Get-PressayLauncherSpec
    $shortcutPaths = @()
    foreach ($specialFolder in @("Programs", "Desktop", "Startup")) {
        $directory = [Environment]::GetFolderPath($specialFolder)
        if ([string]::IsNullOrWhiteSpace($directory) -or -not [System.IO.Path]::IsPathRooted($directory)) {
            Write-Warning "Skipped unavailable Windows folder: $specialFolder"
            continue
        }
        $shortcutPaths += Join-Path ([System.IO.Path]::GetFullPath($directory)) "Pressay.lnk"
    }
    foreach ($shortcutPath in $shortcutPaths) {
        Remove-PressayShortcut -ShortcutPath $shortcutPath -Spec $spec -WhatIf:$WhatIfPreference | Out-Null
    }

    foreach ($target in $appTargets) {
        if (
            (Test-Path -LiteralPath $target.Path) -and
            $PSCmdlet.ShouldProcess($target.Path, $target.Description)
        ) {
            if ($target.Recursive) {
                Remove-Item -LiteralPath $target.Path -Recurse -Force
            }
            else {
                Remove-Item -LiteralPath $target.Path -Force
            }
            Write-Host "Installed app artifact removed: $($target.Path)"
        }
    }

    foreach ($target in $runtimeTargets) {
        if (
            (Test-Path -LiteralPath $target.Path) -and
            $PSCmdlet.ShouldProcess($target.Path, $target.Description)
        ) {
            Remove-Item -LiteralPath $target.Path -Recurse -Force
            Write-Host "Runtime removed. Recover it by running .\scripts\install.ps1 again."
        }
    }

    if ($RemoveUserData) {
        Write-Warning "Configuration and logs selected with -RemoveUserData cannot be recovered."
    }
    foreach ($userFile in $userFiles) {
        if (
            (Test-Path -LiteralPath $userFile) -and
            $PSCmdlet.ShouldProcess($userFile, "Permanently remove Pressay user data")
        ) {
            Remove-Item -LiteralPath $userFile -Force
            Write-Host "User data removed: $userFile"
        }
    }

    if (-not $destructiveMode) {
        Write-Host "Pressay shortcuts were removed. Installed versions, runtime, configuration, logs and models were preserved."
        Write-Host "Run .\scripts\install.ps1 to restore the shortcuts and launch the app."
    }
}
finally {
    Exit-PressayGuard -Guard $appGuard
    Exit-PressayGuard -Guard $installerGuard
}
