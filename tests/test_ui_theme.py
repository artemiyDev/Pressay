from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from pressay.app import _settings_dict
from pressay.config import AppConfig
from pressay.ui import (
    DARK_THEME,
    LIGHT_THEME,
    STATE_COLORS,
    STATE_COLORS_DARK,
    MicrophoneChoice,
    SettingsWindow,
    UiSignals,
    detect_color_scheme,
    state_colors_for_scheme,
    theme_tokens,
)


def _relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance for an ``#rrggbb`` color."""

    hex_color = hex_color.lstrip("#")
    channels = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def linearize(channel: float) -> float:
        return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4

    r, g, b = (linearize(channel) for channel in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(hex_a: str, hex_b: str) -> float:
    """WCAG contrast ratio between two ``#rrggbb`` colors."""

    lum_a, lum_b = _relative_luminance(hex_a), _relative_luminance(hex_b)
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_light_theme_tokens_match_the_original_hardcoded_literals() -> None:
    light_tokens = theme_tokens(Qt.ColorScheme.Light)

    assert light_tokens == LIGHT_THEME
    assert light_tokens == {
        "status_bg": "#f1f5f9",
        "status_text": "#0f172a",
        "subtitle_text": "#64748b",
        "privacy_bg": "#ecfdf5",
        "privacy_text": "#475569",
    }


def test_dark_theme_tokens_differ_with_dark_backgrounds_and_light_text() -> None:
    dark_tokens = theme_tokens(Qt.ColorScheme.Dark)

    assert dark_tokens == DARK_THEME
    assert dark_tokens != LIGHT_THEME
    for key in ("status_bg", "privacy_bg"):
        assert _relative_luminance(dark_tokens[key]) < _relative_luminance(LIGHT_THEME[key])
    for key in ("status_text", "subtitle_text", "privacy_text"):
        assert _relative_luminance(dark_tokens[key]) > _relative_luminance(LIGHT_THEME[key])


def test_dark_theme_status_card_and_privacy_meet_wcag_aa_contrast() -> None:
    dark_tokens = theme_tokens(Qt.ColorScheme.Dark)

    status_ratio = _contrast_ratio(dark_tokens["status_bg"], dark_tokens["status_text"])
    privacy_ratio = _contrast_ratio(dark_tokens["privacy_bg"], dark_tokens["privacy_text"])

    assert status_ratio >= 4.5
    assert privacy_ratio >= 4.5


def test_dark_state_accents_stay_readable_on_the_dark_status_card() -> None:
    dark_tokens = theme_tokens(Qt.ColorScheme.Dark)
    dark_accents = state_colors_for_scheme(Qt.ColorScheme.Dark)

    assert set(dark_accents) == set(STATE_COLORS)
    for state, color in dark_accents.items():
        assert _contrast_ratio(dark_tokens["status_bg"], color) >= 4.5, state


def test_state_colors_public_name_is_unchanged() -> None:
    assert STATE_COLORS == {
        "idle": "#64748b",
        "ready": "#22c55e",
        "recording": "#ef4444",
        "processing": "#8b5cf6",
        "success": "#22c55e",
        "warning": "#f59e0b",
        "error": "#ef4444",
    }
    assert state_colors_for_scheme(Qt.ColorScheme.Light) is STATE_COLORS


def test_detect_color_scheme_falls_back_to_light_without_a_qapplication_instance(
    monkeypatch,
) -> None:
    monkeypatch.setattr(QApplication, "instance", staticmethod(lambda: None))

    assert detect_color_scheme() == Qt.ColorScheme.Light


def test_detect_color_scheme_falls_back_to_palette_when_colorscheme_is_missing(
    monkeypatch,
) -> None:
    _app()

    class _StyleHintsWithoutColorScheme:
        """Stand-in for a Qt build predating ``colorScheme()``."""

    monkeypatch.setattr(QGuiApplication, "styleHints", staticmethod(_StyleHintsWithoutColorScheme))

    # Should not raise, and must resolve via the palette fallback (real,
    # unpatched QApplication.palette()).
    scheme = detect_color_scheme()
    assert scheme in (Qt.ColorScheme.Light, Qt.ColorScheme.Dark)


def test_detect_color_scheme_falls_back_when_reported_scheme_is_unknown(monkeypatch) -> None:
    _app()

    class _StubStyleHints:
        def colorScheme(self) -> Qt.ColorScheme:
            return Qt.ColorScheme.Unknown

    monkeypatch.setattr(QGuiApplication, "styleHints", staticmethod(lambda: _StubStyleHints()))

    scheme = detect_color_scheme()
    assert scheme in (Qt.ColorScheme.Light, Qt.ColorScheme.Dark)


def test_theme_switch_recolors_the_shown_status_without_losing_text(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = _app()

    window = SettingsWindow(
        UiSignals(),
        _settings_dict(AppConfig()),
        [MicrophoneChoice(None, "Системный микрофон")],
    )
    try:
        window.update_status("Идёт запись", "recording")
        light_style = window.status_label.styleSheet()
        assert LIGHT_THEME["status_bg"] in light_style
        assert STATE_COLORS["recording"] in light_style

        window._apply_theme(Qt.ColorScheme.Dark)
        dark_style = window.status_label.styleSheet()

        assert window.status_label.text() == "Идёт запись"
        assert dark_style != light_style
        assert DARK_THEME["status_bg"] in dark_style
        assert STATE_COLORS_DARK["recording"] in dark_style
    finally:
        window.prepare_to_quit()
        window.close()
        app.processEvents()


def test_transcript_history_keeps_twenty_newest_items_and_rejects_adjacent_duplicates(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = _app()
    window = SettingsWindow(
        UiSignals(),
        _settings_dict(AppConfig()),
        [MicrophoneChoice(None, "Системный микрофон")],
    )
    try:
        for index in range(21):
            window.set_last_transcript(f"фраза {index}")

        assert list(window._recent_transcripts) == [
            f"фраза {index}" for index in range(20, 0, -1)
        ]
        assert window.last_transcript.count() == 20
        assert window.last_transcript.item(0).data(Qt.ItemDataRole.UserRole) == "фраза 20"
        assert window.last_transcript.item(19).data(Qt.ItemDataRole.UserRole) == "фраза 1"

        window.set_last_transcript("фраза 20")
        assert window.last_transcript.count() == 20
    finally:
        window.prepare_to_quit()
        window.close()
        app.processEvents()


def test_transcript_history_copies_full_text_and_can_be_cleared(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = _app()
    window = SettingsWindow(
        UiSignals(),
        _settings_dict(AppConfig()),
        [MicrophoneChoice(None, "Системный микрофон")],
    )
    long_text = "длинная фраза " * 20
    try:
        window.set_last_transcript(long_text)
        item = window.last_transcript.item(0)

        assert item.text().endswith("…")
        assert item.toolTip() == long_text

        window.copy_transcript_button.click()
        assert QApplication.clipboard().text() == long_text
        assert window.status_label.text() == "Скопировано"
        assert window._status_state == "success"

        QApplication.clipboard().clear()
        window.last_transcript.itemDoubleClicked.emit(item)
        assert QApplication.clipboard().text() == long_text

        window.clear_transcript_history_button.click()
        assert not window._recent_transcripts
        assert window.last_transcript.count() == 0
        assert not window.copy_transcript_button.isEnabled()
    finally:
        window.prepare_to_quit()
        window.close()
        app.processEvents()
