Set-StrictMode -Version Latest

$script:PressayPayloadManifestName = ".pressay-manifest.json"
$script:PressayPayloadVersionName = ".pressay-version"
$script:PressayRuntimeContractName = ".pressay-runtime.json"
$script:PressayInstalledLauncherName = "Pressay.ps1"
$script:PressayInstalledUninstallerName = "Uninstall-Pressay.ps1"

function Get-PressayWindowsRuntimeDependencySpecs {
    [CmdletBinding()]
    param()

    return [string[]]@(
        "faster-whisper>=1.2.1,<2",
        "numpy>=1.26,<3",
        "PySide6>=6.8,<7",
        "pywin32>=306",
        "sounddevice>=0.5.1,<1",
        "nvidia-cublas-cu12>=12,<13",
        "nvidia-cudnn-cu12>=9,<10",
        "uiautomation>=2.0,<3",
        "pytest>=8.3,<10",
        "pytest-cov>=6,<8"
    )
}

function Get-PressayWindowsRuntimeContractHash {
    [CmdletBinding()]
    param()

    $canonical = @(
        "pressay-windows-runtime-v1",
        "python=cp311-win_amd64"
    ) + @(Get-PressayWindowsRuntimeDependencySpecs)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(
        (($canonical -join "`n") + "`n")
    )
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha256.ComputeHash($bytes)
        return [System.BitConverter]::ToString($hash).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
    }
}

function Get-PressayInstallLayout {
    [CmdletBinding()]
    param(
        [string]$LocalAppData = $env:LOCALAPPDATA
    )

    if ([string]::IsNullOrWhiteSpace($LocalAppData)) {
        throw "LOCALAPPDATA is unavailable."
    }

    $localRoot = [System.IO.Path]::GetFullPath($LocalAppData).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $filesystemRoot = [System.IO.Path]::GetPathRoot($localRoot)
    if ([string]::Equals($localRoot, $filesystemRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "LOCALAPPDATA resolves to a filesystem root: $localRoot"
    }

    $root = [System.IO.Path]::GetFullPath((Join-Path $localRoot "Pressay"))
    $expectedPrefix = $localRoot + [System.IO.Path]::DirectorySeparatorChar
    if (
        -not $root.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $root) -cne "Pressay"
    ) {
        throw "Unsafe Pressay install root: $root"
    }

    [pscustomobject]@{
        LocalAppData = $localRoot
        Root = $root
        VersionsRoot = [System.IO.Path]::GetFullPath((Join-Path $root "app"))
        RuntimeVersionsRoot = [System.IO.Path]::GetFullPath((Join-Path $root "runtime"))
        CurrentFile = [System.IO.Path]::GetFullPath((Join-Path $root "current"))
        LauncherPath = [System.IO.Path]::GetFullPath((Join-Path $root $script:PressayInstalledLauncherName))
        UninstallerPath = [System.IO.Path]::GetFullPath((Join-Path $root $script:PressayInstalledUninstallerName))
        LegacyRuntimeRoot = [System.IO.Path]::GetFullPath((Join-Path $root "venv"))
        # Compatibility alias for older local tooling. Installation code must
        # use RuntimeVersionsRoot for all new releases.
        RuntimeRoot = [System.IO.Path]::GetFullPath((Join-Path $root "venv"))
        IconPath = [System.IO.Path]::GetFullPath((Join-Path $root "pressay.ico"))
    }
}

function Assert-PressayVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    if ($Version -notmatch '^[0-9][0-9A-Za-z]*(?:[._-][0-9A-Za-z]+)*$') {
        throw "Unsafe Pressay version identifier: $Version"
    }
    return $Version
}

function New-PressayRuntimeContract {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $safeVersion = Assert-PressayVersion -Version $Version
    return [ordered]@{
        schema = 1
        version = $safeVersion
        dependency_contract_sha256 = Get-PressayWindowsRuntimeContractHash
        python_tag = "cp311-win_amd64"
    }
}

function ConvertTo-PressayRuntimeContractJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    return ((New-PressayRuntimeContract -Version $Version) | ConvertTo-Json -Compress) + [Environment]::NewLine
}

