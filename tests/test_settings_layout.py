from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from pressay.app import _settings_dict
from pressay.config import AppConfig
from pressay.ui import MicrophoneChoice, SettingsWindow, UiSignals


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _window(
    *,
    config: AppConfig | None = None,
) -> tuple[QApplication, UiSignals, SettingsWindow]:
    app = _app()
    signals = UiSignals()
    window = SettingsWindow(
        signals,
        _settings_dict(config or AppConfig()),
        [MicrophoneChoice(None, "Системный микрофон")],
        macos=False,
    )
    return app, signals, window


def _close(app: QApplication, window: SettingsWindow) -> None:
    window.prepare_to_quit()
    window.close()
    app.processEvents()


def test_initial_size_is_bounded_by_the_available_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app, _signals, window = _window()
    try:
        screen = app.primaryScreen()
        assert screen is not None
        available = screen.availableGeometry()
        assert window.width() == min(
            620,
            max(window.minimumWidth(), available.width() - 32),
        )
        assert window.height() == min(
            700,
            max(window.minimumHeight(), available.height() - 32),
        )
    finally:
        _close(app, window)


def test_initial_size_fits_simulated_hidpi_logical_screen(
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
    app, _signals, window = _window()
    try:
        assert window.size().width() == 480
        assert window.size().height() == 352
        assert window.width() <= 512
        assert window.height() <= 384

        window.show()
        app.processEvents()
        assert window.settings_scroll.horizontalScrollBar().maximum() == 0
        assert window.toggle_button.isVisible()
        assert window.test_button.isVisible()
        assert window.save_button.isVisible()
    finally:
        _close(app, window)


def test_small_window_scrolls_vertically_with_sticky_primary_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app, _signals, window = _window()
    try:
        window.resize(440, 520)
        window.show()
        app.processEvents()

        scroll = window.settings_scroll
        vertical = scroll.verticalScrollBar()
        horizontal = scroll.horizontalScrollBar()
        assert window.minimumWidth() == 440
        assert window.minimumHeight() == 320
        assert scroll.widgetResizable() is True
        assert vertical.maximum() > 0
        assert horizontal.maximum() == 0
        assert horizontal.isVisible() is False
        assert window.action_panel.geometry().top() >= scroll.geometry().bottom()
        assert window.toggle_button.isVisible()
        assert window.test_button.isVisible()
        assert window.save_button.isVisible()

        vertical.setValue(vertical.maximum())
        app.processEvents()
        assert vertical.value() == vertical.maximum()
        assert window.save_button.isVisible()
    finally:
        _close(app, window)


def test_missing_microphone_remains_explicit_and_preserves_original_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = _app()
    signals = UiSignals()
    original = "pressay:microphone:v1?name=Missing+Mic&host_api=WASAPI&sample_rate=48000"
    window = SettingsWindow(
        signals,
        {**_settings_dict(AppConfig()), "microphone": original},
        [
            MicrophoneChoice(None, "Системный микрофон"),
            MicrophoneChoice(
                original,
                "Недоступен: Missing Mic",
                available=False,
            ),
        ],
        macos=False,
    )
    try:
        assert window.microphone_combo.currentIndex() == 1
        assert window.microphone_combo.currentData() == original
        assert window.microphone_combo.currentText() == "Недоступен: Missing Mic"
        assert window.current_settings()["microphone"] == original
    finally:
        _close(app, window)


def test_microphone_probe_ui_resets_controls_and_meter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app, _signals, window = _window()
    try:
        window.begin_microphone_test()
        assert window.microphone_combo.isEnabled() is False
        assert window.test_button.isEnabled() is False
        assert window.test_button.text() == "Проверяю…"
        assert window.microphone_test_meter.value() == 0

        window.update_microphone_test_level(0.02, 0.08)
        assert window.microphone_test_meter.value() > 0
        assert "RMS" in window.microphone_test_meter.accessibleDescription()

        window.finish_microphone_test("Сигнал микрофона обнаружен", "success")
        assert window.microphone_combo.isEnabled() is True
        assert window.test_button.isEnabled() is True
        assert window.test_button.text() == "Проверить микрофон"
        assert window.microphone_test_meter.value() == 0
        assert window.status_label.text() == "Сигнал микрофона обнаружен"
        window.update_microphone_test_level(0.5, 0.8)
        assert window.microphone_test_meter.value() == 0
    finally:
        _close(app, window)


@pytest.mark.parametrize("width", (440, 480, 620))
def test_target_width_has_no_horizontal_scroll_or_hidden_content(
    monkeypatch: pytest.MonkeyPatch,
    width: int,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app, _signals, window = _window()
    try:
        window.resize(width, 520)
        window.show()
        app.processEvents()

        scroll = window.settings_scroll
        assert scroll.horizontalScrollBar().maximum() == 0
        assert scroll.widget() is not None
        assert scroll.widget().width() <= scroll.viewport().width()
        content = scroll.widget()
        for control in (
            window.voice_translate_checkbox,
            window.push_to_talk_checkbox,
            window.strict_editable_check_checkbox,
            window.model_combo,
            window.clear_transcript_history_button,
        ):
            left = control.mapTo(content, QPoint(0, 0)).x()
            assert left >= 0
            assert left + control.width() <= content.width()
    finally:
        _close(app, window)


def test_focus_chain_reaches_translation_and_sticky_actions_without_default_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app, signals, window = _window(
        config=AppConfig(voice_translate=True, translate_model="medium")
    )
    toggles: list[bool] = []
    signals.toggle_requested.connect(lambda: toggles.append(True))
    try:
        window.resize(440, 520)
        window.show()
        app.processEvents()

        wanted = {
            window.voice_translate_checkbox,
            window.translate_model_combo,
            window.dictionary_edit,
            window.last_transcript,
            window.toggle_button,
            window.test_button,
            window.save_button,
        }
        current = window.microphone_combo
        reached: set[object] = set()
        for _ in range(100):
            reached.add(current)
            current = current.nextInFocusChain()
            if current is window.microphone_combo:
                break

        assert wanted <= reached
        assert window.toggle_button.isDefault() is False
        assert window.toggle_button.autoDefault() is False

        vertical = window.settings_scroll.verticalScrollBar()
        window.toggle_key_edit.setFocus()
        app.processEvents()
        vertical.setValue(0)
        app.processEvents()
        QTest.keyClick(window.toggle_key_edit, Qt.Key.Key_Tab)
        app.processEvents()
        focused = app.focusWidget()
        assert focused is window.paste_last_edit
        assert vertical.value() > 0
        focused_top = focused.mapTo(
            window.settings_scroll.viewport(),
            QPoint(0, 0),
        ).y()
        assert focused_top >= 0
        assert focused_top + focused.height() <= window.settings_scroll.viewport().height()

        window.toggle_key_edit.setFocus()
        QTest.keyClick(window.toggle_key_edit, Qt.Key.Key_Return)
        app.processEvents()
        assert toggles == []
    finally:
        _close(app, window)


def test_tab_leaves_dictionary_and_reaches_sticky_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app, _signals, window = _window()
    try:
        window.resize(440, 320)
        window.show()
        app.processEvents()

        window.copy_last_edit.setFocus()
        app.processEvents()
        QTest.keyClick(window.copy_last_edit, Qt.Key.Key_Tab)
        app.processEvents()
        assert app.focusWidget() is window.dictionary_edit
        dictionary_top = window.dictionary_edit.mapTo(
            window.settings_scroll.viewport(),
            QPoint(0, 0),
        ).y()
        assert dictionary_top >= 0
        assert (
            dictionary_top + window.dictionary_edit.height()
            <= window.settings_scroll.viewport().height()
        )

        reached: list[object] = []
        for _ in range(8):
            QTest.keyClick(app.focusWidget(), Qt.Key.Key_Tab)
            app.processEvents()
            reached.append(app.focusWidget())
            if app.focusWidget() is window.save_button:
                break

        assert window.last_transcript in reached
        assert window.toggle_button in reached
        assert window.test_button in reached
        assert window.save_button in reached
        assert window.dictionary_edit.accessibleName() == "Личный словарь"
        assert "произношение" in window.dictionary_edit.accessibleDescription()
        assert window.last_transcript.accessibleName() == "История расшифровок"
        assert "20" in window.last_transcript.accessibleDescription()

        QTest.keyClick(window.save_button, Qt.Key.Key_Tab)
        app.processEvents()
        assert app.focusWidget() is window.microphone_combo
        microphone_top = window.microphone_combo.mapTo(
            window.settings_scroll.viewport(),
            QPoint(0, 0),
        ).y()
        assert microphone_top >= 0
        assert (
            microphone_top + window.microphone_combo.height()
            <= window.settings_scroll.viewport().height()
        )
    finally:
        _close(app, window)


def test_sticky_save_emits_settings_and_translation_controls_stay_in_scroll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app, signals, window = _window(
        config=AppConfig(voice_translate=True, translate_model="medium")
    )
    saved: list[dict[str, object]] = []
    signals.save_requested.connect(saved.append)
    try:
        window.resize(440, 520)
        window.show()
        app.processEvents()

        content = window.settings_scroll.widget()
        assert content is not None
        assert content.isAncestorOf(window.voice_translate_checkbox)
        assert content.isAncestorOf(window.translate_model_combo)
        assert "Голосом" in window.voice_translate_checkbox.text()
        assert "перевод" in window.voice_translate_checkbox.text().casefold()
        assert "переведена" not in window.language_hint.text().casefold()
        assert "распознаться неверно" in window.language_hint.text().casefold()
        resource_labels = [
            window.resource_mode_combo.itemText(index)
            for index in range(window.resource_mode_combo.count())
        ]
        assert not any("GPU" in label for label in resource_labels)
        assert "остаётся загруженной" in resource_labels[0]
        assert "после каждой диктовки" in resource_labels[2]
        for combo in (
            window.model_combo,
            window.resource_mode_combo,
            window.translate_model_combo,
        ):
            assert combo.toolTip() == combo.currentText()
            assert combo.accessibleDescription() == combo.currentText()
        conflict_hint = window.hotkeys_conflict_label.text().casefold()
        assert "конфликтов не имеет" not in conflict_hint
        assert "универсально бесконфликтной пары нет" in conflict_hint
        privacy_hint = window._privacy_label.text().casefold()
        assert "первой загрузки" not in privacy_hint
        assert "выбранных моделей" in privacy_hint
        assert "распознавание затем работает локально" in privacy_hint
        QTest.mouseClick(window.save_button, Qt.MouseButton.LeftButton)
        app.processEvents()

        assert len(saved) == 1
        assert saved[0]["voice_translate"] is True
        assert saved[0]["translate_model"] == "medium"
    finally:
        _close(app, window)
