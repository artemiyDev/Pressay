from __future__ import annotations

import inspect
import json

import pytest
from PySide6.QtWidgets import QApplication

from pressay import app as app_module
from pressay.app import _settings_dict
from pressay.config import AppConfig, ConfigError
from pressay.ui import MicrophoneChoice, SettingsWindow, UiSignals


def test_copy_on_insertion_failure_is_on_by_default_and_round_trips(tmp_path) -> None:
    assert AppConfig().copy_on_insertion_failure is True
    target = tmp_path / "config.json"
    AppConfig(copy_on_insertion_failure=False).save(target)
    assert AppConfig.load(target).copy_on_insertion_failure is False
    target.write_text(json.dumps({"copy_on_insertion_failure": 1}), encoding="utf-8")
    with pytest.raises(ConfigError, match="copy_on_insertion_failure"):
        AppConfig.load(target)


def test_existing_config_without_the_key_gets_the_new_default(tmp_path) -> None:
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"auto_insert": True}), encoding="utf-8")
    assert AppConfig.load(target).copy_on_insertion_failure is True


def test_checkbox_reflects_and_reports_the_setting(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    window = SettingsWindow(
        UiSignals(),
        _settings_dict(AppConfig(copy_on_insertion_failure=False)),
        [MicrophoneChoice(None, "Системный микрофон")],
    )
    try:
        assert window.copy_on_insertion_failure_checkbox.isChecked() is False
        assert window.current_settings()["copy_on_insertion_failure"] is False
        window.copy_on_insertion_failure_checkbox.setChecked(True)
        assert window.current_settings()["copy_on_insertion_failure"] is True
    finally:
        window.prepare_to_quit()


def test_settings_save_applies_the_checkbox_value() -> None:
    """Чекбокс без проводки в save_settings молча игнорировался бы.

    save_settings — замыкание внутри сборки приложения, вызвать его отдельно
    нельзя, поэтому проверяется сам вызов.
    """

    source = inspect.getsource(app_module)
    body = source[source.index("def save_settings"):]
    body = body[: body.index("signals.save_requested.connect(save_settings)")]
    assert 'values.get("copy_on_insertion_failure"' in body