function Read-PressayRuntimeContract {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Version,

        [switch]$AllowAnyContractHash
    )

    $safeVersion = Assert-PressayVersion -Version $Version
    $resolved = Assert-PressayPathIsNotReparsePoint -Path $Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "Pressay runtime contract is missing: $resolved"
    }
    try {
        $contract = [System.IO.File]::ReadAllText($resolved) | ConvertFrom-Json
    }
    catch {
        throw "Pressay runtime contract is unreadable: $resolved"
    }
    if (
        [int]$contract.schema -ne 1 -or
        [string]$contract.version -cne $safeVersion -or
        [string]$contract.dependency_contract_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$contract.python_tag -cne "cp311-win_amd64"
    ) {
        throw "Pressay runtime contract does not match release $safeVersion."
    }
    if (
        -not $AllowAnyContractHash -and
        [string]$contract.dependency_contract_sha256 -cne
            (Get-PressayWindowsRuntimeContractHash)
    ) {
        throw "Pressay dependency contract does not match release $safeVersion."
    }
    return $contract
}

function Get-PressayProjectVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot
    )

    $project = [System.IO.Path]::GetFullPath($ProjectRoot)
    $pyproject = Join-Path $project "pyproject.toml"
    if (-not (Test-Path -LiteralPath $pyproject -PathType Leaf)) {
        throw "Pressay package metadata is missing: $pyproject"
    }
    $content = [System.IO.File]::ReadAllText($pyproject)
    $match = [regex]::Match(
        $content,
        '(?m)^\s*version\s*=\s*"([^"]+)"\s*$'
    )
    if (-not $match.Success) {
        throw "Pressay version is missing from pyproject.toml."
    }
    return Assert-PressayVersion -Version $match.Groups[1].Value
}

function Assert-PressayNotRunning {
    [CmdletBinding()]
    param(
        [string]$MutexName = "Local\Pressay.Desktop.Singleton"
    )

    if ([string]::IsNullOrWhiteSpace($MutexName)) {
        throw "Pressay mutex name is unavailable."
    }

    $appMutex = $null
    try {
        $appMutex = [System.Threading.Mutex]::OpenExisting($MutexName)
    }
    catch [System.Threading.WaitHandleCannotBeOpenedException] {
        return
    }
    finally {
        if ($null -ne $appMutex) {
            $appMutex.Dispose()
        }
    }
    throw "Pressay is running. Exit it from the tray menu before installing or upgrading."
}

function Enter-PressayExclusiveGuard {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$MutexName,

        [Parameter(Mandatory = $true)]
        [string]$ConflictMessage
    )

    if ([string]::IsNullOrWhiteSpace($MutexName)) {
        throw "Pressay guard name is unavailable."
    }

    $createdNew = $false
    $guard = $null
    try {
        $guard = New-Object System.Threading.Mutex($false, $MutexName, [ref]$createdNew)
        if (-not $createdNew) {
            throw $ConflictMessage
        }
        return $guard
    }
    catch {
        if ($null -ne $guard) {
            $guard.Dispose()
        }
        throw
    }
}

function Enter-PressayInstallerGuard {
    [CmdletBinding()]
    param(
        [string]$MutexName = "Local\Pressay.Desktop.Installer"
    )

    return Enter-PressayExclusiveGuard `
        -MutexName $MutexName `
        -ConflictMessage "Another Pressay installation or upgrade is already running."
}

function Enter-PressayAppMaintenanceGuard {
    [CmdletBinding()]
    param(
        [string]$MutexName = "Local\Pressay.Desktop.Singleton"
    )

    return Enter-PressayExclusiveGuard `
        -MutexName $MutexName `
        -ConflictMessage "Pressay is running. Exit it from the tray menu before installing, upgrading or removing files."
}

function Exit-PressayGuard {
    [CmdletBinding()]
    param(
        [AllowNull()]
        [System.Threading.Mutex]$Guard
    )

    if ($null -ne $Guard) {
        $Guard.Dispose()
    }
}

function Test-PressayPathIsReparsePoint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    try {
        $item = Get-Item -LiteralPath ([System.IO.Path]::GetFullPath($Path)) -Force -ErrorAction Stop
    }
    catch [System.Management.Automation.ItemNotFoundException] {
        return $false
    }
    return (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Assert-PressayPathIsNotReparsePoint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (Test-PressayPathIsReparsePoint -Path $resolved) {
        throw "Pressay path must not be a symbolic link or reparse point: $resolved"
    }
    return $resolved
}

function Assert-PressayInstallLayoutSafety {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Layout
    )

    foreach ($path in @(
        $Layout.Root,
        $Layout.VersionsRoot,
        $Layout.RuntimeVersionsRoot,
        $Layout.CurrentFile,
        $Layout.LauncherPath,
        $Layout.UninstallerPath,
        $Layout.LegacyRuntimeRoot,
        $Layout.RuntimeRoot,
        $Layout.IconPath
    )) {
        Assert-PressayPathIsNotReparsePoint -Path ([string]$path) | Out-Null
    }
    return $Layout
}

