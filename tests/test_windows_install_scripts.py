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
    versioned_runtime: bool = False,
    runtime_version: str | None = None,
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
    if versioned_runtime:
        selected_runtime = runtime_version or version
        runtime_contract = {
            "schema": 1,
            "version": selected_runtime,
            "dependency_contract_sha256": "a" * 64,
            "python_tag": "cp311-win_amd64",
        }
        payload_contract = (
            {
                "schema": 2,
                "version": version,
                "runtime_version": selected_runtime,
                "dependency_contract_sha256": "a" * 64,
                "python_tag": "cp311-win_amd64",
            }
            if runtime_version is not None
            else runtime_contract
        )
        (payload_root / ".pressay-runtime.json").write_text(
            json.dumps(payload_contract),
            encoding="utf-8",
        )
        runtime_root = install_root / "runtime" / selected_runtime
        runtime_root.mkdir(parents=True)
        (runtime_root / ".pressay-runtime.json").write_text(
            json.dumps(runtime_contract),
            encoding="utf-8",
        )
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
        "Complete-PressayRuntimeBuild"
    )
    assert setup.index("Complete-PressayRuntimeBuild") < setup.index(
        "Complete-PressayActivation"
    )
    assert setup.index("$versionRoot = Install-PressayPayload") < setup.index(
        "$runtimeBuild = Initialize-PressayRuntimeBuild"
    )
    assert setup.index("$runtimeVersion = Get-PressayRuntimeVersionForInstall") < (
        setup.index("$versionRoot = Install-PressayPayload")
    )
    assert "-RuntimeVersion $runtimeVersion" in setup
    assert "-Version $runtimeVersion" in setup
    assert "$venvRoot = $layout.RuntimeRoot" not in setup
    assert "-UninstallerSource" in setup


def test_local_maintenance_scripts_resolve_the_active_runtime() -> None:
    for name in ("doctor.ps1", "test.ps1", "smoke-app.ps1", "e2e-input.ps1"):
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "Get-PressayActiveRuntimeRoot" in text, name
        assert "Pressay\\venv\\Scripts\\python.exe" not in text, name


@pytest.mark.skipif(not (Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists()), reason="Windows PowerShell required")
def test_doctor_wrapper_preserves_utf8_native_output(tmp_path: Path) -> None:
    source = (SCRIPTS / "doctor.ps1").read_text(encoding="utf-8")
    runtime_block = (
        "$projectRoot = Split-Path -Parent $PSScriptRoot\n"
        '. (Join-Path $PSScriptRoot "install-layout.ps1")\n'
        "$layout = Get-PressayInstallLayout\n"
        "$runtimeRoot = Get-PressayActiveRuntimeRoot -Layout $layout\n"
        '$venvPython = Join-Path $runtimeRoot "Scripts\\python.exe"\n'
    )
    isolated_runtime = tmp_path / "isolated runtime"
    replacement = (
        f"$projectRoot = {_ps_quote(PROJECT_ROOT)}\n"
        f"$runtimeRoot = {_ps_quote(isolated_runtime)}\n"
        f"$venvPython = {_ps_quote(Path(sys.executable))}\n"
    )
    assert runtime_block in source
    source = source.replace(runtime_block, replacement, 1)
    invocation = "& $venvPython -m pressay.doctor @args"
    assert invocation in source
    source = source.replace(
        invocation,
        "& $venvPython -c \"import sys; "
        "sys.stdout.buffer.write(('\\u041c\\u0438\\u043a\\u0440\\u043e\\u0444\\u043e\\u043d \\u2713\\n').encode('utf-8'))\"",
        1,
    )
    script = tmp_path / "doctor-utf8.ps1"
    script.write_text(source, encoding="utf-8")

    result = _run_powershell_file(script)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "Микрофон ✓\n"


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
def test_run_script_uses_matching_versioned_runtime(tmp_path: Path) -> None:
    run_script, env = _isolated_run_script(tmp_path, versioned_runtime=True)
    env["PRESSAY_TEST_EXIT_CODE"] = "17"

    result = _run_powershell_file(run_script, env=env)

    assert result.returncode == 17, result.stdout + result.stderr


