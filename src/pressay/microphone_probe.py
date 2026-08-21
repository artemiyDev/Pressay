"""Bounded, local-only microphone signal probe."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Callable

from .audio import (
    AudioCaptureError,
    AudioDeviceError,
    AudioRecorder,
    AudioStreamError,
)


LOGGER = logging.getLogger(__name__)
_NATIVE_CLOSE_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class MicrophoneProbeResult:
    """Scalar-only outcome; no captured samples leave the worker."""

    signal_detected: bool
    sample_rate: int | None
    rms: float
    peak: float
    peak_rms: float
    error_kind: str | None = None


class MicrophoneProbeCoordinator:
    """Own one short signal probe and its native-close shutdown barrier."""

    def __init__(
        self,
        dispatch_ui: Callable[..., None],
        *,
        recorder_factory: Callable[..., AudioRecorder] = AudioRecorder,
        duration_seconds: float = 2.5,
        poll_interval_seconds: float = 0.05,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 2.0 <= duration_seconds <= 3.0:
            raise ValueError("duration_seconds must be between 2 and 3 seconds")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._dispatch_ui = dispatch_ui
        self._recorder_factory = recorder_factory
        self._duration_seconds = float(duration_seconds)
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._thread_factory = thread_factory
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._generation = 0
        self._active_token: int | None = None
        self._running = False
        self._closed = False
        self._native_close_ok = True
        self._cancel_event: threading.Event | None = None
        self._worker: threading.Thread | None = None
        self._shutdown_complete = threading.Event()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def start(
        self,
        device: int | str | None,
        *,
        on_level: Callable[[float, float], None],
        on_complete: Callable[[MicrophoneProbeResult], None],
    ) -> bool:
        """Start one probe, rejecting concurrent or post-shutdown requests."""

        with self._lock:
            if self._closed or self._running or not self._native_close_ok:
                return False
            self._generation += 1
            token = self._generation
            cancel_event = threading.Event()
            self._active_token = token
            self._cancel_event = cancel_event
            self._running = True
        try:
            thread = self._thread_factory(
                target=self._run,
                args=(token, cancel_event, device, on_level, on_complete),
                name="pressay-microphone-probe",
                daemon=True,
            )
            with self._lock:
                if self._active_token == token:
                    self._worker = thread
            thread.start()
        except Exception:
            LOGGER.exception("microphone_probe_thread_start_failed")
            with self._lock:
                if self._active_token == token:
                    self._active_token = None
                    self._cancel_event = None
                    self._worker = None
                    self._running = False
                shutdown_safe = self._closed and self._native_close_ok
            if shutdown_safe:
                self._shutdown_complete.set()
            return False
        return True

    def shutdown(self) -> threading.Event:
        """Close the submission gate and interrupt the worker without waiting."""

        with self._lock:
            if self._closed:
                return self._shutdown_complete
            self._closed = True
            self._generation += 1
            cancel_event = self._cancel_event
            running = self._running
            native_close_ok = self._native_close_ok
        if cancel_event is not None:
            cancel_event.set()
        if not running and native_close_ok:
            self._shutdown_complete.set()
        return self._shutdown_complete

    @staticmethod
    def _error_kind(exc: BaseException) -> str:
        if isinstance(exc, AudioDeviceError):
            return "device"
        if isinstance(exc, AudioStreamError):
            return "stream"
        if isinstance(exc, AudioCaptureError):
            return "capture"
        return "internal"

    def _publish(
        self,
        token: int,
        callback: Callable[..., None],
        *args: Any,
    ) -> None:
        def invoke_if_current() -> None:
            with self._lock:
                current = not self._closed and token == self._generation
            if current:
                callback(*args)

        try:
            self._dispatch_ui(invoke_if_current)
        except Exception:
            LOGGER.exception("microphone_probe_dispatch_failed")

    @staticmethod
    def _wait_for_native_close(recorder: AudioRecorder) -> bool:
        try:
            # A broken PortAudio driver must not leave the settings UI stuck
            # on "checking" forever. False is fail-closed: the probe reports
            # a stream error, refuses another probe, and application shutdown
            # still keeps the native worker under its hard deadline.
            return (
                recorder.wait_closed(timeout=_NATIVE_CLOSE_TIMEOUT_SECONDS)
                is True
            )
        except Exception:
            LOGGER.exception("microphone_probe_close_wait_failed")
            return False

    @classmethod
    def _cancel_recorder(cls, recorder: AudioRecorder) -> bool:
        cancel_ok = True
        try:
            if recorder.is_recording:
                cancel_ok = recorder.cancel() is True
        except Exception:
            LOGGER.exception("microphone_probe_cancel_failed")
            cancel_ok = False
        return cls._wait_for_native_close(recorder) and cancel_ok

    def _run(
        self,
        token: int,
        cancel_event: threading.Event,
        device: int | str | None,
        on_level: Callable[[float, float], None],
        on_complete: Callable[[MicrophoneProbeResult], None],
    ) -> None:
        recorder: AudioRecorder | None = None
        result: MicrophoneProbeResult | None = None
        native_close_ok = True
        sample_rate: int | None = None
        max_rms = 0.0
        latest_peak = 0.0
        stream_failed = False
        stopped = False
        try:
            recorder = self._recorder_factory(device=device)
            if cancel_event.is_set():
                native_close_ok = self._wait_for_native_close(recorder)
                return
            sample_rate = int(recorder.start())
            deadline = self._monotonic() + self._duration_seconds
            while True:
                rms = max(0.0, float(recorder.current_rms))
                latest_peak = max(0.0, float(recorder.current_peak))
                max_rms = max(max_rms, rms)
                self._publish(token, on_level, rms, latest_peak)
                if recorder.wait_for_stop_signal(0.0):
                    stream_failed = True
                    break
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    break
                if cancel_event.wait(min(self._poll_interval_seconds, remaining)):
                    break

            if cancel_event.is_set():
                native_close_ok = self._cancel_recorder(recorder)
                return

            recording = recorder.stop(validate=False)
            stopped = True
            native_close_ok = self._wait_for_native_close(recorder)
            max_rms = max(max_rms, float(recording.rms))
            latest_peak = max(latest_peak, float(recording.peak))
            if not native_close_ok or stream_failed:
                result = MicrophoneProbeResult(
                    False,
                    sample_rate,
                    float(recording.rms),
                    latest_peak,
                    max_rms,
                    "stream",
                )
            else:
                signal_detected = max_rms >= recorder.silence_rms_threshold
                result = MicrophoneProbeResult(
                    signal_detected,
                    sample_rate,
                    float(recording.rms),
                    latest_peak,
                    max_rms,
                    None if signal_detected else "silent",
                )
        except Exception as exc:
            error_kind = self._error_kind(exc)
            if recorder is not None:
                if not stopped:
                    native_close_ok = self._cancel_recorder(recorder)
                else:
                    native_close_ok = self._wait_for_native_close(recorder)
                if not native_close_ok:
                    error_kind = "stream"
            result = MicrophoneProbeResult(
                False,
                sample_rate,
                0.0,
                latest_peak,
                max_rms,
                error_kind,
            )
            LOGGER.warning("microphone_probe_failed: %s", type(exc).__name__)
        finally:
            with self._lock:
                if self._active_token == token:
                    self._active_token = None
                    self._cancel_event = None
                    self._worker = None
                    self._running = False
                if not native_close_ok:
                    self._native_close_ok = False
                closed = self._closed
                shutdown_safe = self._native_close_ok
            if closed and shutdown_safe:
                self._shutdown_complete.set()
            if result is not None and not cancel_event.is_set():
                self._publish(token, on_complete, result)
