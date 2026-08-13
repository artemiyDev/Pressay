"""Guarded Windows text delivery for Pressay.

Single-line text is emitted with Unicode ``SendInput`` events, so it never
touches the clipboard.  Multiline text uses a short clipboard transaction and
restores only the previous Unicode text when no other process changed the
clipboard in the meantime.

Win32 and ``ctypes`` are loaded lazily.  The pure helpers and injectable
protocols remain importable and testable on every platform.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import importlib
import os
import queue
import threading
import time
from types import SimpleNamespace
from typing import Callable, Iterable, Optional, Protocol, Sequence


VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5

PHYSICAL_MODIFIER_KEYS = (
    VK_SHIFT,
    VK_CONTROL,
    VK_MENU,
    VK_LWIN,
    VK_RWIN,
    VK_LSHIFT,
    VK_RSHIFT,
    VK_LCONTROL,
    VK_RCONTROL,
    VK_LMENU,
    VK_RMENU,
)


class InputStatus(str, Enum):
    """Stable, machine-readable status values for input operations."""

    INSERTED_UNICODE = "inserted_unicode"
    PASTED_CLIPBOARD = "pasted_clipboard"
    COPIED = "copied"
    NO_TEXT = "no_text"
    NO_LAST_TEXT = "no_last_text"
    TARGET_REQUIRED = "target_required"
    NO_FOREGROUND = "no_foreground"
    TARGET_MISMATCH = "target_mismatch"
    MODIFIERS_HELD = "modifiers_held"
    INPUT_FAILED = "input_failed"
    CLIPBOARD_FAILED = "clipboard_failed"
    CLIPBOARD_CHANGED = "clipboard_changed"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ForegroundTarget:
    """Identity of the window that owned focus when recording started."""

    hwnd: int
    pid: int
    title: str = ""
    focused_control: tuple[object, ...] | None = None
    captured_at: float = field(default_factory=time.monotonic, compare=False)

    @property
    def is_valid(self) -> bool:
        return self.hwnd > 0 and self.pid > 0


# A failed focus probe must not degrade to ``None == None`` and accidentally
# authorize input.  ``targets_match`` treats this explicit value as always
# unequal, including to another unavailable result.
_FOCUS_UNAVAILABLE: tuple[object, ...] = ("focus_unavailable",)


@dataclass(frozen=True)
class InputOutcome:
    """Explicit result of a text insertion, paste, or copy operation.

    ``success`` means the requested input operation completed.  ``copied``
    means the requested text is known to remain in the clipboard, including a
    fail-safe fallback after insertion was refused.
    """

    status: InputStatus
    success: bool
    copied: bool = False
    reason: Optional[str] = None
    detail: Optional[str] = None
    method: Optional[str] = None
    characters_sent: int = 0
    target: Optional[ForegroundTarget] = None
    current_target: Optional[ForegroundTarget] = None

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["status"] = self.status.value
        return result


@dataclass(frozen=True)
class ClipboardSnapshot:
    has_text: bool
    text: str
    sequence: int
    all_formats: object | None = None


@dataclass(frozen=True)
class ClipboardPasteResult:
    success: bool
    copied: bool
    restored: bool
    reason: Optional[str] = None
    detail: Optional[str] = None


class WindowsInputError(RuntimeError):
    """Base exception for direct Win32 input helpers."""


class WindowsInputUnavailable(WindowsInputError):
    """Raised when a Win32-only backend is requested off Windows."""


class ClipboardReplaceError(WindowsInputError):
    """A temporary clipboard replacement failed with known rollback state."""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        restored: bool,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.restored = bool(restored)


class InputBackend(Protocol):
    def snapshot_foreground_target(self) -> ForegroundTarget: ...

    def is_physical_key_down(self, vk_code: int) -> bool: ...

    def send_unicode_units(self, units: Sequence[int]) -> bool: ...

    def send_ctrl_v(self) -> bool: ...

    def send_enter(self) -> bool: ...


class ClipboardBackend(Protocol):
    def sequence_number(self) -> int: ...

    def get_text(self) -> tuple[bool, str]: ...

    def set_text(self, text: str) -> None: ...


def windows_input_available() -> bool:
    return os.name == "nt"


def targets_match(expected: ForegroundTarget, current: ForegroundTarget) -> bool:
    """Match stable window identity; titles are informative and may change."""

    window_matches = (
        expected.is_valid
        and current.is_valid
        and expected.hwnd == current.hwnd
        and expected.pid == current.pid
    )
    if not window_matches:
        return False
    if (
        expected.focused_control == _FOCUS_UNAVAILABLE
        or current.focused_control == _FOCUS_UNAVAILABLE
    ):
        return False
    if expected.focused_control is None:
        return current.focused_control is None
    return expected.focused_control == current.focused_control


@dataclass(frozen=True, slots=True)
class _UIARequest:
    process_id: int
    reply: queue.Queue[tuple[object, ...]]


def _load_uiautomation() -> object:
    """Import UI Automation only inside its owning worker apartment."""

    return importlib.import_module("uiautomation")


class _UIAFingerprintWorker:
    """Own the process-global uiautomation singleton on exactly one thread.

    ``uiautomation`` stores its IUIAutomation COM interface in a process-wide
    singleton.  Initializing COM in an arbitrary caller thread does not make
    that interface apartment-safe.  A dedicated daemon therefore performs the
    import, singleton creation, focused-element query, and property reads.  It
    returns only immutable primitive tuples to callers.

    UIA providers can themselves hang.  Callers wait for a bounded interval;
    a timeout marks this worker unhealthy until that exact provider call
    returns. The one-slot queue and ``_pending`` gate ensure a truly stuck
    provider cannot accumulate work, while a merely slow Chromium/Qt query can
    recover without forcing the whole application to restart.
    """

    def __init__(
        self,
        *,
        loader: Callable[[], object] = _load_uiautomation,
        default_timeout_s: float = 1.5,
    ) -> None:
        if default_timeout_s <= 0:
            raise ValueError("default_timeout_s must be positive")
        self._loader = loader
        self.default_timeout_s = float(default_timeout_s)
        self._requests: queue.Queue[_UIARequest] = queue.Queue(maxsize=1)
        self._state_lock = threading.Lock()
        self._pending = False
        self._unhealthy = False
        self._thread = threading.Thread(
            target=self._run,
            name="PressayUIAutomation",
            daemon=True,
        )
        self._thread.start()

    @property
    def unhealthy(self) -> bool:
        with self._state_lock:
            return self._unhealthy

    def _mark_unhealthy(self) -> None:
        with self._state_lock:
            self._unhealthy = True

    def _mark_healthy(self) -> None:
        with self._state_lock:
            self._unhealthy = False

    def query(
        self,
        process_id: int,
        *,
        timeout_s: float | None = None,
    ) -> tuple[object, ...]:
        timeout = self.default_timeout_s if timeout_s is None else float(timeout_s)
        if timeout <= 0:
            raise ValueError("timeout_s must be positive")

        reply: queue.Queue[tuple[object, ...]] = queue.Queue(maxsize=1)
        with self._state_lock:
            if self._unhealthy or self._pending:
                return _FOCUS_UNAVAILABLE
            self._pending = True
        try:
            try:
                self._requests.put_nowait(_UIARequest(int(process_id), reply))
            except queue.Full:
                return _FOCUS_UNAVAILABLE
            try:
                result = reply.get(timeout=timeout)
            except queue.Empty:
                self._mark_unhealthy()
                return _FOCUS_UNAVAILABLE
            return result
        finally:
            with self._state_lock:
                self._pending = False

    @staticmethod
    def _read_fingerprint(automation: object, process_id: int) -> tuple[object, ...]:
        control = automation.GetFocusedControl()  # type: ignore[attr-defined]
        if int(getattr(control, "ProcessId", 0) or 0) != process_id:
            return _FOCUS_UNAVAILABLE
        runtime_id = tuple(int(part) for part in control.GetRuntimeId())
        if not runtime_id:
            return _FOCUS_UNAVAILABLE

        enabled = bool(getattr(control, "IsEnabled", False))
        keyboard_focusable = bool(getattr(control, "IsKeyboardFocusable", False))

        def pattern(pattern_id: int) -> object | None:
            try:
                return control.GetPattern(pattern_id)
            except Exception:
                return None

        pattern_ids = automation.PatternId  # type: ignore[attr-defined]
        value_pattern = pattern(int(pattern_ids.ValuePattern))
        if value_pattern is None:
            value_writable = False
        else:
            try:
                value_writable = not bool(value_pattern.IsReadOnly)
            except Exception:
                value_writable = False
        # TextPattern is also exposed by read-only documents. TextEditPattern
        # is the positive UIA capability for an interactive rich-text editor.
        text_editable = pattern(int(pattern_ids.TextEditPattern)) is not None
        return (
            "uia",
            process_id,
            *runtime_id,
            str(getattr(control, "AutomationId", "") or ""),
            str(getattr(control, "ClassName", "") or ""),
            int(getattr(control, "ControlType", 0) or 0),
            enabled,
            keyboard_focusable,
            value_writable,
            text_editable,
        )

    def _run(self) -> None:
        automation: object | None = None
        initialized = False
        try:
            automation = self._loader()
            automation.InitializeUIAutomationInCurrentThread()  # type: ignore[attr-defined]
            initialized = True
        except Exception:
            self._mark_unhealthy()

        while True:
            request = self._requests.get()
            if automation is None or not initialized:
                result = _FOCUS_UNAVAILABLE
            else:
                try:
                    result = self._read_fingerprint(automation, request.process_id)
                except Exception:
                    result = _FOCUS_UNAVAILABLE
                # The provider call returned (even if that particular element
                # could not be described). Recover a circuit breaker that may
                # have opened only because the caller's bounded wait expired.
                self._mark_healthy()
            try:
                request.reply.put_nowait(result)
            except queue.Full:
                pass


_UIA_WORKER: _UIAFingerprintWorker | None = None
_UIA_WORKER_LOCK = threading.Lock()


def _uia_focused_control_fingerprint(process_id: int) -> tuple[object, ...]:
    global _UIA_WORKER

    with _UIA_WORKER_LOCK:
        if _UIA_WORKER is None:
            _UIA_WORKER = _UIAFingerprintWorker()
        worker = _UIA_WORKER
    return worker.query(process_id)


def target_guard(expected: ForegroundTarget, current: ForegroundTarget) -> bool:
    """Readable alias used at each injection boundary."""

    return targets_match(expected, current)


def target_looks_editable(target: ForegroundTarget) -> bool:
    """Allow only controls with positive evidence that they accept text."""

    fingerprint = target.focused_control
    if not fingerprint or fingerprint == _FOCUS_UNAVAILABLE:
        return False
    if fingerprint[0] == "win32_focus":
        return True
    if fingerprint[0] != "uia" or len(fingerprint) < 10:
        return False
    control_type = int(fingerprint[-5])
    enabled = bool(fingerprint[-4])
    keyboard_focusable = bool(fingerprint[-3])
    value_writable = bool(fingerprint[-2])
    text_editable = bool(fingerprint[-1])
    # UIA Edit, Document and Custom.  Control type alone is insufficient:
    # read-only edits/documents and arbitrary focusable custom widgets remain
    # blocked unless they expose a writable ValuePattern or TextEditPattern.
    return (
        control_type in {50004, 50030, 50025}
        and enabled
        and keyboard_focusable
        and (value_writable or text_editable)
    )


def utf16_code_units(text: str) -> tuple[int, ...]:
    """Return UTF-16 code units, including surrogate pairs for non-BMP text."""

    encoded = text.encode("utf-16-le", errors="surrogatepass")
    return tuple(
        int.from_bytes(encoded[index : index + 2], "little")
        for index in range(0, len(encoded), 2)
    )


def batched(values: Sequence[int], batch_size: int) -> Iterable[tuple[int, ...]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    for start in range(0, len(values), batch_size):
        yield tuple(values[start : start + batch_size])


def _cancel_requested(cancelled: Callable[[], bool] | None) -> bool:
    """Treat a failed cancellation probe as cancelled (input must fail closed)."""

    if cancelled is None:
        return False
    try:
        return bool(cancelled())
    except Exception:
        return True


def _cancelled_outcome(
    *,
    target: Optional[ForegroundTarget] = None,
    current_target: Optional[ForegroundTarget] = None,
    characters_sent: int = 0,
) -> InputOutcome:
    return InputOutcome(
        status=InputStatus.CANCELLED,
        success=False,
        copied=False,
        reason="operation_cancelled",
        characters_sent=characters_sent,
        target=target,
        current_target=current_target,
    )


def wait_for_physical_modifiers_clear(
    is_pressed: Callable[[int], bool],
    *,
    timeout_s: float = 0.8,
    poll_interval_s: float = 0.01,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    modifier_keys: Sequence[int] = PHYSICAL_MODIFIER_KEYS,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Wait until Ctrl/Shift/Alt/Win are physically released.

    The injected ``clock`` and ``sleeper`` keep this helper deterministic in
    tests.  A timeout avoids accidentally typing while a hotkey is still held.
    """

    if timeout_s < 0 or poll_interval_s <= 0:
        raise ValueError("timeout_s must be non-negative and poll_interval_s positive")
    deadline = clock() + timeout_s
    while True:
        if _cancel_requested(cancelled):
            return False
        if not any(is_pressed(vk_code) for vk_code in modifier_keys):
            return not _cancel_requested(cancelled)
        now = clock()
        if now >= deadline:
            return False
        sleeper(min(poll_interval_s, max(0.0, deadline - now)))


