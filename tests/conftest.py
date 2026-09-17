"""Shared test fixtures.

The dictation pipeline must never touch a real ONNX GigaAM model from a test:
that means a multi-second load off disk (best case) or a network stall/
timeout (worst case, cold Hugging Face cache) instead of a millisecond-scale
unit test.  ``AppConfig`` defaults ``russian_engine`` to ``"gigaam"``, so any
controller test that sets ``language="ru"`` without stubbing GigaAM would
otherwise reach the real engine.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest


_REAL_LOAD_ATTEMPTS: list[str] = []


class _BlockedOnnxAsr:
    """Stand-in for the ``onnx_asr`` module installed in the "with GigaAM" venv.

    ``GigaAmTranscriber.load()`` wraps whatever ``load_model`` raises into a
    ``ModelLoadError``, and the controller answers that with a quiet fallback
    to Whisper.  Raising here is therefore not enough to fail a test, so every
    attempt is also recorded and the fixture below fails the test at teardown.
    Tests that need GigaAM stub the engine (``model_factory=...`` or
    ``controller._gigaam = ...``); tests that do not must pin
    ``russian_engine="whisper"``.
    """

    @staticmethod
    def load_model(model: Any = None, *_args: Any, **_kwargs: Any) -> Any:
        _REAL_LOAD_ATTEMPTS.append(str(model))
        raise AssertionError("Test reached the real onnx_asr.load_model().")


@pytest.fixture(autouse=True)
def _block_real_gigaam_model_load(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Fail any test that tries to load a real ONNX GigaAM model."""

    _REAL_LOAD_ATTEMPTS.clear()
    monkeypatch.setitem(sys.modules, "onnx_asr", _BlockedOnnxAsr)
    yield
    attempts = list(_REAL_LOAD_ATTEMPTS)
    _REAL_LOAD_ATTEMPTS.clear()
    assert not attempts, (
        f"Test reached the real onnx_asr.load_model() for {attempts}. Stub GigaAM "
        "(GigaAmTranscriber(model_factory=...) or controller._gigaam = <fake>) or "
        'pin russian_engine="whisper" in the test config.'
    )
