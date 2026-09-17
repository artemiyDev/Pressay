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


class _BlockedOnnxAsr:
    """Stand-in for the ``onnx_asr`` module installed in the "with GigaAM" venv.

    Its ``load_model`` always raises, so ``GigaAmTranscriber.load()`` wraps it
    into a ``ModelLoadError`` with a clear message.  The controller already
    treats ``ModelLoadError`` as "fall back to Whisper", so tests that do not
    care about GigaAM get a deterministic Whisper-only path for free; tests
    that do care must stub the engine explicitly (``model_factory=...`` or
    ``controller._gigaam = ...``), which bypasses this module entirely.
    """

    @staticmethod
    def load_model(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "Test reached the real onnx_asr.load_model(). Stub GigaAM "
            "explicitly instead (GigaAmTranscriber(model_factory=...) or "
            "controller._gigaam = <fake>)."
        )


@pytest.fixture(autouse=True)
def _block_real_gigaam_model_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent any test from loading a real ONNX GigaAM model."""

    monkeypatch.setitem(sys.modules, "onnx_asr", _BlockedOnnxAsr)
