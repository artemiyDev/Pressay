from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
POWERSHELL = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


def _ps_quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_powershell(
    source: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            source,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _make_project(root: Path, version: str, marker: str) -> Path:
    project = root / f"source-{version}-{marker}"
    package = project / "src" / "pressay"
    assets = package / "assets"
    cache = package / "__pycache__"
    assets.mkdir(parents=True)
    cache.mkdir()
    (project / "pyproject.toml").write_text(
        f'[project]\nname = "pressay"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (project / "LICENSE").write_text("test license\n", encoding="utf-8")
    (package / "__init__.py").write_text(
        f'PAYLOAD_MARKER = "{marker}"\n',
        encoding="utf-8",
    )
    (package / "__main__.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (assets / "app-icon.svg").write_text("<svg/>\n", encoding="utf-8")
    (cache / "ignored.pyc").write_bytes(b"not payload")
    (package / "recording.wav").write_bytes(b"private audio is not payload")
    (package / ".env").write_text("NOT_A_RUNTIME_FILE=1\n", encoding="utf-8")
    return project


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell required")
def test_payload_install_is_versioned_idempotent_and_immutable(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "1.2.3", "first")
    conflicting = _make_project(tmp_path, "1.2.3", "changed")
    local_appdata = tmp_path / "Owner's Local AppData with spaces"

    command = (
        "$ErrorActionPreference = 'Stop'; "
        f". {_ps_quote(SCRIPTS / 'install-layout.ps1')}; "
        f"$layout = Get-PressayInstallLayout -LocalAppData {_ps_quote(local_appdata)}; "
        f"$version = Get-PressayProjectVersion -ProjectRoot {_ps_quote(project)}; "
        f"$first = Install-PressayPayload -ProjectRoot {_ps_quote(project)} -Layout $layout -Version $version; "
        f"$second = Install-PressayPayload -ProjectRoot {_ps_quote(project)} -Layout $layout -Version $version; "
        "$conflict = $false; "
        f"try {{ Install-PressayPayload -ProjectRoot {_ps_quote(conflicting)} -Layout $layout -Version $version | Out-Null }} catch {{ $conflict = $true }}; "
        "$staging = @(Get-ChildItem -LiteralPath $layout.VersionsRoot -Directory -Filter '.staging-*'); "
        "[pscustomobject]@{ First = $first; Second = $second; Conflict = $conflict; Staging = $staging.Count } | ConvertTo-Json -Compress"
    )
    result = _run_powershell(command)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    expected_root = local_appdata / "Pressay" / "app" / "1.2.3"
    assert Path(payload["First"]) == expected_root
    assert Path(payload["Second"]) == expected_root
    assert payload["Conflict"] is True
    assert payload["Staging"] == 0
    assert (expected_root / ".pressay-manifest.json").is_file()
    assert (expected_root / "src" / "pressay" / "__main__.py").is_file()
    assert not (expected_root / "src" / "pressay" / "__pycache__").exists()
    assert not (expected_root / "src" / "pressay" / "recording.wav").exists()
    assert not (expected_root / "src" / "pressay" / ".env").exists()
    assert str(project) not in (expected_root / ".pressay-manifest.json").read_text(
        encoding="utf-8"
    )


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell required")
def test_failed_activation_keeps_current_and_reinstall_preserves_shared_data(
    tmp_path: Path,
) -> None:
    old_project = _make_project(tmp_path, "1.0.0", "old")
    new_project = _make_project(tmp_path, "1.1.0", "new")
    local_appdata = tmp_path / "Local AppData"
    app_root = local_appdata / "Pressay"
    preserved = [
        app_root / "config.json",
        app_root / "pressay.log",
        app_root / "venv" / "Scripts" / "python.exe",
        app_root / "models" / "model.bin",
    ]
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep", encoding="utf-8")

    command = (
        "$ErrorActionPreference = 'Stop'; "
        f". {_ps_quote(SCRIPTS / 'install-layout.ps1')}; "
        f"$layout = Get-PressayInstallLayout -LocalAppData {_ps_quote(local_appdata)}; "
        f"Install-PressayPayload -ProjectRoot {_ps_quote(old_project)} -Layout $layout -Version '1.0.0' | Out-Null; "
        f"Complete-PressayActivation -Layout $layout -Version '1.0.0' -LauncherSource {_ps_quote(SCRIPTS / 'run.ps1')} | Out-Null; "
        f"Install-PressayPayload -ProjectRoot {_ps_quote(new_project)} -Layout $layout -Version '1.1.0' | Out-Null; "
        "$failed = $false; "
        f"try {{ Complete-PressayActivation -Layout $layout -Version '1.1.0' -LauncherSource {_ps_quote(tmp_path / 'missing-launcher.ps1')} | Out-Null }} catch {{ $failed = $true }}; "
        "$afterFailure = Get-PressayCurrentVersion -Layout $layout; "
        f"Complete-PressayActivation -Layout $layout -Version '1.1.0' -LauncherSource {_ps_quote(SCRIPTS / 'run.ps1')} | Out-Null; "
        "$afterSuccess = Get-PressayCurrentVersion -Layout $layout; "
        "[pscustomobject]@{ Failed = $failed; AfterFailure = $afterFailure; AfterSuccess = $afterSuccess } | ConvertTo-Json -Compress"
    )
    result = _run_powershell(command)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {
        "Failed": True,
        "AfterFailure": "1.0.0",
        "AfterSuccess": "1.1.0",
    }
    assert all(path.read_text(encoding="utf-8") == "keep" for path in preserved)
    launcher = app_root / "Pressay.ps1"
    assert launcher.is_file()
    assert str(old_project) not in launcher.read_text(encoding="utf-8")
    assert str(new_project) not in launcher.read_text(encoding="utf-8")


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell required")
def test_explicit_uninstall_flags_remove_only_owned_install_targets(tmp_path: Path) -> None:
    fixture = tmp_path / "Uninstall fixture"
    scripts = fixture / "scripts"
    scripts.mkdir(parents=True)
    mutex_name = f"Local\\Pressay.Tests.{uuid.uuid4()}"
    installer_mutex_name = f"Local\\Pressay.Tests.Installer.{uuid.uuid4()}"
    uninstall_source = (SCRIPTS / "uninstall.ps1").read_text(encoding="utf-8")
    uninstall_source = uninstall_source.replace(
        "Local\\Pressay.Desktop.Singleton",
        mutex_name,
    )
    uninstall_script = scripts / "uninstall.ps1"
    uninstall_script.write_text(uninstall_source, encoding="utf-8")
    (scripts / "shortcut-utils.ps1").write_text(
        "function Get-PressayInstallLayout {\n"
        "  $root = Join-Path $env:LOCALAPPDATA 'Pressay'\n"
        "  [pscustomobject]@{\n"
        "    Root = $root\n"
        "    VersionsRoot = Join-Path $root 'app'\n"
        "    CurrentFile = Join-Path $root 'current'\n"
        "    LauncherPath = Join-Path $root 'Pressay.ps1'\n"
        "    RuntimeRoot = Join-Path $root 'venv'\n"
        "    IconPath = Join-Path $root 'pressay.ico'\n"
        "  }\n"
        "}\n"
        "function Assert-PressayPathIsNotReparsePoint {\n"
        "  param([string]$Path)\n"
        "  return [System.IO.Path]::GetFullPath($Path)\n"
        "}\n"
        "function Get-PressaySafeTreeFiles { param([string]$Root) return @() }\n"
        "function Enter-PressayInstallerGuard {\n"
        "  $created = $false\n"
        f"  $guard = New-Object System.Threading.Mutex($false, '{installer_mutex_name}', [ref]$created)\n"
        "  if (-not $created) { $guard.Dispose(); throw 'installer busy' }\n"
        "  return $guard\n"
        "}\n"
        "function Enter-PressayAppMaintenanceGuard {\n"
        "  $created = $false\n"
        f"  $guard = New-Object System.Threading.Mutex($false, '{mutex_name}', [ref]$created)\n"
        "  if (-not $created) { $guard.Dispose(); throw 'app busy' }\n"
        "  return $guard\n"
        "}\n"
        "function Exit-PressayGuard { param($Guard) if ($null -ne $Guard) { $Guard.Dispose() } }\n"
        "function Get-PressayLauncherSpec { [pscustomobject]@{} }\n"
        "function Remove-PressayShortcut {\n"
        "  [CmdletBinding(SupportsShouldProcess = $true)]\n"
        "  param([string]$ShortcutPath, [psobject]$Spec)\n"
        "  return $true\n"
        "}\n",
        encoding="utf-8",
    )

    local_appdata = tmp_path / "Local AppData"
    app_root = local_appdata / "Pressay"
    removed = [
        app_root / "app" / "1.0.0" / "src" / "pressay" / "__main__.py",
        app_root / "current",
        app_root / "Pressay.ps1",
        app_root / "pressay.ico",
        app_root / "venv" / "Scripts" / "python.exe",
        app_root / "config.json",
        app_root / "pressay.log",
    ]
    for path in removed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("remove", encoding="utf-8")
    preserved = [
        app_root / "models" / "model.bin",
        app_root / "notes.txt",
        local_appdata / "Unrelated App" / "data.txt",
    ]
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep", encoding="utf-8")

    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(local_appdata)
    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(uninstall_script),
            "-RemoveApp",
            "-RemoveRuntime",
            "-RemoveUserData",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert all(not path.exists() for path in removed)
    assert all(path.read_text(encoding="utf-8") == "keep" for path in preserved)


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell required")
def test_layout_rejects_root_local_appdata_and_unsafe_versions(tmp_path: Path) -> None:
    drive_root = Path(tmp_path.anchor)
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f". {_ps_quote(SCRIPTS / 'install-layout.ps1')}; "
        "$rootRejected = $false; $versionRejected = $false; "
        f"try {{ Get-PressayInstallLayout -LocalAppData {_ps_quote(drive_root)} | Out-Null }} catch {{ $rootRejected = $true }}; "
        "try { Assert-PressayVersion -Version '..\\outside' | Out-Null } catch { $versionRejected = $true }; "
        "[pscustomobject]@{ Root = $rootRejected; Version = $versionRejected } | ConvertTo-Json -Compress"
    )
    result = _run_powershell(command)

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "Root": True,
        "Version": True,
    }


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell required")
def test_running_app_preflight_refuses_before_install_mutation(tmp_path: Path) -> None:
    mutex_name = f"Local\\Pressay.Tests.{uuid.uuid4()}"
    local_appdata = tmp_path / "Local AppData"
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f". {_ps_quote(SCRIPTS / 'install-layout.ps1')}; "
        "$created = $false; $mutex = $null; "
        f"$mutex = New-Object System.Threading.Mutex($true, {_ps_quote(mutex_name)}, [ref]$created); "
        "$refused = $false; $message = ''; "
        f"try {{ Assert-PressayNotRunning -MutexName {_ps_quote(mutex_name)} }} catch {{ $refused = $true; $message = $_.Exception.Message }}; "
        "if ($created) { $mutex.ReleaseMutex() }; $mutex.Dispose(); "
        "[pscustomobject]@{ Refused = $refused; Message = $message } | ConvertTo-Json -Compress"
    )
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(local_appdata)
    result = _run_powershell(command, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["Refused"] is True
    assert "Exit it from the tray menu" in payload["Message"]
    assert not (local_appdata / "Pressay").exists()

    setup = (SCRIPTS / "setup.ps1").read_text(encoding="utf-8")
    assert setup.index("$installerGuard = Enter-PressayInstallerGuard") < setup.index(
        "$layout = Get-PressayInstallLayout"
    )
    assert setup.index("$appGuard = Enter-PressayAppMaintenanceGuard") < setup.index(
        "$layout = Get-PressayInstallLayout"
    )


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell required")
def test_installer_and_app_guards_are_exclusive_and_reacquirable() -> None:
    installer_name = f"Local\\Pressay.Tests.Installer.{uuid.uuid4()}"
    app_name = f"Local\\Pressay.Tests.App.{uuid.uuid4()}"
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f". {_ps_quote(SCRIPTS / 'install-layout.ps1')}; "
        f"$installer = Enter-PressayInstallerGuard -MutexName {_ps_quote(installer_name)}; "
        "$installerRefused = $false; "
        f"try {{ Enter-PressayInstallerGuard -MutexName {_ps_quote(installer_name)} | Out-Null }} catch {{ $installerRefused = $true }}; "
        f"$app = Enter-PressayAppMaintenanceGuard -MutexName {_ps_quote(app_name)}; "
        "$appRefused = $false; "
        f"try {{ Enter-PressayAppMaintenanceGuard -MutexName {_ps_quote(app_name)} | Out-Null }} catch {{ $appRefused = $true }}; "
        "Exit-PressayGuard -Guard $app; Exit-PressayGuard -Guard $installer; "
        f"$again = Enter-PressayInstallerGuard -MutexName {_ps_quote(installer_name)}; "
        "Exit-PressayGuard -Guard $again; "
        "[pscustomobject]@{ Installer = $installerRefused; App = $appRefused } | ConvertTo-Json -Compress"
    )

    result = _run_powershell(command)

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "Installer": True,
        "App": True,
    }


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell required")
def test_reparse_point_guard_fails_closed_with_mocked_item(tmp_path: Path) -> None:
    command = (
        "function Get-Item { "
        "param([string]$LiteralPath, [switch]$Force, $ErrorAction); "
        "[pscustomobject]@{ Attributes = [System.IO.FileAttributes]::ReparsePoint } }; "
        f". {_ps_quote(SCRIPTS / 'install-layout.ps1')}; "
        "$refused = $false; "
        f"try {{ Assert-PressayPathIsNotReparsePoint -Path {_ps_quote(tmp_path / 'link')} | Out-Null }} catch {{ $refused = $true }}; "
        "if (-not $refused) { throw 'reparse point was accepted' }"
    )

    result = _run_powershell(command)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell required")
