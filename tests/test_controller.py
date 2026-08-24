from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from pressay.audio import AudioStreamError, AudioTooShortError, SilentAudioError
from pressay.config import AppConfig
from pressay.controller import (
    DictationController,
    _prepare_insertion_text,
    _setup_command,
    _setup_recovery_instruction,
)
from pressay.platform_support import hotkey_hint
from pressay.transcriber import TranscriptionResult, TranscriptionTimings
from pressay.windows_input import ForegroundTarget, send_text as real_send_text


@pytest.fixture(autouse=True)
def _controller_uses_testable_windows_adapter(monkeypatch):
    import pressay.windows_input as windows_input

    monkeypatch.setattr("pressay.controller.input_adapter", lambda: windows_input)


@dataclass
class FakeRecording:
    audio: np.ndarray
    limit_reached: bool = False


@pytest.mark.parametrize(
    ("error", "expected_status", "notification_fragment"),
    (
        (
            AudioTooShortError("too short"),
            ("Запись слишком короткая", "warning"),
            "немного дольше",
        ),
        (
            SilentAudioError("silent"),
            ("Сигнал микрофона не обнаружен", "warning"),
            "уровень входа",
        ),
        (
            AudioStreamError(
                reason="portaudio_status",
                status_messages=("overflow",),
                captured_samples=100,
                source_rate=48_000,
            ),
            ("Ошибка аудиопотока", "error"),
            "Поток микрофона прерван",
        ),
    ),
)
def test_recording_failures_have_distinct_actionable_messages(
    error: Exception,
    expected_status: tuple[str, str],
    notification_fragment: str,
) -> None:
    assert DictationController._stop_failure_presentation(error) == expected_status
    assert notification_fragment in DictationController._stop_failure_notification(error)


def test_setup_command_is_native_to_each_platform(monkeypatch) -> None:
    monkeypatch.setattr("pressay.platform_support.sys.platform", "darwin")
    assert _setup_command("small") == "bash scripts/setup-macos.sh --model small"
    monkeypatch.setattr("pressay.platform_support.sys.platform", "win32")
    assert _setup_command("small") == ".\\scripts\\setup.ps1 -Model small"


def test_setup_recovery_requires_windows_app_to_exit(monkeypatch) -> None:
    monkeypatch.setattr("pressay.platform_support.sys.platform", "darwin")
    mac_instruction = _setup_recovery_instruction("small")
    assert mac_instruction == (
        "Запустите bash scripts/setup-macos.sh --model small и перезапустите Pressay."
    )

    monkeypatch.setattr("pressay.platform_support.sys.platform", "win32")
    windows_instruction = _setup_recovery_instruction("small")
    assert "Полностью выйдите из Pressay через меню в трее" in windows_instruction
    assert ".\\scripts\\setup.ps1 -Model small" in windows_instruction
    assert windows_instruction.endswith("и откройте Pressay снова.")


class FakeRecorder:
    def __init__(
        self,
        *,
        limit_reached: bool = False,
        max_duration_seconds: float = 300.0,
    ) -> None:
        self.is_recording = False
        self.limit_reached = limit_reached
        self.max_duration_seconds = max_duration_seconds

    def start(self) -> int:
        self.is_recording = True
        return 16_000

    def stop(self) -> FakeRecording:
        self.is_recording = False
        return FakeRecording(
            np.ones(8_000, dtype=np.float32) * 0.1,
            limit_reached=self.limit_reached,
        )

    def cancel(self) -> bool:
        was = self.is_recording
        self.is_recording = False
        return was


