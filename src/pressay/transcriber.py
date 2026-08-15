"""Persistent faster-whisper adapter with safe platform-aware CPU fallback."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import site
import sys
import threading
import time
from typing import Any, Callable, Mapping

import numpy as np

from .audio import TARGET_SAMPLE_RATE, audio_rms, resample_audio


class TranscriptionError(RuntimeError):
    """Base error shown to callers of the transcription service."""


class ModelLoadError(TranscriptionError):
    """No configured faster-whisper backend could be initialized."""


class NoSpeechDetected(TranscriptionError):
    """No trustworthy spoken text was found in the supplied audio."""


class HallucinationDetected(NoSpeechDetected):
    """Whisper produced a known non-speech/hallucination phrase."""


@dataclass(frozen=True, slots=True)
class TranscriptionSegment:
    start: float
    end: float
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionTimings:
    model_load_seconds: float
    inference_seconds: float
    total_seconds: float


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    language: str | None
    language_probability: float | None
    segments: tuple[TranscriptionSegment, ...]
    audio_duration_seconds: float
    timings: TranscriptionTimings
    device: str
    compute_type: str

    @property
    def transcription_seconds(self) -> float:
        return self.timings.inference_seconds


@dataclass(frozen=True, slots=True)
class _TranscriptionCandidate:
    text: str
    language: str
    language_probability: float | None
    segments: tuple[TranscriptionSegment, ...]
    score: float


ModelFactory = Callable[..., Any]
ModelDownloader = Callable[..., str]
DownloadProgressCallback = Callable[[int], None]


_DLL_DIRECTORY_HANDLES: list[Any] = []
_NVIDIA_DLL_HANDLES: list[Any] = []
_LOADED_NVIDIA_DLL_PATHS: set[str] = set()


def _prepare_windows_nvidia_dlls() -> None:
    """Make pip-installed CUDA runtime DLLs discoverable by CTranslate2."""

    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return
    roots = [Path(item) for item in site.getsitepackages()]
    roots.append(Path(__file__).resolve().parents[2])
    known = {str(getattr(handle, "path", "")) for handle in _DLL_DIRECTORY_HANDLES}
    for root in dict.fromkeys(roots):
        for relative in (
            Path("nvidia") / "cublas" / "bin",
            Path("nvidia") / "cudnn" / "bin",
            Path("ctranslate2"),
        ):
            candidate = root / relative
            if candidate.is_dir() and str(candidate) not in known:
                try:
                    _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(candidate)))
                    known.add(str(candidate))
                except OSError:
                    pass
        # CTranslate2 loads these by basename at first inference. Some Windows
        # environments ignore add_dll_directory for that native load, so keep
        # explicit WinDLL handles alive for the process lifetime as well.
        import ctypes

        for relative in (
            Path("nvidia") / "cublas" / "bin" / "cublasLt64_12.dll",
            Path("nvidia") / "cublas" / "bin" / "cublas64_12.dll",
            Path("ctranslate2") / "cudnn64_9.dll",
            Path("nvidia") / "cudnn" / "bin" / "cudnn64_9.dll",
        ):
            candidate = root / relative
            candidate_key = str(candidate)
            if candidate.is_file() and candidate_key not in _LOADED_NVIDIA_DLL_PATHS:
                try:
                    _NVIDIA_DLL_HANDLES.append(ctypes.WinDLL(str(candidate)))
                    _LOADED_NVIDIA_DLL_PATHS.add(candidate_key)
                except OSError:
                    pass


def _default_model_factory(*args: Any, **kwargs: Any) -> Any:
    _prepare_windows_nvidia_dlls()
    try:
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]
    except (ImportError, OSError) as exc:
        raise ModelLoadError(
            "faster-whisper is unavailable. Install the application dependencies first."
        ) from exc
    return WhisperModel(*args, **kwargs)


def _default_model_downloader(
    model_size: str,
    *,
    local_files_only: bool,
    cache_dir: str | None,
    tqdm_class: type[Any] | None = None,
) -> str:
    """Resolve a cached model or download it with an optional progress class."""

    try:
        from faster_whisper import download_model  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ModelLoadError(
            "faster-whisper is unavailable. Install the application dependencies first."
        ) from exc

    if local_files_only:
        return str(
            download_model(
                model_size,
                local_files_only=True,
                cache_dir=cache_dir,
            )
        )

    if tqdm_class is None:
        return str(download_model(model_size, cache_dir=cache_dir))

    # faster-whisper 1.2.1 hardcodes its silent tqdm implementation, so its
    # public helper cannot expose first-download progress. Mirror its narrow
    # model-file allowlist through huggingface_hub, which accepts tqdm_class.
    try:
        from faster_whisper.utils import _MODELS  # type: ignore[import-not-found]
        from huggingface_hub import snapshot_download  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ModelLoadError(
            "faster-whisper is unavailable. Install the application dependencies first."
        ) from exc

    repo_id = _MODELS.get(model_size)
    if repo_id is None:
        raise ValueError(f"Invalid model size {model_size!r}")
    return str(
        snapshot_download(
            repo_id,
            local_files_only=False,
            cache_dir=cache_dir,
            allow_patterns=[
                "config.json",
                "preprocessor_config.json",
                "model.bin",
                "tokenizer.json",
                "vocabulary.*",
            ],
            tqdm_class=tqdm_class,
        )
    )


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


_HALLUCINATION_PHRASES = {
    "blank audio",
    "thank you for watching",
    "thanks for watching",
    "please subscribe",
    "subtitles by the amara org community",
    "продолжение следует",
    "спасибо за просмотр",
    "субтитры сделал",
    "субтитры создавал",
}
_NON_WORD = re.compile(r"[\W_]+", flags=re.UNICODE)


def _normalized_words(text: str) -> list[str]:
    return [word for word in _NON_WORD.sub(" ", text.casefold()).split() if word]


def is_probable_hallucination(text: str) -> bool:
    """Recognize conservative, common hallucinations produced on silence."""

    words = _normalized_words(text)
    normalized = " ".join(words)
    if not normalized:
        return True
    if normalized in _HALLUCINATION_PHRASES:
        return True
    if normalized in {"music", "музыка", "applause", "тишина"}:
        return True
    # Long runs of one or two tokens are a typical silence-loop failure.  Keep
    # the limit high enough not to reject normal emphatic dictation.
    if len(words) >= 6 and len(set(words)) <= 2:
        return True
    return False


class FasterWhisperTranscriber:
    """Lazy, reusable faster-whisper model with CUDA-to-CPU fallback."""

    SUPPORTED_LANGUAGES = frozenset({"auto", "ru", "en"})

    def __init__(
        self,
        model_size: str = "small",
        *,
        device: str = "auto",
        compute_type: str = "auto",
        download_root: str | None = None,
        cpu_threads: int = 0,
        num_workers: int = 1,
        beam_size: int = 1,
        vad_filter: bool = True,
        vad_parameters: Mapping[str, Any] | None = None,
        no_speech_threshold: float = 0.75,
        log_prob_threshold: float = -1.0,
        min_audio_seconds: float = 0.10,
        silence_rms_threshold: float = 0.00001,
        local_files_only: bool = True,
        model_factory: ModelFactory | None = None,
        model_downloader: ModelDownloader | None = None,
    ) -> None:
        device = device.casefold()
        if device not in {"auto", "cuda", "cpu"}:
            raise ValueError("device must be auto, cuda, or cpu")
        if not model_size:
            raise ValueError("model_size cannot be empty")
        if min_audio_seconds < 0 or silence_rms_threshold < 0:
            raise ValueError("audio thresholds cannot be negative")

        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root
        self.cpu_threads = int(cpu_threads)
        self.num_workers = int(num_workers)
        self.beam_size = int(beam_size)
        self.vad_filter = bool(vad_filter)
        self.vad_parameters = {
            "threshold": 0.5,
            "min_speech_duration_ms": 150,
            "min_silence_duration_ms": 300,
            "speech_pad_ms": 200,
            **dict(vad_parameters or {}),
        }
        self.no_speech_threshold = float(no_speech_threshold)
        self.log_prob_threshold = float(log_prob_threshold)
        self.min_audio_seconds = float(min_audio_seconds)
        self.silence_rms_threshold = float(silence_rms_threshold)
        self.local_files_only = bool(local_files_only)
        self._model_factory = model_factory or _default_model_factory
        self._model_downloader = model_downloader or _default_model_downloader
        self._prepare_download = model_factory is None or model_downloader is not None

        self._lock = threading.RLock()
        self._model: Any | None = None
        self._active_device: str | None = None
        self._active_compute_type: str | None = None
        self._last_load_seconds = 0.0
        self._inference_primed = False
        self._download_progress_callback: DownloadProgressCallback | None = None

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return self._model is not None

    @property
    def active_device(self) -> str | None:
        return self._active_device

    @property
    def active_compute_type(self) -> str | None:
        return self._active_compute_type

    def _attempts(self) -> list[tuple[str, str]]:
        if self.device == "auto":
            if sys.platform == "darwin":
                compute_type = "int8" if self.compute_type == "auto" else self.compute_type
                return [("cpu", compute_type)]
            if self.compute_type == "auto":
                return [("cuda", "int8_float16"), ("cpu", "int8")]
            return [("cuda", self.compute_type), ("cpu", self.compute_type)]
        compute_type = self.compute_type
        if compute_type == "auto":
            compute_type = "int8_float16" if self.device == "cuda" else "int8"
        return [(self.device, compute_type)]

    def _construct(self, device: str, compute_type: str) -> Any:
        kwargs: dict[str, Any] = {
            "device": device,
            "compute_type": compute_type,
            "num_workers": self.num_workers,
            # The model itself always loads offline. `local_files_only=False`
            # on the transcriber only unlocks _prepare_model_download for an
            # absent model; letting it reach WhisperModel would make every
            # cold load re-resolve the model revision over the network, which
            # a local-first dictation app must never do on its own.
            "local_files_only": True,
        }
        if self.cpu_threads > 0:
            kwargs["cpu_threads"] = self.cpu_threads
        if self.download_root is not None:
            kwargs["download_root"] = self.download_root
        return self._model_factory(self.model_size, **kwargs)

    def set_download_progress_callback(
        self, callback: DownloadProgressCallback | None
    ) -> None:
        """Set the transient callback used while fetching an absent model."""

        with self._lock:
            self._download_progress_callback = callback

    def _prepare_model_download(self) -> None:
        if not self._prepare_download or self.local_files_only:
            return
        try:
            self._model_downloader(
                self.model_size,
                local_files_only=True,
                cache_dir=self.download_root,
            )
            return
        except Exception:
            pass

        callback = self._download_progress_callback
        if callback is not None:
            callback(0)

        # tqdm ships with huggingface_hub in the desktop install, but leaner
        # environments (macOS CI) run these code paths without it. Percentages
        # are a nicety: without tqdm the download simply proceeds silently.
        progress_class: type[Any] | None = None
        if callback is not None:
            try:
                from tqdm.auto import tqdm
            except ImportError:
                progress_class = None
            else:

                class DownloadProgress(tqdm):
                    def update(self, count: int = 1) -> bool | None:
                        changed = super().update(count)
                        if self.total and callback is not None:
                            callback(min(100, round(self.n / self.total * 100)))
                        return changed

                progress_class = DownloadProgress

        if progress_class is None:
            self._model_downloader(
                self.model_size,
                local_files_only=False,
                cache_dir=self.download_root,
            )
        else:
            self._model_downloader(
                self.model_size,
                local_files_only=False,
                cache_dir=self.download_root,
                tqdm_class=progress_class,
            )

    def load(self) -> Any:
        """Load once; auto mode tries CUDA int8_float16 then CPU int8."""

        with self._lock:
            if self._model is not None:
                self._last_load_seconds = 0.0
                return self._model
            self._prepare_model_download()
            started = time.perf_counter()
            errors: list[tuple[str, Exception]] = []
            for device, compute_type in self._attempts():
                try:
                    model = self._construct(device, compute_type)
                except Exception as exc:
                    errors.append((device, exc))
                    continue
                self._model = model
                self._active_device = device
                self._active_compute_type = compute_type
                self._last_load_seconds = time.perf_counter() - started
                return model

            self._last_load_seconds = time.perf_counter() - started
            detail = "; ".join(
                f"{backend}: {type(error).__name__}" for backend, error in errors
            )
            if self.local_files_only:
                hint = (
                    " Run scripts/setup-macos.sh"
                    if sys.platform == "darwin"
                    else " Run scripts\\setup.ps1"
                )
            else:
                hint = ""
            raise ModelLoadError(
                f"Could not load local model {self.model_size!r} ({detail}).{hint}"
            )

    def warmup(self) -> tuple[str, str]:
        """Preload the model and execute one short local inference pass."""

        with self._lock:
            self.load()
            if not self._inference_primed:
                # Loading CTranslate2 weights does not initialize every CUDA
                # kernel. Pay that one-time cost at startup instead of after
                # the user's first dictation. No generated text is retained.
                sample_count = max(int(TARGET_SAMPLE_RATE * 0.2), 1)
                time_axis = np.arange(sample_count, dtype=np.float32) / TARGET_SAMPLE_RATE
                probe = (0.001 * np.sin(2 * np.pi * 220 * time_axis)).astype(np.float32)
                try:
                    self.transcribe(probe, language="ru", vad_filter=False)
                except (NoSpeechDetected, HallucinationDetected):
                    pass
                self._inference_primed = True
        return str(self._active_device), str(self._active_compute_type)

    def _transcribe_kwargs(
        self,
        language: str,
        vad_filter: bool | None,
        vad_parameters: Mapping[str, Any] | None,
        initial_prompt: str | None,
    ) -> dict[str, Any]:
        use_vad = self.vad_filter if vad_filter is None else bool(vad_filter)
        kwargs: dict[str, Any] = {
            "language": None if language == "auto" else language,
            "beam_size": self.beam_size,
            "temperature": 0.0,
            "condition_on_previous_text": False,
            "vad_filter": use_vad,
            "no_speech_threshold": self.no_speech_threshold,
            "log_prob_threshold": self.log_prob_threshold,
        }
        if use_vad:
            kwargs["vad_parameters"] = {
                **self.vad_parameters,
                **dict(vad_parameters or {}),
            }
        if initial_prompt:
            kwargs["initial_prompt"] = initial_prompt
        return kwargs

    @staticmethod
    def _consume(model: Any, audio: np.ndarray, kwargs: dict[str, Any]) -> tuple[list[Any], Any]:
        segments, info = model.transcribe(audio, **kwargs)
        # faster-whisper returns a generator; model errors can be raised only
        # here, not by the preceding transcribe() call.
        return list(segments), info

    @staticmethod
    def _supported_languages_by_preference(info: Any) -> tuple[str, ...]:
        """Order RU and EN by the model's own posterior for this audio.

        Auto-detection ranges over every language Whisper knows, but only these
        two are ever produced, so when detection lands outside them the choice
        between RU and EN should come from the model's probabilities rather
        than from comparing how plausible the two transcripts look.  Older
        faster-whisper builds omit ``all_language_probs``; those keep the
        historical RU-then-EN order.
        """

        probabilities = _field(info, "all_language_probs", None) or ()
        table: dict[str, float] = {}
        try:
            for code, value in probabilities:
                table[str(code).casefold().strip()] = float(value)
        except (TypeError, ValueError):
            table = {}
        return tuple(
            sorted(("ru", "en"), key=lambda code: table.get(code, 0.0), reverse=True)
        )

    @staticmethod
    def _reported_language(info: Any) -> str | None:
        language = _field(info, "language", None)
        if not language:
            return None
        normalized = str(language).casefold().strip()
        return normalized or None

    def _candidate(
        self,
        raw_segments: list[Any],
        *,
        language: str,
        language_probability: float | None,
    ) -> _TranscriptionCandidate:
        accepted: list[TranscriptionSegment] = []
        hallucination_seen = False
        for raw in raw_segments:
            text = str(_field(raw, "text", "")).strip()
            if not _normalized_words(text):
                continue
            if is_probable_hallucination(text):
                hallucination_seen = True
                continue
            no_speech_prob = _optional_float(_field(raw, "no_speech_prob"))
            avg_logprob = _optional_float(_field(raw, "avg_logprob"))
            if (
                no_speech_prob is not None
                and no_speech_prob >= self.no_speech_threshold
                and (avg_logprob is None or avg_logprob <= self.log_prob_threshold)
            ):
                continue
            accepted.append(
                TranscriptionSegment(
                    start=float(_field(raw, "start", 0.0)),
                    end=float(_field(raw, "end", 0.0)),
                    text=text,
                    avg_logprob=avg_logprob,
                    no_speech_prob=no_speech_prob,
                )
            )

        text = " ".join(segment.text for segment in accepted).strip()
        if not text:
            if hallucination_seen:
                raise HallucinationDetected(
                    "Model output looks like a silence hallucination"
                )
            raise NoSpeechDetected("VAD/model found no trustworthy speech")
        if is_probable_hallucination(text):
            raise HallucinationDetected("Model output looks like a silence hallucination")

        # Confidence is deliberately more important than output length.  The
        # small, capped coverage term only breaks close calls in favour of a
        # candidate containing more usable speech instead of one lucky token.
        logprob_values = [
            max(-10.0, min(0.0, segment.avg_logprob))
            if segment.avg_logprob is not None
            else self.log_prob_threshold
            for segment in accepted
        ]
        speech_probability_values = [
            1.0 - max(0.0, min(1.0, segment.no_speech_prob))
            if segment.no_speech_prob is not None
            else 0.5
            for segment in accepted
        ]
        usable_characters = sum(
            len(word) for segment in accepted for word in _normalized_words(segment.text)
        )
        mean_logprob = float(np.mean(logprob_values))
        mean_speech_probability = float(np.mean(speech_probability_values))
        coverage = min(usable_characters, 120) / 120.0
        score = mean_logprob + mean_speech_probability + (0.15 * coverage)
        return _TranscriptionCandidate(
            text=text,
            language=language,
            language_probability=language_probability,
            segments=tuple(accepted),
            score=score,
        )

    def _replace_with_cpu(self) -> float:
        started = time.perf_counter()
        old_model = self._model
        self._model = None
        self._active_device = None
        self._active_compute_type = None
        close = getattr(old_model, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        try:
            self._model = self._construct("cpu", "int8")
        except Exception as exc:
            raise ModelLoadError("CUDA inference failed and CPU fallback could not load") from exc
        self._active_device = "cpu"
        self._active_compute_type = "int8"
        self._inference_primed = False
        return time.perf_counter() - started

    def transcribe(
        self,
        audio: Any,
        *,
        sample_rate: int = TARGET_SAMPLE_RATE,
        language: str = "auto",
        vad_filter: bool | None = None,
        vad_parameters: Mapping[str, Any] | None = None,
        initial_prompt: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe one waveform or reject it as silence/hallucination."""

        total_started = time.perf_counter()
        language = language.casefold().strip()
        if language not in self.SUPPORTED_LANGUAGES:
            raise ValueError("language must be auto, ru, or en")
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
        if sample_rate != TARGET_SAMPLE_RATE:
            samples = resample_audio(samples, sample_rate, TARGET_SAMPLE_RATE)

        load_seconds = 0.0
        inference_seconds = 0.0
        candidate: _TranscriptionCandidate
        with self._lock:
            was_loaded = self._model is not None
            model = self.load()
            if not was_loaded:
                load_seconds += self._last_load_seconds

            def infer(run_language: str) -> tuple[list[Any], Any]:
                nonlocal inference_seconds, load_seconds, model
                kwargs = self._transcribe_kwargs(
                    run_language, vad_filter, vad_parameters, initial_prompt
                )
                attempt_started = time.perf_counter()
                try:
                    result = self._consume(model, samples, kwargs)
                except (RuntimeError, OSError, MemoryError) as cuda_error:
                    inference_seconds += time.perf_counter() - attempt_started
                    if self.device != "auto" or self._active_device != "cuda":
                        raise TranscriptionError(
                            "Transcription inference failed"
                        ) from cuda_error
                    load_seconds += self._replace_with_cpu()
                    model = self._model
                    attempt_started = time.perf_counter()
                    try:
                        result = self._consume(model, samples, kwargs)
                    except Exception as cpu_error:
                        raise TranscriptionError(
                            "Transcription failed on both CUDA and CPU"
                        ) from cpu_error
                    finally:
                        inference_seconds += time.perf_counter() - attempt_started
                except Exception as exc:
                    inference_seconds += time.perf_counter() - attempt_started
                    raise TranscriptionError("Transcription inference failed") from exc
                else:
                    inference_seconds += time.perf_counter() - attempt_started
                return result

            raw_segments, info = infer(language)
            reported_language = self._reported_language(info)
            probability = _optional_float(_field(info, "language_probability", None))

            if language != "auto":
                candidate = self._candidate(
                    raw_segments,
                    language=language,
                    language_probability=probability,
                )
            elif reported_language in {"ru", "en"}:
                # Normal RU/EN dictation stays on the single-inference fast path.
                candidate = self._candidate(
                    raw_segments,
                    language=reported_language,
                    language_probability=probability,
                )
            else:
                # Detection wandered outside the two supported languages. Ask
                # the model which of them it actually prefers and stop at the
                # first trustworthy transcript instead of always running both.
                fixed_candidates: list[_TranscriptionCandidate] = []
                inference_errors: list[TranscriptionError] = []
                for fixed_language in self._supported_languages_by_preference(info):
                    try:
                        fixed_segments, fixed_info = infer(fixed_language)
                        fixed_candidates.append(
                            self._candidate(
                                fixed_segments,
                                language=fixed_language,
                                language_probability=_optional_float(
                                    _field(
                                        fixed_info,
                                        "language_probability",
                                        None,
                                    )
                                ),
                            )
                        )
                    except (NoSpeechDetected, HallucinationDetected):
                        continue
                    except TranscriptionError as exc:
                        inference_errors.append(exc)
                    else:
                        # The model's preferred language produced a usable
                        # transcript; transcribing the other one cannot improve
                        # on a choice the model already made.
                        break
                if not fixed_candidates:
                    if len(inference_errors) == 2:
                        raise TranscriptionError(
                            "Both RU and EN fallback transcriptions failed"
                        ) from inference_errors[-1]
                    raise NoSpeechDetected(
                        "Auto-detection was outside RU/EN and no trustworthy "
                        "RU/EN transcription was found"
                    )
                candidate = max(fixed_candidates, key=lambda item: item.score)

        total_seconds = time.perf_counter() - total_started
        return TranscriptionResult(
            text=candidate.text,
            language=candidate.language,
            language_probability=candidate.language_probability,
            segments=candidate.segments,
            audio_duration_seconds=duration,
            timings=TranscriptionTimings(
                model_load_seconds=load_seconds,
                inference_seconds=inference_seconds,
                total_seconds=total_seconds,
            ),
            device=str(self._active_device),
            compute_type=str(self._active_compute_type),
        )

    def close(self) -> None:
        """Drop the persistent model, allowing native resources to be freed."""

        with self._lock:
            model = self._model
            self._model = None
            self._active_device = None
            self._active_compute_type = None
            self._inference_primed = False
            close = getattr(model, "close", None)
            if callable(close):
                close()

    unload = close
