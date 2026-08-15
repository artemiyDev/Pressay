from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from pressay.ui import (
    RECORDING_LEVEL_ACTIVE_COLOR,
    RECORDING_LEVEL_QUIET_COLOR,
    StatusOverlay,
    recording_level_fraction,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_recording_level_fraction_uses_a_perceptible_logarithmic_scale() -> None:
    assert recording_level_fraction(0.0) == 0.0
    assert 0.0 < recording_level_fraction(0.001) < 0.2
    assert recording_level_fraction(0.1) > 0.85
    assert recording_level_fraction(10.0) == 1.0


def test_recording_overlay_updates_only_its_level_bar_from_the_provider(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = _app()
    level = [0.001]
    overlay = StatusOverlay(level_provider=lambda: level[0])
    try:
        overlay.show_status("Слушаю…", "recording")
        app.processEvents()

        assert not overlay._level_track.isHidden()
        assert overlay._level_timer.isActive()
        assert overlay._level_fill.width() > 0
        assert RECORDING_LEVEL_ACTIVE_COLOR in overlay._level_fill.styleSheet()

        level[0] = 0.0001
        overlay._refresh_level()

        assert RECORDING_LEVEL_QUIET_COLOR in overlay._level_fill.styleSheet()
        overlay.show_status("Готов к диктовке", "ready", auto_hide_ms=1)
        assert overlay._level_track.isHidden()
        assert not overlay._level_timer.isActive()
    finally:
        overlay.hide()
        overlay.close()
        app.processEvents()
