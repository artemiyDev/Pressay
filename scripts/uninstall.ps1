[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$RemoveRuntime,
    [switch]$RemoveUserData
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$shortcutUtilities = Join-Path $PSScriptRoot "shortcut-utils.ps1"
. $shortcutUtilities

# Destructive modes must complete their entire read-only preflight before any
# shortcut is changed. A running app therefore causes a fully atomic refusal.
if ($RemoveRuntime -or $RemoveUserData) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "LOCALAPPDATA is unavailable; refusing to remove runtime or user data."
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
    $appDataRoot = [System.IO.Path]::GetFullPath((Join-Path $localAppData "Pressay"))
    $expectedPrefix = $localAppData + [System.IO.Path]::DirectorySeparatorChar
    if (
        -not $appDataRoot.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $appDataRoot) -cne "Pressay"
    ) {
        throw "Unsafe Pressay data path; refusing removal: $appDataRoot"
    }

    if (-not $WhatIfPreference) {
        $appMutex = $null
        $appIsRunning = $false
        try {
            $appMutex = [System.Threading.Mutex]::OpenExisting("Local\Pressay.Desktop.Singleton")
            $appIsRunning = $true
        }
        catch [System.Threading.WaitHandleCannotBeOpenedException] {
            $appIsRunning = $false
        }
        finally {
            if ($null -ne $appMutex) {
                $appMutex.Dispose()
            }
        }
        if ($appIsRunning) {
            throw "Pressay is running. Exit it from the tray menu before removing runtime or user data."
        }
    }
}

$spec = Get-PressayLauncherSpec -ProjectRoot $projectRoot
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

function Assert-PressayRemovalTarget {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedRelativePath
    )

    $resolved = [System.IO.Path]::GetFullPath($Path)
    $expected = [System.IO.Path]::GetFullPath((Join-Path $appDataRoot $ExpectedRelativePath))
    if (-not [string]::Equals($resolved, $expected, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe removal target; refusing removal: $resolved"
    }
    return $resolved
}

if ($RemoveRuntime) {
    $venvPath = Assert-PressayRemovalTarget `
        -Path (Join-Path $appDataRoot "venv") `
        -ExpectedRelativePath "venv"
    if ((Test-Path -LiteralPath $venvPath) -and $PSCmdlet.ShouldProcess($venvPath, "Permanently remove Pressay runtime")) {
        Remove-Item -LiteralPath $venvPath -Recurse -Force
        Write-Host "Runtime removed. Recover it by running .\scripts\install.ps1 again."
    }
}

if ($RemoveUserData) {
    $userFiles = @(
        (Assert-PressayRemovalTarget -Path (Join-Path $appDataRoot "config.json") -ExpectedRelativePath "config.json"),
        (Assert-PressayRemovalTarget -Path (Join-Path $appDataRoot "pressay.log") -ExpectedRelativePath "pressay.log"),
        (Assert-PressayRemovalTarget -Path (Join-Path $appDataRoot "pressay.log.1") -ExpectedRelativePath "pressay.log.1"),
        (Assert-PressayRemovalTarget -Path (Join-Path $appDataRoot "pressay.log.2") -ExpectedRelativePath "pressay.log.2"),
        (Assert-PressayRemovalTarget -Path (Join-Path $appDataRoot "pressay.log.3") -ExpectedRelativePath "pressay.log.3")
    )
    Write-Warning "Configuration and logs selected with -RemoveUserData cannot be recovered."
    foreach ($userFile in $userFiles) {
        if ((Test-Path -LiteralPath $userFile) -and $PSCmdlet.ShouldProcess($userFile, "Permanently remove Pressay user data")) {
            Remove-Item -LiteralPath $userFile -Force
            Write-Host "User data removed: $userFile"
        }
    }
}

if (-not $RemoveRuntime -and -not $RemoveUserData) {
    Write-Host "Pressay shortcuts were removed. Runtime, configuration, logs and models were preserved."
    Write-Host "Run .\scripts\install.ps1 to restore the shortcuts and launch the app."
}
