from __future__ import annotations

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
    _HookKeySuppressor,
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
