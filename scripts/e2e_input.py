"""Safe Win32 Unicode insertion smoke test using an isolated Qt target.

The driver is deliberately direct-input-only.  It does not read, write, or
restore the clipboard.  Input is authorized only after consecutive Win32/UIA
snapshots prove that our child process owns the foreground window and that its
focused control exposes a positive editable capability.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Callable
import uuid

from pressay.windows_input import (
    ForegroundTarget,
    snapshot_foreground_target,
    target_looks_editable,
    targets_match,
)


WINDOW_TITLE = "Pressay E2E Target"
DIRECT_EXPECTED = "Привет, Windows! Hello - Pressay"


class FocusPreconditionError(RuntimeError):
    """Raised before injection when the isolated target cannot be proven."""


def target(expected: str, window_title: str, ready_file: str | None = None) -> int:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QVBoxLayout, QWidget

    app = QApplication([])
    window = QWidget()
    window.setWindowTitle(window_title)
    window.resize(560, 130)
    layout = QVBoxLayout(window)
    layout.addWidget(QLabel("Pressay isolated Unicode input test"))
    edit = QLineEdit()
    edit.setObjectName("dictationTarget")
    edit.setAccessibleName("Pressay isolated dictation target")
    edit.setReadOnly(False)
    edit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    layout.addWidget(edit)
    window.setFocusProxy(edit)

    # The child asks Windows for focus itself.  This avoids attaching the
    # driver's input queue to whatever unrelated application is foreground.
    activation_deadline = time.monotonic() + 4.0

    def request_own_focus() -> None:
        window.showNormal()
        window.raise_()
        window.activateWindow()
        edit.setFocus()
        if (
            QApplication.activeWindow() is window
            and edit.hasFocus()
        ) or time.monotonic() >= activation_deadline:
            activation_timer.stop()

    activation_timer = QTimer()
    activation_timer.timeout.connect(request_own_focus)
    activation_timer.start(75)
    request_own_focus()

    if ready_file:
        ready_path = Path(ready_file)
        temporary_ready_path = ready_path.with_suffix(".tmp")
        temporary_ready_path.write_text(
            json.dumps(
                {"pid": os.getpid(), "hwnd": int(window.winId())},
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary_ready_path.replace(ready_path)

    deadline = time.monotonic() + 10.0

    def poll() -> None:
        if edit.text() == expected:
            print("RESULT:" + json.dumps(edit.text(), ensure_ascii=True), flush=True)
            app.exit(0)
        elif time.monotonic() >= deadline:
            print("RESULT:" + json.dumps(edit.text(), ensure_ascii=True), flush=True)
            app.exit(2)

    result_timer = QTimer()
    result_timer.timeout.connect(poll)
    result_timer.start(50)
    return int(app.exec())


def _wait_for_owned_window(
    ready_file: Path,
    window_title: str,
    launcher: subprocess.Popen[str],
    timeout: float = 5.0,
) -> tuple[int, int]:
    """Accept a HWND/PID handshake only after independent Win32 validation."""

    import win32gui
    import win32process

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready_file.is_file():
            try:
                payload = json.loads(ready_file.read_text(encoding="utf-8"))
                hwnd = int(payload["hwnd"])
                process_id = int(payload["pid"])
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                # The child writes to a sibling and replaces atomically, but a
                # defensive retry keeps malformed data fail-closed.
                pass
            else:
                if hwnd > 0 and process_id > 0 and win32gui.IsWindow(hwnd):
                    _, actual_pid = win32process.GetWindowThreadProcessId(hwnd)
                    if (
                        int(actual_pid) == process_id
                        and win32gui.IsWindowVisible(hwnd)
                        and win32gui.GetWindowText(hwnd) == window_title
                    ):
                        return hwnd, process_id
        if launcher.poll() is not None:
            _, stderr = launcher.communicate(timeout=1)
            detail = stderr.strip() or f"launcher exited {launcher.returncode}"
            raise FocusPreconditionError(
                f"isolated target exited before readiness ({detail}); no input was sent"
            )
        time.sleep(0.05)
    raise FocusPreconditionError(
        "isolated target window did not appear; no input was sent"
    )


def _close_owned_window(hwnd: int, process_id: int, window_title: str) -> None:
    """Ask only the previously validated child window to close."""

    import win32con
    import win32gui
    import win32process

    if not hwnd or not win32gui.IsWindow(hwnd):
        return
    _, actual_pid = win32process.GetWindowThreadProcessId(hwnd)
    if (
        int(actual_pid) == process_id
        and win32gui.GetWindowText(hwnd) == window_title
    ):
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


def _owned_editable_snapshot(
    snapshot: ForegroundTarget,
    *,
    expected_hwnd: int,
    expected_pid: int,
) -> bool:
    """Return True only for our foreground window and a proven text editor."""

    return (
        snapshot.is_valid
        and snapshot.hwnd == expected_hwnd
        and snapshot.pid == expected_pid
        and target_looks_editable(snapshot)
    )


def _snapshot_summary(snapshot: ForegroundTarget | None) -> str:
    if snapshot is None:
        return "snapshot_error"
    fingerprint = snapshot.focused_control
    focus_kind = (
        str(fingerprint[0])
        if isinstance(fingerprint, tuple) and fingerprint
        else "missing"
    )
    capability = ""
    if focus_kind == "uia" and isinstance(fingerprint, tuple) and len(fingerprint) >= 10:
        capability = (
            f" automation_id={fingerprint[-7]!r} class={fingerprint[-6]!r}"
            f" type={fingerprint[-5]} enabled={bool(fingerprint[-4])}"
            f" focusable={bool(fingerprint[-3])}"
            f" value_writable={bool(fingerprint[-2])}"
            f" text_editable={bool(fingerprint[-1])}"
        )
    return (
        f"hwnd={snapshot.hwnd} pid={snapshot.pid} "
        f"focus={focus_kind}{capability} editable={target_looks_editable(snapshot)}"
    )


def _preflight_owned_editable_target(
    snapshotter: Callable[[], ForegroundTarget],
    *,
    expected_hwnd: int,
    expected_pid: int,
    request_focus: Callable[[], None],
    timeout_s: float = 4.0,
    poll_s: float = 0.05,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> ForegroundTarget:
    """Acquire and immediately recheck a stable, owned editable target.

    At least two consecutive matching snapshots are required in the polling
    loop.  A third immediate snapshot is then validated before this function
    returns.  The caller must not inject anything unless this function returns.
    """

    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if poll_s < 0:
        raise ValueError("poll_s cannot be negative")

    deadline = monotonic() + timeout_s
    previous: ForegroundTarget | None = None
    last: ForegroundTarget | None = None
    last_error: Exception | None = None

    try:
        # One advisory activation of the already PID-validated child HWND.
        # The child continues requesting focus from its own Qt event loop; the
        # polling loop itself never re-activates a window.
        request_focus()
    except Exception as exc:
        last_error = exc
    sleeper(poll_s)

    while monotonic() < deadline:
        try:
            current = snapshotter()
            last = current
            last_error = None
        except Exception as exc:
            previous = None
            last_error = exc
            sleeper(poll_s)
            continue

        if _owned_editable_snapshot(
            current,
            expected_hwnd=expected_hwnd,
            expected_pid=expected_pid,
        ):
            if previous is not None and targets_match(previous, current):
                # Recheck immediately after capture.  This is the final
                # precondition before the caller is allowed to call send_text.
                try:
                    rechecked = snapshotter()
                except Exception as exc:
                    last_error = exc
                    previous = None
                else:
                    last = rechecked
                    if _owned_editable_snapshot(
                        rechecked,
                        expected_hwnd=expected_hwnd,
                        expected_pid=expected_pid,
                    ) and targets_match(current, rechecked):
                        return rechecked
                    previous = None
            else:
                previous = current
        else:
            previous = None
        sleeper(poll_s)

    detail = _snapshot_summary(last)
    if last_error is not None:
        detail += f" error={type(last_error).__name__}: {last_error}"
    raise FocusPreconditionError(
        "could not prove stable focus on the isolated editable target "
        f"({detail}); no input was sent"
    )


def _request_owned_window_focus(hwnd: int, process_id: int) -> None:
    """Request focus for our HWND without touching any foreign input queue."""

    import win32con
    import win32gui
    import win32process

    if not win32gui.IsWindow(hwnd):
        raise FocusPreconditionError("isolated target HWND no longer exists")
    _, actual_pid = win32process.GetWindowThreadProcessId(hwnd)
    if int(actual_pid) != process_id:
        raise FocusPreconditionError("isolated target HWND is no longer owned by child")
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        # SetForegroundWindow is advisory and Windows may reject it.  The
        # subsequent foreground/PID/UIA snapshots, not this call, authorize
        # injection.
        pass


def _parse_target_result(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("RESULT:"):
            return str(json.loads(line.removeprefix("RESULT:")))
    return ""


def driver(expected: str) -> int:
    from pressay.windows_input import send_text

    if "\n" in expected or "\r" in expected or any(ord(char) > 0xFFFF for char in expected):
        raise ValueError("safe E2E accepts only single-line BMP text for direct SendInput")

    window_title = f"{WINDOW_TITLE} {uuid.uuid4()}"
    hwnd = 0
    target_pid = 0
    with tempfile.TemporaryDirectory(prefix="pressay-e2e-") as temp_dir:
        ready_file = Path(temp_dir) / "target-ready.json"
        process = subprocess.Popen(
            [
                os.fspath(Path(sys.prefix) / "Scripts" / "python.exe"),
                __file__,
                "--target",
                "--expected",
                expected,
                "--window-title",
                window_title,
                "--ready-file",
                os.fspath(ready_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            hwnd, target_pid = _wait_for_owned_window(
                ready_file,
                window_title,
                process,
            )
            captured = _preflight_owned_editable_target(
                snapshot_foreground_target,
                expected_hwnd=hwnd,
                expected_pid=target_pid,
                request_focus=lambda: _request_owned_window_focus(hwnd, target_pid),
            )

            # This is the only injection call in the driver.  Preflight has
            # already established ownership/editability/stability; send_text
            # independently repeats the guard at every injection boundary.
            # Clipboard fallback is explicitly disabled.
            outcome = send_text(
                expected,
                expected_target=captured,
                modifier_timeout_s=2.0,
                fallback_to_clipboard=False,
            )
            try:
                stdout, stderr = process.communicate(timeout=12)
            except subprocess.TimeoutExpired:
                _close_owned_window(hwnd, target_pid, window_title)
                stdout, stderr = process.communicate(timeout=2)
                print(f"outcome={outcome.as_dict()}")
                if stderr:
                    print(stderr, file=sys.stderr)
                return 1
            received = _parse_target_result(stdout)
            print(
                f"status={outcome.status.value} success={outcome.success} "
                f"received_json={json.dumps(received, ensure_ascii=True)}"
            )
            if not outcome.success or received != expected or process.returncode != 0:
                print(f"captured={_snapshot_summary(captured)}")
                print(f"after={_snapshot_summary(snapshot_foreground_target())}")
                if stderr:
                    print(stderr, file=sys.stderr)
                return 1
            print("Pressay Win32 Unicode direct E2E: OK")
            return 0
        except FocusPreconditionError as exc:
            print(f"PRECONDITION FAILED: {exc}", file=sys.stderr)
            return 1
        finally:
            _close_owned_window(hwnd, target_pid, window_title)
            if process.poll() is None:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # The launcher belongs to this E2E run.  Its validated Qt
                    # child was already asked to close above.
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", action="store_true")
    parser.add_argument("--expected", default=DIRECT_EXPECTED)
    parser.add_argument("--window-title", default=WINDOW_TITLE)
    parser.add_argument("--ready-file")
    args = parser.parse_args()
    if args.target:
        return target(args.expected, args.window_title, args.ready_file)
    return driver(DIRECT_EXPECTED)


if __name__ == "__main__":
    raise SystemExit(main())
