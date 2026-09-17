"""Tests for the GigaAM Russian engine and its routing contract."""

from __future__ import annotations

import numpy as np
import pytest

from pressay.config import AppConfig, ConfigError
from pressay.gigaam import (
    DEFAULT_MODEL,
    MAX_AUDIO_SECONDS,
    EngineUnavailable,
    GigaAmTranscriber,
    normalize_peak,
)
from pressay.transcriber import (
    HallucinationDetected,
    ModelLoadError,
    NoSpeechDetected,
)

SAMPLE_RATE = 16_000


class FakeModel:
    """Stands in for onnx_asr's loaded model; records what it was fed."""

    def __init__(self, text: str = "Привет, это тест.") -> None:
        self.text = text
        self.calls: list[np.ndarray] = []

    def recognize(self, audio, *, sample_rate):  # noqa: ANN001
        assert sample_rate == SAMPLE_RATE
        self.calls.append(np.asarray(audio))
        return self.text


def _engine(text: str = "Привет, это тест.", **kwargs) -> tuple[GigaAmTranscriber, FakeModel]:
    model = FakeModel(text)
    engine = GigaAmTranscriber(model_factory=lambda *_a, **_k: model, **kwargs)
    return engine, model


def _speech(
    seconds: float = 2.0, peak: float = 0.05, rate: int = SAMPLE_RATE
) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (peak * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_normalize_peak_scales_quiet_audio_and_reports_its_gain() -> None:
    quiet = np.array([0.05, -0.025, 0.0], dtype=np.float32)
    scaled, gain = normalize_peak(quiet, target_peak=0.5)
    assert gain == pytest.approx(10.0)
    assert float(np.abs(scaled).max()) == pytest.approx(0.5, abs=1e-6)
    # Relative shape is preserved, so normalising cannot distort the waveform.
    assert scaled[1] / scaled[0] == pytest.approx(quiet[1] / quiet[0])


def test_normalize_peak_leaves_silence_alone_instead_of_amplifying_noise() -> None:
    silence = np.zeros(64, dtype=np.float32)
    scaled, gain = normalize_peak(silence)
    assert gain == 1.0
    assert np.array_equal(scaled, silence)


def test_quiet_speech_reaches_the_model_at_the_target_peak() -> None:
    engine, model = _engine()
    engine.transcribe(_speech(peak=0.02), sample_rate=SAMPLE_RATE)
    fed = model.calls[0]
    assert float(np.abs(fed).max()) == pytest.approx(0.5, abs=1e-3)


def test_result_carries_text_language_and_timings() -> None:
    engine, _ = _engine("Раз, два, три.")
    result = engine.transcribe(_speech(seconds=3.0), sample_rate=SAMPLE_RATE)
    assert result.text == "Раз, два, три."
    assert result.language == "ru"
    assert result.language_probability is None
    assert result.language_choice == "forced"
    assert result.audio_duration_seconds == pytest.approx(3.0)
    assert result.segments[0].text == "Раз, два, три."
    assert result.segments[0].end == pytest.approx(3.0)
    assert result.timings.inference_seconds >= 0.0
    assert result.device == "cpu"


def test_stereo_input_is_downmixed_before_inference() -> None:
    engine, model = _engine()
    mono = _speech()
    stereo = np.stack([mono, mono], axis=1)
    engine.transcribe(stereo, sample_rate=SAMPLE_RATE)
    assert model.calls[0].ndim == 1


def test_input_is_resampled_to_the_model_rate() -> None:
    engine, model = _engine()
    captured_at_48k = _speech(seconds=2.0, rate=48_000)
    result = engine.transcribe(captured_at_48k, sample_rate=48_000)
    assert model.calls[0].size == pytest.approx(2.0 * SAMPLE_RATE, rel=0.01)
    assert result.audio_duration_seconds == pytest.approx(2.0, rel=0.01)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"language": "en"}, "English is not a Russian-only engine's job"),
        ({"task": "translate"}, "GigaAM has no translation head"),
    ],
)
def test_engine_declines_work_it_would_do_worse_than_whisper(kwargs, reason) -> None:
    engine, _ = _engine()
    with pytest.raises(EngineUnavailable):
        engine.transcribe(_speech(), sample_rate=SAMPLE_RATE, **kwargs), reason