class PrearmedRecorder(FakeRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.prepared = threading.Event()
        self.activated = threading.Event()
        self.cancelled = threading.Event()

    def prepare_capture(self, _buffer_seconds: float) -> int:
        self.is_recording = True
        self.prepared.set()
        return 16_000

    def activate_prepared_capture(self) -> bool:
        self.activated.set()
        return True

    def cancel(self) -> bool:
        self.cancelled.set()
        return super().cancel()

    def stop(self) -> SimpleNamespace:
        self.is_recording = False
        return SimpleNamespace(
            audio=np.ones(8_000, dtype=np.float32) * 0.1,
            duration_seconds=0.5,
            limit_reached=False,
            finalize_breakdown={
                "stream_stop_seconds": 0.0,
                "assemble_seconds": 0.0,
                "first_frame_latency_seconds": 0.01,
            },
        )


class DurationRecorder(FakeRecorder):
    def __init__(self, duration_seconds: float) -> None:
        super().__init__()
        self.duration_seconds = duration_seconds

    def stop(self) -> SimpleNamespace:
        self.is_recording = False
        return SimpleNamespace(
            audio=np.ones(round(self.duration_seconds * 16_000), dtype=np.float32) * 0.1,
            duration_seconds=self.duration_seconds,
            limit_reached=False,
            finalize_breakdown={},
        )


def test_current_recording_rms_reads_only_the_active_recorder() -> None:
    controller = DictationController(
        AppConfig(),
        status_callback=lambda *_args: None,
        result_callback=lambda *_args: None,
        notification_callback=lambda *_args: None,
    )
    try:
        assert controller.current_recording_rms() == 0.0
        controller._recorder = SimpleNamespace(current_rms=0.025)
        assert controller.current_recording_rms() == pytest.approx(0.025)
        controller._recorder = None
        assert controller.current_recording_rms() == 0.0
    finally:
        controller.close()


class BlockingLimitRecorder(FakeRecorder):
    """Reaches the duration limit but blocks stop() until the test releases it.

    Used to interleave a cancellation between the native ``stop()`` call and
    the controller's post-stop lock check, so the truncation notification can
    be proven to skip a session that went stale in between.
    """

    def __init__(self) -> None:
        super().__init__(limit_reached=True, max_duration_seconds=300.0)
        self.started = threading.Event()
        self.release = threading.Event()

    def stop(self) -> FakeRecording:
        self.started.set()
        assert self.release.wait(timeout=2)
        return super().stop()


class FakeTranscriber:
    def __init__(self, model_size: str = "small") -> None:
        self.model_size = model_size
        self.options: list[dict[str, object]] = []
        self.closed = threading.Event()

    def transcribe(self, *_args, **_kwargs) -> TranscriptionResult:
        self.options.append(dict(_kwargs))
        return TranscriptionResult(
            text="тестовая фраза",
            language="ru",
            language_probability=0.99,
            segments=(),
            audio_duration_seconds=0.5,
            timings=TranscriptionTimings(0, 0.01, 0.01),
            device="cpu",
            compute_type="int8",
        )

    def warmup(self) -> tuple[str, str]:
        return "cpu", "int8"

    def close(self) -> None:
        self.closed.set()


class BlockingWarmupTranscriber(FakeTranscriber):
    def __init__(
        self,
        *,
        model_size: str = "small",
        error: Exception | None = None,
    ) -> None:
        super().__init__(model_size)
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.closed = threading.Event()
        self.error = error
        self.warmup_thread_id: int | None = None

    def warmup(self) -> tuple[str, str]:
        self.warmup_thread_id = threading.get_ident()
        self.started.set()
        if not self.release.wait(timeout=2):
            raise RuntimeError("test did not release blocked warmup")
        if self.error is not None:
            raise self.error
        self.finished.set()
        return super().warmup()

    def close(self) -> None:
        self.closed.set()


class BlockingTranscriber(FakeTranscriber):
    def __init__(
        self,
        *,
        model_size: str = "small",
        text: str = "исходный текст",
        error: Exception | None = None,
    ) -> None:
        super().__init__(model_size)
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.text = text
        self.error = error
        self.languages: list[str] = []
        self.close_thread_id: int | None = None

    def transcribe(self, *_args, **kwargs) -> TranscriptionResult:
        self.languages.append(str(kwargs["language"]))
        self.started.set()
        if not self.release.wait(timeout=2):
            raise RuntimeError("test did not release blocked inference")
        if self.error is not None:
            raise self.error
        result = super().transcribe()
        return TranscriptionResult(
            text=self.text,
            language=result.language,
            language_probability=result.language_probability,
            segments=result.segments,
            audio_duration_seconds=result.audio_duration_seconds,
            timings=result.timings,
            device=result.device,
            compute_type=result.compute_type,
        )

    def close(self) -> None:
        self.close_thread_id = threading.get_ident()
        self.closed.set()


class BlockingInputBackend:
    def __init__(self, target: ForegroundTarget) -> None:
        self.target = target
        self.snapshot_calls = 0
        self.before_injection = threading.Event()
        self.release = threading.Event()
        self.unicode_batches: list[tuple[int, ...]] = []
        self.ctrl_v_calls = 0
        self.enter_calls = 0

    def snapshot_foreground_target(self) -> ForegroundTarget:
        self.snapshot_calls += 1
        if self.snapshot_calls == 2:
            self.before_injection.set()
            assert self.release.wait(timeout=2)
        return self.target

    def is_physical_key_down(self, _vk_code: int) -> bool:
        return False

    def send_unicode_units(self, units: tuple[int, ...]) -> bool:
        self.unicode_batches.append(tuple(units))
        return True

    def send_ctrl_v(self) -> bool:
        self.ctrl_v_calls += 1
        return True

    def send_enter(self) -> bool:
        self.enter_calls += 1
        return True


def _auto_insert_controller(
    monkeypatch,
) -> tuple[
    DictationController,
    BlockingInputBackend,
    list[tuple[str, str]],
    list[tuple[object, ...]],
]:
    target = ForegroundTarget(
        hwnd=100,
        pid=200,
        title="Editor",
        focused_control=("win32_focus", 200, 101, "Edit"),
    )
    backend = BlockingInputBackend(target)
    statuses: list[tuple[str, str]] = []
    notifications: list[tuple[object, ...]] = []
    controller = DictationController(
        AppConfig(auto_insert=True),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=lambda *_args: None,
        notification_callback=lambda *args: notifications.append(args),
    )
    recorder = FakeRecorder()
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = FakeTranscriber(controller.config.model)  # type: ignore[assignment]

    def guarded_send(text, expected_target=None, **kwargs):
        kwargs.setdefault("fallback_to_clipboard", False)
        return real_send_text(
            text,
            expected_target,
            backend=backend,
            **kwargs,
        )

    monkeypatch.setattr("pressay.windows_input.send_text", guarded_send)
    assert controller.start_recording(target=target) is True
    assert controller.stop_recording() is True
    assert backend.before_injection.wait(timeout=2)
    return controller, backend, statuses, notifications


def test_smart_spacing_only_changes_automatic_insertion(monkeypatch) -> None:
    inserted: list[str] = []
    delivery_options: list[dict[str, object]] = []
    results: list[str] = []
    copied: list[str] = []
    controller = DictationController(
        AppConfig(auto_insert=True),
        status_callback=lambda *_args: None,
        result_callback=results.append,
        notification_callback=lambda *_args: None,
    )
    recorder = FakeRecorder()
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = FakeTranscriber(controller.config.model)  # type: ignore[assignment]
    monkeypatch.setattr(controller, "_copy_text", copied.append)

    def capture_insertion(text, **_kwargs):
        inserted.append(text)
        delivery_options.append(dict(_kwargs))
        return SimpleNamespace(success=True)

    monkeypatch.setattr("pressay.windows_input.send_text", capture_insertion)

    assert controller.start_recording(target="editor") is True
    assert controller.stop_recording() is True
    assert controller._future is not None
    controller._future.result(timeout=2)

    assert inserted == ["тестовая фраза "]
    assert delivery_options[0]["fallback_to_clipboard"] is False
    assert results == ["тестовая фраза"]
    assert controller.last_transcript == "тестовая фраза"
    assert copied == []
    controller.close()


def test_smart_spacing_skips_non_ordinary_text() -> None:
    assert _prepare_insertion_text(
        "обычная фраза", press_enter=False, smart_spacing=True
    ) == "обычная фраза "
    assert _prepare_insertion_text(
        "без настройки", press_enter=False, smart_spacing=False
    ) == "без настройки"
    assert _prepare_insertion_text(
        "уже есть\t", press_enter=False, smart_spacing=True
    ) == "уже есть\t"
    assert _prepare_insertion_text(
        "первая\nвторая", press_enter=False, smart_spacing=True
    ) == "первая\nвторая"
    assert _prepare_insertion_text(
        "", press_enter=True, smart_spacing=True
    ) == ""
    assert _prepare_insertion_text(
        "action payload", press_enter=True, smart_spacing=True
    ) == "action payload"


def test_active_job_uses_smart_spacing_snapshot(monkeypatch) -> None:
    inserted: list[str] = []
    controller = DictationController(
        AppConfig(model="small", auto_insert=True, smart_spacing=False),
        status_callback=lambda *_args: None,
        result_callback=lambda *_args: None,
        notification_callback=lambda *_args: None,
    )
    recorder = FakeRecorder()
    transcriber = BlockingTranscriber(text="снимок настройки")
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = transcriber  # type: ignore[assignment]

    def capture_insertion(text, **_kwargs):
        inserted.append(text)
        return SimpleNamespace(success=True)

    monkeypatch.setattr("pressay.windows_input.send_text", capture_insertion)

    assert controller.start_recording(target="editor") is True
    assert controller.stop_recording() is True
    assert transcriber.started.wait(timeout=2)
    controller.update_config(
        AppConfig(model="small", auto_insert=True, smart_spacing=True)
    )
    transcriber.release.set()
    assert controller._future is not None
    controller._future.result(timeout=2)

    assert inserted == ["снимок настройки"]
    assert controller.config.smart_spacing is True
    controller.close()


def test_controller_records_transcribes_and_keeps_last(monkeypatch) -> None:
    statuses: list[tuple[str, str]] = []
    results: list[str] = []
    controller = DictationController(
        AppConfig(auto_insert=False),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=results.append,
        notification_callback=lambda *_args: None,
    )
    recorder = FakeRecorder()
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = FakeTranscriber(controller.config.model)  # type: ignore[assignment]
    copied: list[str] = []
    monkeypatch.setattr(controller, "_copy_text", copied.append)

    assert controller.start_recording(target="window") is True
    assert controller.stop_recording() is True
    assert controller._future is not None
    controller._future.result(timeout=2)

    assert controller.last_transcript == "тестовая фраза"
    assert results == ["тестовая фраза"]
    assert copied == []
    assert any(state == "processing" for _, state in statuses)
    assert statuses[-1] == ("Готово — текст ниже", "success")
    controller.close()


@pytest.mark.parametrize(
    ("duration_seconds", "expected_vad_used"),
    [(0.5, False), (15.0, False), (15.001, True)],
)
def test_recording_duration_controls_vad_filter(
    duration_seconds: float, expected_vad_used: bool
) -> None:
    controller = DictationController(
        AppConfig(auto_insert=False),
        status_callback=lambda *_args: None,
        result_callback=lambda *_args: None,
        notification_callback=lambda *_args: None,
    )
    recorder = DurationRecorder(duration_seconds)
    transcriber = FakeTranscriber(controller.config.model)
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = transcriber  # type: ignore[assignment]

    assert controller.start_recording(target="window") is True
    assert controller.stop_recording() is True
    assert controller._future is not None
    controller._future.result(timeout=2)

    assert transcriber.options == [
        {"language": "auto", "vad_filter": expected_vad_used}
    ]
    controller.close()


def test_transcription_log_records_vad_usage(caplog) -> None:
    controller = DictationController(
        AppConfig(auto_insert=False),
        status_callback=lambda *_args: None,
        result_callback=lambda *_args: None,
        notification_callback=lambda *_args: None,
    )
    recorder = DurationRecorder(0.5)
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = FakeTranscriber(controller.config.model)  # type: ignore[assignment]
    caplog.set_level(logging.INFO, logger="pressay.controller")

    assert controller.start_recording(target="window") is True
    assert controller.stop_recording() is True
    assert controller._future is not None
    controller._future.result(timeout=2)

    assert any(
        record.message.startswith("transcription_completed")
        and "vad_used=False" in record.message
        for record in caplog.records
    )
    controller.close()


def test_pipeline_log_reports_full_delay_breakdown(monkeypatch, caplog) -> None:
    controller = DictationController(
        AppConfig(auto_insert=False),
        status_callback=lambda *_args: None,
        result_callback=lambda *_args: None,
        notification_callback=lambda *_args: None,
    )
    recorder = FakeRecorder()
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = FakeTranscriber(controller.config.model)  # type: ignore[assignment]
    caplog.set_level(logging.INFO, logger="pressay.controller")

    assert controller.start_recording(target="window") is True
    assert controller.stop_recording() is True
    assert controller._future is not None
    controller._future.result(timeout=2)

    pipeline_log = next(
        record.message
        for record in caplog.records
        if record.message.startswith("dictation_pipeline_completed")
    )
    for key in (
        "audio_finalize_seconds=",
        "post_release_seconds=",
        "stream_stop_seconds=",
        "assemble_seconds=",
        "postprocess_seconds=",
        "insertion_seconds=",
    ):
        assert key in pipeline_log
    controller.close()


def test_prepared_capture_is_reused_and_reported_in_pipeline_log(caplog) -> None:
    controller = DictationController(
        AppConfig(auto_insert=False),
        status_callback=lambda *_args: None,
        result_callback=lambda *_args: None,
        notification_callback=lambda *_args: None,
    )
    recorder = PrearmedRecorder()
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = FakeTranscriber(controller.config.model)  # type: ignore[assignment]
    caplog.set_level(logging.INFO, logger="pressay.controller")

    assert controller.prepare_capture() is True
    assert controller.start_recording(target="window") is True
    assert recorder.prepared.is_set()
    assert recorder.activated.is_set()
    assert controller.stop_recording() is True
    assert controller._future is not None
    controller._future.result(timeout=2)

    pipeline_log = next(
        record.message
        for record in caplog.records
        if record.message.startswith("dictation_pipeline_completed")
    )
    assert "first_frame_latency_seconds=0.010" in pipeline_log
    assert "prearmed=True" in pipeline_log
    controller.close()


def test_abandoned_prepared_capture_closes_then_allows_a_new_prepare_cycle() -> None:
    controller = DictationController(
        AppConfig(),
        status_callback=lambda *_args: None,
        result_callback=lambda *_args: None,
        notification_callback=lambda *_args: None,
    )
    first = PrearmedRecorder()
    second = PrearmedRecorder()
    recorders = iter((first, second))
    controller._new_recorder = lambda: next(recorders)  # type: ignore[method-assign]

    assert controller.prepare_capture() is True
    assert first.prepared.wait(timeout=2)
    assert controller.abandon_prepared_capture() is True
    assert first.cancelled.wait(timeout=2)
    assert controller.prepare_capture() is True
    assert second.prepared.wait(timeout=2)
    assert controller.abandon_prepared_capture() is True
    assert second.cancelled.wait(timeout=2)
    controller.close()


def test_personal_dictionary_biases_asr_and_canonicalizes_result(monkeypatch) -> None:
    results: list[str] = []
    controller = DictationController(
        AppConfig(
            auto_insert=False,
            replacements={
                "фаст апи": "FastAPI",
                "докер композ": "Docker Compose",
            },
        ),
        status_callback=lambda *_args: None,
        result_callback=results.append,
        notification_callback=lambda *_args: None,
    )
    recorder = FakeRecorder()
    transcriber = FakeTranscriber(controller.config.model)
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = transcriber  # type: ignore[assignment]

    assert controller.start_recording(target="editor") is True
    assert controller.stop_recording() is True
    assert controller._future is not None
    controller._future.result(timeout=2)

    assert transcriber.options == [
        {"language": "auto", "initial_prompt": "FastAPI, Docker Compose"}
    ]
    assert results == ["тестовая фраза"]
    controller.close()


def test_eco_resource_mode_retires_model_after_each_result() -> None:
    controller = DictationController(
        AppConfig(auto_insert=False, resource_mode="eco"),
        status_callback=lambda *_args: None,
        result_callback=lambda *_args: None,
        notification_callback=lambda *_args: None,
    )
    recorder = FakeRecorder()
    transcriber = FakeTranscriber(controller.config.model)
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = transcriber  # type: ignore[assignment]

    assert controller.start_recording(target="editor") is True
    assert controller.stop_recording() is True
    assert controller._future is not None
    controller._future.result(timeout=2)
    assert transcriber.closed.wait(timeout=2)
    assert controller._transcriber is None
    controller.close()


def test_missing_target_is_display_only_and_does_not_touch_clipboard(monkeypatch) -> None:
    statuses: list[tuple[str, str]] = []
    results: list[str] = []
    copied: list[str] = []
    controller = DictationController(
        AppConfig(auto_insert=True),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=results.append,
        notification_callback=lambda *_args: None,
    )
    recorder = FakeRecorder()
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = FakeTranscriber(controller.config.model)  # type: ignore[assignment]
    monkeypatch.setattr(controller, "_copy_text", copied.append)

    assert controller.start_recording(target=None) is True
    assert controller.stop_recording() is True
    assert controller._future is not None
    controller._future.result(timeout=2)

    assert controller.last_transcript == "тестовая фраза"
    assert results == ["тестовая фраза"]
    assert copied == []
    assert statuses[-1] == ("Готово — текст ниже", "success")
    controller.close()


def test_update_config_is_serialized_and_active_job_uses_snapshot(monkeypatch) -> None:
    statuses: list[tuple[str, str]] = []
    results: list[str] = []
    copied: list[str] = []
    controller = DictationController(
        AppConfig(
            model="small",
            language="ru",
            auto_insert=False,
            replacements={"исходный": "старый"},
        ),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=results.append,
        notification_callback=lambda *_args: None,
    )
    recorder = FakeRecorder()
    transcriber = BlockingTranscriber()
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = transcriber  # type: ignore[assignment]
    monkeypatch.setattr(controller, "_copy_text", copied.append)

    assert controller.start_recording(target="old-window") is True
    assert controller.stop_recording() is True
    assert transcriber.started.wait(timeout=2)

    caller_thread_id = threading.get_ident()
    controller.update_config(
        AppConfig(
            model="medium",
            language="en",
            auto_insert=False,
            replacements={"исходный": "новый"},
        )
    )
    assert not transcriber.closed.is_set()

    transcriber.release.set()
    assert controller._future is not None
    controller._future.result(timeout=2)
    assert transcriber.closed.wait(timeout=2)

    assert transcriber.languages == ["ru"]
    assert results == ["старый текст"]
    assert copied == []
    assert statuses[-1][1] == "success"
    assert transcriber.close_thread_id != caller_thread_id
    controller.close()


def test_close_invalidates_blocked_result_and_disposes_after_worker(monkeypatch) -> None:
    statuses: list[tuple[str, str]] = []
    results: list[str] = []
    notifications: list[tuple[object, ...]] = []
    copied: list[str] = []
    controller = DictationController(
        AppConfig(auto_insert=False),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=results.append,
        notification_callback=lambda *args: notifications.append(args),
    )
    recorder = FakeRecorder()
    transcriber = BlockingTranscriber(
        model_size=controller.config.model,
        text="секрет после закрытия",
    )
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = transcriber  # type: ignore[assignment]
    monkeypatch.setattr(controller, "_copy_text", copied.append)

    assert controller.start_recording() is True
    assert controller.stop_recording() is True
    assert transcriber.started.wait(timeout=2)
    callbacks_before_close = (list(statuses), list(results), list(notifications), list(copied))

    controller.close()
    assert not transcriber.closed.is_set()
    assert controller.last_transcript == ""
    assert controller.copy_last() is False
    assert controller.paste_last() is False

    transcriber.release.set()
    assert controller._future is not None
    controller._future.result(timeout=2)
    assert transcriber.closed.wait(timeout=2)

    assert (statuses, results, notifications, copied) == callbacks_before_close
    assert controller.last_transcript == ""


def test_auto_insert_disabled_never_mutates_clipboard(monkeypatch) -> None:
    statuses: list[tuple[str, str]] = []
    results: list[str] = []
    copied: list[str] = []
    controller = DictationController(
        AppConfig(auto_insert=False),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=results.append,
        notification_callback=lambda *_args: None,
    )
    recorder = FakeRecorder()
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = FakeTranscriber(controller.config.model)  # type: ignore[assignment]

    monkeypatch.setattr(controller, "_copy_text", copied.append)
    assert controller.start_recording(target="window") is True
    assert controller.stop_recording() is True
    assert controller._future is not None
    controller._future.result(timeout=2)

    assert results == ["тестовая фраза"]
    assert copied == []
    assert statuses[-1] == ("Готово — текст ниже", "success")
    assert controller.last_transcript == "тестовая фраза"
    controller.close()


def test_auto_insert_failure_keeps_result_without_copying(monkeypatch) -> None:
    statuses: list[tuple[str, str]] = []
    notifications: list[tuple[object, ...]] = []
    copied: list[str] = []
    controller = DictationController(
        AppConfig(auto_insert=True),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=lambda *_args: None,
        notification_callback=lambda *args: notifications.append(args),
    )
    recorder = FakeRecorder()
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = FakeTranscriber(controller.config.model)  # type: ignore[assignment]
    monkeypatch.setattr(controller, "_copy_text", copied.append)
    monkeypatch.setattr(
        "pressay.windows_input.send_text",
        lambda *_args, **_kwargs: SimpleNamespace(
            success=False,
            reason="foreground_target_changed",
        ),
    )

    assert controller.start_recording(target="editor") is True
    assert controller.stop_recording() is True
    assert controller._future is not None
    controller._future.result(timeout=2)

    assert copied == []
    assert controller.last_transcript == "тестовая фраза"
    assert statuses[-1] == ("Не вставлено: сменилось активное окно", "warning")
    assert notifications and notifications[-1][1] == "foreground_target_changed"
    controller.close()


def test_auto_insert_exception_keeps_result_without_copying(monkeypatch) -> None:
    statuses: list[tuple[str, str]] = []
    notifications: list[tuple[object, ...]] = []
    copied: list[str] = []
    controller = DictationController(
        AppConfig(auto_insert=True),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=lambda *_args: None,
        notification_callback=lambda *args: notifications.append(args),
    )
    recorder = FakeRecorder()
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = FakeTranscriber(controller.config.model)  # type: ignore[assignment]
    monkeypatch.setattr(controller, "_copy_text", copied.append)

    def fail_delivery(*_args, **_kwargs):
        raise RuntimeError("simulated input failure")

    monkeypatch.setattr("pressay.windows_input.send_text", fail_delivery)

    assert controller.start_recording(target="editor") is True
    assert controller.stop_recording() is True
    assert controller._future is not None
    controller._future.result(timeout=2)

    assert copied == []
    assert controller.last_transcript == "тестовая фраза"
    assert statuses[-1] == ("Не вставлено — текст сохранён ниже", "warning")
    assert notifications and hotkey_hint("copy") in str(notifications[-1][1])
    controller.close()


def _recovery_controller() -> tuple[
    DictationController,
    list[tuple[str, str]],
    list[tuple[object, ...]],
]:
    statuses: list[tuple[str, str]] = []
    notifications: list[tuple[object, ...]] = []
    holder: dict[str, DictationController] = {}

    def assert_callback_outside_lock() -> None:
        controller = holder["controller"]
        assert not controller._lock._is_owned()  # type: ignore[attr-defined]

    def status_callback(text: str, state: str) -> None:
        assert_callback_outside_lock()
        statuses.append((text, state))

    def notification_callback(*args: object) -> None:
        assert_callback_outside_lock()
        notifications.append(args)

    controller = DictationController(
        AppConfig(),
        status_callback=status_callback,
        result_callback=lambda *_args: None,
        notification_callback=notification_callback,
    )
    holder["controller"] = controller
    controller.last_transcript = "текст для восстановления"
    return controller, statuses, notifications


def test_copy_last_reports_real_success_outside_lock(monkeypatch) -> None:
    controller, statuses, notifications = _recovery_controller()
    copied: list[str] = []

    def copy_text(text: str) -> SimpleNamespace:
        copied.append(text)
        return SimpleNamespace(success=True)

    monkeypatch.setattr(
        "pressay.controller.input_adapter",
        lambda: SimpleNamespace(copy_text=copy_text),
    )
    try:
        assert controller.copy_last() is True
        assert copied == ["текст для восстановления"]
        assert statuses == [("Скопировано", "success")]
        assert notifications == []
        assert controller.last_transcript == "текст для восстановления"
    finally:
        controller.close()


def test_copy_last_reports_unsuccessful_outcome_without_losing_text(monkeypatch) -> None:
    controller, statuses, notifications = _recovery_controller()
    monkeypatch.setattr(
        "pressay.controller.input_adapter",
        lambda: SimpleNamespace(
            copy_text=lambda _text: SimpleNamespace(
                success=False,
                reason="clipboard_write_failed",
            )
        ),
    )
    try:
        assert controller.copy_last() is False
        assert statuses == [("Не скопировано — текст сохранён ниже", "warning")]
        assert notifications == [
            (
                "Pressay",
                "Не удалось скопировать последнюю расшифровку. "
                "Текст сохранён в окне Pressay.",
                True,
            )
        ]
        assert controller.last_transcript == "текст для восстановления"
    finally:
        controller.close()


def test_copy_last_reports_exception_without_losing_text(monkeypatch) -> None:
    controller, statuses, notifications = _recovery_controller()

    def fail_copy(_text: str) -> None:
        raise RuntimeError("simulated clipboard failure")

    monkeypatch.setattr(
        "pressay.controller.input_adapter",
        lambda: SimpleNamespace(copy_text=fail_copy),
    )
    try:
        assert controller.copy_last() is False
        assert statuses == [("Не скопировано — текст сохранён ниже", "warning")]
        assert len(notifications) == 1
        assert notifications[0][0] == "Pressay"
        assert notifications[0][2] is True
        assert controller.last_transcript == "текст для восстановления"
    finally:
        controller.close()


def test_paste_last_reports_success_outside_lock_without_copying(monkeypatch) -> None:
    controller, statuses, notifications = _recovery_controller()
    pasted: list[str] = []

    def paste_last(text: str, **_kwargs: object) -> SimpleNamespace:
        pasted.append(text)
        return SimpleNamespace(success=True, copied=False)

    monkeypatch.setattr(
        "pressay.controller.input_adapter",
        lambda: SimpleNamespace(
            paste_last=paste_last,
            copy_text=lambda _text: pytest.fail("paste success must not copy"),
        ),
    )
    try:
        assert controller.paste_last() is True
        assert pasted == ["текст для восстановления"]
        assert statuses == [("Вставлено", "success")]
        assert notifications == []
        assert controller.last_transcript == "текст для восстановления"
    finally:
        controller.close()


def test_paste_last_reports_when_backend_copied_but_did_not_insert(monkeypatch) -> None:
    controller, statuses, notifications = _recovery_controller()
    copy_calls: list[str] = []
    monkeypatch.setattr(
        "pressay.controller.input_adapter",
        lambda: SimpleNamespace(
            paste_last=lambda _text, **_kwargs: SimpleNamespace(
                success=False,
                copied=True,
                reason="physical_modifiers_not_released",
            ),
            copy_text=copy_calls.append,
        ),
    )
    try:
        assert controller.paste_last() is False
        assert copy_calls == []
        assert statuses == [("Не вставлено — текст скопирован", "warning")]
        assert notifications == [
            (
                "Pressay",
                "Не удалось вставить последнюю расшифровку, но текст "
                "скопирован в буфер обмена. Вставьте его вручную.",
                True,
            )
        ]
        assert controller.last_transcript == "текст для восстановления"
    finally:
        controller.close()


def test_paste_last_reports_failure_without_implicit_copy(monkeypatch) -> None:
    controller, statuses, notifications = _recovery_controller()
    copy_calls: list[str] = []
    monkeypatch.setattr(
        "pressay.controller.input_adapter",
        lambda: SimpleNamespace(
            paste_last=lambda _text, **_kwargs: SimpleNamespace(
                success=False,
                copied=False,
                reason="target_mismatch",
            ),
            copy_text=copy_calls.append,
        ),
    )
    try:
        assert controller.paste_last() is False
        assert copy_calls == []
        assert statuses == [("Не вставлено: сменилось активное окно", "warning")]
        assert len(notifications) == 1
        assert hotkey_hint("copy") in str(notifications[0][1])
        assert controller.last_transcript == "текст для восстановления"
    finally:
        controller.close()


@pytest.mark.parametrize("action_name", ["copy_last", "paste_last"])
def test_recovery_action_close_while_backend_blocks_suppresses_late_callbacks(
    monkeypatch,
    action_name: str,
) -> None:
    controller, statuses, notifications = _recovery_controller()
    entered = threading.Event()
    release = threading.Event()
    results: list[bool] = []

    def blocked_failure(*_args: object, **_kwargs: object) -> SimpleNamespace:
        entered.set()
        assert release.wait(timeout=2)
        return SimpleNamespace(success=False, copied=False, reason="input_failed")

    monkeypatch.setattr(
        "pressay.controller.input_adapter",
        lambda: SimpleNamespace(
            copy_text=blocked_failure,
            paste_last=blocked_failure,
        ),
    )
    worker = threading.Thread(
        target=lambda: results.append(bool(getattr(controller, action_name)())),
        daemon=True,
    )
    worker.start()
    assert entered.wait(timeout=1)

    controller.close()
    release.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert results == [False]
    assert statuses == []
    assert notifications == []
    assert controller.wait_closed(2)


def test_paste_last_exception_never_falls_back_to_implicit_copy(monkeypatch) -> None:
    statuses: list[tuple[str, str]] = []
    notifications: list[tuple[object, ...]] = []
    copied: list[str] = []
    controller = DictationController(
        AppConfig(),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=lambda *_args: None,
        notification_callback=lambda *args: notifications.append(args),
    )
    controller.last_transcript = "секретный текст"
    monkeypatch.setattr(controller, "_copy_text", copied.append)

    def fail_paste(*_args, **_kwargs):
        raise RuntimeError("simulated OLE transaction failure")

    monkeypatch.setattr("pressay.windows_input.paste_last", fail_paste)

    assert controller.paste_last() is False
    assert copied == []
    assert controller.last_transcript == "секретный текст"
    assert statuses[-1] == ("Не вставлено — текст сохранён ниже", "warning")
    assert notifications and hotkey_hint("copy") in str(notifications[-1][1])
    controller.close()


def test_close_during_input_guard_prevents_injection(monkeypatch) -> None:
    controller, backend, statuses, notifications = _auto_insert_controller(monkeypatch)
    callbacks_before_close = (list(statuses), list(notifications))

    controller.close()
    backend.release.set()
    assert controller._future is not None
    controller._future.result(timeout=2)

    assert backend.unicode_batches == []
    assert backend.ctrl_v_calls == 0
    assert backend.enter_calls == 0
    assert (statuses, notifications) == callbacks_before_close


def test_cancel_completed_delivery_prevents_injection_and_late_callbacks(monkeypatch) -> None:
    controller, backend, statuses, notifications = _auto_insert_controller(monkeypatch)

    assert controller.cancel() is True
    callbacks_after_cancel = (list(statuses), list(notifications))
    backend.release.set()
    assert controller._future is not None
    controller._future.result(timeout=2)

    assert backend.unicode_batches == []
    assert backend.ctrl_v_calls == 0
    assert backend.enter_calls == 0
    assert (statuses, notifications) == callbacks_after_cancel
    controller.close()


def test_cancel_suppresses_late_worker_error() -> None:
    statuses: list[tuple[str, str]] = []
    notifications: list[tuple[object, ...]] = []
    controller = DictationController(
        AppConfig(),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=lambda *_args: None,
        notification_callback=lambda *args: notifications.append(args),
    )
    recorder = FakeRecorder()
    transcriber = BlockingTranscriber(
        model_size=controller.config.model,
        error=RuntimeError("late failure"),
    )
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = transcriber  # type: ignore[assignment]

    assert controller.start_recording() is True
    assert controller.stop_recording() is True
    assert transcriber.started.wait(timeout=2)
    assert controller.cancel() is True
    callbacks_after_cancel = (list(statuses), list(notifications))

    transcriber.release.set()
    assert controller._future is not None
    controller._future.result(timeout=2)

    assert (statuses, notifications) == callbacks_after_cancel
    controller.close()


def test_cancel_discards_active_recording() -> None:
    controller = DictationController(
        AppConfig(),
        status_callback=lambda *_args: None,
        result_callback=lambda *_args: None,
        notification_callback=lambda *_args: None,
    )
    recorder = FakeRecorder()
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]

    assert controller.start_recording() is True
    assert controller.cancel() is True
    assert recorder.is_recording is False
    assert controller.state.active is False
    controller.close()


