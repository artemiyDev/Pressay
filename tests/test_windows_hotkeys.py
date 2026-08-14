from __future__ import annotations

import inspect
import queue
import threading
import time
from types import SimpleNamespace
from typing import Callable

from pressay.windows_hotkeys import (
    HotkeyAction,
    HotkeyStateMachine,
    KeyEvent,
    VK_ESCAPE,
    VK_LCONTROL,
    VK_LMENU,
    VK_LSHIFT,
    VK_LWIN,
    VK_SPACE,
    VK_X,
    VK_Z,
    WindowsHotkeyService,
    _HookKeySuppressor,
    _HookWatchdog,
    _RESET_MACHINE,
    _STOP,
)


def key(vk_code: int, down: bool, at: float, *, injected: bool = False) -> KeyEvent:
    return KeyEvent(
        vk_code=vk_code,
        is_key_down=down,
        timestamp=at,
        injected=injected,
    )


def test_ctrl_win_hold_starts_after_delay_and_stops_on_release() -> None:
    machine = HotkeyStateMachine(hold_delay_s=0.1)

    assert machine.process(key(VK_LCONTROL, True, 1.0)) == ()
    assert machine.process(key(VK_LWIN, True, 1.01)) == ()
    assert machine.flush_due(1.109) == ()
    assert machine.flush_due(1.11) == (HotkeyAction.HOLD_START,)
    assert machine.process(key(VK_LWIN, False, 1.2)) == (HotkeyAction.HOLD_STOP,)


def test_modifier_release_after_deadline_cannot_start_a_stale_hold() -> None:
    machine = HotkeyStateMachine(hold_delay_s=0.1)

    assert machine.process(key(VK_LCONTROL, True, 1.0)) == ()
    assert machine.process(key(VK_LWIN, True, 1.01)) == ()
    assert machine.process(key(VK_LWIN, False, 1.5)) == ()
    assert machine.hold_active is False


def test_ctrl_win_space_toggle_wins_over_pending_hold() -> None:
    machine = HotkeyStateMachine(hold_delay_s=0.15)

    machine.process(key(VK_LCONTROL, True, 3.0))
    machine.process(key(VK_LWIN, True, 3.01))
    assert machine.process(key(VK_SPACE, True, 3.04)) == (HotkeyAction.TOGGLE,)
    assert machine.flush_due(4.0) == ()
    assert machine.process(key(VK_LWIN, False, 4.1)) == ()


def test_ctrl_win_space_toggle_is_independent_of_three_key_event_order() -> None:
    orders = (
        (VK_LCONTROL, VK_SPACE, VK_LWIN),
        (VK_LWIN, VK_SPACE, VK_LCONTROL),
        (VK_SPACE, VK_LCONTROL, VK_LWIN),
        (VK_SPACE, VK_LWIN, VK_LCONTROL),
    )

    for order in orders:
        machine = HotkeyStateMachine(hold_delay_s=0.12)
        emitted: list[HotkeyAction] = []
        for offset, vk_code in enumerate(order):
            emitted.extend(machine.process(key(vk_code, True, 10.0 + offset * 0.01)))

        assert emitted == [HotkeyAction.TOGGLE], order
        assert machine.flush_due(11.0) == ()
        assert machine.hold_active is False


def test_space_during_active_hold_keeps_ptt_active_until_single_stop() -> None:
    machine = HotkeyStateMachine(hold_delay_s=0.1)

    assert machine.process(key(VK_LCONTROL, True, 5.0)) == ()
    assert machine.process(key(VK_LWIN, True, 5.01)) == ()
    assert machine.flush_due(5.11) == (HotkeyAction.HOLD_START,)

    assert machine.process(key(VK_SPACE, True, 5.12)) == ()
    assert machine.hold_active is True
    assert machine.process(key(VK_LWIN, False, 5.2)) == (HotkeyAction.HOLD_STOP,)
    assert machine.process(key(VK_LCONTROL, False, 5.21)) == ()


def test_zero_delay_hold_is_immediate() -> None:
    machine = HotkeyStateMachine(hold_delay_s=0)

    assert machine.process(key(VK_LCONTROL, True, 1.0)) == ()
    assert machine.process(key(VK_LWIN, True, 1.01)) == (HotkeyAction.HOLD_START,)
    assert machine.shutdown() == (HotkeyAction.HOLD_STOP,)
    assert not machine.pressed_keys


