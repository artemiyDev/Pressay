"""Races between settings changes and GigaAM loading (review of 0.6.9)."""

import threading
from dataclasses import replace

from pressay.config import AppConfig
from pressay.controller import DictationController


class _Whisper:
    model_size = "small"

    def warmup(self):
        return "cpu", "int8"

    def close(self):
        pass


class _Giga:
    def __init__(self, name, started=None, release=None):
        self.model_name = name
        self.active_device = "cpu"
        self.active_compute_type = "float32"
        self.closed = False
        self._started = started
        self._release = release

    def warmup(self):
        if self._started is not None:
            self._started.set()
            assert self._release.wait(timeout=2)

    def close(self):
        self.closed = True


def _make(config, calls):
    return DictationController(
        config,
        status_callback=lambda *_a: None,
        result_callback=lambda *_a: None,
        notification_callback=lambda *_a: None,
        model_ready_callback=lambda *a: calls.append(a),
    )


def test_switch_to_whisper_during_gigaam_part_of_warmup(monkeypatch):
    """Switching to Whisper while GigaAM warms up keeps the Whisper label."""

    config = AppConfig(language="ru", russian_engine="gigaam", model="small")
    calls = []
    c = _make(config, calls)
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(c, "_new_transcriber", lambda _m: _Whisper())

    def ensure(name):
        c._gigaam = _Giga(name, started, release)
        return c._gigaam

    monkeypatch.setattr(c, "_ensure_gigaam", ensure)
    try:
        c.warmup_model()
        assert started.wait(2)
        c.update_config(replace(config, russian_engine="whisper"))
        release.set()
        c._warmup_future.result(timeout=2)
        c._executor.submit(lambda: None).result(timeout=2)
    finally:
        c.close()
    assert calls[-1][0] == "small", calls


def test_switch_engine_while_translating(monkeypatch):
    """An engine switch during voice translation still retires GigaAM."""

    config = AppConfig(language="ru", russian_engine="gigaam", model="small", voice_translate=True)
    calls = []
    c = _make(config, calls)
    giga = _Giga(config.gigaam_model)
    c._gigaam = giga
    c._active_label_is_gigaam = True
    c._last_whisper_ready = ("small", "cpu", "int8")
    c.translating = True
    try:
        c.update_config(replace(config, russian_engine="whisper"))
        c._executor.submit(lambda: None).result(timeout=2)
        with c._lock:
            c.translating = False  # voice command "translation off"
    finally:
        pass
    resident = c._gigaam is not None
    c.close()
    assert calls and calls[-1][0] == "small", calls
    assert not resident