function Assert-PressayDirectChild {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Parent,

        [string]$RequiredLeafPrefix = ""
    )

    $resolved = [System.IO.Path]::GetFullPath($Path)
    $resolvedParent = [System.IO.Path]::GetFullPath($Parent).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $actualParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $resolved)).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    if (-not [string]::Equals($actualParent, $resolvedParent, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is not a direct child of the expected directory: $resolved"
    }
    $leaf = Split-Path -Leaf $resolved
    if (
        [string]::IsNullOrWhiteSpace($leaf) -or
        (
            -not [string]::IsNullOrEmpty($RequiredLeafPrefix) -and
            -not $leaf.StartsWith($RequiredLeafPrefix, [System.StringComparison]::Ordinal)
        )
    ) {
        throw "Unsafe child path: $resolved"
    }
    return $resolved
}

function Get-PressayPayloadRelativePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PayloadRoot,

        [Parameter(Mandatory = $true)]
        [string]$FilePath
    )

    $root = [System.IO.Path]::GetFullPath($PayloadRoot).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $file = [System.IO.Path]::GetFullPath($FilePath)
    $prefix = $root + [System.IO.Path]::DirectorySeparatorChar
    if (-not $file.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Payload file escaped its root: $file"
    }
    return $file.Substring($prefix.Length).Replace('\', '/')
}

function Get-PressaySafeTreeFiles {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root
    )

    $resolvedRoot = Assert-PressayPathIsNotReparsePoint -Path $Root
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

function Get-PressayFileSha256 {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $resolved = [System.IO.Path]::GetFullPath($Path)
    $stream = $null
    $sha256 = $null
    try {
        $stream = [System.IO.File]::Open(
            $resolved,
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

function Write-PressayPayloadManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$PayloadRoot,

        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $safeVersion = Assert-PressayVersion -Version $Version
    $root = [System.IO.Path]::GetFullPath($PayloadRoot)
    $manifestPath = Join-Path $root $script:PressayPayloadManifestName
    $files = @(
        Get-PressaySafeTreeFiles -Root $root |
            Where-Object { $_.FullName -cne $manifestPath } |
            ForEach-Object {
                [ordered]@{
                    path = Get-PressayPayloadRelativePath -PayloadRoot $root -FilePath $_.FullName
                    sha256 = Get-PressayFileSha256 -Path $_.FullName
                }
            } |
            Sort-Object { $_.path }
    )
    $manifest = [ordered]@{
        version = $safeVersion
        files = $files
    }
    $json = $manifest | ConvertTo-Json -Depth 4
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($manifestPath, $json + [Environment]::NewLine, $encoding)
    return $manifestPath
}

function Assert-PressayPayload {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$PayloadRoot,

        [Parameter(Mandatory = $true)]
        [string]$Version,

        [switch]$AllowLegacyRuntimeContract,

        [switch]$AllowAnyRuntimeContractHash
    )

    $safeVersion = Assert-PressayVersion -Version $Version
    $root = [System.IO.Path]::GetFullPath($PayloadRoot)
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "Pressay payload is missing: $root"
    }

    $required = @(
        "src\pressay\__init__.py",
        "src\pressay\__main__.py",
        "src\pressay\assets\app-icon.svg",
        "LICENSE",
        $script:PressayPayloadVersionName,
        $script:PressayPayloadManifestName
    )
    if (-not $AllowLegacyRuntimeContract) {
        $required += $script:PressayRuntimeContractName
    }
    foreach ($relative in $required) {
        $requiredPath = Join-Path $root $relative
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Pressay payload is incomplete; missing $relative"
        }
    }

    $marker = [System.IO.File]::ReadAllText(
        (Join-Path $root $script:PressayPayloadVersionName)
    ).Trim()
    if ($marker -cne $safeVersion) {
        throw "Pressay payload version marker does not match $safeVersion."
    }
    $runtimeContractPath = Join-Path $root $script:PressayRuntimeContractName
    if (Test-Path -LiteralPath $runtimeContractPath -PathType Leaf) {
        Read-PressayRuntimeContract `
            -Path $runtimeContractPath `
            -Version $safeVersion `
            -AllowAnyContractHash:$AllowAnyRuntimeContractHash | Out-Null
    }
    elseif (-not $AllowLegacyRuntimeContract) {
        throw "Pressay payload runtime contract is missing: $runtimeContractPath"
    }

    $manifestPath = Join-Path $root $script:PressayPayloadManifestName
    try {
        $manifest = [System.IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
    }
    catch {
        throw "Pressay payload manifest is unreadable: $manifestPath"
    }
    if ([string]$manifest.version -cne $safeVersion) {
        throw "Pressay payload manifest version does not match $safeVersion."
    }

    $expected = @{}
    foreach ($entry in @($manifest.files)) {
        $relative = [string]$entry.path
        if (
            [string]::IsNullOrWhiteSpace($relative) -or
            [System.IO.Path]::IsPathRooted($relative) -or
            $relative -match '(^|/)\.\.(/|$)' -or
            $expected.ContainsKey($relative)
        ) {
            throw "Pressay payload manifest contains an unsafe path."
        }
        $expected[$relative] = ([string]$entry.sha256).ToLowerInvariant()
    }

    $actualFiles = @(
        Get-PressaySafeTreeFiles -Root $root |
            Where-Object { $_.FullName -cne $manifestPath }
    )
    if ($actualFiles.Count -ne $expected.Count) {
        throw "Pressay payload files do not match its manifest."
    }
    foreach ($file in $actualFiles) {
        $relative = Get-PressayPayloadRelativePath -PayloadRoot $root -FilePath $file.FullName
        if (-not $expected.ContainsKey($relative)) {
            throw "Pressay payload contains an unmanifested file: $relative"
        }
        $actualHash = Get-PressayFileSha256 -Path $file.FullName
        if ($actualHash -cne $expected[$relative]) {
            throw "Pressay payload file failed integrity validation: $relative"
        }
    }
    return $root
}

function Remove-PressayStagingPayload {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param(
        [Parameter(Mandatory = $true)]
        [string]$StagePath,

        [Parameter(Mandatory = $true)]
        [string]$VersionsRoot
    )

    $safeStage = Assert-PressayDirectChild `
        -Path $StagePath `
        -Parent $VersionsRoot `
        -RequiredLeafPrefix ".staging-"
    if (
        (Test-Path -LiteralPath $safeStage) -and
        $PSCmdlet.ShouldProcess($safeStage, "Remove incomplete Pressay payload")
    ) {
        Remove-Item -LiteralPath $safeStage -Recurse -Force
    }
}