def test_escape_cancels_active_hold_without_later_stop() -> None:
    machine = HotkeyStateMachine(hold_delay_s=0)
    machine.process(key(VK_LCONTROL, True, 1.0))
    machine.process(key(VK_LWIN, True, 1.01))

    assert machine.process(key(VK_ESCAPE, True, 1.1)) == (HotkeyAction.CANCEL,)
    assert machine.process(key(VK_LWIN, False, 1.2)) == ()


def test_shift_alt_last_text_shortcuts_and_repeat_suppression() -> None:
    machine = HotkeyStateMachine()
    machine.process(key(VK_LSHIFT, True, 1.0))
    machine.process(key(VK_LMENU, True, 1.01))

    assert machine.process(key(VK_Z, True, 1.02)) == (HotkeyAction.PASTE_LAST,)
    assert machine.process(key(VK_Z, True, 1.03)) == ()
    machine.process(key(VK_Z, False, 1.04))
    assert machine.process(key(VK_X, True, 1.05)) == (HotkeyAction.COPY_LAST,)


def test_injected_events_do_not_change_state_or_emit_actions() -> None:
    machine = HotkeyStateMachine(hold_delay_s=0)

    assert machine.process(key(VK_LCONTROL, True, 1.0, injected=True)) == ()
    assert machine.process(key(VK_LWIN, True, 1.01, injected=True)) == ()
    assert machine.process(key(VK_SPACE, True, 1.02, injected=True)) == ()
    assert machine.pressed_keys == frozenset()


def test_hook_swallows_ctrl_win_and_releases_forwarded_ctrl() -> None:
    suppressor = _HookKeySuppressor()

    assert suppressor.process(key(VK_LCONTROL, True, 1.0)).suppress is False
    decision = suppressor.process(key(VK_LWIN, True, 1.01))
    assert decision.suppress is True
    assert decision.synthetic_events == ((VK_LCONTROL, False),)
    assert suppressor.process(key(VK_SPACE, True, 1.02)).suppress is True
    assert suppressor.process(key(VK_SPACE, False, 1.03)).suppress is True
    assert suppressor.process(key(VK_LWIN, False, 1.04)).suppress is True
    assert suppressor.process(key(VK_LCONTROL, False, 1.05)).suppress is True


def test_hook_swallows_win_then_ctrl_without_replaying_win() -> None:
    suppressor = _HookKeySuppressor()

    assert suppressor.process(key(VK_LWIN, True, 1.0)).suppress is True
    decision = suppressor.process(key(VK_LCONTROL, True, 1.01))
    assert decision.suppress is True
    assert decision.synthetic_events == ()
    assert suppressor.process(key(VK_LCONTROL, False, 1.02)).suppress is True
    assert suppressor.process(key(VK_LWIN, False, 1.03)).suppress is True


def test_hook_replays_plain_win_tap() -> None:
    suppressor = _HookKeySuppressor()

    assert suppressor.process(key(VK_LWIN, True, 1.0)).suppress is True
    decision = suppressor.process(key(VK_LWIN, False, 1.1))
    assert decision.suppress is True
    assert decision.synthetic_events == ((VK_LWIN, True), (VK_LWIN, False))


def test_hook_replays_win_before_an_ordinary_windows_shortcut() -> None:
    suppressor = _HookKeySuppressor()

    assert suppressor.process(key(VK_LWIN, True, 1.0)).suppress is True
    decision = suppressor.process(key(0x45, True, 1.01))  # Win+E
    assert decision.suppress is False
    assert decision.synthetic_events == ((VK_LWIN, True),)
    assert suppressor.process(key(0x45, False, 1.02)).suppress is False
    assert suppressor.process(key(VK_LWIN, False, 1.03)).suppress is False


def test_hook_leaves_ctrl_alone_untouched() -> None:
    suppressor = _HookKeySuppressor()

    assert suppressor.process(key(VK_LCONTROL, True, 1.0)).suppress is False
    assert suppressor.process(key(VK_LCONTROL, False, 1.1)).suppress is False


# --- _HookWatchdog: pure-logic watchdog, exercised with plain stand-ins so it
# needs neither Windows nor a real thread. ---


