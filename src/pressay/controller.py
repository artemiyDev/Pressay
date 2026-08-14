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
from .transcriber import FasterWhisperTranscriber, NoSpeechDetected, TranscriptionError
from .platform_support import hotkey_hint, input_adapter, is_macos


LOGGER = logging.getLogger(__name__)
_SETUP_MODELS = frozenset({"small", "medium", "turbo", "large-v3"})
_MODEL_RETIRE_SECONDS: dict[str, float | None] = {
    "instant": None,
    "balanced": 300.0,
    "eco": 0.0,
}


def _setup_command(model_size: str) -> str:
    if is_macos():
        model_argument = f" --model {model_size}" if model_size in _SETUP_MODELS else ""
        return f"bash scripts/setup-macos.sh{model_argument}"
    return (
        f".\\scripts\\setup.ps1 -Model {model_size}"
        if model_size in _SETUP_MODELS
        else ".\\scripts\\setup.ps1"
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
        return "Не вставлено: курсор не в поле ввода"
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


class _CaptureIntent(str, Enum):
    """Controller-owned intent, independent of a native stream's timing."""

    IDLE = "idle"
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
    ) -> None:
        self.config = config
        self.status_callback = status_callback
        self.result_callback = result_callback
        self.notification_callback = notification_callback
        self.state = SessionState()
        self.last_transcript = ""
        self.target: Any | None = None
        self._recorder: AudioRecorder | None = None
        self._transcriber: FasterWhisperTranscriber | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pressay-asr")
        self._future: Future[Any] | None = None
        self._warmup_future: Future[Any] | None = None
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
        self._preload_enabled = False
        self._residency_generation = 0
        self._residency_timer: threading.Timer | None = None
        self._capture_generation = 0
        self._capture_intent = _CaptureIntent.IDLE
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

    def _schedule_model_retirement(self, resource_mode: str) -> None:
        delay = _MODEL_RETIRE_SECONDS.get(resource_mode)
        with self._lock:
            if self._closed:
                return
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
            self._executor.submit(self._dispose_transcriber)
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

    @staticmethod
    def _new_transcriber(model_size: str) -> FasterWhisperTranscriber:
        return FasterWhisperTranscriber(model_size=model_size)

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

    def _warmup_worker(self, model_size: str, generation: int) -> None:
        with self._warmup_status_gate:
            with self._lock:
                if not self._warmup_is_current_locked(model_size, generation):
                    return
                show_status = not self.state.active
            if show_status:
                self.status_callback("Подготавливаю локальную модель…", "processing")

        # A settings update may have invalidated this queued request before it
        # reached the executor. Do not load a model that is already obsolete.
        if not self._warmup_is_current(model_size, generation):
            return
        try:
            transcriber = self._ensure_transcriber(model_size)
            transcriber.warmup()
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
                    f"Запустите {setup_command} и перезапустите Pressay."
                )
                if not session_active:
                    self.status_callback(
                        f"Модель {model_size} не готова — запустите {setup_command}",
                        "error",
                    )
                self.notification_callback("Pressay", message, True)
            LOGGER.warning("model_warmup_failed: %s", type(exc).__name__)
            return

        with self._warmup_status_gate:
            with self._lock:
                current = self._warmup_is_current_locked(model_size, generation)
                session_active = self.state.active
            if current and not session_active:
                self.status_callback(self._ready_status_text(), "ready")
        if current:
            self._schedule_model_retirement(self.config.resource_mode)

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

    def _request_start_recording(self, *, target: Any | None) -> _AudioCommand | None:
        with self._warmup_status_gate:
            with self._lock:
                if (
                    self._closed
                    or self._capture_intent is not _CaptureIntent.IDLE
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
                self._recorder = None
                self._capture_generation += 1
                generation = self._capture_generation
                self._capture_intent = _CaptureIntent.STARTING
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

    def _audio_start(self, command: _AudioCommand) -> bool:
        session_id = command.session_id
        assert session_id is not None
        with self._lock:
            if not self._capture_is_current_locked(command.generation, session_id):
                return False

        recorder: Any | None = None
        try:
            # Construction is also off the caller thread because third-party
            # recorder fakes/backends are free to probe native state here.
            recorder = self._new_recorder()
            with self._lock:
                if not self._capture_is_current_locked(command.generation, session_id):
                    return False
                self._recorder = recorder
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
        if isinstance(exc, (AudioTooShortError, SilentAudioError)):
            return "Речь не обнаружена", "warning"
        if type(exc).__name__ == "AudioDurationLimitError":
            return "Лимит записи достигнут", "warning"
        if type(exc).__name__ == "AudioStreamError":
            return "Ошибка аудиопотока", "error"
        return "Ошибка записи", "error"

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
                    self.target = None
            if current and self._session_is_current(session_id):
                self.status_callback(text, status)
            if current and self._session_is_current(session_id):
                self.notification_callback("Pressay", str(exc), True)
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
            self.target = None
            self.state = self.state.begin_transcription(session_id)
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

    def _transcribe_worker(self, job: _TranscriptionJob) -> None:
        with self._lock:
            if not self._job_is_active_locked(job.session_id):
                return
        try:
            transcribe_options: dict[str, Any] = {"language": job.config.language}
            prompt = _initial_prompt(job.config.replacements)
            if prompt is not None:
                transcribe_options["initial_prompt"] = prompt
            result = self._ensure_transcriber(job.config.model).transcribe(
                job.audio,
                **transcribe_options,
            )
            processed = process_transcript(
                result.text,
                remove_fillers=job.config.remove_fillers,
                replacements=job.config.replacements,
                snippets=job.config.snippets,
                voice_press_enter=job.config.voice_press_enter,
            )
            if not processed.text and not processed.press_enter:
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
            "audio_seconds=%.3f load_seconds=%.3f inference_seconds=%.3f "
            "total_seconds=%.3f characters=%d",
            getattr(result, "language", "unknown"),
            getattr(result, "device", "unknown"),
            getattr(result, "compute_type", "unknown"),
            float(getattr(result, "audio_duration_seconds", 0.0) or 0.0),
            float(getattr(timings, "model_load_seconds", 0.0) or 0.0),
            float(getattr(timings, "inference_seconds", 0.0) or 0.0),
            float(getattr(timings, "total_seconds", 0.0) or 0.0),
            len(processed.text),
        )

        with self._lock:
            if not self._job_is_active_locked(job.session_id):
                return
            accepted = self.state.accept_result(job.session_id, processed.text)
            if accepted is self.state:
                return
            self.state = accepted
            self.last_transcript = processed.text

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
                "post_release_seconds=%.3f",
                job.session_id,
                job.audio_finalize_seconds,
                pipeline_seconds,
            )
            self._schedule_model_retirement(job.config.resource_mode)

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
            outcome = send_text(
                insertion_text,
                expected_target=target,
                press_enter=press_enter,
                cancelled=cancelled,
                # Automatic delivery never overwrites the user's clipboard on
                # failure. The transcript is already retained in memory/UI;
                # copying remains an explicit hotkey/button action.
                fallback_to_clipboard=False,
            )
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
    def _copy_text(text: str) -> None:
        try:
            input_adapter().copy_text(text)
        except Exception:
            LOGGER.exception("clipboard_copy_failed")

    def paste_last(self) -> bool:
        with self._lock:
            if self._closed or not self.last_transcript:
                return False
            text = self.last_transcript
        try:
            outcome = input_adapter().paste_last(
                text,
                cancelled=lambda: not self._last_transcript_is_current(text),
            )
        except Exception as exc:
            # Paste is a temporary clipboard transaction. An unexpected
            # backend/COM failure must not turn it into a destructive implicit
            # copy; copying is exclusively the explicit copy_last action.
            LOGGER.warning("paste_last_failed: %s", type(exc).__name__)
            if self._last_transcript_is_current(text):
                self.status_callback(
                    "Не вставлено — текст сохранён ниже",
                    "warning",
                )
            if self._last_transcript_is_current(text):
                self.notification_callback(
                    "Pressay",
                    "Не удалось вставить последнюю расшифровку. "
                    "Текст сохранён в окне Pressay. " + self._copy_hint_sentence(),
                    True,
                )
            return False
        return bool(getattr(outcome, "success", False))

    def copy_last(self) -> bool:
        with self._lock:
            if self._closed or not self.last_transcript:
                return False
            text = self.last_transcript
        if not self._last_transcript_is_current(text):
            return False
        self._copy_text(text)
        return True

    def _last_transcript_is_current(self, text: str) -> bool:
        with self._lock:
            return not self._closed and self.last_transcript == text

    def update_config(self, config: AppConfig) -> None:
        with self._warmup_status_gate:
            with self._lock:
                if self._closed:
                    return
                model_changed = config.model != self.config.model
                resource_mode_changed = config.resource_mode != self.config.resource_mode
                self.config = config
                if resource_mode_changed:
                    self._cancel_model_retirement_locked()
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
                    self._schedule_model_retirement(config.resource_mode)

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
                self._preload_enabled = False
                self._cancel_model_retirement_locked()
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
            self._dispose_transcriber()
        finally:
            self._asr_close_complete.set()
            self._maybe_signal_close_complete()

    def _maybe_signal_close_complete(self) -> None:
        if self._asr_close_complete.is_set() and self._audio_close_complete.is_set():
            self._close_complete.set()

    def wait_closed(self, timeout: float | None = None) -> bool:
        """Wait for serialized native cleanup; intended for shutdown watchdogs."""

        return self._close_complete.wait(timeout)
