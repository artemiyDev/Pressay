# Pressay installed launcher. This file is copied to the stable per-user install root.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    throw "LOCALAPPDATA is unavailable."
}

$localRoot = [System.IO.Path]::GetFullPath($env:LOCALAPPDATA).TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
)
$filesystemRoot = [System.IO.Path]::GetPathRoot($localRoot)
if ([string]::Equals($localRoot, $filesystemRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "LOCALAPPDATA resolves to a filesystem root."
}
$installRoot = [System.IO.Path]::GetFullPath((Join-Path $localRoot "Pressay"))
$expectedPrefix = $localRoot + [System.IO.Path]::DirectorySeparatorChar
if (
    -not $installRoot.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
    (Split-Path -Leaf $installRoot) -cne "Pressay"
) {
    throw "Unsafe Pressay install root: $installRoot"
}

function Assert-PressayLauncherPathIsNotReparsePoint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $resolved = [System.IO.Path]::GetFullPath($Path)
    try {
        $item = Get-Item -LiteralPath $resolved -Force -ErrorAction Stop
    }
    catch [System.Management.Automation.ItemNotFoundException] {
        return $resolved
    }
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Pressay refuses to use a symbolic link or reparse point: $resolved"
    }
    return $resolved
}

function Get-PressayLauncherTreeFiles {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root
    )

    $rootPath = Assert-PressayLauncherPathIsNotReparsePoint -Path $Root
    $directories = New-Object 'System.Collections.Generic.Queue[string]'
    $files = New-Object 'System.Collections.Generic.List[System.IO.FileInfo]'
    $directories.Enqueue($rootPath)
    while ($directories.Count -gt 0) {
        $directory = $directories.Dequeue()
        foreach ($item in @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)) {
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Pressay payload contains a symbolic link or reparse point: $($item.FullName)"
            }
            if ($item.PSIsContainer) {
                $directories.Enqueue([System.IO.Path]::GetFullPath($item.FullName))
            }
            elseif ($item -is [System.IO.FileInfo]) {
                $files.Add($item)
            }
            else {
                throw "Pressay payload contains an unsupported filesystem item: $($item.FullName)"
            }
        }
    }
    return $files.ToArray()
}

