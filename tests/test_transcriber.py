from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from pressay.transcriber import (
    FasterWhisperTranscriber,
    HallucinationDetected,
    NoSpeechDetected,
    TranscriptionError,
)


@pytest.fixture(autouse=True)
def _default_transcriber_tests_use_windows_backend_order(monkeypatch) -> None:
    monkeypatch.setattr("pressay.transcriber.sys.platform", "win32")


def test_macos_auto_backend_uses_cpu_int8_without_cuda_probe(monkeypatch) -> None:
    monkeypatch.setattr("pressay.transcriber.sys.platform", "darwin")

    transcriber = FasterWhisperTranscriber(device="auto", compute_type="auto")

    assert transcriber._attempts() == [("cpu", "int8")]


class FakeModel:
    def __init__(
        self,
        segments: list[object],
        *,
        fail_during_iteration: bool = False,
        language: str | None = "ru",
        language_probability: float = 0.97,
    ) -> None:
        self.segments = segments
        self.fail_during_iteration = fail_during_iteration
        self.language = language
        self.language_probability = language_probability
        self.calls: list[tuple[np.ndarray, dict[str, object]]] = []
        self.iterations = 0

    def transcribe(self, audio: np.ndarray, **kwargs: object):
        self.calls.append((audio.copy(), kwargs))

        def generate():
            self.iterations += 1
            if self.fail_during_iteration:
                raise RuntimeError("CUDA driver unavailable")
            yield from self.segments

        info = SimpleNamespace(
            language=self.language,
            language_probability=self.language_probability,
        )
        return generate(), info


class LanguageAwareFakeModel:
    def __init__(
        self,
        responses: dict[str | None, tuple[list[object], str | None]],
        *,
        failures: set[str | None] | None = None,
        language_probs: list[tuple[str, float]] | None = None,
    ) -> None:
        self.responses = responses
        self.failures = failures or set()
        self.language_probs = language_probs
        self.calls: list[str | None] = []

    def transcribe(self, _audio: np.ndarray, **kwargs: object):
        language = kwargs["language"]
        assert language is None or isinstance(language, str)
        self.calls.append(language)

        def generate():
            if language in self.failures:
                raise RuntimeError(f"{language or 'auto'} inference failed")
            yield from self.responses[language][0]

        info = SimpleNamespace(
            language=self.responses[language][1],
            language_probability=0.91,
            all_language_probs=self.language_probs,
        )
        return generate(), info


