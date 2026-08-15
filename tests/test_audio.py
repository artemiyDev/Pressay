from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

import pressay.audio as audio_module
from pressay.audio import (
    MICROPHONE_SELECTOR_PREFIX,
    AudioDeviceError,
    AudioDurationLimitError,
    AudioRecorder,
    AudioStreamError,
    AudioTooShortError,
    SilentAudioError,
    audio_rms,
    build_microphone_selector,
    parse_microphone_selector,
    resample_audio,
)


class FakeStream:
    def __init__(self, owner: "FakeSoundDevice", **kwargs: object) -> None:
        self.owner = owner
        self.callback = kwargs["callback"]
        self.finished_callback = kwargs.get("finished_callback")
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.closed = False
        owner.streams.append(self)

    def start(self) -> None:
        self.started = True
        for chunk in self.owner.chunks:
            self.callback(chunk.reshape(-1, 1), len(chunk), {}, self.owner.status)

    def stop(self) -> None:
        self.stopped = True
        if callable(self.finished_callback):
            self.finished_callback()

    def close(self) -> None:
        self.closed = True

    def finish_unexpectedly(self) -> None:
        if callable(self.finished_callback):
            self.finished_callback()


class FakeSoundDevice:
    def __init__(self, chunks: list[np.ndarray] | None = None) -> None:
        self.devices = [
            {
                "name": "Speakers",
                "max_input_channels": 0,
                "default_samplerate": 48_000.0,
                "hostapi": 0,
            },
            {
                "name": "USB Microphone",
                "max_input_channels": 2,
                "default_samplerate": 48_000.0,
                "hostapi": 1,
            },
        ]
        self.hostapis = [{"name": "MME"}, {"name": "Windows WASAPI"}]
        self.default = SimpleNamespace(device=(1, 0))
        self.chunks = chunks or []
        self.status: object = ""
        self.streams: list[FakeStream] = []
        self.checked: dict[str, object] | None = None

    def query_devices(self, device: object = ... , kind: str | None = None):
        if device is ...:
            return self.devices
        if device is None:
            return self.devices[int(self.default.device[0])]
        if isinstance(device, str):
            return next(item for item in self.devices if item["name"] == device)
        return self.devices[int(device)]

    def query_hostapis(self):
        return self.hostapis

    def check_input_settings(self, **kwargs: object) -> None:
        self.checked = kwargs

    def InputStream(self, **kwargs: object) -> FakeStream:  # noqa: N802 - sounddevice API
        return FakeStream(self, **kwargs)


def install_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeSoundDevice) -> None:
    monkeypatch.setattr(audio_module, "_import_sounddevice", lambda: fake)


def test_resample_is_mono_float32_and_has_rate_derived_length() -> None:
    frames = np.column_stack(
        [np.linspace(-0.5, 0.5, 480, dtype=np.float32)] * 2
    )

    result = resample_audio(frames, 48_000, 16_000)

    assert result.dtype == np.float32
    assert result.ndim == 1
    assert result.shape == (160,)
    assert np.isfinite(result).all()
    assert result[0] == pytest.approx(-0.5)


def test_resample_sanitizes_bad_values_and_validates_rates() -> None:
    result = resample_audio([np.nan, np.inf, -np.inf, 2.0], 16_000)

    assert result.tolist() == [0.0, 1.0, -1.0, 1.0]
    with pytest.raises(ValueError):
        resample_audio([0.0], 0)


def test_resample_upsample_matches_reference_linear_interpolation() -> None:
    ramp = np.linspace(-0.5, 0.5, 100, dtype=np.float32)

    result = resample_audio(ramp, 16_000, 48_000)

    positions = np.arange(result.size, dtype=np.float64) * 16_000 / 48_000
    positions = np.minimum(positions, ramp.size - 1)
    expected = np.interp(positions, np.arange(ramp.size), ramp.astype(np.float64))

    assert result.shape == (300,)
    np.testing.assert_allclose(result, expected, atol=1e-6)