function Get-PressayLauncherFileSha256 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $stream = $null
    $sha256 = $null
    try {
        $stream = [System.IO.File]::Open(
            [System.IO.Path]::GetFullPath($Path),
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read
        )
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        $hash = $sha256.ComputeHash($stream)
        return [System.BitConverter]::ToString($hash).Replace("-", "").ToLowerInvariant()
    }
    finally {
        if ($null -ne $sha256) {
            $sha256.Dispose()
        }
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Assert-PressayLauncherPayloadManifest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PayloadRoot,

        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $root = Assert-PressayLauncherPathIsNotReparsePoint -Path $PayloadRoot
    $manifestPath = Join-Path $root ".pressay-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Pressay payload manifest is missing: $manifestPath"
    }
    Assert-PressayLauncherPathIsNotReparsePoint -Path $manifestPath | Out-Null
    try {
        $manifest = [System.IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
    }
    catch {
        throw "Pressay payload manifest is unreadable: $manifestPath"
    }
    if ([string]$manifest.version -cne $Version) {
        throw "Pressay payload manifest version does not match $Version."
    }

    $expected = @{}
    foreach ($entry in @($manifest.files)) {
        $relative = [string]$entry.path
        $hash = [string]$entry.sha256
        if (
            [string]::IsNullOrWhiteSpace($relative) -or
            [System.IO.Path]::IsPathRooted($relative) -or
            $relative.Contains('\') -or
            $relative.StartsWith('/') -or
            $relative.EndsWith('/') -or
            $relative.Contains('//') -or
            $relative -match '(^|/)\.\.?(/|$)' -or
            $hash -notmatch '^[0-9a-fA-F]{64}$' -or
            $expected.ContainsKey($relative)
        ) {
            throw "Pressay payload manifest contains an unsafe entry."
        }
        $expected[$relative] = $hash.ToLowerInvariant()
    }

    $actualFiles = @(
        Get-PressayLauncherTreeFiles -Root $root |
            Where-Object {
                -not [string]::Equals(
                    $_.FullName,
                    $manifestPath,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            }
    )
    if ($actualFiles.Count -ne $expected.Count) {
        throw "Pressay payload files do not match its manifest."
    }
    $prefix = $root.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    foreach ($file in $actualFiles) {
        $fullPath = [System.IO.Path]::GetFullPath($file.FullName)
        if (-not $fullPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Pressay payload file escaped its root: $fullPath"
        }
        $relative = $fullPath.Substring($prefix.Length).Replace('\', '/')
        if (-not $expected.ContainsKey($relative)) {
            throw "Pressay payload contains an unmanifested file: $relative"
        }
        $actualHash = Get-PressayLauncherFileSha256 -Path $fullPath
        if ($actualHash -cne $expected[$relative]) {
            throw "Pressay payload failed integrity validation: $relative"
        }
    }
}

Assert-PressayLauncherPathIsNotReparsePoint -Path $installRoot | Out-Null

$currentFile = Join-Path $installRoot "current"
if (-not (Test-Path -LiteralPath $currentFile -PathType Leaf)) {
    throw "Pressay is not installed. Run .\scripts\install.ps1 first."
}
Assert-PressayLauncherPathIsNotReparsePoint -Path $currentFile | Out-Null
$version = [System.IO.File]::ReadAllText($currentFile).Trim()
if ($version -notmatch '^[0-9][0-9A-Za-z]*(?:[._-][0-9A-Za-z]+)*$') {
    throw "Pressay current-version pointer is invalid."
}

$versionsRoot = [System.IO.Path]::GetFullPath((Join-Path $installRoot "app"))
$versionsRoot = Assert-PressayLauncherPathIsNotReparsePoint -Path $versionsRoot
$payloadRoot = [System.IO.Path]::GetFullPath((Join-Path $versionsRoot $version))
$payloadParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $payloadRoot))
if (-not [string]::Equals($payloadParent, $versionsRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Pressay current-version pointer escaped the app directory."
}
$versionMarker = Join-Path $payloadRoot ".pressay-version"
$sourceRoot = Join-Path $payloadRoot "src"
$packageMain = Join-Path $sourceRoot "pressay\__main__.py"
if (
    -not (Test-Path -LiteralPath $versionMarker -PathType Leaf) -or
    [System.IO.File]::ReadAllText($versionMarker).Trim() -cne $version -or
    -not (Test-Path -LiteralPath $packageMain -PathType Leaf)
) {
    throw "Pressay installed payload is missing or incomplete: $payloadRoot"
}
Assert-PressayLauncherPayloadManifest -PayloadRoot $payloadRoot -Version $version

$runtimeRoot = Assert-PressayLauncherPathIsNotReparsePoint -Path (Join-Path $installRoot "venv")
$venvPython = Join-Path $runtimeRoot "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Pressay runtime is missing. Run .\scripts\install.ps1 first."
}
Assert-PressayLauncherPathIsNotReparsePoint -Path $venvPython | Out-Null

$env:PYTHONPATH = $sourceRoot
$env:PYTHONDONTWRITEBYTECODE = "1"
$sitePackages = Join-Path $runtimeRoot "Lib\site-packages"
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

$venvPythonw = Join-Path $runtimeRoot "Scripts\pythonw.exe"
if ($args -contains "--background") {
    if (-not (Test-Path -LiteralPath $venvPythonw -PathType Leaf)) {
        [Console]::Error.WriteLine("Background Python launcher is missing: $venvPythonw")
        exit 1
    }
    Assert-PressayLauncherPathIsNotReparsePoint -Path $venvPythonw | Out-Null

    try {
        $backgroundProcess = Start-Process `
            -FilePath $venvPythonw `
            -ArgumentList (@("-m", "pressay") + @($args)) `
            -WorkingDirectory $payloadRoot `
            -WindowStyle Hidden `
            -PassThru `
            -ErrorAction Stop
    }
    catch {
        [Console]::Error.WriteLine("Failed to launch Pressay in background: $($_.Exception.Message)")
        exit 1
    }

    if ($backgroundProcess.WaitForExit(1500)) {
        $backgroundExitCode = [int]$backgroundProcess.ExitCode
        if ($backgroundExitCode -ne 0) {
            [Console]::Error.WriteLine("Pressay background process exited during startup (code $backgroundExitCode).")
            exit $backgroundExitCode
        }
    }
    exit 0
}

& $venvPython -m pressay @args
$foregroundExitCode = [int]$LASTEXITCODE
exit $foregroundExitCode
