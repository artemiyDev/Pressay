from __future__ import annotations

import json

import pytest
from PySide6.QtWidgets import QApplication

from pressay.app import _settings_dict
from pressay.config import AppConfig, ConfigError
from pressay.text import process_transcript
from pressay.ui import MicrophoneChoice, SettingsWindow, UiSignals


def test_voice_formatting_replaces_commands_inside_an_utterance() -> None:
    result = process_transcript(
        "Первая строка С  новой\tстроки вторая. Первый абзац, второй.",
        remove_fillers=False,
        voice_formatting=True,
    )

    assert result.text == "Первая строка\nвторая. Первый\n\nвторой."


def test_voice_formatting_is_opt_in_and_keeps_whole_word_boundaries() -> None:
    source = "с новой строки абзацем абзац"

    assert process_transcript(source, remove_fillers=False).text == source
    assert process_transcript(
        source, remove_fillers=False, voice_formatting=True
    ).text == "\nабзацем\n\n"


@pytest.mark.parametrize(
    ("command", "expected"),
    [("с новой строки!", "\n"), ("абзац…", "\n\n")],
)
def test_voice_formatting_allows_a_command_as_the_entire_utterance(
    command: str, expected: str
) -> None:
    assert process_transcript(
        command, remove_fillers=False, voice_formatting=True
    ).text == expected


def test_voice_formatting_precedes_replacements() -> None:
    result = process_transcript(
        "метка с новой строки продолжение",
        remove_fillers=False,
        voice_formatting=True,
        replacements={"метка": "абзац", "продолжение": "абзац"},
    )

    assert result.text == "абзац\nабзац"


def test_voice_formatting_config_round_trip_and_invalid_value(tmp_path) -> None:
    target = tmp_path / "config.json"
    config = AppConfig(voice_formatting=True)

    assert AppConfig().voice_formatting is False
    config.save(target)

    assert AppConfig.load(target).voice_formatting is True
    target.write_text(json.dumps({"voice_formatting": 1}), encoding="utf-8")
    with pytest.raises(ConfigError, match="voice_formatting"):
        AppConfig.load(target)


def test_voice_formatting_checkbox_is_exposed_in_current_settings(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    window = SettingsWindow(
        UiSignals(),
        _settings_dict(AppConfig(voice_formatting=True)),
        [MicrophoneChoice(None, "Системный микрофон")],
    )
    try:
        assert window.voice_formatting_checkbox.isChecked() is True
        assert window.current_settings()["voice_formatting"] is True
    finally:
        window.prepare_to_quit()
        window.close()
        QApplication.instance().processEvents()
