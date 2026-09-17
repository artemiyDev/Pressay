from __future__ import annotations

import pytest

from pressay import doctor
from pressay.config import AppConfig
from pressay.doctor import Check


def test_check_defaults_to_required() -> None:
    check = Check("thing", True, "ready")

    assert check.level == "required"
    assert check.ok is True


def test_gigaam_check_warns_when_onnx_asr_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor,
        "_module_check",
        lambda name: Check(name, False, "ModuleNotFoundError: No module named 'onnx_asr'"),
    )
    called: list[str] = []
    monkeypatch.setattr(
        doctor.model_setup_module,
        "gigaam_model_ready",
        lambda model: called.append(model) or True,
    )

    check = doctor._gigaam_check(AppConfig(russian_engine="gigaam"))

    assert check.ok is False
    assert check.level == "optional"
    assert "onnx-asr is not installed" in check.detail
    assert "Reinstall Pressay" in check.detail
    assert called == [], "must not touch the cache once onnx-asr is missing"


def test_gigaam_check_warns_when_model_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_module_check", lambda name: Check(name, True, "installed"))
    monkeypatch.setattr(doctor.model_setup_module, "gigaam_model_ready", lambda model: False)

    check = doctor._gigaam_check(AppConfig(russian_engine="gigaam", gigaam_model="gigaam-v3-e2e-rnnt"))

    assert check.ok is False
    assert check.level == "optional"
    assert "gigaam-v3-e2e-rnnt" in check.detail
    assert "not cached yet" in check.detail
    assert "pressay.model_setup" in check.detail


def test_gigaam_check_is_ok_when_installed_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_module_check", lambda name: Check(name, True, "installed"))
    monkeypatch.setattr(doctor.model_setup_module, "gigaam_model_ready", lambda model: True)

    check = doctor._gigaam_check(AppConfig(russian_engine="gigaam", gigaam_model="gigaam-v3-e2e-rnnt"))

    assert check.ok is True
    assert check.level == "optional"
    assert "gigaam-v3-e2e-rnnt" in check.detail


def test_gigaam_check_ok_when_unavailable_but_whisper_is_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not having GigaAM ready is not a problem if the user picked Whisper."""

    monkeypatch.setattr(doctor, "_module_check", lambda name: Check(name, False, "boom"))

    check = doctor._gigaam_check(AppConfig(russian_engine="whisper"))

    assert check.ok is True


def test_collect_checks_does_not_load_the_gigaam_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The doctor is read-only: it must never call onnx_asr.load_model()."""

    monkeypatch.setattr(doctor, "_module_check", lambda name: Check(name, True, "installed"))
    monkeypatch.setattr(doctor.model_setup_module, "gigaam_model_ready", lambda model: True)

    checks, _devices = doctor.collect_checks("turbo", config=AppConfig())

    gigaam_checks = [c for c in checks if c.name == "gigaam"]
    assert len(gigaam_checks) == 1
    assert gigaam_checks[0].ok is True
