from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Callable

import pytest

from pressay.audio import AudioDeviceError
from pressay.microphone_probe import (
    MicrophoneProbeCoordinator,
    MicrophoneProbeResult,
)


class _FakeRecorder:
    def __init__(
        self,
        *,
        rms: float = 0.0,
        peak: float = 0.0,
        stream_failed: bool = False,
        close_ok: bool = True,
    ) -> None:
        self.silence_rms_threshold = 0.0003
        self._rms = rms
        self._peak = peak
        self._stream_failed = stream_failed
        self._close_ok = close_ok
        self.is_recording = False
        self.started = threading.Event()
        self.cancel_calls = 0
        self.stop_calls: list[bool] = []
        self.wait_closed_calls = 0
        self.wait_closed_timeouts: list[float | None] = []

    @property
    def current_rms(self) -> float:
        return self._rms

    @property
    def current_peak(self) -> float:
        return self._peak

    def start(self) -> int:
        self.is_recording = True
        self.started.set()
        return 48_000

    def wait_for_stop_signal(self, _timeout: float = 0.0) -> bool:
        return self._stream_failed

    def stop(self, *, validate: bool) -> SimpleNamespace:
        self.stop_calls.append(validate)
        self.is_recording = False
        return SimpleNamespace(rms=self._rms, peak=self._peak)

    def cancel(self) -> bool:
        self.cancel_calls += 1
        self.is_recording = False
        return True

    def wait_closed(self, timeout: float | None = None) -> bool:
        self.wait_closed_calls += 1
        self.wait_closed_timeouts.append(timeout)
        return self._close_ok


def _elapsed_probe_clock() -> Callable[[], float]:
    values = iter((0.0, 3.0))
    return lambda: next(values)


@pytest.mark.parametrize(
    ("rms", "expected_signal", "expected_error"),
    (
        (0.01, True, None),
        (0.0, False, "silent"),
    ),
)
def test_probe_uses_bounded_capture_and_existing_silence_threshold(
    rms: float,
    expected_signal: bool,
    expected_error: str | None,
) -> None:
    recorder = _FakeRecorder(rms=rms, peak=max(rms, 0.02))
    levels: list[tuple[float, float]] = []
    results: list[MicrophoneProbeResult] = []
    completed = threading.Event()
    coordinator = MicrophoneProbeCoordinator(
        lambda callback: callback(),
        recorder_factory=lambda **_kwargs: recorder,  # type: ignore[arg-type]
        duration_seconds=2.5,
        monotonic=_elapsed_probe_clock(),
    )

    assert coordinator.start(
        "selected-device",
        on_level=lambda current_rms, peak: levels.append((current_rms, peak)),
        on_complete=lambda result: (results.append(result), completed.set()),
    )
    assert completed.wait(timeout=1)

    assert levels == [(rms, max(rms, 0.02))]
    assert recorder.stop_calls == [False]
    assert recorder.wait_closed_calls == 1
    assert recorder.wait_closed_timeouts == [1.0]
    assert results[0].signal_detected is expected_signal
    assert results[0].error_kind == expected_error
    assert results[0].peak_rms == pytest.approx(rms)


def test_probe_maps_device_and_stream_failures_without_exposing_exception_text() -> None:
    device_results: list[MicrophoneProbeResult] = []
    device_done = threading.Event()

    def unavailable(**_kwargs: object) -> _FakeRecorder:
        raise AudioDeviceError("private driver detail")

    device_probe = MicrophoneProbeCoordinator(
        lambda callback: callback(),
        recorder_factory=unavailable,  # type: ignore[arg-type]
        duration_seconds=2.5,
    )
    assert device_probe.start(
        None,
        on_level=lambda *_args: None,
        on_complete=lambda result: (device_results.append(result), device_done.set()),
    )
    assert device_done.wait(timeout=1)
    assert device_results[0].error_kind == "device"

    stream_recorder = _FakeRecorder(rms=0.1, peak=0.2, stream_failed=True)
    stream_results: list[MicrophoneProbeResult] = []
    stream_done = threading.Event()
    stream_probe = MicrophoneProbeCoordinator(
        lambda callback: callback(),
        recorder_factory=lambda **_kwargs: stream_recorder,  # type: ignore[arg-type]
        duration_seconds=2.5,
    )
    assert stream_probe.start(
        None,
        on_level=lambda *_args: None,
        on_complete=lambda result: (stream_results.append(result), stream_done.set()),
    )
    assert stream_done.wait(timeout=1)
    assert stream_results[0].error_kind == "stream"
    assert stream_results[0].signal_detected is False
    assert stream_recorder.stop_calls == [False]