def _snapshot_clipboard_text(
    clipboard: ClipboardBackend, attempts: int = 2
) -> ClipboardSnapshot:
    """Take a text snapshot associated with a stable clipboard sequence."""

    for _ in range(max(1, attempts)):
        before = clipboard.sequence_number()
        all_formats = None
        capture_all = getattr(clipboard, "capture_all_formats", None)
        if callable(capture_all):
            all_formats = capture_all()
        has_text, text = clipboard.get_text()
        after = clipboard.sequence_number()
        if before == after:
            return ClipboardSnapshot(
                has_text=has_text,
                text=text,
                sequence=after,
                all_formats=all_formats,
            )
    raise WindowsInputError("clipboard_changed_while_reading")


def clipboard_paste_transaction(
    text: str,
    *,
    clipboard: ClipboardBackend,
    paste: Callable[[], bool],
    guard: Callable[[], bool] = lambda: True,
    settle_s: float = 0.08,
    sleeper: Callable[[float], None] = time.sleep,
    cancelled: Callable[[], bool] | None = None,
) -> ClipboardPasteResult:
    """Paste text and conditionally restore the previous Unicode text.

    Restoration occurs only when the clipboard sequence still belongs to this
    transaction.  If a user or another application changes the clipboard, the
    newer value is never overwritten.
    """

    begin_transaction = getattr(clipboard, "begin_transaction", None)
    end_transaction = getattr(clipboard, "end_transaction", None)
    transaction_started = callable(begin_transaction)
    if transaction_started:
        begin_transaction()
    try:
        return _clipboard_paste_transaction_inner(
            text,
            clipboard=clipboard,
            paste=paste,
            guard=guard,
            settle_s=settle_s,
            sleeper=sleeper,
            cancelled=cancelled,
        )
    finally:
        if transaction_started and callable(end_transaction):
            end_transaction()


