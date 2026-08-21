from __future__ import annotations

from PySide6.QtWidgets import QApplication

from pressay.app import _settings_dict
from pressay.config import AppConfig
from pressay.ui import (
    STATE_COLORS_DARK,
    MicrophoneChoice,
    SettingsWindow,
    StatusOverlay,
    UiSignals,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_translation_controls_are_exposed_without_turbo(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = _app()
    window = SettingsWindow(
        UiSignals(),
        _settings_dict(
            AppConfig(voice_translate=True, translate_model="medium")
        ),
        [MicrophoneChoice(None, "Системный микрофон")],
    )
    try:
        values = [
            window.translate_model_combo.itemData(index)
            for index in range(window.translate_model_combo.count())
        ]
        hints = " ".join(label.text() for label in window._hint_labels)

        assert window.voice_translate_checkbox.isChecked() is True
        assert values == ["small", "medium", "large-v3"]
        assert "turbo" not in values
        assert window.current_settings()["voice_translate"] is True
        assert window.current_settings()["translate_model"] == "medium"
        assert "переведи на английский" in hints
        assert "хватит переводить" in hints
        assert "только на английский" in hints
        assert "Large v3" in hints
    finally:
        window.prepare_to_quit()
        window.close()
        app.processEvents()


def test_recording_overlay_shows_translation_badge_from_dark_palette(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = _app()
    translating = [True]
    overlay = StatusOverlay(translation_provider=lambda: translating[0])
    try:
        overlay.show_status("Слушаю…", "recording")
        app.processEvents()

        assert overlay._translation_badge.text() == "→ EN"
        assert not overlay._translation_badge.isHidden()
        assert STATE_COLORS_DARK["processing"] in overlay._translation_badge.styleSheet()

        translating[0] = False
        overlay.show_status("Слушаю…", "recording")
        assert overlay._translation_badge.isHidden()

        translating[0] = True
        overlay.show_status("Готов к диктовке", "ready")
        assert overlay._translation_badge.isHidden()
    finally:
        overlay.hide()
        overlay.close()
        app.processEvents()
