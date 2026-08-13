Set-StrictMode -Version Latest

$script:PressayShortcutDescription = "Local Pressay voice dictation"

function Get-PressayLauncherSpec {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot
    )

    $resolvedProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
    $runScript = Join-Path $resolvedProjectRoot "scripts\run.ps1"
    if (-not (Test-Path -LiteralPath $runScript -PathType Leaf)) {
        throw "Pressay launcher is missing: $runScript"
    }

    $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
    [pscustomobject]@{
        TargetPath       = [System.IO.Path]::GetFullPath($powershell)
        Arguments        = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runScript`" --background"
        WorkingDirectory = $resolvedProjectRoot
        Description      = $script:PressayShortcutDescription
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

    if (-not (Test-Path -LiteralPath $ShortcutPath -PathType Leaf)) {
        return $false
    }

    $shell = $null
    $shortcut = $null
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($ShortcutPath)

        $targetMatches = [string]::Equals(
            [System.IO.Path]::GetFullPath($shortcut.TargetPath),
            [System.IO.Path]::GetFullPath([string]$Spec.TargetPath),
            [System.StringComparison]::OrdinalIgnoreCase
        )
        $workingDirectoryMatches = [string]::Equals(
            [System.IO.Path]::GetFullPath($shortcut.WorkingDirectory),
            [System.IO.Path]::GetFullPath([string]$Spec.WorkingDirectory),
            [System.StringComparison]::OrdinalIgnoreCase
        )

        return (
            $targetMatches -and
            $workingDirectoryMatches -and
            $shortcut.Arguments -ceq [string]$Spec.Arguments -and
            $shortcut.Description -ceq [string]$Spec.Description
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
        throw "Refusing to replace an unmanaged shortcut: $ShortcutPath"
    }

    $parentDirectory = Split-Path -Parent $ShortcutPath
    if (-not (Test-Path -LiteralPath $parentDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $parentDirectory -Force | Out-Null
    }

    $shell = $null
    $shortcut = $null
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($ShortcutPath)
        $shortcut.TargetPath = [string]$Spec.TargetPath
        $shortcut.Arguments = [string]$Spec.Arguments
        $shortcut.WorkingDirectory = [string]$Spec.WorkingDirectory
        $shortcut.Description = [string]$Spec.Description
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

    if (-not (Test-PressayShortcut -ShortcutPath $ShortcutPath -Spec $Spec)) {
        throw "Shortcut verification failed: $ShortcutPath"
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
    if (-not (Test-PressayShortcut -ShortcutPath $ShortcutPath -Spec $Spec)) {
        Write-Warning "Kept an unmanaged shortcut with the same name: $ShortcutPath"
        return $false
    }
    if ($PSCmdlet.ShouldProcess($ShortcutPath, "Remove managed Pressay shortcut")) {
        Remove-Item -LiteralPath $ShortcutPath -Force
        Write-Host "Shortcut removed: $ShortcutPath"
    }
    return $true
}