function New-PressayPayloadStage {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,

        [Parameter(Mandatory = $true)]
        [psobject]$Layout,

        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $safeVersion = Assert-PressayVersion -Version $Version
    Assert-PressayInstallLayoutSafety -Layout $Layout | Out-Null
    $project = [System.IO.Path]::GetFullPath($ProjectRoot)
    $sourcePackage = Join-Path $project "src\pressay"
    $license = Join-Path $project "LICENSE"
    if (-not (Test-Path -LiteralPath (Join-Path $sourcePackage "__main__.py") -PathType Leaf)) {
        throw "Pressay source package is incomplete: $sourcePackage"
    }
    if (-not (Test-Path -LiteralPath $license -PathType Leaf)) {
        throw "Pressay license is missing: $license"
    }

    New-Item -ItemType Directory -Path $Layout.VersionsRoot -Force | Out-Null
    $stagePath = Join-Path $Layout.VersionsRoot (
        ".staging-{0}-{1}" -f $safeVersion, [guid]::NewGuid().ToString("N")
    )
    $safeStage = Assert-PressayDirectChild `
        -Path $stagePath `
        -Parent $Layout.VersionsRoot `
        -RequiredLeafPrefix ".staging-"
    New-Item -ItemType Directory -Path $safeStage | Out-Null

    try {
        $sourceRoot = [System.IO.Path]::GetFullPath($sourcePackage).TrimEnd(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
        $sourcePrefix = $sourceRoot + [System.IO.Path]::DirectorySeparatorChar
        foreach ($file in @(Get-PressaySafeTreeFiles -Root $sourceRoot)) {
            $fullSource = [System.IO.Path]::GetFullPath($file.FullName)
            if (-not $fullSource.StartsWith($sourcePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Source file escaped the Pressay package root: $fullSource"
            }
            $relative = $fullSource.Substring($sourcePrefix.Length)
            $isPythonSource = $file.Extension -ceq ".py"
            $isPackagedAsset = (
                $relative -match '^assets[\\/][^\\/]+\.(?:svg|md)$'
            )
            if (-not $isPythonSource -and -not $isPackagedAsset) {
                continue
            }
            $target = Join-Path (Join-Path $safeStage "src\pressay") $relative
            $targetParent = Split-Path -Parent $target
            if (-not (Test-Path -LiteralPath $targetParent -PathType Container)) {
                New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
            }
            Copy-Item -LiteralPath $fullSource -Destination $target
        }
        Copy-Item -LiteralPath $license -Destination (Join-Path $safeStage "LICENSE")

        $encoding = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText(
            (Join-Path $safeStage $script:PressayPayloadVersionName),
            $safeVersion + [Environment]::NewLine,
            $encoding
        )
        [System.IO.File]::WriteAllText(
            (Join-Path $safeStage $script:PressayRuntimeContractName),
            (ConvertTo-PressayRuntimeContractJson -Version $safeVersion),
            $encoding
        )
        Write-PressayPayloadManifest -PayloadRoot $safeStage -Version $safeVersion | Out-Null
        Assert-PressayPayload -PayloadRoot $safeStage -Version $safeVersion | Out-Null
        return $safeStage
    }
    catch {
        Remove-PressayStagingPayload `
            -StagePath $safeStage `
            -VersionsRoot $Layout.VersionsRoot `
            -Confirm:$false
        throw
    }
}

