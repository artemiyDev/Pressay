from __future__ import annotations

from types import SimpleNamespace
import threading

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from pressay.app import _settings_dict
from pressay.config import AppConfig
from pressay.controller import DictationController
from pressay.transcriber import FasterWhisperTranscriber
from pressay.ui import DARK_THEME, MicrophoneChoice, SettingsWindow, UiSignals


class _SilentModel:
    def transcribe(self, *_args, **_kwargs):
        return iter(()), SimpleNamespace(language="ru", language_probability=0.99)


def test_missing_model_download_reports_progress_through_substituted_loader() -> None:
    downloader_calls: list[dict[str, object]] = []
    progress: list[int] = []

    def downloader(_size: str, **kwargs: object) -> str:
        downloader_calls.append(dict(kwargs))
        if kwargs["local_files_only"]:
            raise RuntimeError("not cached")
        progress_bar = kwargs["tqdm_class"](total=100)
        progress_bar.update(34)
        progress_bar.update(66)
        progress_bar.close()
        return "cached-path"

    transcriber = FasterWhisperTranscriber(
        "large-v3",
        device="cpu",
        local_files_only=False,
        model_factory=lambda *_args, **_kwargs: _SilentModel(),
        model_downloader=downloader,
    )
    transcriber.set_download_progress_callback(progress.append)

    assert transcriber.warmup() == ("cpu", "int8")
    assert [call["local_files_only"] for call in downloader_calls] == [True, False]
    assert progress == [0, 34, 100]


def test_cached_model_skips_download_progress_with_substituted_loader() -> None:
    downloader_calls: list[dict[str, object]] = []
    progress: list[int] = []

    def downloader(_size: str, **kwargs: object) -> str:
        downloader_calls.append(dict(kwargs))
        return "cached-path"

    transcriber = FasterWhisperTranscriber(
        "turbo",
        device="cpu",
        local_files_only=False,
        model_factory=lambda *_args, **_kwargs: _SilentModel(),
        model_downloader=downloader,
    )
    transcriber.set_download_progress_callback(progress.append)

    assert transcriber.warmup() == ("cpu", "int8")
    assert [call["local_files_only"] for call in downloader_calls] == [True]
    assert progress == []


def test_model_itself_always_loads_offline_even_in_network_mode() -> None:
    factory_kwargs: list[dict[str, object]] = []

    def factory(_size: str, **kwargs: object) -> _SilentModel:
        factory_kwargs.append(dict(kwargs))
        return _SilentModel()

    transcriber = FasterWhisperTranscriber(
        "turbo",
        device="cpu",
        local_files_only=False,
        model_factory=factory,
        model_downloader=lambda _size, **_kwargs: "cached-path",
    )

    assert transcriber.warmup() == ("cpu", "int8")
    # Networking is confined to the absent-model download path; a cold load of
    # a cached model must never re-resolve revisions online.
    assert [call["local_files_only"] for call in factory_kwargs] == [True]


class _BlockingProgressTranscriber:
    def __init__(self, model_size: str) -> None:
        self.model_size = model_size
        self.callback = None
        self.started = threading.Event()
        self.release = threading.Event()

    def set_download_progress_callback(self, callback) -> None:
        self.callback = callback

    def warmup(self) -> tuple[str, str]:
        assert self.callback is not None
        self.callback(0)
        self.started.set()
        assert self.release.wait(timeout=2)
        self.callback(75)
        return "cpu", "int8"

    def close(self) -> None:
        pass


class _ReadyTranscriber:
    def __init__(self, model_size: str) -> None:
        self.model_size = model_size

    def set_download_progress_callback(self, callback) -> None:
        callback(0)

    def warmup(self) -> tuple[str, str]:
        return "cpu", "int8"

    def close(self) -> None:
        pass


def test_stale_download_progress_and_model_ready_callback_are_suppressed() -> None:
    statuses: list[tuple[str, str]] = []
    ready: list[tuple[str, str, str]] = []
    controller = DictationController(
        AppConfig(model="small"),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=lambda _text: None,
        notification_callback=lambda *_args: None,
        model_ready_callback=lambda *details: ready.append(details),
    )
    stale = _BlockingProgressTranscriber("small")
    controller._new_transcriber = lambda model: (  # type: ignore[method-assign]
        stale if model == "small" else _ReadyTranscriber(model)
    )

    assert controller.warmup_model() is True
    assert stale.started.wait(timeout=2)
    first_warmup = controller._warmup_future
    controller.update_config(AppConfig(model="medium"))
    stale.release.set()
    assert first_warmup is not None
    first_warmup.result(timeout=2)
    assert controller._warmup_future is not None
    controller._warmup_future.result(timeout=2)

    assert ("Готовлю модель small…", "processing") in statuses
    assert not any("модель small — 75%" in text for text, _state in statuses)
    assert ready == [("medium", "cpu", "int8")]
    controller.close()
    assert controller.wait_closed(2)


def test_download_status_updates_are_limited_to_one_per_second(monkeypatch) -> None:
    statuses: list[tuple[str, str]] = []
    controller = DictationController(
        AppConfig(model="large-v3"),
        status_callback=lambda text, state: statuses.append((text, state)),
        result_callback=lambda _text: None,
        notification_callback=lambda *_args: None,
    )
    timestamps = iter((0.0, 0.2, 1.0))
    monkeypatch.setattr("pressay.controller.time.monotonic", lambda: next(timestamps))
    with controller._lock:
        controller._preload_enabled = True
        controller._model_generation = 7
    report = controller._warmup_progress_callback("large-v3", 7)

    report(10)
    report(34)

    assert statuses == [("Скачиваю модель large-v3 — 34%…", "processing")]
    controller.close()
    assert controller.wait_closed(2)


def test_settings_window_shows_active_model_and_preserves_model_values(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = SettingsWindow(
        UiSignals(),
        _settings_dict(AppConfig()),
        [MicrophoneChoice(None, "Системный микрофон")],
    )
    try:
        assert window.active_model_label.text() == "Модель ещё не загружалась"
        assert [window.model_combo.itemData(index) for index in range(4)] == [
            "small",
            "medium",
            "turbo",
            "large-v3",
        ]
        assert "~0.5 ГБ" in window.model_combo.itemText(0)
        assert "~1.5 ГБ" in window.model_combo.itemText(1)
        assert "~1.5 ГБ" in window.model_combo.itemText(2)
        assert "~3 ГБ" in window.model_combo.itemText(3)

        window.update_active_model("turbo", "cuda", "int8_float16")
        window._apply_theme(Qt.ColorScheme.Dark)

        assert window.active_model_label.text() == "Активна: turbo · CUDA · int8_float16"
        assert window.active_model_label in window._hint_labels
        assert DARK_THEME["subtitle_text"] in window.active_model_label.styleSheet()
    finally:
        window.prepare_to_quit()
        window.close()
        app.processEvents()