def test_resample_downsample_preserves_inband_amplitude() -> None:
    source_rate = 48_000
    target_rate = 16_000
    t = np.arange(int(source_rate * 0.05), dtype=np.float64) / source_rate
    tone = (0.5 * np.sin(2 * np.pi * 1_000 * t)).astype(np.float32)

    result = resample_audio(tone, source_rate, target_rate)

    assert audio_rms(result) == pytest.approx(audio_rms(tone), rel=0.03)


def test_resample_does_not_crash_on_empty_single_or_short_buffers() -> None:
    assert resample_audio(np.array([], dtype=np.float32), 48_000, 16_000).size == 0

    single = resample_audio(np.array([0.25], dtype=np.float32), 48_000, 16_000)
    assert single.tolist() == pytest.approx([0.25])

    short = np.ones(5, dtype=np.float32) * 0.2
    result = resample_audio(short, 48_000, 16_000)
    assert np.isfinite(result).all()
    assert result.size >= 1


def test_default_silence_floor_accepts_quiet_laptop_microphone_arrays() -> None:
    recorder = AudioRecorder()

    assert recorder.silence_rms_threshold == pytest.approx(0.0003)


def test_lists_only_input_devices_and_marks_default(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSoundDevice()
    install_fake(monkeypatch, fake)

    devices = AudioRecorder.list_input_devices()

    assert len(devices) == 1
    assert devices[0].index == 1
    assert devices[0].name == "USB Microphone"
    assert devices[0].default_sample_rate == 48_000
    assert devices[0].is_default
    assert devices[0].host_api == "Windows WASAPI"
    assert parse_microphone_selector(devices[0].stable_selector) == (
        "USB Microphone",
        "Windows WASAPI",
        48_000,
    )


def test_previous_product_microphone_selector_remains_loadable() -> None:
    selector = (
        "whisperflow:microphone:v1?name=USB+Microphone&"
        "host_api=Windows+WASAPI&sample_rate=48000"
    )

    assert parse_microphone_selector(selector) == (
        "USB Microphone",
        "Windows WASAPI",
        48_000,
    )


def test_lists_devices_with_sounddevice_input_output_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSoundDevice()
    class Pair:
        def __getitem__(self, index: int) -> int:
            return (1, 0)[index]

    fake.default.device = Pair()
    install_fake(monkeypatch, fake)

    devices = AudioRecorder.list_input_devices()

    assert len(devices) == 1
    assert devices[0].is_default is True


def test_stable_selector_survives_portaudio_index_reordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = FakeSoundDevice()
    install_fake(monkeypatch, first)
    selector = AudioRecorder.list_input_devices()[0].stable_selector

    reordered = FakeSoundDevice()
    reordered.devices = [reordered.devices[1], reordered.devices[0]]
    reordered.default.device = (0, 1)
    install_fake(monkeypatch, reordered)

    recorder = AudioRecorder(selector)
    assert recorder.prepare() == 48_000
    assert reordered.checked is not None
    assert reordered.checked["device"] == 0


def test_selector_prefers_host_api_and_missing_selection_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSoundDevice()
    fake.devices = [
        {
            "name": "USB Microphone",
            "max_input_channels": 1,
            "default_samplerate": 44_100.0,
            "hostapi": 0,
        },
        {
            "name": "USB Microphone",
            "max_input_channels": 1,
            "default_samplerate": 48_000.0,
            "hostapi": 1,
        },
    ]
    fake.default.device = (0, 0)
    install_fake(monkeypatch, fake)

    changed_rate = build_microphone_selector(
        name="USB Microphone", host_api="Windows WASAPI", sample_rate=96_000
    )
    recorder = AudioRecorder(changed_rate)
    assert recorder.prepare() == 48_000
    assert fake.checked is not None
    assert fake.checked["device"] == 1

    missing = build_microphone_selector(
        name="Disconnected Microphone", host_api="Windows WASAPI", sample_rate=48_000
    )
    recorder = AudioRecorder(missing)
    with pytest.raises(AudioDeviceError, match="explicitly selected"):
        recorder.prepare()


@pytest.mark.parametrize(
    "selected",
    [
        99,
        "99",
        "Disconnected Microphone",
        build_microphone_selector(
            name="Disconnected Microphone",
            host_api="Windows WASAPI",
            sample_rate=48_000,
        ),
        f"{MICROPHONE_SELECTOR_PREFIX}broken",
    ],
)
def test_every_missing_or_malformed_explicit_device_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    selected: int | str,
) -> None:
    fake = FakeSoundDevice()
    install_fake(monkeypatch, fake)

    with pytest.raises(AudioDeviceError):
        AudioRecorder(selected).prepare()


