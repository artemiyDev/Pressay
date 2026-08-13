"""Explicit model download and warm-up command used during setup."""

from __future__ import annotations

import argparse
import time

import numpy as np

from .transcriber import FasterWhisperTranscriber, NoSpeechDetected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download and validate a local Whisper model")
    parser.add_argument("--model", default="turbo")
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
