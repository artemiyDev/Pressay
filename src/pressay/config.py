"""Persistent application settings for Pressay.

The module deliberately has no UI or audio dependencies.  Configuration is
stored as UTF-8 JSON in the native per-user data directory and replaced atomically so a crash
cannot leave a partially-written settings file behind.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping

from . import hotkey_bindings
from .platform_support import is_windows, user_data_directory
from .text import replacement_key, snippet_key


APP_DIRECTORY = "Pressay"
LEGACY_APP_DIRECTORY = "WhisperFlow"
CONFIG_FILENAME = "config.json"
SUPPORTED_LANGUAGES = frozenset({"auto", "ru", "en"})
SUPPORTED_RESOURCE_MODES = frozenset({"instant", "balanced", "eco"})


class ConfigError(ValueError):
    """Raised when a configuration file cannot be read or validated."""


def config_path(local_appdata: str | os.PathLike[str] | None = None) -> Path:
    """Return the default per-user configuration path.

    A caller may pass *local_appdata* to select an explicit Windows-style base
    directory in tests or migration tools.
    """

    if local_appdata is not None:
        return Path(local_appdata) / APP_DIRECTORY / CONFIG_FILENAME
    return user_data_directory() / CONFIG_FILENAME


def legacy_config_path(local_appdata: str | os.PathLike[str] | None = None) -> Path:
    """Return the previous product-name path used before the Pressay rename."""

    if local_appdata is None:
        local_appdata = os.environ.get("LOCALAPPDATA")
    base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    return base / LEGACY_APP_DIRECTORY / CONFIG_FILENAME


def _string_map(
    value: object, setting: str, key_func: Callable[[str], str]
) -> dict[str, str]:
    """Validate a snippets/replacements mapping against its runtime key rule.

    *key_func* must be the same normalization the matching runtime function in
    :mod:`pressay.text` applies (``replacement_key`` for ``apply_replacements``,
    ``snippet_key`` for ``expand_snippet``), so a config that passes here is
    guaranteed not to raise ``ValueError`` there.
    """

    if not isinstance(value, Mapping):
        raise ConfigError(f"{setting} must be a JSON object")

    result: dict[str, str] = {}
    seen_keys: set[str] = set()
    for key, replacement in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigError(f"{setting} keys must be non-empty strings")
        if not isinstance(replacement, str):
            raise ConfigError(f"{setting} values must be strings")
        normalized_key = key_func(key)
        if not normalized_key:
            raise ConfigError(f"{setting} key {key!r} normalizes to an empty string")
        if normalized_key in seen_keys:
            raise ConfigError(f"{setting} keys must be unique after normalization: {key!r}")
        seen_keys.add(normalized_key)
        result[key] = replacement
    return result


def _hotkeys(value: object) -> hotkey_bindings.HotkeyBindings:
    """Parse the ``hotkeys`` section, reporting problems as ConfigError."""

    try:
        return hotkey_bindings.from_mapping(value)
    except hotkey_bindings.HotkeyBindingError as exc:
        raise ConfigError(f"hotkeys: {exc}") from exc


def _bool(value: object, setting: str) -> bool:
    # bool is intentionally checked exactly: JSON integers 0/1 should not
    # silently turn a mistyped setting on or off.
    if type(value) is not bool:
        raise ConfigError(f"{setting} must be a boolean")
    return value


def _non_empty_string(value: object, setting: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{setting} must be a non-empty string")
    return value.strip()


@dataclass(slots=True)
class AppConfig:
    """User-editable Pressay settings.

    ``microphone`` normally stores the audio backend's stable selector;
    ``None`` means the system default input device.  Integer device indexes
    remain accepted so configurations written by older versions can migrate
    on their next UI save. Snippet triggers and replacement keys are
    interpreted by :mod:`pressay.text` as literal text, never as regular
    expressions.
    """

    model: str = "turbo"
    language: str = "auto"
    microphone: str | int | None = None
    auto_insert: bool = True
    smart_spacing: bool = True
    remove_fillers: bool = False
    voice_press_enter: bool = False
    voice_formatting: bool = False
    strict_editable_check: bool = False
    resource_mode: str = "instant"
    snippets: dict[str, str] = field(default_factory=dict)
    replacements: dict[str, str] = field(default_factory=dict)
    hotkeys: hotkey_bindings.HotkeyBindings = field(
        default_factory=hotkey_bindings.HotkeyBindings
    )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AppConfig":
        """Validate settings loaded from JSON.

        Unknown keys are ignored for forward/backward compatibility.  Known
        keys are never coerced, which prevents corrupt JSON from silently
        changing behaviour.
        """

        if not isinstance(raw, Mapping):
            raise ConfigError("configuration root must be a JSON object")

        defaults = cls()
        model = _non_empty_string(raw.get("model", defaults.model), "model")
        language = _non_empty_string(raw.get("language", defaults.language), "language")
        if language not in SUPPORTED_LANGUAGES:
            raise ConfigError("language must be one of: auto, ru, en")
        resource_mode = _non_empty_string(
            raw.get("resource_mode", defaults.resource_mode),
            "resource_mode",
        )
        if resource_mode not in SUPPORTED_RESOURCE_MODES:
            raise ConfigError("resource_mode must be one of: instant, balanced, eco")

        microphone = raw.get("microphone", defaults.microphone)
        if type(microphone) is int:
            if microphone < 0:
                raise ConfigError("microphone index must be non-negative")
        elif microphone is not None:
            microphone = _non_empty_string(microphone, "microphone")

        return cls(
            model=model,
            language=language,
            microphone=microphone,
            auto_insert=_bool(raw.get("auto_insert", defaults.auto_insert), "auto_insert"),
            smart_spacing=_bool(
                raw.get("smart_spacing", defaults.smart_spacing), "smart_spacing"
            ),
            remove_fillers=_bool(
                raw.get("remove_fillers", defaults.remove_fillers), "remove_fillers"
            ),
            voice_press_enter=_bool(
                raw.get("voice_press_enter", defaults.voice_press_enter),
                "voice_press_enter",
            ),
            voice_formatting=_bool(
                raw.get("voice_formatting", defaults.voice_formatting),
                "voice_formatting",
            ),
            strict_editable_check=_bool(
                raw.get("strict_editable_check", defaults.strict_editable_check),
                "strict_editable_check",
            ),
            resource_mode=resource_mode,
            snippets=_string_map(
                raw.get("snippets", defaults.snippets), "snippets", snippet_key
            ),
            replacements=_string_map(
                raw.get("replacements", defaults.replacements), "replacements", replacement_key
            ),
            hotkeys=_hotkeys(raw.get("hotkeys")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a validated, JSON-serializable representation."""

        # asdict() recurses into the nested binding dataclasses and would emit
        # their internal shape (including a frozenset of modifier names), which
        # is neither JSON-serializable nor what from_dict() accepts. The
        # bindings therefore carry their own canonical text form.
        raw = asdict(self)
        raw["hotkeys"] = self.hotkeys.to_mapping()
        # Round-tripping through the validator also protects callers that
        # mutated one of the dictionary fields after construction.
        validated = type(self).from_dict(raw)
        result = asdict(validated)
        result["hotkeys"] = validated.hotkeys.to_mapping()
        return result

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "AppConfig":
        """Load *path*, returning defaults when the file does not yet exist."""

        target = Path(path) if path is not None else config_path()
        if path is None and is_windows() and not target.exists():
            previous = legacy_config_path()
            if previous.is_file():
                target = previous
        try:
            with target.open("r", encoding="utf-8") as stream:
                raw = json.load(stream)
        except FileNotFoundError:
            return cls()
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read configuration {target}: {exc}") from exc

        return cls.from_dict(raw)

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        """Atomically save settings and return the path written.

        The temporary file is created beside the destination, flushed to disk,
        and then installed with :func:`os.replace`.  Therefore readers observe
        either the old complete file or the new complete file.
        """

        target = Path(path) if path is not None else config_path()
        data = self.to_dict()
        temp_path: Path | None = None

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
                delete=False,
            ) as stream:
                temp_path = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())

            os.replace(temp_path, target)
            temp_path = None
        except (OSError, TypeError, ValueError) as exc:
            raise ConfigError(f"cannot save configuration {target}: {exc}") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    # The original write error is more useful to the caller.
                    pass

        return target
