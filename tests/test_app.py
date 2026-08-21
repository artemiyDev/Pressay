from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from pressay.app import (
    _InputActionWorker,
    _SingleInstance,
    _build_microphone_test_handler,
    _effective_tray_status,
    _load_config,
    _microphones,
    _overlay_auto_hide_ms,
    _release_single_instance_after_shutdown,
    _report_hotkey_start_failure,
    _settings_dict,
    _save_settings_transaction,
    _start_native_shutdown,
    _test_microphone_device,
)
from pressay.controller import DictationController
from pressay.audio import AudioDevice
from pressay.config import AppConfig
from pressay.hotkey_bindings import HotkeyBindings
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


def test_single_instance_is_held_until_shutdown_completes() -> None:
    shutdown_complete = threading.Event()
    release_called = threading.Event()
    waiter_started = threading.Event()
    single_instance = SimpleNamespace(close=release_called.set)

    def release_after_shutdown() -> None:
        waiter_started.set()
        _release_single_instance_after_shutdown(
            single_instance,  # type: ignore[arg-type]
            shutdown_complete,
        )

    waiter = threading.Thread(target=release_after_shutdown, daemon=True)
    waiter.start()
    assert waiter_started.wait(timeout=1)
    assert release_called.is_set() is False

    shutdown_complete.set()
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert release_called.is_set()


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
    # Duplicate detection must use the same normalization as apply_replacements
    # (collapsed internal whitespace), not just a raw casefold of the alias.
    with pytest.raises(ValueError, match="Строка 2") as excinfo:
        parse_replacements("фаст  апи = FastAPI\nфаст апи = X")
    assert "повторяется" in str(excinfo.value)


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


class _FakeHotkeyFailureWindow:
    def __init__(self) -> None:
        self.runtime_warnings: list[str] = []

    def set_runtime_warning(self, message: str) -> None:
        self.runtime_warnings.append(message)


class _FakeHotkeyFailureTray:
    def __init__(self) -> None:
        self.states: list[tuple[str, str]] = []
        self.notifications: list[tuple[str, str, bool]] = []
        self.show_calls = 0

    def update_state(self, text: str, state: str) -> None:
        self.states.append((text, state))

    def notify(self, title: str, message: str, *, warning: bool = False) -> None:
        self.notifications.append((title, message, warning))

    def show_window(self) -> None:
        self.show_calls += 1


@pytest.mark.parametrize(
    ("background", "expected_show_calls"),
    ((True, 1), (False, 0)),
)
def test_macos_hotkey_failure_is_persistent_actionable_and_hides_raw_error(
    caplog,
    background: bool,
    expected_show_calls: int,
) -> None:
    window = _FakeHotkeyFailureWindow()
    tray = _FakeHotkeyFailureTray()
    secret_error = "permission details that must stay private"

    warning = _report_hotkey_start_failure(
        RuntimeError(secret_error),
        macos=True,
        background=background,
        window=window,
        tray=tray,
    )

    assert warning is not None
    assert window.runtime_warnings == [warning]
    assert "System Settings" in warning
    assert "Privacy & Security" in warning
    assert "Accessibility" in warning
    assert "Input Monitoring" in warning
    assert "полностью закройте" in warning
    assert tray.states == [("Нужны разрешения macOS", "error")]
    assert tray.notifications == [("Pressay", warning, True)]
    assert tray.show_calls == expected_show_calls
    assert secret_error not in warning
    assert all(
        secret_error not in message
        for _title, message, _warning in tray.notifications
    )
    assert secret_error in caplog.text
    assert "RuntimeError" in caplog.text


def test_late_ready_status_cannot_hide_macos_hotkey_tray_error() -> None:
    warning = "persistent permission warning"

    assert _effective_tray_status(
        "Готов — удерживайте Control+Option",
        "ready",
        warning,
    ) == ("Нужны разрешения macOS", "error")
    assert _effective_tray_status(
        "Ошибка config.json — автовставка отключена",
        "error",
        warning,
    ) == ("Нужны разрешения macOS", "error")
    assert _effective_tray_status("Слушаю…", "recording", None) == (
        "Слушаю…",
        "recording",
    )


