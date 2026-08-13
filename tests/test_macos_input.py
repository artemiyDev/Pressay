from __future__ import annotations

from dataclasses import replace

from pressay.macos_input import (
    ForegroundTarget,
    InputStatus,
    copy_text,
    paste_last,
    send_text,
    targets_match,
)


def target(identity: int = 1, *, editable: bool = True) -> ForegroundTarget:
    return ForegroundTarget(
        pid=42,
        focused_control=("ax", 42, identity, "AXTextArea", "", "editor"),
        editable=editable,
    )


class FakeBackend:
    def __init__(self, snapshots: list[ForegroundTarget] | None = None) -> None:
        self.snapshots = list(snapshots or [target()])
        self.last = self.snapshots[-1]
        self.modifiers_are_released = True
        self.unicode: list[str] = []
        self.enter_calls = 0
        self.copied: list[str] = []

    def snapshot_foreground_target(self) -> ForegroundTarget:
        if self.snapshots:
            self.last = self.snapshots.pop(0)
        return self.last

    def modifiers_released(self) -> bool:
        return self.modifiers_are_released

    def send_unicode(self, text: str) -> bool:
        self.unicode.append(text)
        return True

    def send_enter(self) -> bool:
        self.enter_calls += 1
        return True

    def copy_text(self, text: str) -> None:
        self.copied.append(text)


def test_targets_require_same_ax_element_identity() -> None:
    original = target()

    assert targets_match(original, replace(original, captured_at=99.0))
    assert not targets_match(original, target(2))


def test_unicode_delivery_rechecks_focus_before_each_batch_and_enter() -> None:
    expected = target()
    backend = FakeBackend([expected, expected, expected, expected])

    outcome = send_text(
        "Привет, Mac! " * 4,
        expected_target=expected,
        press_enter=True,
        backend=backend,
    )

    assert outcome.success is True
    assert outcome.status is InputStatus.INSERTED_UNICODE
    assert "".join(backend.unicode) == "Привет, Mac! " * 4
    assert backend.enter_calls == 1


def test_focus_change_fails_closed_before_any_input() -> None:
    expected = target()
    backend = FakeBackend([target(2)])

    outcome = send_text("secret", expected_target=expected, backend=backend)

    assert outcome.success is False
    assert outcome.status is InputStatus.TARGET_MISMATCH
    assert backend.unicode == []
    assert backend.enter_calls == 0


def test_noneditable_and_held_modifiers_never_inject() -> None:
    noneditable = target(editable=False)
    backend = FakeBackend([noneditable])
    outcome = send_text("text", expected_target=noneditable, backend=backend)
    assert outcome.status is InputStatus.TARGET_REQUIRED
    assert backend.unicode == []

    expected = target()
    backend = FakeBackend([expected])
    backend.modifiers_are_released = False
    outcome = send_text("text", expected_target=expected, backend=backend)
    assert outcome.status is InputStatus.MODIFIERS_HELD
    assert backend.unicode == []


def test_explicit_paste_is_direct_and_copy_is_explicit() -> None:
    expected = target()
    backend = FakeBackend([expected, expected, expected])

    pasted = paste_last("again", backend=backend)
    copied = copy_text("copy", backend=backend)

    assert pasted.success is True
    assert backend.unicode == ["again"]
    assert copied.copied is True
    assert backend.copied == ["copy"]