class _Clock:
    """A controllable stand-in for time.monotonic()."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _advance_on_wait(clock: _Clock) -> Callable[[float], bool]:
    """A ``wait`` stand-in that advances the fake clock like a real timed wait."""

    def wait(timeout: float) -> bool:
        clock.now += timeout
        return False

    return wait


def test_watchdog_requests_routine_reinstall_when_nothing_is_pressed() -> None:
    """The common, almost-always-taken case: a hook lost to
    LowLevelHooksTimeout drops with the modifiers already released, so a
    plain reinstall on every interval recovers it without ever detecting
    the drop.
    """

    clock = _Clock()
    requests: list[bool] = []
    watchdog = _HookWatchdog(
        pressed_snapshot=lambda: (frozenset(), 0.0),
        now=clock,
        hook_ready=lambda: True,
        request_reinstall=lambda reset: requests.append(reset),
        wait=_advance_on_wait(clock),
    )

    for _ in range(5):
        watchdog.poll_once()

    assert requests == [False] * 5


def test_watchdog_never_interrupts_a_live_gesture() -> None:
    """The main regression guard: this is exactly the scenario the previous
    three attempts at this watchdog each broke in a different way -- an
    autorepeat-dependent timeout, a physical/hook-state mismatch, and an
    idle-timer-defeating synthetic probe.  A held gesture, kept fresh, must
    never be interrupted for as long as any legitimate recording can last.
    """

    clock = _Clock()
    requests: list[bool] = []
    watchdog = _HookWatchdog(
        pressed_snapshot=lambda: (frozenset({VK_LCONTROL, VK_LWIN}), 0.0),
        now=clock,
        hook_ready=lambda: True,
        request_reinstall=lambda reset: requests.append(reset),
        wait=_advance_on_wait(clock),
        stale_after_s=360.0,
    )

    # 10 polls at the 30s cadence = 300s = audio.py's 5-minute recording
    # cap, comfortably inside stale_after_s.
    for _ in range(10):
        watchdog.poll_once()
        clock.now += watchdog.reinstall_interval_s

    assert requests == []


def test_watchdog_requests_reset_reinstall_once_hold_state_goes_stale() -> None:
    """A watched modifier stuck pressed for longer than any legitimate
    recording means the hook died mid-hold and the release never arrived.
    """

    clock = _Clock()
    requests: list[bool] = []
    watchdog = _HookWatchdog(
        pressed_snapshot=lambda: (frozenset({VK_LWIN}), 0.0),
        now=clock,
        hook_ready=lambda: True,
        request_reinstall=lambda reset: requests.append(reset),
        wait=_advance_on_wait(clock),
        stale_after_s=360.0,
    )

    clock.now = 359.9
    watchdog.poll_once()
    assert requests == []  # still under the threshold

    clock.now = 360.0
    watchdog.poll_once()
    assert requests == [True]


def test_watchdog_does_nothing_while_the_hook_thread_is_not_ready() -> None:
    clock = _Clock()
    requests: list[bool] = []
    watchdog = _HookWatchdog(
        pressed_snapshot=lambda: (frozenset(), 0.0),
        now=clock,
        hook_ready=lambda: False,
        request_reinstall=lambda reset: requests.append(reset),
        wait=_advance_on_wait(clock),
    )

    for _ in range(10):
        watchdog.poll_once()
        clock.now += watchdog.reinstall_interval_s

    assert requests == []


def test_watchdog_constructor_injects_no_synthetic_input() -> None:
    """Structural regression guard: nothing in the constructor should even
    look like it could inject keyboard input again -- that was the previous
    design's disqualifying side effect (it reset the OS idle timer via
    GetLastInputInfo and permanently defeated screen blanking, the
    screensaver, auto-lock and sleep).  This fails loudly if it ever
    reappears, regardless of what it might be named.
    """

    for name in inspect.signature(_HookWatchdog.__init__).parameters:
        lowered = name.lower()
        assert "probe" not in lowered
        assert "inject" not in lowered
        assert "keybd" not in lowered
        assert "synthetic" not in lowered


def test_watchdog_run_polls_until_wait_reports_stop() -> None:
    clock = _Clock()
    poll_count = 0

    class _CountingWatchdog(_HookWatchdog):
        def poll_once(self) -> None:  # type: ignore[override]
            nonlocal poll_count
            poll_count += 1
            super().poll_once()

    waits: list[float] = []

    def wait(timeout: float) -> bool:
        waits.append(timeout)
        return len(waits) >= 3  # stop after the third wait

    watchdog = _CountingWatchdog(
        pressed_snapshot=lambda: (frozenset(), 0.0),
        now=clock,
        hook_ready=lambda: False,  # nothing to decide: only run()'s cadence matters
        request_reinstall=lambda reset: None,
        wait=wait,
    )

    watchdog.run()

    assert poll_count == 3
    assert waits == [watchdog.reinstall_interval_s] * 3


# --- WindowsHotkeyService: watchdog wiring, dispatch-loop timeout, and
# reinstall side effects, all exercised without a real Win32 hook. ---


def test_dispatch_loop_blocks_indefinitely_without_a_pending_hold() -> None:
    class _RecordingQueue:
        def __init__(self, items: list) -> None:
            self._items = list(items)
            self.timeouts: list[float | None] = []

        def get(self, timeout: float | None = None):
            self.timeouts.append(timeout)
            if self._items:
                return self._items.pop(0)
            raise queue.Empty

        def put(self, item) -> None:
            self._items.append(item)

    service = WindowsHotkeyService(hold_delay_s=0.12)
    recording = _RecordingQueue(
        [
            key(VK_LCONTROL, True, 1.0),
            key(VK_LWIN, True, 1.01),  # arms a delayed hold (hold_pending)
            _STOP,
        ]
    )
    service._events = recording

    service._dispatch_loop()

    assert recording.timeouts[0] is None  # nothing pending yet: block forever
    assert 0.025 in recording.timeouts  # once armed, poll to resolve the delay


def test_stop_before_start_is_a_safe_no_op_for_the_watchdog() -> None:
    service = WindowsHotkeyService()

    assert service._watchdog is None
    assert service._watchdog_thread is None

    service.stop()  # must not raise even though nothing was ever started

    assert service._watchdog is None
    assert service._watchdog_thread is None


def test_watchdog_wiring_is_inert_before_the_hook_thread_is_ready() -> None:
    service = WindowsHotkeyService()

    assert service._hook_thread_ready() is False
    service._request_hook_reinstall(False)  # no api/thread id yet: must not raise
    service._request_hook_reinstall(True)


def test_hook_thread_ready_reflects_api_and_thread_id_being_set() -> None:
    service = WindowsHotkeyService()
    assert service._hook_thread_ready() is False

    service._api = SimpleNamespace()
    assert service._hook_thread_ready() is False  # thread id still missing

    service._hook_thread_id = 4242
    assert service._hook_thread_ready() is True


def test_publish_pressed_snapshot_only_stamps_the_timestamp_on_change() -> None:
    """The watchdog reads ``_hook_known_pressed`` from another thread as an
    immutable ``(frozenset, float)`` tuple the dispatcher thread reassigns
    (atomic under the GIL, no lock needed).  The timestamp must move only
    when the watched set actually changes: the watchdog uses it as "how
    long has this been unchanged", not "when was this last touched".
    """

    service = WindowsHotkeyService()

    service._machine.process(key(VK_LCONTROL, True, 1.0))
    service._publish_pressed_snapshot()
    pressed, changed_at = service._hook_known_pressed
    assert pressed == frozenset({VK_LCONTROL})

    # No state change: republishing must not move the timestamp.
    service._publish_pressed_snapshot()
    assert service._hook_known_pressed == (pressed, changed_at)

    service._machine.process(key(VK_LWIN, True, 1.01))
    service._publish_pressed_snapshot()
    pressed_2, changed_at_2 = service._hook_known_pressed
    assert pressed_2 == frozenset({VK_LCONTROL, VK_LWIN})
    assert changed_at_2 >= changed_at


def test_dispatch_loop_publishes_pressed_snapshot_via_the_queue() -> None:
    service = WindowsHotkeyService()

    dispatch_thread = threading.Thread(target=service._dispatch_loop, daemon=True)
    dispatch_thread.start()
    try:
        service._events.put(key(VK_LWIN, True, 1.0))
        for _ in range(200):
            if service._hook_known_pressed[0] == frozenset({VK_LWIN}):
                break
            time.sleep(0.01)
        assert service._hook_known_pressed[0] == frozenset({VK_LWIN})
    finally:
        service._events.put(_STOP)
        dispatch_thread.join(timeout=3.0)


def test_reinstall_sets_the_new_hook_before_unhooking_the_old_one() -> None:
    """If this order were reversed and SetWindowsHookExW then failed, the
    service would be left with no hook installed at all until the next
    30-second attempt.
    """

    service = WindowsHotkeyService()
    service._hook_proc = object()
    order: list[str] = []
    fake_api = SimpleNamespace(
        user32=SimpleNamespace(
            SetWindowsHookExW=lambda *_a: order.append("set") or 4242,
            UnhookWindowsHookEx=lambda _h: order.append("unhook"),
        ),
        kernel32=SimpleNamespace(GetModuleHandleW=lambda _name: 0),
        ctypes=SimpleNamespace(get_last_error=lambda: 0),
    )

    new_handle = service._reinstall_hook(fake_api, old_handle=1234, reset=False)

    assert new_handle == 4242
    assert order == ["set", "unhook"]


def test_routine_reinstall_without_reset_leaves_suppressor_and_machine_alone() -> None:
    service = WindowsHotkeyService()
    service._hook_proc = object()
    old_suppressor = service._suppressor
    fake_api = SimpleNamespace(
        user32=SimpleNamespace(
            SetWindowsHookExW=lambda *_a: 4242,
            UnhookWindowsHookEx=lambda _h: None,
        ),
        kernel32=SimpleNamespace(GetModuleHandleW=lambda _name: 0),
        ctypes=SimpleNamespace(get_last_error=lambda: 0),
    )

    new_handle = service._reinstall_hook(fake_api, old_handle=None, reset=False)

    assert new_handle == 4242
    assert service._hook_handle == 4242
    assert service._suppressor is old_suppressor
    assert service._events.empty()


def test_reset_reinstall_queues_reset_machine_for_the_dispatcher() -> None:
    service = WindowsHotkeyService()
    service._hook_proc = object()
    fake_api = SimpleNamespace(
        user32=SimpleNamespace(
            SetWindowsHookExW=lambda *_a: 4242,
            UnhookWindowsHookEx=lambda _h: None,
        ),
        kernel32=SimpleNamespace(GetModuleHandleW=lambda _name: 0),
        ctypes=SimpleNamespace(get_last_error=lambda: 0),
    )

    service._reinstall_hook(fake_api, old_handle=None, reset=True)

    assert service._events.get_nowait() is _RESET_MACHINE


def test_reset_reinstall_recreates_suppressor_and_ends_active_hold_via_dispatcher() -> None:
    received: list[HotkeyAction] = []
    service = WindowsHotkeyService(callback=received.append, hold_delay_s=0)

    # Simulate an active push-to-talk hold, as if it had started before the
    # hook was silently dropped by Windows.
    service._machine.process(key(VK_LCONTROL, True, 1.0))
    service._machine.process(key(VK_LWIN, True, 1.01))
    assert service._machine.hold_active is True

    old_suppressor = service._suppressor
    service._hook_proc = object()
    unhooked: list[object] = []
    fake_api = SimpleNamespace(
        user32=SimpleNamespace(
            UnhookWindowsHookEx=lambda handle: unhooked.append(handle),
            SetWindowsHookExW=lambda *_args: 4242,
        ),
        kernel32=SimpleNamespace(GetModuleHandleW=lambda _name: 0),
        ctypes=SimpleNamespace(get_last_error=lambda: 0),
    )

    dispatch_thread = threading.Thread(target=service._dispatch_loop, daemon=True)
    dispatch_thread.start()
    try:
        new_handle = service._reinstall_hook(fake_api, old_handle=1234, reset=True)
    finally:
        service._events.put(_STOP)
        dispatch_thread.join(timeout=3.0)

    assert new_handle == 4242
    assert service._hook_handle == 4242
    assert unhooked == [1234]
    assert service._suppressor is not old_suppressor
    assert received == [HotkeyAction.HOLD_STOP]
    assert not dispatch_thread.is_alive()


def test_failed_reinstall_is_logged_and_keeps_the_previous_hook() -> None:
    service = WindowsHotkeyService()
    service._hook_proc = object()
    unhooked: list[object] = []
    fake_api = SimpleNamespace(
        user32=SimpleNamespace(
            UnhookWindowsHookEx=lambda h: unhooked.append(h),
            SetWindowsHookExW=lambda *_args: 0,  # Win32 failure convention
        ),
        kernel32=SimpleNamespace(GetModuleHandleW=lambda _name: 0),
        ctypes=SimpleNamespace(get_last_error=lambda: 5),
    )

    new_handle = service._reinstall_hook(fake_api, old_handle=1234, reset=False)

    assert new_handle == 1234  # the old, possibly-still-live hook stays in place
    assert unhooked == []  # never unhooked: nothing ever replaced it
    assert service._events.empty()  # no reset queued for a routine attempt
