"""Application orchestration kept separate from Qt widgets and Win32 hooks."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
import logging
import queue
import threading
import time
from typing import Any, Callable

from .audio import (
    AudioCaptureError,
    AudioRecorder,
    AudioTooShortError,
    SilentAudioError,
    normalize_device_selector,
)
from .config import AppConfig
from .state import SessionState
from .text import process_transcript
from .transcriber import (
    FasterWhisperTranscriber,
    ModelLoadError,
    NoSpeechDetected,
    TranscriptionError,
)
from .platform_support import hotkey_hint, input_adapter, is_macos


LOGGER = logging.getLogger(__name__)
_SETUP_MODELS = frozenset({"small", "medium", "turbo", "large-v3"})
_SHORT_RECORDING_VAD_THRESHOLD_SECONDS = 15.0
# Turbo was empirically verified to ignore faster-whisper's translate task.
_TRANSLATION_INCAPABLE_MODELS = frozenset({"turbo"})
_PREPARE_CAPTURE_TIMEOUT_SECONDS = 1.5
_PREPARE_CAPTURE_BUFFER_SECONDS = 2.0
_MODEL_RETIRE_SECONDS: dict[str, float | None] = {
    "instant": None,
    "balanced": 300.0,
    "eco": 0.0,
}
ModelReadyCallback = Callable[[str, str, str], None]


def _setup_command(model_size: str) -> str:
    if is_macos():
        model_argument = f" --model {model_size}" if model_size in _SETUP_MODELS else ""
        return f"bash scripts/setup-macos.sh{model_argument}"
    return (
        f".\\scripts\\setup.ps1 -Model {model_size}"
        if model_size in _SETUP_MODELS
        else ".\\scripts\\setup.ps1"
    )


def _setup_recovery_instruction(model_size: str) -> str:
    """Describe the platform-specific safe model setup sequence."""

    setup_command = _setup_command(model_size)
    if is_macos():
        return f"Запустите {setup_command} и перезапустите Pressay."
    return (
        "Полностью выйдите из Pressay через меню в трее, запустите "
        f"{setup_command} и откройте Pressay снова."
    )


def _insertion_status_text(reason: str, bindings: Any | None = None) -> str:
    """Turn privacy-safe adapter reason codes into concise user guidance."""

    if reason in {
        "foreground_target_changed",
        "foreground_target_changed_before_enter",
        "target_mismatch",
        "target_guard_failed",
    }:
        return "Не вставлено: сменилось активное окно"
    if reason == "focused_control_is_not_editable":
        return (
            "Поле не распознано — текст в истории "
            "(настройка «Вставлять только в…»)"
        )
    if reason == "physical_modifiers_not_released":
        return f"Не вставлено: отпустите {hotkey_hint('hold', bindings)}"
    if reason in {
        "recording_target_required",
        "foreground_snapshot_failed",
        "no_foreground_window",
    }:
        return "Не вставлено: поле ввода не определено"
    return "Не вставлено — текст сохранён ниже"


def _duration_limit_notification_text(max_duration_seconds: float) -> str:
    """Russian warning naming the recorder's own configured duration bound."""

    minutes = max_duration_seconds / 60.0
    minutes_text = (
        str(int(round(minutes)))
        if abs(minutes - round(minutes)) < 1e-6
        else f"{minutes:.1f}"
    )
    return (
        f"Достигнут предел записи {minutes_text} мин — "
        "распознаётся только записанная часть."
    )


def _initial_prompt(replacements: dict[str, str], *, limit: int = 512) -> str | None:
    """Build a bounded local ASR vocabulary hint from canonical spellings."""

    terms: list[str] = []
    seen: set[str] = set()
    used = 0
    for canonical in replacements.values():
        term = canonical.strip()
        folded = term.casefold()
        if not term or folded in seen:
            continue
        extra = len(term) + (2 if terms else 0)
        if used + extra > limit:
            break
        terms.append(term)
        seen.add(folded)
        used += extra
    return ", ".join(terms) or None


StatusCallback = Callable[[str, str], None]
ResultCallback = Callable[[str], None]
NotificationCallback = Callable[[str, str, bool], None]


def _prepare_insertion_text(
    text: str,
    *,
    press_enter: bool,
    smart_spacing: bool,
) -> str:
    """Add a separator only to ordinary single-line automatic dictation.

    The canonical transcript stays untouched.  This helper is used solely for
    the text handed to the input adapter, so display/copy paths retain exactly
    what speech processing produced.
    """

    if (
        not smart_spacing
        or press_enter
        or not text
        or text[-1].isspace()
        or "\n" in text
        or "\r" in text
    ):
        return text
    return f"{text} "


@dataclass(frozen=True, slots=True)
class _TranscriptionJob:
    """Everything a worker needs from the session that submitted it."""

    session_id: int
    audio: Any
    target: Any | None
    config: AppConfig
    cancelled: threading.Event
    display_only: bool
    released_at: float
    audio_finalize_seconds: float
    finalize_breakdown: dict[str, float]
    prearmed: bool
    vad_used: bool | None
    translating: bool
    translation_generation: int


class _CaptureIntent(str, Enum):
    """Controller-owned intent, independent of a native stream's timing."""

    IDLE = "idle"
    PREPARING = "preparing"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    CLOSED = "closed"


@dataclass(slots=True)
class _AudioCommand:
    """One operation serialized by the dedicated daemon audio worker."""

    action: str
    generation: int
    session_id: int | None = None
    recorder: Any | None = None
    completion: threading.Event | None = None
    result: bool = False
    requested_at: float = field(default_factory=time.perf_counter)


