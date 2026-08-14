"""Small platform boundary shared by the desktop entry point and adapters."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from types import ModuleType
from typing import Any


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def platform_label() -> str:
    if is_windows():
        return "Windows"
    if is_macos():
        return "macOS"
    return sys.platform


def user_data_directory() -> Path:
    """Return the native per-user application data directory."""

    if is_windows():
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "Pressay"
        return Path.home() / "AppData" / "Local" / "Pressay"
    if is_macos():
        return Path.home() / "Library" / "Application Support" / "Pressay"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "Pressay"


def input_adapter() -> ModuleType:
    """Load the guarded text adapter for the current desktop platform."""

    if is_windows():
        from . import windows_input

        return windows_input
    if is_macos():
        from . import macos_input

        return macos_input
    raise RuntimeError(f"Pressay does not support text delivery on {sys.platform}")


def hotkey_hint(action: str, bindings: Any | None = None) -> str | None:
    """Label for a gesture; ``None`` when the user disabled that action.

    macOS chords are fixed for now, so *bindings* only affects Windows.
    """

    mac = {
        "hold": "Control+Option",
        "toggle": "Control+Option+Space",
        "cancel": "Esc",
        "paste": "Control+Option+V",
        "copy": "Control+Option+C",
    }
    if is_macos():
        return mac[action]
    if bindings is None:
        return {
            "hold": "Ctrl+Win",
            "toggle": "Ctrl+Win+Space",
            "cancel": "Esc",
            "paste": "Shift+Alt+Z",
            "copy": "Shift+Alt+X",
        }[action]
    return {
        "hold": bindings.hold_label(),
        "toggle": bindings.toggle_label(),
        "cancel": "Esc",
        "paste": None if bindings.paste_last is None else bindings.paste_last.label(),
        "copy": None if bindings.copy_last is None else bindings.copy_last.label(),
    }[action]


__all__ = [
    "hotkey_hint",
    "input_adapter",
    "is_macos",
    "is_windows",
    "platform_label",
    "user_data_directory",
]
