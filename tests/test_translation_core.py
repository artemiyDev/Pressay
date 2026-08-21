from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from pressay.config import AppConfig, ConfigError
from pressay.text import process_transcript, translation_command
from pressay.transcriber import FasterWhisperTranscriber


class _TaskRecordingModel:
    def __init__(self) -> None:
        self.tasks: list[str] = []

    def transcribe(self, _audio: np.ndarray, **kwargs: object):
        self.tasks.append(str(kwargs["task"]))
        segment = SimpleNamespace(
            start=0.0,
            end=0.2,
            text=" Translated text ",
            no_speech_prob=0.01,
            avg_logprob=-0.1,
        )
        info = SimpleNamespace(language="ru", language_probability=0.99)
        return iter((segment,)), info


def _speech() -> np.ndarray:
    time_axis = np.arange(3_200, dtype=np.float32) / 16_000
    return (0.05 * np.sin(2 * np.pi * 220 * time_axis)).astype(np.float32)


def test_transcriber_passes_translate_task_to_model() -> None:
    model = _TaskRecordingModel()
    transcriber = FasterWhisperTranscriber(
        device="cpu",
        model_factory=lambda *_args, **_kwargs: model,
        min_audio_seconds=0.05,
    )

    result = transcriber.transcribe(_speech(), language="ru", task="translate")

    assert result.text == "Translated text"
    assert model.tasks == ["translate"]


@pytest.mark.parametrize("task", ("summarize", "", None))
def test_transcriber_rejects_unknown_task_before_loading_model(task: object) -> None:
    loaded = False

    def factory(*_args: object, **_kwargs: object) -> object:
        nonlocal loaded
        loaded = True
        return object()

    transcriber = FasterWhisperTranscriber(
        device="cpu",
        model_factory=factory,
        min_audio_seconds=0.05,
    )

    with pytest.raises(ValueError, match="task must be transcribe or translate"):
        transcriber.transcribe(_speech(), task=task)  # type: ignore[arg-type]

    assert loaded is False


@pytest.mark.parametrize(
    "phrase",
    (
        "переведи на английский",
        "Переведи на английский язык!",
        "перевод на английский…",
        "translate to english.",
    ),
)
def test_translation_on_command_is_a_whole_phrase(phrase: str) -> None:
    assert translation_command(phrase, enabled=True) == "on"
    processed = process_transcript(
        phrase,
        voice_translate=True,
        snippets={"переведи на английский": "не раскрывать"},
        replacements={"translate to english": "not a command"},
    )
    assert processed.text == ""
    assert processed.translation_mode == "on"


@pytest.mark.parametrize(
    "phrase",
    (
        "хватит переводить",
        "Выключи перевод!",
        "отмени перевод.",
        "stop translating…",
    ),
)
def test_translation_off_command_is_a_whole_phrase(phrase: str) -> None:
    assert translation_command(phrase, enabled=True) == "off"
    assert process_transcript(phrase, voice_translate=True).translation_mode == "off"


def test_translation_command_is_opt_in_and_does_not_match_inside_sentence() -> None:
    disabled = process_transcript(
        "переведи на английский",
        remove_fillers=False,
        voice_translate=False,
    )

    assert disabled.text == "переведи на английский"
    assert disabled.translation_mode is None
    assert translation_command("пожалуйста, переведи на английский", enabled=True) is None


def test_translation_config_round_trip_and_defaults(tmp_path) -> None:
    target = tmp_path / "config.json"
    config = AppConfig(voice_translate=True, translate_model="medium")

    config.save(target)

    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["voice_translate"] is True
    assert saved["translate_model"] == "medium"
    assert "translating" not in saved
    assert AppConfig.load(target) == config
    assert AppConfig().voice_translate is False
    assert AppConfig().translate_model == "large-v3"


def test_translate_model_rejects_turbo_and_unknown_models() -> None:
    with pytest.raises(ConfigError, match="turbo не поддерживает перевод"):
        AppConfig.from_dict({"translate_model": "turbo"})
    with pytest.raises(ConfigError, match="small, medium, large-v3"):
        AppConfig.from_dict({"translate_model": "custom"})


def test_voice_translate_requires_a_json_boolean() -> None:
    with pytest.raises(ConfigError, match="voice_translate"):
        AppConfig.from_dict({"voice_translate": 1})