class DictationController:
    """Own one active recording and serialize model work on a worker thread."""

    def __init__(
        self,
        config: AppConfig,
        *,
        status_callback: StatusCallback,
        result_callback: ResultCallback,
        notification_callback: NotificationCallback,
        model_ready_callback: ModelReadyCallback | None = None,
    ) -> None:
        self.config = config
        self.status_callback = status_callback
        self.result_callback = result_callback
        self.notification_callback = notification_callback
        self.model_ready_callback = model_ready_callback
        self.state = SessionState()
        self.translating = False
        self.last_transcript = ""
        self.target: Any | None = None
        self._recorder: AudioRecorder | None = None
        self._transcriber: FasterWhisperTranscriber | None = None
        self._translator: FasterWhisperTranscriber | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pressay-asr")
        self._future: Future[Any] | None = None
        self._warmup_future: Future[Any] | None = None
        self._translator_warmup_future: Future[Any] | None = None
        self._lock = threading.RLock()
        # Serializes warmup-only UI callbacks against state invalidation. It is
        # deliberately separate from _lock: user callbacks never run while the
        # controller state/model lock is held.
        self._warmup_status_gate = threading.RLock()
        self._closed = False
        self._close_complete = threading.Event()
        self._asr_close_complete = threading.Event()
        self._audio_close_complete = threading.Event()
        self._session_cancelled: threading.Event | None = None
        self._model_generation = 0
        self._translation_generation = 0
        self._preload_enabled = False
        self._residency_generation = 0
        self._residency_timer: threading.Timer | None = None
        self._capture_generation = 0
        self._capture_intent = _CaptureIntent.IDLE
        self._prepared_timeout: threading.Timer | None = None
        self._recording_prearmed = False
        self._audio_commands: queue.Queue[_AudioCommand] = queue.Queue()
        self._audio_thread = threading.Thread(
            target=self._audio_worker,
            name="pressay-audio",
            daemon=True,
        )
        self._audio_thread.start()

    def _cancel_model_retirement_locked(self) -> None:
        self._residency_generation += 1
        timer = self._residency_timer
        self._residency_timer = None
        if timer is not None:
            timer.cancel()

    def _schedule_model_retirement(self) -> None:
        with self._lock:
            if self._closed:
                return
            delay = _MODEL_RETIRE_SECONDS.get(self.config.resource_mode)
            self._cancel_model_retirement_locked()
            if delay is None:
                return
            generation = self._residency_generation
            timer = threading.Timer(
                delay,
                self._retire_model_if_idle,
                args=(generation,),
            )
            timer.daemon = True
            self._residency_timer = timer
            timer.start()

    def _retire_model_if_idle(self, generation: int) -> None:
        with self._lock:
            if (
                self._closed
                or generation != self._residency_generation
                or self.state.active
                or self._capture_intent is not _CaptureIntent.IDLE
            ):
                return
            self._residency_timer = None
        LOGGER.info("model_retirement_queued resource_mode=%s", self.config.resource_mode)
        try:
            self._executor.submit(self._dispose_models)
        except RuntimeError:
            return

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._capture_intent in {
                _CaptureIntent.STARTING,
                _CaptureIntent.RECORDING,
                _CaptureIntent.STOPPING,
            }

    def current_recording_rms(self) -> float:
        """Return the current active recorder level without blocking UI work."""

        with self._lock:
            recorder = self._recorder
        return float(getattr(recorder, "current_rms", 0.0)) if recorder is not None else 0.0

    def _microphone_device(self) -> int | str | None:
        return normalize_device_selector(self.config.microphone)

    def _ready_status_text(self) -> str:
        """Name whichever gesture actually starts a recording right now."""

        bindings = self.config.hotkeys
        if bindings.push_to_talk:
            return f"Готов — удерживайте {hotkey_hint('hold', bindings)}"
        toggle = hotkey_hint("toggle", bindings)
        return "Готов к диктовке" if toggle is None else f"Готов — {toggle}"

    def _copy_hint_sentence(self) -> str:
        """Closing sentence about copying, minus the shortcut when disabled."""

        shortcut = hotkey_hint("copy", self.config.hotkeys)
        if shortcut is None:
            return "Скопируйте его кнопкой в окне Pressay."
        return f"Скопируйте его кнопкой или {shortcut}."

    def _new_recorder(self) -> AudioRecorder:
        return AudioRecorder(device=self._microphone_device())

    def _ensure_transcriber(self, model_size: str) -> FasterWhisperTranscriber:
        """Return the requested model.

        This method and :meth:`_dispose_transcriber` are only run by the ASR
        executor.  Native model lifetime changes therefore cannot race an
        inference or block the GUI thread.
        """

        if self._transcriber is None or self._transcriber.model_size != model_size:
            self._dispose_transcriber()
            self._transcriber = self._new_transcriber(model_size)
        return self._transcriber

    def _ensure_translator(self, model_size: str) -> FasterWhisperTranscriber:
        """Return the separately resident translation model on the ASR worker."""

        if self._translator is None or self._translator.model_size != model_size:
            self._dispose_translator()
            self._translator = self._new_transcriber(model_size)
        return self._translator

    @staticmethod
    def _new_transcriber(model_size: str) -> FasterWhisperTranscriber:
        return FasterWhisperTranscriber(model_size=model_size, local_files_only=False)

    def _dispose_transcriber(self) -> None:
        with self._lock:
            transcriber = self._transcriber
            self._transcriber = None
        if transcriber is None:
            return
        try:
            transcriber.close()
        except Exception:
            LOGGER.exception("transcriber_close_failed")

    def _dispose_translator(self) -> None:
        with self._lock:
            translator = self._translator
            self._translator = None
        if translator is None:
            return
        try:
            translator.close()
        except Exception:
            LOGGER.exception("translator_close_failed")

    def _dispose_models(self) -> None:
        """Release both native model slots from the serialized ASR worker."""

        self._dispose_transcriber()
        self._dispose_translator()

    def warmup_model(self) -> bool:
        """Queue a local model preload without blocking the caller.

        Warmup and transcription share the same single-worker executor. A user
        can therefore start recording immediately, while a later transcription
        waits for the already-running preload instead of racing a second model
        instance.
        """

        with self._warmup_status_gate:
            with self._lock:
                if self._closed:
                    return False
                self._preload_enabled = True
                self._model_generation += 1
                generation = self._model_generation
                model_size = self.config.model
                self._warmup_future = self._executor.submit(
                    self._warmup_worker,
                    model_size,
                    generation,
                )
        return True

    def _warmup_is_current_locked(self, model_size: str, generation: int) -> bool:
        return (
            not self._closed
            and self._preload_enabled
            and self._model_generation == generation
            and self.config.model == model_size
        )

    def _warmup_is_current(self, model_size: str, generation: int) -> bool:
        with self._lock:
            return self._warmup_is_current_locked(model_size, generation)

    def _warmup_progress_callback(
        self, model_size: str, generation: int
    ) -> Callable[[int], None]:
        last_update = time.monotonic()

        def report(percent: int) -> None:
            nonlocal last_update
            now = time.monotonic()
            with self._warmup_status_gate:
                with self._lock:
                    current = self._warmup_is_current_locked(model_size, generation)
                    show_status = not self.state.active
                if not current or not show_status or now - last_update < 1.0:
                    return
                last_update = now
                self.status_callback(
                    f"Скачиваю модель {model_size} — {max(0, min(100, percent))}%…",
                    "processing",
                )

        return report

    def _warmup_worker(self, model_size: str, generation: int) -> None:
        with self._warmup_status_gate:
            with self._lock:
                if not self._warmup_is_current_locked(model_size, generation):
                    return
                show_status = not self.state.active
            if show_status:
                self.status_callback(f"Готовлю модель {model_size}…", "processing")

        # A settings update may have invalidated this queued request before it
        # reached the executor. Do not load a model that is already obsolete.
        if not self._warmup_is_current(model_size, generation):
            return
        try:
            transcriber = self._ensure_transcriber(model_size)
            set_progress_callback = getattr(transcriber, "set_download_progress_callback", None)
            if set_progress_callback is not None:
                set_progress_callback(self._warmup_progress_callback(model_size, generation))
            device, compute_type = transcriber.warmup()
        except Exception as exc:
            with self._warmup_status_gate:
                with self._lock:
                    current = self._warmup_is_current_locked(model_size, generation)
                    session_active = self.state.active
                if not current:
                    LOGGER.debug("stale_model_warmup_failure: %s", type(exc).__name__)
                    return
                setup_command = _setup_command(model_size)
                message = (
                    f"Локальная модель {model_size!r} не загрузилась. "
                    f"{_setup_recovery_instruction(model_size)}"
                )
                if not session_active:
                    status = f"Модель {model_size} не готова — "
                    if is_macos():
                        status += f"запустите {setup_command}"
                    else:
                        status += (
                            "выйдите из Pressay и запустите "
                            f"{setup_command}"
                        )
                    self.status_callback(status, "error")
                self.notification_callback("Pressay", message, True)
            LOGGER.warning("model_warmup_failed: %s", type(exc).__name__)
            return

        with self._warmup_status_gate:
            with self._lock:
                current = self._warmup_is_current_locked(model_size, generation)
                session_active = self.state.active
            if current and self.model_ready_callback is not None:
                self.model_ready_callback(model_size, device, compute_type)
            if current and not session_active:
                self.status_callback(self._ready_status_text(), "ready")
        if current:
            self._schedule_model_retirement()

    def _translation_warmup_is_current_locked(
        self, model_size: str, generation: int
    ) -> bool:
        return (
            not self._closed
            and self.translating
            and self._translation_generation == generation
            and self.config.model in _TRANSLATION_INCAPABLE_MODELS
            and self.config.translate_model == model_size
        )

    def _translation_warmup_is_current(
        self, model_size: str, generation: int
    ) -> bool:
        with self._lock:
            return self._translation_warmup_is_current_locked(model_size, generation)

    def _translation_warmup_progress_callback(
        self, model_size: str, generation: int
    ) -> Callable[[int], None]:
        last_update = time.monotonic()

        def report(percent: int) -> None:
            nonlocal last_update
            now = time.monotonic()
            with self._warmup_status_gate:
                with self._lock:
                    current = self._translation_warmup_is_current_locked(
                        model_size, generation
                    )
                    show_status = not self.state.active
                if not current or not show_status or now - last_update < 1.0:
                    return
                last_update = now
                self.status_callback(
                    "Скачиваю модель перевода "
                    f"{model_size} — {max(0, min(100, percent))}%…",
                    "processing",
                )

        return report

    def _queue_translation_warmup_locked(self) -> bool:
        if (
            self._closed
            or not self.translating
            or self.config.model not in _TRANSLATION_INCAPABLE_MODELS
        ):
            return False
        model_size = self.config.translate_model
        translator = self._translator
        if (
            translator is not None
            and translator.model_size == model_size
            and bool(getattr(translator, "is_loaded", False))
        ):
            return False
        self._translation_generation += 1
        generation = self._translation_generation
        self._translator_warmup_future = self._executor.submit(
            self._translation_warmup_worker,
            model_size,
            generation,
        )
        return True

    def _translation_warmup_worker(self, model_size: str, generation: int) -> None:
        with self._warmup_status_gate:
            with self._lock:
                if not self._translation_warmup_is_current_locked(
                    model_size, generation
                ):
                    return
                show_status = not self.state.active
            if show_status:
                self.status_callback(
                    f"Готовлю модель перевода {model_size}…",
                    "processing",
                )

        if not self._translation_warmup_is_current(model_size, generation):
            return
        try:
            translator = self._ensure_translator(model_size)
            set_progress_callback = getattr(
                translator, "set_download_progress_callback", None
            )
            if set_progress_callback is not None:
                set_progress_callback(
                    self._translation_warmup_progress_callback(model_size, generation)
                )
            translator.warmup()
        except Exception as exc:
            with self._warmup_status_gate:
                with self._lock:
                    current = self._translation_warmup_is_current_locked(
                        model_size, generation
                    )
                    session_active = self.state.active
                    if current:
                        self.translating = False
                        self._translation_generation += 1
                if not current:
                    LOGGER.debug(
                        "stale_translation_warmup_failure: %s",
                        type(exc).__name__,
                    )
                    return
                self._dispose_translator()
                message = (
                    f"Модель перевода {model_size!r} не загрузилась. "
                    "Режим перевода выключен; обычная диктовка продолжит работать. "
                    f"{_setup_recovery_instruction(model_size)}"
                )
                if not session_active:
                    self.status_callback(
                        "Перевод выключен: модель перевода не готова",
                        "warning",
                    )
                self.notification_callback("Pressay", message, True)
            LOGGER.warning("translation_model_warmup_failed: %s", type(exc).__name__)
            return

        with self._warmup_status_gate:
            with self._lock:
                current = self._translation_warmup_is_current_locked(
                    model_size, generation
                )
                session_active = self.state.active
            if current and not session_active:
                self.status_callback("Перевод на английский включён — → EN", "ready")
        if current:
            self._schedule_model_retirement()

    def _capture_is_current_locked(
        self,
        generation: int,
        session_id: int,
        *,
        recorder: Any | None = None,
    ) -> bool:
        if (
            self._closed
            or self._capture_generation != generation
            or self.state.session_id != session_id
            or self._session_cancelled is None
            or self._session_cancelled.is_set()
            or self._capture_intent
            not in {
                _CaptureIntent.STARTING,
                _CaptureIntent.RECORDING,
                _CaptureIntent.STOPPING,
            }
        ):
            return False
        return recorder is None or self._recorder is recorder

    def _queue_audio(self, command: _AudioCommand) -> _AudioCommand:
        self._audio_commands.put_nowait(command)
        return command

    def _cancel_prepared_timeout_locked(self) -> None:
        if self._prepared_timeout is not None:
            self._prepared_timeout.cancel()
            self._prepared_timeout = None

    def prepare_capture(self) -> bool:
        """Open a bounded, silent prearmed capture without changing UI state.

        Disabled by default: live telemetry showed the pre-opened WASAPI
        stream still delivered its first frame ~0.4s after the gesture (no
        real head-start), while opening a device stream on every Ctrl+Win
        touch - including Windows' own Ctrl+Win shortcuts - multiplied
        open/close churn on the system audio engine. Suspected of degrading
        the audio stack over time (system-wide crackling until reboot).
        """

        with self._lock:
            if (
                not self.config.prearm_capture
                or self._closed
                or self.state.active
                or self._capture_intent is not _CaptureIntent.IDLE
            ):
                return False
            self._capture_generation += 1
            generation = self._capture_generation
            self._capture_intent = _CaptureIntent.PREPARING
            self._recording_prearmed = False
            self._queue_audio(_AudioCommand(action="prepare", generation=generation))
            return True

    def abandon_prepared_capture(self) -> bool:
        """Discard a prearmed microphone stream without visible lifecycle effects."""

        with self._lock:
            if self._capture_intent is not _CaptureIntent.PREPARING:
                return False
            recorder = self._recorder
            self._capture_generation += 1
            self._capture_intent = _CaptureIntent.IDLE
            self._recorder = None
            self._recording_prearmed = False
            self._cancel_prepared_timeout_locked()
            if recorder is not None:
                self._queue_audio(
                    _AudioCommand(
                        action="cancel",
                        generation=self._capture_generation,
                        recorder=recorder,
                    )
                )
        LOGGER.debug("prepared_capture_abandoned")
        return True

    def _expire_prepared_capture(self, generation: int, recorder: Any) -> None:
        with self._lock:
            if (
                self._closed
                or self._capture_generation != generation
                or self._capture_intent is not _CaptureIntent.PREPARING
                or self._recorder is not recorder
            ):
                return
            self._capture_generation += 1
            self._capture_intent = _CaptureIntent.IDLE
            self._recorder = None
            self._recording_prearmed = False
            self._prepared_timeout = None
            self._queue_audio(
                _AudioCommand(
                    action="cancel",
                    generation=self._capture_generation,
                    recorder=recorder,
                )
            )
        LOGGER.debug("prepared_capture_timed_out")

    def _request_start_recording(self, *, target: Any | None) -> _AudioCommand | None:
        with self._warmup_status_gate:
            with self._lock:
                if (
                    self._closed
                    or self._capture_intent
                    not in {_CaptureIntent.IDLE, _CaptureIntent.PREPARING}
                    or self.state.active
                ):
                    return None
                if self._session_cancelled is not None:
                    self._session_cancelled.set()
                self.state = self.state.start()
                session_id = self.state.session_id
                assert session_id is not None
                self._session_cancelled = threading.Event()
                self._cancel_model_retirement_locked()
                self.target = target
                prearmed = self._capture_intent is _CaptureIntent.PREPARING
                if not prearmed:
                    self._recorder = None
                    self._capture_generation += 1
                generation = self._capture_generation
                self._capture_intent = _CaptureIntent.STARTING
                self._recording_prearmed = prearmed
                if prearmed:
                    self._cancel_prepared_timeout_locked()
                command = _AudioCommand(
                    action="start",
                    generation=generation,
                    session_id=session_id,
                    completion=threading.Event(),
                )
                return self._queue_audio(command)

    def request_start_recording(self, *, target: Any | None = None) -> bool:
        """Request capture start and return before any native audio call."""

        return self._request_start_recording(target=target) is not None

    def start_recording(self, *, target: Any | None = None) -> bool:
        """Synchronous compatibility wrapper; desktop handlers use request_* APIs."""

        command = self._request_start_recording(target=target)
        if command is None:
            return False
        assert command.completion is not None
        command.completion.wait()
        return command.result

    def _request_stop_recording(self) -> _AudioCommand | None:
        with self._lock:
            if (
                self._closed
                or self.state.session_id is None
                or self._capture_intent
                not in {_CaptureIntent.STARTING, _CaptureIntent.RECORDING}
            ):
                return None
            session_id = self.state.session_id
            generation = self._capture_generation
            self._capture_intent = _CaptureIntent.STOPPING
            command = _AudioCommand(
                action="stop",
                generation=generation,
                session_id=session_id,
                completion=threading.Event(),
            )
            return self._queue_audio(command)

    def request_stop_recording(self) -> bool:
        """Request capture stop/preprocessing on the daemon audio worker."""

        return self._request_stop_recording() is not None

    def stop_recording(self) -> bool:
        """Synchronous compatibility wrapper; desktop handlers use request_* APIs."""

        command = self._request_stop_recording()
        if command is None:
            return False
        assert command.completion is not None
        command.completion.wait()
        return command.result

    def request_toggle_recording(self, *, target: Any | None = None) -> bool:
        """Atomically choose start/stop intent without touching native audio."""

        with self._lock:
            stopping = self._capture_intent in {
                _CaptureIntent.STARTING,
                _CaptureIntent.RECORDING,
            }
        if stopping:
            return self.request_stop_recording()
        return self.request_start_recording(target=target)

    def toggle_recording(self, *, target: Any | None = None) -> bool:
        """Synchronous compatibility toggle."""

        with self._lock:
            stopping = self._capture_intent in {
                _CaptureIntent.STARTING,
                _CaptureIntent.RECORDING,
            }
        if stopping:
            return self.stop_recording()
        return self.start_recording(target=target)

    def _request_cancel(self) -> tuple[bool, _AudioCommand | None, int | None]:
        with self._warmup_status_gate:
            with self._lock:
                session_id = self.state.session_id
                cancelled = self._session_cancelled
                cancellable_result = self.state.result is not None
                if (
                    session_id is None
                    or cancelled is None
                    or cancelled.is_set()
                    or (not self.state.active and not cancellable_result)
                ):
                    return False, None, session_id
                cancelled.set()
                # Invalidate capture before its queued/in-flight native call can
                # publish state or submit ASR.
                self._capture_generation += 1
                self._model_generation += 1
                recorder = self._recorder
                self._recorder = None
                self._capture_intent = _CaptureIntent.IDLE
                self._recording_prearmed = False
                self.target = None
                if self.state.active:
                    self.state = self.state.cancel(session_id).reset()
                else:
                    # COMPLETED remains cancellable until delivery returns. This
                    # lets Esc win while windows_input is waiting at a guard.
                    self.state = self.state.reset()
                command: _AudioCommand | None = None
                if recorder is not None:
                    command = _AudioCommand(
                        action="cancel",
                        generation=self._capture_generation,
                        session_id=session_id,
                        recorder=recorder,
                        completion=threading.Event(),
                    )
                    self._queue_audio(command)
        if self._session_is_current(session_id):
            self.status_callback("Отменено", "warning")
        return True, command, session_id

    def request_cancel(self) -> bool:
        """Invalidate immediately; run any recorder cancellation asynchronously."""

        accepted, _command, _session_id = self._request_cancel()
        return accepted

    def cancel(self) -> bool:
        """Synchronous compatibility wrapper; desktop handlers use request_cancel."""

        accepted, command, _session_id = self._request_cancel()
        if command is not None:
            assert command.completion is not None
            command.completion.wait()
        return accepted

    def _audio_worker(self) -> None:
        """Serialize every native recorder call on one daemon thread."""

        while True:
            command = self._audio_commands.get()
            try:
                if command.action == "start":
                    command.result = self._audio_start(command)
                elif command.action == "prepare":
                    command.result = self._audio_prepare(command)
                elif command.action == "stop":
                    command.result = self._audio_stop(command)
                elif command.action == "cancel":
                    command.result = self._audio_cancel(command.recorder)
                elif command.action == "close":
                    command.result = True
                    return
                else:
                    LOGGER.error("unknown_audio_command: %s", command.action)
            except Exception:
                # An unexpected Python-level failure must not kill the only
                # serializer and strand shutdown behind an unprocessed command.
                LOGGER.exception("audio_worker_command_failed: %s", command.action)
                command.result = False
            finally:
                if command.completion is not None:
                    command.completion.set()
                if command.action == "close":
                    self._audio_close_complete.set()
                    self._maybe_signal_close_complete()

    def _audio_prepare(self, command: _AudioCommand) -> bool:
        with self._lock:
            if (
                self._closed
                or self._capture_generation != command.generation
                or self._capture_intent
                not in {_CaptureIntent.PREPARING, _CaptureIntent.STARTING}
            ):
                return False

        recorder: Any | None = None
        try:
            recorder = self._new_recorder()
            with self._lock:
                if (
                    self._closed
                    or self._capture_generation != command.generation
                    or self._capture_intent
                    not in {_CaptureIntent.PREPARING, _CaptureIntent.STARTING}
                ):
                    return False
                self._recorder = recorder
            recorder.prepare_capture(_PREPARE_CAPTURE_BUFFER_SECONDS)
        except Exception as exc:
            if recorder is not None:
                self._audio_cancel(recorder)
            with self._lock:
                current = (
                    not self._closed
                    and self._capture_generation == command.generation
                    and self._capture_intent
                    in {_CaptureIntent.PREPARING, _CaptureIntent.STARTING}
                    and self._recorder is recorder
                )
                if current:
                    self._recorder = None
                    self._recording_prearmed = False
                    if self._capture_intent is _CaptureIntent.PREPARING:
                        self._capture_intent = _CaptureIntent.IDLE
            LOGGER.debug("prepared_capture_failed: %s", type(exc).__name__)
            return False

        with self._lock:
            current = (
                not self._closed
                and self._capture_generation == command.generation
                and self._recorder is recorder
                and self._capture_intent
                in {_CaptureIntent.PREPARING, _CaptureIntent.STARTING}
            )
            still_preparing = current and self._capture_intent is _CaptureIntent.PREPARING
            if still_preparing:
                timeout = threading.Timer(
                    _PREPARE_CAPTURE_TIMEOUT_SECONDS,
                    self._expire_prepared_capture,
                    args=(command.generation, recorder),
                )
                timeout.daemon = True
                self._prepared_timeout = timeout
        if not current:
            self._audio_cancel(recorder)
            return False
        if still_preparing:
            timeout.start()
        return True

    def _audio_start(self, command: _AudioCommand) -> bool:
        session_id = command.session_id
        assert session_id is not None
        with self._lock:
            if not self._capture_is_current_locked(command.generation, session_id):
                return False

        recorder: Any | None = None
        prearmed = False
        try:
            with self._lock:
                if not self._capture_is_current_locked(command.generation, session_id):
                    return False
                recorder = self._recorder
                prearmed = self._recording_prearmed and recorder is not None
            if recorder is None:
                # Construction is also off the caller thread because third-party
                # recorder fakes/backends are free to probe native state here.
                recorder = self._new_recorder()
                with self._lock:
                    if not self._capture_is_current_locked(command.generation, session_id):
                        return False
                    self._recorder = recorder
            if prearmed:
                if not recorder.activate_prepared_capture():
                    raise AudioCaptureError("Prepared microphone capture is unavailable")
            else:
                recorder.start()
        except Exception as exc:
            if recorder is not None:
                self._audio_cancel(recorder)
            with self._lock:
                current = self._capture_is_current_locked(
                    command.generation,
                    session_id,
                    recorder=recorder,
                )
                if current:
                    assert self._session_cancelled is not None
                    self._session_cancelled.set()
                    self.state = self.state.fail(session_id, str(exc) or "Ошибка микрофона").reset()
                    self._capture_intent = _CaptureIntent.IDLE
                    self._recorder = None
                    self._recording_prearmed = False
                    self.target = None
            if current and self._session_is_current(session_id):
                self.status_callback("Ошибка микрофона", "error")
            if current and self._session_is_current(session_id):
                self.notification_callback("Pressay", str(exc), True)
            if current:
                LOGGER.warning("microphone_start_failed: %s", type(exc).__name__)
            return False

        with self._lock:
            current = self._capture_is_current_locked(
                command.generation,
                session_id,
                recorder=recorder,
            )
            if current and self._capture_intent is _CaptureIntent.STARTING:
                self._capture_intent = _CaptureIntent.RECORDING
            listening = current and self._capture_intent is _CaptureIntent.RECORDING
        if not current:
            # Cancellation/close queued a matching native cleanup after this
            # in-flight start. Never publish or submit work for the stale token.
            return False
        if listening and self._job_is_active(session_id):
            self.status_callback("Слушаю…", "recording")
            self._start_stop_signal_monitor(command.generation, session_id, recorder)
        return True

    def _start_stop_signal_monitor(
        self,
        generation: int,
        session_id: int,
        recorder: Any,
    ) -> None:
        waiter = getattr(recorder, "wait_for_stop_signal", None)
        if not callable(waiter):
            return
        threading.Thread(
            target=self._monitor_stop_signal,
            args=(generation, session_id, recorder, waiter),
            name=f"pressay-audio-monitor-{session_id}",
            daemon=True,
        ).start()

    def _monitor_stop_signal(
        self,
        generation: int,
        session_id: int,
        recorder: Any,
        waiter: Callable[[float | None], bool],
    ) -> None:
        while True:
            with self._lock:
                if (
                    not self._capture_is_current_locked(
                        generation,
                        session_id,
                        recorder=recorder,
                    )
                    or self._capture_intent is not _CaptureIntent.RECORDING
                ):
                    return
            try:
                signalled = bool(waiter(0.25))
            except Exception as exc:
                LOGGER.warning("audio_stop_signal_failed: %s", type(exc).__name__)
                self._request_stop_for_token(generation, session_id, recorder)
                return
            if signalled:
                self._request_stop_for_token(generation, session_id, recorder)
                return

    def _request_stop_for_token(
        self,
        generation: int,
        session_id: int,
        recorder: Any,
    ) -> bool:
        with self._lock:
            if (
                not self._capture_is_current_locked(
                    generation,
                    session_id,
                    recorder=recorder,
                )
                or self._capture_intent is not _CaptureIntent.RECORDING
            ):
                return False
            self._capture_intent = _CaptureIntent.STOPPING
            self._queue_audio(
                _AudioCommand(
                    action="stop",
                    generation=generation,
                    session_id=session_id,
                )
            )
            return True

    @staticmethod
    def _stop_failure_presentation(exc: Exception) -> tuple[str, str]:
        if isinstance(exc, AudioTooShortError):
            return "Запись слишком короткая", "warning"
        if isinstance(exc, SilentAudioError):
            return "Сигнал микрофона не обнаружен", "warning"
        if type(exc).__name__ == "AudioDurationLimitError":
            return "Лимит записи достигнут", "warning"
        if type(exc).__name__ == "AudioStreamError":
            return "Ошибка аудиопотока", "error"
        return "Ошибка записи", "error"

    @staticmethod
    def _stop_failure_notification(exc: Exception) -> str:
        if isinstance(exc, AudioTooShortError):
            return "Говорите немного дольше перед завершением записи."
        if isinstance(exc, SilentAudioError):
            return (
                "Pressay не получает сигнал. Проверьте выбранный микрофон, "
                "уровень входа и разрешение на доступ."
            )
        if type(exc).__name__ == "AudioDurationLimitError":
            return "Запись превысила допустимый лимит. Начните новую диктовку."
        if type(exc).__name__ == "AudioStreamError":
            return (
                "Поток микрофона прерван. Закройте приложение, которое "
                "использует микрофон, и повторите запись."
            )
        return "Не удалось завершить запись. Проверьте микрофон и повторите."

    def _audio_stop(self, command: _AudioCommand) -> bool:
        session_id = command.session_id
        assert session_id is not None
        with self._lock:
            if (
                not self._capture_is_current_locked(command.generation, session_id)
                or self._capture_intent is not _CaptureIntent.STOPPING
                or self._recorder is None
            ):
                return False
            recorder = self._recorder
            cancelled = self._session_cancelled
            target = self.target
            prearmed = self._recording_prearmed
            assert cancelled is not None

        finalize_started = time.perf_counter()
        try:
            # stop() closes PortAudio and performs all PCM preprocessing. Both
            # operations stay on this daemon worker, never Qt/hotkey threads.
            recording = recorder.stop()
        except Exception as exc:
            text, status = self._stop_failure_presentation(exc)
            with self._lock:
                current = self._capture_is_current_locked(
                    command.generation,
                    session_id,
                    recorder=recorder,
                )
                if current:
                    cancelled.set()
                    if status == "warning":
                        self.state = self.state.cancel(session_id).reset()
                    else:
                        self.state = self.state.fail(
                            session_id,
                            str(exc) or text,
                        ).reset()
                    self._capture_intent = _CaptureIntent.IDLE
                    self._recorder = None
                    self._recording_prearmed = False
                    self.target = None
            if current and self._session_is_current(session_id):
                self.status_callback(text, status)
            if current and self._session_is_current(session_id):
                self.notification_callback(
                    "Pressay",
                    self._stop_failure_notification(exc),
                    True,
                )
            if current and status == "error":
                LOGGER.warning("microphone_stop_failed: %s", type(exc).__name__)
            return False
        audio_finalize_seconds = time.perf_counter() - finalize_started

        with self._lock:
            current = self._capture_is_current_locked(
                command.generation,
                session_id,
                recorder=recorder,
            )
            if not current:
                return False
            self._capture_intent = _CaptureIntent.IDLE
            self._recorder = None
            self._recording_prearmed = False
            self.target = None
            self.state = self.state.begin_transcription(session_id)
            recording_duration = getattr(recording, "duration_seconds", None)
            job = _TranscriptionJob(
                session_id=session_id,
                audio=recording.audio,
                target=target,
                config=deepcopy(self.config),
                cancelled=cancelled,
                # UI/tray-triggered recording has no trustworthy external
                # focus target. Display it in Pressay without mutating
                # the user's clipboard.
                display_only=target is None,
                released_at=command.requested_at,
                audio_finalize_seconds=audio_finalize_seconds,
                finalize_breakdown=dict(
                    getattr(recording, "finalize_breakdown", {}) or {}
                ),
                prearmed=prearmed,
                vad_used=(
                    float(recording_duration) > _SHORT_RECORDING_VAD_THRESHOLD_SECONDS
                    if recording_duration is not None
                    else None
                ),
                translating=self.translating,
                translation_generation=self._translation_generation,
            )
        if recording.limit_reached and current and self._session_is_current(session_id):
            # Recognition proceeds on the truncated buffer below; the user
            # still needs to know the tail of a long dictation was cut.
            self.notification_callback(
                "Pressay",
                _duration_limit_notification_text(recorder.max_duration_seconds),
                True,
            )
        if self._job_is_active(session_id):
            self.status_callback("Распознаю локально…", "processing")
        with self._lock:
            if not self._job_is_active_locked(session_id):
                return False
            self._future = self._executor.submit(self._transcribe_worker, job)
        return True

    @staticmethod
    def _audio_cancel(recorder: Any | None) -> bool:
        if recorder is None:
            return False
        try:
            return bool(recorder.cancel())
        except AudioCaptureError:
            LOGGER.warning("microphone_cancel_failed")
            return False

    def _session_is_current_locked(self, session_id: int) -> bool:
        return not self._closed and self.state.session_id == session_id

    def _job_is_active_locked(self, session_id: int) -> bool:
        return (
            not self._closed
            and self.state.session_id == session_id
            and self.state.active
            and self._session_cancelled is not None
            and not self._session_cancelled.is_set()
        )

    def _result_is_current_locked(self, session_id: int) -> bool:
        return (
            not self._closed
            and self.state.session_id == session_id
            and not self.state.active
            and self.state.result is not None
            and self._session_cancelled is not None
            and not self._session_cancelled.is_set()
        )

    def _session_is_current(self, session_id: int) -> bool:
        with self._lock:
            return self._session_is_current_locked(session_id)

    def _job_is_active(self, session_id: int) -> bool:
        with self._lock:
            return self._job_is_active_locked(session_id)

    def _result_is_current(self, session_id: int) -> bool:
        with self._lock:
            return self._result_is_current_locked(session_id)

    def _delivery_cancelled(self, job: _TranscriptionJob) -> bool:
        if job.cancelled.is_set():
            return True
        with self._lock:
            return (
                self._closed
                or self.state.session_id != job.session_id
                or self._session_cancelled is not job.cancelled
                or not self._result_is_current_locked(job.session_id)
            )

    def _report_failure(
        self,
        session_id: int,
        *,
        state_text: str,
        error_message: str,
        notification_message: str | None = None,
    ) -> bool:
        """Publish an error only for the still-current, open session."""

        with self._lock:
            if not self._job_is_active_locked(session_id):
                return False
            self.state = self.state.fail(session_id, error_message).reset()
        if self._session_is_current(session_id):
            self.status_callback(state_text, "error")
        if self._session_is_current(session_id):
            self.notification_callback(
                "Pressay",
                notification_message or error_message,
                True,
            )
        return True

    def _disable_translation_after_load_failure(
        self,
        job: _TranscriptionJob,
        model_size: str,
        exc: Exception,
    ) -> None:
        """Fail translation closed while keeping the current dictation usable."""

        with self._lock:
            current = (
                not self._closed
                and self.translating
                and self.config.voice_translate
                and self._translation_generation == job.translation_generation
                and self.config.model == job.config.model
                and self.config.translate_model == model_size
            )
            if current:
                self.translating = False
                self._translation_generation += 1
        self._dispose_translator()
        if not current:
            LOGGER.debug(
                "stale_translation_model_load_failure: %s", type(exc).__name__
            )
            return
        message = (
            f"Модель перевода {model_size!r} не загрузилась. "
            "Режим перевода выключен; эта фраза распознаётся без перевода. "
            f"{_setup_recovery_instruction(model_size)}"
        )
        if self._job_is_active(job.session_id):
            self.status_callback("Перевод выключен: модель не готова", "warning")
        if self._job_is_active(job.session_id):
            self.notification_callback("Pressay", message, True)
        LOGGER.warning("translation_model_load_failed: %s", type(exc).__name__)

    def _complete_translation_command(
        self, job: _TranscriptionJob, command: str
    ) -> bool:
        enabled = command == "on"
        with self._lock:
            if not self._job_is_active_locked(job.session_id):
                return False
            ignored = enabled and not self.config.voice_translate
            if not ignored:
                self.translating = enabled
                if enabled:
                    queued = self._queue_translation_warmup_locked()
                    if not queued:
                        self._translation_generation += 1
                else:
                    self._translation_generation += 1
            accepted = self.state.accept_result(job.session_id, "")
            if accepted is self.state:
                return False
            self.state = accepted.reset()
            if self._session_cancelled is job.cancelled:
                self._session_cancelled = None

        if not self._session_is_current(job.session_id):
            return False
        if ignored:
            self.status_callback(self._ready_status_text(), "ready")
            self._schedule_model_retirement()
            return True
        if enabled:
            status_text = "Перевод на английский включён — → EN"
            notification_text = "Перевод на английский включён."
        else:
            status_text = "Перевод выключен — обычная диктовка"
            notification_text = "Перевод на английский выключен."
        self.status_callback(status_text, "success")
        if self._session_is_current(job.session_id):
            self.notification_callback("Pressay", notification_text, False)
        self._schedule_model_retirement()
        return True

    def _transcribe_worker(self, job: _TranscriptionJob) -> None:
        with self._lock:
            if not self._job_is_active_locked(job.session_id):
                return
        try:
            transcribe_options: dict[str, Any] = {"language": job.config.language}
            prompt = _initial_prompt(job.config.replacements)
            if prompt is not None:
                transcribe_options["initial_prompt"] = prompt
            if job.vad_used is not None:
                transcribe_options["vad_filter"] = job.vad_used
            with self._lock:
                translating = job.translating and self.translating
            task = "translate" if translating else "transcribe"
            if translating and job.config.model in _TRANSLATION_INCAPABLE_MODELS:
                try:
                    selected_transcriber = self._ensure_translator(
                        job.config.translate_model
                    )
                except Exception as exc:
                    self._disable_translation_after_load_failure(
                        job, job.config.translate_model, exc
                    )
                    if not self._job_is_active(job.session_id):
                        return
                    task = "transcribe"
                    selected_transcriber = self._ensure_transcriber(job.config.model)
                    result = selected_transcriber.transcribe(
                        job.audio,
                        **transcribe_options,
                    )
                else:
                    try:
                        result = selected_transcriber.transcribe(
                            job.audio,
                            task="translate",
                            **transcribe_options,
                        )
                    except ModelLoadError as exc:
                        self._disable_translation_after_load_failure(
                            job, job.config.translate_model, exc
                        )
                        if not self._job_is_active(job.session_id):
                            return
                        task = "transcribe"
                        result = self._ensure_transcriber(job.config.model).transcribe(
                            job.audio,
                            **transcribe_options,
                        )
            else:
                selected_transcriber = self._ensure_transcriber(job.config.model)
                if translating:
                    result = selected_transcriber.transcribe(
                        job.audio,
                        task="translate",
                        **transcribe_options,
                    )
                else:
                    result = selected_transcriber.transcribe(
                        job.audio,
                        **transcribe_options,
                    )
            postprocess_started = time.perf_counter()
            processed = process_transcript(
                result.text,
                remove_fillers=job.config.remove_fillers,
                replacements=job.config.replacements,
                snippets=job.config.snippets,
                voice_press_enter=job.config.voice_press_enter,
                voice_formatting=job.config.voice_formatting,
                voice_translate=job.config.voice_translate,
            )
            postprocess_seconds = time.perf_counter() - postprocess_started
            if (
                not processed.text
                and not processed.press_enter
                and processed.translation_mode is None
            ):
                raise NoSpeechDetected("Речь не обнаружена")
        except (NoSpeechDetected, TranscriptionError) as exc:
            reported = self._report_failure(
                job.session_id,
                state_text="Не удалось распознать",
                error_message=str(exc),
            )
            if reported:
                LOGGER.warning("transcription_failed: %s", type(exc).__name__)
            else:
                LOGGER.debug("stale_transcription_failure: %s", type(exc).__name__)
            return
        except Exception as exc:
            reported = self._report_failure(
                job.session_id,
                state_text="Внутренняя ошибка",
                error_message="Внутренняя ошибка",
                notification_message=f"{type(exc).__name__}: {exc}",
            )
            if reported:
                LOGGER.exception("unexpected_transcription_failure")
            else:
                LOGGER.debug("stale_unexpected_transcription_failure: %s", type(exc).__name__)
            return

        timings = getattr(result, "timings", None)
        LOGGER.info(
            "transcription_completed language=%s device=%s compute=%s "
            "audio_seconds=%.3f vad_used=%s load_seconds=%.3f inference_seconds=%.3f "
            "total_seconds=%.3f characters=%d task=%s language_choice=%s",
            getattr(result, "language", "unknown"),
            getattr(result, "device", "unknown"),
            getattr(result, "compute_type", "unknown"),
            float(getattr(result, "audio_duration_seconds", 0.0) or 0.0),
            job.vad_used is not False,
            float(getattr(timings, "model_load_seconds", 0.0) or 0.0),
            float(getattr(timings, "inference_seconds", 0.0) or 0.0),
            float(getattr(timings, "total_seconds", 0.0) or 0.0),
            len(processed.text),
            task,
            getattr(result, "language_choice", "unknown"),
        )

        if processed.translation_mode is not None:
            self._complete_translation_command(job, processed.translation_mode)
            return

        with self._lock:
            if not self._job_is_active_locked(job.session_id):
                return
            accepted = self.state.accept_result(job.session_id, processed.text)
            if accepted is self.state:
                return
            self.state = accepted
            self.last_transcript = processed.text

        insertion_timing = [0.0]
        try:
            if not self._result_is_current(job.session_id):
                return
            self.result_callback(processed.text)
            if not self._result_is_current(job.session_id):
                return
            self._deliver(
                processed.text,
                job.target,
                press_enter=processed.press_enter,
                auto_insert=job.config.auto_insert,
                smart_spacing=job.config.smart_spacing,
                session_id=job.session_id,
                cancelled=lambda: self._delivery_cancelled(job),
                display_only=job.display_only,
                insertion_timing=insertion_timing,
            )
        finally:
            pipeline_seconds = time.perf_counter() - job.released_at
            with self._lock:
                if self._result_is_current_locked(job.session_id):
                    self.state = self.state.reset()
                if self._session_cancelled is job.cancelled:
                    self._session_cancelled = None
            LOGGER.info(
                "dictation_pipeline_completed session=%d audio_finalize_seconds=%.3f "
                "post_release_seconds=%.3f stream_stop_seconds=%.3f "
                "assemble_seconds=%.3f first_frame_latency_seconds=%.3f prearmed=%s "
                "postprocess_seconds=%.3f "
                "insertion_seconds=%.3f",
                job.session_id,
                job.audio_finalize_seconds,
                pipeline_seconds,
                float(job.finalize_breakdown.get("stream_stop_seconds", 0.0)),
                float(job.finalize_breakdown.get("assemble_seconds", 0.0)),
                float(job.finalize_breakdown.get("first_frame_latency_seconds", 0.0)),
                job.prearmed,
                postprocess_seconds,
                insertion_timing[0],
            )
            self._schedule_model_retirement()

    def _deliver(
        self,
        text: str,
        target: Any | None,
        *,
        press_enter: bool,
        auto_insert: bool,
        smart_spacing: bool,
        session_id: int,
        cancelled: Callable[[], bool],
        display_only: bool,
        insertion_timing: list[float],
    ) -> None:
        """Deliver without holding ``_lock``, checking invalidation between effects."""

        if cancelled() or not self._result_is_current(session_id):
            return
        if display_only:
            self.status_callback("Готово — текст ниже", "success")
            return
        if not auto_insert:
            if cancelled() or not self._result_is_current(session_id):
                return
            self.status_callback("Готово — текст ниже", "success")
            return
        try:
            send_text = input_adapter().send_text

            insertion_text = _prepare_insertion_text(
                text,
                press_enter=press_enter,
                smart_spacing=smart_spacing,
            )
            insertion_started = time.perf_counter()
            try:
                outcome = send_text(
                    insertion_text,
                    expected_target=target,
                    press_enter=press_enter,
                    strict_editable_check=self.config.strict_editable_check,
                    cancelled=cancelled,
                    # Automatic delivery never overwrites the user's clipboard on
                    # failure. The transcript is already retained in memory/UI;
                    # copying remains an explicit hotkey/button action.
                    fallback_to_clipboard=False,
                )
            finally:
                insertion_timing[0] = time.perf_counter() - insertion_started
        except Exception as exc:
            LOGGER.warning("insertion_failed: %s", type(exc).__name__)
            if cancelled() or not self._result_is_current(session_id):
                return
            self.status_callback("Не вставлено — текст сохранён ниже", "warning")
            if cancelled() or not self._result_is_current(session_id):
                return
            self.notification_callback(
                "Pressay",
                "Автовставка не сработала; текст сохранён в окне Pressay. "
                + self._copy_hint_sentence(),
                True,
            )
            return
        status_value = getattr(getattr(outcome, "status", None), "value", None)
        LOGGER.info(
            "insertion_outcome success=%s status=%s reason=%s method=%s "
            "characters_sent=%d target_present=%s",
            bool(getattr(outcome, "success", False)),
            status_value or getattr(outcome, "status", "unknown"),
            getattr(outcome, "reason", None),
            getattr(outcome, "method", None),
            int(getattr(outcome, "characters_sent", 0) or 0),
            target is not None,
        )
        if bool(getattr(outcome, "success", False)):
            if cancelled() or not self._result_is_current(session_id):
                return
            self.status_callback("Вставлено", "success")
        else:
            if cancelled() or not self._result_is_current(session_id):
                return
            reason = str(getattr(outcome, "reason", "Целевое окно изменилось"))
            if cancelled() or not self._result_is_current(session_id):
                return
            self.status_callback(
                _insertion_status_text(reason, self.config.hotkeys), "warning"
            )
            if cancelled() or not self._result_is_current(session_id):
                return
            self.notification_callback("Pressay", reason, True)

    @staticmethod
    def _copy_text(text: str) -> Any:
        return input_adapter().copy_text(text)

    def _report_last_transcript_feedback(
        self,
        text: str,
        status_text: str,
        status: str,
        *,
        notification_text: str | None = None,
        warning: bool = False,
    ) -> None:
        """Publish recovery feedback only while its transcript is still current."""

        if not self._last_transcript_is_current(text):
            return
        self.status_callback(status_text, status)
        if (
            notification_text is not None
            and self._last_transcript_is_current(text)
        ):
            self.notification_callback(
                "Pressay",
                notification_text,
                warning,
            )

    def paste_last(self) -> bool:
        with self._lock:
            if self._closed or not self.last_transcript:
                return False
            text = self.last_transcript
            strict_editable_check = self.config.strict_editable_check
            bindings = self.config.hotkeys
        if not self._last_transcript_is_current(text):
            return False
        try:
            outcome = input_adapter().paste_last(
                text,
                strict_editable_check=strict_editable_check,
                cancelled=lambda: not self._last_transcript_is_current(text),
            )
        except Exception as exc:
            # Paste is a temporary clipboard transaction. An unexpected
            # backend/COM failure must not turn it into a destructive implicit
            # copy; copying is exclusively the explicit copy_last action.
            LOGGER.warning("paste_last_failed: %s", type(exc).__name__)
            self._report_last_transcript_feedback(
                text,
                "Не вставлено — текст сохранён ниже",
                "warning",
                notification_text=(
                    "Не удалось вставить последнюю расшифровку. "
                    "Текст сохранён в окне Pressay. " + self._copy_hint_sentence()
                ),
                warning=True,
            )
            return False
        success = bool(getattr(outcome, "success", False))
        if success:
            self._report_last_transcript_feedback(text, "Вставлено", "success")
            return True

        copied = bool(getattr(outcome, "copied", False))
        reason = str(getattr(outcome, "reason", "input_failed"))
        if copied:
            self._report_last_transcript_feedback(
                text,
                "Не вставлено — текст скопирован",
                "warning",
                notification_text=(
                    "Не удалось вставить последнюю расшифровку, но текст "
                    "скопирован в буфер обмена. Вставьте его вручную."
                ),
                warning=True,
            )
        else:
            self._report_last_transcript_feedback(
                text,
                _insertion_status_text(reason, bindings),
                "warning",
                notification_text=(
                    "Не удалось вставить последнюю расшифровку. "
                    "Текст сохранён в окне Pressay. " + self._copy_hint_sentence()
                ),
                warning=True,
            )
        return False

    def copy_last(self) -> bool:
        with self._lock:
            if self._closed or not self.last_transcript:
                return False
            text = self.last_transcript
        if not self._last_transcript_is_current(text):
            return False
        try:
            outcome = self._copy_text(text)
        except Exception as exc:
            LOGGER.warning("copy_last_failed: %s", type(exc).__name__)
            self._report_last_transcript_feedback(
                text,
                "Не скопировано — текст сохранён ниже",
                "warning",
                notification_text=(
                    "Не удалось скопировать последнюю расшифровку. "
                    "Текст сохранён в окне Pressay."
                ),
                warning=True,
            )
            return False
        success = bool(getattr(outcome, "success", False))
        if success:
            self._report_last_transcript_feedback(text, "Скопировано", "success")
            return True
        self._report_last_transcript_feedback(
            text,
            "Не скопировано — текст сохранён ниже",
            "warning",
            notification_text=(
                "Не удалось скопировать последнюю расшифровку. "
                "Текст сохранён в окне Pressay."
            ),
            warning=True,
        )
        return False

    def _last_transcript_is_current(self, text: str) -> bool:
        with self._lock:
            return not self._closed and self.last_transcript == text

    def update_config(self, config: AppConfig) -> None:
        with self._warmup_status_gate:
            with self._lock:
                if self._closed:
                    return
                model_changed = config.model != self.config.model
                translate_model_changed = (
                    config.translate_model != self.config.translate_model
                )
                voice_translate_disabled = (
                    self.config.voice_translate and not config.voice_translate
                )
                translation_disabled = self.translating and not config.voice_translate
                resource_mode_changed = config.resource_mode != self.config.resource_mode
                translator_redundant = (
                    model_changed
                    and config.model not in _TRANSLATION_INCAPABLE_MODELS
                )
                translator_cleanup_queued = (
                    voice_translate_disabled or translator_redundant
                )
                self.config = config
                if translation_disabled:
                    self.translating = False
                if resource_mode_changed:
                    self._cancel_model_retirement_locked()
                if translator_cleanup_queued:
                    self._executor.submit(self._dispose_translator)
                if model_changed:
                    # Queued behind any active inference; never release native
                    # model resources from the GUI thread. Once startup
                    # preloading is enabled, a settings change retires and warms
                    # the new model in that same serialized queue.
                    self._model_generation += 1
                    generation = self._model_generation
                    if self._preload_enabled:
                        self._warmup_future = self._executor.submit(
                            self._warmup_worker,
                            config.model,
                            generation,
                        )
                    else:
                        self._executor.submit(self._dispose_transcriber)
                elif resource_mode_changed:
                    self._schedule_model_retirement()

                if model_changed or translate_model_changed or translation_disabled:
                    self._translation_generation += 1
                    if (
                        self.translating
                        and config.model in _TRANSLATION_INCAPABLE_MODELS
                    ):
                        self._queue_translation_warmup_locked()
                    elif translate_model_changed and not translator_cleanup_queued:
                        self._executor.submit(self._dispose_translator)

    def close(self) -> None:
        with self._warmup_status_gate:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                # Invalidate capture/session tokens before any native cleanup.
                # A blocked start/stop can finish later, but can never publish
                # a result, submit ASR, or deliver text after this point.
                self._capture_generation += 1
                self._model_generation += 1
                self._translation_generation += 1
                self._preload_enabled = False
                self.translating = False
                self._cancel_model_retirement_locked()
                self._cancel_prepared_timeout_locked()
                if self._session_cancelled is not None:
                    self._session_cancelled.set()
                recorder = self._recorder
                self._capture_intent = _CaptureIntent.CLOSED
                # Retaining the id while resetting makes every in-flight result
                # stale. Clearing the text also prevents copy/paste after close.
                self.state = self.state.reset()
                self.last_transcript = ""
                self.target = None
                self._recorder = None
                self._recording_prearmed = False
                # This cleanup runs after an active worker (and after any
                # already queued model retirement), so close() stays
                # non-blocking without racing native inference resources.
                self._executor.submit(self._close_worker)
                if recorder is not None:
                    self._queue_audio(
                        _AudioCommand(
                            action="cancel",
                            generation=self._capture_generation,
                            recorder=recorder,
                        )
                    )
                self._queue_audio(
                    _AudioCommand(
                        action="close",
                        generation=self._capture_generation,
                    )
                )
        self._executor.shutdown(wait=False, cancel_futures=False)

    def _close_worker(self) -> None:
        try:
            self._dispose_models()
        finally:
            self._asr_close_complete.set()
            self._maybe_signal_close_complete()

    def _maybe_signal_close_complete(self) -> None:
        if self._asr_close_complete.is_set() and self._audio_close_complete.is_set():
            self._close_complete.set()

    def wait_closed(self, timeout: float | None = None) -> bool:
        """Wait for serialized native cleanup; intended for shutdown watchdogs."""

        return self._close_complete.wait(timeout)
