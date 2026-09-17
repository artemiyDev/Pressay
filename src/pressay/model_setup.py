"""Explicit model download and warm-up command used during setup."""

from __future__ import annotations

import argparse
import time

import numpy as np

from . import gigaam as gigaam_module
from .transcriber import FasterWhisperTranscriber, NoSpeechDetected

#: Files needed to run ``gigaam-v3-e2e-rnnt`` fully offline (see
#: ``GigaAmTranscriber``/``_default_model_factory`` in gigaam.py). The upstream
#: repository also carries the v2, v3 (non-e2e) and multilingual heads; listing
#: only the e2e RNN-T files here keeps the download to this one model instead
#: of the whole repository.
_GIGAAM_MODEL_FILES: dict[str, tuple[str, ...]] = {
    "gigaam-v3-e2e-rnnt": (
        "v3_e2e_rnnt_encoder.onnx",
        "v3_e2e_rnnt_decoder.onnx",
        "v3_e2e_rnnt_joint.onnx",
        "v3_e2e_rnnt_vocab.txt",
    ),
}


def gigaam_model_ready(model_name: str = gigaam_module.DEFAULT_MODEL) -> bool:
    """True if ``model_name`` is already in the local Hugging Face cache."""

    # A snapshot directory alone is not enough: an interrupted download leaves
    # one behind, and treating it as ready would skip the retry forever.
    files = _GIGAAM_MODEL_FILES.get(model_name, ())
    return any(
        path.is_dir() and all((path / name).exists() for name in files)
        for path in gigaam_module._cached_model_paths(model_name)
    )


def prepare_gigaam_model(model_name: str = gigaam_module.DEFAULT_MODEL) -> tuple[bool, str]:
    """Download GigaAM into the local Hugging Face cache unless it is already there.

    Never raises: a failed GigaAM download must not fail the rest of setup
    (Whisper keeps working), so callers should print the returned message and
    move on regardless of the returned ``ok`` flag.
    """

    if gigaam_model_ready(model_name):
        return True, f"GigaAM model {model_name!r} is already cached."

    repo = gigaam_module._HF_REPOS.get(model_name)
    files = _GIGAAM_MODEL_FILES.get(model_name)
    if repo is None or files is None:
        return False, f"GigaAM model {model_name!r} has no known download recipe."

    allow_patterns = [
        "config.json",
        *files,
        # onnx-asr may store large encoders as external ONNX data next to the
        # graph file, named either "<name>.onnx_data" or "<name>.onnx.data";
        # the "?" glob wildcard matches either separator.
        *(f"{name}?data" for name in files if name.endswith(".onnx")),
    ]

    try:
        from huggingface_hub import snapshot_download  # noqa: PLC0415 - optional extra
    except Exception as exc:  # pragma: no cover - onnx-asr[hub] always brings this in
        return False, f"huggingface_hub is not available: {type(exc).__name__}: {exc}"

    try:
        snapshot_download(repo, allow_patterns=allow_patterns)
    except Exception as exc:  # noqa: BLE001 - a network/HF failure must not raise
        return False, f"Could not download GigaAM model {model_name!r}: {type(exc).__name__}: {exc}"
    return True, f"GigaAM model {model_name!r} is ready."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download and validate a local Whisper model")
    parser.add_argument("--model", default="turbo")
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument(
        "--gigaam-model",
        default=gigaam_module.DEFAULT_MODEL,
        help="GigaAM model to prepare for Russian dictation",
    )
    parser.add_argument(
        "--skip-gigaam",
        action="store_true",
        help="Do not prepare the GigaAM model used for Russian dictation",
    )
    args = parser.parse_args(argv)

    print(f"Preparing model {args.model!r}. The first download can take several minutes...")
    started = time.perf_counter()
    transcriber = FasterWhisperTranscriber(
        model_size=args.model,
        device=args.device,
        local_files_only=False,
    )
    try:
        transcriber.warmup()
        # Model construction alone does not load every CUDA DLL. A quiet test
        # waveform forces one real inference pass and validates CPU fallback.
        sample_rate = 16_000
        timeline = np.arange(sample_rate, dtype=np.float32) / sample_rate
        probe = (0.002 * np.sin(2 * np.pi * 220 * timeline)).astype(np.float32)
        try:
            transcriber.transcribe(probe, sample_rate=sample_rate, language="en")
        except NoSpeechDetected:
            pass
        device = str(transcriber.active_device)
        compute_type = str(transcriber.active_compute_type)
    finally:
        transcriber.close()
    elapsed = time.perf_counter() - started
    print(f"Model ready: device={device}, compute_type={compute_type}, elapsed={elapsed:.1f}s")

    if not args.skip_gigaam:
        print(f"Preparing GigaAM model {args.gigaam_model!r} for Russian dictation...")
        ok, message = prepare_gigaam_model(args.gigaam_model)
        print(message)
        if not ok:
            print("Continuing with Whisper only; russian_engine=\"gigaam\" will fall back until this is retried.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
