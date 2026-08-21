from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from types import SimpleNamespace
from typing import Callable

import numpy as np
import pytest

import pressay.controller as controller_module
from pressay.config import AppConfig
from pressay.controller import DictationController
from pressay.transcriber import ModelLoadError, TranscriptionResult, TranscriptionTimings


@dataclass
class _Recording:
    audio: np.ndarray
    duration_seconds: float = 0.5
    limit_reached: bool = False
    finalize_breakdown: dict[str, float] | None = None


class _Recorder:
    max_duration_seconds = 300.0

    def start(self) -> int:
        return 16_000

    def stop(self) -> _Recording:
        return _Recording(
            np.ones(8_000, dtype=np.float32) * 0.1,
            finalize_breakdown={},
        )

    def cancel(self) -> bool:
        return True


class _FakeTranscriber:
    def __init__(self, model_size: str, responses: list[str] | None = None) -> None:
        self.model_size = model_size
        self.responses = list(responses or ["обычный текст"])
        self.tasks: list[str] = []
        self.options: list[dict[str, object]] = []
        self.closed = threading.Event()
        self.warmups = 0
        self.progress_callback: Callable[[int], None] | None = None

    @property
    def is_loaded(self) -> bool:
        return self.warmups > 0

    def set_download_progress_callback(
        self, callback: Callable[[int], None] | None
    ) -> None:
        self.progress_callback = callback

    def warmup(self) -> tuple[str, str]:
        self.warmups += 1
        return "cpu", "int8"

    def transcribe(
        self,
        *_args: object,
        task: str = "transcribe",
        **kwargs: object,
    ) -> TranscriptionResult:
        self.tasks.append(task)
        self.options.append(dict(kwargs))
        text = self.responses.pop(0)
        return TranscriptionResult(
            text=text,
            language="ru",
            language_probability=0.99,
            segments=(),
            audio_duration_seconds=0.5,
            timings=TranscriptionTimings(0.0, 0.01, 0.01),
            device="cpu",
            compute_type="int8",
        )

    def close(self) -> None:
        self.closed.set()


class _FailingWarmupTranscriber(_FakeTranscriber):
    def warmup(self) -> tuple[str, str]:
        self.warmups += 1
        raise RuntimeError("translation model unavailable")


class _BlockingWarmupTranscriber(_FakeTranscriber):
    def __init__(self, model_size: str) -> None:
        super().__init__(model_size)
        self.started = threading.Event()
        self.release = threading.Event()

    @property
    def is_loaded(self) -> bool:
        return False

    def warmup(self) -> tuple[str, str]:
        self.started.set()
        assert self.release.wait(timeout=2)
        self.warmups += 1
        return "cpu", "int8"


class _BlockingResponseTranscriber(_FakeTranscriber):
    def __init__(self, model_size: str, response: str) -> None:
        super().__init__(model_size, [response])
        self.started = threading.Event()
        self.release = threading.Event()

    def transcribe(
        self,
        *_args: object,
        task: str = "transcribe",
        **kwargs: object,
    ) -> TranscriptionResult:
        self.started.set()
        assert self.release.wait(timeout=2)
        return super().transcribe(task=task, **kwargs)


class _BlockingLoadFailureTranscriber(_FakeTranscriber):
    def __init__(self, model_size: str) -> None:
        super().__init__(model_size)
        self.started = threading.Event()
        self.release = threading.Event()
        self.warmups = 1

    def transcribe(self, *_args: object, **_kwargs: object) -> TranscriptionResult:
        self.started.set()
        assert self.release.wait(timeout=2)
        raise ModelLoadError("translation model unavailable")


