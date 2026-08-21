from __future__ import annotations

import threading
import time
from typing import Callable

import pytest

from pressay.hotkey_coordinator import (
    _HotkeyCallbackGate,
    _WindowsHotkeyCoordinator,
)


class _HotkeyBlock:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()


class _FakeHotkeyFactory:
    def __init__(self) -> None:
        self.services: list[_FakeHotkeyService] = []
        self.start_blocks: dict[str, _HotkeyBlock] = {}
        self.stop_blocks: dict[str, _HotkeyBlock] = {}
        self.start_failures: set[str] = set()
        self.stop_failures: set[str] = set()
        self.events: list[str] = []
        self.active_count = 0
        self.max_active_count = 0
        self._lock = threading.Lock()

    def __call__(self, callback, *, bindings: str):
        service = _FakeHotkeyService(self, callback, bindings)
        self.services.append(service)
        return service

    def set_running(self, service: "_FakeHotkeyService", running: bool) -> None:
        with self._lock:
            if service.running == running:
                return
            service.running = running
            self.active_count += 1 if running else -1
            self.max_active_count = max(self.max_active_count, self.active_count)


class _FakeHotkeyService:
    def __init__(self, factory: _FakeHotkeyFactory, callback, bindings: str) -> None:
        self.factory = factory
        self.callback = callback
        self.bindings = bindings
        self.running = False
        self.stop_requested = threading.Event()
        self.stop_calls = 0
        self.retire_calls = 0

    def start(self) -> None:
        block = self.factory.start_blocks.get(self.bindings)
        if block is not None:
            block.entered.set()
            while not block.release.wait(0.005):
                if self.stop_requested.is_set():
                    raise RuntimeError("startup stopped")
        if self.bindings in self.factory.start_failures:
            raise RuntimeError(f"cannot start {self.bindings}")
        if self.stop_requested.is_set():
            raise RuntimeError("startup stopped")
        self.factory.set_running(self, True)
        self.factory.events.append(f"start:{self.bindings}")

    def retire_callbacks(self) -> None:
        self.retire_calls += 1

    def stop(self, timeout_s: float = 3.0) -> bool:
        self.stop_calls += 1
        self.stop_requested.set()
        self.factory.events.append(f"stop:{self.bindings}")
        if self.bindings in self.factory.stop_failures:
            return False
        block = self.factory.stop_blocks.get(self.bindings)
        if block is not None:
            block.entered.set()
            if not block.release.wait(timeout_s):
                return False
        self.factory.set_running(self, False)
        return True

    def fire(self, action: str) -> None:
        self.callback(action)


def _hotkey_coordinator(
    factory: _FakeHotkeyFactory,
    received: list[str] | None = None,
) -> _WindowsHotkeyCoordinator:
    received = [] if received is None else received
    coordinator = _WindowsHotkeyCoordinator(
        factory,
        lambda action, _still_current: received.append(action),
        lambda callback, *args: callback(*args),
        "old",
    )
    coordinator.start()
    return coordinator


def test_hotkey_change_supersedes_candidate_blocked_before_persist() -> None:
    factory = _FakeHotkeyFactory()
    block_a = _HotkeyBlock()
    factory.start_blocks["A"] = block_a
    persisted: list[str] = []
    applied: list[str] = []
    coordinator = _hotkey_coordinator(factory)
    try:
        future_a = coordinator.request_change(
            "A",
            persist=lambda: persisted.append("A"),
            on_applied=lambda: applied.append("A"),
            on_failed=lambda _error: None,
        )
        assert block_a.entered.wait(timeout=1)
        future_b = coordinator.request_change(
            "B",
            persist=lambda: persisted.append("B"),
            on_applied=lambda: applied.append("B"),
            on_failed=lambda _error: None,
        )
        block_a.release.set()

        assert future_a.result(timeout=1) is False
        assert future_b.result(timeout=1) is True
        assert persisted == ["B"]
        assert applied == ["B"]
        assert coordinator.active_service.bindings == "B"
        assert factory.max_active_count == 1
    finally:
        coordinator.stop()