def test_audio_longer_than_the_encoder_window_is_declined_not_truncated() -> None:
    engine, model = _engine()
    with pytest.raises(EngineUnavailable):
        engine.transcribe(
            _speech(seconds=MAX_AUDIO_SECONDS + 1.0), sample_rate=SAMPLE_RATE
        )
    assert model.calls == [], "declined audio must never reach the model"


def test_silence_is_rejected_before_the_model_runs() -> None:
    engine, model = _engine()
    with pytest.raises(NoSpeechDetected):
        engine.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), sample_rate=SAMPLE_RATE)
    assert model.calls == []


def test_empty_transcript_is_reported_as_no_speech() -> None:
    engine, _ = _engine(text="   ")
    with pytest.raises(NoSpeechDetected):
        engine.transcribe(_speech(), sample_rate=SAMPLE_RATE)


def test_hallucinated_transcript_is_rejected() -> None:
    engine, _ = _engine(text="Продолжение следует...")
    with pytest.raises(HallucinationDetected):
        engine.transcribe(_speech(), sample_rate=SAMPLE_RATE)


def test_load_failure_surfaces_as_model_load_error() -> None:
    def explode(*_args, **_kwargs):
        raise RuntimeError("onnxruntime is unhappy")

    engine = GigaAmTranscriber(model_factory=explode)
    with pytest.raises(ModelLoadError):
        engine.load()
    assert not engine.is_loaded


def test_model_is_loaded_once_and_released_on_close() -> None:
    loads = []

    def factory(name, **_kwargs):
        loads.append(name)
        return FakeModel()

    engine = GigaAmTranscriber(model_factory=factory)
    engine.transcribe(_speech(), sample_rate=SAMPLE_RATE)
    engine.transcribe(_speech(), sample_rate=SAMPLE_RATE)
    assert loads == [DEFAULT_MODEL]
    engine.close()
    assert not engine.is_loaded


def test_unknown_model_name_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        GigaAmTranscriber(model_name="gigaam-v2-ctc")


class _Config:
    """Minimal stand-in for AppConfig's engine-routing fields."""

    def __init__(self, russian_engine: str, language: str) -> None:
        self.russian_engine = russian_engine
        self.language = language


@pytest.mark.parametrize(
    ("engine", "language", "translating", "eligible"),
    [
        ("gigaam", "ru", False, True),
        ("gigaam", "ru", True, False),
        ("gigaam", "auto", False, False),
        ("gigaam", "en", False, False),
        ("whisper", "ru", False, False),
    ],
)
def test_routing_only_sends_pinned_russian_transcription_to_gigaam(
    engine, language, translating, eligible
) -> None:
    from pressay.controller import DictationController

    config = _Config(engine, language)
    assert (
        DictationController._gigaam_is_eligible(config, translating=translating)
        is eligible
    )


def test_config_defaults_to_gigaam_for_russian() -> None:
    config = AppConfig()
    assert config.russian_engine == "gigaam"
    assert config.gigaam_model == DEFAULT_MODEL


def test_config_round_trips_the_new_engine_settings() -> None:
    config = AppConfig.from_dict({"russian_engine": "whisper"})
    assert config.russian_engine == "whisper"
    assert AppConfig.from_dict(config.to_dict()).russian_engine == "whisper"


@pytest.mark.parametrize(
    "raw",
    [
        {"russian_engine": "vosk"},
        {"gigaam_model": "gigaam-v2-rnnt"},
        {"russian_engine": ""},
    ],
)
def test_config_rejects_unsupported_engine_settings(raw) -> None:
    with pytest.raises(ConfigError):
        AppConfig.from_dict(raw)