def test_model_warmup_is_async_and_reports_preparing_then_ready(monkeypatch) -> None:
    statuses: list[tuple[str, str]] = []
    controller = DictationController(
        AppConfig(model="small"),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=lambda *_args: None,
        notification_callback=lambda *_args: None,
    )
    transcriber = BlockingWarmupTranscriber(model_size="small")
    monkeypatch.setattr(controller, "_new_transcriber", lambda _model: transcriber)

    caller_thread_id = threading.get_ident()
    assert controller.warmup_model() is True
    assert transcriber.started.wait(timeout=2)
    assert statuses == [("Готовлю модель small…", "processing")]
    assert transcriber.warmup_thread_id != caller_thread_id

    transcriber.release.set()
    assert controller._warmup_future is not None
    controller._warmup_future.result(timeout=2)
    assert statuses[-1] == (f"Готов — удерживайте {hotkey_hint('hold')}", "ready")
    controller.close()
    assert controller.wait_closed(2)


def test_recording_can_start_while_warmup_blocks_and_transcription_queues(
    monkeypatch,
) -> None:
    results: list[str] = []
    controller = DictationController(
        AppConfig(model="small", auto_insert=False),
        status_callback=lambda *_args: None,
        result_callback=results.append,
        notification_callback=lambda *_args: None,
    )
    transcriber = BlockingWarmupTranscriber(model_size="small")
    recorder = FakeRecorder()
    monkeypatch.setattr(controller, "_new_transcriber", lambda _model: transcriber)
    monkeypatch.setattr(controller, "_new_recorder", lambda: recorder)
    monkeypatch.setattr(controller, "_copy_text", lambda _text: None)

    assert controller.warmup_model() is True
    assert transcriber.started.wait(timeout=2)
    assert controller.start_recording(target="editor") is True
    assert controller.stop_recording() is True
    assert controller._future is not None
    assert controller._future.done() is False

    transcriber.release.set()
    controller._future.result(timeout=2)
    assert results == ["тестовая фраза"]
    controller.close()
    assert controller.wait_closed(2)


