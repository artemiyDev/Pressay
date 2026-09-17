from __future__ import annotations

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
    """Значение чекбокса из values должно попасть в сохранённый AppConfig."""

    config_on = AppConfig(copy_on_insertion_failure=True)
    updated_off = app_module._build_updated_config(
        config_on, {"copy_on_insertion_failure": False}, config_on.microphone
    )
    assert updated_off.copy_on_insertion_failure is False

    config_off = AppConfig(copy_on_insertion_failure=False)
    updated_on = app_module._build_updated_config(
        config_off, {"copy_on_insertion_failure": True}, config_off.microphone
    )
    assert updated_on.copy_on_insertion_failure is True
