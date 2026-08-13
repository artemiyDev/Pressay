from __future__ import annotations

from pressay.doctor import Check


def test_check_defaults_to_required() -> None:
    check = Check("thing", True, "ready")

    assert check.level == "required"
    assert check.ok is True
