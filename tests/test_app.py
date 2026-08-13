from __future__ import annotations

import os
from pathlib import Path
import threading
import uuid

import pytest

from pressay.app import (
    _SingleInstance,
    _load_config,
    _microphones,
    _overlay_auto_hide_ms,
    _settings_dict,
    _start_native_shutdown,
)
from pressay.controller import DictationController
from pressay.audio import AudioDevice
from pressay.config import AppConfig
from pressay.ui import (
    MicrophoneChoice,
    SettingsWindow,
    UiSignals,
    format_replacements,
    microphone_choice_index,
    parse_replacements,
)


@pytest.fixture(autouse=True)
def _windows_app_platform(monkeypatch):
    monkeypatch.setattr("pressay.platform_support.sys.platform", "win32")


def test_windows_single_instance_mutex_is_reacquirable() -> None:
    if os.name != "nt":
        pytest.skip("Windows mutex test")

    name = f"Local\\Pressay.Test.{uuid.uuid4()}"
    first = _SingleInstance(name)
    second = _SingleInstance(name)
    assert first.acquire() is True
    assert second.acquire() is False
    first.close()

    third = _SingleInstance(name)
    assert third.acquire() is True
    third.close()


def test_microphone_picker_persists_stable_selector(monkeypatch) -> None:
    device = AudioDevice(
        index=7,
        name="USB Microphone",
        default_sample_rate=48_000,
        max_input_channels=1,
        is_default=True,
        host_api="Windows WASAPI",
    )
    monkeypatch.setattr(
        "pressay.app.AudioRecorder.list_input_devices", lambda: [device]
    )

    choices = _microphones()

    assert choices[1].value == device.stable_selector
    assert choices[1].legacy_index == 7
    assert choices[1].device_name == "USB Microphone"
    assert "Windows WASAPI" in choices[1].name


def test_picker_resolves_legacy_index_and_name_to_stable_choice() -> None:
    choices = [
        MicrophoneChoice(None, "Default"),
        MicrophoneChoice(
            "stable-usb",
            "USB Microphone",
            legacy_index=4,
            device_name="USB Microphone",
        ),
        MicrophoneChoice(
            "stable-usb-default",
            "USB Microphone (default)",
            legacy_index=8,
            device_name="USB Microphone",
            is_default=True,
        ),
    ]

    assert microphone_choice_index(choices, 4) == 1
    assert microphone_choice_index(choices, "4") == 1
    assert microphone_choice_index(choices, "usb microphone") == 2
    assert microphone_choice_index(choices, "stable-usb") == 1
    assert microphone_choice_index(choices, "missing") == 0
    assert _settings_dict(AppConfig(microphone=4))["microphone"] == 4
    assert _settings_dict(AppConfig(microphone="4"))["microphone"] == 4
    assert _settings_dict(AppConfig())["smart_spacing"] is True
    assert _settings_dict(AppConfig(smart_spacing=False))["smart_spacing"] is False


def test_personal_dictionary_text_round_trip_and_validation() -> None:
    rules = {
        "фаст апи": "FastAPI",
        "докер композ": "Docker Compose",
    }
    assert parse_replacements(format_replacements(rules)) == rules
    assert parse_replacements("# comment\n\nредис = Redis") == {"редис": "Redis"}
    with pytest.raises(ValueError, match="знак ="):
        parse_replacements("broken rule")
    with pytest.raises(ValueError, match="повторяется"):
        parse_replacements("Редис = Redis\nредис = REDIS")


def test_settings_window_keeps_only_two_recent_transcripts_in_memory(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    window = SettingsWindow(
        UiSignals(),
        _settings_dict(AppConfig()),
        [MicrophoneChoice(None, "Системный микрофон")],
    )
    window.set_last_transcript("первый")
    window.set_last_transcript("второй")
    window.set_last_transcript("третий")

    rendered = window.last_transcript.toPlainText()
    assert "Последняя:\nтретий" in rendered
    assert "Предыдущая:\nвторой" in rendered
    assert "первый" not in rendered
    window.prepare_to_quit()
    window.close()
    app.processEvents()


def test_corrupt_config_loads_fail_closed_without_overwriting_or_logging_contents(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    target = tmp_path / "Pressay" / "config.json"
    target.parent.mkdir(parents=True)
    corrupt_contents = '{"auto_insert": false, "secret-marker": BROKEN'
    target.write_text(corrupt_contents, encoding="utf-8")

    loaded = _load_config()

    assert loaded.warning is not None
    assert "автовставка" in loaded.warning.casefold()
    assert loaded.config.auto_insert is False
    assert loaded.config.smart_spacing is False
    assert loaded.config.voice_press_enter is False
    assert loaded.config.remove_fillers is False
    assert loaded.config.snippets == {}
    assert loaded.config.replacements == {}
    assert target.read_text(encoding="utf-8") == corrupt_contents
    assert "secret-marker" not in caplog.text


def test_valid_config_load_has_no_warning(monkeypatch) -> None:
    expected = AppConfig(auto_insert=False, language="ru")
    monkeypatch.setattr(AppConfig, "load", classmethod(lambda cls: expected))

    loaded = _load_config()

    assert loaded.config is expected
    assert loaded.warning is None


def test_overlay_hides_transient_states_but_not_active_work() -> None:
    assert _overlay_auto_hide_ms("ready") == 1500
    assert _overlay_auto_hide_ms("success") == 1500
    assert _overlay_auto_hide_ms("warning") == 1500
    assert _overlay_auto_hide_ms("error") == 1500
    assert _overlay_auto_hide_ms("recording") == 0
    assert _overlay_auto_hide_ms("processing") == 0


def test_native_shutdown_completes_without_hard_exit() -> None:
    class FakeController:
        def __init__(self) -> None:
            self.close_calls = 0
            self.wait_calls = 0

        def close(self) -> None:
            self.close_calls += 1

        def wait_closed(self, _timeout: float | None = None) -> bool:
            self.wait_calls += 1
            return True

    controller = FakeController()
    exits: list[int] = []
    complete, watchdog, coordinator = _start_native_shutdown(
        controller,  # type: ignore[arg-type]
        None,
        timeout_seconds=0.05,
        hard_exit=exits.append,
    )
    assert complete.wait(timeout=1)
    coordinator.join(timeout=1)
    watchdog.join(timeout=1)
    assert exits == []
    assert controller.close_calls == 1
    assert controller.wait_calls == 1


def test_shutdown_deadline_covers_blocking_recorder_cancel() -> None:
    class BlockingCancelRecorder:
        def __init__(self) -> None:
            self.is_recording = True
            self.entered = threading.Event()
            self.release = threading.Event()

        def cancel(self) -> bool:
            self.entered.set()
            assert self.release.wait(timeout=2)
            self.is_recording = False
            return True

    controller = DictationController(
        AppConfig(),
        status_callback=lambda *_args: None,
        result_callback=lambda *_args: None,
        notification_callback=lambda *_args: None,
    )
    recorder = BlockingCancelRecorder()
    controller._recorder = recorder  # type: ignore[assignment]

    exits: list[int] = []
    complete, watchdog, coordinator = _start_native_shutdown(
        controller,
        None,
        timeout_seconds=0.05,
        hard_exit=exits.append,
    )
    assert recorder.entered.wait(timeout=1)
    watchdog.join(timeout=1)
    assert exits == [0]
    assert complete.is_set() is False

    recorder.release.set()
    coordinator.join(timeout=1)
    assert complete.is_set()