def test_model_change_invalidates_old_warmup_and_preloads_new_model(monkeypatch) -> None:
    statuses: list[tuple[str, str]] = []
    old_model = BlockingWarmupTranscriber(model_size="small")
    new_model = BlockingWarmupTranscriber(model_size="medium")
    created: list[str] = []
    controller = DictationController(
        AppConfig(model="small"),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=lambda *_args: None,
        notification_callback=lambda *_args: None,
    )

    def factory(model_size: str) -> BlockingWarmupTranscriber:
        created.append(model_size)
        return old_model if model_size == "small" else new_model

    monkeypatch.setattr(controller, "_new_transcriber", factory)
    assert controller.warmup_model() is True
    assert old_model.started.wait(timeout=2)

    controller.update_config(AppConfig(model="medium"))
    old_model.release.set()
    assert new_model.started.wait(timeout=2)
    assert old_model.closed.wait(timeout=2)
    new_model.release.set()
    assert controller._warmup_future is not None
    controller._warmup_future.result(timeout=2)

    assert created == ["small", "medium"]
    assert [state for _, state in statuses].count("ready") == 1
    controller.close()
    assert controller.wait_closed(2)


def test_close_during_blocked_warmup_suppresses_late_callbacks(monkeypatch) -> None:
    statuses: list[tuple[str, str]] = []
    notifications: list[tuple[object, ...]] = []
    controller = DictationController(
        AppConfig(model="small"),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=lambda *_args: None,
        notification_callback=lambda *args: notifications.append(args),
    )
    transcriber = BlockingWarmupTranscriber(model_size="small")
    monkeypatch.setattr(controller, "_new_transcriber", lambda _model: transcriber)

    assert controller.warmup_model() is True
    assert transcriber.started.wait(timeout=2)
    callbacks_before_close = (list(statuses), list(notifications))
    controller.close()
    assert controller.wait_closed(0.01) is False

    transcriber.release.set()
    assert controller.wait_closed(2)
    assert transcriber.closed.is_set()
    assert (statuses, notifications) == callbacks_before_close


