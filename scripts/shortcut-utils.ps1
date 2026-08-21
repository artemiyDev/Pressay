Set-StrictMode -Version Latest

$script:PressayShortcutDescription = "Local Pressay voice dictation"
. (Join-Path $PSScriptRoot "install-layout.ps1")

function Get-PressayLauncherSpec {
    [CmdletBinding()]
    param(
        [string]$LocalAppData = $env:LOCALAPPDATA
    )

    $layout = Get-PressayInstallLayout -LocalAppData $LocalAppData
    $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
    [pscustomobject]@{
        TargetPath       = [System.IO.Path]::GetFullPath($powershell)
        Arguments        = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$($layout.LauncherPath)`" --background"
        WorkingDirectory = $layout.Root
        Description      = $script:PressayShortcutDescription
        IconLocation     = if (Test-Path -LiteralPath $layout.IconPath -PathType Leaf) { $layout.IconPath } else { "" }
    }
}

function Test-PressayPathEquals {
    param(
        [AllowNull()]
        [string]$Left,

        [AllowNull()]
        [string]$Right
    )

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

function Test-PressayInstalledShortcutObject {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Shortcut,

        [Parameter(Mandatory = $true)]
        [psobject]$Spec
    )

    return (
        (Test-PressayPathEquals -Left $Shortcut.TargetPath -Right ([string]$Spec.TargetPath)) -and
        (Test-PressayPathEquals -Left $Shortcut.WorkingDirectory -Right ([string]$Spec.WorkingDirectory)) -and
        $Shortcut.Arguments -ceq [string]$Spec.Arguments -and
        $Shortcut.Description -ceq [string]$Spec.Description
    )
}

function Test-PressayLegacyShortcutObject {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Shortcut,

        [Parameter(Mandatory = $true)]
        [string]$PowershellPath
    )

    if (
        $Shortcut.Description -cne $script:PressayShortcutDescription -or
        -not (Test-PressayPathEquals -Left $Shortcut.TargetPath -Right $PowershellPath) -or
        [string]::IsNullOrWhiteSpace([string]$Shortcut.WorkingDirectory)
    ) {
        return $false
    }
    try {
        $projectRoot = [System.IO.Path]::GetFullPath([string]$Shortcut.WorkingDirectory)
    }
    catch {
        return $false
    }
    if ([string]::Equals(
        $projectRoot,
        [System.IO.Path]::GetPathRoot($projectRoot),
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        return $false
    }
    $legacyLauncher = Join-Path $projectRoot "scripts\run.ps1"
    $legacyArguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$legacyLauncher`" --background"
    return $Shortcut.Arguments -ceq $legacyArguments
}

function Get-PressayShortcutOwnership {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ShortcutPath,

        [Parameter(Mandatory = $true)]
        [psobject]$Spec
    )

    if (
        -not (Test-Path -LiteralPath $ShortcutPath -PathType Leaf) -or
        (Test-PressayPathIsReparsePoint -Path $ShortcutPath)
    ) {
        return "missing"
    }
    $shell = $null
    $shortcut = $null
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($ShortcutPath)
        if (Test-PressayInstalledShortcutObject -Shortcut $shortcut -Spec $Spec) {
            return "installed"
        }
        if (Test-PressayLegacyShortcutObject -Shortcut $shortcut -PowershellPath ([string]$Spec.TargetPath)) {
            return "legacy"
        }
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

function Test-PressayShortcut {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ShortcutPath,

        [Parameter(Mandatory = $true)]
        [psobject]$Spec
    )

    if (
        -not (Test-Path -LiteralPath $ShortcutPath -PathType Leaf) -or
        (Test-PressayPathIsReparsePoint -Path $ShortcutPath)
    ) {
        return $false
    }

    $shell = $null
    $shortcut = $null
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($ShortcutPath)

        $iconMatches = (
            [string]::IsNullOrWhiteSpace([string]$Spec.IconLocation) -or
            (Test-PressayPathEquals `
                -Left (($shortcut.IconLocation -split ',')[0]) `
                -Right ([string]$Spec.IconLocation))
        )
        return (
            (Test-PressayInstalledShortcutObject -Shortcut $shortcut -Spec $Spec) -and
            $iconMatches
        )
    }
    catch {
        return $false
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

function New-PressayShortcut {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ShortcutPath,

        [Parameter(Mandatory = $true)]
        [psobject]$Spec
    )

    if (Test-Path -LiteralPath $ShortcutPath) {
        if (Test-PressayShortcut -ShortcutPath $ShortcutPath -Spec $Spec) {
            Write-Host "Shortcut is already ready: $ShortcutPath"
            return
        }
        $ownership = Get-PressayShortcutOwnership -ShortcutPath $ShortcutPath -Spec $Spec
        if ($ownership -notin @("installed", "legacy")) {
            throw "Refusing to replace an unmanaged shortcut: $ShortcutPath"
        }
    }

    $parentDirectory = Split-Path -Parent $ShortcutPath
    if (-not (Test-Path -LiteralPath $parentDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $parentDirectory -Force | Out-Null
    }

    $temporaryPath = Join-Path $parentDirectory (
        ".Pressay.{0}.tmp.lnk" -f [guid]::NewGuid().ToString("N")
    )
    $safeTemporary = Assert-PressayDirectChild `
        -Path $temporaryPath `
        -Parent $parentDirectory `
        -RequiredLeafPrefix ".Pressay."
    $backupPath = Join-Path $parentDirectory (
        ".Pressay.{0}.bak.lnk" -f [guid]::NewGuid().ToString("N")
    )
    $safeBackup = Assert-PressayDirectChild `
        -Path $backupPath `
        -Parent $parentDirectory `
        -RequiredLeafPrefix ".Pressay."
    $shell = $null
    $shortcut = $null
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($safeTemporary)
        $shortcut.TargetPath = [string]$Spec.TargetPath
        $shortcut.Arguments = [string]$Spec.Arguments
        $shortcut.WorkingDirectory = [string]$Spec.WorkingDirectory
        $shortcut.Description = [string]$Spec.Description
        if (-not [string]::IsNullOrWhiteSpace([string]$Spec.IconLocation)) {
            $shortcut.IconLocation = [string]$Spec.IconLocation
        }
        $shortcut.Save()
    }
    finally {
        if ($null -ne $shortcut) {
            [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
        }
        if ($null -ne $shell) {
            [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
        }
    }

    $published = $false
    $verified = $false
    $replacingExisting = $false
    try {
        if (-not (Test-PressayShortcut -ShortcutPath $safeTemporary -Spec $Spec)) {
            throw "Shortcut verification failed before publication: $ShortcutPath"
        }
        if (Test-Path -LiteralPath $ShortcutPath -PathType Leaf) {
            $replacingExisting = $true
            Assert-PressayPathIsNotReparsePoint -Path $ShortcutPath | Out-Null
            $ownership = Get-PressayShortcutOwnership -ShortcutPath $ShortcutPath -Spec $Spec
            if ($ownership -notin @("installed", "legacy")) {
                throw "Refusing to replace an unmanaged shortcut: $ShortcutPath"
            }
            Invoke-PressayFileReplace `
                -Source $safeTemporary `
                -Destination $ShortcutPath `
                -Backup $safeBackup
        }
        else {
            [System.IO.File]::Move($safeTemporary, $ShortcutPath)
        }
        $published = $true
        if (-not (Test-PressayShortcut -ShortcutPath $ShortcutPath -Spec $Spec)) {
            throw "Shortcut verification failed after publication: $ShortcutPath"
        }
        $verified = $true
    }
    finally {
        if (-not $verified -and (Test-Path -LiteralPath $safeBackup -PathType Leaf)) {
            try {
                if (
                    (Test-Path -LiteralPath $ShortcutPath -PathType Leaf) -and
                    -not (Test-PressayPathIsReparsePoint -Path $ShortcutPath)
                ) {
                    [System.IO.File]::Delete($ShortcutPath)
                }
                if (-not (Test-Path -LiteralPath $ShortcutPath)) {
                    [System.IO.File]::Move($safeBackup, $ShortcutPath)
                }
            }
            catch {}
        }
        elseif (-not $verified -and $published -and -not $replacingExisting) {
            if (
                (Test-Path -LiteralPath $ShortcutPath -PathType Leaf) -and
                -not (Test-PressayPathIsReparsePoint -Path $ShortcutPath)
            ) {
                try { [System.IO.File]::Delete($ShortcutPath) } catch {}
            }
        }
        if (Test-Path -LiteralPath $safeTemporary -PathType Leaf) {
            try { [System.IO.File]::Delete($safeTemporary) } catch {}
        }
        if ($verified -and (Test-Path -LiteralPath $safeBackup -PathType Leaf)) {
            try { [System.IO.File]::Delete($safeBackup) } catch {}
        }
    }
    Write-Host "Shortcut created: $ShortcutPath"
}

function Remove-PressayShortcut {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ShortcutPath,

        [Parameter(Mandatory = $true)]
        [psobject]$Spec
    )

    if (-not (Test-Path -LiteralPath $ShortcutPath)) {
        return $true
    }
    $ownership = Get-PressayShortcutOwnership -ShortcutPath $ShortcutPath -Spec $Spec
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
