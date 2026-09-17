"""GigaAM v3 end-to-end transcription for Russian dictation.

Why this exists next to :mod:`pressay.transcriber`: measured on this machine's
own recordings, GigaAM v3 e2e RNN-T matches or beats Whisper large-v3 on plain
Russian while running two to four times faster *on the CPU* than Whisper does on
the GPU, and it emits punctuation and capitalisation natively.  It is a
Russian-only model, so English speech and translation stay with Whisper; the
controller owns that routing.

The engine deliberately mirrors :class:`~pressay.transcriber.FasterWhisperTranscriber`'s
public surface -- ``load``/``transcribe``/``close`` returning a
:class:`~pressay.transcriber.TranscriptionResult` -- so the controller can hold
either one behind the same call.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from .audio import (
    DEFAULT_TARGET_PEAK,
    TARGET_SAMPLE_RATE,
    audio_rms,
    normalize_peak,
    resample_audio,
)
from .transcriber import (
    HallucinationDetected,
    ModelLoadError,
    NoSpeechDetected,
    TranscriptionError,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionTimings,
    is_probable_hallucination,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_MODEL = "gigaam-v3-e2e-rnnt"

#: Variants that emit punctuation and normalised numbers on their own.  The
#: plain ``ctc``/``rnnt`` heads return lowercase text with no punctuation, which
#: would be a visible regression for dictation, so they are not offered here.
SUPPORTED_MODELS = frozenset({"gigaam-v3-e2e-rnnt", "gigaam-v3-e2e-ctc"})

#: GigaAM's encoder is trained on windows of roughly half a minute.  Anything
#: longer has to be segmented, which needs a VAD we deliberately do not pull in;
#: the controller falls back to Whisper instead.
MAX_AUDIO_SECONDS = 25.0

ModelFactory = Callable[..., Any]


class EngineUnavailable(TranscriptionError):
    """The GigaAM engine cannot handle this request; the caller should fall back."""


def _default_model_factory(model_name: str, *, local_files_only: bool = True, **kwargs: Any) -> Any:
    import onnx_asr  # Imported lazily: a missing extra must not break Whisper.

    # A dictation app must never reach the network mid-sentence: onnx_asr would
    # otherwise fall through to Hugging Face when the cache is cold, stalling the
    # take behind a download.  The model is fetched at install/warmup time.
    if local_files_only:
        for candidate in _cached_model_paths(model_name):
            if candidate.is_dir():
                # Both arguments are required: given only a directory, onnx_asr
                # cannot tell which architecture it holds and raises
                # ModelNotSupportedError.
                return onnx_asr.load_model(model_name, str(candidate), **kwargs)
        raise ModelLoadError(
            f"{model_name} is not in the local cache and downloads are disabled"
        )
    return onnx_asr.load_model(model_name, **kwargs)


def _cached_model_paths(model_name: str) -> tuple[Path, ...]:
    """Where a previously downloaded onnx-asr model can be found offline."""

    repo = _HF_REPOS.get(model_name)
    if repo is None:
        return ()
    roots = []
    env_home = os.environ.get("HF_HOME")
    if env_home:
        roots.append(Path(env_home) / "hub")
    env_cache = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if env_cache:
        roots.append(Path(env_cache))
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")

    found: list[Path] = []
    for root in roots:
        snapshots = root / f"models--{repo.replace('/', '--')}" / "snapshots"
        if not snapshots.is_dir():
            continue
        found.extend(sorted(snapshots.iterdir(), key=lambda p: p.name))
    return tuple(found)


#: onnx-asr resolves these names to Hugging Face repositories; the offline
#: loader needs the mapping to find an already-downloaded copy.
_HF_REPOS = {
    "gigaam-v3-e2e-rnnt": "istupakov/gigaam-v3-onnx",
    "gigaam-v3-e2e-ctc": "istupakov/gigaam-v3-onnx",
}


class GigaAmTranscriber:
    """Russian-only transcription through GigaAM v3 e2e via onnxruntime."""

    SUPPORTED_LANGUAGES = frozenset({"auto", "ru"})

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        providers: tuple[str, ...] | None = None,
        min_audio_seconds: float = 0.10,
        silence_rms_threshold: float = 0.00001,
        target_peak: float = DEFAULT_TARGET_PEAK,
        max_audio_seconds: float = MAX_AUDIO_SECONDS,
        local_files_only: bool = True,
        model_factory: ModelFactory | None = None,
    ) -> None:
        if model_name not in SUPPORTED_MODELS:
            raise ValueError(
                "model_name must be one of: " + ", ".join(sorted(SUPPORTED_MODELS))
            )
        if min_audio_seconds < 0 or silence_rms_threshold < 0:
            raise ValueError("audio thresholds cannot be negative")
        if max_audio_seconds <= 0:
            raise ValueError("max_audio_seconds must be positive")

        self.model_name = model_name
        # CUDA is not offered: onnxruntime's CUDA provider needs cudart/cufft/
        # curand, while this install ships only the cuBLAS and cuDNN that
        # CTranslate2 requires.  Measured CPU latency already beats Whisper on
        # the GPU, so the missing provider costs nothing here.
        self.providers = tuple(providers) if providers else ("CPUExecutionProvider",)
        self.min_audio_seconds = float(min_audio_seconds)
        self.silence_rms_threshold = float(silence_rms_threshold)
        self.target_peak = float(target_peak)
        self.max_audio_seconds = float(max_audio_seconds)
        self.local_files_only = bool(local_files_only)
        self._model_factory = model_factory or _default_model_factory
        self._model: Any | None = None
        self._lock = threading.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def active_device(self) -> str | None:
        if self._model is None:
            return None
        return "cuda" if "CUDAExecutionProvider" in self.providers else "cpu"

    @property
    def active_compute_type(self) -> str | None:
        return "float32" if self._model is not None else None

    def load(self) -> Any:
        with self._lock:
            if self._model is not None:
                return self._model
            started = time.perf_counter()
            try:
                model = self._model_factory(
                    self.model_name,
                    providers=list(self.providers),
                    local_files_only=self.local_files_only,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as ModelLoadError
                raise ModelLoadError(
                    f"Could not load {self.model_name}: {exc}"
                ) from exc
            self._model = model
            LOGGER.info(
                "gigaam_model_loaded model=%s providers=%s load_seconds=%.3f",
                self.model_name,
                ",".join(self.providers),
                time.perf_counter() - started,
            )
            return model

    def warmup(self) -> str:
        """Run one throwaway inference so the first dictation is not the slowest."""

        self.load()
        silence = np.zeros(int(0.2 * TARGET_SAMPLE_RATE), dtype=np.float32)
        try:
            self._recognize(silence)
        except Exception:  # noqa: BLE001 - warmup must never break startup
            LOGGER.debug("gigaam_warmup_failed", exc_info=True)
        return self.model_name

    def _recognize(self, samples: np.ndarray) -> str:
        model = self._model
        if model is None:
            raise ModelLoadError("Model is not loaded")
        text = model.recognize(samples, sample_rate=TARGET_SAMPLE_RATE)
        if isinstance(text, (list, tuple)):
            text = " ".join(str(part) for part in text)
        return str(text or "").strip()

    def transcribe(
        self,
        audio: Any,
        *,
        sample_rate: int = TARGET_SAMPLE_RATE,
        language: str = "auto",
        task: str = "transcribe",
        **_ignored: Any,
    ) -> TranscriptionResult:
        """Transcribe one Russian waveform or reject it as silence.

        ``**_ignored`` swallows the VAD and prompt keywords the Whisper engine
        accepts, so the controller can call either engine with one signature.
        """

        total_started = time.perf_counter()
        language = language.casefold().strip()
        if language not in self.SUPPORTED_LANGUAGES:
            raise EngineUnavailable(f"GigaAM handles Russian only, not {language!r}")
        if task.casefold().strip() != "transcribe":
            raise EngineUnavailable("GigaAM cannot translate")
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")

        samples = np.asarray(audio, dtype=np.float32)
        if samples.ndim == 2:
            samples = samples.mean(axis=1, dtype=np.float32)
        if samples.ndim != 1:
            raise ValueError("audio must be a mono or frames-by-channels array")
        samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
        np.clip(samples, -1.0, 1.0, out=samples)

        duration = samples.size / sample_rate
        if duration < self.min_audio_seconds:
            raise NoSpeechDetected(
                f"Audio is {duration:.2f}s; minimum is {self.min_audio_seconds:.2f}s"
            )
        if audio_rms(samples) < self.silence_rms_threshold:
            raise NoSpeechDetected("Audio is silent")
        if duration > self.max_audio_seconds:
            raise EngineUnavailable(
                f"Audio is {duration:.1f}s; GigaAM handles up to "
                f"{self.max_audio_seconds:.0f}s"
            )

        if sample_rate != TARGET_SAMPLE_RATE:
            samples = resample_audio(samples, sample_rate, TARGET_SAMPLE_RATE)

        samples, gain = normalize_peak(samples, self.target_peak)

        load_started = time.perf_counter()
        self.load()
        load_seconds = time.perf_counter() - load_started

        inference_started = time.perf_counter()
        text = self._recognize(samples)
        inference_seconds = time.perf_counter() - inference_started

        if not text:
            raise NoSpeechDetected("GigaAM returned an empty transcript")
        if is_probable_hallucination(text):
            raise HallucinationDetected("Model output looks like a silence hallucination")

        LOGGER.debug(
            "gigaam_transcribed seconds=%.2f gain=%.2f inference=%.3f chars=%d",
            duration,
            gain,
            inference_seconds,
            len(text),
        )
        return TranscriptionResult(
            text=text,
            language="ru",
            language_probability=None,
            segments=(TranscriptionSegment(start=0.0, end=duration, text=text),),
            audio_duration_seconds=duration,
            timings=TranscriptionTimings(
                model_load_seconds=load_seconds,
                inference_seconds=inference_seconds,
                total_seconds=time.perf_counter() - total_started,
            ),
            device=str(self.active_device),
            compute_type=str(self.active_compute_type),
            language_choice="forced",
        )

    def close(self) -> None:
        with self._lock:
            self._model = None
