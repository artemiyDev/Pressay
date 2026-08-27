"""Qt/Win32 entry point for the desktop application."""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import ctypes
from dataclasses import dataclass
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import sys
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from . import hotkey_bindings
from . import __version__
from .audio import (
    AudioCaptureError,
    AudioRecorder,
    LEGACY_MICROPHONE_SELECTOR_PREFIX,
    MICROPHONE_SELECTOR_PREFIX,
    collapse_input_device_variants,
    normalize_device_selector,
    parse_microphone_selector,
)
from .config import AppConfig, ConfigError
from .controller import DictationController
from .hotkey_coordinator import _WindowsHotkeyCoordinator
from .microphone_probe import MicrophoneProbeCoordinator, MicrophoneProbeResult
from .ui import (
    MicrophoneChoice,
    SettingsWindow,
    StatusOverlay,
    TrayController,
    UiSignals,
    microphone_choice_index,
)
from .platform_support import input_adapter, is_macos, is_windows, user_data_directory


LOGGER = logging.getLogger(__name__)
_MICROPHONE_QUIET_RMS = 0.003
_MICROPHONE_CLIPPING_PEAK = 0.98


@dataclass(frozen=True, slots=True)
class _ConfigLoadResult:
    """Validated startup settings plus an optional user-facing warning."""

    config: AppConfig
    warning: str | None = None


class _SingleInstance:
    """Per-user Windows mutex preventing duplicate hooks and tray icons."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str = "Local\\Pressay.Desktop.Singleton") -> None:
        self._handle: int | None = None
        self._lock_stream: Any | None = None
        self._name = name

    def acquire(self) -> bool:
        if is_macos():
            import fcntl

            lock_path = user_data_directory() / "pressay.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            stream = lock_path.open("a+b")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                stream.close()
                return False
            self._lock_stream = stream
            return True
        if not is_windows():
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, self._name)
        if not handle:
            return False
        if ctypes.get_last_error() == self.ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = int(handle)
        return True

    def close(self) -> None:
        if self._lock_stream is not None:
            try:
                import fcntl

                fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_stream.close()
                self._lock_stream = None
            return
        if self._handle is None or not is_windows():
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        kernel32.CloseHandle(self._handle)
        self._handle = None


class _MainThreadDispatcher(QObject):
    requested = Signal(object, object, object)

    def __init__(self) -> None:
        super().__init__()
        self.requested.connect(self._run)

    @staticmethod
    def _run(callback: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        callback(*args, **kwargs)


class _InputActionWorker:
    """Serialize paste/copy clipboard transactions off the Qt thread.

    A single-worker ``ThreadPoolExecutor`` (rather than a bare
    ``threading.Lock``) is used so a burst of paste/copy requests queues
    cleanly instead of spawning a new thread per request that then blocks on a
    lock. Shutdown cancels work that has not started and tracks the executor's
    non-daemon worker under the process-wide deadline.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="pressay-input"
        )
        self._state_lock = threading.Lock()
        self._closed = False
        self._shutdown_complete = threading.Event()
        self._shutdown_thread: threading.Thread | None = None

    def submit(self, action: Callable[[], Any]) -> bool:
        with self._state_lock:
            if self._closed:
                return False
            self._executor.submit(self._run, action)
            return True

    @staticmethod
    def _run(action: Callable[[], Any]) -> None:
        try:
            action()
        except Exception:
            # A failed paste/copy transaction must not strand the single
            # serializing worker; the next queued request still has to run.
            LOGGER.exception("input_action_failed")

    def shutdown(self) -> threading.Event:
        """Close submissions immediately and finish the executor off-thread."""

        with self._state_lock:
            if self._closed:
                return self._shutdown_complete
            self._closed = True
            # Cancel pending clipboard/input effects synchronously. The second
            # blocking shutdown below only waits for the action already running.
            self._executor.shutdown(wait=False, cancel_futures=True)
            thread = threading.Thread(
                target=self._wait_for_shutdown,
                name="pressay-input-close",
                daemon=True,
            )
            self._shutdown_thread = thread
            thread.start()
            return self._shutdown_complete

    def _wait_for_shutdown(self) -> None:
        try:
            self._executor.shutdown(wait=True, cancel_futures=True)
        except Exception:
            LOGGER.exception("input_worker_shutdown_failed")
            return
        self._shutdown_complete.set()


