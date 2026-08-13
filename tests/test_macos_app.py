from __future__ import annotations

import sys

import pytest

from pressay.app import _SingleInstance


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS file lock required")
def test_macos_single_instance_lock_is_reacquirable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("pressay.app.user_data_directory", lambda: tmp_path)
    first = _SingleInstance()
    second = _SingleInstance()

    assert first.acquire() is True
    assert second.acquire() is False
    first.close()

    third = _SingleInstance()
    assert third.acquire() is True
    third.close()
