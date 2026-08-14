from __future__ import annotations

import pytest

from pressay.config import AppConfig, ConfigError
from pressay.hotkey_bindings import (
    HOLD_MODIFIER_PAIRS,
    Chord,
    HotkeyBindingError,
    HotkeyBindings,
    from_mapping,
    parse_chord,
    parse_hold_modifiers,
    parse_key,
)
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


VK_A = 0x41
VK_V = 0x56


def key(vk_code: int, down: bool, at: float = 0.0) -> KeyEvent:
    return KeyEvent(vk_code=vk_code, is_key_down=down, timestamp=at)


def chord(text: str) -> Chord:
    parsed = parse_chord(text, "test")
    assert parsed is not None
    return parsed


# --- parsing -----------------------------------------------------------------


def test_chord_parsing_ignores_order_case_and_padding() -> None:
    assert chord("  ALT + shift+Z ").text() == "shift+alt+z"
    assert chord("z+shift+alt").text() == "shift+alt+z"


def test_chord_label_uses_the_familiar_shipping_order() -> None:
    defaults = HotkeyBindings()
    assert defaults.hold_label() == "Ctrl+Win"
    assert defaults.toggle_label() == "Ctrl+Win+Space"
    assert defaults.paste_last.label() == "Shift+Alt+Z"
    assert defaults.copy_last.label() == "Shift+Alt+X"


def test_hold_pair_is_canonical_regardless_of_written_order() -> None:
    assert parse_hold_modifiers("win+ctrl") == ("ctrl", "win")
    assert parse_hold_modifiers("CTRL + Win") == ("ctrl", "win")


def test_every_supported_hold_pair_parses_back_to_itself() -> None:
    for pair in HOLD_MODIFIER_PAIRS:
        assert parse_hold_modifiers("+".join(pair)) == pair


def test_none_and_empty_string_disable_an_action() -> None:
    bindings = from_mapping(
        {"toggle_key": "none", "paste_last": "", "copy_last": "none"}
    )
    assert bindings.toggle_key is None
    assert bindings.toggle_vk is None
    assert bindings.paste_last is None
    assert bindings.copy_last is None


def test_mapping_round_trip_preserves_every_binding() -> None:
    bindings = from_mapping(
        {
            "hold_modifiers": "ctrl+alt",
            "toggle_key": "f9",
            "paste_last": "ctrl+shift+v",
            "copy_last": "none",
            "push_to_talk": False,
        }
    )
    assert from_mapping(bindings.to_mapping()) == bindings


@pytest.mark.parametrize(
    "raw",
    [
        {"hold_modifiers": "ctrl"},
        {"hold_modifiers": "ctrl+win+alt"},
        {"hold_modifiers": "ctrl+ctrl"},
        {"hold_modifiers": "ctrl+enter"},
        {"toggle_key": "shift"},
        {"paste_last": "z"},
        {"paste_last": "shift+alt+z+x"},
        {"paste_last": "shift+alt"},
        {"paste_last": "shift+alt+z", "copy_last": "alt+shift+z"},
        {"copy_last": "ctrl+win+space"},
        {"push_to_talk": 1},
        {"push_to_talk": "true"},
        {"hold_modifiers": 5},
    ],
)
def test_unusable_binding_sets_are_refused(raw: dict[str, object]) -> None:
    with pytest.raises(HotkeyBindingError):
        from_mapping(raw)


def test_modifier_is_not_accepted_as_a_regular_key() -> None:
    with pytest.raises(HotkeyBindingError):
        parse_key("ctrl", "toggle_key")


def test_deferred_modifier_is_win_then_alt_and_never_ctrl_or_shift() -> None:
    assert HotkeyBindings(hold_modifiers=("ctrl", "win")).deferred_modifier == "win"
    assert HotkeyBindings(hold_modifiers=("ctrl", "alt")).deferred_modifier == "alt"
    assert HotkeyBindings(hold_modifiers=("win", "alt")).deferred_modifier == "win"
    assert HotkeyBindings(hold_modifiers=("ctrl", "shift")).deferred_modifier is None