def test_probe_is_single_flight_and_shutdown_cancels_off_the_caller_thread() -> None:
    recorder = _FakeRecorder(rms=0.01, peak=0.02)
    callback_threads: list[int] = []
    coordinator = MicrophoneProbeCoordinator(
        lambda callback: callback(),
        recorder_factory=lambda **_kwargs: recorder,  # type: ignore[arg-type]
        duration_seconds=2.5,
    )
    caller_thread = threading.get_ident()

    assert coordinator.start(
        None,
        on_level=lambda *_args: callback_threads.append(threading.get_ident()),
        on_complete=lambda _result: pytest.fail("shutdown published completion"),
    )
    assert recorder.started.wait(timeout=1)
    assert coordinator.start(
        None,
        on_level=lambda *_args: None,
        on_complete=lambda _result: None,
    ) is False

    shutdown_complete = coordinator.shutdown()

    assert shutdown_complete.wait(timeout=1)
    assert recorder.cancel_calls == 1
    assert recorder.wait_closed_calls == 1
    assert callback_threads
    assert all(thread_id != caller_thread for thread_id in callback_threads)


def test_probe_shutdown_stays_incomplete_when_native_close_is_unconfirmed() -> None:
    recorder = _FakeRecorder(rms=0.01, peak=0.02, close_ok=False)
    coordinator = MicrophoneProbeCoordinator(
        lambda callback: callback(),
        recorder_factory=lambda **_kwargs: recorder,  # type: ignore[arg-type]
        duration_seconds=2.5,
    )
    assert coordinator.start(
        None,
        on_level=lambda *_args: None,
        on_complete=lambda _result: None,
    )
    assert recorder.started.wait(timeout=1)

    shutdown_complete = coordinator.shutdown()
    deadline = time.monotonic() + 1
    while coordinator.running and time.monotonic() < deadline:
        time.sleep(0.01)

    assert coordinator.running is False
    assert shutdown_complete.is_set() is False
    assert recorder.cancel_calls == 1
    assert recorder.wait_closed_calls == 1


def test_queued_probe_callbacks_are_suppressed_after_shutdown() -> None:
    recorder = _FakeRecorder(rms=0.01, peak=0.02)
    queued: list[Callable[[], None]] = []
    observed: list[object] = []
    coordinator = MicrophoneProbeCoordinator(
        queued.append,
        recorder_factory=lambda **_kwargs: recorder,  # type: ignore[arg-type]
        duration_seconds=2.5,
        monotonic=_elapsed_probe_clock(),
    )
    assert coordinator.start(
        None,
        on_level=lambda *_args: observed.append("level"),
        on_complete=observed.append,
    )
    deadline = time.monotonic() + 1
    while coordinator.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert coordinator.running is False

    assert coordinator.shutdown().is_set()
    for callback in queued:
        callback()
    assert observed == []


def test_queued_callbacks_from_previous_probe_are_suppressed_by_successor() -> None:
    recorders = iter(
        (
            _FakeRecorder(rms=0.01, peak=0.02),
            _FakeRecorder(rms=0.02, peak=0.03),
        )
    )
    clock_values = iter((0.0, 3.0, 10.0, 13.0))
    queued: list[Callable[[], None]] = []
    observed: list[str] = []
    coordinator = MicrophoneProbeCoordinator(
        queued.append,
        recorder_factory=lambda **_kwargs: next(recorders),  # type: ignore[arg-type]
        duration_seconds=2.5,
        monotonic=lambda: next(clock_values),
    )

    assert coordinator.start(
        None,
        on_level=lambda *_args: observed.append("old-level"),
        on_complete=lambda _result: observed.append("old-complete"),
    )
    deadline = time.monotonic() + 1
    while coordinator.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert coordinator.start(
        None,
        on_level=lambda *_args: observed.append("new-level"),
        on_complete=lambda _result: observed.append("new-complete"),
    )
    deadline = time.monotonic() + 1
    while coordinator.running and time.monotonic() < deadline:
        time.sleep(0.01)

    for callback in queued:
        callback()

    assert observed == ["new-level", "new-complete"]


def test_thread_factory_constructor_failure_rolls_back_single_flight_gate() -> None:
    def broken_thread_factory(**_kwargs: object) -> threading.Thread:
        raise RuntimeError("thread unavailable")

    coordinator = MicrophoneProbeCoordinator(
        lambda callback: callback(),
        thread_factory=broken_thread_factory,
        duration_seconds=2.5,
    )

    assert coordinator.start(
        None,
        on_level=lambda *_args: None,
        on_complete=lambda _result: None,
    ) is False
    assert coordinator.running is False
    assert coordinator.shutdown().is_set()