def test_cancel_recording_suppresses_late_warmup_ready(monkeypatch) -> None:
    statuses: list[tuple[str, str]] = []
    controller = DictationController(
        AppConfig(model="small"),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=lambda *_args: None,
        notification_callback=lambda *_args: None,
    )
    transcriber = BlockingWarmupTranscriber(model_size="small")
    recorder = FakeRecorder()
    monkeypatch.setattr(controller, "_new_transcriber", lambda _model: transcriber)
    monkeypatch.setattr(controller, "_new_recorder", lambda: recorder)

    assert controller.warmup_model() is True
    assert transcriber.started.wait(timeout=2)
    assert controller.start_recording() is True
    assert controller.cancel() is True
    callbacks_after_cancel = list(statuses)

    transcriber.release.set()
    assert controller._warmup_future is not None
    controller._warmup_future.result(timeout=2)
    assert statuses == callbacks_after_cancel
    controller.close()
    assert controller.wait_closed(2)


def test_duration_limit_reached_still_transcribes_and_warns_once() -> None:
    statuses: list[tuple[str, str]] = []
    notifications: list[tuple[object, ...]] = []
    results: list[str] = []
    controller = DictationController(
        AppConfig(auto_insert=False),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=results.append,
        notification_callback=lambda *args: notifications.append(args),
    )
    recorder = FakeRecorder(limit_reached=True, max_duration_seconds=300.0)
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = FakeTranscriber(controller.config.model)  # type: ignore[assignment]

    assert controller.start_recording(target="window") is True
    assert controller.stop_recording() is True
    assert controller._future is not None
    controller._future.result(timeout=2)

    # Recognition still ran on the truncated buffer.
    assert results == ["тестовая фраза"]
    assert notifications == [
        (
            "Pressay",
            "Достигнут предел записи 5 мин — распознаётся только записанная часть.",
            True,
        )
    ]
    controller.close()