def _controller(
    config: AppConfig,
    *,
    statuses: list[tuple[str, str]] | None = None,
    results: list[str] | None = None,
    notifications: list[tuple[str, str, bool]] | None = None,
    model_ready: list[tuple[str, str, str]] | None = None,
    callback_lock_check: bool = False,
) -> DictationController:
    status_items = statuses if statuses is not None else []
    result_items = results if results is not None else []
    notification_items = notifications if notifications is not None else []
    model_ready_items = model_ready if model_ready is not None else []
    holder: dict[str, DictationController] = {}

    def check_lock() -> None:
        if callback_lock_check and "controller" in holder:
            assert not holder["controller"]._lock._is_owned()

    def status_callback(text: str, state: str) -> None:
        check_lock()
        status_items.append((text, state))

    def result_callback(text: str) -> None:
        check_lock()
        result_items.append(text)

    def notification_callback(title: str, text: str, warning: bool) -> None:
        check_lock()
        notification_items.append((title, text, warning))

    def model_ready_callback(model: str, device: str, compute_type: str) -> None:
        check_lock()
        model_ready_items.append((model, device, compute_type))

    controller = DictationController(
        config,
        status_callback=status_callback,
        result_callback=result_callback,
        notification_callback=notification_callback,
        model_ready_callback=model_ready_callback,
    )
    holder["controller"] = controller
    controller._new_recorder = _Recorder  # type: ignore[method-assign]
    return controller


def _dictate(controller: DictationController) -> None:
    assert controller.start_recording(target="editor") is True
    assert controller.stop_recording() is True
    assert controller._future is not None
    controller._future.result(timeout=2)


def test_turbo_translation_uses_separate_slot_and_voice_command_turns_it_off() -> None:
    results: list[str] = []
    notifications: list[tuple[str, str, bool]] = []
    controller = _controller(
        AppConfig(model="turbo", voice_translate=True, auto_insert=False),
        results=results,
        notifications=notifications,
    )
    primary = _FakeTranscriber("turbo", ["переведи на английский"])
    translator = _FakeTranscriber("large-v3", ["stop translating"])
    controller._transcriber = primary  # type: ignore[assignment]
    controller._new_transcriber = lambda model: translator  # type: ignore[method-assign]
    try:
        _dictate(controller)
        assert controller.translating is True
        assert controller._translator_warmup_future is not None
        controller._translator_warmup_future.result(timeout=2)

        _dictate(controller)

        assert controller.translating is False
        assert primary.tasks == ["transcribe"]
        assert translator.tasks == ["translate"]
        assert primary.closed.is_set() is False
        assert results == []
        assert [item[2] for item in notifications] == [False, False]
    finally:
        controller.close()
        assert controller.wait_closed(2)


def test_translation_capable_primary_model_is_reused_without_second_slot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    results: list[str] = []
    controller = _controller(
        AppConfig(model="large-v3", voice_translate=True, auto_insert=False),
        results=results,
    )
    primary = _FakeTranscriber(
        "large-v3",
        ["translate to english", "Today we prepare the report."],
    )
    controller._transcriber = primary  # type: ignore[assignment]
    created: list[str] = []
    controller._new_transcriber = lambda model: created.append(model)  # type: ignore[method-assign]
    try:
        _dictate(controller)
        with caplog.at_level(logging.INFO, logger="pressay.controller"):
            _dictate(controller)

        assert controller.translating is True
        assert controller._translator is None
        assert created == []
        assert primary.tasks == ["transcribe", "translate"]
        assert primary.options[-1]["vad_filter"] is False
        assert results == ["Today we prepare the report."]
        assert "task=translate" in caplog.text
    finally:
        controller.close()
        assert controller.wait_closed(2)


def test_translation_warmup_failure_disables_mode_and_dictation_continues() -> None:
    results: list[str] = []
    notifications: list[tuple[str, str, bool]] = []
    controller = _controller(
        AppConfig(model="turbo", voice_translate=True, auto_insert=False),
        results=results,
        notifications=notifications,
    )
    primary = _FakeTranscriber(
        "turbo",
        ["переведи на английский", "обычная диктовка"],
    )
    translator = _FailingWarmupTranscriber("large-v3")
    controller._transcriber = primary  # type: ignore[assignment]
    controller._new_transcriber = lambda _model: translator  # type: ignore[method-assign]
    try:
        _dictate(controller)
        assert controller._translator_warmup_future is not None
        controller._translator_warmup_future.result(timeout=2)

        assert controller.translating is False
        assert translator.closed.is_set()
        assert notifications[-1][2] is True
        assert "обычная диктовка продолжит работать" in notifications[-1][1]

        _dictate(controller)

        assert primary.tasks == ["transcribe", "transcribe"]
        assert results == ["обычная диктовка"]
    finally:
        controller.close()
        assert controller.wait_closed(2)


