"""Audio capture and small, dependency-light signal helpers.

``sounddevice`` is deliberately imported only when a device operation is
requested.  Importing the package (and running non-hardware unit tests) is
therefore possible on machines without PortAudio installed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
import time
from typing import Any
import unicodedata
from urllib.parse import parse_qs, urlencode

import numpy as np


TARGET_SAMPLE_RATE = 16_000
MICROPHONE_SELECTOR_PREFIX = "pressay:microphone:v1?"
LEGACY_MICROPHONE_SELECTOR_PREFIX = "whisperflow:microphone:v1?"
_UNRESOLVED_DEVICE = object()


class AudioCaptureError(RuntimeError):
    """Base error for microphone capture failures."""


class AudioDeviceError(AudioCaptureError):
    """The selected recording device is unavailable or unsupported."""


class AudioTooShortError(AudioCaptureError):
    """The recording did not reach the configured minimum duration."""


class SilentAudioError(AudioCaptureError):
    """The recording contains no signal above the configured RMS floor."""


class AudioDurationLimitError(AudioCaptureError):
    """Capture reached its configured duration bound and was discarded."""

    def __init__(
        self,
        *,
        max_duration_seconds: float,
        duration_seconds: float,
        captured_samples: int,
        source_sample_rate: int,
        status_messages: tuple[str, ...] = (),
    ) -> None:
        self.max_duration_seconds = float(max_duration_seconds)
        self.duration_seconds = float(duration_seconds)
        self.captured_samples = int(captured_samples)
        self.source_sample_rate = int(source_sample_rate)
        self.status_messages = tuple(status_messages)
        super().__init__(
            f"Recording reached the {self.max_duration_seconds:.2f}s duration limit"
        )


class AudioStreamError(AudioCaptureError):
    """Captured audio is incomplete because the input stream lost integrity."""

    def __init__(
        self,
        *,
        reason: str,
        status_messages: tuple[str, ...],
        captured_samples: int,
        source_rate: int,
    ) -> None:
        self.reason = str(reason)
        self.status_messages = tuple(status_messages)
        self.captured_samples = int(captured_samples)
        self.source_rate = int(source_rate)
        # Alias matches AudioRecording/AudioDurationLimitError terminology.
        self.source_sample_rate = self.source_rate
        super().__init__(f"Microphone stream integrity failed: {self.reason}")


@dataclass(frozen=True, slots=True)
class AudioDevice:
    """A sounddevice input device suitable for displaying in a picker."""

    index: int
    name: str
    default_sample_rate: int
    max_input_channels: int
    is_default: bool = False
    host_api: str = ""

    @property
    def stable_selector(self) -> str:
        """Return a restart-safe selector independent of PortAudio indexes."""

        return build_microphone_selector(
            name=self.name,
            host_api=self.host_api,
            sample_rate=self.default_sample_rate,
        )


def build_microphone_selector(*, name: str, host_api: str, sample_rate: int) -> str:
    """Build the persisted selector used to find a device after reordering."""

    return MICROPHONE_SELECTOR_PREFIX + urlencode(
        (
            ("name", str(name)),
            ("host_api", str(host_api)),
            ("sample_rate", str(int(sample_rate))),
        )
    )


def parse_microphone_selector(value: object) -> tuple[str, str, int] | None:
    """Decode a stable selector, returning ``None`` for legacy values."""

    if not isinstance(value, str):
        return None
    prefix = next(
        (
            item
            for item in (MICROPHONE_SELECTOR_PREFIX, LEGACY_MICROPHONE_SELECTOR_PREFIX)
            if value.startswith(item)
        ),
        None,
    )
    if prefix is None:
        return None
    try:
        values = parse_qs(
            value[len(prefix) :],
            keep_blank_values=True,
            strict_parsing=True,
        )
        name = values["name"][0]
        host_api = values["host_api"][0]
        sample_rate = int(values["sample_rate"][0])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if not name.strip() or sample_rate <= 0:
        return None
    return name, host_api, sample_rate


def _match_text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


@dataclass(frozen=True, slots=True)
class AudioRecording:
    """A completed mono recording, normalized for Whisper input."""

    audio: np.ndarray
    sample_rate: int
    source_sample_rate: int
    duration_seconds: float
    rms: float
    peak: float
    silent: bool
    status_messages: tuple[str, ...] = ()
    limit_reached: bool = False

    @property
    def samples(self) -> np.ndarray:
        """Alias useful to callers that name PCM arrays ``samples``."""

        return self.audio


def _import_sounddevice() -> Any:
    try:
        import sounddevice  # type: ignore[import-not-found]
    except (ImportError, OSError) as exc:
        raise AudioDeviceError(
            "Microphone support is unavailable. Install sounddevice and "
            "ensure an input device is enabled."
        ) from exc
    return sounddevice


def _mono_float32(audio: Any) -> np.ndarray:
    samples = np.asarray(audio, dtype=np.float32)
    if samples.ndim == 0:
        samples = samples.reshape(1)
    elif samples.ndim == 2:
        if samples.shape[1] == 0:
            return np.empty(0, dtype=np.float32)
        samples = samples.mean(axis=1, dtype=np.float32)
    elif samples.ndim != 1:
        raise ValueError("audio must be a one-dimensional or frames-by-channels array")

    samples = np.ascontiguousarray(samples, dtype=np.float32)
    # Device/driver bugs must not leak NaNs into a model invocation.
    samples = np.nan_to_num(samples, copy=True, nan=0.0, posinf=1.0, neginf=-1.0)
    np.clip(samples, -1.0, 1.0, out=samples)
    return samples


def audio_rms(audio: Any) -> float:
    """Return RMS without overflowing float32 intermediate values."""

    samples = _mono_float32(audio)
    if samples.size == 0:
        return 0.0
    values = samples.astype(np.float64, copy=False)
    return float(np.sqrt(np.mean(values * values)))


def resample_audio(
    audio: Any,
    source_sample_rate: int | float,
    target_sample_rate: int = TARGET_SAMPLE_RATE,
) -> np.ndarray:
    """Resample mono PCM with a deterministic NumPy-only implementation.

    Linear interpolation is appropriate for the short, mono speech captures
    handled here and avoids making SciPy a required desktop dependency.  The
    output length is derived from the rate ratio, so repeated calls do not
    accumulate timestamp drift.
    """

    source_rate = float(source_sample_rate)
    target_rate = int(target_sample_rate)
    if not np.isfinite(source_rate) or source_rate <= 0:
        raise ValueError("source_sample_rate must be a positive finite number")
    if target_rate <= 0:
        raise ValueError("target_sample_rate must be positive")

    samples = _mono_float32(audio)
    if samples.size == 0:
        return samples
    if np.isclose(source_rate, target_rate):
        return samples.copy()

    output_size = max(1, int(round(samples.size * target_rate / source_rate)))
    if samples.size == 1:
        return np.full(output_size, samples[0], dtype=np.float32)

    # Positions in source-frame units avoid constructing duration arrays and
    # ensure the last requested sample never reads past the captured buffer.
    positions = np.arange(output_size, dtype=np.float64) * source_rate / target_rate
    positions = np.minimum(positions, samples.size - 1)
    left = np.floor(positions).astype(np.int64)
    right = np.minimum(left + 1, samples.size - 1)
    fraction = (positions - left).astype(np.float32)
    result = samples[left] + (samples[right] - samples[left]) * fraction
    return np.ascontiguousarray(result, dtype=np.float32)


class AudioRecorder:
    """Thread-safe, one-shot microphone recorder backed by sounddevice."""

    def __init__(
        self,
        device: int | str | None = None,
        *,
        target_sample_rate: int = TARGET_SAMPLE_RATE,
        native_sample_rate: int | None = None,
        min_duration_seconds: float = 0.25,
        max_duration_seconds: float = 300.0,
        # Keep this only above the measured hardware noise floor.  Laptop
        # microphone arrays can expose much quieter mono PCM through
        # PortAudio than through Windows' native capture path; the ASR VAD is
        # the authoritative speech detector after this inexpensive guard.
        silence_rms_threshold: float = 0.0003,
        blocksize: int = 0,
        latency: str | float | None = "low",
    ) -> None:
        if target_sample_rate <= 0:
            raise ValueError("target_sample_rate must be positive")
        if native_sample_rate is not None and native_sample_rate <= 0:
            raise ValueError("native_sample_rate must be positive")
        if min_duration_seconds < 0:
            raise ValueError("min_duration_seconds cannot be negative")
        if not np.isfinite(max_duration_seconds) or max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive and finite")
        if silence_rms_threshold < 0:
            raise ValueError("silence_rms_threshold cannot be negative")

        self.device = device
        self.target_sample_rate = int(target_sample_rate)
        self._native_sample_rate = native_sample_rate
        self.min_duration_seconds = float(min_duration_seconds)
        self.max_duration_seconds = float(max_duration_seconds)
        self.silence_rms_threshold = float(silence_rms_threshold)
        self.blocksize = int(blocksize)
        self.latency = latency

        self._lock = threading.RLock()
        self._stream: Any | None = None
        self._recording = False
        self._chunks: list[np.ndarray] = []
        self._status_messages: deque[str] = deque(maxlen=32)
        self._retained_samples = 0
        self._max_retained_samples: int | None = None
        self._duration_limit_reached = False
        self._duration_limit_event = threading.Event()
        self._stop_signal_event = threading.Event()
        self._stop_signal_reason: str | None = None
        self._finishing = False
        self._unexpected_stream_finished = False
        self._stream_generation = 0
        self._active_stream_generation: int | None = None
        self._resolved_device: object | int | None = _UNRESOLVED_DEVICE

    @staticmethod
    def _default_input_index(sd: Any) -> int:
        default = getattr(getattr(sd, "default", None), "device", None)
        try:
            if hasattr(default, "input"):
                return int(default.input)
            if isinstance(default, (tuple, list, np.ndarray)) or hasattr(
                default, "__getitem__"
            ):
                return int(default[0])
            if default is None:
                return -1
            return int(default)
        except (TypeError, ValueError, IndexError):
            return -1

    @staticmethod
    def _host_api_names(sd: Any) -> dict[int, str]:
        try:
            host_apis = sd.query_hostapis()
        except Exception:
            return {}
        result: dict[int, str] = {}
        for index, details in enumerate(host_apis):
            try:
                result[index] = str(details.get("name", ""))
            except (AttributeError, TypeError):
                result[index] = ""
        return result

    @classmethod
    def _list_input_devices(cls, sd: Any) -> list[AudioDevice]:
        devices = sd.query_devices()
        default_input = cls._default_input_index(sd)
        host_apis = cls._host_api_names(sd)
        result: list[AudioDevice] = []
        for index, details in enumerate(devices):
            channels = int(details.get("max_input_channels", 0))
            if channels <= 0:
                continue
            rate = int(round(float(details.get("default_samplerate", 0))))
            try:
                host_api_index = int(details.get("hostapi", -1))
            except (TypeError, ValueError):
                host_api_index = -1
            result.append(
                AudioDevice(
                    index=index,
                    name=str(details.get("name", f"Input {index}")),
                    default_sample_rate=rate,
                    max_input_channels=channels,
                    is_default=index == default_input,
                    host_api=host_apis.get(host_api_index, ""),
                )
            )
        return result

    @staticmethod
    def list_input_devices() -> list[AudioDevice]:
        """List input-capable devices without opening any of them."""

        sd = _import_sounddevice()
        try:
            return AudioRecorder._list_input_devices(sd)
        except AudioCaptureError:
            raise
        except Exception as exc:
            raise AudioDeviceError("Could not enumerate input devices") from exc

    @staticmethod
    def _preferred_device(devices: list[AudioDevice]) -> AudioDevice | None:
        if not devices:
            return None
        return min(devices, key=lambda item: (not item.is_default, item.index))

    def _resolve_device(self, sd: Any) -> int | None:
        """Resolve a persisted selector to the current PortAudio index.

        Stable selectors prefer an exact name/host API/rate match.  Driver
        updates may alter one component, so progressively weaker name matches
        are allowed.  A missing device deliberately falls back to the system
        default input instead of accidentally opening an unrelated index.
        """

        cached = self._resolved_device
        if cached is not _UNRESOLVED_DEVICE:
            return cached if type(cached) is int else None

        resolved = self._resolve_device_uncached(sd)
        self._resolved_device = resolved
        return resolved

    def _resolve_device_uncached(self, sd: Any) -> int | None:
        """Perform the device enumeration for :meth:`_resolve_device`."""

        selected = self.device
        if selected is None:
            return None

        try:
            devices = self._list_input_devices(sd)
        except Exception as exc:
            raise AudioDeviceError(
                "Could not resolve the explicitly selected input device"
            ) from exc

        if type(selected) is int:
            if selected >= 0 and any(item.index == selected for item in devices):
                return selected
            raise AudioDeviceError("The explicitly selected input device is unavailable")

        value = str(selected).strip()
        if value.isdecimal():
            legacy_index = int(value)
            if any(item.index == legacy_index for item in devices):
                return legacy_index
            raise AudioDeviceError("The explicitly selected input device is unavailable")

        identity = parse_microphone_selector(value)
        if value.startswith(
            (MICROPHONE_SELECTOR_PREFIX, LEGACY_MICROPHONE_SELECTOR_PREFIX)
        ) and identity is None:
            raise AudioDeviceError("The saved input-device selector is malformed")

        if identity is None:
            wanted_name = _match_text(value)
            match = self._preferred_device(
                [item for item in devices if _match_text(item.name) == wanted_name]
            )
            if match is None:
                raise AudioDeviceError("The explicitly selected input device is unavailable")
            return match.index

        name, host_api, sample_rate = identity
        wanted_name = _match_text(name)
        wanted_host = _match_text(host_api)
        name_matches = [
            item for item in devices if _match_text(item.name) == wanted_name
        ]
        exact = [
            item
            for item in name_matches
            if _match_text(item.host_api) == wanted_host
            and item.default_sample_rate == sample_rate
        ]
        host_matches = [
            item for item in name_matches if _match_text(item.host_api) == wanted_host
        ]
        rate_matches = [
            item for item in name_matches if item.default_sample_rate == sample_rate
        ]
        match = (
            self._preferred_device(exact)
            or self._preferred_device(host_matches)
            or self._preferred_device(rate_matches)
            or self._preferred_device(name_matches)
        )
        if match is None:
            raise AudioDeviceError("The explicitly selected input device is unavailable")
        return match.index

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    @property
    def native_sample_rate(self) -> int | None:
        return self._native_sample_rate

    @property
    def duration_limit_reached(self) -> bool:
        """Whether the active capture has reached its one-shot duration limit."""

        return self._duration_limit_event.is_set()

    def wait_for_duration_limit(self, timeout: float | None = 0.0) -> bool:
        """Wait for the active capture's limit signal without invoking UI code."""

        return self._duration_limit_event.wait(timeout)

    def wait_for_stop_signal(self, timeout: float | None = 0.0) -> bool:
        """Wait for a capture-integrity signal without invoking UI code."""

        return self._stop_signal_event.wait(timeout)

    @property
    def stop_signal_reason(self) -> str | None:
        with self._lock:
            return self._stop_signal_reason

    @property
    def stop_signal_status_messages(self) -> tuple[str, ...]:
        """Return a bounded snapshot of statuses observed by the active stream."""

        with self._lock:
            return tuple(self._status_messages)

    @property
    def retained_samples(self) -> int:
        with self._lock:
            return self._retained_samples

    @property
    def max_retained_samples(self) -> int | None:
        with self._lock:
            return self._max_retained_samples

    def _resolve_native_sample_rate(self, sd: Any) -> int:
        if self._native_sample_rate is not None:
            return int(self._native_sample_rate)
        resolved_device = self._resolve_device(sd)
        try:
            details = sd.query_devices(resolved_device, "input")
            rate = int(round(float(details["default_samplerate"])))
        except Exception as exc:
            raise AudioDeviceError("Could not query the selected input device") from exc
        if rate <= 0:
            raise AudioDeviceError("The selected input device has no valid sample rate")
        self._native_sample_rate = rate
        return rate

    def prepare(self) -> int:
        """Resolve and validate the device format without beginning capture."""

        sd = _import_sounddevice()
        rate = self._resolve_native_sample_rate(sd)
        resolved_device = self._resolve_device(sd)
        try:
            sd.check_input_settings(
                device=resolved_device,
                channels=1,
                dtype="float32",
                samplerate=rate,
            )
        except Exception as exc:
            raise AudioDeviceError(
                f"The selected input device cannot record mono float32 at {rate} Hz"
            ) from exc
        return rate

    def _stream_kwargs(
        self,
        rate: int,
        callback: Any,
        sd: Any,
        *,
        finished_callback: Any | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "device": self._resolve_device(sd),
            "samplerate": rate,
            "channels": 1,
            "dtype": "float32",
            "blocksize": self.blocksize,
            "callback": callback,
        }
        if finished_callback is not None:
            kwargs["finished_callback"] = finished_callback
        if self.latency is not None:
            kwargs["latency"] = self.latency
        return kwargs

    def warmup(self, duration_seconds: float = 0.05) -> int:
        """Open/start/close the device once to surface permission errors early."""

        if duration_seconds < 0:
            raise ValueError("duration_seconds cannot be negative")
        with self._lock:
            if self._recording:
                raise AudioCaptureError("Cannot warm up while recording")

        sd = _import_sounddevice()
        rate = self.prepare()
        stream: Any | None = None
        try:
            stream = sd.InputStream(
                **self._stream_kwargs(rate, lambda *_args: None, sd)
            )
            stream.start()
            if duration_seconds:
                time.sleep(duration_seconds)
            stream.stop()
            return rate
        except Exception as exc:
            raise AudioDeviceError("Could not open the selected input device") from exc
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    def _audio_callback(
        self,
        indata: Any,
        _frames: int,
        _time_info: Any,
        status: Any,
    ) -> None:
        # PortAudio invokes this on a real-time thread: only copy and append.
        # Once the duration signal is set, return before converting/copying any
        # further driver buffers.
        with self._lock:
            if not self._recording or self._duration_limit_reached:
                return
            if status:
                # Record PortAudio's integrity warning even if conversion of
                # this same callback buffer subsequently fails.
                self._status_messages.append(str(status))
                self._set_stop_signal_locked("portaudio_status")
        try:
            chunk = _mono_float32(indata)
            with self._lock:
                if not self._recording or self._duration_limit_reached:
                    return
                if chunk.size:
                    maximum = self._max_retained_samples
                    assert maximum is not None
                    remaining = max(0, maximum - self._retained_samples)
                    retained = min(remaining, int(chunk.size))
                    if retained:
                        if retained == chunk.size:
                            self._chunks.append(chunk)
                        else:
                            # A slice would retain the full oversized callback
                            # array through its base object; copy only the
                            # bounded prefix instead.
                            self._chunks.append(chunk[:retained].copy())
                        self._retained_samples += retained
                    if self._retained_samples >= maximum:
                        self._duration_limit_reached = True
                        self._duration_limit_event.set()
        except Exception as exc:  # Never propagate through the PortAudio callback.
            with self._lock:
                if self._recording:
                    self._status_messages.append(
                        f"callback_error: {type(exc).__name__}"
                    )
                    self._set_stop_signal_locked("callback_error")

    def _set_stop_signal_locked(self, reason: str) -> None:
        if self._stop_signal_reason is None:
            self._stop_signal_reason = reason
        self._stop_signal_event.set()

    def _stream_finished_callback(self, generation: int) -> None:
        """Signal an unexpected PortAudio finish for the active stream only."""

        with self._lock:
            if (
                not self._recording
                or self._finishing
                or self._active_stream_generation != generation
                or self._unexpected_stream_finished
            ):
                return
            self._unexpected_stream_finished = True
            self._status_messages.append("stream_finished_unexpectedly")
            self._set_stop_signal_locked("stream_finished_unexpectedly")

    def _reset_capture_locked(self) -> None:
        self._chunks.clear()
        self._status_messages.clear()
        self._retained_samples = 0
        self._max_retained_samples = None
        self._duration_limit_reached = False
        self._duration_limit_event.clear()
        self._stop_signal_reason = None
        self._stop_signal_event.clear()
        self._finishing = False
        self._unexpected_stream_finished = False
        self._active_stream_generation = None

    def start(self) -> int:
        """Begin a fresh recording and return the native capture rate."""

        with self._lock:
            if self._recording:
                raise AudioCaptureError("Recording is already active")

        sd = _import_sounddevice()
        rate = self.prepare()
        stream: Any | None = None
        with self._lock:
            self._stream_generation += 1
            generation = self._stream_generation
        try:
            stream = sd.InputStream(
                **self._stream_kwargs(
                    rate,
                    self._audio_callback,
                    sd,
                    finished_callback=lambda: self._stream_finished_callback(generation),
                )
            )
            with self._lock:
                self._reset_capture_locked()
                self._max_retained_samples = max(
                    1,
                    int(rate * self.max_duration_seconds),
                )
                self._stream = stream
                self._active_stream_generation = generation
                self._recording = True
            stream.start()
            return rate
        except Exception as exc:
            with self._lock:
                self._finishing = True
                self._stream = None
                self._recording = False
                self._reset_capture_locked()
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            raise AudioDeviceError("Could not start microphone capture") from exc

    def _finish_stream(
        self,
    ) -> tuple[
        list[np.ndarray],
        tuple[str, ...],
        bool,
        int,
        str | None,
        Exception | None,
    ]:
        with self._lock:
            if not self._recording or self._stream is None:
                raise AudioCaptureError("No recording is active")
            stream = self._stream
            self._finishing = True

        stop_error: Exception | None = None
        try:
            stream.stop()
        except Exception as exc:
            stop_error = exc
        finally:
            try:
                stream.close()
            except Exception as exc:
                stop_error = stop_error or exc

        with self._lock:
            self._recording = False
            self._stream = None
            chunks = self._chunks
            statuses = tuple(self._status_messages)
            limit_reached = self._duration_limit_reached
            retained_samples = self._retained_samples
            stop_signal_reason = self._stop_signal_reason
            self._chunks = []
            self._status_messages.clear()
            self._retained_samples = 0
            self._max_retained_samples = None
            self._duration_limit_reached = False
            self._duration_limit_event.clear()
            self._stop_signal_reason = None
            self._stop_signal_event.clear()
            self._finishing = False
            self._unexpected_stream_finished = False
            self._active_stream_generation = None

        return (
            chunks,
            statuses,
            limit_reached,
            retained_samples,
            stop_signal_reason,
            stop_error,
        )

    def stop(self, *, validate: bool = True) -> AudioRecording:
        """Stop, resample to the target rate and return capture metadata."""

        (
            chunks,
            statuses,
            limit_reached,
            retained_samples,
            stop_signal_reason,
            stop_error,
        ) = self._finish_stream()
        native = int(self._native_sample_rate or self.target_sample_rate)
        duration = retained_samples / native
        if validate and stop_signal_reason is not None:
            # Integrity failures take precedence and are reported before any
            # whole-recording allocation, even if stopping the failed stream
            # also produced a device error.
            raise AudioStreamError(
                reason=stop_signal_reason,
                status_messages=statuses,
                captured_samples=retained_samples,
                source_rate=native,
            )
        if validate and limit_reached:
            # Fail before concatenating, sanitizing or resampling the complete
            # bounded capture. The exception carries lightweight metadata only.
            raise AudioDurationLimitError(
                max_duration_seconds=self.max_duration_seconds,
                duration_seconds=duration,
                captured_samples=retained_samples,
                source_sample_rate=native,
                status_messages=statuses,
            )
        if stop_error is not None:
            raise AudioDeviceError("Could not finish microphone capture") from stop_error
        raw = (
            np.concatenate(chunks).astype(np.float32, copy=False)
            if chunks
            else np.empty(0, dtype=np.float32)
        )
        duration = raw.size / native
        rms = audio_rms(raw)
        peak = float(np.max(np.abs(raw))) if raw.size else 0.0
        silent = rms < self.silence_rms_threshold

        if validate and duration < self.min_duration_seconds:
            raise AudioTooShortError(
                f"Recording is {duration:.2f}s; minimum is {self.min_duration_seconds:.2f}s"
            )
        if validate and silent:
            raise SilentAudioError(
                f"Recording RMS {rms:.5f} is below {self.silence_rms_threshold:.5f}"
            )

        return AudioRecording(
            audio=resample_audio(raw, native, self.target_sample_rate),
            sample_rate=self.target_sample_rate,
            source_sample_rate=native,
            duration_seconds=duration,
            rms=rms,
            peak=peak,
            silent=silent,
            status_messages=statuses,
            limit_reached=limit_reached,
        )

    def cancel(self) -> bool:
        """Discard an active recording. Returns whether one was cancelled."""

        with self._lock:
            if not self._recording or self._stream is None:
                return False
        try:
            *_, stop_error = self._finish_stream()
            if stop_error is not None:
                raise AudioDeviceError("Could not finish microphone capture") from stop_error
        finally:
            with self._lock:
                self._reset_capture_locked()
        return True

    def close(self) -> None:
        """Best-effort shutdown helper for application exit."""

        if self.is_recording:
            try:
                self.cancel()
            except AudioCaptureError:
                pass

    def __enter__(self) -> "AudioRecorder":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
