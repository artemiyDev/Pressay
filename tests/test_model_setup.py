from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pressay import gigaam as gigaam_module
from pressay import model_setup


def _complete_snapshot(tmp_path: Path) -> Path:
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    for name in model_setup._GIGAAM_MODEL_FILES["gigaam-v3-e2e-rnnt"]:
        (snapshot / name).write_bytes(b"")
    return snapshot


def test_gigaam_model_ready_is_false_for_an_interrupted_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = _complete_snapshot(tmp_path)
    (snapshot / "v3_e2e_rnnt_encoder.onnx").unlink()
    monkeypatch.setattr(gigaam_module, "_cached_model_paths", lambda _name: (snapshot,))

    assert model_setup.gigaam_model_ready("gigaam-v3-e2e-rnnt") is False

def test_gigaam_model_ready_is_true_when_cache_has_a_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = _complete_snapshot(tmp_path)
    monkeypatch.setattr(gigaam_module, "_cached_model_paths", lambda _name: (snapshot,))

    assert model_setup.gigaam_model_ready("gigaam-v3-e2e-rnnt") is True


def test_prepare_gigaam_model_skips_download_when_already_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mutation guard: removing the cache check must make this test fail."""

    snapshot = _complete_snapshot(tmp_path)
    monkeypatch.setattr(gigaam_module, "_cached_model_paths", lambda _name: (snapshot,))

    def _unexpected_download(*_a: object, **_k: object) -> str:
        raise AssertionError("network was reached with a warm cache")

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        type("M", (), {"snapshot_download": staticmethod(_unexpected_download)}),
    )

    ok, message = model_setup.prepare_gigaam_model("gigaam-v3-e2e-rnnt")

    assert ok is True
    assert "already cached" in message


def test_prepare_gigaam_model_downloads_when_cache_is_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gigaam_module, "_cached_model_paths", lambda _name: ())
    calls: list[tuple[str, list[str]]] = []

    def fake_snapshot_download(repo_id: str, *, allow_patterns: list[str]) -> str:
        calls.append((repo_id, allow_patterns))
        return "/fake/snapshot"

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        type("M", (), {"snapshot_download": staticmethod(fake_snapshot_download)}),
    )

    ok, message = model_setup.prepare_gigaam_model("gigaam-v3-e2e-rnnt")

    assert ok is True
    assert "is ready" in message
    assert len(calls) == 1
    repo_id, allow_patterns = calls[0]
    assert repo_id == "istupakov/gigaam-v3-onnx"
    assert "config.json" in allow_patterns
    assert "v3_e2e_rnnt_encoder.onnx" in allow_patterns
    assert "v3_e2e_rnnt_decoder.onnx" in allow_patterns
    assert "v3_e2e_rnnt_joint.onnx" in allow_patterns
    assert "v3_e2e_rnnt_vocab.txt" in allow_patterns


def test_prepare_gigaam_model_failure_does_not_raise_and_has_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gigaam_module, "_cached_model_paths", lambda _name: ())

    def failing_snapshot_download(*_a: object, **_k: object) -> str:
        raise OSError("connection refused")

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        type("M", (), {"snapshot_download": staticmethod(failing_snapshot_download)}),
    )

    ok, message = model_setup.prepare_gigaam_model("gigaam-v3-e2e-rnnt")

    assert ok is False
    assert "connection refused" in message
    assert "Traceback" not in message


def test_main_skip_gigaam_flag_prevents_any_download_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeTranscriber:
        def __init__(self, **_kwargs: object) -> None:
            self.active_device = "cpu"
            self.active_compute_type = "int8"

        def warmup(self) -> None:
            pass

        def transcribe(self, *_a: object, **_k: object) -> None:
            raise model_setup.NoSpeechDetected("silence")

        def close(self) -> None:
            pass

    monkeypatch.setattr(model_setup, "FasterWhisperTranscriber", _FakeTranscriber)
    called: list[str] = []
    monkeypatch.setattr(
        model_setup,
        "prepare_gigaam_model",
        lambda model_name=gigaam_module.DEFAULT_MODEL: called.append(model_name) or (True, "unused"),
    )

    exit_code = model_setup.main(["--model", "tiny", "--skip-gigaam"])

    assert exit_code == 0
    assert called == []


def test_main_exit_code_is_unaffected_by_a_failed_gigaam_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeTranscriber:
        def __init__(self, **_kwargs: object) -> None:
            pass

        active_device = "cpu"
        active_compute_type = "int8"

        def warmup(self) -> None:
            pass

        def transcribe(self, *_a: object, **_k: object) -> None:
            raise model_setup.NoSpeechDetected("silence")

        def close(self) -> None:
            pass

    monkeypatch.setattr(model_setup, "FasterWhisperTranscriber", _FakeTranscriber)
    monkeypatch.setattr(
        model_setup,
        "prepare_gigaam_model",
        lambda model_name=gigaam_module.DEFAULT_MODEL: (False, "Could not download GigaAM model"),
    )

    exit_code = model_setup.main(["--model", "tiny"])

    assert exit_code == 0
