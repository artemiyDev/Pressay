from __future__ import annotations

import pytest

from PySide6.QtWidgets import QApplication, QLabel

from pressay.app import _settings_dict
from pressay.config import AppConfig
from pressay.hotkey_bindings import HOLD_MODIFIER_PAIRS, HotkeyBindings
from pressay.ui import MicrophoneChoice, SettingsWindow, UiSignals


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _make_window(settings: dict) -> SettingsWindow:
    return SettingsWindow(
        UiSignals(),
        settings,
        [MicrophoneChoice(None, "Системный микрофон")],
    )


def _close(window: SettingsWindow) -> None:
    window.prepare_to_quit()
    window.close()
    QApplication.instance().processEvents()


def test_missing_hotkeys_key_shows_and_saves_the_shipped_defaults(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _app()

    settings = _settings_dict(AppConfig())
    settings.pop("hotkeys", None)  # exercise the exact contract: key absent
    window = _make_window(settings)
    try:
        defaults = HotkeyBindings().to_mapping()

        assert window.hold_modifiers_combo.currentData() == defaults["hold_modifiers"]
        assert window.push_to_talk_checkbox.isChecked() == defaults["push_to_talk"]
        assert window.toggle_key_edit.text() == defaults["toggle_key"]
        assert window.paste_last_edit.text() == defaults["paste_last"]
        assert window.copy_last_edit.text() == defaults["copy_last"]

        assert window.current_settings()["hotkeys"] == defaults
    finally:
        _close(window)


def test_current_settings_hotkeys_has_all_five_keys_and_passes_from_mapping(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _app()

    settings = _settings_dict(AppConfig())
    settings.pop("hotkeys", None)
    window = _make_window(settings)
    try:
        hotkeys = window.current_settings()["hotkeys"]

        assert set(hotkeys) == {
            "hold_modifiers",
            "toggle_key",
            "paste_last",
            "copy_last",
            "push_to_talk",
        }
        assert isinstance(hotkeys["push_to_talk"], bool)
        from pressay.hotkey_bindings import from_mapping

        from_mapping(hotkeys)  # must not raise
    finally:
        _close(window)


def test_nonstandard_hotkeys_round_trip_through_the_form(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _app()

    custom_hotkeys = {
        "hold_modifiers": "ctrl+alt",
        "toggle_key": "f9",
        "paste_last": "ctrl+shift+v",
        "copy_last": "none",
        "push_to_talk": False,
    }
    settings = _settings_dict(AppConfig())
    settings["hotkeys"] = custom_hotkeys
    window = _make_window(settings)
    try:
        assert window.hold_modifiers_combo.currentData() == "ctrl+alt"
        assert window.push_to_talk_checkbox.isChecked() is False
        assert window.toggle_key_edit.text() == "f9"
        assert window.paste_last_edit.text() == "ctrl+shift+v"
        assert window.copy_last_edit.text() == "none"

        assert window.current_settings()["hotkeys"] == custom_hotkeys
    finally:
        _close(window)


def test_invalid_hotkey_field_raises_russian_valueerror_without_saving(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _app()

    settings = _settings_dict(AppConfig())
    settings.pop("hotkeys", None)
    window = _make_window(settings)
    try:
        window.copy_last_edit.setText("паста")
        with pytest.raises(ValueError, match="Горячие клавиши"):
            window.current_settings()

        window.copy_last_edit.setText("shift+alt+x")  # restore a valid value
        window.toggle_key_edit.setText("shift+alt")
        with pytest.raises(ValueError, match="Горячие клавиши"):
            window.current_settings()
    finally:
        _close(window)


def test_none_disables_an_action_and_survives_the_round_trip(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _app()

    settings = _settings_dict(AppConfig())
    settings.pop("hotkeys", None)
    window = _make_window(settings)
    try:
        window.copy_last_edit.setText("none")

        hotkeys = window.current_settings()["hotkeys"]
        assert hotkeys["copy_last"] == "none"

        window2 = _make_window({**settings, "hotkeys": hotkeys})
        try:
            assert window2.copy_last_edit.text() == "none"
            assert window2.current_settings()["hotkeys"]["copy_last"] == "none"
        finally:
            _close(window2)
    finally:
        _close(window)


def test_hold_modifier_combo_has_exactly_the_six_documented_pairs(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _app()

    settings = _settings_dict(AppConfig())
    settings.pop("hotkeys", None)
    window = _make_window(settings)
    try:
        combo = window.hold_modifiers_combo
        assert combo.count() == len(HOLD_MODIFIER_PAIRS) == 6

        canonical_values = {combo.itemData(index) for index in range(combo.count())}
        assert canonical_values == {"+".join(pair) for pair in HOLD_MODIFIER_PAIRS}

        # Labels are the human-readable form ("Ctrl+Win"), the stored data is
        # the canonical lowercase form ("ctrl+win").
        first_index = combo.findData("ctrl+win")
        assert combo.itemText(first_index) == "Ctrl+Win"
    finally:
        _close(window)


def test_conflict_warning_is_present_in_the_window(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _app()

    settings = _settings_dict(AppConfig())
    settings.pop("hotkeys", None)
    window = _make_window(settings)
    try:
        all_text = "\n".join(label.text() for label in window.findChildren(QLabel))

        assert "AltGr" in all_text
        assert "Ctrl+Shift" in all_text
        assert "Shift+Alt" in all_text
        assert "Ctrl+Win" in all_text
    finally:
        _close(window)
