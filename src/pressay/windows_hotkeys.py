"""Windows global hotkeys for Pressay.

The state machine in this module is deliberately independent of Win32.  The
actual ``WH_KEYBOARD_LL`` callback only copies a small event into a queue; all
user callbacks run on a separate dispatcher thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
import os
import queue
import threading
import time
from types import SimpleNamespace
from typing import Callable, Mapping, Optional, Sequence

from .hotkey_bindings import (
    CTRL_KEYS as _CTRL_KEYS,
    Chord,
    HotkeyBindings,
    MODIFIER_KEYS,
    VK_CONTROL,
    VK_ESCAPE,
    VK_LCONTROL,
    VK_LMENU,
    VK_LSHIFT,
    VK_LWIN,
    VK_MENU,
    VK_RCONTROL,
    VK_RMENU,
    VK_RSHIFT,
    VK_RWIN,
    VK_SHIFT,
    VK_SPACE,
    WIN_KEYS as _WIN_KEYS,
)


LOG = logging.getLogger(__name__)

# Virtual-key values are constants in the Win32 ABI.  The shared ones live in
# hotkey_bindings, which owns the modifier families; these two are only needed
# for the default paste/copy chords and for tests.
VK_X = 0x58
VK_Z = 0x5A


class HotkeyAction(str, Enum):
    """Semantic actions emitted by :class:`HotkeyStateMachine`."""

    HOLD_START = "hold_start"
    HOLD_STOP = "hold_stop"
    # Compatibility names used by the application controller.  Enum aliases
    # keep one canonical wire value while making the intent concise there.
    START = "hold_start"
    STOP = "hold_stop"
    TOGGLE = "toggle"
    CANCEL = "cancel"
    PASTE_LAST = "paste_last"
    COPY_LAST = "copy_last"


@dataclass(frozen=True)
class KeyEvent:
    """A minimal keyboard event copied out of a low-level hook."""

    vk_code: int
    is_key_down: bool
    injected: bool = False
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class _HookDecision:
    """Tell the Win32 hook which physical event to swallow and what to replay."""

    suppress: bool = False
    synthetic_events: tuple[tuple[int, bool], ...] = ()


class _HookKeySuppressor:
    """Keep Pressay's modifier-only gesture away from foreground apps.

    The gesture's deferred modifier (Win, or Alt when Win is not in the pair)
    is held back briefly.  If the other modifier joins it, both physical
    modifiers belong to Pressay.  Otherwise the deferred key is replayed before
    the next key so ordinary Win shortcuts and a plain Win tap still work.

    A Ctrl+Shift pair has no deferred modifier: neither key means anything
    pressed alone, and both are constantly used with the mouse, whose events
    this hook never sees.  A withheld Ctrl could not be replayed in time for
    Ctrl+click, so such a pair is passed through untouched and only the toggle
    key and Esc are swallowed while the gesture is active.
    """

    def __init__(self, bindings: HotkeyBindings | None = None) -> None:
        self._bindings = bindings or HotkeyBindings()
        first, second = self._bindings.hold_key_sets
        self._hold_keys = first | second
        deferred = self._bindings.deferred_modifier
        if deferred is None:
            self._deferred_keys: frozenset[int] = frozenset()
            self._other_keys: frozenset[int] = frozenset()
        else:
            self._deferred_keys = MODIFIER_KEYS[deferred]
            self._other_keys = self._hold_keys - self._deferred_keys
        toggle_vk = self._bindings.toggle_vk
        self._gesture_swallowed = frozenset(
            code for code in (toggle_vk, VK_ESCAPE) if code is not None
        )
        # Without a deferred modifier the hold keys are never withheld, so they
        # must not be swallowed on auto-repeat either.
        self._active_suppression = (
            self._hold_keys | self._gesture_swallowed
            if self._deferred_keys
            else self._gesture_swallowed
        )
        self._pressed: set[int] = set()
        self._pending_deferred: set[int] = set()
        self._forwarded_deferred: set[int] = set()
        self._forwarded_other: set[int] = set()
        self._gesture_active = False
        self._swallowed_keys: set[int] = set()

    def _deferred_down(self) -> bool:
        return not self._pressed.isdisjoint(self._deferred_keys)

    def _other_down(self) -> bool:
        return not self._pressed.isdisjoint(self._other_keys)

    def _any_hold_down(self) -> bool:
        return not self._pressed.isdisjoint(self._hold_keys)

    def _hold_chord_down(self) -> bool:
        first, second = self._bindings.hold_key_sets
        return not self._pressed.isdisjoint(first) and not self._pressed.isdisjoint(
            second
        )

    def _activate_gesture(self) -> tuple[tuple[int, bool], ...]:
        self._gesture_active = True
        self._pending_deferred.clear()
        releases = tuple((vk_code, False) for vk_code in self._forwarded_other)
        self._swallowed_keys.update(self._forwarded_other)
        self._forwarded_other.clear()
        return releases

    def process(self, event: KeyEvent) -> _HookDecision:
        if event.injected:
            return _HookDecision()

        vk_code = event.vk_code
        if event.is_key_down:
            repeated = vk_code in self._pressed
            self._pressed.add(vk_code)
            if repeated:
                return _HookDecision(
                    suppress=(
                        vk_code in self._pending_deferred
                        or (
                            self._gesture_active
                            and vk_code in self._active_suppression
                        )
                    )
                )

            if self._deferred_keys:
                if vk_code in self._deferred_keys:
                    if self._gesture_active or self._other_down():
                        synthetic = self._activate_gesture()
                        self._swallowed_keys.add(vk_code)
                        return _HookDecision(True, synthetic)
                    self._pending_deferred.add(vk_code)
                    self._swallowed_keys.add(vk_code)
                    return _HookDecision(suppress=True)

                if vk_code in self._other_keys:
                    if self._gesture_active or self._deferred_down():
                        synthetic = self._activate_gesture()
                        self._swallowed_keys.add(vk_code)
                        return _HookDecision(True, synthetic)
                    self._forwarded_other.add(vk_code)
                    return _HookDecision()
            elif vk_code in self._hold_keys:
                if self._hold_chord_down():
                    self._gesture_active = True
                return _HookDecision()

            if self._gesture_active:
                if vk_code in self._gesture_swallowed:
                    self._swallowed_keys.add(vk_code)
                    return _HookDecision(suppress=True)
                return _HookDecision()

            if self._pending_deferred:
                synthetic = tuple((key, True) for key in self._pending_deferred)
                self._forwarded_deferred.update(self._pending_deferred)
                self._swallowed_keys.difference_update(self._pending_deferred)
                self._pending_deferred.clear()
                return _HookDecision(False, synthetic)
            return _HookDecision()

        self._pressed.discard(vk_code)

        if vk_code in self._swallowed_keys:
            self._swallowed_keys.discard(vk_code)
            if self._gesture_active and not self._any_hold_down():
                self._gesture_active = False
            if vk_code in self._deferred_keys and vk_code in self._pending_deferred:
                self._pending_deferred.discard(vk_code)
                return _HookDecision(
                    True,
                    ((vk_code, True), (vk_code, False)),
                )
            return _HookDecision(suppress=True)

        if self._gesture_active and not self._any_hold_down():
            self._gesture_active = False
        if vk_code in self._forwarded_deferred:
            self._forwarded_deferred.discard(vk_code)
        if vk_code in self._forwarded_other:
            self._forwarded_other.discard(vk_code)
        return _HookDecision()


class HotkeyStateMachine:
    """Translate raw key transitions into Pressay actions.

    ``hold_delay_s`` disambiguates Ctrl+Win+Space from Ctrl+Win push-to-talk.
    A value of zero starts push-to-talk as soon as the second modifier lands.
    With a positive value, callers should invoke :meth:`flush_due` regularly.
    """

    def __init__(
        self,
        hold_delay_s: float = 0.12,
        bindings: HotkeyBindings | None = None,
    ) -> None:
        if hold_delay_s < 0:
            raise ValueError("hold_delay_s must be non-negative")
        self.hold_delay_s = float(hold_delay_s)
        self.bindings = bindings or HotkeyBindings()
        first, second = self.bindings.hold_key_sets
        self._hold_key_sets = (first, second)
        self._hold_keys = first | second
        self._pressed: set[int] = set()
        self._hold_pending_since: Optional[float] = None
        self._hold_active = False
        self._suppress_hold_until_release = False

    @property
    def pressed_keys(self) -> frozenset[int]:
        return frozenset(self._pressed)

    @property
    def hold_active(self) -> bool:
        return self._hold_active

    @property
    def hold_pending(self) -> bool:
        return self._hold_pending_since is not None

    def _any_pressed(self, keys: Sequence[int] | frozenset[int]) -> bool:
        return not self._pressed.isdisjoint(keys)

    def _hold_chord_down(self) -> bool:
        first, second = self._hold_key_sets
        return self._any_pressed(first) and self._any_pressed(second)

    def _chord_fired(self, chord: Chord | None, vk_code: int) -> bool:
        """Whether *vk_code* completes *chord* with its modifiers already down."""

        if chord is None or vk_code != chord.vk_code:
            return False
        return all(self._any_pressed(keys) for keys in chord.modifier_key_sets)

    def process(self, event: KeyEvent) -> tuple[HotkeyAction, ...]:
        """Process one event, ignoring injected and auto-repeat events."""

        if event.injected:
            return ()

        actions: list[HotkeyAction] = []
        was_hold_chord = self._hold_chord_down()
        toggle_consumed = False
        toggle_vk = self.bindings.toggle_vk
        push_to_talk = self.bindings.push_to_talk

        if event.is_key_down:
            if event.vk_code in self._pressed:
                return self.flush_due(event.timestamp)
            self._pressed.add(event.vk_code)
        else:
            if event.vk_code not in self._pressed:
                return self.flush_due(event.timestamp)
            self._pressed.remove(event.vk_code)

        is_hold_chord = self._hold_chord_down()

        if event.is_key_down:
            if event.vk_code == VK_ESCAPE:
                # Cancel owns the current recording lifecycle.  Suppression
                # prevents a later modifier release from also issuing STOP.
                self._hold_pending_since = None
                self._hold_active = False
                self._suppress_hold_until_release = is_hold_chord
                actions.append(HotkeyAction.CANCEL)
            elif toggle_vk is not None and event.vk_code == toggle_vk and is_hold_chord:
                # The toggle key only wins while the hold chord is still in its
                # explicit disambiguation window.  Once push-to-talk has started
                # it owns the lifecycle until modifier release, so the toggle
                # key cannot silently replace the active hold.  With
                # push-to-talk switched off there is no window to wait for and
                # no hold to replace, so the toggle fires straight away.
                pending_since = self._hold_pending_since
                if not push_to_talk or (
                    pending_since is not None
                    and event.timestamp - pending_since < self.hold_delay_s
                ):
                    self._hold_pending_since = None
                    self._suppress_hold_until_release = True
                    toggle_consumed = True
                    actions.append(HotkeyAction.TOGGLE)
            elif self._chord_fired(self.bindings.paste_last, event.vk_code):
                actions.append(HotkeyAction.PASTE_LAST)
            elif self._chord_fired(self.bindings.copy_last, event.vk_code):
                actions.append(HotkeyAction.COPY_LAST)

        if (
            not was_hold_chord
            and is_hold_chord
            and not self._suppress_hold_until_release
        ):
            # Low-level keyboard events for a three-key chord are ordered, even
            # when the user presses the keys together.  The toggle key may
            # therefore already be down when the second hold modifier arrives.
            # That is the same toggle gesture and must not accidentally become
            # push-to-talk merely because of event order.
            if toggle_vk is not None and toggle_vk in self._pressed:
                self._hold_pending_since = None
                self._suppress_hold_until_release = True
                toggle_consumed = True
                actions.append(HotkeyAction.TOGGLE)
            elif push_to_talk:
                self._hold_pending_since = event.timestamp
                if self.hold_delay_s == 0:
                    self._hold_pending_since = None
                    self._hold_active = True
                    actions.append(HotkeyAction.HOLD_START)

        if was_hold_chord and not is_hold_chord:
            self._hold_pending_since = None
            if self._hold_active:
                self._hold_active = False
                actions.append(HotkeyAction.HOLD_STOP)
            self._suppress_hold_until_release = False

        # A non-modifier event may arrive after the deadline while the
        # dispatcher was busy. Resolve the pending hold without waiting for
        # another queue timeout. A toggle key consumed inside the
        # disambiguation window is excluded so the toggle chord wins; an
        # overdue one instead resolves the hold. Modifier key-up is excluded so
        # a quick tap of the hold chord stays a no-op.
        releasing_hold_modifier = (
            not event.is_key_down
            and event.vk_code in self._hold_keys
            and not is_hold_chord
        )
        if not (toggle_consumed or releasing_hold_modifier):
            actions.extend(self.flush_due(event.timestamp))
        return tuple(actions)

    def flush_due(self, now: Optional[float] = None) -> tuple[HotkeyAction, ...]:
        """Start a pending hold once its disambiguation delay passes."""

        if self._hold_pending_since is None:
            return ()
        now = time.monotonic() if now is None else now
        if now - self._hold_pending_since < self.hold_delay_s:
            return ()
        if not self._hold_chord_down() or self._suppress_hold_until_release:
            self._hold_pending_since = None
            return ()
        self._hold_pending_since = None
        self._hold_active = True
        return (HotkeyAction.HOLD_START,)

    def shutdown(self) -> tuple[HotkeyAction, ...]:
        """Reset all state, stopping an active push-to-talk operation."""

        actions = (HotkeyAction.HOLD_STOP,) if self._hold_active else ()
        self._pressed.clear()
        self._hold_pending_since = None
        self._hold_active = False
        self._suppress_hold_until_release = False
        return actions


_WATCHED_MODIFIER_KEYS = _CTRL_KEYS | _WIN_KEYS


class _HookWatchdog:
    """Recover a ``WH_KEYBOARD_LL`` hook silently dropped by Windows.

    Windows removes a low-level keyboard hook without notice once its
    procedure runs longer than ``LowLevelHooksTimeout`` (~300ms by default),
    which is plausible for a GIL-bound Python callback during model loading
    or transcription.  This class only *decides* when a reinstall is needed;
    it never touches Win32 itself, so it is fully testable with plain
    stand-ins on any platform.  All Win32 interaction is injected.

    Earlier designs tried to actively *detect* the drop: first by comparing
    physical key state to what the hook itself had seen (unreliable --
    the application's own synthetic replay desyncs the two during its own
    gesture, and the generic ``VK_CONTROL`` never matches the side-specific
    code the hook receives), then by injecting a synthetic probe event and
    watching for it to arrive (worse -- any periodic synthetic keyboard
    input resets Windows' idle timer via ``GetLastInputInfo``, which
    permanently defeats screen blanking, the screensaver, auto-lock and
    sleep for as long as Pressay runs in the background).

    This design detects nothing.  It just reinstalls the hook on a fixed
    interval, whenever that is provably safe: if no watched modifier is
    pressed there is nothing to lose, and if one has been pressed for far
    longer than any legitimate recording can last, the state is almost
    certainly stuck on a dead hook and worth resetting.  A live gesture in
    progress is left completely alone.
    """

    def __init__(
        self,
        *,
        pressed_snapshot: Callable[[], tuple[frozenset[int], float]],
        now: Callable[[], float],
        hook_ready: Callable[[], bool],
        request_reinstall: Callable[[bool], None],
        wait: Callable[[float], bool],
        reinstall_interval_s: float = 30.0,
        stale_after_s: float = 360.0,
    ) -> None:
        self._pressed_snapshot = pressed_snapshot
        self._now = now
        self._hook_ready = hook_ready
        self._request_reinstall = request_reinstall
        self._wait = wait
        self.reinstall_interval_s = reinstall_interval_s
        self.stale_after_s = stale_after_s

    def poll_once(self) -> None:
        """Run a single decision cycle; called every ``reinstall_interval_s``."""

        if not self._hook_ready():
            # Startup or shutdown: nothing to do yet.
            return
        pressed, changed_at = self._pressed_snapshot()
        if not pressed:
            # The common case, and the one that matters: a hook lost to
            # LowLevelHooksTimeout during model loading or transcription
            # drops because the hook procedure was slow, not because of
            # what the user's fingers were doing -- so the modifiers are
            # essentially always already released by the time we notice.
            # Nothing is at risk; just reinstall.
            self._request_reinstall(False)
            return
        if self._now() - changed_at < self.stale_after_s:
            # A live gesture, or one that only just ended; leave it alone.
            return
        # The watched modifiers have looked pressed for far longer than any
        # legitimate recording can last: the hook almost certainly died
        # mid-hold and the release never arrived.  Recover and unstick it.
        self._request_reinstall(True)

    def run(self) -> None:
        """Run on an interval until ``wait`` reports a stop request."""

        while True:
            self.poll_once()
            if self._wait(self.reinstall_interval_s):
                return


class WindowsHotkeyError(RuntimeError):
    """Base error raised by the Windows hotkey service."""


class WindowsHotkeyUnavailable(WindowsHotkeyError):
    """Raised when the Windows low-level keyboard API is unavailable."""


def windows_hotkeys_available() -> bool:
    return os.name == "nt"


def _load_win32_api() -> SimpleNamespace:
    """Load and type Win32 functions only when the service is started."""

    if not windows_hotkeys_available():
        raise WindowsHotkeyUnavailable(
            "Global keyboard hooks are only available on Windows"
        )

    import ctypes
    from ctypes import wintypes

    lresult_t = ctypes.c_ssize_t
    wparam_t = ctypes.c_size_t
    lparam_t = ctypes.c_ssize_t
    hook_proc_t = ctypes.WINFUNCTYPE(lresult_t, ctypes.c_int, wparam_t, lparam_t)

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = (
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        )

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.SetWindowsHookExW.argtypes = (
        ctypes.c_int,
        hook_proc_t,
        wintypes.HINSTANCE,
        wintypes.DWORD,
    )
    user32.SetWindowsHookExW.restype = wintypes.HANDLE
    user32.CallNextHookEx.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wparam_t,
        lparam_t,
    )
    user32.CallNextHookEx.restype = lresult_t
    user32.UnhookWindowsHookEx.argtypes = (wintypes.HANDLE,)
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = (
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    )
    user32.GetMessageW.restype = ctypes.c_int
    user32.PeekMessageW.argtypes = (
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    )
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
    user32.DispatchMessageW.restype = lresult_t
    user32.PostThreadMessageW.argtypes = (
        wintypes.DWORD,
        wintypes.UINT,
        wparam_t,
        lparam_t,
    )
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.keybd_event.argtypes = (
        ctypes.c_ubyte,
        ctypes.c_ubyte,
        wintypes.DWORD,
        ctypes.c_size_t,
    )
    user32.keybd_event.restype = None
    kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetCurrentThreadId.argtypes = ()
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    return SimpleNamespace(
        ctypes=ctypes,
        wintypes=wintypes,
        user32=user32,
        kernel32=kernel32,
        HookProc=hook_proc_t,
        KBDLLHOOKSTRUCT=KBDLLHOOKSTRUCT,
    )


_STOP = object()
_RESET_MACHINE = object()

# Custom thread message the watchdog uses to ask the hook thread to reinstall
# a dropped hook.  Must live in the WM_APP+ range so it cannot collide with a
# system message delivered to the same thread queue.
_WM_REINSTALL_HOOK = 0x8001


class WindowsHotkeyService:
    """Own a ``WH_KEYBOARD_LL`` hook and dispatch semantic hotkey actions.

    ``callback`` is called as ``callback(action)`` on the dispatcher thread.
    Action-specific callbacks supplied in ``callbacks`` take no arguments.
    """

    def __init__(
        self,
        callback: Optional[Callable[[HotkeyAction], None]] = None,
        *,
        callbacks: Optional[Mapping[HotkeyAction | str, Callable[[], None]]] = None,
        hold_delay_s: float = 0.12,
        bindings: HotkeyBindings | None = None,
    ) -> None:
        self.bindings = bindings or HotkeyBindings()
        self._callback = callback
        self._callbacks: dict[HotkeyAction, list[Callable[[], None]]] = {
            action: [] for action in HotkeyAction
        }
        if callbacks:
            for action, action_callback in callbacks.items():
                self._callbacks[HotkeyAction(action)].append(action_callback)
        self._callback_lock = threading.RLock()
        self._machine = HotkeyStateMachine(
            hold_delay_s=hold_delay_s, bindings=self.bindings
        )
        self._suppressor = _HookKeySuppressor(self.bindings)
        self._events: queue.Queue[KeyEvent | object] = queue.Queue()
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._hook_thread: Optional[threading.Thread] = None
        self._dispatch_thread: Optional[threading.Thread] = None
        self._hook_thread_id: Optional[int] = None
        self._api: Optional[SimpleNamespace] = None
        self._hook_handle: object | None = None
        self._hook_proc: object | None = None
        self._startup_error: Optional[BaseException] = None
        self._lifecycle_lock = threading.RLock()
        # Snapshot of watched modifiers the state machine currently believes
        # are pressed, paired with when that set last changed.  Only the
        # dispatcher thread writes it (by reassigning the tuple, which is
        # atomic under the GIL); the watchdog thread only reads it, so no
        # lock is needed.
        self._hook_known_pressed: tuple[frozenset[int], float] = (
            frozenset(),
            time.monotonic(),
        )
        self._watchdog: Optional[_HookWatchdog] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()

    @property
    def is_running(self) -> bool:
        return bool(self._hook_thread and self._hook_thread.is_alive())

    @property
    def startup_error(self) -> Optional[BaseException]:
        return self._startup_error

    def subscribe(self, action: HotkeyAction | str, callback: Callable[[], None]) -> None:
        with self._callback_lock:
            self._callbacks[HotkeyAction(action)].append(callback)

    def unsubscribe(self, action: HotkeyAction | str, callback: Callable[[], None]) -> None:
        with self._callback_lock:
            callbacks = self._callbacks[HotkeyAction(action)]
            if callback in callbacks:
                callbacks.remove(callback)

    def start(self, timeout_s: float = 3.0) -> None:
        """Start dispatcher and hook threads, or raise a descriptive error."""

        with self._lifecycle_lock:
            if self.is_running:
                return
            if not windows_hotkeys_available():
                raise WindowsHotkeyUnavailable(
                    "Pressay global hotkeys require Windows"
                )
            self._ready.clear()
            self._stop_requested.clear()
            self._startup_error = None
            self._hook_thread_id = None
            self._events = queue.Queue()
            self._suppressor = _HookKeySuppressor(self.bindings)
            self._hook_known_pressed = (frozenset(), time.monotonic())
            self._watchdog_stop.clear()
            self._watchdog = _HookWatchdog(
                pressed_snapshot=lambda: self._hook_known_pressed,
                now=time.monotonic,
                hook_ready=self._hook_thread_ready,
                request_reinstall=self._request_hook_reinstall,
                wait=self._watchdog_stop.wait,
            )
            self._dispatch_thread = threading.Thread(
                target=self._dispatch_loop,
                name="PressayHotkeyDispatch",
                daemon=True,
            )
            self._hook_thread = threading.Thread(
                target=self._hook_loop,
                name="PressayKeyboardHook",
                daemon=True,
            )
            self._watchdog_thread = threading.Thread(
                target=self._watchdog.run,
                name="PressayHotkeyWatchdog",
                daemon=True,
            )
            self._dispatch_thread.start()
            self._hook_thread.start()
            self._watchdog_thread.start()

        if not self._ready.wait(timeout_s):
            self.stop()
            raise WindowsHotkeyError("Timed out while installing the keyboard hook")
        if self._startup_error is not None:
            error = self._startup_error
            self.stop()
            if isinstance(error, WindowsHotkeyError):
                raise error
            raise WindowsHotkeyError("Could not install the keyboard hook") from error

    def stop(self, timeout_s: float = 3.0) -> None:
        """Stop the Win32 message loop and drain the dispatcher safely."""

        with self._lifecycle_lock:
            self._stop_requested.set()
            self._watchdog_stop.set()
            api = self._api
            thread_id = self._hook_thread_id
            if api is not None and thread_id is not None:
                # The hook thread creates its queue with PeekMessage before it
                # announces readiness, so PostThreadMessage is reliable here.
                api.user32.PostThreadMessageW(thread_id, 0x0012, 0, 0)  # WM_QUIT
            hook_thread = self._hook_thread
            dispatch_thread = self._dispatch_thread
            watchdog_thread = self._watchdog_thread

        current = threading.current_thread()
        if watchdog_thread is not None and watchdog_thread is not current:
            watchdog_thread.join(timeout_s)
        if hook_thread is not None and hook_thread is not current:
            hook_thread.join(timeout_s)
        self._events.put(_STOP)
        if dispatch_thread is not None and dispatch_thread is not current:
            dispatch_thread.join(timeout_s)

        with self._lifecycle_lock:
            self._hook_thread = None
            self._dispatch_thread = None
            self._watchdog_thread = None
            self._watchdog = None
            self._hook_thread_id = None
            self._hook_handle = None
            self._hook_proc = None
            self._api = None

    def __enter__(self) -> "WindowsHotkeyService":
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()

    def _hook_thread_ready(self) -> bool:
        """Whether the hook thread has finished loading the Win32 API.

        Guards the watchdog from probing -- and therefore from counting a
        failure -- before ``start()`` has finished installing the hook.
        """

        return self._api is not None and self._hook_thread_id is not None

    def _publish_pressed_snapshot(self) -> None:
        """Refresh the watched-modifier snapshot the watchdog reads.

        Only bumps the "last changed" timestamp when the watched set
        actually differs from the published one, since the watchdog needs
        to know how long it has been unchanged, not merely how recently
        this was called.
        """

        watched_pressed = self._machine.pressed_keys & _WATCHED_MODIFIER_KEYS
        if watched_pressed != self._hook_known_pressed[0]:
            self._hook_known_pressed = (watched_pressed, time.monotonic())

    def _request_hook_reinstall(self, reset: bool) -> None:
        """Ask the hook thread to reinstall its hook; runs on the watchdog thread.

        ``SetWindowsHookExW`` must be called from the thread pumping the
        message loop the hook procedure runs on, so this only posts a
        request rather than reinstalling anything itself.  ``reset`` rides
        along as the message's wParam (0/1) instead of a second message,
        since ``PostThreadMessageW`` already carries one.
        """

        api = self._api
        thread_id = self._hook_thread_id
        if api is None or thread_id is None:
            return
        api.user32.PostThreadMessageW(thread_id, _WM_REINSTALL_HOOK, int(reset), 0)

    def _reinstall_hook(
        self, api: SimpleNamespace, old_handle: object | None, *, reset: bool
    ) -> object | None:
        """Swap the hook for a fresh one; runs on the hook thread only.

        The new hook is installed *before* the old one is unhooked: if
        ``SetWindowsHookExW`` fails, the previous (possibly still-live) hook
        is left in place instead of leaving the service with no hook at all
        until the next attempt.  A brief window with both installed is
        harmless -- the duplicate event it would produce is just an
        auto-repeat as far as both the state machine (``vk_code in
        self._pressed``) and the suppressor (``repeated``) are concerned,
        and both already handle that safely.

        ``reset`` only applies when the watchdog decided the state machine
        looks stuck (a watched modifier pressed far longer than any
        legitimate recording): the suppressor is recreated directly (only
        the hook thread ever touches it) and the dispatcher ends an active
        hold via the normal callback path, since user callbacks always run
        on the dispatcher thread, not here.  A routine reinstall (nothing
        pressed) resets nothing -- there is nothing to reset.
        """

        module = api.kernel32.GetModuleHandleW(None)
        new_handle = api.user32.SetWindowsHookExW(13, self._hook_proc, module, 0)
        if not new_handle:
            error_code = api.ctypes.get_last_error()
            LOG.warning("Pressay keyboard hook reinstall failed (%s)", error_code)
            return old_handle
        if old_handle:
            api.user32.UnhookWindowsHookEx(old_handle)
        self._hook_handle = new_handle
        if reset:
            LOG.info("Pressay keyboard hook state looked stuck; reinstalled and reset")
            self._suppressor = _HookKeySuppressor(self.bindings)
            self._events.put(_RESET_MACHINE)
        else:
            LOG.debug("Pressay keyboard hook routine reinstall")
        return new_handle

    def _emit(self, action: HotkeyAction) -> None:
        with self._callback_lock:
            general = self._callback
            specific = tuple(self._callbacks[action])
        try:
            if general is not None:
                general(action)
        except Exception:  # callbacks must never kill the dispatcher
            LOG.exception("Pressay hotkey callback failed for %s", action.value)
        for callback in specific:
            try:
                callback()
            except Exception:
                LOG.exception("Pressay hotkey callback failed for %s", action.value)

    def _dispatch_loop(self) -> None:
        try:
            while True:
                # A short timeout is only needed to poll flush_due() for a
                # pending Ctrl+Win hold; with nothing pending there is no
                # reason to wake up 40 times a second, so block indefinitely.
                timeout = 0.025 if self._machine.hold_pending else None
                try:
                    item = self._events.get(timeout=timeout)
                except queue.Empty:
                    item = None
                if item is _STOP:
                    break
                if item is _RESET_MACHINE:
                    for action in self._machine.shutdown():
                        self._emit(action)
                    self._publish_pressed_snapshot()
                    continue
                actions = (
                    self._machine.process(item)
                    if isinstance(item, KeyEvent)
                    else self._machine.flush_due()
                )
                self._publish_pressed_snapshot()
                for action in actions:
                    self._emit(action)
        finally:
            for action in self._machine.shutdown():
                self._emit(action)
            self._publish_pressed_snapshot()

    def _hook_loop(self) -> None:
        hook_handle = None
        try:
            api = _load_win32_api()
            self._api = api
            self._hook_thread_id = int(api.kernel32.GetCurrentThreadId())
            msg = api.wintypes.MSG()
            # Force creation of this thread's message queue before start()
            # returns; otherwise an early PostThreadMessage(WM_QUIT) can fail.
            api.user32.PeekMessageW(api.ctypes.byref(msg), None, 0, 0, 0)

            @api.HookProc
            def hook_proc(n_code: int, w_param: int, l_param: int) -> int:
                if n_code >= 0 and w_param in (0x0100, 0x0101, 0x0104, 0x0105):
                    data = api.ctypes.cast(
                        l_param, api.ctypes.POINTER(api.KBDLLHOOKSTRUCT)
                    ).contents
                    injected = bool(data.flags & (0x10 | 0x02))
                    if not injected:
                        event = KeyEvent(
                            vk_code=int(data.vkCode),
                            is_key_down=w_param in (0x0100, 0x0104),
                            injected=False,
                        )
                        self._events.put_nowait(
                            event
                        )
                        decision = self._suppressor.process(event)
                        for vk_code, is_key_down in decision.synthetic_events:
                            flags = 0 if is_key_down else 0x0002  # KEYEVENTF_KEYUP
                            api.user32.keybd_event(vk_code, 0, flags, 0)
                        if decision.suppress:
                            return 1
                return int(api.user32.CallNextHookEx(hook_handle, n_code, w_param, l_param))

            self._hook_proc = hook_proc  # keep the ctypes callback alive
            module = api.kernel32.GetModuleHandleW(None)
            hook_handle = api.user32.SetWindowsHookExW(13, hook_proc, module, 0)
            if not hook_handle:
                error_code = api.ctypes.get_last_error()
                raise WindowsHotkeyError(
                    f"SetWindowsHookExW(WH_KEYBOARD_LL) failed ({error_code})"
                )
            self._hook_handle = hook_handle
            self._ready.set()

            while not self._stop_requested.is_set():
                result = api.user32.GetMessageW(api.ctypes.byref(msg), None, 0, 0)
                if result == 0:  # WM_QUIT
                    break
                if result == -1:
                    error_code = api.ctypes.get_last_error()
                    raise WindowsHotkeyError(f"GetMessageW failed ({error_code})")
                if msg.message == _WM_REINSTALL_HOOK:
                    hook_handle = self._reinstall_hook(
                        api, hook_handle, reset=bool(msg.wParam)
                    )
                    continue
                api.user32.TranslateMessage(api.ctypes.byref(msg))
                api.user32.DispatchMessageW(api.ctypes.byref(msg))
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            if not self._stop_requested.is_set():
                LOG.exception("Pressay keyboard hook stopped unexpectedly")
        finally:
            if hook_handle and self._api is not None:
                self._api.user32.UnhookWindowsHookEx(hook_handle)
            self._hook_handle = None
            self._events.put(_STOP)
            self._ready.set()


__all__ = [
    "HotkeyAction",
    "HotkeyStateMachine",
    "KeyEvent",
    "WindowsHotkeyError",
    "WindowsHotkeyService",
    "WindowsHotkeyUnavailable",
    "windows_hotkeys_available",
]
