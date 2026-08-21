from __future__ import annotations

import threading

import pytest

from pressay.macos_hotkeys import (
    HotkeyAction,
    MacHotkeyStateMachine,
    MacOSHotkeyService,
)


def test_hold_starts_after_disambiguation_and_stops_on_release() -> None:
    machine = MacHotkeyStateMachine(hold_delay_s=0.12)

    assert machine.set_chord(True, now=1.0) == ()
    assert machine.flush_due(now=1.11) == ()
    assert machine.flush_due(now=1.12) == (HotkeyAction.HOLD_START,)
    assert machine.set_chord(False, now=2.0) == (HotkeyAction.HOLD_STOP,)


def test_space_during_pending_window_toggles_without_ptt() -> None:
    machine = MacHotkeyStateMachine(hold_delay_s=0.12)

    machine.set_chord(True, now=1.0)
    assert machine.shortcut(HotkeyAction.TOGGLE) == (HotkeyAction.TOGGLE,)
    assert machine.flush_due(now=2.0) == ()
    assert machine.set_chord(False, now=2.1) == ()


def test_late_shortcut_cannot_change_active_ptt_mode() -> None:
    machine = MacHotkeyStateMachine(hold_delay_s=0.01)

    machine.set_chord(True, now=1.0)
    assert machine.flush_due(now=1.02) == (HotkeyAction.HOLD_START,)
    assert machine.shortcut(HotkeyAction.TOGGLE) == ()
    assert machine.set_chord(False, now=1.03) == (HotkeyAction.HOLD_STOP,)


def test_copy_paste_cancel_and_shutdown_actions_are_deterministic() -> None:
    machine = MacHotkeyStateMachine()
    machine.set_chord(True, now=1.0)
    assert machine.shortcut(HotkeyAction.PASTE_LAST) == (HotkeyAction.PASTE_LAST,)
    assert machine.cancel() == (HotkeyAction.CANCEL,)
    assert machine.shutdown() == ()


def test_escape_suppresses_down_repeat_and_up_for_one_accepted_cancel() -> None:
    actions: list[HotkeyAction] = []

    def callback(action: HotkeyAction) -> bool:
        actions.append(action)
        return True

    service = MacOSHotkeyService(callback)

    assert service._handle_escape(key_down=True) is True
    assert service._handle_escape(key_down=True, is_repeat=True) is True
    assert actions == [HotkeyAction.CANCEL]
    assert service._handle_escape(key_down=False) is True
    assert service._handle_escape(key_down=False) is False


@pytest.mark.parametrize("result", [False, None, 1, "accepted"])
def test_escape_passes_both_phases_unless_callback_returns_exact_true(
    result: object,
) -> None:
    actions: list[HotkeyAction] = []

    def callback(action: HotkeyAction) -> object:
        actions.append(action)
        return result

    service = MacOSHotkeyService(callback)

    assert service._handle_escape(key_down=True) is False
    assert service._handle_escape(key_down=True, is_repeat=True) is False
    assert service._handle_escape(key_down=False) is False
    assert actions == [HotkeyAction.CANCEL]


def test_escape_callback_exception_fails_open_and_next_cycle_retries() -> None:
    calls = 0

    def callback(_action: HotkeyAction) -> bool:
        nonlocal calls
        calls += 1
        raise RuntimeError("callback failed")

    service = MacOSHotkeyService(callback)

    assert service._handle_escape(key_down=True) is False
    assert service._handle_escape(key_down=True, is_repeat=True) is False
    assert service._handle_escape(key_down=False) is False
    assert service._handle_escape(key_down=True) is False
    assert service._handle_escape(key_down=False) is False
    assert calls == 2


def test_escape_decision_does_not_leak_into_the_next_press() -> None:
    results = iter([False, True])
    actions: list[HotkeyAction] = []

    def callback(action: HotkeyAction) -> bool:
        actions.append(action)
        return next(results)

    service = MacOSHotkeyService(callback)

    assert service._handle_escape(key_down=True) is False
    assert service._handle_escape(key_down=False) is False
    assert service._handle_escape(key_down=True) is True
    assert service._handle_escape(key_down=False) is True
    assert actions == [HotkeyAction.CANCEL, HotkeyAction.CANCEL]


def test_escape_recovers_when_previous_key_up_was_lost() -> None:
    results = iter([True, False])
    actions: list[HotkeyAction] = []

    def callback(action: HotkeyAction) -> bool:
        actions.append(action)
        return next(results)

    service = MacOSHotkeyService(callback)

    assert service._handle_escape(key_down=True) is True
    # No key-up arrives. Quartz marks the next physical down as non-repeat.
    assert service._handle_escape(key_down=True, is_repeat=False) is False
    assert service._handle_escape(key_down=False) is False
    assert actions == [HotkeyAction.CANCEL, HotkeyAction.CANCEL]


def test_orphan_escape_repeat_fails_open_without_calling_callback() -> None:
    actions: list[HotkeyAction] = []
    service = MacOSHotkeyService(lambda action: actions.append(action) or True)

    assert service._handle_escape(key_down=True, is_repeat=True) is False
    assert service._handle_escape(key_down=False) is False
    assert actions == []


def test_escape_reset_invalidates_inflight_callback_result() -> None:
    entered = threading.Event()
    release = threading.Event()
    result: list[bool] = []

    def callback(_action: HotkeyAction) -> bool:
        entered.set()
        assert release.wait(timeout=2)
        return True

    service = MacOSHotkeyService(callback)
    worker = threading.Thread(
        target=lambda: result.append(service._handle_escape(key_down=True))
    )
    worker.start()
    assert entered.wait(timeout=2)

    service._reset_escape_state()
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == [False]
    assert service._handle_escape(key_down=False) is False


def test_stop_clears_escape_pairing_state() -> None:
    service = MacOSHotkeyService(lambda _action: True)
    assert service._handle_escape(key_down=True) is True

    service.stop()

    assert service._handle_escape(key_down=False) is False


def test_start_clears_escape_pairing_state(monkeypatch: pytest.MonkeyPatch) -> None:
    service = MacOSHotkeyService(lambda _action: True)
    assert service._handle_escape(key_down=True) is True

    monkeypatch.setattr("pressay.macos_hotkeys.sys.platform", "darwin")
    monkeypatch.setattr(service, "_run", service._started.set)
    service.start()

    assert service._handle_escape(key_down=False) is False


def test_tap_reenable_clears_escape_pairing_before_delivery_resumes() -> None:
    calls: list[tuple[object, bool]] = []

    class FakeQuartz:
        @staticmethod
        def CGEventTapEnable(tap: object, enabled: bool) -> None:
            calls.append((tap, enabled))

    service = MacOSHotkeyService(lambda _action: True)
    service._tap = object()
    event = object()
    assert service._handle_escape(key_down=True) is True

    assert service._recover_disabled_tap(FakeQuartz, event) is event

    assert calls == [(service._tap, True)]
    assert service._handle_escape(key_down=False) is False