def test_windows_hotkey_failure_is_sanitized_without_macos_onboarding(caplog) -> None:
    window = _FakeHotkeyFailureWindow()
    tray = _FakeHotkeyFailureTray()
    secret_error = "driver path that must stay private"

    warning = _report_hotkey_start_failure(
        RuntimeError(secret_error),
        macos=False,
        background=True,
        window=window,
        tray=tray,
    )

    assert warning is None
    assert window.runtime_warnings == []
    assert tray.states == [("Глобальные клавиши недоступны", "error")]
    assert tray.show_calls == 0
    assert tray.notifications == [
        (
            "Pressay",
            "Глобальные клавиши недоступны. Полностью перезапустите Pressay.",
            True,
        )
    ]
    assert all(
        secret_error not in message
        for _title, message, _warning in tray.notifications
    )
    assert secret_error in caplog.text
    assert "RuntimeError" in caplog.text


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


def test_native_shutdown_does_not_complete_while_hotkey_cleanup_is_live() -> None:
    controller = SimpleNamespace(
        close=lambda: None,
        wait_closed=lambda: True,
    )
    hotkeys = SimpleNamespace(stop=lambda: False)
    exits: list[int] = []

    complete, watchdog, coordinator = _start_native_shutdown(
        controller,
        hotkeys,
        timeout_seconds=0.03,
        hard_exit=exits.append,
    )

    watchdog.join(timeout=1)
    assert exits == [0]
    assert complete.is_set() is False
    assert coordinator.is_alive()


def test_microphone_device_normalizes_digit_strings_only() -> None:
    assert _test_microphone_device(None) is None
    assert _test_microphone_device("7") == 7
    assert _test_microphone_device("stable-usb-selector") == "stable-usb-selector"


class _FakeMicTestWindow:
    def __init__(self, settings: dict | None = None, *, error: str | None = None) -> None:
        self._settings = settings or {}
        self._error = error
        self.status_calls: list[tuple[str, str]] = []

    def current_settings(self) -> dict:
        if self._error is not None:
            raise ValueError(self._error)
        return self._settings

    def update_status(self, text: str, state: str) -> None:
        self.status_calls.append((text, state))


class _FakeMicTestTray:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, str, bool]] = []

    def notify(self, title: str, message: str, *, warning: bool = False) -> None:
        self.notifications.append((title, message, warning))


def test_microphone_test_handler_reports_form_error_without_raising() -> None:
    window = _FakeMicTestWindow(error="строка должна содержать знак =")
    tray = _FakeMicTestTray()

    handler = _build_microphone_test_handler(
        window=window,
        tray=tray,
        status_callback=lambda *_args: None,
        notification_callback=lambda *_args: None,
    )

    handler()  # must not raise ValueError out of the Qt slot

    assert window.status_calls == [("строка должна содержать знак =", "error")]
    assert tray.notifications == [
        ("Pressay", "строка должна содержать знак =", True)
    ]


def test_microphone_test_handler_never_saves_settings(monkeypatch) -> None:
    def _forbidden_save(self) -> None:
        raise AssertionError("test_microphone must not write config.json")

    monkeypatch.setattr(AppConfig, "save", _forbidden_save)
    window = _FakeMicTestWindow({"microphone": None})
    tray = _FakeMicTestTray()
    done = threading.Event()

    def status_callback(_text: str, _state: str) -> None:
        done.set()

    handler = _build_microphone_test_handler(
        window=window,
        tray=tray,
        status_callback=status_callback,
        notification_callback=lambda *_args: None,
        recorder_factory=lambda device: SimpleNamespace(warmup=lambda _s: 48_000),
    )

    handler()

    assert done.wait(timeout=1)


def test_microphone_test_handler_uses_form_device_not_saved_config() -> None:
    window = _FakeMicTestWindow({"microphone": "5"})
    tray = _FakeMicTestTray()
    seen_devices: list[object] = []
    done = threading.Event()

    def recorder_factory(device: object) -> SimpleNamespace:
        seen_devices.append(device)
        return SimpleNamespace(warmup=lambda _s: 44_100)

    def status_callback(_text: str, _state: str) -> None:
        done.set()

    handler = _build_microphone_test_handler(
        window=window,
        tray=tray,
        status_callback=status_callback,
        notification_callback=lambda *_args: None,
        recorder_factory=recorder_factory,
    )

    handler()

    assert done.wait(timeout=1)
    assert seen_devices == [5]


