Set-StrictMode -Version Latest

$script:PressayShortcutDescription = "Local Pressay voice dictation"
$script:PressayShortcutAppId = "Pressay.Pressay"
. (Join-Path $PSScriptRoot "install-layout.ps1")

function Initialize-PressayShortcutAppIdType {
    [CmdletBinding()]
    param()

    if ('Pressay.ShortcutAppId' -as [type]) {
        return
    }

    # A minimal IShellLinkW/IPropertyStore interop surface. WScript.Shell has
    # no way to set System.AppUserModel.ID on a .lnk, so this reads and writes
    # PKEY_AppUserModel_ID directly through the shell link's property store.
    # Loaded lazily (not at dot-source time) so a compile failure here cannot
    # take down callers that only need the WScript.Shell-based functions, and
    # guarded by a type check so repeated dot-sourcing in the same process
    # (install.ps1 -> install-autostart.ps1) does not re-run Add-Type.
    $csharp = @"
using System;
using System.Runtime.InteropServices;

namespace Pressay
{
    [ComImport]
    [Guid("0000010b-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPersistFile
    {
        void GetClassID(out Guid pClassID);
        [PreserveSig] int IsDirty();
        void Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, uint dwMode);
        void Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, [MarshalAs(UnmanagedType.Bool)] bool fRemember);
        void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);
        void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string ppszFileName);
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct PROPERTYKEY
    {
        public Guid fmtid;
        public uint pid;
        public PROPERTYKEY(Guid fmtid, uint pid) { this.fmtid = fmtid; this.pid = pid; }
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct PROPVARIANT : IDisposable
    {
        public ushort vt;
        public ushort wReserved1;
        public ushort wReserved2;
        public ushort wReserved3;
        public IntPtr p;
        public int p2;

        public void Dispose()
        {
            PropVariantClear(ref this);
        }

        public static PROPVARIANT FromString(string value)
        {
            PROPVARIANT pv = new PROPVARIANT();
            pv.vt = 31; // VT_LPWSTR
            pv.p = Marshal.StringToCoTaskMemUni(value);
            return pv;
        }

        public string ToStringValue()
        {
            if (vt != 31) { return string.Empty; }
            return Marshal.PtrToStringUni(p);
        }

        [DllImport("ole32.dll")]
        private static extern int PropVariantClear(ref PROPVARIANT pvar);
    }

    [ComImport]
    [Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPropertyStore
    {
        int GetCount(out uint cProps);
        int GetAt(uint iProp, out PROPERTYKEY pkey);
        int GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
        int SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
        int Commit();
    }

    public static class ShortcutAppId
    {
        private static readonly Guid CLSID_ShellLink = new Guid("00021401-0000-0000-C000-000000000046");
        private static readonly PROPERTYKEY PKEY_AppUserModel_ID = new PROPERTYKEY(
            new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5);

        public static void SetAppId(string shortcutPath, string appId)
        {
            object shellLinkObj = Activator.CreateInstance(Type.GetTypeFromCLSID(CLSID_ShellLink));
            try
            {
                IPersistFile persistFile = (IPersistFile)shellLinkObj;
                IPropertyStore propertyStore = (IPropertyStore)shellLinkObj;
                try
                {
                    persistFile.Load(shortcutPath, 2); // STGM_READWRITE
                    PROPERTYKEY key = PKEY_AppUserModel_ID;
                    PROPVARIANT pv = PROPVARIANT.FromString(appId);
                    try
                    {
                        int hr = propertyStore.SetValue(ref key, ref pv);
                        if (hr != 0) { throw new System.ComponentModel.Win32Exception(hr); }
                        int hrCommit = propertyStore.Commit();
                        if (hrCommit != 0) { throw new System.ComponentModel.Win32Exception(hrCommit); }
                    }
                    finally
                    {
                        pv.Dispose();
                    }
                    persistFile.Save(null, true);
                }
                finally
                {
                    Marshal.ReleaseComObject(persistFile);
                    Marshal.ReleaseComObject(propertyStore);
                }
            }
            finally
            {
                Marshal.ReleaseComObject(shellLinkObj);
            }
        }

        public static string GetAppId(string shortcutPath)
        {
            object shellLinkObj = Activator.CreateInstance(Type.GetTypeFromCLSID(CLSID_ShellLink));
            try
            {
                IPersistFile persistFile = (IPersistFile)shellLinkObj;
                IPropertyStore propertyStore = (IPropertyStore)shellLinkObj;
                try
                {
                    persistFile.Load(shortcutPath, 0); // STGM_READ
                    PROPERTYKEY key = PKEY_AppUserModel_ID;
                    PROPVARIANT pv;
                    int hr = propertyStore.GetValue(ref key, out pv);
                    if (hr != 0) { return string.Empty; }
                    try
                    {
                        return pv.ToStringValue();
                    }
                    finally
                    {
                        pv.Dispose();
                    }
                }
                finally
                {
                    Marshal.ReleaseComObject(persistFile);
                    Marshal.ReleaseComObject(propertyStore);
                }
            }
            finally
            {
                Marshal.ReleaseComObject(shellLinkObj);
            }
        }
    }
}
"@
    Add-Type -TypeDefinition $csharp -Language CSharp
}

function Set-PressayShortcutAppId {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ShortcutPath,

        [Parameter(Mandatory = $true)]
        [string]$AppId
    )

    Initialize-PressayShortcutAppIdType
    [Pressay.ShortcutAppId]::SetAppId(
        [System.IO.Path]::GetFullPath($ShortcutPath),
        $AppId
    )
}

function Get-PressayShortcutAppId {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ShortcutPath
    )

    Initialize-PressayShortcutAppIdType
    return [Pressay.ShortcutAppId]::GetAppId(
        [System.IO.Path]::GetFullPath($ShortcutPath)
    )
}

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
        AppUserModelId   = $script:PressayShortcutAppId
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
        [psobject]$Spec,

        # Set when a prior Set-PressayShortcutAppId call failed (COM error) so
        # publication is not blocked and does not loop retrying the ID forever.
        [switch]$IgnoreAppUserModelId
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
        $appIdMatches = (
            $IgnoreAppUserModelId -or
            [string]::IsNullOrWhiteSpace([string]$Spec.AppUserModelId) -or
            (Get-PressayShortcutAppId -ShortcutPath $ShortcutPath) -ceq [string]$Spec.AppUserModelId
        )
        return (
            (Test-PressayInstalledShortcutObject -Shortcut $shortcut -Spec $Spec) -and
            $iconMatches -and
            $appIdMatches
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

    $appIdApplied = $true
    if (-not [string]::IsNullOrWhiteSpace([string]$Spec.AppUserModelId)) {
        try {
            Set-PressayShortcutAppId `
                -ShortcutPath $safeTemporary `
                -AppId ([string]$Spec.AppUserModelId)
        }
        catch {
            # A taskbar pin mismatch is cosmetic, not fatal: publish the
            # shortcut without the ID rather than fail the whole install.
            $appIdApplied = $false
            Write-Warning "Could not set the taskbar app identity on ${ShortcutPath}: $($_.Exception.Message)"
        }
    }

    $published = $false
    $verified = $false
    $replacingExisting = $false
    try {
        if (-not (Test-PressayShortcut -ShortcutPath $safeTemporary -Spec $Spec -IgnoreAppUserModelId:(-not $appIdApplied))) {
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
        if (-not (Test-PressayShortcut -ShortcutPath $ShortcutPath -Spec $Spec -IgnoreAppUserModelId:(-not $appIdApplied))) {
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