def test_translation_warmup_does_not_report_translator_as_primary_model() -> None:
    model_ready: list[tuple[str, str, str]] = []
    controller = _controller(
        AppConfig(model="turbo", voice_translate=True, auto_insert=False),
        model_ready=model_ready,
    )
    primary = _FakeTranscriber("turbo", ["переведи на английский"])
    translator = _FakeTranscriber("large-v3")
    controller._transcriber = primary  # type: ignore[assignment]
    controller._new_transcriber = lambda _model: translator  # type: ignore[method-assign]
    try:
        _dictate(controller)
        assert controller._translator_warmup_future is not None
        controller._translator_warmup_future.result(timeout=2)

        assert model_ready == []
    finally:
        controller.close()
        assert controller.wait_closed(2)


def test_disabling_voice_translation_wins_over_in_flight_on_command() -> None:
    notifications: list[tuple[str, str, bool]] = []
    controller = _controller(
        AppConfig(model="turbo", voice_translate=True, auto_insert=False),
        notifications=notifications,
        callback_lock_check=True,
    )
    primary = _BlockingResponseTranscriber("turbo", "переведи на английский")
    translator = _FakeTranscriber("large-v3")
    controller._transcriber = primary  # type: ignore[assignment]
    controller._new_transcriber = lambda _model: translator  # type: ignore[method-assign]
    try:
        assert controller.start_recording(target="editor") is True
        assert controller.stop_recording() is True
        assert primary.started.wait(timeout=2)

        controller.update_config(
            AppConfig(model="turbo", voice_translate=False, auto_insert=False)
        )
        primary.release.set()
        assert controller._future is not None
        controller._future.result(timeout=2)

        assert controller.translating is False
        assert not any("Перевод на английский включён" in item[1] for item in notifications)
    finally:
        primary.release.set()
        controller.close()
        assert controller.wait_closed(2)


def test_disabling_translation_keeps_already_started_english_result() -> None:
    results: list[str] = []
    controller = _controller(
        AppConfig(model="turbo", voice_translate=True, auto_insert=False),
        results=results,
    )
    primary = _FakeTranscriber("turbo")
    translator = _BlockingResponseTranscriber("large-v3", "English result")
    translator.warmups = 1
    controller._transcriber = primary  # type: ignore[assignment]
    controller._translator = translator  # type: ignore[assignment]
    controller.translating = True
    try:
        assert controller.start_recording(target="editor") is True
        assert controller.stop_recording() is True
        assert translator.started.wait(timeout=2)

        controller.update_config(
            AppConfig(model="turbo", voice_translate=False, auto_insert=False)
        )
        translator.release.set()
        assert controller._future is not None
        controller._future.result(timeout=2)

        assert controller.translating is False
        assert translator.tasks == ["translate"]
        assert primary.tasks == []
        assert results == ["English result"]
    finally:
        translator.release.set()
        controller.close()
        assert controller.wait_closed(2)


def test_stale_translator_load_failure_does_not_disable_new_model() -> None:
    results: list[str] = []
    notifications: list[tuple[str, str, bool]] = []
    controller = _controller(
        AppConfig(model="turbo", voice_translate=True, auto_insert=False),
        results=results,
        notifications=notifications,
        callback_lock_check=True,
    )
    primary = _FakeTranscriber("turbo", ["обычная диктовка"])
    old_translator = _BlockingLoadFailureTranscriber("large-v3")
    new_translator = _FakeTranscriber("medium")
    controller._transcriber = primary  # type: ignore[assignment]
    controller._translator = old_translator  # type: ignore[assignment]
    controller._new_transcriber = lambda _model: new_translator  # type: ignore[method-assign]
    controller.translating = True
    try:
        assert controller.start_recording(target="editor") is True
        assert controller.stop_recording() is True
        assert old_translator.started.wait(timeout=2)

        controller.update_config(
            AppConfig(
                model="turbo",
                voice_translate=True,
                translate_model="medium",
                auto_insert=False,
            )
        )
        replacement_warmup = controller._translator_warmup_future
        assert replacement_warmup is not None
        old_translator.release.set()
        assert controller._future is not None
        controller._future.result(timeout=2)
        replacement_warmup.result(timeout=2)

        assert controller.translating is True
        assert old_translator.closed.is_set()
        assert new_translator.warmups == 1
        assert results == ["обычная диктовка"]
        assert not any(item[2] for item in notifications)
    finally:
        old_translator.release.set()
        controller.close()
        assert controller.wait_closed(2)


