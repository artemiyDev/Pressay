"""Quartz global hotkeys for the Pressay macOS preview."""

from __future__ import annotations

from enum import Enum
import sys
import threading
import time
from typing import Any, Callable


class HotkeyAction(str, Enum):
    HOLD_START = "hold_start"
    HOLD_STOP = "hold_stop"
    TOGGLE = "toggle"
    CANCEL = "cancel"
    PASTE_LAST = "paste_last"
    COPY_LAST = "copy_last"
    START = "hold_start"
    STOP = "hold_stop"


class MacOSHotkeyError(RuntimeError):
    pass


class MacOSHotkeyUnavailable(MacOSHotkeyError):
    pass


class MacHotkeyStateMachine:
    """Disambiguate Control+Option hold from chord shortcuts."""

    def __init__(self, *, hold_delay_s: float = 0.12) -> None:
        if hold_delay_s < 0:
            raise ValueError("hold_delay_s must be non-negative")
        self.hold_delay_s = float(hold_delay_s)
        self._chord_active = False
        self._pending_since: float | None = None
        self._hold_active = False
        self._shortcut_consumed = False

    @property
    def chord_active(self) -> bool:
        return self._chord_active

    def set_chord(self, active: bool, *, now: float | None = None) -> tuple[HotkeyAction, ...]:
        moment = time.monotonic() if now is None else float(now)
        if active == self._chord_active:
            return ()
        self._chord_active = active
        if active:
            self._pending_since = moment
            self._hold_active = False
            self._shortcut_consumed = False
            return ()

        actions = (HotkeyAction.HOLD_STOP,) if self._hold_active else ()
        self._pending_since = None
        self._hold_active = False
        self._shortcut_consumed = False
        return actions

    def shortcut(self, action: HotkeyAction) -> tuple[HotkeyAction, ...]:
        if not self._chord_active or action not in {
            HotkeyAction.TOGGLE,
            HotkeyAction.PASTE_LAST,
            HotkeyAction.COPY_LAST,
        }:
            return ()
        # Once PTT has actually started, a late shortcut is ignored so the
        # active recording cannot be converted into a different mode.
        if self._hold_active:
            return ()
        self._pending_since = None
        self._shortcut_consumed = True
        return (action,)

    def cancel(self) -> tuple[HotkeyAction, ...]:
        return (HotkeyAction.CANCEL,)

    def flush_due(self, *, now: float | None = None) -> tuple[HotkeyAction, ...]:
        moment = time.monotonic() if now is None else float(now)
        if (
            not self._chord_active
            or self._hold_active
            or self._shortcut_consumed
            or self._pending_since is None
            or moment - self._pending_since < self.hold_delay_s
        ):
            return ()
        self._pending_since = None
        self._hold_active = True
        return (HotkeyAction.HOLD_START,)

    def shutdown(self) -> tuple[HotkeyAction, ...]:
        actions = (HotkeyAction.HOLD_STOP,) if self._hold_active else ()
        self._chord_active = False
        self._pending_since = None
        self._hold_active = False
        self._shortcut_consumed = False
        return actions