def test_hotkey_change_commits_in_order_when_superseded_during_persist() -> None:
    factory = _FakeHotkeyFactory()
    persist_a_entered = threading.Event()
    release_persist_a = threading.Event()
    persisted: list[str] = []
    applied: list[str] = []
    coordinator = _hotkey_coordinator(factory)

    def persist_a() -> None:
        persist_a_entered.set()
        assert release_persist_a.wait(timeout=2)
        persisted.append("A")

    try:
        future_a = coordinator.request_change(
            "A",
            persist=persist_a,
            on_applied=lambda: applied.append("A"),
            on_failed=lambda _error: None,
        )
        assert persist_a_entered.wait(timeout=1)
        future_b = coordinator.request_change(
            "B",
            persist=lambda: persisted.append("B"),
            on_applied=lambda: applied.append("B"),
            on_failed=lambda _error: None,
        )
        release_persist_a.set()

        assert future_a.result(timeout=1) is True
        assert future_b.result(timeout=1) is True
        assert persisted == ["A", "B"]
        assert applied == ["A", "B"]
        assert coordinator.active_service.bindings == "B"
        assert factory.max_active_count == 1
    finally:
        release_persist_a.set()
        coordinator.stop()


def test_hotkey_change_restores_old_service_when_persistence_fails() -> None:
    factory = _FakeHotkeyFactory()
    errors: list[BaseException] = []
    coordinator = _hotkey_coordinator(factory)

    def fail_persist() -> None:
        raise RuntimeError("disk full")

    try:
        future = coordinator.request_change(
            "B",
            persist=fail_persist,
            on_applied=lambda: pytest.fail("failed config must not apply"),
            on_failed=errors.append,
        )

        assert future.result(timeout=1) is False
        assert [service.bindings for service in factory.services] == ["old", "B", "old"]
        assert coordinator.active_service is factory.services[-1]
        assert factory.services[1].running is False
        assert factory.services[-1].running is True
        assert factory.max_active_count == 1
        assert len(errors) == 1
        assert "disk full" in str(errors[0])
    finally:
        coordinator.stop()


def test_hotkey_change_cancels_capture_after_gate_retirement_before_stop() -> None:
    factory = _FakeHotkeyFactory()
    coordinator = _hotkey_coordinator(factory)
    factory.events.clear()
    try:
        future = coordinator.request_change(
            "B",
            before_replace=lambda: factory.events.append("cancel"),
            persist=lambda: factory.events.append("persist"),
            on_applied=lambda: None,
            on_failed=lambda error: pytest.fail(str(error)),
        )

        assert future.result(timeout=1) is True
        assert factory.events == ["cancel", "stop:old", "start:B", "persist"]
        assert factory.max_active_count == 1
    finally:
        coordinator.stop()


def test_hotkey_change_restores_old_service_when_candidate_start_fails() -> None:
    factory = _FakeHotkeyFactory()
    factory.start_failures.add("B")
    persisted: list[str] = []
    errors: list[BaseException] = []
    coordinator = _hotkey_coordinator(factory)
    try:
        future = coordinator.request_change(
            "B",
            persist=lambda: persisted.append("B"),
            on_applied=lambda: pytest.fail("failed hook must not apply"),
            on_failed=errors.append,
        )

        assert future.result(timeout=1) is False
        assert persisted == []
        assert [service.bindings for service in factory.services] == ["old", "B", "old"]
        assert coordinator.active_service is factory.services[-1]
        assert factory.max_active_count == 1
        assert len(errors) == 1
        assert "cannot start B" in str(errors[0])
    finally:
        coordinator.stop()


def test_superseded_change_rolls_back_to_latest_committed_bindings() -> None:
    factory = _FakeHotkeyFactory()
    block_b = _HotkeyBlock()
    factory.start_blocks["B"] = block_b
    persisted: list[str] = []
    errors: list[BaseException] = []
    coordinator = _hotkey_coordinator(factory)
    try:
        committed = coordinator.request_change(
            "A",
            persist=lambda: persisted.append("A"),
            on_applied=lambda: None,
            on_failed=errors.append,
        )
        assert committed.result(timeout=1) is True
        assert coordinator.active_service.bindings == "A"

        superseded = coordinator.request_change(
            "B",
            persist=lambda: persisted.append("B"),
            on_applied=lambda: pytest.fail("superseded config must not apply"),
            on_failed=errors.append,
        )
        assert block_b.entered.wait(timeout=1)

        factory.start_failures.add("C")
        failed = coordinator.request_change(
            "C",
            persist=lambda: persisted.append("C"),
            on_applied=lambda: pytest.fail("failed config must not apply"),
            on_failed=errors.append,
        )
        block_b.release.set()

        assert superseded.result(timeout=1) is False
        assert failed.result(timeout=1) is False
        assert persisted == ["A"]
        assert [service.bindings for service in factory.services] == [
            "old",
            "A",
            "B",
            "C",
            "A",
        ]
        assert coordinator.active_service is factory.services[-1]
        assert coordinator.active_service.bindings == "A"
        assert len(errors) == 1
        assert "cannot start C" in str(errors[0])
        assert factory.max_active_count == 1
    finally:
        block_b.release.set()
        coordinator.stop()