def _clipboard_paste_transaction_inner(
    text: str,
    *,
    clipboard: ClipboardBackend,
    paste: Callable[[], bool],
    guard: Callable[[], bool],
    settle_s: float,
    sleeper: Callable[[float], None],
    cancelled: Callable[[], bool] | None,
) -> ClipboardPasteResult:
    if _cancel_requested(cancelled):
        return ClipboardPasteResult(
            success=False,
            copied=False,
            restored=False,
            reason="operation_cancelled",
        )
    try:
        previous = _snapshot_clipboard_text(clipboard)
        if _cancel_requested(cancelled):
            return ClipboardPasteResult(
                success=False,
                copied=False,
                restored=False,
                reason="operation_cancelled",
            )
        replace_if_unchanged = getattr(clipboard, "replace_text_if_sequence", None)
        if callable(replace_if_unchanged):
            if bool(getattr(clipboard, "supports_transactional_restore", False)):
                replaced = replace_if_unchanged(
                    text,
                    previous.sequence,
                    rollback_data_object=previous.all_formats,
                )
            else:
                replaced = replace_if_unchanged(text, previous.sequence)
            if not replaced:
                return ClipboardPasteResult(
                    success=False,
                    copied=False,
                    restored=False,
                    reason="clipboard_changed_before_write",
                )
        else:
            # Backends without an atomic replace API are retained for pure
            # tests/portable adapters. The Win32 backend uses the guarded path.
            if clipboard.sequence_number() != previous.sequence:
                return ClipboardPasteResult(
                    success=False,
                    copied=False,
                    restored=False,
                    reason="clipboard_changed_before_write",
                )
            clipboard.set_text(text)
        our_sequence = clipboard.sequence_number()
    except ClipboardReplaceError as exc:
        return ClipboardPasteResult(
            success=False,
            copied=False,
            restored=exc.restored,
            reason=exc.reason,
            detail=str(exc),
        )
    except Exception as exc:
        return ClipboardPasteResult(
            success=False,
            copied=False,
            restored=False,
            reason="clipboard_write_failed",
            detail=str(exc),
        )

    # A failed target guard must never inject or turn this temporary clipboard
    # transaction into an implicit copy operation. Restore the full snapshot
    # while our sequence still owns the clipboard.
    try:
        guarded = guard()
    except Exception as exc:
        reason = "operation_cancelled" if _cancel_requested(cancelled) else "target_guard_failed"
        return _rollback_temporary_clipboard(
            clipboard,
            previous=previous,
            our_sequence=our_sequence,
            failure_reason=reason,
            failure_detail=None if reason == "operation_cancelled" else str(exc),
        )
    if _cancel_requested(cancelled):
        return _rollback_temporary_clipboard(
            clipboard,
            previous=previous,
            our_sequence=our_sequence,
            failure_reason="operation_cancelled",
        )
    if not guarded:
        return _rollback_temporary_clipboard(
            clipboard,
            previous=previous,
            our_sequence=our_sequence,
            failure_reason="target_mismatch",
        )

    # Do not paste somebody else's clipboard value if it changed in the tiny
    # interval between our write and SendInput.
    try:
        if clipboard.sequence_number() != our_sequence:
            return ClipboardPasteResult(
                success=False,
                copied=False,
                restored=False,
                reason="clipboard_changed_before_paste",
            )
    except Exception as exc:
        return _rollback_temporary_clipboard(
            clipboard,
            previous=previous,
            our_sequence=our_sequence,
            failure_reason="clipboard_sequence_failed",
            failure_detail=str(exc),
        )

    # This is the final cancellation boundary before the backend's Ctrl+V
    # SendInput call.  No controller lock is held while waiting above.
    if _cancel_requested(cancelled):
        return _rollback_temporary_clipboard(
            clipboard,
            previous=previous,
            our_sequence=our_sequence,
            failure_reason="operation_cancelled",
        )

    try:
        pasted = bool(paste())
    except Exception as exc:
        pasted = False
        paste_detail = str(exc)
    else:
        paste_detail = None
    if not pasted:
        return _rollback_temporary_clipboard(
            clipboard,
            previous=previous,
            our_sequence=our_sequence,
            failure_reason="paste_input_failed",
            failure_detail=paste_detail,
        )

    if settle_s > 0:
        sleeper(settle_s)

    try:
        unchanged = clipboard.sequence_number() == our_sequence
    except Exception as exc:
        return ClipboardPasteResult(
            success=True,
            copied=False,
            restored=False,
            reason="clipboard_sequence_failed_after_paste",
            detail=str(exc),
        )

    if not unchanged:
        return ClipboardPasteResult(
            success=True,
            copied=False,
            restored=False,
            reason="clipboard_changed_not_restored",
        )

    restore_all = getattr(clipboard, "restore_all_formats", None)
    if previous.all_formats is not None and callable(restore_all):
        try:
            restore_all(previous.all_formats)
        except Exception as exc:
            return ClipboardPasteResult(
                success=True,
                copied=True,
                restored=False,
                reason="clipboard_restore_failed",
                detail=str(exc),
            )
        return ClipboardPasteResult(success=True, copied=False, restored=True)

    if not previous.has_text:
        # We cannot safely recreate non-text formats, so retain the transcript
        # instead of clearing a clipboard that did not originally contain text.
        return ClipboardPasteResult(
            success=True,
            copied=True,
            restored=False,
            reason="no_text_snapshot_to_restore",
        )

    try:
        clipboard.set_text(previous.text)
    except Exception as exc:
        return ClipboardPasteResult(
            success=True,
            copied=True,
            restored=False,
            reason="clipboard_restore_failed",
            detail=str(exc),
        )
    return ClipboardPasteResult(success=True, copied=False, restored=True)


def _rollback_temporary_clipboard(
    clipboard: ClipboardBackend,
    *,
    previous: ClipboardSnapshot,
    our_sequence: int,
    failure_reason: str,
    failure_detail: str | None = None,
) -> ClipboardPasteResult:
    """Best-effort rollback after a temporary write that was never pasted.

    Restoration is allowed only while our clipboard sequence still owns the
    clipboard.  A concurrent user/application update therefore always wins.
    The sequence check is retried once so a transient query failure can still
    restore the retained OLE object without weakening that ownership guard.
    """

    current_sequence: int | None = None
    sequence_error: Exception | None = None
    for _ in range(2):
        try:
            current_sequence = clipboard.sequence_number()
        except Exception as exc:
            sequence_error = exc
            continue
        break

    if current_sequence is None:
        detail_parts = [part for part in (failure_detail, str(sequence_error)) if part]
        return ClipboardPasteResult(
            success=False,
            copied=False,
            restored=False,
            reason="clipboard_restore_failed",
            detail=f"{failure_reason}; " + "; ".join(detail_parts),
        )
    if current_sequence != our_sequence:
        detail = failure_detail
        changed_detail = "clipboard changed before rollback; original not restored"
        detail = f"{detail}; {changed_detail}" if detail else changed_detail
        return ClipboardPasteResult(
            success=False,
            copied=False,
            restored=False,
            reason=failure_reason,
            detail=detail,
        )

    try:
        restore_all = getattr(clipboard, "restore_all_formats", None)
        if previous.all_formats is not None and callable(restore_all):
            restore_all(previous.all_formats)
            return ClipboardPasteResult(
                success=False,
                copied=False,
                restored=True,
                reason=failure_reason,
                detail=failure_detail,
            )
        if previous.has_text:
            clipboard.set_text(previous.text)
            return ClipboardPasteResult(
                success=False,
                copied=False,
                restored=True,
                reason=failure_reason,
                detail=failure_detail,
            )
    except Exception as exc:
        detail_parts = [part for part in (failure_detail, str(exc)) if part]
        return ClipboardPasteResult(
            success=False,
            copied=False,
            restored=False,
            reason="clipboard_restore_failed",
            detail=f"{failure_reason}; " + "; ".join(detail_parts),
        )
    return ClipboardPasteResult(
        success=False,
        copied=False,
        restored=False,
        reason="clipboard_restore_failed",
        detail=f"{failure_reason}; original clipboard format cannot be restored",
    )
