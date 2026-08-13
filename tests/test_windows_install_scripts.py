from __future__ import annotations

import json
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


def _isolated_run_script(tmp_path: Path, *, use_current_python: bool = True) -> Path:
    project = tmp_path / "Run fixture with spaces"
    scripts = project / "scripts"
    package = project / "src" / "pressay"
    scripts.mkdir(parents=True)
    package.mkdir(parents=True)
    run_source = (SCRIPTS / "run.ps1").read_text(encoding="utf-8")
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    if use_current_python:
        run_source = run_source.replace(
            '$venvPython = Join-Path $env:LOCALAPPDATA "Pressay\\venv\\Scripts\\python.exe"',
            f"$venvPython = '{python}'",
        ).replace(
            '$venvPythonw = Join-Path $env:LOCALAPPDATA "Pressay\\venv\\Scripts\\pythonw.exe"',
            f"$venvPythonw = '{pythonw}'",
        )
    (scripts / "run.ps1").write_text(run_source, encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import os, sys\n"
        "sys.exit(int(os.environ.get('PRESSAY_TEST_EXIT_CODE', '0')))\n",
        encoding="utf-8",
    )
    return scripts / "run.ps1"


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
    project = tmp_path / "Project with spaces"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "run.ps1").write_text("# isolated launcher\n", encoding="utf-8")
    shortcut = tmp_path / "isolated shortcuts" / "Pressay.lnk"
    unmanaged = tmp_path / "isolated shortcuts" / "Unmanaged.lnk"

    result = _run_powershell(
        f". {_ps_quote(SCRIPTS / 'shortcut-utils.ps1')}; "
        f"$spec = Get-PressayLauncherSpec -ProjectRoot {_ps_quote(project)}; "
        f"New-PressayShortcut -ShortcutPath {_ps_quote(shortcut)} -Spec $spec; "
        f"New-PressayShortcut -ShortcutPath {_ps_quote(shortcut)} -Spec $spec; "
        f"if (-not (Test-PressayShortcut -ShortcutPath {_ps_quote(shortcut)} -Spec $spec)) {{ throw 'managed validation failed' }}; "
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$other = $shell.CreateShortcut({_ps_quote(unmanaged)}); "
        "$other.TargetPath = 'C:\\Windows\\System32\\notepad.exe'; "
        "$other.Arguments = ''; $other.WorkingDirectory = $env:TEMP; $other.Description = 'user shortcut'; $other.Save(); "
        f"$removed = Remove-PressayShortcut -ShortcutPath {_ps_quote(unmanaged)} -Spec $spec; "
        f"if ($removed -or -not (Test-Path -LiteralPath {_ps_quote(unmanaged)})) {{ throw 'unmanaged shortcut was removed' }}; "
        f"Remove-PressayShortcut -ShortcutPath {_ps_quote(shortcut)} -Spec $spec | Out-Null; "
        f"if (Test-Path -LiteralPath {_ps_quote(shortcut)}) {{ throw 'managed shortcut remained' }}; "
        "[pscustomobject]@{ TargetPath = $spec.TargetPath; Arguments = $spec.Arguments; WorkingDirectory = $spec.WorkingDirectory } | ConvertTo-Json -Compress"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["TargetPath"].lower().endswith("powershell.exe")
    assert f'-File "{scripts / "run.ps1"}" --background' in payload["Arguments"]
    assert Path(payload["WorkingDirectory"]) == project
    assert unmanaged.exists()


@pytest.mark.skipif(not (Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists()), reason="Windows PowerShell required")
def test_run_script_preserves_foreground_native_exit_code(tmp_path: Path) -> None:
    run_script = _isolated_run_script(tmp_path)
    env = os.environ.copy()
    env["PRESSAY_TEST_EXIT_CODE"] = "23"

    result = _run_powershell_file(run_script, env=env)

    assert result.returncode == 23, result.stdout + result.stderr


@pytest.mark.skipif(not (Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists()), reason="Windows PowerShell required")
def test_run_script_propagates_early_background_pythonw_failure(tmp_path: Path) -> None:
    run_script = _isolated_run_script(tmp_path)
    env = os.environ.copy()
    env["PRESSAY_TEST_EXIT_CODE"] = "29"

    result = _run_powershell_file(run_script, "--background", env=env)

    assert result.returncode == 29, result.stdout + result.stderr
    assert "exited during startup (code 29)" in result.stderr


@pytest.mark.skipif(not (Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists()), reason="Windows PowerShell required")
def test_run_script_rejects_invalid_background_launcher(tmp_path: Path) -> None:
    run_script = _isolated_run_script(tmp_path, use_current_python=False)
    local_appdata = tmp_path / "isolated local appdata"
    launchers = local_appdata / "Pressay" / "venv" / "Scripts"
    launchers.mkdir(parents=True)
    (launchers / "python.exe").write_bytes(b"not an executable")
    (launchers / "pythonw.exe").write_bytes(b"not an executable")
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(local_appdata)

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
        "function Get-PressayLauncherSpec {\n"
        "  param([string]$ProjectRoot)\n"
        "  [pscustomobject]@{ ProjectRoot = $ProjectRoot }\n"
        "}\n"
        "function Remove-PressayShortcut {\n"
        "  [CmdletBinding(SupportsShouldProcess = $true)]\n"
        "  param([string]$ShortcutPath, [psobject]$Spec)\n"
        "  Add-Content -LiteralPath $env:PRESSAY_TEST_SHORTCUT_MARKER -Value $ShortcutPath\n"
        "}\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(tmp_path / "isolated local appdata")
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