def _save_settings_transaction(
    updated: AppConfig,
    coordinator: _WindowsHotkeyCoordinator | None,
    *,
    previous_hotkeys: Any,
    before_hotkey_change: Callable[[], Any],
    on_applied: Callable[[AppConfig], None],
    on_failed: Callable[[BaseException], None],
) -> Future[bool] | None:
    """Persist only through the serialized runtime-hotkey commit path."""

    if coordinator is not None:
        return coordinator.request_change(
            updated.hotkeys,
            before_replace=(
                before_hotkey_change
                if updated.hotkeys != previous_hotkeys
                else None
            ),
            persist=updated.save,
            on_applied=lambda: on_applied(updated),
            on_failed=on_failed,
        )
    try:
        updated.save()
    except ConfigError as exc:
        on_failed(exc)
        return None
    on_applied(updated)
    return None


def _configure_logging() -> None:
    base = user_data_directory()
    try:
        base.mkdir(parents=True, exist_ok=True)
        log_path = base / "pressay.log"
        root_logger = logging.getLogger()
        if any(
            bool(getattr(handler, "_pressay_rotating_file", False))
            for handler in root_logger.handlers
        ):
            return
        handler = RotatingFileHandler(
            log_path,
            maxBytes=1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler._pressay_rotating_file = True  # type: ignore[attr-defined]
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)
    except OSError:
        logging.basicConfig(level=logging.INFO)


def _overlay_auto_hide_ms(state: str) -> int:
    return 1500 if state in {"ready", "success", "warning", "error"} else 0


_MACOS_HOTKEY_PERMISSION_WARNING = (
    "Глобальные клавиши недоступны. Откройте System Settings → Privacy & "
    "Security → Accessibility и Input Monitoring, разрешите Pressay (или "
    "Python, если macOS показывает его), затем полностью закройте и снова "
    "запустите Pressay."
)
_MACOS_HOTKEY_TRAY_ERROR = "Нужны разрешения macOS"


def _effective_tray_status(
    text: str,
    state: str,
    runtime_warning: str | None,
) -> tuple[str, str]:
    """Keep a fatal startup warning visible across later routine statuses."""

    if runtime_warning:
        return _MACOS_HOTKEY_TRAY_ERROR, "error"
    return text, state


def _report_hotkey_start_failure(
    error: BaseException,
    *,
    macos: bool,
    background: bool,
    window: Any,
    tray: Any,
) -> str | None:
    """Report a global-hotkey startup failure without exposing error details."""

    LOGGER.error(
        "hotkey_service_failed type=%s",
        type(error).__name__,
        exc_info=(type(error), error, error.__traceback__),
    )
    if macos:
        message = _MACOS_HOTKEY_PERMISSION_WARNING
        window.set_runtime_warning(message)
        tray.update_state(_MACOS_HOTKEY_TRAY_ERROR, "error")
        tray.notify("Pressay", message, warning=True)
        if background:
            tray.show_window()
        return message

    message = "Глобальные клавиши недоступны. Полностью перезапустите Pressay."
    tray.update_state("Глобальные клавиши недоступны", "error")
    tray.notify("Pressay", message, warning=True)
    return None


def _start_shutdown_watchdog(
    shutdown_complete: threading.Event,
    *,
    timeout_seconds: float = 3.0,
    hard_exit: Any = os._exit,
) -> threading.Thread:
    """Enforce an overall deadline without touching potentially stuck locks.

    Logging handlers and their locks may themselves be held by a stuck worker,
    so the deadline thread deliberately performs no logging or flushing. The
    rotating file handler flushes normal records as they are emitted.
    """

    def watch() -> None:
        if shutdown_complete.wait(timeout_seconds):
            return
        hard_exit(0)

    thread = threading.Thread(
        target=watch,
        name="pressay-shutdown-watchdog",
        daemon=True,
    )
    thread.start()
    return thread