def speech(seconds: float = 0.2, rate: int = 16_000) -> np.ndarray:
    t = np.arange(int(seconds * rate), dtype=np.float32) / rate
    return (0.05 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def segment(
    text: str,
    *,
    no_speech_prob: float = 0.02,
    avg_logprob: float = -0.2,
) -> SimpleNamespace:
    return SimpleNamespace(
        start=0.0,
        end=0.2,
        text=text,
        no_speech_prob=no_speech_prob,
        avg_logprob=avg_logprob,
    )


def test_auto_falls_back_to_cpu_and_reuses_persistent_model() -> None:
    cpu_model = FakeModel([segment(" Привет "), segment("мир")])
    attempts: list[tuple[str, str]] = []
    local_only_values: list[bool] = []

    def factory(_size: str, **kwargs: object) -> FakeModel:
        device = str(kwargs["device"])
        compute = str(kwargs["compute_type"])
        attempts.append((device, compute))
        local_only_values.append(bool(kwargs["local_files_only"]))
        if device == "cuda":
            raise RuntimeError("CUDA is not available")
        return cpu_model

    transcriber = FasterWhisperTranscriber(
        model_factory=factory,
        min_audio_seconds=0.05,
    )
    first = transcriber.transcribe(speech(), language="auto")
    second = transcriber.transcribe(speech(), language="ru")

    assert attempts == [("cuda", "int8_float16"), ("cpu", "int8")]
    assert local_only_values == [True, True]
    assert first.text == "Привет мир"
    assert first.language == "ru"
    assert first.language_probability == pytest.approx(0.97)
    assert first.device == "cpu"
    assert first.compute_type == "int8"
    assert first.timings.model_load_seconds >= 0
    assert len(first.segments) == 2
    assert second.timings.model_load_seconds == 0
    assert cpu_model.iterations == 2  # proves the returned generator was consumed
    assert cpu_model.calls[0][1]["language"] is None
    assert cpu_model.calls[1][1]["language"] == "ru"
    assert cpu_model.calls[0][1]["vad_filter"] is True
    assert cpu_model.calls[0][1]["vad_parameters"] == {
        "threshold": 0.5,
        "min_speech_duration_ms": 150,
        "min_silence_duration_ms": 300,
        "speech_pad_ms": 200,
    }


def test_warmup_runs_exactly_one_inference_probe() -> None:
    model = FakeModel([])
    transcriber = FasterWhisperTranscriber(
        device="cpu",
        model_factory=lambda *_args, **_kwargs: model,
        min_audio_seconds=0.05,
    )

    assert transcriber.warmup() == ("cpu", "int8")
    assert transcriber.warmup() == ("cpu", "int8")

    assert len(model.calls) == 1
    probe, options = model.calls[0]
    assert probe.shape == (3_200,)
    assert options["language"] == "ru"
    assert options["vad_filter"] is False


def test_generator_cuda_failure_retries_once_on_cpu() -> None:
    cuda_model = FakeModel([], fail_during_iteration=True)
    cpu_model = FakeModel([segment("works")])
    attempts: list[str] = []

    def factory(_size: str, **kwargs: object) -> FakeModel:
        device = str(kwargs["device"])
        attempts.append(device)
        return cuda_model if device == "cuda" else cpu_model

    transcriber = FasterWhisperTranscriber(
        model_factory=factory,
        min_audio_seconds=0.05,
    )

    result = transcriber.transcribe(speech(), language="en")

    assert result.text == "works"
    assert result.device == "cpu"
    assert attempts == ["cuda", "cpu"]
    assert cuda_model.iterations == 1
    assert cpu_model.iterations == 1


def test_non_16k_audio_is_resampled_before_model() -> None:
    model = FakeModel([segment("resampled")])
    transcriber = FasterWhisperTranscriber(
        device="cpu",
        model_factory=lambda *_args, **_kwargs: model,
        min_audio_seconds=0.05,
    )

    result = transcriber.transcribe(speech(rate=48_000), sample_rate=48_000)

    assert result.audio_duration_seconds == pytest.approx(0.2)
    assert model.calls[0][0].shape == (3_200,)


@pytest.mark.parametrize("detected_language", ["de", None])
def test_auto_wrong_or_missing_language_uses_only_ru_en_candidates(
    detected_language: str | None,
) -> None:
    model = LanguageAwareFakeModel(
        {
            None: ([segment("nicht zuruckgeben")], detected_language),
            "ru": ([segment("русский вариант", avg_logprob=-0.15)], "ru"),
            "en": ([segment("English fallback", avg_logprob=-0.55)], "en"),
        }
    )
    transcriber = FasterWhisperTranscriber(
        device="cpu",
        model_factory=lambda *_args, **_kwargs: model,
        min_audio_seconds=0.05,
    )

    result = transcriber.transcribe(speech(), language="auto")

    # Without per-language probabilities the historical RU-then-EN order
    # applies, and a trustworthy RU transcript ends the search.
    assert model.calls == [None, "ru"]
    assert result.language == "ru"
    assert result.text == "русский вариант"
    assert "nicht" not in result.text


def test_auto_fallback_follows_the_model_language_posterior_not_text_score() -> None:
    # RU would win on transcript score alone (much better avg_logprob), but the
    # model itself is confident the audio is English, and only RU/EN exist.
    model = LanguageAwareFakeModel(
        {
            None: ([segment("kalt draussen")], "de"),
            "ru": ([segment("совершенно другое", avg_logprob=-0.05)], "ru"),
            "en": ([segment("the actual sentence", avg_logprob=-0.60)], "en"),
        },
        language_probs=[("de", 0.44), ("en", 0.40), ("ru", 0.02)],
    )
    transcriber = FasterWhisperTranscriber(
        device="cpu",
        model_factory=lambda *_args, **_kwargs: model,
        min_audio_seconds=0.05,
    )

    result = transcriber.transcribe(speech(), language="auto")

    assert model.calls == [None, "en"]
    assert result.language == "en"
    assert result.text == "the actual sentence"


def test_auto_fallback_still_tries_the_other_language_when_preferred_one_fails() -> None:
    model = LanguageAwareFakeModel(
        {
            None: ([segment("bonjour")], "fr"),
            "en": ([segment("   ")], "en"),
            "ru": ([segment("нормальный текст", avg_logprob=-0.2)], "ru"),
        },
        language_probs=[("fr", 0.5), ("en", 0.3), ("ru", 0.1)],
    )
    transcriber = FasterWhisperTranscriber(
        device="cpu",
        model_factory=lambda *_args, **_kwargs: model,
        min_audio_seconds=0.05,
    )

    result = transcriber.transcribe(speech(), language="auto")

    assert model.calls == [None, "en", "ru"]
    assert result.language == "ru"
    assert result.text == "нормальный текст"


def test_auto_ru_en_fast_path_does_not_run_fixed_candidates() -> None:
    model = LanguageAwareFakeModel(
        {None: ([segment("hello")], "EN")}
    )
    transcriber = FasterWhisperTranscriber(
        device="cpu",
        model_factory=lambda *_args, **_kwargs: model,
        min_audio_seconds=0.05,
    )

    result = transcriber.transcribe(speech(), language="auto")

    assert model.calls == [None]
    assert result.language == "en"
    assert result.text == "hello"


def test_auto_fallback_skips_failed_candidate_and_never_returns_third_language() -> None:
    model = LanguageAwareFakeModel(
        {
            None: ([segment("bonjour")], "fr"),
            "ru": ([segment("unused")], "ru"),
            "en": ([segment("reliable English", avg_logprob=-0.1)], "de"),
        },
        failures={"ru"},
    )
    transcriber = FasterWhisperTranscriber(
        device="cpu",
        model_factory=lambda *_args, **_kwargs: model,
        min_audio_seconds=0.05,
    )

    result = transcriber.transcribe(speech(), language="auto")

    assert model.calls == [None, "ru", "en"]
    assert result.language == "en"
    assert result.text == "reliable English"


def test_auto_fallback_rejects_empty_and_hallucinated_candidates() -> None:
    model = LanguageAwareFakeModel(
        {
            None: ([segment("bonjour")], "fr"),
            "ru": ([segment("Спасибо за просмотр")], "ru"),
            "en": ([segment("   ")], "en"),
        }
    )
    transcriber = FasterWhisperTranscriber(
        device="cpu",
        model_factory=lambda *_args, **_kwargs: model,
        min_audio_seconds=0.05,
    )

    with pytest.raises(NoSpeechDetected):
        transcriber.transcribe(speech(), language="auto")

    assert model.calls == [None, "ru", "en"]


def test_auto_fallback_reports_when_both_fixed_inferences_fail() -> None:
    model = LanguageAwareFakeModel(
        {
            None: ([segment("bonjour")], "fr"),
            "ru": ([segment("unused")], "ru"),
            "en": ([segment("unused")], "en"),
        },
        failures={"ru", "en"},
    )
    transcriber = FasterWhisperTranscriber(
        device="cpu",
        model_factory=lambda *_args, **_kwargs: model,
        min_audio_seconds=0.05,
    )

    with pytest.raises(TranscriptionError, match="Both RU and EN"):
        transcriber.transcribe(speech(), language="auto")

    assert model.calls == [None, "ru", "en"]


def test_rejects_model_no_speech_segments() -> None:
    model = FakeModel(
        [segment("invented", no_speech_prob=0.99, avg_logprob=-2.0)]
    )
    transcriber = FasterWhisperTranscriber(
        device="cpu",
        model_factory=lambda *_args, **_kwargs: model,
        min_audio_seconds=0.05,
    )

    with pytest.raises(NoSpeechDetected):
        transcriber.transcribe(speech())


@pytest.mark.parametrize(
    "hallucination",
    ["[BLANK_AUDIO]", "Спасибо за просмотр", "music", "да да да да да да"],
)
def test_rejects_known_silence_hallucinations(hallucination: str) -> None:
    model = FakeModel([segment(hallucination)])
    transcriber = FasterWhisperTranscriber(
        device="cpu",
        model_factory=lambda *_args, **_kwargs: model,
        min_audio_seconds=0.05,
    )

    with pytest.raises(HallucinationDetected):
        transcriber.transcribe(speech())


def test_rejects_raw_silence_short_audio_and_unknown_language() -> None:
    factory_called = False

    def factory(*_args: object, **_kwargs: object) -> object:
        nonlocal factory_called
        factory_called = True
        return object()

    transcriber = FasterWhisperTranscriber(
        device="cpu", model_factory=factory, min_audio_seconds=0.1
    )

    with pytest.raises(NoSpeechDetected):
        transcriber.transcribe(np.zeros(3_200, dtype=np.float32))
    with pytest.raises(NoSpeechDetected):
        transcriber.transcribe(np.ones(100, dtype=np.float32) * 0.1)
    with pytest.raises(ValueError):
        transcriber.transcribe(speech(), language="de")
    assert factory_called is False