def test_duration_limit_notification_suppressed_for_stale_session() -> None:
    statuses: list[tuple[str, str]] = []
    notifications: list[tuple[object, ...]] = []
    controller = DictationController(
        AppConfig(auto_insert=False),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=lambda *_args: None,
        notification_callback=lambda *args: notifications.append(args),
    )
    recorder = BlockingLimitRecorder()
    controller._new_recorder = lambda: recorder  # type: ignore[method-assign]
    controller._transcriber = FakeTranscriber(controller.config.model)  # type: ignore[assignment]

    assert controller.start_recording(target="window") is True
    stop_command = controller._request_stop_recording()
    assert stop_command is not None
    assert recorder.started.wait(timeout=2)

    # Cancel while the native stop() is still in flight: this bumps the
    # capture generation the queued stop command was issued with.
    accepted, cancel_command, _session_id = controller._request_cancel()
    assert accepted is True

    recorder.release.set()
    assert stop_command.completion is not None
    assert stop_command.completion.wait(timeout=2)
    if cancel_command is not None:
        assert cancel_command.completion is not None
        assert cancel_command.completion.wait(timeout=2)

    # The stop command is stale by the time recorder.stop() returns: no
    # notification, and recognition never got submitted.
    assert notifications == []
    assert controller._future is None
    controller.close()