def _start_native_shutdown(
    controller: DictationController,
    hotkey_service: Any | None,
    input_worker: _InputActionWorker | None = None,
    microphone_probe: MicrophoneProbeCoordinator | None = None,
    *,
    timeout_seconds: float = 3.0,
    hard_exit: Any = os._exit,
) -> tuple[threading.Event, threading.Thread, threading.Thread]:
    """Stop native services off the GUI thread under one hard deadline.

    Controller/PortAudio cleanup, the low-level keyboard hook and the serialized
    input executor are independent, so one stuck native call cannot prevent the
    other cleanup from starting. The watchdog is started first and also covers
    a synchronous hang inside ``controller.close()`` or an in-flight clipboard
    transaction.
    """

    shutdown_complete = threading.Event()
    watchdog = _start_shutdown_watchdog(
        shutdown_complete,
        timeout_seconds=timeout_seconds,
        hard_exit=hard_exit,
    )
    controller_call_done = threading.Event()
    hotkey_done = threading.Event()
    input_done = threading.Event()
    microphone_probe_done = threading.Event()
    if microphone_probe is None:
        microphone_probe_done.set()
    else:
        try:
            # This only closes the gate and sets a cancellation event. Native
            # cancel/PortAudio close remain on the probe worker.
            microphone_probe_done = microphone_probe.shutdown()
        except Exception:
            LOGGER.exception("microphone_probe_shutdown_start_failed")
    if input_worker is None:
        input_done.set()
    else:
        try:
            input_done = input_worker.shutdown()
        except Exception:
            # Keep input_done unset so the watchdog remains authoritative if
            # the executor could not enter its bounded shutdown path.
            LOGGER.exception("input_worker_shutdown_start_failed")

    def close_controller() -> None:
        try:
            controller.close()
        except Exception:
            LOGGER.exception("controller_close_failed")
        finally:
            controller_call_done.set()

    def stop_hotkeys() -> None:
        stopped_cleanly = hotkey_service is None
        try:
            if hotkey_service is not None:
                stopped_cleanly = hotkey_service.stop() is not False
                if not stopped_cleanly:
                    LOGGER.error("hotkey_stop_incomplete")
        except Exception:
            LOGGER.exception("hotkey_stop_failed")
        finally:
            # An explicit False means the service retained live native threads
            # for background cleanup.  Do not declare shutdown complete: the
            # process watchdog remains the final bound for an orphaned hook.
            if stopped_cleanly:
                hotkey_done.set()

    controller_thread = threading.Thread(
        target=close_controller,
        name="pressay-controller-close",
        daemon=True,
    )
    hotkey_thread = threading.Thread(
        target=stop_hotkeys,
        name="pressay-hotkey-close",
        daemon=True,
    )
    controller_thread.start()
    hotkey_thread.start()

    def coordinate() -> None:
        controller_call_done.wait()
        hotkey_done.wait()
        input_done.wait()
        microphone_probe_done.wait()
        try:
            native_closed = controller.wait_closed()
        except Exception:
            LOGGER.exception("controller_close_wait_failed")
            return
        if native_closed:
            shutdown_complete.set()

    coordinator = threading.Thread(
        target=coordinate,
        name="pressay-shutdown-coordinator",
        daemon=True,
    )
    coordinator.start()
    return shutdown_complete, watchdog, coordinator


def _release_single_instance_after_shutdown(
    single_instance: _SingleInstance,
    shutdown_complete: threading.Event,
) -> None:
    """Keep the process lock until every bounded shutdown participant is done."""

    shutdown_complete.wait()
    single_instance.close()


def _load_config() -> _ConfigLoadResult:
    try:
        return _ConfigLoadResult(AppConfig.load())
    except ConfigError:
        # Never include file contents or parser details in the log. More
        # importantly, a damaged/manual config must not silently restore the
        # default auto-insert behaviour. The original file is deliberately
        # left untouched until the user explicitly saves valid settings.
        LOGGER.warning("configuration_load_failed: ConfigError")
        safe_config = AppConfig(
            auto_insert=False,
            smart_spacing=False,
            remove_fillers=False,
            voice_press_enter=False,
            voice_formatting=False,
            voice_translate=False,
            snippets={},
            replacements={},
        )
        warning = (
            "Не удалось прочитать config.json: файл повреждён или содержит "
            "недопустимую настройку. Автовставка и голосовые команды отключены; "
            "исходный файл не изменён. Проверьте настройки и сохраните их вручную."
        )
        return _ConfigLoadResult(safe_config, warning)