def test_hotkey_change_does_not_start_replacement_after_old_stop_timeout() -> None:
    factory = _FakeHotkeyFactory()
    factory.stop_failures.add("old")
    persisted: list[str] = []
    errors: list[BaseException] = []
    coordinator = _hotkey_coordinator(factory)
    try:
        future = coordinator.request_change(
            "B",
            persist=lambda: persisted.append("B"),
            on_applied=lambda: pytest.fail("timed-out stop must not apply"),
            on_failed=errors.append,
        )

        assert future.result(timeout=1) is False
        assert persisted == []
        assert [service.bindings for service in factory.services] == ["old"]
        assert factory.services[0].running is True
        assert coordinator.active_service is None
        assert len(errors) == 1
        assert "не завершилась вовремя" in str(errors[0])
    finally:
        factory.stop_failures.clear()
        coordinator.stop()


def test_hotkey_shutdown_stops_candidate_blocked_in_start() -> None:
    factory = _FakeHotkeyFactory()
    block_b = _HotkeyBlock()
    factory.start_blocks["B"] = block_b
    persisted: list[str] = []
    coordinator = _hotkey_coordinator(factory)

    future = coordinator.request_change(
        "B",
        persist=lambda: persisted.append("B"),
        on_applied=lambda: None,
        on_failed=lambda _error: None,
    )
    assert block_b.entered.wait(timeout=1)

    assert coordinator.stop(timeout_s=1) is True
    assert future.result(timeout=1) is False
    assert persisted == []
    assert all(not service.running for service in factory.services)
    assert factory.services[-1].stop_calls >= 1


def test_hotkey_shutdown_serializes_with_blocked_old_stop() -> None:
    factory = _FakeHotkeyFactory()
    old_stop = _HotkeyBlock()
    factory.stop_blocks["old"] = old_stop
    persisted: list[str] = []
    coordinator = _hotkey_coordinator(factory)
    future = coordinator.request_change(
        "B",
        persist=lambda: persisted.append("B"),
        on_applied=lambda: None,
        on_failed=lambda _error: None,
    )
    assert old_stop.entered.wait(timeout=1)
    result: list[bool] = []
    shutdown = threading.Thread(
        target=lambda: result.append(coordinator.stop(timeout_s=1)),
        daemon=True,
    )
    shutdown.start()
    deadline = time.monotonic() + 1
    while factory.services[0].stop_calls < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    old_stop.release.set()
    shutdown.join(timeout=1)

    assert result == [True]
    assert future.result(timeout=1) is False
    assert persisted == []
    assert [service.bindings for service in factory.services] == ["old"]
    assert factory.active_count == 0


def test_retired_hotkey_generation_cannot_emit_callbacks() -> None:
    factory = _FakeHotkeyFactory()
    block_b = _HotkeyBlock()
    factory.start_blocks["B"] = block_b
    received: list[str] = []
    coordinator = _hotkey_coordinator(factory, received)
    old = factory.services[0]
    try:
        old.fire("before")
        future = coordinator.request_change(
            "B",
            persist=lambda: None,
            on_applied=lambda: None,
            on_failed=lambda _error: None,
        )
        assert block_b.entered.wait(timeout=1)
        old.fire("retired")
        block_b.release.set()
        assert future.result(timeout=1) is True
        current = coordinator.active_service
        old.fire("late")
        current.fire("current")

        assert received == ["before", "current"]
        assert old.retire_calls >= 1
    finally:
        block_b.release.set()
        coordinator.stop()


def test_queued_ui_callback_rechecks_generation_after_retirement() -> None:
    queued: list[Callable[[], None]] = []
    received: list[str] = []

    def callback(action: str, still_current) -> None:
        queued.append(
            lambda: received.append(action) if still_current() else None
        )

    gate = _HotkeyCallbackGate(callback)
    gate.activate()
    gate("queued")
    gate.retire()

    queued[0]()
    assert received == []