# --- регрессии по аудиту 2026-09-08 --------------------------------------


def test_whisper_also_normalises_quiet_audio(monkeypatch) -> None:
    """Смена движка не должна незаметно менять и обработку громкости."""

    from pressay.transcriber import FasterWhisperTranscriber

    seen: list[np.ndarray] = []

    class _Model:
        def transcribe(self, audio, **_kwargs):  # noqa: ANN001
            seen.append(np.asarray(audio))
            info = type("Info", (), {"language": "ru", "language_probability": 0.99,
                                     "all_language_probs": [("ru", 0.99)]})()
            segment = type("Seg", (), {"start": 0.0, "end": 1.0, "text": "текст",
                                       "avg_logprob": -0.2, "no_speech_prob": 0.01})()
            return [segment], info

    engine = FasterWhisperTranscriber(model_factory=lambda *_a, **_k: _Model())
    engine.transcribe(_speech(peak=0.01), sample_rate=SAMPLE_RATE, language="ru")
    assert seen, "модель не получила аудио"
    assert float(np.abs(seen[0]).max()) == pytest.approx(0.5, abs=1e-3)


def test_offline_load_refuses_to_download_mid_dictation(monkeypatch, tmp_path) -> None:
    """Диктовка не должна вставать в ожидание сети при холодном кэше."""

    from pressay import gigaam as gigaam_module

    monkeypatch.setattr(gigaam_module, "_cached_model_paths", lambda _name: ())
    called: list[str] = []
    monkeypatch.setitem(
        __import__("sys").modules,
        "onnx_asr",
        type("M", (), {"load_model": staticmethod(lambda *a, **k: called.append("net"))}),
    )
    engine = GigaAmTranscriber()
    with pytest.raises(ModelLoadError):
        engine.load()
    assert called == [], "загрузчик ушёл в сеть вместо отказа"


def test_offline_load_passes_both_model_name_and_path(monkeypatch, tmp_path) -> None:
    """onnx_asr требует ОБА аргумента; по одному каталогу он архитектуру не знает.

    Прежняя версия этого теста подменяла load_model сигнатурой ``(path, **kw)``
    и потому подтверждала не настоящий API, а моё неверное представление о нём.
    Загрузка падала на живой машине при зелёном тесте.
    """

    from pressay import gigaam as gigaam_module

    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    monkeypatch.setattr(gigaam_module, "_cached_model_paths", lambda _n: (snapshot,))
    passed: list[tuple] = []

    def load_model(model, path=None, **_kwargs):  # noqa: ANN001 — сигнатура onnx_asr
        if path is None:
            raise RuntimeError("ModelNotSupportedError: одного пути недостаточно")
        passed.append((model, path))
        return FakeModel()

    monkeypatch.setitem(
        __import__("sys").modules, "onnx_asr",
        type("M", (), {"load_model": staticmethod(load_model)}),
    )
    GigaAmTranscriber().load()
    assert passed == [(DEFAULT_MODEL, str(snapshot))]


def test_saving_settings_keeps_engine_choice_that_the_window_never_shows() -> None:
    """Сохранение настроек не должно отменять откат на Whisper."""

    from dataclasses import replace

    config = AppConfig(russian_engine="whisper", language="ru", prearm_capture=True)
    updated = replace(config, model="large-v3")
    assert updated.russian_engine == "whisper"
    assert updated.prearm_capture is True
    assert updated.model == "large-v3"