def _unavailable_microphone_name(value: object) -> str:
    parsed = parse_microphone_selector(value)
    if parsed is not None:
        return parsed[0]
    if type(value) is int or (isinstance(value, str) and value.isdecimal()):
        return f"устройство #{int(value)}"
    if isinstance(value, str):
        display = value.strip()
        if display.startswith(
            (MICROPHONE_SELECTOR_PREFIX, LEGACY_MICROPHONE_SELECTOR_PREFIX)
        ):
            return "сохранённый микрофон"
        if display:
            return display
    return "сохранённый микрофон"


def _microphones(selected: object = None) -> list[MicrophoneChoice]:
    result = [MicrophoneChoice(None, "Системный микрофон по умолчанию")]
    try:
        enumerated_devices = AudioRecorder.list_input_devices()
        devices = collapse_input_device_variants(enumerated_devices, selected)
        if len(devices) != len(enumerated_devices):
            LOGGER.info(
                "microphone_driver_variants_collapsed enumerated=%d displayed=%d",
                len(enumerated_devices),
                len(devices),
            )
    except AudioCaptureError:
        devices = []
    seen: set[str] = set()
    for device in sorted(devices, key=lambda item: (not item.is_default, item.index)):
        selector = device.stable_selector
        if selector in seen:
            continue
        seen.add(selector)
        suffix_parts: list[str] = []
        if device.host_api.strip().casefold() == "windows wasapi":
            suffix_parts.append("рекомендуется")
        if device.is_default:
            suffix_parts.append("по умолчанию")
        suffix = f" — {', '.join(suffix_parts)}" if suffix_parts else ""
        host_api = f", {device.host_api}" if device.host_api else ""
        variants = (
            f"; вариантов драйвера: {device.variant_count}"
            if device.variant_count > 1
            else ""
        )
        result.append(
            MicrophoneChoice(
                selector,
                (
                    f"{device.name} ({device.default_sample_rate} Hz{host_api}"
                    f"{variants}){suffix}"
                ),
                legacy_index=device.index,
                device_name=device.name,
                is_default=device.is_default,
            )
        )
    if selected is not None and microphone_choice_index(result, selected) < 0:
        result.append(
            MicrophoneChoice(
                selected,  # exact legacy/stable value must survive a no-op save
                f"Недоступен: {_unavailable_microphone_name(selected)}",
                available=False,
            )
        )
    return result


def _settings_dict(config: AppConfig) -> dict[str, Any]:
    return {
        "microphone": config.microphone,
        "language": config.language,
        "model": config.model,
        "resource_mode": config.resource_mode,
        "auto_insert": config.auto_insert,
        "smart_spacing": config.smart_spacing,
        "remove_fillers": config.remove_fillers,
        "press_enter": config.voice_press_enter,
        "voice_formatting": config.voice_formatting,
        "voice_translate": config.voice_translate,
        "translate_model": config.translate_model,
        "strict_editable_check": config.strict_editable_check,
        "replacements": dict(config.replacements),
        "hotkeys": config.hotkeys.to_mapping(),
    }


def _test_microphone_device(value: Any) -> int | str | None:
    """Normalize a combo-box selection the same way the controller does.

    The form stores the stable selector (``str | None``) chosen by the user;
    a purely numeric string is a legacy device index and must become ``int``.
    """

    return normalize_device_selector(value)


