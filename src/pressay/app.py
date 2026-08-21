"""Qt/Win32 entry point for the desktop application."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
from dataclasses import dataclass
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from . import hotkey_bindings
from . import __version__
from .audio import AudioCaptureError, AudioRecorder, normalize_device_selector
from .config import AppConfig, ConfigError
from .controller import DictationController
from .ui import MicrophoneChoice, SettingsWindow, StatusOverlay, TrayController, UiSignals
from .platform_support import input_adapter, is_macos, is_windows, user_data_directory


LOGGER = logging.getLogger(__name__)


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
    cleanly on one daemon-style worker instead of spawning a new thread per
    request that then blocks on a lock.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="pressay-input"
        )

    def submit(self, action: Callable[[], Any]) -> None:
        self._executor.submit(self._run, action)

    @staticmethod
    def _run(action: Callable[[], Any]) -> None:
        try:
            action()
        except Exception:
            # A failed paste/copy transaction must not strand the single
            # serializing worker; the next queued request still has to run.
            LOGGER.exception("input_action_failed")

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)


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
    *,
    timeout_seconds: float = 3.0,
    hard_exit: Any = os._exit,
) -> tuple[threading.Event, threading.Thread, threading.Thread]:
    """Stop native services off the GUI thread under one hard deadline.

    Controller/PortAudio cleanup and the low-level keyboard hook are independent
    daemon workers, so one stuck native call cannot prevent the other cleanup
    from starting. The watchdog is started first and also covers a synchronous
    hang inside ``controller.close()`` itself.
    """

    shutdown_complete = threading.Event()
    watchdog = _start_shutdown_watchdog(
        shutdown_complete,
        timeout_seconds=timeout_seconds,
        hard_exit=hard_exit,
    )
    controller_call_done = threading.Event()
    hotkey_done = threading.Event()

    def close_controller() -> None:
        try:
            controller.close()
        except Exception:
            LOGGER.exception("controller_close_failed")
        finally:
            controller_call_done.set()

    def stop_hotkeys() -> None:
        try:
            if hotkey_service is not None:
                hotkey_service.stop()
        except Exception:
            LOGGER.exception("hotkey_stop_failed")
        finally:
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


def _microphones() -> list[MicrophoneChoice]:
    result = [MicrophoneChoice(None, "Системный микрофон по умолчанию")]
    try:
        devices = AudioRecorder.list_input_devices()
    except AudioCaptureError:
        return result
    seen: set[str] = set()
    for device in sorted(devices, key=lambda item: (not item.is_default, item.index)):
        selector = device.stable_selector
        if selector in seen:
            continue
        seen.add(selector)
        suffix = " — по умолчанию" if device.is_default else ""
        host_api = f", {device.host_api}" if device.host_api else ""
        result.append(
            MicrophoneChoice(
                selector,
                f"{device.name} ({device.default_sample_rate} Hz{host_api}){suffix}",
                legacy_index=device.index,
                device_name=device.name,
                is_default=device.is_default,
            )
        )
    return result


def _settings_dict(config: AppConfig) -> dict[str, Any]:
    microphone: str | int | None = config.microphone
    if isinstance(microphone, str) and microphone.isdecimal():
        microphone = int(microphone)
    return {
        "microphone": microphone,
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
    status_callback: Callable[[str, str], None],
    notification_callback: Callable[[str, str, bool], None],
    recorder_factory: Callable[..., AudioRecorder] = AudioRecorder,
    thread_factory: Callable[..., threading.Thread] = threading.Thread,
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
        window.update_status("Проверяю микрофон…", "processing")
        device = _test_microphone_device(values.get("microphone"))

        def work() -> None:
            try:
                recorder = recorder_factory(device=device)
                rate = recorder.warmup(0.10)
            except Exception as exc:
                status_callback("Микрофон недоступен", "error")
                notification_callback("Pressay", str(exc), True)
            else:
                status_callback(f"Микрофон готов: {int(rate)} Hz", "success")

        thread_factory(target=work, name="pressay-mic-test", daemon=True).start()

    return test_microphone


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
            }
        )
        LOGGER.info(
            "recording_target_captured valid=%s editable=%s hwnd=%s pid=%s "
            "focus_kind=%s control_type=%s enabled=%s focusable=%s "
            "value_writable=%s text_editable=%s caret=%s win32_caret=%s",
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
    app.aboutToQuit.connect(single_instance.close)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        LOGGER.error("system_tray_unavailable")

    config_load = _load_config()
    config = config_load.config
    signals = UiSignals()
    window = SettingsWindow(signals, _settings_dict(config), _microphones())
    tray = TrayController(signals, window)
    app.aboutToQuit.connect(window.prepare_to_quit)

    dispatcher = _MainThreadDispatcher()

    def dispatch_ui(callback: Any, *args: Any, **kwargs: Any) -> None:
        dispatcher.requested.emit(callback, args, kwargs)

    def status_callback(text: str, state: str) -> None:
        def update() -> None:
            window.update_status(text, state)
            tray.update_state(text, state)
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

    # Filled in once the hotkey service exists; empty when it failed to start.
    hotkey_restart: dict[str, Any] = {}

    def save_settings(values: dict[str, Any]) -> None:
        nonlocal config
        previous_resource_mode = config.resource_mode
        previous_hotkeys = config.hotkeys
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
            microphone=None if microphone is None else str(microphone),
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
        try:
            updated.save()
        except ConfigError as exc:
            # Keep the previous config in memory: a failed write must not leave
            # the running app on settings that were never persisted.
            tray.notify("Pressay", str(exc), warning=True)
            return
        config = updated
        controller.update_config(config)
        if previous_resource_mode == "eco" and config.resource_mode != "eco":
            controller.warmup_model()
        restart_hotkeys = hotkey_restart.get("restart")
        if hotkeys != previous_hotkeys and restart_hotkeys is not None:
            restart_hotkeys(hotkeys)
        window.update_status("Настройки сохранены", "success")

    signals.save_requested.connect(save_settings)

    test_microphone = _build_microphone_test_handler(
        window=window,
        tray=tray,
        status_callback=status_callback,
        notification_callback=notification_callback,
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

        def hotkey_callback(action: Any) -> bool | None:
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

        # macOS chords are still fixed, so only the Windows service is
        # configurable; passing bindings it does not accept would break it.
        hotkey_kwargs = {} if is_macos() else {"bindings": config.hotkeys}
        hotkey_service = hotkey_type(hotkey_callback, **hotkey_kwargs)
        hotkey_service.start()

        if not is_macos():

            def restart_hotkeys(bindings: Any) -> None:
                """Swap the keyboard hook for one bound to the new chords.

                Runs off the Qt thread: stopping the service joins the hook,
                dispatcher and watchdog threads, which must not freeze the
                settings window.
                """

                def swap() -> None:
                    nonlocal hotkey_service
                    try:
                        if hotkey_service is not None:
                            hotkey_service.stop()
                        service = hotkey_type(hotkey_callback, bindings=bindings)
                        service.start()
                        hotkey_service = service
                    except Exception as exc:
                        LOGGER.exception("hotkey_restart_failed")
                        notification_callback(
                            "Pressay",
                            f"Не удалось применить новые клавиши: {exc}. "
                            "Перезапустите Pressay.",
                            True,
                        )

                threading.Thread(
                    target=swap, name="pressay-hotkey-swap", daemon=True
                ).start()

            hotkey_restart["restart"] = restart_hotkeys
    except Exception as exc:
        LOGGER.exception("hotkey_service_failed")
        tray.notify("Pressay", f"Глобальные клавиши недоступны: {exc}", warning=True)

    shutdown_started = False
    shutdown_handle: tuple[threading.Event, threading.Thread, threading.Thread] | None = None

    def begin_shutdown() -> None:
        nonlocal shutdown_started, shutdown_handle
        if shutdown_started:
            return
        shutdown_started = True
        # Start the hard deadline before touching PortAudio, CUDA or the
        # low-level keyboard hook. All native cleanup happens off the Qt thread.
        shutdown_handle = _start_native_shutdown(controller, hotkey_service)
        # Never block application exit on a queued/in-flight paste or copy.
        input_worker.shutdown()

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
        tray.update_state("Ошибка config.json — автовставка отключена", "error")
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
    LOGGER.info("application_exited code=%d", exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
