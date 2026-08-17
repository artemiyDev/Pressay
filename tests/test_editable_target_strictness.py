from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from pressay.app import _settings_dict, _snapshot_target
from pressay.config import AppConfig, ConfigError
from pressay.controller import _insertion_status_text
from pressay.macos_input import describe_focus as describe_macos_focus
from pressay.ui import MicrophoneChoice, SettingsWindow, UiSignals
from pressay.windows_input import ForegroundTarget, describe_focus, target_looks_editable


def _uia_target(
    *,
    control_type: int,
    enabled: bool = True,
    keyboard_focusable: bool = True,
    value_writable: bool = False,
    text_editable: bool = False,
) -> ForegroundTarget:
    return ForegroundTarget(
        hwnd=100,
        pid=200,
        focused_control=(
            "uia",
            200,
            1,
            2,
            "field",
            "Edit",
            control_type,
            enabled,
            keyboard_focusable,
            value_writable,
            text_editable,
        ),
    )


def test_focusable_pane_is_allowed_only_in_soft_mode() -> None:
    target = _uia_target(control_type=50033)

    assert target_looks_editable(target) is True
    assert target_looks_editable(target, strict=True) is False


@pytest.mark.parametrize("strict", (False, True))
def test_writable_uia_pattern_allows_an_exotic_control_in_both_modes(strict: bool) -> None:
    target = _uia_target(control_type=50026, text_editable=True)

    assert target_looks_editable(target, strict=strict) is True


@pytest.mark.parametrize("strict", (False, True))
def test_group_with_only_value_pattern_is_rejected_in_both_modes(strict: bool) -> None:
    target = _uia_target(control_type=50026, value_writable=True)

    assert target_looks_editable(target, strict=strict) is False


@pytest.mark.parametrize("strict", (False, True))
def test_list_item_without_editing_patterns_is_rejected_in_both_modes(strict: bool) -> None:
    assert target_looks_editable(_uia_target(control_type=50007), strict=strict) is False


@pytest.mark.parametrize("strict", (False, True))
def test_disabled_uia_control_is_rejected_even_with_editing_pattern(strict: bool) -> None:
    target = _uia_target(control_type=50026, enabled=False, value_writable=True)

    assert target_looks_editable(target, strict=strict) is False


def test_strict_editable_check_config_round_trip_and_invalid_value(tmp_path) -> None:
    target = tmp_path / "config.json"
    AppConfig(strict_editable_check=True).save(target)

    assert AppConfig.load(target).strict_editable_check is True
    target.write_text(json.dumps({"strict_editable_check": 1}), encoding="utf-8")
    with pytest.raises(ConfigError, match="strict_editable_check"):
        AppConfig.load(target)


def test_strict_editable_check_checkbox_is_exposed_with_its_hint(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    window = SettingsWindow(
        UiSignals(),
        _settings_dict(AppConfig(strict_editable_check=True)),
        [MicrophoneChoice(None, "Системный микрофон")],
    )
    try:
        assert window.strict_editable_check_checkbox.isChecked() is True
        assert window.current_settings()["strict_editable_check"] is True
        assert any("Pressay не будет вставлять" in label.text() for label in window._hint_labels)
    finally:
        window.prepare_to_quit()
        window.close()
        QApplication.instance().processEvents()


def test_describe_focus_exposes_uia_evidence_and_macos_uses_none() -> None:
    target = _uia_target(
        control_type=50033,
        keyboard_focusable=False,
        value_writable=True,
    )

    assert describe_focus(target) == {
        "focus_kind": "uia",
        "control_type": 50033,
        "enabled": True,
        "keyboard_focusable": False,
        "value_writable": True,
        "text_editable": False,
    }
    assert describe_macos_focus(None) == {
        "focus_kind": "none",
        "control_type": None,
        "enabled": None,
        "keyboard_focusable": None,
        "value_writable": None,
        "text_editable": None,
    }


def test_snapshot_log_appends_focus_evidence(monkeypatch, caplog) -> None:
    target = _uia_target(control_type=50033)
    adapter = SimpleNamespace(
        snapshot_foreground_target=lambda: target,
        target_looks_editable=lambda _target, *, strict: not strict,
        describe_focus=describe_focus,
    )
    monkeypatch.setattr("pressay.app.input_adapter", lambda: adapter)

    with caplog.at_level("INFO"):
        assert _snapshot_target(strict_editable_check=False) is target

    assert (
        "recording_target_captured valid=True editable=True hwnd=100 pid=200 "
        "focus_kind=uia control_type=50033 enabled=True focusable=True "
        "value_writable=False text_editable=False"
    ) in caplog.text


def test_non_editable_status_names_the_reason_and_recovery_setting() -> None:
    status = _insertion_status_text("focused_control_is_not_editable")

    assert "Поле не распознано" in status
    assert "Вставлять только" in status