def _worker_controller(config: AppConfig):
    """Контроллер, доведённый до состояния, в котором воркер согласен работать."""

    import threading
    from pressay.controller import DictationController, _TranscriptionJob

    controller = DictationController(
        config,
        status_callback=lambda *_a: None,
        result_callback=lambda *_a: None,
        notification_callback=lambda *_a: None,
    )
    cancelled = threading.Event()
    # SessionState неизменяем: сессию надо провести штатным переходом,
    # иначе воркер сочтёт задание устаревшим и молча выйдет.
    controller.state = controller.state.start()
    session_id = controller.state.session_id
    controller.state = controller.state.begin_transcription(session_id)
    controller._session_cancelled = cancelled
    job = _TranscriptionJob(
        session_id=session_id,
        audio=_speech(peak=0.02),
        target=None,
        config=config,
        cancelled=cancelled,
        display_only=True,
        released_at=0.0,
        audio_finalize_seconds=0.0,
        finalize_breakdown={},
        prearmed=False,
        vad_used=True,
        translating=False,
        translation_generation=0,
    )
    return controller, job


class _ExplodingEngine:
    """Движок, падающий именно на распознавании, а не на загрузке."""

    model_name = DEFAULT_MODEL

    def transcribe(self, *_args, **_kwargs):
        raise RuntimeError("onnxruntime died mid-inference")

    def close(self) -> None:
        pass


def test_gigaam_inference_crash_falls_back_to_whisper_instead_of_losing_speech(caplog):
    config = AppConfig(language="ru", russian_engine="gigaam", auto_insert=False)
    controller, job = _worker_controller(config)
    controller._gigaam = _ExplodingEngine()

    whisper_calls: list[dict] = []

    class _Whisper:
        model_size = config.model

        def transcribe(self, *_a, **kwargs):
            whisper_calls.append(kwargs)
            from pressay.transcriber import TranscriptionResult, TranscriptionTimings
            return TranscriptionResult(
                text="спасено виспером", language="ru", language_probability=0.9,
                segments=(), audio_duration_seconds=2.0,
                timings=TranscriptionTimings(0.0, 0.01, 0.01),
                device="cuda", compute_type="int8_float16")

        def close(self) -> None:
            pass

    controller._transcriber = _Whisper()
    try:
        with caplog.at_level("INFO", logger="pressay.controller"):
            controller._transcribe_worker(job)
    finally:
        controller.close()

    assert whisper_calls, "речь потеряна: Whisper даже не позвали"
    assert any("gigaam_inference_failed" in r.getMessage() for r in caplog.records)
    completed = [r for r in caplog.records if "transcription_completed" in r.getMessage()]
    assert completed and "engine=whisper" in completed[-1].getMessage()


def test_capture_level_is_logged_even_when_the_take_fails(caplog):
    config = AppConfig(language="ru", russian_engine="gigaam", auto_insert=False)
    controller, job = _worker_controller(config)

    class _AlwaysSilent:
        model_name = DEFAULT_MODEL

        def transcribe(self, *_a, **_k):
            raise NoSpeechDetected("тишина")

        def close(self) -> None:
            pass

    controller._gigaam = _AlwaysSilent()
    try:
        with caplog.at_level("INFO", logger="pressay.controller"):
            controller._transcribe_worker(job)
    finally:
        controller.close()

    levels = [r for r in caplog.records if "capture_level" in r.getMessage()]
    assert levels, "провалившаяся диктовка не оставила замера уровня"
    rendered = levels[-1].getMessage()
    for field in ("peak_dbfs=", "rms_dbfs=", "clipped=", "level="):
        assert field in rendered
    assert not any("transcription_completed" in r.getMessage() for r in caplog.records)


def test_settings_save_path_preserves_unknown_fields_structurally() -> None:
    """Страховка от возврата к перечислению полей в app.py.

    Тест на семантике replace() был бы тавтологией: он зелен и без правки в
    app.py. Поэтому проверяется сам вызов.
    """

    import inspect
    from pressay import app as app_module

    source = inspect.getsource(app_module)
    start = source.index("def save_settings")
    body = source[start:start + 3000]
    assert "replace(\n            config," in body, (
        "save_settings снова конструирует AppConfig поимённо — новые поля будут "
        "молча сброшены к значениям по умолчанию"
    )