function Publish-PressayPayload {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$StagePath,

        [Parameter(Mandatory = $true)]
        [psobject]$Layout,

        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $safeVersion = Assert-PressayVersion -Version $Version
    $safeStage = Assert-PressayDirectChild `
        -Path $StagePath `
        -Parent $Layout.VersionsRoot `
        -RequiredLeafPrefix ".staging-"
    Assert-PressayPayload -PayloadRoot $safeStage -Version $safeVersion | Out-Null
    $versionRoot = Assert-PressayDirectChild `
        -Path (Join-Path $Layout.VersionsRoot $safeVersion) `
        -Parent $Layout.VersionsRoot

    if (Test-Path -LiteralPath $versionRoot) {
        Assert-PressayPayload -PayloadRoot $versionRoot -Version $safeVersion | Out-Null
        $stageManifest = Get-PressayFileSha256 `
            -Path (Join-Path $safeStage $script:PressayPayloadManifestName)
        $publishedManifest = Get-PressayFileSha256 `
            -Path (Join-Path $versionRoot $script:PressayPayloadManifestName)
        if ($stageManifest -cne $publishedManifest) {
            throw "Pressay $safeVersion is already installed with a different payload; bump the version."
        }
        Remove-PressayStagingPayload `
            -StagePath $safeStage `
            -VersionsRoot $Layout.VersionsRoot `
            -Confirm:$false
        return $versionRoot
    }

    [System.IO.Directory]::Move($safeStage, $versionRoot)
    Assert-PressayPayload -PayloadRoot $versionRoot -Version $safeVersion | Out-Null
    return $versionRoot
}