def test_close_during_translator_load_failure_skips_primary_fallback() -> None:
    statuses: list[tuple[str, str]] = []
    notifications: list[tuple[str, str, bool]] = []
    controller = _controller(
        AppConfig(model="turbo", voice_translate=True, auto_insert=False),
        statuses=statuses,
        notifications=notifications,
    )
    primary = _FakeTranscriber("turbo", ["не должен распознаваться"])
    translator = _BlockingLoadFailureTranscriber("large-v3")
    controller._transcriber = primary  # type: ignore[assignment]
    controller._translator = translator  # type: ignore[assignment]
    controller.translating = True

    assert controller.start_recording(target="editor") is True
    assert controller.stop_recording() is True
    assert translator.started.wait(timeout=2)
    callbacks_before_close = (list(statuses), list(notifications))
    controller.close()
    translator.release.set()

    assert controller.wait_closed(2)
    assert primary.tasks == []
    assert (statuses, notifications) == callbacks_before_close


@pytest.mark.parametrize(
    ("initial_mode", "updated_mode", "should_retire"),
    (("eco", "instant", False), ("instant", "eco", True)),
)
def test_resource_mode_change_during_translation_uses_latest_setting(
    initial_mode: str,
    updated_mode: str,
    should_retire: bool,
) -> None:
    controller = _controller(
        AppConfig(
            model="turbo",
            voice_translate=True,
            resource_mode=initial_mode,
            auto_insert=False,
        )
    )
    primary = _FakeTranscriber("turbo")
    translator = _BlockingResponseTranscriber("large-v3", "English result")
    translator.warmups = 1
    controller._transcriber = primary  # type: ignore[assignment]
    controller._translator = translator  # type: ignore[assignment]
    controller.translating = True
    try:
        assert controller.start_recording(target="editor") is True
        assert controller.stop_recording() is True
        assert translator.started.wait(timeout=2)

        controller.update_config(
            AppConfig(
                model="turbo",
                voice_translate=True,
                resource_mode=updated_mode,
                auto_insert=False,
            )
        )
        translator.release.set()
        assert controller._future is not None
        controller._future.result(timeout=2)

        if should_retire:
            assert primary.closed.wait(timeout=2)
            assert translator.closed.wait(timeout=2)
        else:
            controller._executor.submit(lambda: None).result(timeout=2)
            assert primary.closed.is_set() is False
            assert translator.closed.is_set() is False
    finally:
        translator.release.set()
        controller.close()
        assert controller.wait_closed(2)


def test_translate_model_change_invalidates_blocked_warmup_without_stale_callbacks() -> None:
    statuses: list[tuple[str, str]] = []
    controller = _controller(
        AppConfig(model="turbo", voice_translate=True, auto_insert=False),
        statuses=statuses,
        callback_lock_check=True,
    )
    primary = _FakeTranscriber("turbo", ["переведи на английский"])
    old_translator = _BlockingWarmupTranscriber("large-v3")
    new_translator = _FakeTranscriber("medium")
    controller._transcriber = primary  # type: ignore[assignment]
    controller._new_transcriber = (  # type: ignore[method-assign]
        lambda model: old_translator if model == "large-v3" else new_translator
    )
    try:
        _dictate(controller)
        assert old_translator.started.wait(timeout=2)
        ready_before = sum(state == "ready" for _, state in statuses)

        controller.update_config(
            AppConfig(
                model="turbo",
                voice_translate=True,
                translate_model="medium",
                auto_insert=False,
            )
        )
        old_translator.release.set()
        assert controller._translator_warmup_future is not None
        controller._translator_warmup_future.result(timeout=2)

        assert controller.translating is True
        assert old_translator.closed.wait(timeout=2)
        assert new_translator.warmups == 1
        assert sum(state == "ready" for _, state in statuses) == ready_before + 1
    finally:
        controller.close()
        assert controller.wait_closed(2)