# --- configuration -----------------------------------------------------------


def test_config_without_a_hotkeys_section_uses_the_shipping_defaults() -> None:
    config = AppConfig.from_dict({"model": "turbo"})
    assert config.hotkeys == HotkeyBindings()


def test_config_reports_a_broken_hotkeys_section_as_config_error() -> None:
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.from_dict({"hotkeys": {"hold_modifiers": "ctrl+enter"}})
    assert "hotkeys" in str(excinfo.value)


def test_config_to_dict_emits_the_canonical_nested_hotkeys_object() -> None:
    config = AppConfig.from_dict({"hotkeys": {"hold_modifiers": "alt+ctrl"}})
    assert config.to_dict()["hotkeys"] == {
        "hold_modifiers": "ctrl+alt",
        "toggle_key": "space",
        "paste_last": "shift+alt+z",
        "copy_last": "shift+alt+x",
        "push_to_talk": True,
    }


def test_config_save_load_round_trip_keeps_custom_hotkeys(tmp_path) -> None:
    path = tmp_path / "config.json"
    original = AppConfig.from_dict(
        {"hotkeys": {"hold_modifiers": "ctrl+shift", "paste_last": "ctrl+alt+v"}}
    )
    original.save(path)
    assert AppConfig.load(path).hotkeys == original.hotkeys


# --- state machine with custom bindings --------------------------------------


def machine(**kwargs: object) -> HotkeyStateMachine:
    return HotkeyStateMachine(hold_delay_s=0.1, bindings=from_mapping(kwargs))


def test_custom_hold_pair_responds_to_its_own_chord_only() -> None:
    state = machine(hold_modifiers="ctrl+alt")
    state.process(key(VK_LCONTROL, True, 0.0))
    assert state.process(key(VK_LWIN, True, 0.01)) == ()
    assert state.flush_due(0.5) == ()

    assert state.process(key(VK_LMENU, True, 0.6)) == ()
    assert state.flush_due(0.8) == (HotkeyAction.HOLD_START,)
    assert state.process(key(VK_LMENU, False, 0.9)) == (HotkeyAction.HOLD_STOP,)


def test_custom_toggle_key_fires_and_the_old_one_does_not() -> None:
    state = machine(hold_modifiers="ctrl+win", toggle_key="f9")
    state.process(key(VK_LCONTROL, True, 0.0))
    state.process(key(VK_LWIN, True, 0.01))
    assert state.process(key(VK_SPACE, True, 0.02)) == ()
    assert state.process(key(0x78, True, 0.03)) == (HotkeyAction.TOGGLE,)


def test_custom_paste_and_copy_chords_replace_the_defaults() -> None:
    state = machine(paste_last="ctrl+shift+v", copy_last="ctrl+shift+a")
    state.process(key(VK_LCONTROL, True, 0.0))
    state.process(key(VK_LSHIFT, True, 0.01))
    assert state.process(key(VK_V, True, 0.02)) == (HotkeyAction.PASTE_LAST,)
    assert state.process(key(VK_A, True, 0.03)) == (HotkeyAction.COPY_LAST,)

    state.process(key(VK_LCONTROL, False, 0.04))
    state.process(key(VK_LMENU, True, 0.05))
    assert state.process(key(VK_Z, True, 0.06)) == ()
    assert state.process(key(VK_X, True, 0.07)) == ()


def test_disabled_actions_never_fire() -> None:
    state = machine(toggle_key="none", paste_last="none", copy_last="none")
    state.process(key(VK_LSHIFT, True, 0.0))
    state.process(key(VK_LMENU, True, 0.01))
    assert state.process(key(VK_Z, True, 0.02)) == ()
    assert state.process(key(VK_X, True, 0.03)) == ()
    state.process(key(VK_LSHIFT, False, 0.04))
    state.process(key(VK_LMENU, False, 0.05))

    state.process(key(VK_LCONTROL, True, 0.06))
    state.process(key(VK_LWIN, True, 0.07))
    assert state.process(key(VK_SPACE, True, 0.08)) == ()


