"""Read-only environment diagnostics for Pressay.

The doctor deliberately never opens an input stream: enumerating microphones is
enough to validate PortAudio/WASAPI without recording the user.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    level: str = "required"


def _module_check(module_name: str) -> Check:
    try:
        module = importlib.import_module(module_name)
        version = getattr(module, "__version__", "installed")
        return Check(module_name, True, str(version))
    except Exception as exc:  # diagnostics must report every broken import
        return Check(module_name, False, f"{type(exc).__name__}: {exc}")


def _audio_check() -> tuple[Check, list[dict[str, Any]]]:
    try:
        import sounddevice as sd

        devices: list[dict[str, Any]] = []
        for index, raw in enumerate(sd.query_devices()):
            channels = int(raw.get("max_input_channels", 0))
            if channels <= 0:
                continue
            devices.append(
                {
                    "index": index,
                    "name": str(raw.get("name", "Unknown input")),
                    "channels": channels,
                    "sample_rate": int(float(raw.get("default_samplerate", 0))),
                }
            )
        default_input = sd.default.device[0] if sd.default.device else None
        detail = f"{len(devices)} input device(s); default={default_input}"
        return Check("microphones", bool(devices), detail), devices
    except Exception as exc:
        return Check("microphones", False, f"{type(exc).__name__}: {exc}"), []


def _cuda_check() -> Check:
    try:
        import ctranslate2

        count = int(ctranslate2.get_cuda_device_count())
        if count < 1:
            return Check("cuda", False, "No CUDA device; CPU fallback will be used", "optional")
        compute_types = sorted(ctranslate2.get_supported_compute_types("cuda"))
        return Check("cuda", True, f"{count} device(s); {', '.join(compute_types)}", "optional")
    except Exception as exc:
        return Check(
            "cuda",
            False,
            f"Unavailable ({type(exc).__name__}: {exc}); CPU fallback will be used",
            "optional",
        )


def _cuda_runtime_check() -> Check:
    candidates: list[Path] = []
    for root in dict.fromkeys(Path(item) for item in sys.path if item):
        candidates.extend(
            (
                root / "nvidia" / "cublas" / "bin" / "cublas64_12.dll",
                root / "ctranslate2" / "cudnn64_9.dll",
                root / "nvidia" / "cudnn" / "bin" / "cudnn64_9.dll",
            )
        )
    cublas = [path for path in candidates if path.name == "cublas64_12.dll" and path.exists()]
    cudnn = [path for path in candidates if path.name == "cudnn64_9.dll" and path.exists()]
    if cublas and cudnn:
        return Check("cuda_runtime", True, "cuBLAS 12 and cuDNN 9 DLLs found", "optional")
    missing = []
    if not cublas:
        missing.append("cuBLAS 12")
    if not cudnn:
        missing.append("cuDNN 9")
    return Check(
        "cuda_runtime",
        False,
        f"Missing {', '.join(missing)}; CPU fallback will be used",
        "optional",
    )


def _model_cache_check(model: str) -> Check:
    aliases = {
        "tiny": "models--Systran--faster-whisper-tiny",
        "base": "models--Systran--faster-whisper-base",
        "small": "models--Systran--faster-whisper-small",
        "medium": "models--Systran--faster-whisper-medium",
        "large-v3": "models--Systran--faster-whisper-large-v3",
        "turbo": "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo",
        "large-v3-turbo": "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo",
    }
    cache_roots: list[Path] = []
    explicit = os.environ.get("HF_HUB_CACHE")
    if explicit:
        cache_roots.append(Path(explicit))
    cache_roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        cache_roots.append(Path(local_app_data) / "Pressay" / "models")

    folder = aliases.get(model, f"models--Systran--faster-whisper-{model}")
    matches = [root / folder for root in cache_roots if (root / folder).exists()]
    if matches:
        return Check("model", True, f"{model} is cached in {matches[0]}", "optional")
    return Check(
        "model",
        False,
        f"{model} is not cached yet; first setup/recognition requires internet",
        "optional",
    )


def collect_checks(model: str = "turbo") -> tuple[list[Check], list[dict[str, Any]]]:
    checks = [
        Check(
            "windows",
            sys.platform == "win32",
            f"{platform.system()} {platform.release()} {platform.machine()}",
        ),
        Check("python", sys.version_info[:2] == (3, 11), platform.python_version()),
        _module_check("numpy"),
        _module_check("sounddevice"),
        _module_check("faster_whisper"),
        _module_check("PySide6"),
        _module_check("win32api"),
        _cuda_check(),
        _cuda_runtime_check(),
        _model_cache_check(model),
    ]
    microphone_check, devices = _audio_check()
    checks.append(microphone_check)
    return checks, devices


def _render_human(checks: list[Check], devices: list[dict[str, Any]]) -> None:
    print("Pressay doctor")
    print(f"Python: {sys.executable}")
    for check in checks:
        symbol = "OK" if check.ok else ("WARN" if check.level == "optional" else "FAIL")
        print(f"[{symbol:4}] {check.name}: {check.detail}")
    if devices:
        print("Input devices:")
        for device in devices:
            print(
                f"  {device['index']}: {device['name']} "
                f"({device['channels']} ch, {device['sample_rate']} Hz)"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Pressay environment checks")
    parser.add_argument("--model", default="turbo", help="Model whose local cache should be checked")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args(argv)

    checks, devices = collect_checks(args.model)
    if args.json:
        print(json.dumps({"checks": [asdict(c) for c in checks], "devices": devices}, ensure_ascii=False, indent=2))
    else:
        _render_human(checks, devices)
    return 1 if any(not c.ok and c.level == "required" for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
