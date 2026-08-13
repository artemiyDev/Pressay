from __future__ import annotations

from pathlib import Path

import pressay.platform_support as platform_support


def test_windows_and_macos_native_data_directories(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(platform_support.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert platform_support.user_data_directory() == tmp_path / "Pressay"

    monkeypatch.setattr(platform_support.sys, "platform", "darwin")
    monkeypatch.setattr(platform_support.Path, "home", classmethod(lambda cls: tmp_path))
    assert platform_support.user_data_directory() == (
        tmp_path / "Library" / "Application Support" / "Pressay"
    )


def test_platform_hotkey_labels(monkeypatch) -> None:
    monkeypatch.setattr(platform_support.sys, "platform", "darwin")
    assert platform_support.hotkey_hint("hold") == "Control+Option"
    assert platform_support.hotkey_hint("copy") == "Control+Option+C"

    monkeypatch.setattr(platform_support.sys, "platform", "win32")
    assert platform_support.hotkey_hint("hold") == "Ctrl+Win"