def test_microphone_test_handler_runs_off_the_calling_thread() -> None:
    window = _FakeMicTestWindow({"microphone": None})
    tray = _FakeMicTestTray()
    entered = threading.Event()
    release = threading.Event()
    status_events: list[tuple[str, str]] = []

    class BlockingRecorder:
        def __init__(self, device: object) -> None:
            self.device = device

        def warmup(self, _duration: float) -> int:
            entered.set()
            assert release.wait(timeout=2)
            return 16_000

    handler = _build_microphone_test_handler(
        window=window,
        tray=tray,
        status_callback=lambda text, state: status_events.append((text, state)),
        notification_callback=lambda *_args: None,
        recorder_factory=BlockingRecorder,
    )

    handler()

    # The call above must return before the blocking warmup() unblocks.
    assert entered.wait(timeout=1)
    assert status_events == []
    release.set()

    deadline = time.monotonic() + 1
    while not status_events and time.monotonic() < deadline:
        time.sleep(0.01)
    assert status_events == [("Микрофон готов: 16000 Hz", "success")]


def test_microphone_test_handler_reports_recorder_failure() -> None:
    window = _FakeMicTestWindow({"microphone": None})
    tray = _FakeMicTestTray()
    status_events: list[tuple[str, str]] = []
    notifications: list[tuple[str, str, bool]] = []
    done = threading.Event()

    def recorder_factory(device: object) -> SimpleNamespace:
        raise RuntimeError("микрофон занят")

    def status_callback(text: str, state: str) -> None:
        status_events.append((text, state))
        done.set()

    def notification_callback(title: str, message: str, warning: bool) -> None:
        notifications.append((title, message, warning))

    handler = _build_microphone_test_handler(
        window=window,
        tray=tray,
        status_callback=status_callback,
        notification_callback=notification_callback,
        recorder_factory=recorder_factory,
    )

    handler()

    assert done.wait(timeout=1)
    assert status_events == [("Микрофон недоступен", "error")]
    assert notifications == [("Pressay", "микрофон занят", True)]


def test_input_action_worker_submit_returns_before_action_completes() -> None:
    worker = _InputActionWorker()
    entered = threading.Event()
    release = threading.Event()

    def blocking_action() -> None:
        entered.set()
        assert release.wait(timeout=2)

    started_at = time.monotonic()
    worker.submit(blocking_action)
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.2
    assert entered.wait(timeout=1)
    release.set()
    assert worker.shutdown().wait(timeout=1)


def test_input_action_worker_serializes_queued_requests() -> None:
    worker = _InputActionWorker()
    order: list[str] = []
    first_entered = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()

    def first() -> None:
        first_entered.set()
        assert release_first.wait(timeout=2)
        order.append("first")

    def second() -> None:
        order.append("second")
        second_done.set()

    worker.submit(first)
    assert first_entered.wait(timeout=1)
    worker.submit(second)
    time.sleep(0.05)
    assert order == []  # second must not run while first still holds the worker

    release_first.set()
    assert second_done.wait(timeout=1)
    assert order == ["first", "second"]
    assert worker.shutdown().wait(timeout=1)


def test_input_action_worker_survives_action_exception() -> None:
    worker = _InputActionWorker()
    done = threading.Event()

    def failing_action() -> None:
        raise RuntimeError("clipboard exploded")

    def next_action() -> None:
        done.set()

    worker.submit(failing_action)
    worker.submit(next_action)

    assert done.wait(timeout=1)
    assert worker.shutdown().wait(timeout=1)


def test_input_action_worker_shutdown_closes_gate_and_cancels_queued_actions() -> None:
    worker = _InputActionWorker()
    first_entered = threading.Event()
    release_first = threading.Event()
    queued_effect = threading.Event()
    rejected_effect = threading.Event()

    def first() -> None:
        first_entered.set()
        assert release_first.wait(timeout=2)

    assert worker.submit(first) is True
    assert first_entered.wait(timeout=1)
    assert worker.submit(queued_effect.set) is True

    shutdown_complete = worker.shutdown()

    assert worker.submit(rejected_effect.set) is False
    assert worker._shutdown_thread is not None
    assert worker._shutdown_thread.daemon is True
    assert shutdown_complete.is_set() is False
    release_first.set()
    assert shutdown_complete.wait(timeout=1)
    assert worker.shutdown() is shutdown_complete
    assert queued_effect.is_set() is False
    assert rejected_effect.is_set() is False


