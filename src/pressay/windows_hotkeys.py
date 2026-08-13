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


LOG = logging.getLogger(__name__)

# Virtual-key values are constants in the Win32 ABI and are safe to define on
# every platform.  Keeping them here also makes the state machine easy to test.
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_X = 0x58
VK_Z = 0x5A
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5

_CTRL_KEYS = frozenset((VK_CONTROL, VK_LCONTROL, VK_RCONTROL))
_WIN_KEYS = frozenset((VK_LWIN, VK_RWIN))
_SHIFT_KEYS = frozenset((VK_SHIFT, VK_LSHIFT, VK_RSHIFT))
_ALT_KEYS = frozenset((VK_MENU, VK_LMENU, VK_RMENU))


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

    A Windows-key press is held back briefly.  If Ctrl joins it, both physical
    modifiers belong to Pressay.  Otherwise the Win key is replayed before
    the next key so ordinary Win shortcuts and a plain Win tap still work.
    """

    def __init__(self) -> None:
        self._pressed: set[int] = set()
        self._pending_win: set[int] = set()
        self._forwarded_win: set[int] = set()
        self._forwarded_ctrl: set[int] = set()
        self._gesture_active = False
        self._swallowed_keys: set[int] = set()

    def _ctrl_down(self) -> bool:
        return not self._pressed.isdisjoint(_CTRL_KEYS)

    def _win_down(self) -> bool:
        return not self._pressed.isdisjoint(_WIN_KEYS)

    def _activate_gesture(self) -> tuple[tuple[int, bool], ...]:
        self._gesture_active = True
        self._pending_win.clear()
        releases = tuple((vk_code, False) for vk_code in self._forwarded_ctrl)
        self._swallowed_keys.update(self._forwarded_ctrl)
        self._forwarded_ctrl.clear()
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
                        vk_code in self._pending_win
                        or (
                            self._gesture_active
                            and vk_code in (_CTRL_KEYS | _WIN_KEYS | {VK_SPACE, VK_ESCAPE})
                        )
                    )
                )

            if vk_code in _WIN_KEYS:
                if self._gesture_active or self._ctrl_down():
                    synthetic = self._activate_gesture()
                    self._swallowed_keys.add(vk_code)
                    return _HookDecision(True, synthetic)
                self._pending_win.add(vk_code)
                self._swallowed_keys.add(vk_code)
                return _HookDecision(suppress=True)

            if vk_code in _CTRL_KEYS:
                if self._gesture_active or self._win_down():
                    synthetic = self._activate_gesture()
                    self._swallowed_keys.add(vk_code)
                    return _HookDecision(True, synthetic)
                self._forwarded_ctrl.add(vk_code)
                return _HookDecision()

            if self._gesture_active:
                if vk_code in (VK_SPACE, VK_ESCAPE):
                    self._swallowed_keys.add(vk_code)
                    return _HookDecision(suppress=True)
                return _HookDecision()

            if self._pending_win:
                synthetic = tuple((win_key, True) for win_key in self._pending_win)
                self._forwarded_win.update(self._pending_win)
                self._swallowed_keys.difference_update(self._pending_win)
                self._pending_win.clear()
                return _HookDecision(False, synthetic)
            return _HookDecision()

        self._pressed.discard(vk_code)

        if vk_code in self._swallowed_keys:
            self._swallowed_keys.discard(vk_code)
            if self._gesture_active and not self._ctrl_down() and not self._win_down():
                self._gesture_active = False
            if vk_code in _WIN_KEYS and vk_code in self._pending_win:
                self._pending_win.discard(vk_code)
                return _HookDecision(
                    True,
                    ((vk_code, True), (vk_code, False)),
                )
            return _HookDecision(suppress=True)

        if vk_code in self._forwarded_win:
            self._forwarded_win.discard(vk_code)
        if vk_code in self._forwarded_ctrl:
            self._forwarded_ctrl.discard(vk_code)
        return _HookDecision()


class HotkeyStateMachine:
    """Translate raw key transitions into Pressay actions.

    ``hold_delay_s`` disambiguates Ctrl+Win+Space from Ctrl+Win push-to-talk.
    A value of zero starts push-to-talk as soon as the second modifier lands.
    With a positive value, callers should invoke :meth:`flush_due` regularly.
    """

    def __init__(self, hold_delay_s: float = 0.12) -> None:
        if hold_delay_s < 0:
            raise ValueError("hold_delay_s must be non-negative")
        self.hold_delay_s = float(hold_delay_s)
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

    def _ctrl_win_down(self) -> bool:
        return self._any_pressed(_CTRL_KEYS) and self._any_pressed(_WIN_KEYS)

    def _shift_alt_down(self) -> bool:
        return self._any_pressed(_SHIFT_KEYS) and self._any_pressed(_ALT_KEYS)

    def process(self, event: KeyEvent) -> tuple[HotkeyAction, ...]:
        """Process one event, ignoring injected and auto-repeat events."""

        if event.injected:
            return ()

        actions: list[HotkeyAction] = []
        was_ctrl_win = self._ctrl_win_down()
        space_consumed_as_toggle = False

        if event.is_key_down:
            if event.vk_code in self._pressed:
                return self.flush_due(event.timestamp)
            self._pressed.add(event.vk_code)
        else:
            if event.vk_code not in self._pressed:
                return self.flush_due(event.timestamp)
            self._pressed.remove(event.vk_code)

        is_ctrl_win = self._ctrl_win_down()

        if event.is_key_down:
            if event.vk_code == VK_ESCAPE:
                # Cancel owns the current recording lifecycle.  Suppression
                # prevents a later modifier release from also issuing STOP.
                self._hold_pending_since = None
                self._hold_active = False
                self._suppress_hold_until_release = is_ctrl_win
                actions.append(HotkeyAction.CANCEL)
            elif event.vk_code == VK_SPACE and is_ctrl_win:
                # Space only wins while Ctrl+Win is still in its explicit
                # disambiguation window.  Once push-to-talk has started it
                # owns the lifecycle until modifier release, so Space cannot
                # silently replace the active hold with a toggle operation.
                pending_since = self._hold_pending_since
                if (
                    pending_since is not None
                    and event.timestamp - pending_since < self.hold_delay_s
                ):
                    self._hold_pending_since = None
                    self._suppress_hold_until_release = True
                    space_consumed_as_toggle = True
                    actions.append(HotkeyAction.TOGGLE)
            elif event.vk_code == VK_Z and self._shift_alt_down():
                actions.append(HotkeyAction.PASTE_LAST)
            elif event.vk_code == VK_X and self._shift_alt_down():
                actions.append(HotkeyAction.COPY_LAST)

        if not was_ctrl_win and is_ctrl_win and not self._suppress_hold_until_release:
            # Low-level keyboard events for a three-key chord are ordered, even
            # when the user presses the keys together.  Space may therefore
            # already be down when the second Ctrl/Win modifier arrives.  That
            # is the same toggle gesture as Ctrl -> Win -> Space and must not
            # accidentally become push-to-talk merely because of event order.
            if VK_SPACE in self._pressed:
                self._hold_pending_since = None
                self._suppress_hold_until_release = True
                space_consumed_as_toggle = True
                actions.append(HotkeyAction.TOGGLE)
            else:
                self._hold_pending_since = event.timestamp
                if self.hold_delay_s == 0:
                    self._hold_pending_since = None
                    self._hold_active = True
                    actions.append(HotkeyAction.HOLD_START)

        if was_ctrl_win and not is_ctrl_win:
            self._hold_pending_since = None
            if self._hold_active:
                self._hold_active = False
                actions.append(HotkeyAction.HOLD_STOP)
            self._suppress_hold_until_release = False

        # A non-modifier event may arrive after the deadline while the
        # dispatcher was busy. Resolve the pending hold without waiting for
        # another queue timeout. A Space consumed inside the disambiguation
        # window is excluded so the toggle chord wins; overdue Space instead
        # resolves the hold. Modifier key-up is excluded so a quick Ctrl+Win
        # tap stays a no-op.
        releasing_ctrl_win_modifier = (
            not event.is_key_down
            and event.vk_code in (_CTRL_KEYS | _WIN_KEYS)
            and not is_ctrl_win
        )
        if not (
            space_consumed_as_toggle
            or releasing_ctrl_win_modifier
        ):
            actions.extend(self.flush_due(event.timestamp))
        return tuple(actions)

    def flush_due(self, now: Optional[float] = None) -> tuple[HotkeyAction, ...]:
        """Start a pending Ctrl+Win hold once its disambiguation delay passes."""

        if self._hold_pending_since is None:
            return ()
        now = time.monotonic() if now is None else now
        if now - self._hold_pending_since < self.hold_delay_s:
            return ()
        if not self._ctrl_win_down() or self._suppress_hold_until_release:
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
    ) -> None:
        self._callback = callback
        self._callbacks: dict[HotkeyAction, list[Callable[[], None]]] = {
            action: [] for action in HotkeyAction
        }
        if callbacks:
            for action, action_callback in callbacks.items():
                self._callbacks[HotkeyAction(action)].append(action_callback)
        self._callback_lock = threading.RLock()
        self._machine = HotkeyStateMachine(hold_delay_s=hold_delay_s)
        self._suppressor = _HookKeySuppressor()
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
            self._suppressor = _HookKeySuppressor()
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
            self._dispatch_thread.start()
            self._hook_thread.start()

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
            api = self._api
            thread_id = self._hook_thread_id
            if api is not None and thread_id is not None:
                # The hook thread creates its queue with PeekMessage before it
                # announces readiness, so PostThreadMessage is reliable here.
                api.user32.PostThreadMessageW(thread_id, 0x0012, 0, 0)  # WM_QUIT
            hook_thread = self._hook_thread
            dispatch_thread = self._dispatch_thread

        current = threading.current_thread()
        if hook_thread is not None and hook_thread is not current:
            hook_thread.join(timeout_s)
        self._events.put(_STOP)
        if dispatch_thread is not None and dispatch_thread is not current:
            dispatch_thread.join(timeout_s)

        with self._lifecycle_lock:
            self._hook_thread = None
            self._dispatch_thread = None
            self._hook_thread_id = None
            self._hook_handle = None
            self._hook_proc = None
            self._api = None

    def __enter__(self) -> "WindowsHotkeyService":
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()

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
                try:
                    item = self._events.get(timeout=0.025)
                except queue.Empty:
                    item = None
                if item is _STOP:
                    break
                actions = (
                    self._machine.process(item)
                    if isinstance(item, KeyEvent)
                    else self._machine.flush_due()
                )
                for action in actions:
                    self._emit(action)
        finally:
            for action in self._machine.shutdown():
                self._emit(action)

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