def test_missing_local_model_reports_actionable_warmup_error(monkeypatch) -> None:
    statuses: list[tuple[str, str]] = []
    notifications: list[tuple[object, ...]] = []
    controller = DictationController(
        AppConfig(model="large-v3"),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=lambda *_args: None,
        notification_callback=lambda *args: notifications.append(args),
    )
    transcriber = BlockingWarmupTranscriber(
        model_size="large-v3",
        error=RuntimeError("model files are absent"),
    )
    monkeypatch.setattr(controller, "_new_transcriber", lambda _model: transcriber)

    assert controller.warmup_model() is True
    assert transcriber.started.wait(timeout=2)
    transcriber.release.set()
    assert controller._warmup_future is not None
    controller._warmup_future.result(timeout=2)

    assert statuses[-1][1] == "error"
    assert _setup_command("large-v3") in statuses[-1][0]
    assert notifications
    assert _setup_command("large-v3") in str(notifications[-1][1])
    controller.close()


@pytest.mark.parametrize("language_choice", ["posterior", "dual", "forced"])
def test_transcription_log_records_language_choice(caplog, language_choice: str) -> None:
    class LanguageChoiceTranscriber(FakeTranscriber):
        def transcribe(self, *_args, **_kwargs) -> TranscriptionResult:
            result = super().transcribe(*_args, **_kwargs)
            return TranscriptionResult(
                text=result.text,
                language=result.language,
                language_probability=result.language_probability,
                segments=result.segments,
                audio_duration_seconds=result.audio_duration_seconds,
                timings=result.timings,
                device=result.device,
                compute_type=result.compute_type,
                language_choice=language_choice,
            )

    controller = DictationController(
        AppConfig(auto_insert=False),
        status_callback=lambda *_args: None,
        result_callback=lambda *_args: None,
        notification_callback=lambda *_args: None,
    )
    controller._new_recorder = lambda: DurationRecorder(0.5)  # type: ignore[method-assign]
    controller._transcriber = LanguageChoiceTranscriber(  # type: ignore[assignment]
        controller.config.model
    )
    caplog.set_level(logging.INFO, logger="pressay.controller")

    assert controller.start_recording(target="window") is True
    assert controller.stop_recording() is True
    assert controller._future is not None
    controller._future.result(timeout=2)

    assert any(
        record.message.startswith("transcription_completed")
        and f"language_choice={language_choice}" in record.message
        for record in caplog.records
    )
    controller.close()
    assert controller.wait_closed(2)
