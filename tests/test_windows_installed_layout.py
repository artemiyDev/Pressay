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
    assert (expected_root / ".pressay-runtime.json").is_file()
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
        "function New-TestRuntime($layout, $version) { "
        "$build = Initialize-PressayRuntimeBuild -Layout $layout -Version $version; "
        "$scripts = Join-Path $build.VenvRoot 'Scripts'; New-Item -ItemType Directory -Path $scripts -Force | Out-Null; "
        "Set-Content -LiteralPath (Join-Path $scripts 'python.exe') -Value 'fake'; "
        "Set-Content -LiteralPath (Join-Path $scripts 'pythonw.exe') -Value 'fake'; "
        "Complete-PressayRuntimeBuild -Layout $layout -Version $version | Out-Null }; "
        f"$layout = Get-PressayInstallLayout -LocalAppData {_ps_quote(local_appdata)}; "
        f"Install-PressayPayload -ProjectRoot {_ps_quote(old_project)} -Layout $layout -Version '1.0.0' | Out-Null; "
        "New-TestRuntime $layout '1.0.0'; "
        f"Complete-PressayActivation -Layout $layout -Version '1.0.0' -LauncherSource {_ps_quote(SCRIPTS / 'run.ps1')} -UninstallerSource {_ps_quote(SCRIPTS / 'uninstall.ps1')} | Out-Null; "
        f"Install-PressayPayload -ProjectRoot {_ps_quote(new_project)} -Layout $layout -Version '1.1.0' | Out-Null; "
        "New-TestRuntime $layout '1.1.0'; "
        "$failed = $false; "
        f"try {{ Complete-PressayActivation -Layout $layout -Version '1.1.0' -LauncherSource {_ps_quote(tmp_path / 'missing-launcher.ps1')} -UninstallerSource {_ps_quote(SCRIPTS / 'uninstall.ps1')} | Out-Null }} catch {{ $failed = $true }}; "
        "$afterFailure = Get-PressayCurrentVersion -Layout $layout; "
        f"Complete-PressayActivation -Layout $layout -Version '1.1.0' -LauncherSource {_ps_quote(SCRIPTS / 'run.ps1')} -UninstallerSource {_ps_quote(SCRIPTS / 'uninstall.ps1')} | Out-Null; "
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
    uninstaller = app_root / "Uninstall-Pressay.ps1"
    assert uninstaller.is_file()
    assert "shortcut-utils.ps1" not in uninstaller.read_text(encoding="utf-8")
    assert str(old_project) not in launcher.read_text(encoding="utf-8")
    assert str(new_project) not in launcher.read_text(encoding="utf-8")


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell required")
def test_activation_retains_only_current_and_one_previous_install_pair(
    tmp_path: Path,
) -> None:
    projects = {
        version: _make_project(tmp_path, version, version.replace(".", "-"))
        for version in ("1.0.0", "1.1.0", "1.2.0")
    }
    local_appdata = tmp_path / "Retention Local AppData"
    shared = [
        local_appdata / "Pressay" / "config.json",
        local_appdata / "Pressay" / "models" / "model.bin",
    ]
    for path in shared:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep", encoding="utf-8")

    command = (
        "$ErrorActionPreference = 'Stop'; "
        f". {_ps_quote(SCRIPTS / 'install-layout.ps1')}; "
        "function Install-TestRelease($layout, $project, $version) { "
        "Install-PressayPayload -ProjectRoot $project -Layout $layout -Version $version | Out-Null; "
        "$build = Initialize-PressayRuntimeBuild -Layout $layout -Version $version; "
        "$scripts = Join-Path $build.VenvRoot 'Scripts'; New-Item -ItemType Directory -Path $scripts -Force | Out-Null; "
        "Set-Content -LiteralPath (Join-Path $scripts 'python.exe') -Value 'fake'; "
        "Set-Content -LiteralPath (Join-Path $scripts 'pythonw.exe') -Value 'fake'; "
        "Complete-PressayRuntimeBuild -Layout $layout -Version $version | Out-Null; "
        f"Complete-PressayActivation -Layout $layout -Version $version -LauncherSource {_ps_quote(SCRIPTS / 'run.ps1')} -UninstallerSource {_ps_quote(SCRIPTS / 'uninstall.ps1')} | Out-Null }}; "
        f"$layout = Get-PressayInstallLayout -LocalAppData {_ps_quote(local_appdata)}; "
        f"Install-TestRelease $layout {_ps_quote(projects['1.0.0'])} '1.0.0'; "
        f"Install-TestRelease $layout {_ps_quote(projects['1.1.0'])} '1.1.0'; "
        f"Install-TestRelease $layout {_ps_quote(projects['1.2.0'])} '1.2.0'; "
        f"Complete-PressayActivation -Layout $layout -Version '1.2.0' -LauncherSource {_ps_quote(SCRIPTS / 'run.ps1')} -UninstallerSource {_ps_quote(SCRIPTS / 'uninstall.ps1')} | Out-Null; "
        "$apps = @(Get-ChildItem -LiteralPath $layout.VersionsRoot -Directory | Sort-Object Name | ForEach-Object Name); "
        "$runtimes = @(Get-ChildItem -LiteralPath $layout.RuntimeVersionsRoot -Directory | Sort-Object Name | ForEach-Object Name); "
        "[pscustomobject]@{ Apps = $apps; Runtimes = $runtimes; Current = (Get-PressayCurrentVersion -Layout $layout) } | ConvertTo-Json -Compress"
    )
    result = _run_powershell(command)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {
        "Apps": ["1.1.0", "1.2.0"],
        "Runtimes": ["1.1.0", "1.2.0"],
        "Current": "1.2.0",
    }
    assert all(path.read_text(encoding="utf-8") == "keep" for path in shared)


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell required")
def test_obsolete_cleanup_leaves_unpaired_and_tampered_releases_untouched(
    tmp_path: Path,
) -> None:
    projects = {
        version: _make_project(tmp_path, version, version.replace(".", "-"))
        for version in ("2.0.0", "2.1.0", "2.2.0")
    }
    local_appdata = tmp_path / "Fail Closed Retention Local AppData"
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f". {_ps_quote(SCRIPTS / 'install-layout.ps1')}; "
        "function Install-TestRelease($layout, $project, $version) { "
        "Install-PressayPayload -ProjectRoot $project -Layout $layout -Version $version | Out-Null; "
        "$build = Initialize-PressayRuntimeBuild -Layout $layout -Version $version; "
        "$scripts = Join-Path $build.VenvRoot 'Scripts'; New-Item -ItemType Directory -Path $scripts -Force | Out-Null; "
        "Set-Content -LiteralPath (Join-Path $scripts 'python.exe') -Value 'fake'; "
        "Set-Content -LiteralPath (Join-Path $scripts 'pythonw.exe') -Value 'fake'; "
        "Complete-PressayRuntimeBuild -Layout $layout -Version $version | Out-Null; "
        f"Complete-PressayActivation -Layout $layout -Version $version -LauncherSource {_ps_quote(SCRIPTS / 'run.ps1')} -UninstallerSource {_ps_quote(SCRIPTS / 'uninstall.ps1')} | Out-Null }}; "
        f"$layout = Get-PressayInstallLayout -LocalAppData {_ps_quote(local_appdata)}; "
        f"Install-TestRelease $layout {_ps_quote(projects['2.0.0'])} '2.0.0'; "
        f"Install-TestRelease $layout {_ps_quote(projects['2.1.0'])} '2.1.0'; "
        "$tampered = Join-Path $layout.VersionsRoot '2.0.0\\src\\pressay\\__main__.py'; Add-Content -LiteralPath $tampered -Value 'changed'; "
        "$orphan = Initialize-PressayRuntimeBuild -Layout $layout -Version '1.9.0'; "
        "$orphanScripts = Join-Path $orphan.VenvRoot 'Scripts'; New-Item -ItemType Directory -Path $orphanScripts -Force | Out-Null; "
        "Set-Content -LiteralPath (Join-Path $orphanScripts 'python.exe') -Value 'fake'; "
        "Set-Content -LiteralPath (Join-Path $orphanScripts 'pythonw.exe') -Value 'fake'; "
        "Complete-PressayRuntimeBuild -Layout $layout -Version '1.9.0' | Out-Null; "
        f"Install-TestRelease $layout {_ps_quote(projects['2.2.0'])} '2.2.0'; "
        "$apps = @(Get-ChildItem -LiteralPath $layout.VersionsRoot -Directory | Sort-Object Name | ForEach-Object Name); "
        "$runtimes = @(Get-ChildItem -LiteralPath $layout.RuntimeVersionsRoot -Directory | Sort-Object Name | ForEach-Object Name); "
        "[pscustomobject]@{ Apps = $apps; Runtimes = $runtimes; Current = (Get-PressayCurrentVersion -Layout $layout); Tampered = (Test-Path -LiteralPath $tampered) } | ConvertTo-Json -Compress"
    )
    result = _run_powershell(command)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {
        "Apps": ["2.0.0", "2.1.0", "2.2.0"],
        "Runtimes": ["1.9.0", "2.0.0", "2.1.0", "2.2.0"],
        "Current": "2.2.0",
        "Tampered": True,
    }


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
    ).replace(
        "Local\\Pressay.Desktop.Installer",
        installer_mutex_name,
    )
    uninstall_script = scripts / "uninstall.ps1"
    uninstall_script.write_text(uninstall_source, encoding="utf-8")

    local_appdata = tmp_path / "Local AppData"
    app_root = local_appdata / "Pressay"
    shortcut_directory = tmp_path / "empty shortcuts"
    shortcut_directory.mkdir()
    removed = [
        app_root / "app" / "1.0.0" / "src" / "pressay" / "__main__.py",
        app_root / "runtime" / "1.0.0" / "venv" / "Scripts" / "python.exe",
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
        app_root / "Uninstall-Pressay.ps1",
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
            "-ShortcutDirectories",
            str(shortcut_directory),
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
def test_versioned_runtime_ready_marker_reuse_and_active_cleanup_guard(
    tmp_path: Path,
) -> None:
    local_appdata = tmp_path / "Runtime Local AppData"
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f". {_ps_quote(SCRIPTS / 'install-layout.ps1')}; "
        f"$layout = Get-PressayInstallLayout -LocalAppData {_ps_quote(local_appdata)}; "
        "$first = Initialize-PressayRuntimeBuild -Layout $layout -Version '2.0.0'; "
        "$marker = Join-Path $first.RuntimeVersionRoot '.pressay-runtime.json'; "
        "$markerBefore = Test-Path -LiteralPath $marker; "
        "$scripts = Join-Path $first.VenvRoot 'Scripts'; New-Item -ItemType Directory -Path $scripts -Force | Out-Null; "
        "Set-Content -LiteralPath (Join-Path $scripts 'python.exe') -Value 'fake'; "
        "Set-Content -LiteralPath (Join-Path $scripts 'pythonw.exe') -Value 'fake'; "
        "Complete-PressayRuntimeBuild -Layout $layout -Version '2.0.0' | Out-Null; "
        "$second = Initialize-PressayRuntimeBuild -Layout $layout -Version '2.0.0'; "
        "$inactive = Get-PressayRuntimeVersionRoot -Layout $layout -Version '3.0.0'; "
        "New-Item -ItemType Directory -Path $inactive -Force | Out-Null; "
        "Set-Content -LiteralPath (Join-Path $inactive 'partial.txt') -Value 'partial'; "
        "Set-PressayFileAtomically -Path $layout.CurrentFile -Content \"3.0.0`n\" | Out-Null; "
        "$refused = $false; try { Initialize-PressayRuntimeBuild -Layout $layout -Version '3.0.0' | Out-Null } catch { $refused = $true }; "
        "$partialKept = Test-Path -LiteralPath (Join-Path $inactive 'partial.txt'); "
        "Set-PressayFileAtomically -Path $layout.CurrentFile -Content \"2.0.0`n\" | Out-Null; "
        "$rebuilt = Initialize-PressayRuntimeBuild -Layout $layout -Version '3.0.0'; "
        "$partialRemoved = -not (Test-Path -LiteralPath (Join-Path $inactive 'partial.txt')); "
        "[pscustomobject]@{ MarkerBefore = $markerBefore; MarkerAfter = (Test-Path -LiteralPath $marker); Reused = $second.Reused; Refused = $refused; PartialKept = $partialKept; Rebuilt = (-not $rebuilt.Reused); PartialRemoved = $partialRemoved } | ConvertTo-Json -Compress"
    )
    result = _run_powershell(command)

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "MarkerBefore": False,
        "MarkerAfter": True,
        "Reused": True,
        "Refused": True,
        "PartialKept": True,
        "Rebuilt": True,
        "PartialRemoved": True,
    }


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell required")
def test_activation_refuses_marked_payload_without_ready_runtime(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "4.0.0", "candidate")
    local_appdata = tmp_path / "Activation Local AppData"
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f". {_ps_quote(SCRIPTS / 'install-layout.ps1')}; "
        f"$layout = Get-PressayInstallLayout -LocalAppData {_ps_quote(local_appdata)}; "
        f"Install-PressayPayload -ProjectRoot {_ps_quote(project)} -Layout $layout -Version '4.0.0' | Out-Null; "
        "$failed = $false; "
        f"try {{ Complete-PressayActivation -Layout $layout -Version '4.0.0' -LauncherSource {_ps_quote(SCRIPTS / 'run.ps1')} -UninstallerSource {_ps_quote(SCRIPTS / 'uninstall.ps1')} | Out-Null }} catch {{ $failed = $true }}; "
        "[pscustomobject]@{ Failed = $failed; Current = (Test-Path -LiteralPath $layout.CurrentFile); Launcher = (Test-Path -LiteralPath $layout.LauncherPath); Uninstaller = (Test-Path -LiteralPath $layout.UninstallerPath) } | ConvertTo-Json -Compress"
    )
    result = _run_powershell(command)

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "Failed": True,
        "Current": False,
        "Launcher": False,
        "Uninstaller": False,
    }


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell required")
def test_active_runtime_resolver_rejects_removed_manifested_marker(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path, "5.0.0", "resolver")
    local_appdata = tmp_path / "Resolver Local AppData"
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f". {_ps_quote(SCRIPTS / 'install-layout.ps1')}; "
        f"$layout = Get-PressayInstallLayout -LocalAppData {_ps_quote(local_appdata)}; "
        f"$payload = Install-PressayPayload -ProjectRoot {_ps_quote(project)} -Layout $layout -Version '5.0.0'; "
        "New-Item -ItemType Directory -Path (Join-Path $layout.LegacyRuntimeRoot 'Scripts') -Force | Out-Null; "
        "Set-Content -LiteralPath (Join-Path $layout.LegacyRuntimeRoot 'Scripts\\python.exe') -Value 'legacy'; "
        "Set-PressayFileAtomically -Path $layout.CurrentFile -Content \"5.0.0`n\" | Out-Null; "
        "Remove-Item -LiteralPath (Join-Path $payload '.pressay-runtime.json') -Force; "
        "$rejected = $false; try { Get-PressayActiveRuntimeRoot -Layout $layout | Out-Null } catch { $rejected = $true }; "
        "Write-PressayPayloadManifest -PayloadRoot $payload -Version '5.0.0' | Out-Null; "
        "$legacy = Get-PressayActiveRuntimeRoot -Layout $layout; "
        "[pscustomobject]@{ Rejected = $rejected; Legacy = $legacy } | ConvertTo-Json -Compress"
    )
    result = _run_powershell(command)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["Rejected"] is True
    assert Path(payload["Legacy"]) == local_appdata / "Pressay" / "venv"


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell required")
def test_installed_uninstaller_is_self_contained_after_checkout_removal(
    tmp_path: Path,
) -> None:
    local_appdata = tmp_path / "Installed Local AppData"
    install_root = local_appdata / "Pressay"
    install_root.mkdir(parents=True)
    installed = install_root / "Uninstall-Pressay.ps1"
    installed.write_text(
        (SCRIPTS / "uninstall.ps1").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    empty_shortcuts = tmp_path / "empty shortcuts"
    empty_shortcuts.mkdir()
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
            str(installed),
            "-ShortcutDirectories",
            str(empty_shortcuts),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    text = installed.read_text(encoding="utf-8")
    assert "shortcut-utils.ps1" not in text
    assert "install-layout.ps1" not in text


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