def _build_microphone_test_handler(
    *,
    window: Any,
    tray: Any,
    microphone_probe: MicrophoneProbeCoordinator,
) -> Callable[[], None]:
    """Build the microphone-test slot: no settings save, form's own device.

    Reading the form happens synchronously on the caller (Qt) thread, exactly
    like ``_emit_save`` in ``ui.py``. A ``ValueError`` from a broken personal
    dictionary is reported the same way, without ever writing config.json.
    """

    def test_microphone() -> None:
        try:
            values = window.current_settings()
        except ValueError as exc:
            window.update_status(str(exc), "error")
            tray.notify("Pressay", str(exc), warning=True)
            return
        device = _test_microphone_device(values.get("microphone"))

        def complete(result: MicrophoneProbeResult) -> None:
            text, state, notification = _microphone_probe_presentation(result)
            outcome = _microphone_probe_outcome(result)
            LOGGER.info(
                "microphone_probe_completed outcome=%s sample_rate=%s "
                "rms_dbfs=%s peak_rms_dbfs=%s peak_dbfs=%s",
                outcome,
                result.sample_rate,
                _format_dbfs_for_log(result.rms),
                _format_dbfs_for_log(result.peak_rms),
                _format_dbfs_for_log(result.peak),
            )
            window.finish_microphone_test(
                text,
                state,
                rms=result.peak_rms,
                peak=result.peak,
            )
            if notification is not None:
                tray.notify("Pressay", notification, warning=True)

        if microphone_probe.running:
            return
        window.begin_microphone_test()
        started = microphone_probe.start(
            device,
            on_level=window.update_microphone_test_level,
            on_complete=complete,
        )
        if not started and not microphone_probe.running:
            message = "Проверка микрофона сейчас недоступна. Перезапустите Pressay."
            window.finish_microphone_test(message, "error")
            tray.notify("Pressay", message, warning=True)

    return test_microphone


def _amplitude_dbfs(value: float) -> float | None:
    """Convert a finite positive full-scale amplitude to bounded dBFS."""

    if not math.isfinite(value) or value <= 0:
        return None
    return max(-120.0, min(0.0, 20.0 * math.log10(min(value, 1.0))))


def _format_dbfs_for_log(value: float) -> str:
    decibels = _amplitude_dbfs(value)
    return "none" if decibels is None else f"{decibels:.1f}"


def _microphone_probe_outcome(result: MicrophoneProbeResult) -> str:
    if result.error_kind is not None:
        return result.error_kind
    if not result.signal_detected:
        return "silent"
    if math.isfinite(result.peak) and result.peak >= _MICROPHONE_CLIPPING_PEAK:
        return "clipping"
    if (
        not math.isfinite(result.peak_rms)
        or result.peak_rms < _MICROPHONE_QUIET_RMS
    ):
        return "quiet"
    return "normal"


def _microphone_probe_level_text(result: MicrophoneProbeResult) -> str:
    decibels = _amplitude_dbfs(result.peak_rms)
    level = "не измерен" if decibels is None else f"{decibels:.0f} dBFS"
    rate = result.sample_rate
    if rate is None or rate <= 0:
        return level
    frequency = f"{rate // 1000} кГц" if rate % 1000 == 0 else f"{rate} Гц"
    return f"{level}, {frequency}"


def _microphone_probe_presentation(
    result: MicrophoneProbeResult,
) -> tuple[str, str, str | None]:
    outcome = _microphone_probe_outcome(result)
    if outcome == "normal":
        return (
            f"Уровень микрофона нормальный ({_microphone_probe_level_text(result)})",
            "success",
            None,
        )
    if outcome == "quiet":
        message = (
            "Сигнал есть, но уровень слишком тихий "
            f"({_microphone_probe_level_text(result)}). Подойдите ближе к "
            "микрофону или увеличьте уровень входа."
        )
        return message, "warning", message
    if outcome == "clipping":
        peak = _amplitude_dbfs(result.peak)
        peak_text = "0 dBFS" if peak is None else f"{peak:.1f} dBFS"
        message = (
            f"Микрофон перегружается (пик {peak_text}). Уменьшите уровень "
            "входа или отодвиньтесь от микрофона."
        )
        return message, "warning", message
    if outcome == "silent":
        message = (
            "Сигнал не обнаружен. Проверьте выбранный микрофон, уровень входа "
            "и разрешение на доступ."
        )
        return message, "warning", message
    if result.error_kind == "device":
        message = (
            "Выбранный микрофон недоступен. Выберите другой микрофон или "
            "освободите его в настройках звука."
        )
        return message, "error", message
    if result.error_kind == "stream":
        message = (
            "Поток микрофона прерван. Закройте приложение, которое использует "
            "микрофон, и повторите проверку."
        )
        return message, "error", message
    message = (
        "Не удалось проверить микрофон. Проверьте разрешение на доступ и "
        "настройки звука."
    )
    return message, "error", message