@pytest.mark.skipif(not (Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists()), reason="Windows PowerShell required")
def test_run_script_uses_referenced_shared_runtime(tmp_path: Path) -> None:
    run_script, env = _isolated_run_script(
        tmp_path,
        versioned_runtime=True,
        runtime_version="9.7.0",
    )
    env["PRESSAY_TEST_EXIT_CODE"] = "19"

    result = _run_powershell_file(run_script, env=env)

    assert result.returncode == 19, result.stdout + result.stderr


@pytest.mark.skipif(not (Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists()), reason="Windows PowerShell required")
def test_run_script_rejects_unsafe_shared_runtime_reference(tmp_path: Path) -> None:
    run_script, env = _isolated_run_script(
        tmp_path,
        versioned_runtime=True,
        runtime_version="9.7.0",
    )
    payload_root = Path(env["LOCALAPPDATA"]) / "Pressay" / "app" / "9.8.7"
    contract_path = payload_root / ".pressay-runtime.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["runtime_version"] = "../outside"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    manifest_path = payload_root / ".pressay-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == ".pressay-runtime.json":
            entry["sha256"] = hashlib.sha256(contract_path.read_bytes()).hexdigest()
            break
    else:
        raise AssertionError("runtime contract is absent from the test manifest")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_powershell_file(run_script, env=env)

    assert result.returncode != 0
    assert "payload runtime contract is invalid" in result.stderr


@pytest.mark.skipif(not (Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists()), reason="Windows PowerShell required")
def test_marked_payload_never_falls_back_to_legacy_runtime(tmp_path: Path) -> None:
    run_script, env = _isolated_run_script(tmp_path, versioned_runtime=True)
    install_root = Path(env["LOCALAPPDATA"]) / "Pressay"
    shutil.rmtree(install_root / "runtime")
    legacy_scripts = install_root / "venv" / "Scripts"
    legacy_scripts.mkdir(parents=True)
    (legacy_scripts / "python.exe").write_bytes(b"legacy")
    (legacy_scripts / "pythonw.exe").write_bytes(b"legacy")

    result = _run_powershell_file(run_script, env=env)

    assert result.returncode != 0
    assert "runtime contract is missing" in result.stderr


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
    ).replace(
        "Local\\Pressay.Desktop.Installer",
        installer_mutex_name,
    )
    uninstall_script = scripts / "uninstall.ps1"
    uninstall_script.write_text(uninstall_source, encoding="utf-8")
    env = os.environ.copy()
    local_appdata = tmp_path / "isolated local appdata"
    app_root = local_appdata / "Pressay"
    shortcut_directory = tmp_path / "empty shortcut directory"
    shortcut_directory.mkdir()
    preserved = [
        app_root / "app" / "1.0.0" / "src" / "pressay" / "__main__.py",
        app_root / "runtime" / "1.0.0" / "venv" / "Scripts" / "python.exe",
        app_root / "venv" / "Scripts" / "python.exe",
        app_root / "config.json",
        app_root / "pressay.log",
        app_root / "models" / "cached.bin",
    ]
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserve", encoding="utf-8")
    env["LOCALAPPDATA"] = str(local_appdata)

    command = (
        "$created = $false; $mutex = $null; "
        f"$mutex = New-Object System.Threading.Mutex($true, '{mutex_name}', [ref]$created); "
        "$refused = $false; $message = ''; "
        f"try {{ & {_ps_quote(uninstall_script)} -RemoveRuntime -ShortcutDirectories {_ps_quote(shortcut_directory)} }} catch {{ $refused = $true; $message = $_.Exception.Message }}; "
        "if (-not $refused) { exit 91 }; "
        "if ($message -notmatch 'Exit it from the tray menu') { exit 92 }; "
        f"& {_ps_quote(uninstall_script)} -ShortcutDirectories {_ps_quote(shortcut_directory)}; "
        "if ($created) { $mutex.ReleaseMutex() }; $mutex.Dispose(); exit 0"
    )
    result = _run_powershell(command, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert all(path.exists() for path in preserved)
