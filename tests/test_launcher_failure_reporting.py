from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_SCRIPT = PROJECT_ROOT / "scripts" / "run.ps1"
RUN_MACOS_SCRIPT = PROJECT_ROOT / "scripts" / "run-macos.sh"
POWERSHELL = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
WINDOWS_ONLY = pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell required")


def _run_launcher(
    script: Path,
    *arguments: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(POWERSHELL),
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


def _isolated_missing_install(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    local_appdata = tmp_path / "isolated local appdata"
    launcher = tmp_path / "Pressay.ps1"
    launcher.write_text(RUN_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(local_appdata)
    env["PRESSAY_LAUNCHER_NO_UI"] = "1"
    return launcher, env, local_appdata / "Pressay" / "launcher.log"


def _isolated_valid_install(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    local_appdata = tmp_path / "valid local appdata"
    install_root = local_appdata / "Pressay"
    version = "9.8.7"
    payload_root = install_root / "app" / version
    package = payload_root / "src" / "pressay"
    package.mkdir(parents=True)
    launcher = install_root / "Pressay.ps1"
    source = RUN_SCRIPT.read_text(encoding="utf-8")
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    source = source.replace(
        '$venvPython = Join-Path $runtimeRoot "Scripts\\python.exe"',
        f"$venvPython = '{python}'",
    ).replace(
        '$venvPythonw = Join-Path $runtimeRoot "Scripts\\pythonw.exe"',
        f"$venvPythonw = '{pythonw}'",
    )
    launcher.write_text(source, encoding="utf-8")
    (install_root / "current").write_text(version + "\n", encoding="utf-8")
    (payload_root / ".pressay-version").write_text(version + "\n", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    manifest_files = [
        {
            "path": path.relative_to(payload_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(path for path in payload_root.rglob("*") if path.is_file())
    ]
    (payload_root / ".pressay-manifest.json").write_text(
        json.dumps({"version": version, "files": manifest_files}),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(local_appdata)
    env["PRESSAY_LAUNCHER_NO_UI"] = "1"
    return launcher, env, install_root / "launcher.log"


@WINDOWS_ONLY
def test_background_missing_install_reports_to_launcher_log(tmp_path: Path) -> None:
    launcher, env, log = _isolated_missing_install(tmp_path)

    result = _run_launcher(launcher, "--background", env=env)

    assert result.returncode != 0
    entry = log.read_text(encoding="utf-8")
    assert "mode=background" in entry
    assert "Pressay is not installed" in entry


@WINDOWS_ONLY
def test_foreground_missing_install_reports_to_stderr_and_log(tmp_path: Path) -> None:
    launcher, env, log = _isolated_missing_install(tmp_path)

    result = _run_launcher(launcher, env=env)

    assert result.returncode != 0
    assert "Pressay is not installed" in result.stderr
    entry = log.read_text(encoding="utf-8")
    assert "mode=foreground" in entry
    assert "Pressay is not installed" in entry


@WINDOWS_ONLY
def test_successful_background_launch_does_not_create_launcher_log(tmp_path: Path) -> None:
    launcher, env, log = _isolated_valid_install(tmp_path)

    result = _run_launcher(launcher, "--background", env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not log.exists()


@WINDOWS_ONLY
def test_invalid_version_pointer_uses_common_failure_log(tmp_path: Path) -> None:
    launcher, env, log = _isolated_missing_install(tmp_path)
    current = Path(env["LOCALAPPDATA"]) / "Pressay" / "current"
    current.parent.mkdir(parents=True)
    current.write_text("../escaped\n", encoding="utf-8")

    result = _run_launcher(launcher, env=env)

    assert result.returncode != 0
    assert "current-version pointer is invalid" in result.stderr
    assert "current-version pointer is invalid" in log.read_text(encoding="utf-8")


@WINDOWS_ONLY
def test_launcher_log_is_trimmed_when_size_limit_is_exceeded(tmp_path: Path) -> None:
    launcher, env, log = _isolated_missing_install(tmp_path)
    log.parent.mkdir(parents=True)
    log.write_bytes(b"x" * 300_000)

    result = _run_launcher(launcher, env=env)

    assert result.returncode != 0
    assert log.stat().st_size <= 204_800
    assert "Pressay is not installed" in log.read_text(encoding="utf-8")


@WINDOWS_ONLY
def test_unavailable_launcher_log_does_not_mask_original_failure(tmp_path: Path) -> None:
    launcher = tmp_path / "Pressay.ps1"
    launcher.write_text(RUN_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    local_appdata = tmp_path / "blocked local appdata"
    local_appdata.mkdir()
    (local_appdata / "Pressay").write_text("blocks the log directory", encoding="utf-8")
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(local_appdata)
    env["PRESSAY_LAUNCHER_NO_UI"] = "1"

    result = _run_launcher(launcher, env=env)

    assert result.returncode != 0
    assert "Pressay is not installed" in result.stderr
    assert "Directory.CreateDirectory" not in result.stderr


def test_macos_launcher_routes_fatal_checks_through_bounded_log() -> None:
    source = RUN_MACOS_SCRIPT.read_text(encoding="utf-8")

    assert 'launcher_log="${launcher_root}/launcher.log"' in source
    assert "maximum_bytes=204800" in source
    assert 'fail_launcher 2 "Pressay macOS launcher can only run on macOS."' in source
    assert 'fail_launcher 1 "Pressay runtime is missing.' in source