def test_input_action_worker_shutdown_failure_keeps_completion_unset() -> None:
    worker = _InputActionWorker()
    real_executor = worker._executor

    class FailingExecutor:
        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            assert cancel_futures is True
            if wait:
                raise RuntimeError("simulated executor join failure")

    worker._executor = FailingExecutor()  # type: ignore[assignment]
    try:
        shutdown_complete = worker.shutdown()
        assert worker._shutdown_thread is not None
        worker._shutdown_thread.join(timeout=1)

        assert not worker._shutdown_thread.is_alive()
        assert shutdown_complete.is_set() is False
    finally:
        real_executor.shutdown(wait=True, cancel_futures=True)


def test_native_shutdown_cancels_queued_input_and_waits_for_running_action() -> None:
    controller = SimpleNamespace(
        close=lambda: None,
        wait_closed=lambda: True,
    )
    worker = _InputActionWorker()
    first_entered = threading.Event()
    release_first = threading.Event()
    queued_effect = threading.Event()
    exits: list[int] = []

    def first() -> None:
        first_entered.set()
        assert release_first.wait(timeout=2)

    assert worker.submit(first) is True
    assert first_entered.wait(timeout=1)
    assert worker.submit(queued_effect.set) is True
    complete, watchdog, coordinator = _start_native_shutdown(
        controller,
        None,
        worker,
        timeout_seconds=0.5,
        hard_exit=exits.append,
    )
    try:
        assert complete.wait(timeout=0.03) is False
        assert worker.submit(queued_effect.set) is False
    finally:
        release_first.set()

    assert complete.wait(timeout=1)
    coordinator.join(timeout=1)
    watchdog.join(timeout=1)
    assert not coordinator.is_alive()
    assert exits == []
    assert queued_effect.is_set() is False


def test_shutdown_deadline_covers_blocking_input_action() -> None:
    controller = SimpleNamespace(
        close=lambda: None,
        wait_closed=lambda: True,
    )
    worker = _InputActionWorker()
    entered = threading.Event()
    release = threading.Event()
    exits: list[int] = []

    def blocking_action() -> None:
        entered.set()
        assert release.wait(timeout=2)

    assert worker.submit(blocking_action) is True
    assert entered.wait(timeout=1)
    complete, watchdog, coordinator = _start_native_shutdown(
        controller,
        None,
        worker,
        timeout_seconds=0.03,
        hard_exit=exits.append,
    )
    try:
        watchdog.join(timeout=1)
        assert exits == [0]
        assert complete.is_set() is False
        assert coordinator.is_alive()
    finally:
        release.set()

    assert complete.wait(timeout=1)
    coordinator.join(timeout=1)
    assert not coordinator.is_alive()


def test_settings_transaction_cancels_capture_before_deferring_changed_hotkeys(
    monkeypatch,
) -> None:
    previous = HotkeyBindings()
    updated = AppConfig(
        hotkeys=HotkeyBindings(hold_modifiers=("ctrl", "shift"))
    )
    order: list[str] = []

    class FakeCoordinator:
        def request_change(
            self,
            bindings,
            *,
            before_replace,
            persist,
            on_applied,
            on_failed,
        ):
            assert bindings == updated.hotkeys
            order.append("request")
            self.before_replace = before_replace
            self.persist = persist
            self.on_applied = on_applied
            self.on_failed = on_failed
            return SimpleNamespace()

    coordinator = FakeCoordinator()
    monkeypatch.setattr(
        AppConfig,
        "save",
        lambda self: order.append("persist"),
    )

    _save_settings_transaction(
        updated,
        coordinator,  # type: ignore[arg-type]
        previous_hotkeys=previous,
        before_hotkey_change=lambda: order.append("cancel"),
        on_applied=lambda config: order.append(f"apply:{config.hotkeys.hold_label()}"),
        on_failed=lambda error: pytest.fail(str(error)),
    )

    assert order == ["request"]
    coordinator.before_replace()
    coordinator.persist()
    coordinator.on_applied()
    assert order == ["request", "cancel", "persist", "apply:Ctrl+Shift"]