_WIN32_API: Optional[SimpleNamespace] = None


def _load_win32_api() -> SimpleNamespace:
    global _WIN32_API
    if _WIN32_API is not None:
        return _WIN32_API
    if not windows_input_available():
        raise WindowsInputUnavailable("Windows text input requires Windows")

    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = (
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        )

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = (
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        )

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = (
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        )

    class INPUTUNION(ctypes.Union):
        _fields_ = (
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        )

    class INPUT(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = (("type", wintypes.DWORD), ("value", INPUTUNION))

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", wintypes.RECT),
        )

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetGUIThreadInfo.argtypes = (
        wintypes.DWORD,
        ctypes.POINTER(GUITHREADINFO),
    )
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = (
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    user32.GetAsyncKeyState.restype = wintypes.SHORT
    user32.SendInput.argtypes = (
        wintypes.UINT,
        ctypes.POINTER(INPUT),
        ctypes.c_int,
    )
    user32.SendInput.restype = wintypes.UINT

    user32.OpenClipboard.argtypes = (wintypes.HWND,)
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = ()
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = ()
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = (wintypes.UINT,)
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardSequenceNumber.argtypes = ()
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

    kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    _WIN32_API = SimpleNamespace(
        ctypes=ctypes,
        wintypes=wintypes,
        user32=user32,
        kernel32=kernel32,
        INPUT=INPUT,
        KEYBDINPUT=KEYBDINPUT,
        GUITHREADINFO=GUITHREADINFO,
    )
    return _WIN32_API


def _native_focused_control_fingerprint(
    api: SimpleNamespace,
    *,
    foreground_hwnd: int,
    foreground_thread_id: int,
    process_id: int,
) -> tuple[object, ...] | None:
    """Return a cheap child-HWND identity when Win32 exposes real focus.

    Native edit controls have their own child HWND, unlike Qt/Electron render
    surfaces whose focus HWND is normally the top-level window.  The former can
    be guarded without COM; the latter deliberately falls through to the
    dedicated UIA worker for control-level identity.
    """

    if foreground_hwnd <= 0 or foreground_thread_id <= 0 or process_id <= 0:
        return None
    info = api.GUITHREADINFO()
    info.cbSize = api.ctypes.sizeof(api.GUITHREADINFO)
    if not api.user32.GetGUIThreadInfo(foreground_thread_id, api.ctypes.byref(info)):
        return None
    focus_hwnd = int(info.hwndFocus or 0)
    if not focus_hwnd or focus_hwnd == foreground_hwnd:
        return None

    focus_process_id = api.wintypes.DWORD()
    api.user32.GetWindowThreadProcessId(
        info.hwndFocus,
        api.ctypes.byref(focus_process_id),
    )
    # GA_ROOT == 2.  A focus HWND outside the captured top-level window is not
    # trustworthy and must fall through to the fail-closed UIA path.
    root_hwnd = int(api.user32.GetAncestor(info.hwndFocus, 2) or 0)
    if int(focus_process_id.value) != process_id or root_hwnd != foreground_hwnd:
        return None

    class_buffer = api.ctypes.create_unicode_buffer(256)
    api.user32.GetClassNameW(info.hwndFocus, class_buffer, len(class_buffer))
    class_name = class_buffer.value
    normalized_class = class_name.casefold()

    is_text_editor = (
        normalized_class == "edit"
        or normalized_class.startswith("richedit")
        or normalized_class.startswith("scintilla")
    )
    if is_text_editor:
        # GWL_STYLE == -16; ES_READONLY == 0x0800. Window styles are a 32-bit
        # value even on 64-bit Windows, so GetWindowLongW is the correct ABI.
        style = int(api.user32.GetWindowLongW(info.hwndFocus, -16)) & 0xFFFFFFFF
        if style & 0x0800:
            return _FOCUS_UNAVAILABLE
        return (
            "win32_focus",
            process_id,
            focus_hwnd,
            class_name,
        )

    known_non_text = (
        normalized_class.startswith("button")
        or normalized_class.startswith("static")
        or normalized_class.startswith("listbox")
        or normalized_class.startswith("combobox")
        or normalized_class.startswith("syslistview32")
        or normalized_class.startswith("systreeview32")
        or normalized_class.startswith("toolbarwindow32")
    )
    if known_non_text:
        return _FOCUS_UNAVAILABLE

    # Unknown child HWND classes may be custom text surfaces. UIA provides the
    # capability evidence needed to decide safely.
    return None


class Win32InputBackend:
    """Thin ctypes wrapper around foreground and SendInput APIs."""

    def __init__(self) -> None:
        self._api = _load_win32_api()

    def snapshot_foreground_target(self) -> ForegroundTarget:
        api = self._api
        hwnd_raw = api.user32.GetForegroundWindow()
        hwnd = int(hwnd_raw or 0)
        if not hwnd:
            return ForegroundTarget(hwnd=0, pid=0, title="")
        process_id = api.wintypes.DWORD()
        foreground_thread_id = int(
            api.user32.GetWindowThreadProcessId(hwnd_raw, api.ctypes.byref(process_id))
        )
        length = max(0, int(api.user32.GetWindowTextLengthW(hwnd_raw)))
        title_buffer = api.ctypes.create_unicode_buffer(length + 1)
        api.user32.GetWindowTextW(hwnd_raw, title_buffer, len(title_buffer))
        native_focus = _native_focused_control_fingerprint(
            api,
            foreground_hwnd=hwnd,
            foreground_thread_id=foreground_thread_id,
            process_id=int(process_id.value),
        )
        focused_control = (
            native_focus
            if native_focus is not None
            else _uia_focused_control_fingerprint(int(process_id.value))
        )
        return ForegroundTarget(
            hwnd=hwnd,
            pid=int(process_id.value),
            title=title_buffer.value,
            focused_control=focused_control,
        )

    def is_physical_key_down(self, vk_code: int) -> bool:
        return bool(self._api.user32.GetAsyncKeyState(vk_code) & 0x8000)

    def _send_keyboard_inputs(self, inputs: Sequence[tuple[int, int, int]]) -> bool:
        api = self._api
        if not inputs:
            return True
        array_type = api.INPUT * len(inputs)
        items = array_type(
            *(
                api.INPUT(
                    type=1,  # INPUT_KEYBOARD
                    ki=api.KEYBDINPUT(
                        wVk=vk_code,
                        wScan=scan_code,
                        dwFlags=flags,
                        time=0,
                        dwExtraInfo=0,
                    ),
                )
                for vk_code, scan_code, flags in inputs
            )
        )
        sent = int(api.user32.SendInput(len(items), items, api.ctypes.sizeof(api.INPUT)))
        # SendInput only queues events. Give the target thread a moment before
        # the next UIA focus probe; synchronous accessibility calls can
        # otherwise overtake the queued keyboard messages on Qt/Electron.
        if sent == len(items):
            time.sleep(0.001)
        return sent == len(items)

    def send_unicode_units(self, units: Sequence[int]) -> bool:
        events: list[tuple[int, int, int]] = []
        for unit in units:
            events.append((0, int(unit), 0x0004))  # KEYEVENTF_UNICODE
            events.append((0, int(unit), 0x0004 | 0x0002))  # plus KEYUP
        return self._send_keyboard_inputs(events)

    def send_ctrl_v(self) -> bool:
        return self._send_keyboard_inputs(
            (
                (VK_CONTROL, 0, 0),
                (0x56, 0, 0),
                (0x56, 0, 0x0002),
                (VK_CONTROL, 0, 0x0002),
            )
        )

    def send_enter(self) -> bool:
        return self._send_keyboard_inputs(
            (
                (0x0D, 0, 0),  # VK_RETURN down
                (0x0D, 0, 0x0002),
            )
        )


class Win32Clipboard:
    """Unicode-text-only Windows clipboard adapter."""

    CF_UNICODETEXT = 13
    supports_transactional_restore = True

    def __init__(
        self,
        *,
        open_timeout_s: float = 0.25,
        retry_interval_s: float = 0.01,
    ) -> None:
        self._api = _load_win32_api()
        self.open_timeout_s = open_timeout_s
        self.retry_interval_s = retry_interval_s
        self._com_transaction_depth = 0

    def begin_transaction(self) -> None:
        import pythoncom

        if self._com_transaction_depth == 0:
            pythoncom.CoInitialize()
        self._com_transaction_depth += 1

    def end_transaction(self) -> None:
        import pythoncom

        if self._com_transaction_depth <= 0:
            return
        self._com_transaction_depth -= 1
        if self._com_transaction_depth == 0:
            pythoncom.CoUninitialize()

    def _open(self) -> None:
        deadline = time.monotonic() + self.open_timeout_s
        while not self._api.user32.OpenClipboard(None):
            if time.monotonic() >= deadline:
                error = self._api.ctypes.get_last_error()
                raise WindowsInputError(f"OpenClipboard failed ({error})")
            time.sleep(self.retry_interval_s)

    def sequence_number(self) -> int:
        return int(self._api.user32.GetClipboardSequenceNumber())

    def capture_all_formats(self) -> object:
        """Hold the complete OLE IDataObject, including rich/private formats."""

        import pythoncom

        owns_com = self._com_transaction_depth == 0
        if owns_com:
            pythoncom.CoInitialize()
        try:
            return pythoncom.OleGetClipboard()
        finally:
            if owns_com:
                pythoncom.CoUninitialize()

    def restore_all_formats(self, data_object: object) -> None:
        """Restore and materialize a previously retained OLE data object."""

        import pythoncom

        owns_com = self._com_transaction_depth == 0
        if owns_com:
            pythoncom.CoInitialize()
        try:
            pythoncom.OleSetClipboard(data_object)
            pythoncom.OleFlushClipboard()
        finally:
            if owns_com:
                pythoncom.CoUninitialize()

    def replace_text_if_sequence(
        self,
        text: str,
        expected_sequence: int,
        *,
        rollback_data_object: object | None = None,
    ) -> bool:
        """Verify, replace, and restore rich data after a partial write failure.

        Only this backend can know that ``EmptyClipboard`` succeeded while it
        owned the clipboard lock. If a later allocation/copy/SetClipboardData
        step fails, it closes the native lock and restores the retained OLE
        object while the surrounding COM transaction is still alive. A changed
        sequence is treated as external ownership and is never overwritten.
        """

        api = self._api
        encoded = text.encode("utf-16-le", errors="surrogatepass") + b"\x00\x00"
        self._open()
        memory = None
        transferred = False
        emptied = False
        owned_sequence: int | None = None
        failure: Exception | None = None
        replaced = False
        try:
            if self.sequence_number() != expected_sequence:
                return False
            if not api.user32.EmptyClipboard():
                error = api.ctypes.get_last_error()
                raise WindowsInputError(f"EmptyClipboard failed ({error})")
            emptied = True
            owned_sequence = self.sequence_number()
            memory = api.kernel32.GlobalAlloc(0x0002, len(encoded))
            if not memory:
                error = api.ctypes.get_last_error()
                raise WindowsInputError(f"GlobalAlloc failed ({error})")
            pointer = api.kernel32.GlobalLock(memory)
            if not pointer:
                error = api.ctypes.get_last_error()
                raise WindowsInputError(f"GlobalLock failed ({error})")
            try:
                api.ctypes.memmove(pointer, encoded, len(encoded))
            finally:
                api.kernel32.GlobalUnlock(memory)
            if not api.user32.SetClipboardData(self.CF_UNICODETEXT, memory):
                error = api.ctypes.get_last_error()
                raise WindowsInputError(f"SetClipboardData failed ({error})")
            transferred = True
            replaced = True
        except Exception as exc:
            failure = exc
        finally:
            if memory and not transferred:
                api.kernel32.GlobalFree(memory)
            api.user32.CloseClipboard()

        if replaced:
            return True
        assert failure is not None
        if not emptied or rollback_data_object is None:
            raise failure

        try:
            current_sequence = self.sequence_number()
        except Exception as sequence_error:
            raise ClipboardReplaceError(
                f"{failure}; clipboard ownership check failed: {sequence_error}",
                reason="clipboard_restore_failed",
                restored=False,
            ) from failure
        if owned_sequence is None or current_sequence != owned_sequence:
            raise ClipboardReplaceError(
                f"{failure}; clipboard changed after partial write; original not restored",
                reason="clipboard_changed_during_write",
                restored=False,
            ) from failure
        try:
            self.restore_all_formats(rollback_data_object)
        except Exception as restore_error:
            raise ClipboardReplaceError(
                f"{failure}; clipboard restore failed: {restore_error}",
                reason="clipboard_restore_failed",
                restored=False,
            ) from failure
        raise ClipboardReplaceError(
            str(failure),
            reason="clipboard_write_failed",
            restored=True,
        ) from failure

    def get_text(self) -> tuple[bool, str]:
        api = self._api
        self._open()
        try:
            if not api.user32.IsClipboardFormatAvailable(self.CF_UNICODETEXT):
                return False, ""
            handle = api.user32.GetClipboardData(self.CF_UNICODETEXT)
            if not handle:
                error = api.ctypes.get_last_error()
                raise WindowsInputError(f"GetClipboardData failed ({error})")
            pointer = api.kernel32.GlobalLock(handle)
            if not pointer:
                error = api.ctypes.get_last_error()
                raise WindowsInputError(f"GlobalLock failed ({error})")
            try:
                return True, api.ctypes.wstring_at(pointer)
            finally:
                api.kernel32.GlobalUnlock(handle)
        finally:
            api.user32.CloseClipboard()

    def set_text(self, text: str) -> None:
        api = self._api
        encoded = text.encode("utf-16-le", errors="surrogatepass") + b"\x00\x00"
        self._open()
        memory = None
        transferred = False
        try:
            if not api.user32.EmptyClipboard():
                error = api.ctypes.get_last_error()
                raise WindowsInputError(f"EmptyClipboard failed ({error})")
            memory = api.kernel32.GlobalAlloc(0x0002, len(encoded))  # GMEM_MOVEABLE
            if not memory:
                error = api.ctypes.get_last_error()
                raise WindowsInputError(f"GlobalAlloc failed ({error})")
            pointer = api.kernel32.GlobalLock(memory)
            if not pointer:
                error = api.ctypes.get_last_error()
                raise WindowsInputError(f"GlobalLock failed ({error})")
            try:
                api.ctypes.memmove(pointer, encoded, len(encoded))
            finally:
                api.kernel32.GlobalUnlock(memory)
            if not api.user32.SetClipboardData(self.CF_UNICODETEXT, memory):
                error = api.ctypes.get_last_error()
                raise WindowsInputError(f"SetClipboardData failed ({error})")
            transferred = True  # Windows now owns the HGLOBAL.
        finally:
            if memory and not transferred:
                api.kernel32.GlobalFree(memory)
            api.user32.CloseClipboard()


def _default_backend() -> InputBackend:
    return Win32InputBackend()


def _default_clipboard() -> ClipboardBackend:
    return Win32Clipboard()


def snapshot_foreground_target(
    *, backend: Optional[InputBackend] = None
) -> ForegroundTarget:
    """Capture HWND, PID, and title of the current foreground window."""

    return (backend or _default_backend()).snapshot_foreground_target()


capture_foreground_target = snapshot_foreground_target


def _copy_fallback(
    text: str,
    *,
    clipboard: Optional[ClipboardBackend],
) -> tuple[bool, Optional[str]]:
    try:
        (clipboard or _default_clipboard()).set_text(text)
        return True, None
    except Exception as exc:
        return False, str(exc)


def _failure(
    status: InputStatus,
    reason: str,
    text: str,
    *,
    clipboard: Optional[ClipboardBackend],
    fallback_to_clipboard: bool,
    target: Optional[ForegroundTarget] = None,
    current_target: Optional[ForegroundTarget] = None,
    characters_sent: int = 0,
    detail: Optional[str] = None,
) -> InputOutcome:
    copied = False
    if fallback_to_clipboard and text:
        copied, copy_error = _copy_fallback(text, clipboard=clipboard)
        if copy_error:
            detail = f"{detail}; clipboard fallback: {copy_error}" if detail else copy_error
    return InputOutcome(
        status=status,
        success=False,
        copied=copied,
        reason=reason,
        detail=detail,
        method="clipboard_copy" if copied else None,
        characters_sent=characters_sent,
        target=target,
        current_target=current_target,
    )


def _safe_snapshot(backend: InputBackend) -> tuple[Optional[ForegroundTarget], Optional[str]]:
    try:
        return backend.snapshot_foreground_target(), None
    except Exception as exc:
        return None, str(exc)


def _guard_callback(
    backend: InputBackend,
    expected: ForegroundTarget,
    observed: list[Optional[ForegroundTarget]],
) -> Callable[[], bool]:
    def guard() -> bool:
        current = backend.snapshot_foreground_target()
        observed[0] = current
        return target_guard(expected, current)

    return guard


def send_text(
    text: str,
    expected_target: Optional[ForegroundTarget] = None,
    *,
    backend: Optional[InputBackend] = None,
    clipboard: Optional[ClipboardBackend] = None,
    modifier_timeout_s: float = 0.8,
    batch_size: int = 96,
    clipboard_settle_s: float = 0.08,
    fallback_to_clipboard: bool = True,
    press_enter: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
    cancelled: Callable[[], bool] | None = None,
) -> InputOutcome:
    """Deliver transcribed text to the window captured at recording start.

    A missing or mismatched target is a hard injection stop.  When enabled,
    fail-safe fallback copies the full text and reports ``copied=True`` without
    pretending that insertion succeeded.
    """

    if not isinstance(text, str):
        raise TypeError("text must be str")
    if _cancel_requested(cancelled):
        return _cancelled_outcome(target=expected_target)
    if not text and not press_enter:
        return InputOutcome(
            status=InputStatus.NO_TEXT,
            success=False,
            reason="empty_text",
        )
    if expected_target is None:
        return _failure(
            InputStatus.TARGET_REQUIRED,
            "recording_target_required",
            text,
            clipboard=clipboard,
            fallback_to_clipboard=fallback_to_clipboard,
        )
    if not target_looks_editable(expected_target):
        return _failure(
            InputStatus.TARGET_MISMATCH,
            "focused_control_is_not_editable",
            text,
            clipboard=clipboard,
            fallback_to_clipboard=fallback_to_clipboard,
            target=expected_target,
            current_target=expected_target,
        )

    try:
        active_backend = backend or _default_backend()
    except Exception as exc:
        return _failure(
            InputStatus.UNAVAILABLE,
            "windows_input_unavailable",
            text,
            clipboard=clipboard,
            fallback_to_clipboard=fallback_to_clipboard,
            target=expected_target,
            detail=str(exc),
        )

    current, snapshot_error = _safe_snapshot(active_backend)
    if _cancel_requested(cancelled):
        return _cancelled_outcome(
            target=expected_target,
            current_target=current,
        )
    if current is None:
        return _failure(
            InputStatus.INPUT_FAILED,
            "foreground_snapshot_failed",
            text,
            clipboard=clipboard,
            fallback_to_clipboard=fallback_to_clipboard,
            target=expected_target,
            detail=snapshot_error,
        )
    if not current.is_valid:
        return _failure(
            InputStatus.NO_FOREGROUND,
            "no_foreground_window",
            text,
            clipboard=clipboard,
            fallback_to_clipboard=fallback_to_clipboard,
            target=expected_target,
            current_target=current,
        )
    if not targets_match(expected_target, current):
        return _failure(
            InputStatus.TARGET_MISMATCH,
            "foreground_target_changed",
            text,
            clipboard=clipboard,
            fallback_to_clipboard=fallback_to_clipboard,
            target=expected_target,
            current_target=current,
        )

    try:
        modifiers_clear = wait_for_physical_modifiers_clear(
            active_backend.is_physical_key_down,
            timeout_s=modifier_timeout_s,
            sleeper=sleeper,
            cancelled=cancelled,
        )
    except Exception as exc:
        return _failure(
            InputStatus.INPUT_FAILED,
            "modifier_state_failed",
            text,
            clipboard=clipboard,
            fallback_to_clipboard=fallback_to_clipboard,
            target=expected_target,
            current_target=current,
            detail=str(exc),
        )
    if _cancel_requested(cancelled):
        return _cancelled_outcome(
            target=expected_target,
            current_target=current,
        )
    if not modifiers_clear:
        return _failure(
            InputStatus.MODIFIERS_HELD,
            "physical_modifiers_not_released",
            text,
            clipboard=clipboard,
            fallback_to_clipboard=fallback_to_clipboard,
            target=expected_target,
            current_target=current,
        )

    if not text and press_enter:
        latest, snapshot_error = _safe_snapshot(active_backend)
        if _cancel_requested(cancelled):
            return _cancelled_outcome(
                target=expected_target,
                current_target=latest,
            )
        if latest is None or not targets_match(expected_target, latest):
            return InputOutcome(
                status=InputStatus.TARGET_MISMATCH,
                success=False,
                reason="foreground_target_changed_before_enter",
                detail=snapshot_error,
                target=expected_target,
                current_target=latest,
            )
        try:
            if _cancel_requested(cancelled):
                return _cancelled_outcome(
                    target=expected_target,
                    current_target=latest,
                )
            enter_sent = bool(active_backend.send_enter())
        except Exception as exc:
            return InputOutcome(
                status=InputStatus.INPUT_FAILED,
                success=False,
                reason="enter_send_failed",
                detail=str(exc),
                target=expected_target,
                current_target=latest,
            )
        return InputOutcome(
            status=InputStatus.INSERTED_UNICODE,
            success=enter_sent,
            reason=None if enter_sent else "enter_send_failed",
            method="sendinput_enter",
            target=expected_target,
            current_target=latest,
        )

    # Some Windows controls reorder a surrogate pair delivered as separate
    # KEYEVENTF_UNICODE events. Use one clipboard paste for non-BMP text so
    # emoji and historic scripts preserve exact character order.
    if "\n" in text or "\r" in text or any(ord(character) > 0xFFFF for character in text):
        try:
            active_clipboard = clipboard or _default_clipboard()
        except Exception as exc:
            return InputOutcome(
                status=InputStatus.CLIPBOARD_FAILED,
                success=False,
                copied=False,
                reason="clipboard_unavailable",
                detail=str(exc),
                target=expected_target,
                current_target=current,
            )
        observed: list[Optional[ForegroundTarget]] = [current]
        result = clipboard_paste_transaction(
            text,
            clipboard=active_clipboard,
            paste=active_backend.send_ctrl_v,
            guard=_guard_callback(active_backend, expected_target, observed),
            settle_s=clipboard_settle_s,
            sleeper=sleeper,
            cancelled=cancelled,
        )
        if result.success:
            if press_enter:
                latest, enter_guard_error = _safe_snapshot(active_backend)
                if _cancel_requested(cancelled):
                    return _cancelled_outcome(
                        target=expected_target,
                        current_target=latest,
                        characters_sent=len(text),
                    )
                if latest is None or not targets_match(expected_target, latest):
                    return _failure(
                        InputStatus.TARGET_MISMATCH,
                        "foreground_target_changed_before_enter",
                        text,
                        clipboard=active_clipboard,
                        fallback_to_clipboard=fallback_to_clipboard,
                        target=expected_target,
                        current_target=latest,
                        characters_sent=len(text),
                        detail=enter_guard_error,
                    )
                try:
                    if _cancel_requested(cancelled):
                        return _cancelled_outcome(
                            target=expected_target,
                            current_target=latest,
                            characters_sent=len(text),
                        )
                    enter_sent = bool(active_backend.send_enter())
                except Exception as exc:
                    enter_sent = False
                    enter_error = str(exc)
                else:
                    enter_error = None
                if not enter_sent:
                    return _failure(
                        InputStatus.INPUT_FAILED,
                        "enter_send_failed",
                        text,
                        clipboard=active_clipboard,
                        fallback_to_clipboard=fallback_to_clipboard,
                        target=expected_target,
                        current_target=latest,
                        characters_sent=len(text),
                        detail=enter_error,
                    )
            return InputOutcome(
                status=InputStatus.PASTED_CLIPBOARD,
                success=True,
                copied=result.copied,
                reason=result.reason,
                detail=result.detail,
                method="clipboard_paste",
                characters_sent=len(text),
                target=expected_target,
                current_target=observed[0],
            )
        status = (
            InputStatus.CANCELLED
            if result.reason == "operation_cancelled"
            else InputStatus.TARGET_MISMATCH
            if result.reason in ("target_mismatch", "target_guard_failed")
            else InputStatus.CLIPBOARD_CHANGED
            if result.reason in (
                "clipboard_changed_before_write",
                "clipboard_changed_before_paste",
                "clipboard_changed_during_write",
            )
            else InputStatus.INPUT_FAILED
            if result.reason == "paste_input_failed"
            else InputStatus.CLIPBOARD_FAILED
        )
        return InputOutcome(
            status=status,
            success=False,
            copied=result.copied,
            reason=result.reason,
            detail=result.detail,
            method="clipboard_copy" if result.copied else None,
            target=expected_target,
            current_target=observed[0],
        )

    units = utf16_code_units(text)
    sent_units = 0
    for unit_batch in batched(units, batch_size):
        latest, snapshot_error = _safe_snapshot(active_backend)
        if _cancel_requested(cancelled):
            return _cancelled_outcome(
                target=expected_target,
                current_target=latest,
                characters_sent=sent_units,
            )
        if latest is None or not targets_match(expected_target, latest):
            return _failure(
                InputStatus.TARGET_MISMATCH,
                "foreground_target_changed",
                text,
                clipboard=clipboard,
                fallback_to_clipboard=fallback_to_clipboard,
                target=expected_target,
                current_target=latest,
                characters_sent=sent_units,
                detail=snapshot_error,
            )
        try:
            if _cancel_requested(cancelled):
                return _cancelled_outcome(
                    target=expected_target,
                    current_target=latest,
                    characters_sent=sent_units,
                )
            sent = bool(active_backend.send_unicode_units(unit_batch))
        except Exception as exc:
            sent = False
            send_error = str(exc)
        else:
            send_error = None
        if not sent:
            return _failure(
                InputStatus.INPUT_FAILED,
                "unicode_send_failed",
                text,
                clipboard=clipboard,
                fallback_to_clipboard=fallback_to_clipboard,
                target=expected_target,
                current_target=latest,
                characters_sent=sent_units,
                detail=send_error,
            )
        sent_units += len(unit_batch)

    if press_enter:
        latest, enter_guard_error = _safe_snapshot(active_backend)
        if _cancel_requested(cancelled):
            return _cancelled_outcome(
                target=expected_target,
                current_target=latest,
                characters_sent=sent_units,
            )
        if latest is None or not targets_match(expected_target, latest):
            return _failure(
                InputStatus.TARGET_MISMATCH,
                "foreground_target_changed_before_enter",
                text,
                clipboard=clipboard,
                fallback_to_clipboard=fallback_to_clipboard,
                target=expected_target,
                current_target=latest,
                characters_sent=sent_units,
                detail=enter_guard_error,
            )
        try:
            if _cancel_requested(cancelled):
                return _cancelled_outcome(
                    target=expected_target,
                    current_target=latest,
                    characters_sent=sent_units,
                )
            enter_sent = bool(active_backend.send_enter())
        except Exception as exc:
            enter_sent = False
            enter_error = str(exc)
        else:
            enter_error = None
        if not enter_sent:
            return _failure(
                InputStatus.INPUT_FAILED,
                "enter_send_failed",
                text,
                clipboard=clipboard,
                fallback_to_clipboard=fallback_to_clipboard,
                target=expected_target,
                current_target=latest,
                characters_sent=sent_units,
                detail=enter_error,
            )

    return InputOutcome(
        status=InputStatus.INSERTED_UNICODE,
        success=True,
        copied=False,
        method="unicode_sendinput",
        characters_sent=len(text),
        target=expected_target,
        current_target=current,
    )


insert_text = send_text


def paste_last(
    text: str,
    expected_target: Optional[ForegroundTarget] = None,
    *,
    backend: Optional[InputBackend] = None,
    clipboard: Optional[ClipboardBackend] = None,
    modifier_timeout_s: float = 0.8,
    clipboard_settle_s: float = 0.08,
    sleeper: Callable[[float], None] = time.sleep,
    cancelled: Callable[[], bool] | None = None,
) -> InputOutcome:
    """Paste remembered text; a previously saved target is optional.

    Without ``expected_target`` the current foreground target is captured for
    this command, then checked again immediately before Ctrl+V is injected.
    """

    if not isinstance(text, str):
        raise TypeError("text must be str")
    if _cancel_requested(cancelled):
        return _cancelled_outcome(target=expected_target)
    if not text:
        return InputOutcome(
            status=InputStatus.NO_LAST_TEXT,
            success=False,
            reason="no_last_text",
        )
    try:
        active_backend = backend or _default_backend()
        active_clipboard = clipboard or _default_clipboard()
    except Exception as exc:
        return InputOutcome(
            status=InputStatus.UNAVAILABLE,
            success=False,
            reason="windows_input_unavailable",
            detail=str(exc),
        )

    # Refuse a caller-supplied non-editable target before comparing focus.
    # In particular, the explicit unavailable sentinel intentionally never
    # matches itself; letting that comparison run first would incorrectly take
    # the clipboard-copy fallback for a known Button/read-only control.
    if expected_target is not None and not target_looks_editable(expected_target):
        return InputOutcome(
            status=InputStatus.TARGET_MISMATCH,
            success=False,
            copied=False,
            reason="focused_control_is_not_editable",
            target=expected_target,
            current_target=expected_target,
        )

    current, snapshot_error = _safe_snapshot(active_backend)
    if _cancel_requested(cancelled):
        return _cancelled_outcome(
            target=expected_target,
            current_target=current,
        )
    if current is None or not current.is_valid:
        copied, copy_error = _copy_fallback(text, clipboard=active_clipboard)
        return InputOutcome(
            status=InputStatus.NO_FOREGROUND,
            success=False,
            copied=copied,
            reason="no_foreground_window" if current else "foreground_snapshot_failed",
            detail=snapshot_error or copy_error,
            method="clipboard_copy" if copied else None,
            current_target=current,
        )
    guarded_target = expected_target or current
    if expected_target is not None and not targets_match(expected_target, current):
        copied, copy_error = _copy_fallback(text, clipboard=active_clipboard)
        return InputOutcome(
            status=InputStatus.TARGET_MISMATCH,
            success=False,
            copied=copied,
            reason="foreground_target_changed",
            detail=copy_error,
            method="clipboard_copy" if copied else None,
            target=expected_target,
            current_target=current,
        )
    if not target_looks_editable(guarded_target):
        return InputOutcome(
            status=InputStatus.TARGET_MISMATCH,
            success=False,
            copied=False,
            reason="focused_control_is_not_editable",
            target=guarded_target,
            current_target=current,
        )

    if not wait_for_physical_modifiers_clear(
        active_backend.is_physical_key_down,
        timeout_s=modifier_timeout_s,
        sleeper=sleeper,
        cancelled=cancelled,
    ):
        if _cancel_requested(cancelled):
            return _cancelled_outcome(
                target=guarded_target,
                current_target=current,
            )
        copied, copy_error = _copy_fallback(text, clipboard=active_clipboard)
        return InputOutcome(
            status=InputStatus.MODIFIERS_HELD,
            success=False,
            copied=copied,
            reason="physical_modifiers_not_released",
            detail=copy_error,
            method="clipboard_copy" if copied else None,
            target=guarded_target,
            current_target=current,
        )

    observed: list[Optional[ForegroundTarget]] = [current]
    result = clipboard_paste_transaction(
        text,
        clipboard=active_clipboard,
        paste=active_backend.send_ctrl_v,
        guard=_guard_callback(active_backend, guarded_target, observed),
        settle_s=clipboard_settle_s,
        sleeper=sleeper,
        cancelled=cancelled,
    )
    if result.success:
        return InputOutcome(
            status=InputStatus.PASTED_CLIPBOARD,
            success=True,
            copied=result.copied,
            reason=result.reason,
            detail=result.detail,
            method="clipboard_paste",
            characters_sent=len(text),
            target=guarded_target,
            current_target=observed[0],
        )
    status = (
        InputStatus.CANCELLED
        if result.reason == "operation_cancelled"
        else InputStatus.TARGET_MISMATCH
        if result.reason in ("target_mismatch", "target_guard_failed")
        else InputStatus.CLIPBOARD_CHANGED
        if result.reason in (
            "clipboard_changed_before_paste",
            "clipboard_changed_during_write",
        )
        else InputStatus.CLIPBOARD_FAILED
        if result.reason and result.reason.startswith("clipboard_")
        else InputStatus.INPUT_FAILED
    )
    return InputOutcome(
        status=status,
        success=False,
        copied=result.copied,
        reason=result.reason,
        detail=result.detail,
        method="clipboard_copy" if result.copied else None,
        target=guarded_target,
        current_target=observed[0],
    )


def copy_last(
    text: str,
    *,
    clipboard: Optional[ClipboardBackend] = None,
) -> InputOutcome:
    """Copy remembered text without requiring or touching a target window."""

    if not isinstance(text, str):
        raise TypeError("text must be str")
    if not text:
        return InputOutcome(
            status=InputStatus.NO_LAST_TEXT,
            success=False,
            reason="no_last_text",
        )
    copied, error = _copy_fallback(text, clipboard=clipboard)
    return InputOutcome(
        status=InputStatus.COPIED if copied else InputStatus.CLIPBOARD_FAILED,
        success=copied,
        copied=copied,
        reason=None if copied else "clipboard_write_failed",
        detail=error,
        method="clipboard_copy" if copied else None,
    )


def copy_text(
    text: str,
    *,
    clipboard: Optional[ClipboardBackend] = None,
) -> InputOutcome:
    """Compatibility name for copying arbitrary/last transcribed text."""

    return copy_last(text, clipboard=clipboard)


class WindowsInputController:
    """Small stateful facade that owns the most recent transcript."""

    def __init__(
        self,
        *,
        backend: Optional[InputBackend] = None,
        clipboard: Optional[ClipboardBackend] = None,
    ) -> None:
        self.backend = backend
        self.clipboard = clipboard
        self.last_text: Optional[str] = None

    def capture_target(self) -> ForegroundTarget:
        return snapshot_foreground_target(backend=self.backend)

    def deliver(self, text: str, target: ForegroundTarget, **kwargs: object) -> InputOutcome:
        self.last_text = text
        return send_text(
            text,
            target,
            backend=self.backend,
            clipboard=self.clipboard,
            **kwargs,
        )

    def paste_last(
        self, target: Optional[ForegroundTarget] = None, **kwargs: object
    ) -> InputOutcome:
        return paste_last(
            self.last_text or "",
            target,
            backend=self.backend,
            clipboard=self.clipboard,
            **kwargs,
        )

    def copy_last(self) -> InputOutcome:
        return copy_last(self.last_text or "", clipboard=self.clipboard)


WindowsInputManager = WindowsInputController


__all__ = [
    "ClipboardBackend",
    "ClipboardPasteResult",
    "ClipboardReplaceError",
    "ClipboardSnapshot",
    "ForegroundTarget",
    "InputBackend",
    "InputOutcome",
    "InputStatus",
    "PHYSICAL_MODIFIER_KEYS",
    "Win32Clipboard",
    "Win32InputBackend",
    "WindowsInputController",
    "WindowsInputError",
    "WindowsInputManager",
    "WindowsInputUnavailable",
    "batched",
    "capture_foreground_target",
    "clipboard_paste_transaction",
    "copy_last",
    "copy_text",
    "insert_text",
    "paste_last",
    "send_text",
    "snapshot_foreground_target",
    "target_guard",
    "targets_match",
    "utf16_code_units",
    "wait_for_physical_modifiers_clear",
    "windows_input_available",
]
