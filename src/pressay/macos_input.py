"""Fail-closed macOS focus guard and Unicode text delivery.

The adapter never uses the clipboard for automatic insertion. Accessibility is
used only to fingerprint the focused editable control; Quartz posts bounded
Unicode keyboard events after that fingerprint has been revalidated.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import sys
import time
from types import SimpleNamespace
from typing import Any, Callable, Protocol


class InputStatus(str, Enum):
    INSERTED_UNICODE = "inserted_unicode"
    COPIED = "copied"
    NO_TEXT = "no_text"
    TARGET_REQUIRED = "target_required"
    NO_FOREGROUND = "no_foreground"
    TARGET_MISMATCH = "target_mismatch"
    MODIFIERS_HELD = "modifiers_held"
    INPUT_FAILED = "input_failed"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ForegroundTarget:
    pid: int
    focused_control: tuple[object, ...] | None
    editable: bool
    trusted: bool = True
    captured_at: float = field(default_factory=time.monotonic, compare=False)

    @property
    def is_valid(self) -> bool:
        return self.pid > 0 and self.trusted and bool(self.focused_control)

    @property
    def hwnd(self) -> int:
        """Compatibility field used only by privacy-safe structured logging."""

        return 0


@dataclass(frozen=True)
class InputOutcome:
    status: InputStatus
    success: bool
    copied: bool = False
    reason: str | None = None
    detail: str | None = None
    method: str | None = None
    characters_sent: int = 0
    target: ForegroundTarget | None = None
    current_target: ForegroundTarget | None = None

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["status"] = self.status.value
        return result


class MacOSInputError(RuntimeError):
    pass


class MacOSInputUnavailable(MacOSInputError):
    pass


class InputBackend(Protocol):
    def snapshot_foreground_target(self) -> ForegroundTarget: ...

    def modifiers_released(self) -> bool: ...

    def send_unicode(self, text: str) -> bool: ...

    def send_enter(self) -> bool: ...

    def copy_text(self, text: str) -> None: ...


def macos_input_available() -> bool:
    return sys.platform == "darwin"


def targets_match(expected: ForegroundTarget, current: ForegroundTarget) -> bool:
    return (
        expected.is_valid
        and current.is_valid
        and expected.pid == current.pid
        and expected.focused_control == current.focused_control
    )


def target_looks_editable(target: ForegroundTarget, *, strict: bool = False) -> bool:
    del strict
    return target.is_valid and target.editable


def describe_focus(target: ForegroundTarget | None) -> dict[str, object]:
    """Return log-safe primitives; same shape as windows_input.describe_focus.

    The AX fingerprint has no numeric control type equivalent, so that field
    is always ``None`` here.
    """

    fingerprint = getattr(target, "focused_control", None) if target is not None else None
    focus_kind = fingerprint[0] if fingerprint else "none"
    return {
        "focus_kind": focus_kind,
        "control_type": None,
        "enabled": None,
        "keyboard_focusable": None,
        "value_writable": None,
        "text_editable": None,
        "caret_active": None,
        "win32_caret": None,
    }


def _load_frameworks() -> SimpleNamespace:
    if not macos_input_available():
        raise MacOSInputUnavailable("macOS input is only available on macOS")
    try:
        import AppKit
        import ApplicationServices
        import Quartz
    except Exception as exc:  # pragma: no cover - exercised by macOS CI import
        raise MacOSInputUnavailable(
            "Install the Pressay macOS dependencies and grant Accessibility access"
        ) from exc
    return SimpleNamespace(appkit=AppKit, ax=ApplicationServices, quartz=Quartz)


def _ax_value(ax: Any, element: Any, attribute: str) -> Any | None:
    try:
        error, value = ax.AXUIElementCopyAttributeValue(element, attribute, None)
    except Exception:
        return None
    return value if int(error) == int(ax.kAXErrorSuccess) else None


def _ax_settable(ax: Any, element: Any, attribute: str) -> bool:
    try:
        error, value = ax.AXUIElementIsAttributeSettable(element, attribute, None)
    except Exception:
        return False
    return int(error) == int(ax.kAXErrorSuccess) and bool(value)


class _MacOSBackend:
    _EDITABLE_ROLES = frozenset(
        {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
    )

    def __init__(self) -> None:
        self._frameworks = _load_frameworks()

    def snapshot_foreground_target(self) -> ForegroundTarget:
        appkit = self._frameworks.appkit
        ax = self._frameworks.ax
        try:
            trusted = bool(ax.AXIsProcessTrusted())
        except Exception:
            trusted = False
        if not trusted:
            return ForegroundTarget(0, None, False, trusted=False)

        application = appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if application is None:
            return ForegroundTarget(0, None, False)
        pid = int(application.processIdentifier())
        app_element = ax.AXUIElementCreateApplication(pid)
        focused = _ax_value(ax, app_element, ax.kAXFocusedUIElementAttribute)
        if focused is None:
            return ForegroundTarget(pid, None, False)

        role = str(_ax_value(ax, focused, ax.kAXRoleAttribute) or "")
        subrole = str(_ax_value(ax, focused, ax.kAXSubroleAttribute) or "")
        enabled_value = _ax_value(ax, focused, ax.kAXEnabledAttribute)
        enabled = True if enabled_value is None else bool(enabled_value)
        writable = _ax_settable(ax, focused, ax.kAXValueAttribute) or _ax_settable(
            ax, focused, ax.kAXSelectedTextAttribute
        )
        editable = (
            enabled
            and writable
            and role in self._EDITABLE_ROLES
            and "secure" not in subrole.casefold()
        )
        identifier = _ax_value(ax, focused, "AXIdentifier") or _ax_value(
            ax, focused, "AXDOMIdentifier"
        )
        try:
            element_hash = int(hash(focused))
        except Exception:
            element_hash = 0
        if element_hash == 0 and not identifier:
            return ForegroundTarget(pid, None, False)
        fingerprint = ("ax", pid, element_hash, role, subrole, str(identifier or ""))
        return ForegroundTarget(pid, fingerprint, editable)

    def modifiers_released(self) -> bool:
        quartz = self._frameworks.quartz
        flags = int(
            quartz.CGEventSourceFlagsState(
                quartz.kCGEventSourceStateCombinedSessionState
            )
        )
        modifier_mask = int(
            quartz.kCGEventFlagMaskShift
            | quartz.kCGEventFlagMaskControl
            | quartz.kCGEventFlagMaskAlternate
            | quartz.kCGEventFlagMaskCommand
        )
        return not bool(flags & modifier_mask)

    def send_unicode(self, text: str) -> bool:
        quartz = self._frameworks.quartz
        try:
            down = quartz.CGEventCreateKeyboardEvent(None, 0, True)
            up = quartz.CGEventCreateKeyboardEvent(None, 0, False)
            if down is None or up is None:
                return False
            utf16_length = len(text.encode("utf-16-le")) // 2
            quartz.CGEventKeyboardSetUnicodeString(down, utf16_length, text)
            quartz.CGEventKeyboardSetUnicodeString(up, utf16_length, text)
            quartz.CGEventPost(quartz.kCGHIDEventTap, down)
            quartz.CGEventPost(quartz.kCGHIDEventTap, up)
            return True
        except Exception:
            return False

    def send_enter(self) -> bool:
        quartz = self._frameworks.quartz
        try:
            down = quartz.CGEventCreateKeyboardEvent(None, 36, True)
            up = quartz.CGEventCreateKeyboardEvent(None, 36, False)
            if down is None or up is None:
                return False
            quartz.CGEventPost(quartz.kCGHIDEventTap, down)
            quartz.CGEventPost(quartz.kCGHIDEventTap, up)
            return True
        except Exception:
            return False

    def copy_text(self, text: str) -> None:
        pasteboard = self._frameworks.appkit.NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        if not pasteboard.setString_forType_(
            text, self._frameworks.appkit.NSPasteboardTypeString
        ):
            raise MacOSInputError("Could not write text to the macOS pasteboard")


def _backend(value: InputBackend | None) -> InputBackend:
    return value if value is not None else _MacOSBackend()


def snapshot_foreground_target(*, backend: InputBackend | None = None) -> ForegroundTarget:
    return _backend(backend).snapshot_foreground_target()


def _cancelled(callback: Callable[[], bool] | None) -> bool:
    return bool(callback and callback())


def _wait_for_modifiers(
    backend: InputBackend,
    cancelled: Callable[[], bool] | None,
    *,
    timeout: float = 0.8,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _cancelled(cancelled):
            return False
        if backend.modifiers_released():
            return True
        time.sleep(0.01)
    return backend.modifiers_released()


def send_text(
    text: str,
    *,
    expected_target: ForegroundTarget | None,
    press_enter: bool = False,
    cancelled: Callable[[], bool] | None = None,
    fallback_to_clipboard: bool = False,
    strict_editable_check: bool = False,
    backend: InputBackend | None = None,
) -> InputOutcome:
    """Insert Unicode only while the same writable AX element keeps focus."""

    del fallback_to_clipboard, strict_editable_check
    adapter = _backend(backend)
    if _cancelled(cancelled):
        return InputOutcome(InputStatus.CANCELLED, False, reason="cancelled")
    if not text and not press_enter:
        return InputOutcome(InputStatus.NO_TEXT, False, reason="no_text")
    if expected_target is None or not target_looks_editable(expected_target):
        return InputOutcome(
            InputStatus.TARGET_REQUIRED,
            False,
            reason="recording_target_required",
            target=expected_target,
        )
    current = adapter.snapshot_foreground_target()
    if not target_looks_editable(current):
        return InputOutcome(
            InputStatus.TARGET_MISMATCH,
            False,
            reason="focused_control_is_not_editable",
            target=expected_target,
            current_target=current,
        )
    if not targets_match(expected_target, current):
        return InputOutcome(
            InputStatus.TARGET_MISMATCH,
            False,
            reason="target_mismatch",
            target=expected_target,
            current_target=current,
        )
    if not _wait_for_modifiers(adapter, cancelled):
        status = InputStatus.CANCELLED if _cancelled(cancelled) else InputStatus.MODIFIERS_HELD
        reason = "cancelled" if status is InputStatus.CANCELLED else "physical_modifiers_not_released"
        return InputOutcome(status, False, reason=reason, target=expected_target)

    sent = 0
    for offset in range(0, len(text), 32):
        if _cancelled(cancelled):
            return InputOutcome(InputStatus.CANCELLED, False, reason="cancelled")
        current = adapter.snapshot_foreground_target()
        if not targets_match(expected_target, current) or not target_looks_editable(current):
            return InputOutcome(
                InputStatus.TARGET_MISMATCH,
                False,
                reason="target_mismatch",
                characters_sent=sent,
                target=expected_target,
                current_target=current,
            )
        chunk = text[offset : offset + 32]
        if not adapter.send_unicode(chunk):
            return InputOutcome(
                InputStatus.INPUT_FAILED,
                False,
                reason="unicode_input_failed",
                characters_sent=sent,
                target=expected_target,
            )
        sent += len(chunk)

    if press_enter:
        if _cancelled(cancelled):
            return InputOutcome(InputStatus.CANCELLED, False, reason="cancelled")
        current = adapter.snapshot_foreground_target()
        if not targets_match(expected_target, current) or not target_looks_editable(current):
            return InputOutcome(
                InputStatus.TARGET_MISMATCH,
                False,
                reason="foreground_target_changed_before_enter",
                characters_sent=sent,
                target=expected_target,
                current_target=current,
            )
        if not adapter.send_enter():
            return InputOutcome(
                InputStatus.INPUT_FAILED,
                False,
                reason="enter_input_failed",
                characters_sent=sent,
                target=expected_target,
            )

    return InputOutcome(
        InputStatus.INSERTED_UNICODE,
        True,
        method="quartz_unicode",
        characters_sent=sent,
        target=expected_target,
    )


def paste_last(
    text: str,
    *,
    cancelled: Callable[[], bool] | None = None,
    strict_editable_check: bool = False,
    backend: InputBackend | None = None,
) -> InputOutcome:
    adapter = _backend(backend)
    target = adapter.snapshot_foreground_target()
    return send_text(
        text,
        expected_target=target,
        cancelled=cancelled,
        strict_editable_check=strict_editable_check,
        backend=adapter,
    )


def copy_text(text: str, *, backend: InputBackend | None = None) -> InputOutcome:
    if not text:
        return InputOutcome(InputStatus.NO_TEXT, False, reason="no_text")
    adapter = _backend(backend)
    adapter.copy_text(text)
    return InputOutcome(InputStatus.COPIED, True, copied=True, method="pasteboard")


__all__ = [
    "ForegroundTarget",
    "InputOutcome",
    "InputStatus",
    "MacOSInputError",
    "MacOSInputUnavailable",
    "copy_text",
    "describe_focus",
    "macos_input_available",
    "paste_last",
    "send_text",
    "snapshot_foreground_target",
    "target_looks_editable",
    "targets_match",
]
