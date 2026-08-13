from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from pressay.windows_input import ForegroundTarget, _FOCUS_UNAVAILABLE


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_driver() -> ModuleType:
    path = PROJECT_ROOT / "scripts" / "e2e_input.py"
    spec = importlib.util.spec_from_file_location("pressay_e2e_input", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


E2E = _load_driver()


def _editable_target(
    *,
    hwnd: int = 101,
    pid: int = 202,
    runtime_id: tuple[int, ...] = (7, 11),
) -> ForegroundTarget:
    return ForegroundTarget(
        hwnd=hwnd,
        pid=pid,
        focused_control=(
            "uia",
            pid,
            *runtime_id,
            "dictationTarget",
            "QLineEdit",
            50004,
            True,
            True,
            True,
            False,
        ),
    )


@pytest.mark.parametrize(
    "target",
    (
        _editable_target(hwnd=999),
        _editable_target(pid=999),
        ForegroundTarget(hwnd=101, pid=202, focused_control=_FOCUS_UNAVAILABLE),
        ForegroundTarget(
            hwnd=101,
            pid=202,
            focused_control=(
                "uia", 202, 7, 11, "dictationTarget", "QLineEdit",
                50004, True, True, False, False,
            ),
        ),
    ),
)
def test_owned_editable_snapshot_fails_closed(target: ForegroundTarget) -> None:
    assert E2E._owned_editable_snapshot(
        target,
        expected_hwnd=101,
        expected_pid=202,
    ) is False


def test_preflight_requires_three_stable_owned_editable_snapshots() -> None:
    first = _editable_target()
    second = _editable_target()
    third = _editable_target()
    snapshots = iter((first, second, third))

    captured = E2E._preflight_owned_editable_target(
        lambda: next(snapshots),
        expected_hwnd=101,
        expected_pid=202,
        request_focus=lambda: None,
        timeout_s=1.0,
        poll_s=0.0,
    )

    assert captured is third


def test_preflight_rejects_last_moment_focus_change_without_authorizing_input() -> None:
    clock_values = iter((0.0, 0.1, 0.2, 0.3, 1.1))
    stable = _editable_target()
    changed = _editable_target(hwnd=303, pid=404)
    snapshots = iter((stable, stable, changed))

    with pytest.raises(E2E.FocusPreconditionError, match="no input was sent"):
        E2E._preflight_owned_editable_target(
            lambda: next(snapshots),
            expected_hwnd=101,
            expected_pid=202,
            request_focus=lambda: None,
            timeout_s=1.0,
            poll_s=0.0,
            monotonic=lambda: next(clock_values),
            sleeper=lambda _seconds: None,
        )


def test_powershell_e2e_has_no_clipboard_mode() -> None:
    source = (PROJECT_ROOT / "scripts" / "e2e-input.ps1").read_text(encoding="utf-8")
    driver_source = (PROJECT_ROOT / "scripts" / "e2e_input.py").read_text(encoding="utf-8")

    assert "IncludeClipboard" not in source
    assert "--case" not in source
    assert "fallback_to_clipboard=False" in driver_source