def test_primary_model_change_disposes_redundant_translator_slot() -> None:
    controller = _controller(
        AppConfig(model="turbo", voice_translate=True, auto_insert=False)
    )
    primary = _FakeTranscriber("turbo")
    translator = _FakeTranscriber("large-v3")
    translator.warmups = 1
    controller._transcriber = primary  # type: ignore[assignment]
    controller._translator = translator  # type: ignore[assignment]
    try:
        controller.update_config(
            AppConfig(model="medium", voice_translate=True, auto_insert=False)
        )
        barrier = controller._executor.submit(lambda: None)
        barrier.result(timeout=2)

        assert primary.closed.is_set()
        assert translator.closed.is_set()
        assert controller._translator is None
    finally:
        controller.close()
        assert controller.wait_closed(2)


def test_disabling_voice_translation_setting_releases_resident_translator() -> None:
    controller = _controller(
        AppConfig(model="turbo", voice_translate=True, auto_insert=False)
    )
    translator = _FakeTranscriber("large-v3")
    translator.warmups = 1
    controller._translator = translator  # type: ignore[assignment]
    try:
        controller.update_config(
            AppConfig(model="turbo", voice_translate=False, auto_insert=False)
        )
        controller._executor.submit(lambda: None).result(timeout=2)

        assert translator.closed.is_set()
        assert controller._translator is None
    finally:
        controller.close()
        assert controller.wait_closed(2)


def test_close_during_translation_warmup_suppresses_late_callbacks() -> None:
    statuses: list[tuple[str, str]] = []
    notifications: list[tuple[str, str, bool]] = []
    controller = _controller(
        AppConfig(model="turbo", voice_translate=True, auto_insert=False),
        statuses=statuses,
        notifications=notifications,
    )
    primary = _FakeTranscriber("turbo", ["переведи на английский"])
    translator = _BlockingWarmupTranscriber("large-v3")
    controller._transcriber = primary  # type: ignore[assignment]
    controller._new_transcriber = lambda _model: translator  # type: ignore[method-assign]

    _dictate(controller)
    assert translator.started.wait(timeout=2)
    callbacks_before_close = (list(statuses), list(notifications))
    controller.close()
    translator.release.set()

    assert controller.wait_closed(2)
    assert primary.closed.is_set()
    assert translator.closed.is_set()
    assert (statuses, notifications) == callbacks_before_close


@pytest.mark.parametrize("resource_mode", ("eco", "balanced"))
def test_resource_retirement_releases_both_model_slots(
    monkeypatch: pytest.MonkeyPatch,
    resource_mode: str,
) -> None:
    if resource_mode == "balanced":
        monkeypatch.setitem(
            controller_module._MODEL_RETIRE_SECONDS,
            "balanced",
            0.0,
        )
    controller = _controller(
        AppConfig(
            model="turbo",
            voice_translate=True,
            resource_mode=resource_mode,
            auto_insert=False,
        )
    )
    primary = _FakeTranscriber("turbo", ["обычный перевод"])
    translator = _FakeTranscriber("large-v3", ["English translation"])
    translator.warmups = 1
    controller._transcriber = primary  # type: ignore[assignment]
    controller._translator = translator  # type: ignore[assignment]
    controller.translating = True
    try:
        _dictate(controller)

        assert primary.closed.wait(timeout=2)
        assert translator.closed.wait(timeout=2)
        assert controller._transcriber is None
        assert controller._translator is None
    finally:
        controller.close()
        assert controller.wait_closed(2)


def test_translation_mode_is_new_session_state_for_each_controller() -> None:
    config = AppConfig(voice_translate=True)
    first = _controller(config)
    first.translating = True
    first.close()
    assert first.wait_closed(2)

    second = _controller(config)
    try:
        assert second.translating is False
    finally:
        second.close()
        assert second.wait_closed(2)