def test_explicit_device_does_not_fall_back_when_enumeration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSoundDevice()

    def fail_enumeration(device: object = ..., kind: str | None = None):
        if device is ...:
            raise RuntimeError("temporary PortAudio enumeration failure")
        return FakeSoundDevice.query_devices(fake, device, kind)

    fake.query_devices = fail_enumeration  # type: ignore[method-assign]
    install_fake(monkeypatch, fake)
    selected = build_microphone_selector(
        name="USB Microphone", host_api="Windows WASAPI", sample_rate=48_000
    )

    with pytest.raises(AudioDeviceError, match="resolve"):
        AudioRecorder(selected).prepare()
    with pytest.raises(AudioDeviceError, match="resolve"):
        AudioRecorder(1).prepare()


def test_device_none_is_the_only_selection_that_uses_system_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSoundDevice()
    install_fake(monkeypatch, fake)

    recorder = AudioRecorder(device=None)

    assert recorder.prepare() == 48_000
    assert fake.checked is not None
    assert fake.checked["device"] is None


def test_prepare_warmup_and_capture_at_native_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    t = np.arange(4_800, dtype=np.float32) / 48_000
    signal = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    fake = FakeSoundDevice([signal[:2_400], signal[2_400:]])
    fake.status = "input overflow"
    install_fake(monkeypatch, fake)
    recorder = AudioRecorder(
        "USB Microphone",
        min_duration_seconds=0.05,
        silence_rms_threshold=0.001,
    )

    assert recorder.prepare() == 48_000
    assert fake.checked == {
        "device": 1,
        "channels": 1,
        "dtype": "float32",
        "samplerate": 48_000,
    }
    # Warmup opens the stream but its no-op callback does not retain samples.
    assert recorder.warmup(0) == 48_000
    assert fake.streams[-1].closed

    assert recorder.start() == 48_000
    assert recorder.is_recording
    assert recorder.wait_for_stop_signal() is True
    assert recorder.stop_signal_reason == "portaudio_status"
    assert recorder.stop_signal_status_messages == (
        "input overflow",
        "input overflow",
    )
    recording = recorder.stop(validate=False)

    assert not recorder.is_recording
    assert recording.sample_rate == 16_000
    assert recording.source_sample_rate == 48_000
    assert recording.audio.shape == (1_600,)
    assert recording.duration_seconds == pytest.approx(0.1)
    assert recording.rms == pytest.approx(audio_rms(signal), rel=1e-5)
    assert recording.status_messages == ("input overflow", "input overflow")
    assert recorder.wait_for_stop_signal() is False
    assert recorder.stop_signal_reason is None
    assert recorder.stop_signal_status_messages == ()


def test_current_rms_tracks_the_latest_chunk_and_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSoundDevice()
    install_fake(monkeypatch, fake)
    recorder = AudioRecorder(min_duration_seconds=0, silence_rms_threshold=0)

    assert recorder.current_rms == 0.0
    recorder.start()
    recorder._audio_callback(np.full((48, 1), 0.25, dtype=np.float32), 48, {}, "")

    assert recorder.current_rms == pytest.approx(0.25)
    recorder.stop(validate=False)
    assert recorder.current_rms == 0.0