def _snapshot_target(*, strict_editable_check: bool = False) -> Any | None:
    try:
        adapter = input_adapter()
        target = adapter.snapshot_foreground_target()
        # The adapter is chosen dynamically; a future third platform without
        # describe_focus must still produce a loggable (if uninformative) line.
        describe_focus = getattr(adapter, "describe_focus", None)
        focus_info = (
            describe_focus(target)
            if callable(describe_focus)
            else {
                "focus_kind": "none",
                "control_type": None,
                "enabled": None,
                "keyboard_focusable": None,
                "value_writable": None,
                "text_editable": None,
                "caret_active": None,
                "win32_caret": None,
                "refetched": None,
            }
        )
        LOGGER.info(
            "recording_target_captured valid=%s editable=%s hwnd=%s pid=%s "
            "focus_kind=%s control_type=%s enabled=%s focusable=%s "
            "value_writable=%s text_editable=%s caret=%s win32_caret=%s "
            "refetched=%s",
            bool(getattr(target, "is_valid", False)),
            adapter.target_looks_editable(target, strict=strict_editable_check),
            int(getattr(target, "hwnd", 0) or 0),
            int(getattr(target, "pid", 0) or 0),
            focus_info.get("focus_kind", "none"),
            focus_info.get("control_type"),
            focus_info.get("enabled"),
            focus_info.get("keyboard_focusable"),
            focus_info.get("value_writable"),
            focus_info.get("text_editable"),
            focus_info.get("caret_active"),
            focus_info.get("win32_caret"),
            focus_info.get("refetched"),
        )
        return target
    except Exception as exc:
        LOGGER.warning("recording_target_capture_failed: %s", type(exc).__name__)
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--background", action="store_true", help="Start in the system tray")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    _configure_logging()
    single_instance = _SingleInstance()
    if not single_instance.acquire():
        LOGGER.info("secondary_instance_exited")
        if not args.background and is_windows():
            ctypes.windll.user32.MessageBoxW(
                None,
                "Pressay уже работает. Откройте его значок в системном трее.",
                "Pressay",
                0x40,
            )
        elif not args.background:
            print("Pressay is already running in the menu bar.", file=sys.stderr)
        return 0
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Pressay")
    app.setOrganizationName("Local")
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        LOGGER.error("system_tray_unavailable")

    config_load = _load_config()
    config = config_load.config
    signals = UiSignals()
    window = SettingsWindow(
        signals,
        _settings_dict(config),
        _microphones(config.microphone),
    )
    tray = TrayController(signals, window)
    app.aboutToQuit.connect(window.prepare_to_quit)

    dispatcher = _MainThreadDispatcher()
    hotkey_runtime_warning: str | None = None

    def dispatch_ui(callback: Any, *args: Any, **kwargs: Any) -> None:
        dispatcher.requested.emit(callback, args, kwargs)

    microphone_probe = MicrophoneProbeCoordinator(dispatch_ui)

    def status_callback(text: str, state: str) -> None:
        def update() -> None:
            window.update_status(text, state)
            tray_text, tray_state = _effective_tray_status(
                text,
                state,
                hotkey_runtime_warning,
            )
            tray.update_state(tray_text, tray_state)
            auto_hide = _overlay_auto_hide_ms(state)
            overlay.show_status(text, state, auto_hide_ms=auto_hide)

        dispatch_ui(update)

    def result_callback(text: str) -> None:
        dispatch_ui(window.set_last_transcript, text)

    def notification_callback(title: str, message: str, warning: bool) -> None:
        dispatch_ui(tray.notify, title, message, warning=warning)

    def model_ready_callback(model: str, device: str, compute_type: str) -> None:
        dispatch_ui(window.update_active_model, model, device, compute_type)

    controller = DictationController(
        config,
        status_callback=status_callback,
        result_callback=result_callback,
        notification_callback=notification_callback,
        model_ready_callback=model_ready_callback,
    )
    overlay = StatusOverlay(
        level_provider=controller.current_recording_rms,
        translation_provider=lambda: controller.translating,
    )

    def start_or_stop(*, capture_target: bool = False) -> None:
        if controller.is_recording:
            controller.request_stop_recording()
        else:
            controller.request_start_recording(
                target=(
                    _snapshot_target(
                        strict_editable_check=config.strict_editable_check
                    )
                    if capture_target
                    else None
                )
            )

    # paste_last/copy_last touch Win32 clipboard/COM with retries that can take
    # up to roughly a second; both the Qt-signal path and the hotkey path run
    # them on this one serialized worker so they never block the GUI thread.
    input_worker = _InputActionWorker()

    signals.toggle_requested.connect(lambda: start_or_stop(capture_target=False))
    signals.cancel_requested.connect(controller.request_cancel)
    signals.paste_last_requested.connect(lambda: input_worker.submit(controller.paste_last))
    signals.copy_last_requested.connect(lambda: input_worker.submit(controller.copy_last))

    hotkey_coordinator: _WindowsHotkeyCoordinator | None = None

    def apply_saved_settings(updated: AppConfig) -> None:
        nonlocal config
        previous_resource_mode = config.resource_mode
        config = updated
        controller.update_config(config)
        if previous_resource_mode == "eco" and config.resource_mode != "eco":
            controller.warmup_model()
        window.update_status("Настройки сохранены", "success")

    def report_settings_failure(error: BaseException) -> None:
        message = f"Не удалось применить настройки: {error}"
        window.update_status(message, "error")
        tray.notify("Pressay", message, warning=True)

    def save_settings(values: dict[str, Any]) -> None:
        microphone = values.get("microphone")
        try:
            hotkeys = hotkey_bindings.from_mapping(
                values.get("hotkeys", config.hotkeys.to_mapping())
            )
        except hotkey_bindings.HotkeyBindingError as exc:
            message = f"Горячие клавиши: {exc}"
            window.update_status(message, "error")
            tray.notify("Pressay", message, warning=True)
            return
        updated = AppConfig(
            model=str(values.get("model", config.model)),
            language=str(values.get("language", config.language)),
            microphone=microphone,
            auto_insert=bool(values.get("auto_insert", config.auto_insert)),
            smart_spacing=bool(values.get("smart_spacing", config.smart_spacing)),
            remove_fillers=bool(values.get("remove_fillers", config.remove_fillers)),
            voice_press_enter=bool(values.get("press_enter", config.voice_press_enter)),
            voice_formatting=bool(values.get("voice_formatting", config.voice_formatting)),
            voice_translate=bool(values.get("voice_translate", config.voice_translate)),
            translate_model=str(values.get("translate_model", config.translate_model)),
            strict_editable_check=bool(
                values.get("strict_editable_check", config.strict_editable_check)
            ),
            resource_mode=str(values.get("resource_mode", config.resource_mode)),
            snippets=dict(config.snippets),
            replacements=dict(values.get("replacements", config.replacements)),
            hotkeys=hotkeys,
        )
        if hotkey_coordinator is not None:
            window.update_status("Применяю настройки…", "processing")
        _save_settings_transaction(
            updated,
            hotkey_coordinator,
            previous_hotkeys=config.hotkeys,
            before_hotkey_change=controller.request_cancel,
            on_applied=apply_saved_settings,
            on_failed=report_settings_failure,
        )

    signals.save_requested.connect(save_settings)

    test_microphone = _build_microphone_test_handler(
        window=window,
        tray=tray,
        microphone_probe=microphone_probe,
    )
    signals.microphone_test_requested.connect(test_microphone)

    hotkey_service: Any | None = None
    try:
        if is_macos():
            from .macos_hotkeys import HotkeyAction, MacOSHotkeyService

            hotkey_type: Any = MacOSHotkeyService
        else:
            from .windows_hotkeys import HotkeyAction, WindowsHotkeyService

            hotkey_type = WindowsHotkeyService

        def hotkey_callback(
            action: Any,
            still_current: Callable[[], bool] = lambda: True,
        ) -> bool | None:
            # Quartz must decide whether to suppress Esc before its event-tap
            # callback returns. Other actions keep their existing async path.
            if is_macos() and action == HotkeyAction.CANCEL:
                return controller.request_cancel()
            # PASTE_LAST/COPY_LAST must not go through the Qt-thread dispatch
            # below; they share the same serialized input worker as the
            # Qt-signal path so a hotkey and a button click never race.
            if action == HotkeyAction.PASTE_LAST:
                input_worker.submit(controller.paste_last)
                return
            if action == HotkeyAction.COPY_LAST:
                input_worker.submit(controller.copy_last)
                return

            def handle() -> None:
                if not still_current():
                    return
                if action == getattr(HotkeyAction, "HOLD_CANDIDATE", None):
                    controller.prepare_capture()
                elif action == getattr(HotkeyAction, "HOLD_ABANDONED", None):
                    controller.abandon_prepared_capture()
                elif action == HotkeyAction.START:
                    controller.request_start_recording(
                        target=_snapshot_target(
                            strict_editable_check=config.strict_editable_check
                        )
                    )
                elif action == HotkeyAction.STOP:
                    controller.request_stop_recording()
                elif action == HotkeyAction.TOGGLE:
                    start_or_stop(capture_target=True)
                elif action == HotkeyAction.CANCEL:
                    controller.request_cancel()

            dispatch_ui(handle)

        if is_macos():
            hotkey_service = hotkey_type(hotkey_callback)
            hotkey_service.start()
        else:
            hotkey_coordinator = _WindowsHotkeyCoordinator(
                hotkey_type,
                hotkey_callback,
                dispatch_ui,
                config.hotkeys,
            )
            hotkey_coordinator.start()
            # Native shutdown owns the stable coordinator so an in-flight
            # candidate cannot appear after a captured service was stopped.
            hotkey_service = hotkey_coordinator
    except Exception as exc:
        hotkey_runtime_warning = _report_hotkey_start_failure(
            exc,
            macos=is_macos(),
            background=args.background,
            window=window,
            tray=tray,
        )

    shutdown_started = False
    shutdown_handle: tuple[threading.Event, threading.Thread, threading.Thread] | None = None

    def begin_shutdown() -> None:
        nonlocal shutdown_started, shutdown_handle
        if shutdown_started:
            return
        shutdown_started = True
        # Start the hard deadline before touching PortAudio, CUDA or the
        # low-level keyboard hook. All native cleanup happens off the Qt thread.
        shutdown_handle = _start_native_shutdown(
            controller,
            hotkey_service,
            input_worker,
            microphone_probe,
        )

    def quit_application() -> None:
        begin_shutdown()
        window.prepare_to_quit()
        tray.tray.hide()
        app.quit()

    signals.quit_requested.connect(quit_application)
    app.aboutToQuit.connect(begin_shutdown)
    if not args.background:
        window.show()
    if config.resource_mode == "eco":
        status_callback("Готов — экономный режим", "ready")
    elif not controller.warmup_model():
        status_callback("Не удалось запустить подготовку модели", "error")
    if config_load.warning:
        window.update_status("Ошибка config.json — автовставка отключена", "error")
        tray_text, tray_state = _effective_tray_status(
            "Ошибка config.json — автовставка отключена",
            "error",
            hotkey_runtime_warning,
        )
        tray.update_state(tray_text, tray_state)
        tray.notify("Pressay", config_load.warning, warning=True)
    # Record which source tree is actually running: several shortcuts have
    # pointed at stale copies of the project, and a stale copy is otherwise
    # indistinguishable in the log from the current one.
    LOGGER.info(
        "application_started version=%s package=%s",
        __version__,
        Path(__file__).resolve().parent,
    )
    exit_code = int(app.exec())
    # aboutToQuit normally starts cleanup. Keep this fallback for an event-loop
    # return that bypasses the signal, then retain the singleton until cleanup
    # is complete. A stuck participant remains covered by the active watchdog.
    begin_shutdown()
    assert shutdown_handle is not None
    _release_single_instance_after_shutdown(single_instance, shutdown_handle[0])
    LOGGER.info("application_exited code=%d", exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