def test_push_to_talk_off_keeps_toggle_and_never_starts_a_hold() -> None:
    state = machine(push_to_talk=False)
    state.process(key(VK_LCONTROL, True, 0.0))
    assert state.process(key(VK_LWIN, True, 0.01)) == ()
    assert state.hold_pending is False
    assert state.flush_due(5.0) == ()

    assert state.process(key(VK_SPACE, True, 5.1)) == (HotkeyAction.TOGGLE,)
    state.process(key(VK_SPACE, False, 5.2))
    assert state.process(key(VK_LWIN, False, 5.3)) == ()
    assert state.process(key(VK_LCONTROL, False, 5.4)) == ()


def test_push_to_talk_off_still_cancels_on_escape() -> None:
    state = machine(push_to_talk=False)
    assert state.process(key(VK_ESCAPE, True, 0.0)) == (HotkeyAction.CANCEL,)


# --- suppressor with custom bindings -----------------------------------------


def suppressor(**kwargs: object) -> _HookKeySuppressor:
    return _HookKeySuppressor(from_mapping(kwargs))


def test_alt_pair_defers_and_replays_alt_exactly_like_win_does() -> None:
    guard = suppressor(hold_modifiers="ctrl+alt")

    held = guard.process(key(VK_LMENU, True, 0.0))
    assert held.suppress is True
    assert held.synthetic_events == ()

    released = guard.process(key(VK_LMENU, False, 0.1))
    assert released.suppress is True
    assert released.synthetic_events == ((VK_LMENU, True), (VK_LMENU, False))


def test_alt_pair_takes_the_gesture_once_ctrl_joins() -> None:
    guard = suppressor(hold_modifiers="ctrl+alt")
    guard.process(key(VK_LMENU, True, 0.0))
    joined = guard.process(key(VK_LCONTROL, True, 0.05))
    assert joined.suppress is True

    swallowed = guard.process(key(VK_SPACE, True, 0.06))
    assert swallowed.suppress is True


def test_ctrl_shift_pair_never_withholds_anything() -> None:
    # Ctrl and Shift are used constantly with the mouse, whose events this hook
    # cannot see, so withholding either would break Ctrl+click.
    guard = suppressor(hold_modifiers="ctrl+shift")

    for event in (
        key(VK_LCONTROL, True, 0.0),
        key(VK_LSHIFT, True, 0.01),
        key(VK_LCONTROL, False, 0.2),
        key(VK_LSHIFT, False, 0.21),
    ):
        decision = guard.process(event)
        assert decision.suppress is False
        assert decision.synthetic_events == ()


def test_ctrl_shift_pair_still_owns_the_toggle_key_while_held() -> None:
    guard = suppressor(hold_modifiers="ctrl+shift")
    guard.process(key(VK_LCONTROL, True, 0.0))
    guard.process(key(VK_LSHIFT, True, 0.01))

    assert guard.process(key(VK_SPACE, True, 0.02)).suppress is True
    assert guard.process(key(VK_ESCAPE, True, 0.03)).suppress is True
    # An unrelated key must still reach the foreground application.
    assert guard.process(key(VK_A, True, 0.04)).suppress is False


def test_custom_toggle_key_is_the_one_swallowed_during_the_gesture() -> None:
    guard = suppressor(hold_modifiers="ctrl+win", toggle_key="f9")
    guard.process(key(VK_LWIN, True, 0.0))
    guard.process(key(VK_LCONTROL, True, 0.01))

    assert guard.process(key(0x78, True, 0.02)).suppress is True
    assert guard.process(key(VK_SPACE, True, 0.03)).suppress is False