def test_stop_reports_finalize_phase_timings(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSoundDevice([np.ones(4_800, dtype=np.float32) * 0.1])
    install_fake(monkeypatch, fake)
    recorder = AudioRecorder(min_duration_seconds=0, silence_rms_threshold=0)

    recorder.start()
    recording = recorder.stop()

    assert set(recording.finalize_breakdown) == {
        "stream_stop_seconds",
        "assemble_seconds",
    }
    assert all(seconds >= 0.0 for seconds in recording.finalize_breakdown.values())


def test_stop_rejects_short_and_silent_recordings(monkeypatch: pytest.MonkeyPatch) -> None:
    short = FakeSoundDevice([np.ones(100, dtype=np.float32) * 0.1])
    install_fake(monkeypatch, short)
    recorder = AudioRecorder(min_duration_seconds=0.01)
    recorder.start()
    with pytest.raises(AudioTooShortError):
        recorder.stop()

    silent = FakeSoundDevice([np.zeros(4_800, dtype=np.float32)])
    install_fake(monkeypatch, silent)
    recorder = AudioRecorder(min_duration_seconds=0.01)
    recorder.start()
    with pytest.raises(SilentAudioError):
        recorder.stop()


def test_cancel_discards_capture_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSoundDevice([np.ones(1_000, dtype=np.float32) * 0.1])
    install_fake(monkeypatch, fake)
    recorder = AudioRecorder()

    recorder.start()
    assert recorder.cancel() is True
    assert recorder.is_recording is False
    assert recorder.cancel() is False
    assert fake.streams[-1].closed


def test_duration_limit_retains_exact_native_sample_bound_and_signals_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSoundDevice(
        [
            np.ones(30, dtype=np.float32) * 0.1,
            np.ones(30, dtype=np.float32) * 0.1,
            np.ones(30, dtype=np.float32) * 0.1,
        ]
    )
    install_fake(monkeypatch, fake)
    recorder = AudioRecorder(
        max_duration_seconds=0.001,
        min_duration_seconds=0,
        silence_rms_threshold=0,
        # Opt into the legacy discard-the-whole-capture behaviour: this test
        # exercises that path specifically (see
        # test_stop_returns_truncated_recording_when_limit_reached for the
        # default toggle-dictation behaviour).
        discard_on_limit=True,
    )

    assert recorder.start() == 48_000
    assert recorder.max_retained_samples == 48
    assert recorder.retained_samples == 48
    assert recorder.duration_limit_reached is True
    assert recorder.wait_for_duration_limit() is True

    # A validate=True stop must fail from lightweight metadata before any
    # whole-recording concatenate/resample allocation.
    monkeypatch.setattr(
        audio_module.np,
        "concatenate",
        lambda *_args, **_kwargs: pytest.fail("duration-limit stop concatenated audio"),
    )
    with pytest.raises(AudioDurationLimitError) as caught:
        recorder.stop()

    assert caught.value.captured_samples == 48
    assert caught.value.source_sample_rate == 48_000
    assert caught.value.duration_seconds == pytest.approx(0.001)
    assert caught.value.max_duration_seconds == pytest.approx(0.001)
    assert recorder.duration_limit_reached is False
    assert recorder.wait_for_duration_limit() is False
    assert recorder.retained_samples == 0
    assert recorder.max_retained_samples is None


def test_stop_returns_truncated_recording_when_limit_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSoundDevice(
        [
            np.ones(30, dtype=np.float32) * 0.1,
            np.ones(30, dtype=np.float32) * 0.1,
        ]
    )
    install_fake(monkeypatch, fake)
    # discard_on_limit defaults to False: toggle dictation must recognize the
    # captured tail instead of losing the whole recording.
    recorder = AudioRecorder(
        max_duration_seconds=0.001,
        min_duration_seconds=0,
        silence_rms_threshold=0,
    )

    recorder.start()
    recording = recorder.stop()

    assert recording.limit_reached is True
    assert recording.audio.size > 0
    assert recording.duration_seconds == pytest.approx(0.001)
    assert recording.source_sample_rate == 48_000


@pytest.mark.parametrize("discard_on_limit", [False, True])
def test_stream_error_outranks_duration_limit_regardless_of_discard_setting(
    monkeypatch: pytest.MonkeyPatch,
    discard_on_limit: bool,
) -> None:
    # A single oversized callback both trips the PortAudio integrity signal
    # and fills the (tiny) duration bound, so both failure paths are live at
    # once. AudioStreamError must win either way.
    fake = FakeSoundDevice([np.ones(4_800, dtype=np.float32) * 0.1])
    fake.status = "input overflow"
    install_fake(monkeypatch, fake)
    recorder = AudioRecorder(
        max_duration_seconds=0.05,
        min_duration_seconds=0,
        silence_rms_threshold=0,
        discard_on_limit=discard_on_limit,
    )

    recorder.start()
    assert recorder.duration_limit_reached is True
    assert recorder.wait_for_stop_signal() is True

    with pytest.raises(AudioStreamError) as caught:
        recorder.stop()

    assert caught.value.reason == "portaudio_status"


def test_duration_limit_metadata_is_available_when_validation_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSoundDevice([np.ones(100, dtype=np.float32) * 0.1])
    install_fake(monkeypatch, fake)
    recorder = AudioRecorder(
        max_duration_seconds=0.001,
        min_duration_seconds=0,
        silence_rms_threshold=0,
    )

    recorder.start()
    recording = recorder.stop(validate=False)

    assert recording.limit_reached is True
    assert recording.duration_seconds == pytest.approx(0.001)
    assert recording.source_sample_rate == 48_000
    assert recording.audio.size == 16


def test_callbacks_after_duration_limit_do_not_convert_or_retain_more_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSoundDevice()
    install_fake(monkeypatch, fake)
    recorder = AudioRecorder(
        max_duration_seconds=0.001,
        min_duration_seconds=0,
        silence_rms_threshold=0,
    )
    recorder.start()
    recorder._audio_callback(np.ones((48, 1), dtype=np.float32), 48, {}, "")
    assert recorder.duration_limit_reached is True

    monkeypatch.setattr(
        audio_module,
        "_mono_float32",
        lambda _audio: pytest.fail("post-limit callback converted its buffer"),
    )
    recorder._audio_callback(np.ones((100, 1), dtype=np.float32), 100, {}, "")

    assert recorder.retained_samples == 48
    recorder.cancel()
    assert recorder.duration_limit_reached is False
    assert recorder.retained_samples == 0


def test_callback_status_history_is_bounded_to_last_32_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSoundDevice()
    install_fake(monkeypatch, fake)
    recorder = AudioRecorder(
        max_duration_seconds=1,
        min_duration_seconds=0,
        silence_rms_threshold=0,
    )
    recorder.start()
    for index in range(100):
        recorder._audio_callback(
            np.ones((1, 1), dtype=np.float32) * 0.1,
            1,
            {},
            f"status-{index}",
        )

    recording = recorder.stop(validate=False)

    assert len(recording.status_messages) == 32
    assert recording.status_messages[0] == "status-68"
    assert recording.status_messages[-1] == "status-99"


def test_portaudio_status_raises_stream_error_before_whole_audio_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSoundDevice([np.ones(4_800, dtype=np.float32) * 0.1])
    fake.status = "input overflow"
    install_fake(monkeypatch, fake)
    recorder = AudioRecorder(min_duration_seconds=0, silence_rms_threshold=0)

    recorder.start()
    assert recorder.wait_for_stop_signal() is True
    assert recorder.stop_signal_reason == "portaudio_status"
    assert recorder.stop_signal_status_messages == ("input overflow",)

    monkeypatch.setattr(
        audio_module.np,
        "concatenate",
        lambda *_args, **_kwargs: pytest.fail("stream-error stop concatenated audio"),
    )
    with pytest.raises(AudioStreamError) as caught:
        recorder.stop()

    assert caught.value.reason == "portaudio_status"
    assert caught.value.status_messages == ("input overflow",)
    assert caught.value.captured_samples == 4_800
    assert caught.value.source_rate == 48_000
    assert caught.value.source_sample_rate == 48_000
    assert recorder.wait_for_stop_signal() is False
    assert recorder.stop_signal_reason is None


def test_callback_error_sets_bounded_stop_signal_and_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSoundDevice()
    install_fake(monkeypatch, fake)
    recorder = AudioRecorder(min_duration_seconds=0, silence_rms_threshold=0)
    recorder.start()
    monkeypatch.setattr(
        audio_module,
        "_mono_float32",
        lambda _audio: (_ for _ in ()).throw(RuntimeError("bad callback buffer")),
    )

    recorder._audio_callback(np.ones((1, 1), dtype=np.float32), 1, {}, "")

    assert recorder.wait_for_stop_signal() is True
    assert recorder.stop_signal_reason == "callback_error"
    assert recorder.stop_signal_status_messages == ("callback_error: RuntimeError",)
    with pytest.raises(AudioStreamError) as caught:
        recorder.stop()
    assert caught.value.reason == "callback_error"
    assert caught.value.status_messages == ("callback_error: RuntimeError",)
    assert caught.value.captured_samples == 0


def test_unexpected_finished_callback_signals_once_and_stop_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSoundDevice([np.ones(100, dtype=np.float32) * 0.1])
    install_fake(monkeypatch, fake)
    recorder = AudioRecorder(min_duration_seconds=0, silence_rms_threshold=0)
    recorder.start()
    stream = fake.streams[-1]

    stream.finish_unexpectedly()
    stream.finish_unexpectedly()

    assert recorder.wait_for_stop_signal() is True
    assert recorder.stop_signal_reason == "stream_finished_unexpectedly"
    assert recorder.stop_signal_status_messages == ("stream_finished_unexpectedly",)
    with pytest.raises(AudioStreamError) as caught:
        recorder.stop()
    assert caught.value.reason == "stream_finished_unexpectedly"
    assert caught.value.status_messages == ("stream_finished_unexpectedly",)


def test_deliberate_stop_finished_callback_is_not_an_integrity_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSoundDevice([np.ones(4_800, dtype=np.float32) * 0.1])
    install_fake(monkeypatch, fake)
    recorder = AudioRecorder(min_duration_seconds=0, silence_rms_threshold=0)
    recorder.start()

    recording = recorder.stop()

    assert recording.audio.size == 1_600
    assert recording.status_messages == ()
    assert recorder.wait_for_stop_signal() is False
    assert recorder.stop_signal_reason is None


def test_cancel_clears_stream_health_signal_and_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSoundDevice()
    install_fake(monkeypatch, fake)
    recorder = AudioRecorder()
    recorder.start()
    recorder._audio_callback(
        np.ones((1, 1), dtype=np.float32),
        1,
        {},
        "input underflow",
    )
    assert recorder.wait_for_stop_signal() is True

    assert recorder.cancel() is True

    assert recorder.wait_for_stop_signal() is False
    assert recorder.stop_signal_reason is None
    assert recorder.stop_signal_status_messages == ()


@pytest.mark.parametrize("maximum", [0, -1, float("inf"), float("nan")])
def test_duration_limit_must_be_positive_and_finite(maximum: float) -> None:
    with pytest.raises(ValueError, match="max_duration_seconds"):
        AudioRecorder(max_duration_seconds=maximum)