class MacOSHotkeyService:
    """Long-lived Quartz event tap with bounded startup and clean shutdown.

    A callback must return literal ``True`` for ``CANCEL`` to suppress the
    matching Escape down/up pair. Other callback results are ignored.
    """

    _SPACE = 49
    _V = 9
    _C = 8
    _ESCAPE = 53

    def __init__(
        self,
        callback: Callable[[HotkeyAction], object] | None = None,
        *,
        hold_delay_s: float = 0.12,
    ) -> None:
        self._callback = callback or (lambda action: None)
        self._machine = MacHotkeyStateMachine(hold_delay_s=hold_delay_s)
        self._lock = threading.RLock()
        self._started = threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._timer: threading.Timer | None = None
        self._run_loop: Any | None = None
        self._tap: Any | None = None
        self._quartz: Any | None = None
        self._startup_error: BaseException | None = None
        self._suppressed: set[int] = set()
        self._escape_pressed = False
        self._escape_generation = 0

    def start(self) -> None:
        if sys.platform != "darwin":
            raise MacOSHotkeyUnavailable("Pressay macOS hotkeys require macOS")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._started.clear()
            self._stopped.clear()
            self._startup_error = None
            self._reset_escape_state()
            self._thread = threading.Thread(
                target=self._run,
                name="PressayMacHotkeys",
                daemon=True,
            )
            self._thread.start()
        if not self._started.wait(3.0):
            raise MacOSHotkeyError("Timed out while installing the macOS event tap")
        if self._startup_error is not None:
            error = self._startup_error
            if isinstance(error, MacOSHotkeyError):
                raise error
            raise MacOSHotkeyError("Could not install the macOS event tap") from error

    def stop(self) -> None:
        with self._lock:
            timer = self._timer
            self._timer = None
            quartz = self._quartz
            run_loop = self._run_loop
        if timer is not None:
            timer.cancel()
        if quartz is not None and run_loop is not None:
            try:
                quartz.CFRunLoopStop(run_loop)
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._reset_escape_state()
        for action in self._machine.shutdown():
            self._emit(action)

    def __enter__(self) -> "MacOSHotkeyService":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def _emit(self, action: HotkeyAction) -> object:
        try:
            return self._callback(action)
        except Exception:
            # A user callback must not tear down the global event tap.
            return None

    def _emit_many(self, actions: tuple[HotkeyAction, ...]) -> None:
        for action in actions:
            self._emit(action)

    def _schedule_hold(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(
                self._machine.hold_delay_s,
                self._flush_pending,
            )
            self._timer.daemon = True
            self._timer.start()

    def _cancel_timer(self) -> None:
        with self._lock:
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()

    def _flush_pending(self) -> None:
        with self._lock:
            self._timer = None
            actions = self._machine.flush_due()
        self._emit_many(actions)

    def _update_chord(self, active: bool) -> tuple[HotkeyAction, ...]:
        with self._lock:
            changed = active != self._machine.chord_active
            actions = self._machine.set_chord(active)
        if changed and active:
            self._schedule_hold()
        elif changed:
            self._cancel_timer()
        return actions

    def _shortcut(self, action: HotkeyAction) -> tuple[HotkeyAction, ...]:
        with self._lock:
            actions = self._machine.shortcut(action)
        if actions:
            self._cancel_timer()
        return actions

    def _reset_escape_state(self) -> None:
        """Forget an incomplete Escape pair after lifecycle/tap recovery."""

        with self._lock:
            self._escape_generation += 1
            self._escape_pressed = False
            self._suppressed.discard(self._ESCAPE)

    def _handle_escape(self, *, key_down: bool, is_repeat: bool = False) -> bool:
        """Return whether this phase belongs to an accepted cancel gesture."""

        with self._lock:
            if not key_down:
                suppressed = self._ESCAPE in self._suppressed
                self._reset_escape_state()
                return suppressed
            if is_repeat:
                if not self._escape_pressed:
                    return False
                return self._ESCAPE in self._suppressed

            # A non-repeat down is a new physical press. Recover even if the
            # preceding key-up was lost while the event tap was unavailable.
            self._escape_generation += 1
            generation = self._escape_generation
            self._escape_pressed = True
            self._suppressed.discard(self._ESCAPE)

        accepted = self._emit(HotkeyAction.CANCEL) is True
        with self._lock:
            # stop(), start(), tap recovery or another non-repeat down may have
            # invalidated this callback while it ran outside the service lock.
            if generation != self._escape_generation or not self._escape_pressed:
                return False
            if accepted:
                self._suppressed.add(self._ESCAPE)
        return accepted

    def _recover_disabled_tap(self, quartz: Any, event: Any) -> Any:
        """Reset incomplete pairing before Quartz resumes event delivery."""

        self._reset_escape_state()
        quartz.CGEventTapEnable(self._tap, True)
        return event

    def _run(self) -> None:  # pragma: no cover - exercised on a real Mac
        try:
            import ApplicationServices as ax
            import Quartz

            trusted = bool(
                ax.AXIsProcessTrustedWithOptions(
                    {ax.kAXTrustedCheckOptionPrompt: True}
                )
            )
            if not trusted:
                raise MacOSHotkeyUnavailable(
                    "Grant Pressay Accessibility permission in System Settings, then restart it"
                )
            self._quartz = Quartz
            modifier_mask = int(
                Quartz.kCGEventFlagMaskControl | Quartz.kCGEventFlagMaskAlternate
            )

            def callback(proxy: Any, event_type: int, event: Any, refcon: Any) -> Any:
                del proxy, refcon
                if event_type in {
                    Quartz.kCGEventTapDisabledByTimeout,
                    Quartz.kCGEventTapDisabledByUserInput,
                }:
                    return self._recover_disabled_tap(Quartz, event)
                flags = int(Quartz.CGEventGetFlags(event))
                chord = (flags & modifier_mask) == modifier_mask
                self._emit_many(self._update_chord(chord))
                if event_type not in {Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp}:
                    return event
                keycode = int(
                    Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGKeyboardEventKeycode
                    )
                )
                if keycode == self._ESCAPE:
                    suppress = self._handle_escape(
                        key_down=event_type == Quartz.kCGEventKeyDown,
                        is_repeat=(
                            event_type == Quartz.kCGEventKeyDown
                            and bool(
                                Quartz.CGEventGetIntegerValueField(
                                    event, Quartz.kCGKeyboardEventAutorepeat
                                )
                            )
                        ),
                    )
                    return None if suppress else event
                if event_type == Quartz.kCGEventKeyUp and keycode in self._suppressed:
                    self._suppressed.discard(keycode)
                    return None
                if event_type != Quartz.kCGEventKeyDown:
                    return event
                shortcuts = {
                    self._SPACE: HotkeyAction.TOGGLE,
                    self._V: HotkeyAction.PASTE_LAST,
                    self._C: HotkeyAction.COPY_LAST,
                }
                action = shortcuts.get(keycode)
                if chord and action is not None:
                    emitted = self._shortcut(action)
                    if emitted:
                        self._emit_many(emitted)
                        self._suppressed.add(keycode)
                        return None
                return event

            mask = (
                Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
                | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
                | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
            )
            self._tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionDefault,
                mask,
                callback,
                None,
            )
            if self._tap is None:
                raise MacOSHotkeyUnavailable(
                    "macOS refused the event tap; enable Accessibility and Input Monitoring"
                )
            source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            self._run_loop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(
                self._run_loop, source, Quartz.kCFRunLoopCommonModes
            )
            Quartz.CGEventTapEnable(self._tap, True)
            self._started.set()
            Quartz.CFRunLoopRun()
        except BaseException as exc:
            self._startup_error = exc
            self._started.set()
        finally:
            self._stopped.set()


__all__ = [
    "HotkeyAction",
    "MacHotkeyStateMachine",
    "MacOSHotkeyError",
    "MacOSHotkeyService",
    "MacOSHotkeyUnavailable",
]