function Install-PressayPayload {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,

        [Parameter(Mandatory = $true)]
        [psobject]$Layout,

        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $stage = $null
    try {
        $stage = New-PressayPayloadStage `
            -ProjectRoot $ProjectRoot `
            -Layout $Layout `
            -Version $Version
        $published = Publish-PressayPayload `
            -StagePath $stage `
            -Layout $Layout `
            -Version $Version
        $stage = $null
        return $published
    }
    finally {
        if ($null -ne $stage -and (Test-Path -LiteralPath $stage)) {
            Remove-PressayStagingPayload `
                -StagePath $stage `
                -VersionsRoot $Layout.VersionsRoot `
                -Confirm:$false
        }
    }
}

function Get-PressayRuntimeVersionRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Layout,

        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $safeVersion = Assert-PressayVersion -Version $Version
    return Assert-PressayDirectChild `
        -Path (Join-Path $Layout.RuntimeVersionsRoot $safeVersion) `
        -Parent $Layout.RuntimeVersionsRoot
}

function Test-PressayVersionIsCurrent {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Layout,

        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $safeVersion = Assert-PressayVersion -Version $Version
    if (-not (Test-Path -LiteralPath $Layout.CurrentFile -PathType Leaf)) {
        return $false
    }
    Assert-PressayPathIsNotReparsePoint -Path $Layout.CurrentFile | Out-Null
    try {
        $current = Assert-PressayVersion -Version ([System.IO.File]::ReadAllText($Layout.CurrentFile).Trim())
    }
    catch {
        throw "Pressay current-version pointer is invalid; refusing runtime maintenance."
    }
    return $current -ceq $safeVersion
}

function Assert-PressayRuntime {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Layout,

        [Parameter(Mandatory = $true)]
        [string]$Version,

        [switch]$InspectTree,

        [switch]$AllowAnyContractHash
    )

    $safeVersion = Assert-PressayVersion -Version $Version
    $runtimeVersionRoot = Get-PressayRuntimeVersionRoot -Layout $Layout -Version $safeVersion
    Assert-PressayPathIsNotReparsePoint -Path $runtimeVersionRoot | Out-Null
    if (-not (Test-Path -LiteralPath $runtimeVersionRoot -PathType Container)) {
        throw "Pressay runtime is missing for release ${safeVersion}: $runtimeVersionRoot"
    }
    Read-PressayRuntimeContract `
        -Path (Join-Path $runtimeVersionRoot $script:PressayRuntimeContractName) `
        -Version $safeVersion `
        -AllowAnyContractHash:$AllowAnyContractHash | Out-Null

    $venvRoot = Join-Path $runtimeVersionRoot "venv"
    $venvPython = Join-Path $venvRoot "Scripts\python.exe"
    $venvPythonw = Join-Path $venvRoot "Scripts\pythonw.exe"
    foreach ($path in @($venvRoot, $venvPython, $venvPythonw)) {
        Assert-PressayPathIsNotReparsePoint -Path $path | Out-Null
    }
    if (
        -not (Test-Path -LiteralPath $venvRoot -PathType Container) -or
        -not (Test-Path -LiteralPath $venvPython -PathType Leaf) -or
        -not (Test-Path -LiteralPath $venvPythonw -PathType Leaf)
    ) {
        throw "Pressay runtime is incomplete for release ${safeVersion}: $runtimeVersionRoot"
    }
    if ($InspectTree) {
        Get-PressaySafeTreeFiles -Root $runtimeVersionRoot | Out-Null
    }
    return $venvRoot
}

function Initialize-PressayRuntimeBuild {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Layout,

        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $safeVersion = Assert-PressayVersion -Version $Version
    Assert-PressayInstallLayoutSafety -Layout $Layout | Out-Null
    $runtimeVersionRoot = Get-PressayRuntimeVersionRoot -Layout $Layout -Version $safeVersion
    $readyMarker = Join-Path $runtimeVersionRoot $script:PressayRuntimeContractName

    if (Test-Path -LiteralPath $runtimeVersionRoot) {
        Assert-PressayPathIsNotReparsePoint -Path $runtimeVersionRoot | Out-Null
        if (Test-Path -LiteralPath $readyMarker -PathType Leaf) {
            $venvRoot = Assert-PressayRuntime `
                -Layout $Layout `
                -Version $safeVersion `
                -InspectTree
            return [pscustomobject]@{
                RuntimeVersionRoot = $runtimeVersionRoot
                VenvRoot = $venvRoot
                Reused = $true
            }
        }
        if (Test-PressayVersionIsCurrent -Layout $Layout -Version $safeVersion) {
            throw "Pressay refuses to rebuild the active runtime for release $safeVersion."
        }
        Get-PressaySafeTreeFiles -Root $runtimeVersionRoot | Out-Null
        Remove-Item -LiteralPath $runtimeVersionRoot -Recurse -Force
    }

    New-Item -ItemType Directory -Path $Layout.RuntimeVersionsRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $runtimeVersionRoot | Out-Null
    return [pscustomobject]@{
        RuntimeVersionRoot = $runtimeVersionRoot
        VenvRoot = Join-Path $runtimeVersionRoot "venv"
        Reused = $false
    }
}

function Complete-PressayRuntimeBuild {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Layout,

        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $safeVersion = Assert-PressayVersion -Version $Version
    $runtimeVersionRoot = Get-PressayRuntimeVersionRoot -Layout $Layout -Version $safeVersion
    $venvRoot = Join-Path $runtimeVersionRoot "venv"
    foreach ($relative in @("Scripts\python.exe", "Scripts\pythonw.exe")) {
        $required = Join-Path $venvRoot $relative
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Pressay runtime validation did not produce $relative."
        }
        Assert-PressayPathIsNotReparsePoint -Path $required | Out-Null
    }
    Get-PressaySafeTreeFiles -Root $runtimeVersionRoot | Out-Null
    Set-PressayFileAtomically `
        -Path (Join-Path $runtimeVersionRoot $script:PressayRuntimeContractName) `
        -Content (ConvertTo-PressayRuntimeContractJson -Version $safeVersion) | Out-Null
    return Assert-PressayRuntime -Layout $Layout -Version $safeVersion
}

function Remove-PressayIncompleteRuntimeBuild {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Layout,

        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $safeVersion = Assert-PressayVersion -Version $Version
    $runtimeVersionRoot = Get-PressayRuntimeVersionRoot -Layout $Layout -Version $safeVersion
    if (-not (Test-Path -LiteralPath $runtimeVersionRoot)) {
        return
    }
    if (Test-PressayVersionIsCurrent -Layout $Layout -Version $safeVersion) {
        throw "Pressay refuses to remove the active runtime for release $safeVersion."
    }
    $readyMarker = Join-Path $runtimeVersionRoot $script:PressayRuntimeContractName
    if (Test-Path -LiteralPath $readyMarker -PathType Leaf) {
        return
    }
    Get-PressaySafeTreeFiles -Root $runtimeVersionRoot | Out-Null
    Remove-Item -LiteralPath $runtimeVersionRoot -Recurse -Force
}

function Get-PressayActiveRuntimeRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Layout
    )

    if (-not (Test-Path -LiteralPath $Layout.CurrentFile -PathType Leaf)) {
        $legacyPython = Join-Path $Layout.LegacyRuntimeRoot "Scripts\python.exe"
        if (Test-Path -LiteralPath $legacyPython -PathType Leaf) {
            Assert-PressayPathIsNotReparsePoint -Path $legacyPython | Out-Null
            return $Layout.LegacyRuntimeRoot
        }
        throw "Pressay has no active installed runtime."
    }

    $version = Get-PressayCurrentVersion -Layout $Layout
    $payloadRoot = Assert-PressayDirectChild `
        -Path (Join-Path $Layout.VersionsRoot $version) `
        -Parent $Layout.VersionsRoot
    Assert-PressayPayload `
        -PayloadRoot $payloadRoot `
        -Version $version `
        -AllowLegacyRuntimeContract `
        -AllowAnyRuntimeContractHash | Out-Null
    $payloadContract = Join-Path $payloadRoot $script:PressayRuntimeContractName
    if (Test-Path -LiteralPath $payloadContract -PathType Leaf) {
        $payloadRuntimeContract = Read-PressayRuntimeContract `
            -Path $payloadContract `
            -Version $version `
            -AllowAnyContractHash
        $venvRoot = Assert-PressayRuntime `
            -Layout $Layout `
            -Version $version `
            -AllowAnyContractHash
        $runtimeVersionRoot = Get-PressayRuntimeVersionRoot -Layout $Layout -Version $version
        $runtimeContract = Read-PressayRuntimeContract `
            -Path (Join-Path $runtimeVersionRoot $script:PressayRuntimeContractName) `
            -Version $version `
            -AllowAnyContractHash
        if (
            [string]$payloadRuntimeContract.dependency_contract_sha256 -cne
                [string]$runtimeContract.dependency_contract_sha256
        ) {
            throw "Pressay payload and active runtime contracts do not match release $version."
        }
        return $venvRoot
    }

    $legacyPython = Join-Path $Layout.LegacyRuntimeRoot "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $legacyPython -PathType Leaf)) {
        throw "Pressay legacy runtime is missing for release $version."
    }
    Assert-PressayPathIsNotReparsePoint -Path $legacyPython | Out-Null
    return $Layout.LegacyRuntimeRoot
}

function Set-PressayFileAtomically {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Content
    )

    $target = [System.IO.Path]::GetFullPath($Path)
    $parent = Split-Path -Parent $target
    if ([string]::IsNullOrWhiteSpace($parent)) {
        throw "Atomic file target has no parent: $target"
    }
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $leaf = Split-Path -Leaf $target
    $temporary = Join-Path $parent (
        ".{0}.{1}.tmp" -f $leaf, [guid]::NewGuid().ToString("N")
    )
    $safeTemporary = Assert-PressayDirectChild `
        -Path $temporary `
        -Parent $parent `
        -RequiredLeafPrefix ("." + $leaf + ".")
    $backup = Join-Path $parent (
        ".{0}.{1}.bak" -f $leaf, [guid]::NewGuid().ToString("N")
    )
    $safeBackup = Assert-PressayDirectChild `
        -Path $backup `
        -Parent $parent `
        -RequiredLeafPrefix ("." + $leaf + ".")
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $published = $false
    try {
        [System.IO.File]::WriteAllText($safeTemporary, $Content, $encoding)
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            Assert-PressayPathIsNotReparsePoint -Path $target | Out-Null
            Invoke-PressayFileReplace `
                -Source $safeTemporary `
                -Destination $target `
                -Backup $safeBackup
        }
        else {
            [System.IO.File]::Move($safeTemporary, $target)
        }
        $published = $true
    }
    finally {
        if (-not $published -and (Test-Path -LiteralPath $safeBackup -PathType Leaf)) {
            if (-not (Test-Path -LiteralPath $target)) {
                try { [System.IO.File]::Move($safeBackup, $target) } catch {}
            }
        }
        if (Test-Path -LiteralPath $safeTemporary -PathType Leaf) {
            try { [System.IO.File]::Delete($safeTemporary) } catch {}
        }
        if ($published -and (Test-Path -LiteralPath $safeBackup -PathType Leaf)) {
            try { [System.IO.File]::Delete($safeBackup) } catch {}
        }
    }
    return $target
}

function Invoke-PressayFileReplace {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,

        [Parameter(Mandatory = $true)]
        [string]$Destination,

        [Parameter(Mandatory = $true)]
        [string]$Backup
    )

    [System.IO.File]::Replace($Source, $Destination, $Backup, $true)
}

function Publish-PressayInstalledLauncher {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$LauncherSource,

        [Parameter(Mandatory = $true)]
        [psobject]$Layout
    )

    $source = [System.IO.Path]::GetFullPath($LauncherSource)
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Pressay launcher source is missing: $source"
    }
    $content = [System.IO.File]::ReadAllText($source)
    if ($content -notmatch 'Pressay installed launcher') {
        throw "Pressay launcher source does not contain the installed-launcher contract."
    }
    return Set-PressayFileAtomically -Path $Layout.LauncherPath -Content $content
}

function Publish-PressayInstalledUninstaller {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$UninstallerSource,

        [Parameter(Mandatory = $true)]
        [psobject]$Layout
    )

    $source = [System.IO.Path]::GetFullPath($UninstallerSource)
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Pressay uninstaller source is missing: $source"
    }
    $content = [System.IO.File]::ReadAllText($source)
    if ($content -notmatch 'Pressay installed uninstaller') {
        throw "Pressay uninstaller source does not contain the installed-uninstaller contract."
    }
    if ($content -match 'shortcut-utils\.ps1|install-layout\.ps1') {
        throw "Pressay installed uninstaller must be self-contained."
    }
    return Set-PressayFileAtomically -Path $Layout.UninstallerPath -Content $content
}

function Assert-PressayActivationCandidate {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Layout,

        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $safeVersion = Assert-PressayVersion -Version $Version
    $versionRoot = Assert-PressayDirectChild `
        -Path (Join-Path $Layout.VersionsRoot $safeVersion) `
        -Parent $Layout.VersionsRoot
    Assert-PressayPayload -PayloadRoot $versionRoot -Version $safeVersion | Out-Null
    $payloadContract = Read-PressayRuntimeContract `
        -Path (Join-Path $versionRoot $script:PressayRuntimeContractName) `
        -Version $safeVersion
    $venvRoot = Assert-PressayRuntime -Layout $Layout -Version $safeVersion
    $runtimeVersionRoot = Get-PressayRuntimeVersionRoot -Layout $Layout -Version $safeVersion
    $runtimeContract = Read-PressayRuntimeContract `
        -Path (Join-Path $runtimeVersionRoot $script:PressayRuntimeContractName) `
        -Version $safeVersion
    if (
        [string]$payloadContract.dependency_contract_sha256 -cne
            [string]$runtimeContract.dependency_contract_sha256
    ) {
        throw "Pressay payload and runtime contracts do not match release $safeVersion."
    }
    return [pscustomobject]@{
        PayloadRoot = $versionRoot
        RuntimeRoot = $venvRoot
    }
}

function Set-PressayCurrentVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Layout,

        [Parameter(Mandatory = $true)]
        [string]$Version
    )

    $safeVersion = Assert-PressayVersion -Version $Version
    Assert-PressayActivationCandidate -Layout $Layout -Version $safeVersion | Out-Null
    Set-PressayFileAtomically `
        -Path $Layout.CurrentFile `
        -Content ($safeVersion + [Environment]::NewLine) | Out-Null
    return $safeVersion
}

function Get-PressayCurrentVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Layout
    )

    if (-not (Test-Path -LiteralPath $Layout.CurrentFile -PathType Leaf)) {
        throw "Pressay current-version pointer is missing: $($Layout.CurrentFile)"
    }
    $version = [System.IO.File]::ReadAllText($Layout.CurrentFile).Trim()
    return Assert-PressayVersion -Version $version
}

function Complete-PressayActivation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [psobject]$Layout,

        [Parameter(Mandatory = $true)]
        [string]$Version,

        [Parameter(Mandatory = $true)]
        [string]$LauncherSource,

        [Parameter(Mandatory = $true)]
        [string]$UninstallerSource
    )

    $safeVersion = Assert-PressayVersion -Version $Version
    $candidate = Assert-PressayActivationCandidate -Layout $Layout -Version $safeVersion
    Publish-PressayInstalledLauncher `
        -LauncherSource $LauncherSource `
        -Layout $Layout | Out-Null
    Publish-PressayInstalledUninstaller `
        -UninstallerSource $UninstallerSource `
        -Layout $Layout | Out-Null
    Set-PressayCurrentVersion -Layout $Layout -Version $safeVersion | Out-Null
    return $candidate.PayloadRoot
}
