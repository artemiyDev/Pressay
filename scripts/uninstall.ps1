# Pressay installed uninstaller. This self-contained file is copied to the stable per-user install root.
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$RemoveApp,
    [switch]$RemoveRuntime,
    [switch]$RemoveUserData,
    [switch]$RemoveInstaller,
    [Parameter(DontShow = $true)]
    [string[]]$ShortcutDirectories
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:PressayShortcutDescription = "Local Pressay voice dictation"

function Get-PressayUninstallLayout {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "LOCALAPPDATA is unavailable."
    }
    $localRoot = [System.IO.Path]::GetFullPath($env:LOCALAPPDATA).TrimEnd('\', '/')
    $filesystemRoot = [System.IO.Path]::GetPathRoot($localRoot)
    if ([string]::Equals($localRoot, $filesystemRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "LOCALAPPDATA resolves to a filesystem root."
    }
    $root = [System.IO.Path]::GetFullPath((Join-Path $localRoot "Pressay"))
    $expectedPrefix = $localRoot + [System.IO.Path]::DirectorySeparatorChar
    if (
        -not $root.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $root) -cne "Pressay"
    ) {
        throw "Unsafe Pressay install root: $root"
    }
    return [pscustomobject]@{
        Root = $root
        VersionsRoot = Join-Path $root "app"
        RuntimeVersionsRoot = Join-Path $root "runtime"
        LegacyRuntimeRoot = Join-Path $root "venv"
        CurrentFile = Join-Path $root "current"
        LauncherPath = Join-Path $root "Pressay.ps1"
        UninstallerPath = Join-Path $root "Uninstall-Pressay.ps1"
        IconPath = Join-Path $root "pressay.ico"
    }
}

function Test-PressayUninstallPathIsReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)
    try {
        $item = Get-Item -LiteralPath ([System.IO.Path]::GetFullPath($Path)) -Force -ErrorAction Stop
    }
    catch [System.Management.Automation.ItemNotFoundException] {
        return $false
    }
    return (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Assert-PressayUninstallPathIsNotReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (Test-PressayUninstallPathIsReparsePoint -Path $resolved) {
        throw "Pressay path must not be a symbolic link or reparse point: $resolved"
    }
    return $resolved
}

function Get-PressayUninstallSafeTreeFiles {
    param([Parameter(Mandatory = $true)][string]$Root)
    $resolvedRoot = Assert-PressayUninstallPathIsNotReparsePoint -Path $Root
    if (-not (Test-Path -LiteralPath $resolvedRoot -PathType Container)) {
        throw "Pressay directory is missing: $resolvedRoot"
    }
    $directories = New-Object 'System.Collections.Generic.Queue[string]'
    $files = New-Object 'System.Collections.Generic.List[System.IO.FileInfo]'
    $directories.Enqueue($resolvedRoot)
    while ($directories.Count -gt 0) {
        $directory = $directories.Dequeue()
        foreach ($item in @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)) {
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Pressay tree contains a symbolic link or reparse point: $($item.FullName)"
            }
            if ($item.PSIsContainer) {
                $directories.Enqueue([System.IO.Path]::GetFullPath($item.FullName))
            }
            elseif ($item -is [System.IO.FileInfo]) {
                $files.Add($item)
            }
            else {
                throw "Pressay tree contains an unsupported filesystem item: $($item.FullName)"
            }
        }
    }
    return $files.ToArray()
}

function Enter-PressayUninstallGuard {
    param(
        [Parameter(Mandatory = $true)][string]$MutexName,
        [Parameter(Mandatory = $true)][string]$ConflictMessage
    )
    $createdNew = $false
    $guard = $null
    try {
        $guard = New-Object System.Threading.Mutex($false, $MutexName, [ref]$createdNew)
        if (-not $createdNew) { throw $ConflictMessage }
        return $guard
    }
    catch {
        if ($null -ne $guard) { $guard.Dispose() }
        throw
    }
}

