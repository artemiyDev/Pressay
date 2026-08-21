from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _run_powershell(
    source: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
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


def _run_powershell_file(
    script: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _isolated_run_script(
    tmp_path: Path,
    *,
    use_current_python: bool = True,
) -> tuple[Path, dict[str, str]]:
    local_appdata = tmp_path / "owner's isolated local appdata"
    install_root = local_appdata / "Pressay"
    version = "9.8.7"
    package = install_root / "app" / version / "src" / "pressay"
    launcher = install_root / "Pressay.ps1"
    package.mkdir(parents=True)
    run_source = (SCRIPTS / "run.ps1").read_text(encoding="utf-8")
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    if use_current_python:
        run_source = run_source.replace(
            '$venvPython = Join-Path $runtimeRoot "Scripts\\python.exe"',
            f"$venvPython = '{python}'",
        ).replace(
            '$venvPythonw = Join-Path $runtimeRoot "Scripts\\pythonw.exe"',
            f"$venvPythonw = '{pythonw}'",
        )
    launcher.write_text(run_source, encoding="utf-8")
    (install_root / "current").write_text(version + "\n", encoding="utf-8")
    (install_root / "app" / version / ".pressay-version").write_text(
        version + "\n",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import os, sys\n"
        "sys.exit(int(os.environ.get('PRESSAY_TEST_EXIT_CODE', '0')))\n",
        encoding="utf-8",
    )
    payload_root = install_root / "app" / version
    manifest_files = []
    for path in sorted(path for path in payload_root.rglob("*") if path.is_file()):
        manifest_files.append(
            {
                "path": path.relative_to(payload_root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    (payload_root / ".pressay-manifest.json").write_text(
        json.dumps({"version": version, "files": manifest_files}),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(local_appdata)
    return launcher, env


@pytest.mark.skipif(not (Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists()), reason="Windows PowerShell required")
def test_all_powershell_scripts_parse() -> None:
    files = ",".join(_ps_quote(path) for path in sorted(SCRIPTS.glob("*.ps1")))
    result = _run_powershell(
        "$failed = @(); "
        f"foreach ($file in @({files})) {{ "
        "$tokens = $null; $errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$errors); "
        "if ($errors.Count -gt 0) { $failed += ($file + ': ' + (($errors | ForEach-Object Message) -join ', ')) } "
        "}; if ($failed.Count -gt 0) { throw ($failed -join [Environment]::NewLine) }"
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not (Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists()), reason="Windows PowerShell required")
def test_managed_shortcut_round_trip_is_isolated_and_idempotent(tmp_path: Path) -> None:
    local_appdata = tmp_path / "Local AppData with spaces"
    install_root = local_appdata / "Pressay"
    install_root.mkdir(parents=True)
    (install_root / "Pressay.ps1").write_text("# isolated launcher\n", encoding="utf-8")
    legacy_project = tmp_path / "Legacy checkout with spaces"
    (legacy_project / "scripts").mkdir(parents=True)
    shortcut = tmp_path / "isolated shortcuts" / "Pressay.lnk"
    legacy = tmp_path / "isolated shortcuts" / "Legacy Pressay.lnk"
    unmanaged = tmp_path / "isolated shortcuts" / "Unmanaged.lnk"

    result = _run_powershell(
        f". {_ps_quote(SCRIPTS / 'shortcut-utils.ps1')}; "
        f"$spec = Get-PressayLauncherSpec -LocalAppData {_ps_quote(local_appdata)}; "
        f"New-PressayShortcut -ShortcutPath {_ps_quote(shortcut)} -Spec $spec; "
        f"New-PressayShortcut -ShortcutPath {_ps_quote(shortcut)} -Spec $spec; "
        f"if (-not (Test-PressayShortcut -ShortcutPath {_ps_quote(shortcut)} -Spec $spec)) {{ throw 'managed validation failed' }}; "
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$old = $shell.CreateShortcut({_ps_quote(legacy)}); "
        "$old.TargetPath = $spec.TargetPath; "
        f"$old.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"{legacy_project}\\scripts\\run.ps1\" --background'; "
        f"$old.WorkingDirectory = {_ps_quote(legacy_project)}; "
        "$old.Description = $spec.Description; $old.Save(); "
        f"New-PressayShortcut -ShortcutPath {_ps_quote(legacy)} -Spec $spec; "
        f"if (-not (Test-PressayShortcut -ShortcutPath {_ps_quote(legacy)} -Spec $spec)) {{ throw 'legacy shortcut was not migrated' }}; "
        f"$other = $shell.CreateShortcut({_ps_quote(unmanaged)}); "
        "$other.TargetPath = 'C:\\Windows\\System32\\notepad.exe'; "
        "$other.Arguments = ''; $other.WorkingDirectory = $env:TEMP; $other.Description = 'user shortcut'; $other.Save(); "
        "$refused = $false; "
        f"try {{ New-PressayShortcut -ShortcutPath {_ps_quote(unmanaged)} -Spec $spec }} catch {{ $refused = $true }}; "
        "if (-not $refused) { throw 'unmanaged shortcut was replaced' }; "
        f"$removed = Remove-PressayShortcut -ShortcutPath {_ps_quote(unmanaged)} -Spec $spec; "
        f"if ($removed -or -not (Test-Path -LiteralPath {_ps_quote(unmanaged)})) {{ throw 'unmanaged shortcut was removed' }}; "
        f"Remove-PressayShortcut -ShortcutPath {_ps_quote(shortcut)} -Spec $spec | Out-Null; "
        f"if (Test-Path -LiteralPath {_ps_quote(shortcut)}) {{ throw 'managed shortcut remained' }}; "
        "[pscustomobject]@{ TargetPath = $spec.TargetPath; Arguments = $spec.Arguments; WorkingDirectory = $spec.WorkingDirectory } | ConvertTo-Json -Compress"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["TargetPath"].lower().endswith("powershell.exe")
    assert f'-File "{install_root / "Pressay.ps1"}" --background' in payload["Arguments"]
    assert Path(payload["WorkingDirectory"]) == install_root
    assert str(legacy_project) not in payload["Arguments"]
    assert legacy.exists()
    assert unmanaged.exists()


def test_setup_passes_icon_paths_as_arguments_and_validates_runtime() -> None:
    setup = (SCRIPTS / "setup.ps1").read_text(encoding="utf-8")

    assert "QImage(sys.argv[1])" in setup
    assert "image.save(sys.argv[2], 'ICO')" in setup
    assert "r'$iconSource'" not in setup
    assert "import ctranslate2, faster_whisper" in setup
    assert "if ($runtimeCreated)" in setup
    assert setup.index("& $venvPython -m pip check") < setup.index(
        "Complete-PressayActivation"
    )
    assert setup.index("$versionRoot = Install-PressayPayload") < setup.index(
        "$venvRoot = $layout.RuntimeRoot"
    )


@pytest.mark.skipif(not (Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists()), reason="Windows PowerShell required")
def test_run_script_preserves_foreground_native_exit_code(tmp_path: Path) -> None:
    run_script, env = _isolated_run_script(tmp_path)
    env["PRESSAY_TEST_EXIT_CODE"] = "23"

    first = _run_powershell_file(run_script, env=env)
    second = _run_powershell_file(run_script, env=env)

    assert first.returncode == 23, first.stdout + first.stderr
    assert second.returncode == 23, second.stdout + second.stderr
    assert not any((Path(env["LOCALAPPDATA"]) / "Pressay" / "app").rglob("__pycache__"))


@pytest.mark.skipif(not (Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists()), reason="Windows PowerShell required")
def test_run_script_rejects_hidden_unmanifested_payload_file(tmp_path: Path) -> None:
    run_script, env = _isolated_run_script(tmp_path)
    payload_root = Path(env["LOCALAPPDATA"]) / "Pressay" / "app" / "9.8.7"
    unmanifested = payload_root / "hidden-overwrite.py"
    unmanifested.write_text("raise SystemExit(0)\n", encoding="utf-8")
    import ctypes

    assert ctypes.windll.kernel32.SetFileAttributesW(str(unmanifested), 0x2)

    try:
        result = _run_powershell_file(run_script, env=env)
    finally:
        ctypes.windll.kernel32.SetFileAttributesW(str(unmanifested), 0x80)

    assert result.returncode != 0
    assert "payload files do not match its manifest" in result.stderr


@pytest.mark.skipif(not (Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists()), reason="Windows PowerShell required")
def test_run_script_propagates_early_background_pythonw_failure(tmp_path: Path) -> None:
    run_script, env = _isolated_run_script(tmp_path)
    env["PRESSAY_TEST_EXIT_CODE"] = "29"

    result = _run_powershell_file(run_script, "--background", env=env)

    assert result.returncode == 29, result.stdout + result.stderr
    assert "exited during startup (code 29)" in result.stderr


@pytest.mark.skipif(not (Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists()), reason="Windows PowerShell required")
def test_run_script_rejects_invalid_background_launcher(tmp_path: Path) -> None:
    run_script, env = _isolated_run_script(tmp_path, use_current_python=False)
    local_appdata = Path(env["LOCALAPPDATA"])
    launchers = local_appdata / "Pressay" / "venv" / "Scripts"
    launchers.mkdir(parents=True)
    (launchers / "python.exe").write_bytes(b"not an executable")
    (launchers / "pythonw.exe").write_bytes(b"not an executable")

    result = _run_powershell_file(run_script, "--background", env=env)

    assert result.returncode == 1
    assert "Failed to launch Pressay in background" in result.stderr


@pytest.mark.skipif(not (Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists()), reason="Windows PowerShell required")
def test_destructive_uninstall_refuses_before_shortcuts_but_shortcut_only_is_allowed(
    tmp_path: Path,
) -> None:
    project = tmp_path / "Uninstall fixture with spaces"
    scripts = project / "scripts"
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
    marker = tmp_path / "shortcut calls.txt"
    helper = scripts / "shortcut-utils.ps1"
    helper.write_text(
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
        "function Get-PressayLauncherSpec {\n"
        "  [pscustomobject]@{ TargetPath = 'powershell.exe' }\n"
        "}\n"
        "function Remove-PressayShortcut {\n"
        "  [CmdletBinding(SupportsShouldProcess = $true)]\n"
        "  param([string]$ShortcutPath, [psobject]$Spec)\n"
        "  Add-Content -LiteralPath $env:PRESSAY_TEST_SHORTCUT_MARKER -Value $ShortcutPath\n"
        "}\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    local_appdata = tmp_path / "isolated local appdata"
    app_root = local_appdata / "Pressay"
    preserved = [
        app_root / "app" / "1.0.0" / "src" / "pressay" / "__main__.py",
        app_root / "venv" / "Scripts" / "python.exe",
        app_root / "config.json",
        app_root / "pressay.log",
        app_root / "models" / "cached.bin",
    ]
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserve", encoding="utf-8")
    env["LOCALAPPDATA"] = str(local_appdata)
    env["PRESSAY_TEST_SHORTCUT_MARKER"] = str(marker)

    command = (
        "$created = $false; $mutex = $null; "
        f"$mutex = New-Object System.Threading.Mutex($true, '{mutex_name}', [ref]$created); "
        "$refused = $false; "
        f"try {{ & {_ps_quote(uninstall_script)} -RemoveRuntime }} catch {{ $refused = $true }}; "
        "if (-not $refused) { exit 91 }; "
        f"if (Test-Path -LiteralPath {_ps_quote(marker)}) {{ exit 92 }}; "
        f"& {_ps_quote(uninstall_script)}; "
        "$shortcutCalls = @(Get-Content -LiteralPath $env:PRESSAY_TEST_SHORTCUT_MARKER); "
        "if ($shortcutCalls.Count -ne 3) { exit 93 }; "
        "if ($created) { $mutex.ReleaseMutex() }; $mutex.Dispose(); exit 0"
    )
    result = _run_powershell(command, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(marker.read_text(encoding="utf-8").splitlines()) == 3
    assert all(path.exists() for path in preserved)