def test_atomic_replace_partial_failure_restores_old_target(tmp_path: Path) -> None:
    target = tmp_path / "atomic path with spaces" / "current"
    target.parent.mkdir(parents=True)
    target.write_text("old-version\n", encoding="utf-8")
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f". {_ps_quote(SCRIPTS / 'install-layout.ps1')}; "
        "function Invoke-PressayFileReplace { "
        "param([string]$Source, [string]$Destination, [string]$Backup); "
        "[System.IO.File]::Move($Destination, $Backup); "
        "throw 'injected replace failure' }; "
        "$failed = $false; $message = ''; "
        f"try {{ Set-PressayFileAtomically -Path {_ps_quote(target)} -Content 'new-version' | Out-Null }} "
        "catch { $failed = $true; $message = $_.Exception.Message }; "
        f"$content = [System.IO.File]::ReadAllText({_ps_quote(target)}).Trim(); "
        f"$leftovers = @(Get-ChildItem -LiteralPath {_ps_quote(target.parent)} -Force | Where-Object Name -ne 'current').Count; "
        "[pscustomobject]@{ Failed = $failed; Message = $message; Content = $content; Leftovers = $leftovers } | ConvertTo-Json -Compress"
    )

    result = _run_powershell(command)

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "Failed": True,
        "Message": "injected replace failure",
        "Content": "old-version",
        "Leftovers": 0,
    }