function Exit-PressayUninstallGuard {
    param([AllowNull()][System.Threading.Mutex]$Guard)
    if ($null -ne $Guard) { $Guard.Dispose() }
}

function Test-PressayUninstallPathEquals {
    param([AllowNull()][string]$Left, [AllowNull()][string]$Right)
    if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) {
        return $false
    }
    try {
        return [string]::Equals(
            [System.IO.Path]::GetFullPath($Left),
            [System.IO.Path]::GetFullPath($Right),
            [System.StringComparison]::OrdinalIgnoreCase
        )
    }
    catch {
        return $false
    }
}

function Get-PressayUninstallLauncherSpec {
    param([Parameter(Mandatory = $true)][psobject]$Layout)
    $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
    return [pscustomobject]@{
        TargetPath = [System.IO.Path]::GetFullPath($powershell)
        Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$($Layout.LauncherPath)`" --background"
        WorkingDirectory = $Layout.Root
        Description = $script:PressayShortcutDescription
    }
}

function Get-PressayUninstallShortcutOwnership {
    param(
        [Parameter(Mandatory = $true)][string]$ShortcutPath,
        [Parameter(Mandatory = $true)][psobject]$Spec
    )
    if (
        -not (Test-Path -LiteralPath $ShortcutPath -PathType Leaf) -or
        (Test-PressayUninstallPathIsReparsePoint -Path $ShortcutPath)
    ) {
        return "missing"
    }
    $shell = $null
    $shortcut = $null
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($ShortcutPath)
        $installed = (
            (Test-PressayUninstallPathEquals -Left $shortcut.TargetPath -Right $Spec.TargetPath) -and
            (Test-PressayUninstallPathEquals -Left $shortcut.WorkingDirectory -Right $Spec.WorkingDirectory) -and
            $shortcut.Arguments -ceq [string]$Spec.Arguments -and
            $shortcut.Description -ceq [string]$Spec.Description
        )
        if ($installed) { return "installed" }
        if (
            $shortcut.Description -cne [string]$Spec.Description -or
            -not (Test-PressayUninstallPathEquals -Left $shortcut.TargetPath -Right $Spec.TargetPath) -or
            [string]::IsNullOrWhiteSpace([string]$shortcut.WorkingDirectory)
        ) {
            return "unmanaged"
        }
        try { $legacyRoot = [System.IO.Path]::GetFullPath([string]$shortcut.WorkingDirectory) }
        catch { return "unmanaged" }
        if ([string]::Equals(
            $legacyRoot,
            [System.IO.Path]::GetPathRoot($legacyRoot),
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            return "unmanaged"
        }
        $legacyLauncher = Join-Path $legacyRoot "scripts\run.ps1"
        $legacyArguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$legacyLauncher`" --background"
        if ($shortcut.Arguments -ceq $legacyArguments) { return "legacy" }
        return "unmanaged"
    }
    catch {
        return "unmanaged"
    }
    finally {
        if ($null -ne $shortcut) {
            [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
        }
        if ($null -ne $shell) {
            [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
        }
    }
}

function Remove-PressayUninstallShortcut {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param(
        [Parameter(Mandatory = $true)][string]$ShortcutPath,
        [Parameter(Mandatory = $true)][psobject]$Spec
    )
    $ownership = Get-PressayUninstallShortcutOwnership -ShortcutPath $ShortcutPath -Spec $Spec
    if ($ownership -eq "missing") { return $true }
    if ($ownership -notin @("installed", "legacy")) {
        Write-Warning "Kept an unmanaged shortcut with the same name: $ShortcutPath"
        return $false
    }
    if ($PSCmdlet.ShouldProcess($ShortcutPath, "Remove managed Pressay shortcut")) {
        Remove-Item -LiteralPath $ShortcutPath -Force
        Write-Host "Shortcut removed: $ShortcutPath"
    }
    return $true
}

function Assert-PressayUninstallRemovalTarget {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedRelativePath,
        [Parameter(Mandatory = $true)][string]$AppDataRoot
    )
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $expected = [System.IO.Path]::GetFullPath((Join-Path $AppDataRoot $ExpectedRelativePath))
    if (-not [string]::Equals($resolved, $expected, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe removal target; refusing removal: $resolved"
    }
    Assert-PressayUninstallPathIsNotReparsePoint -Path $resolved | Out-Null
    return $resolved
}

$layout = Get-PressayUninstallLayout
$destructiveMode = $RemoveApp -or $RemoveRuntime -or $RemoveUserData -or $RemoveInstaller
$appTargets = @()
$runtimeTargets = @()
$userFiles = @()
$installerTargets = @()

if ($RemoveInstaller -and -not $RemoveApp -and (Test-Path -LiteralPath $layout.CurrentFile)) {
    throw "RemoveInstaller requires RemoveApp while Pressay is installed."
}

if ($destructiveMode) {
    $appDataRoot = Assert-PressayUninstallPathIsNotReparsePoint -Path $layout.Root
    if ($RemoveApp) {
        $appTargets = @(
            [pscustomobject]@{ Path = Assert-PressayUninstallRemovalTarget -Path $layout.VersionsRoot -ExpectedRelativePath "app" -AppDataRoot $appDataRoot; Recursive = $true; Description = "Permanently remove installed Pressay versions" },
            [pscustomobject]@{ Path = Assert-PressayUninstallRemovalTarget -Path $layout.CurrentFile -ExpectedRelativePath "current" -AppDataRoot $appDataRoot; Recursive = $false; Description = "Permanently remove the Pressay current-version pointer" },
            [pscustomobject]@{ Path = Assert-PressayUninstallRemovalTarget -Path $layout.LauncherPath -ExpectedRelativePath "Pressay.ps1" -AppDataRoot $appDataRoot; Recursive = $false; Description = "Permanently remove the Pressay launcher" },
            [pscustomobject]@{ Path = Assert-PressayUninstallRemovalTarget -Path $layout.IconPath -ExpectedRelativePath "pressay.ico" -AppDataRoot $appDataRoot; Recursive = $false; Description = "Permanently remove the Pressay icon" }
        )
    }
    if ($RemoveRuntime) {
        $runtimeTargets = @(
            [pscustomobject]@{ Path = Assert-PressayUninstallRemovalTarget -Path $layout.RuntimeVersionsRoot -ExpectedRelativePath "runtime" -AppDataRoot $appDataRoot; Recursive = $true; Description = "Permanently remove versioned Pressay runtimes" },
            [pscustomobject]@{ Path = Assert-PressayUninstallRemovalTarget -Path $layout.LegacyRuntimeRoot -ExpectedRelativePath "venv" -AppDataRoot $appDataRoot; Recursive = $true; Description = "Permanently remove the legacy Pressay runtime" }
        )
    }
    if ($RemoveUserData) {
        $userFiles = @(
            (Assert-PressayUninstallRemovalTarget -Path (Join-Path $appDataRoot "config.json") -ExpectedRelativePath "config.json" -AppDataRoot $appDataRoot),
            (Assert-PressayUninstallRemovalTarget -Path (Join-Path $appDataRoot "pressay.log") -ExpectedRelativePath "pressay.log" -AppDataRoot $appDataRoot),
            (Assert-PressayUninstallRemovalTarget -Path (Join-Path $appDataRoot "pressay.log.1") -ExpectedRelativePath "pressay.log.1" -AppDataRoot $appDataRoot),
            (Assert-PressayUninstallRemovalTarget -Path (Join-Path $appDataRoot "pressay.log.2") -ExpectedRelativePath "pressay.log.2" -AppDataRoot $appDataRoot),
            (Assert-PressayUninstallRemovalTarget -Path (Join-Path $appDataRoot "pressay.log.3") -ExpectedRelativePath "pressay.log.3" -AppDataRoot $appDataRoot)
        )
    }
    if ($RemoveInstaller) {
        $installerTargets = @(
            (Assert-PressayUninstallRemovalTarget -Path $layout.UninstallerPath -ExpectedRelativePath "Uninstall-Pressay.ps1" -AppDataRoot $appDataRoot)
        )
    }
    foreach ($target in @($appTargets + $runtimeTargets)) {
        if ($target.Recursive -and (Test-Path -LiteralPath $target.Path -PathType Container)) {
            Get-PressayUninstallSafeTreeFiles -Root $target.Path | Out-Null
        }
    }
}

$installerGuard = $null
$appGuard = $null
try {
    if ($destructiveMode -and -not $WhatIfPreference) {
        $installerGuard = Enter-PressayUninstallGuard `
            -MutexName "Local\Pressay.Desktop.Installer" `
            -ConflictMessage "Another Pressay installation or upgrade is already running."
        $appGuard = Enter-PressayUninstallGuard `
            -MutexName "Local\Pressay.Desktop.Singleton" `
            -ConflictMessage "Pressay is running. Exit it from the tray menu before installing, upgrading or removing files."
    }

    $spec = Get-PressayUninstallLauncherSpec -Layout $layout
    $directories = @()
    if ($PSBoundParameters.ContainsKey("ShortcutDirectories")) {
        $directories = @($ShortcutDirectories)
    }
    else {
        foreach ($specialFolder in @("Programs", "Desktop", "Startup")) {
            $directory = [Environment]::GetFolderPath($specialFolder)
            if ([string]::IsNullOrWhiteSpace($directory) -or -not [System.IO.Path]::IsPathRooted($directory)) {
                Write-Warning "Skipped unavailable Windows folder: $specialFolder"
                continue
            }
            $directories += $directory
        }
    }
    foreach ($directory in $directories) {
        if ([string]::IsNullOrWhiteSpace($directory) -or -not [System.IO.Path]::IsPathRooted($directory)) {
            throw "Unsafe shortcut directory: $directory"
        }
        $shortcutPath = Join-Path ([System.IO.Path]::GetFullPath($directory)) "Pressay.lnk"
        Remove-PressayUninstallShortcut `
            -ShortcutPath $shortcutPath `
            -Spec $spec `
            -WhatIf:$WhatIfPreference | Out-Null
    }

    foreach ($target in @($appTargets + $runtimeTargets)) {
        if ((Test-Path -LiteralPath $target.Path) -and $PSCmdlet.ShouldProcess($target.Path, $target.Description)) {
            if ($target.Recursive) { Remove-Item -LiteralPath $target.Path -Recurse -Force }
            else { Remove-Item -LiteralPath $target.Path -Force }
            Write-Host "Installed artifact removed: $($target.Path)"
        }
    }
    if ($RemoveUserData) {
        Write-Warning "Configuration and logs selected with -RemoveUserData cannot be recovered."
    }
    foreach ($userFile in $userFiles) {
        if ((Test-Path -LiteralPath $userFile) -and $PSCmdlet.ShouldProcess($userFile, "Permanently remove Pressay user data")) {
            Remove-Item -LiteralPath $userFile -Force
            Write-Host "User data removed: $userFile"
        }
    }
    foreach ($installerTarget in $installerTargets) {
        if ((Test-Path -LiteralPath $installerTarget) -and $PSCmdlet.ShouldProcess($installerTarget, "Permanently remove the installed Pressay uninstaller")) {
            Remove-Item -LiteralPath $installerTarget -Force
            Write-Host "Installed uninstaller removed: $installerTarget"
        }
    }
    if (-not $destructiveMode) {
        Write-Host "Pressay shortcuts were removed. Installed versions, runtimes, configuration, logs, models and the uninstaller were preserved."
    }
}
finally {
    Exit-PressayUninstallGuard -Guard $appGuard
    Exit-PressayUninstallGuard -Guard $installerGuard
}
