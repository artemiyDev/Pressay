from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from pressay.app import _settings_dict
from pressay.config import AppConfig
from pressay.ui import MicrophoneChoice, SettingsWindow, UiSignals


MAC_SHORTCUTS = {
    "hold": "Control+Option",
    "toggle": "Control+Option+Space",
    "cancel": "Esc",
    "paste": "Control+Option+V",
    "copy": "Control+Option+C",
}


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _window(
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: dict[str, Any] | None = None,
    detect_platform: bool = False,
) -> tuple[QApplication, UiSignals, SettingsWindow]:
    app = _app()
    monkeypatch.setattr(
        "pressay.ui.hotkey_hint",
        lambda action: MAC_SHORTCUTS[action],
    )
    if detect_platform:
        monkeypatch.setattr("pressay.ui.is_macos", lambda: True)
    signals = UiSignals()
    window = SettingsWindow(
        signals,
        settings or _settings_dict(AppConfig()),
        [MicrophoneChoice(None, "Системный микрофон")],
        **({} if detect_platform else {"macos": True}),
    )
    return app, signals, window


def _close(app: QApplication, window: SettingsWindow) -> None:
    window.prepare_to_quit()
    window.close()
    app.processEvents()


def test_macos_platform_detection_shows_fixed_read_only_shortcuts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _signals, window = _window(monkeypatch, detect_platform=True)
    try:
        window.resize(440, 520)
        window.show()
        app.processEvents()

        assert window.hotkeys_editor is None
        assert window.macos_hotkeys_panel is not None
        assert window.macos_hotkeys_panel.isVisible()
        assert not hasattr(window, "hold_modifiers_combo")
        assert not hasattr(window, "toggle_key_edit")
        assert not window.macos_hotkeys_panel.findChildren(QLineEdit)
        assert {
            action: label.text()
            for action, label in window.macos_shortcut_labels.items()
        } == MAC_SHORTCUTS
        assert all(
            label.focusPolicy() == Qt.FocusPolicy.NoFocus
            for label in window.macos_shortcut_labels.values()
        )
    finally:
        _close(app, window)


def test_macos_save_preserves_initial_hotkeys_without_revalidating_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_hotkeys = {
        "hold_modifiers": "ctrl+alt",
        "toggle_key": "f9",
        "paste_last": "ctrl+shift+v",
        "copy_last": "none",
        "push_to_talk": False,
    }
    settings = _settings_dict(AppConfig(voice_translate=True, translate_model="medium"))
    settings["hotkeys"] = custom_hotkeys
    app, signals, window = _window(monkeypatch, settings=settings)
    saved: list[dict[str, Any]] = []
    signals.save_requested.connect(saved.append)
    try:
        monkeypatch.setattr(
            "pressay.ui.hotkey_bindings.from_mapping",
            lambda _raw: (_ for _ in ()).throw(
                AssertionError("macOS save must not revalidate Windows hotkeys")
            ),
        )

        current = window.current_settings()
        assert current["hotkeys"] == custom_hotkeys
        assert current["voice_translate"] is True
        assert current["translate_model"] == "medium"

        QTest.mouseClick(window.save_button, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert len(saved) == 1
        assert saved[0]["hotkeys"] == custom_hotkeys
    finally:
        _close(app, window)


def test_macos_real_tab_skips_fixed_shortcuts_and_reaches_sticky_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _signals, window = _window(monkeypatch)
    try:
        window.resize(440, 520)
        window.show()
        app.processEvents()

        window.voice_translate_checkbox.setFocus()
        app.processEvents()
        reached: list[object] = [app.focusWidget()]
        for _ in range(40):
            QTest.keyClick(app.focusWidget(), Qt.Key.Key_Tab)
            app.processEvents()
            reached.append(app.focusWidget())
            if app.focusWidget() is window.save_button:
                break

        assert window.translate_model_combo in reached
        assert window.dictionary_edit in reached
        assert window.toggle_button in reached
        assert window.test_button in reached
        assert window.save_button in reached
        assert not any(isinstance(widget, QLineEdit) for widget in reached)
        assert not any(
            label in reached for label in window.macos_shortcut_labels.values()
        )
    finally:
        _close(app, window)


def test_macos_440px_layout_has_no_horizontal_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _signals, window = _window(monkeypatch)
    try:
        window.resize(440, 520)
        window.show()
        app.processEvents()

        content = window.settings_scroll.widget()
        assert content is not None
        assert window.settings_scroll.horizontalScrollBar().maximum() == 0
        assert content.width() <= window.settings_scroll.viewport().width()
        assert window.macos_hotkeys_panel is not None
        for control in (
            window.macos_hotkeys_panel,
            window.voice_translate_checkbox,
            window.translate_model_combo,
        ):
            left = control.mapTo(content, QPoint(0, 0)).x()
            assert left >= 0
            assert left + control.width() <= content.width()
        assert window.toggle_button.isVisible()
        assert window.test_button.isVisible()
        assert window.save_button.isVisible()
    finally:
        _close(app, window)


def test_macos_layout_fits_simulated_hidpi_logical_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LogicalScreen:
        @staticmethod
        def availableGeometry() -> QRect:  # noqa: N802 - Qt API spelling
            return QRect(0, 0, 512, 384)

    monkeypatch.setattr(
        "pressay.ui.QGuiApplication.primaryScreen",
        lambda: LogicalScreen(),
    )
    app, _signals, window = _window(monkeypatch)
    try:
        assert window.size().width() == 480
        assert window.size().height() == 352
        window.show()
        app.processEvents()

        assert window.settings_scroll.horizontalScrollBar().maximum() == 0
        assert window.settings_scroll.verticalScrollBar().maximum() > 0
        assert window.toggle_button.isVisible()
        assert window.test_button.isVisible()
        assert window.save_button.isVisible()
    finally:
        _close(app, window)


def test_runtime_warning_is_hidden_persistent_and_theme_aware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _signals, window = _window(monkeypatch)
    try:
        window.show()
        app.processEvents()
        assert window.runtime_warning_label.isHidden()

        window.set_runtime_warning("  Разрешите Accessibility  ")
        app.processEvents()
        assert window.runtime_warning_label.isVisible()
        assert window.runtime_warning_label.text() == "Разрешите Accessibility"
        assert window.runtime_warning_label.accessibleName() == "Предупреждение Pressay"

        window.update_status("Готов к диктовке", "ready")
        assert window.runtime_warning_label.isVisible()
        assert window.runtime_warning_label.text() == "Разрешите Accessibility"

        window._apply_theme(Qt.ColorScheme.Dark)
        warning_style = window.runtime_warning_label.styleSheet()
        assert "#451a03" in warning_style
        assert "#fde68a" in warning_style

        window.set_runtime_warning(None)
        assert window.runtime_warning_label.isHidden()
        assert window.runtime_warning_label.text() == ""
    finally:
        _close(app, window)
