from __future__ import annotations

from pressay.macos_hotkeys import HotkeyAction, MacHotkeyStateMachine


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
