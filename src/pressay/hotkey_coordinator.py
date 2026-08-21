"""Serialized Windows hotkey replacement for the desktop application."""

from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Callable


LOGGER = logging.getLogger(__name__)


class _HotkeyCallbackGate:
    """Retire one hook generation without racing an in-flight callback."""

    def __init__(
        self,
        callback: Callable[[Any, Callable[[], bool]], Any],
    ) -> None:
        self._callback = callback
        self._active = False
        self._lock = threading.RLock()

    def activate(self) -> None:
        with self._lock:
            self._active = True

    def retire(self) -> None:
        with self._lock:
            self._active = False

    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def __call__(self, action: Any) -> Any:
        with self._lock:
            if not self._active:
                return None
            return self._callback(action, self.is_active)


@dataclass(slots=True)
class _ManagedHotkeyService:
    service: Any
    gate: _HotkeyCallbackGate
    bindings: Any


class _WindowsHotkeyCoordinator:
    """Serialize hook replacement, persistence and process shutdown."""

    def __init__(
        self,
        service_type: Any,
        callback: Callable[[Any, Callable[[], bool]], Any],
        dispatch_ui: Callable[..., None],
        initial_bindings: Any,
    ) -> None:
        self._service_type = service_type
        self._callback = callback
        self._dispatch_ui = dispatch_ui
        self._initial_bindings = initial_bindings
        self._committed_bindings = initial_bindings
        self._lock = threading.RLock()
        self._generation = 0
        self._closed = False
        self._active: _ManagedHotkeyService | None = None
        self._candidate: _ManagedHotkeyService | None = None
        self._retiring: list[_ManagedHotkeyService] = []
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="pressay-hotkey-swap",
        )
        self._futures: set[Future[bool]] = set()
        self._started = False

    @property
    def active_service(self) -> Any | None:
        with self._lock:
            return None if self._active is None else self._active.service

    def _new_service(self, bindings: Any) -> _ManagedHotkeyService:
        gate = _HotkeyCallbackGate(self._callback)
        service = self._service_type(gate, bindings=bindings)
        return _ManagedHotkeyService(service, gate, bindings)

    @staticmethod
    def _retire_service(
        entry: _ManagedHotkeyService,
        *,
        timeout_s: float,
    ) -> bool:
        entry.gate.retire()
        retire = getattr(entry.service, "retire_callbacks", None)
        if retire is not None:
            retire()
        result = entry.service.stop(timeout_s=max(0.0, timeout_s))
        return result is not False

    def start(self) -> None:
        entry = self._new_service(self._initial_bindings)
        with self._lock:
            if self._closed:
                raise RuntimeError("hotkey coordinator is closed")
            self._candidate = entry
        try:
            entry.service.start()
        except BaseException:
            try:
                if not self._retire_service(entry, timeout_s=3.0):
                    with self._lock:
                        self._retiring.append(entry)
            finally:
                with self._lock:
                    if self._candidate is entry:
                        self._candidate = None
            raise

        with self._lock:
            if self._closed:
                publish = False
            else:
                self._active = entry
                self._candidate = None
                entry.gate.activate()
                publish = True
                self._started = True
        if not publish:
            self._retire_service(entry, timeout_s=3.0)
            raise RuntimeError("hotkey coordinator closed during startup")

    def request_change(
        self,
        bindings: Any,
        *,
        before_replace: Callable[[], Any] | None = None,
        persist: Callable[[], None],
        on_applied: Callable[[], None],
        on_failed: Callable[[BaseException], None],
    ) -> Future[bool]:
        with self._lock:
            if self._closed or not self._started:
                error = RuntimeError("hotkey coordinator is not running")
                self._dispatch_ui(on_failed, error)
                future: Future[bool] = Future()
                future.set_result(False)
                return future
            self._generation += 1
            generation = self._generation
            future = self._executor.submit(
                self._apply_change,
                generation,
                bindings,
                before_replace,
                persist,
                on_applied,
                on_failed,
            )
            self._futures.add(future)
            future.add_done_callback(self._forget_future)
            return future

    def _forget_future(self, future: Future[bool]) -> None:
        with self._lock:
            self._futures.discard(future)

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return not self._closed and generation == self._generation

    def _remember_retiring(self, entry: _ManagedHotkeyService) -> None:
        with self._lock:
            if all(existing is not entry for existing in self._retiring):
                self._retiring.append(entry)

    def _clear_finished_retiring(self) -> bool:
        with self._lock:
            retiring = tuple(self._retiring)
        remaining: list[_ManagedHotkeyService] = []
        for entry in retiring:
            try:
                stopped = self._retire_service(entry, timeout_s=0.0)
            except Exception:
                LOGGER.exception("hotkey_retired_service_stop_failed")
                stopped = False
            if not stopped:
                remaining.append(entry)
        with self._lock:
            self._retiring = remaining
        return not remaining

    def _stop_entry(
        self,
        entry: _ManagedHotkeyService,
        *,
        timeout_s: float = 3.0,
    ) -> bool:
        try:
            stopped = self._retire_service(entry, timeout_s=timeout_s)
        except Exception:
            LOGGER.exception("hotkey_service_stop_failed")
            stopped = False
        if not stopped:
            self._remember_retiring(entry)
        return stopped

    def _notify_failure(
        self,
        generation: int,
        callback: Callable[[BaseException], None],
        error: BaseException,
    ) -> None:
        with self._lock:
            notify = not self._closed and generation == self._generation
        if notify:
            self._dispatch_ui(callback, error)

    def _restore_bindings(
        self,
        generation: int,
        bindings: Any,
    ) -> BaseException | None:
        if not self._is_current(generation):
            return None
        if not self._clear_finished_retiring():
            return RuntimeError("предыдущая служба горячих клавиш ещё завершается")
        rollback = self._new_service(bindings)
        with self._lock:
            if self._closed or generation != self._generation:
                should_start = False
            else:
                self._candidate = rollback
                should_start = True
        if not should_start:
            return None
        try:
            rollback.service.start()
        except BaseException as exc:
            self._stop_entry(rollback)
            with self._lock:
                if self._candidate is rollback:
                    self._candidate = None
            return exc
        with self._lock:
            if self._closed or generation != self._generation:
                publish = False
            else:
                self._candidate = None
                self._active = rollback
                rollback.gate.activate()
                publish = True
        if not publish:
            self._stop_entry(rollback)
        return None

    def _apply_change(
        self,
        generation: int,
        bindings: Any,
        before_replace: Callable[[], Any] | None,
        persist: Callable[[], None],
        on_applied: Callable[[], None],
        on_failed: Callable[[BaseException], None],
    ) -> bool:
        if not self._is_current(generation):
            return False
        if not self._clear_finished_retiring():
            self._notify_failure(
                generation,
                on_failed,
                RuntimeError("предыдущая служба горячих клавиш ещё завершается"),
            )
            return False

        with self._lock:
            previous = self._active
            previous_bindings = self._committed_bindings
        replacement: _ManagedHotkeyService | None = None
        runtime_changed = False

        if previous is not None and previous.bindings != bindings:
            try:
                previous.gate.retire()
                previous.service.retire_callbacks()
                if before_replace is not None:
                    before_replace()
            except BaseException as exc:
                stopped = self._stop_entry(previous)
                with self._lock:
                    if self._active is previous:
                        self._active = None
                if stopped:
                    rollback_error = self._restore_bindings(
                        generation, previous_bindings
                    )
                    if rollback_error is not None:
                        exc = RuntimeError(
                            f"{exc}; не удалось восстановить прежние клавиши: "
                            f"{rollback_error}"
                        )
                self._notify_failure(generation, on_failed, exc)
                return False
            if not self._stop_entry(previous):
                with self._lock:
                    if self._active is previous:
                        self._active = None
                self._notify_failure(
                    generation,
                    on_failed,
                    RuntimeError("служба горячих клавиш не завершилась вовремя"),
                )
                return False
            with self._lock:
                if self._active is previous:
                    self._active = None
            runtime_changed = True

        if not self._is_current(generation):
            return False

        with self._lock:
            active = self._active
        if active is None or active.bindings != bindings:
            try:
                replacement = self._new_service(bindings)
            except BaseException as exc:
                rollback_error = self._restore_bindings(
                    generation, previous_bindings
                )
                if rollback_error is not None:
                    exc = RuntimeError(
                        f"{exc}; не удалось восстановить прежние клавиши: "
                        f"{rollback_error}"
                    )
                self._notify_failure(generation, on_failed, exc)
                return False
            with self._lock:
                if self._closed or generation != self._generation:
                    should_start = False
                else:
                    self._candidate = replacement
                    should_start = True
            if not should_start:
                return False
            try:
                replacement.service.start()
            except BaseException as exc:
                self._stop_entry(replacement)
                with self._lock:
                    if self._candidate is replacement:
                        self._candidate = None
                rollback_error = self._restore_bindings(
                    generation, previous_bindings
                )
                if rollback_error is not None:
                    exc = RuntimeError(
                        f"{exc}; не удалось восстановить прежние клавиши: "
                        f"{rollback_error}"
                    )
                self._notify_failure(generation, on_failed, exc)
                return False

            if not self._is_current(generation):
                self._stop_entry(replacement)
                with self._lock:
                    if self._candidate is replacement:
                        self._candidate = None
                return False
            runtime_changed = True

        try:
            persist()
        except BaseException as exc:
            if replacement is not None:
                stopped = self._stop_entry(replacement)
                with self._lock:
                    if self._candidate is replacement:
                        self._candidate = None
                if stopped:
                    rollback_error = self._restore_bindings(
                        generation, previous_bindings
                    )
                    if rollback_error is not None:
                        exc = RuntimeError(
                            f"{exc}; не удалось восстановить прежние клавиши: "
                            f"{rollback_error}"
                        )
            elif runtime_changed:
                rollback_error = self._restore_bindings(
                    generation, previous_bindings
                )
                if rollback_error is not None:
                    exc = RuntimeError(
                        f"{exc}; не удалось восстановить прежние клавиши: "
                        f"{rollback_error}"
                    )
            self._notify_failure(generation, on_failed, exc)
            return False

        if replacement is not None:
            with self._lock:
                if self._closed:
                    publish = False
                else:
                    self._candidate = None
                    self._active = replacement
                    replacement.gate.activate()
                    publish = True
            if not publish:
                self._stop_entry(replacement)
                return False
        with self._lock:
            notify = not self._closed
            if notify:
                self._committed_bindings = bindings
        if notify:
            self._dispatch_ui(on_applied)
        return True

    def stop(self, timeout_s: float = 3.0) -> bool:
        """Invalidate all generations and stop active/candidate hooks."""

        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._lock:
            self._closed = True
            self._generation += 1
            entries = tuple(
                entry
                for entry in (self._active, self._candidate, *self._retiring)
                if entry is not None
            )
            futures = tuple(self._futures)
        self._executor.shutdown(wait=False, cancel_futures=True)

        service_status: dict[int, bool] = {}
        for entry in entries:
            identity = id(entry.service)
            if identity in service_status:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            service_status[identity] = self._stop_entry(
                entry, timeout_s=remaining
            )

        futures_complete = True
        for future in futures:
            try:
                future.result(timeout=max(0.0, deadline - time.monotonic()))
            except CancelledError:
                pass
            except TimeoutError:
                futures_complete = False
            except BaseException:
                LOGGER.exception("hotkey_change_shutdown_failed")

        with self._lock:
            latest_entries = tuple(
                entry
                for entry in (self._active, self._candidate, *self._retiring)
                if entry is not None
            )
        for entry in latest_entries:
            identity = id(entry.service)
            if service_status.get(identity, False):
                continue
            remaining = max(0.0, deadline - time.monotonic())
            service_status[identity] = self._stop_entry(
                entry, timeout_s=remaining
            )

        futures_complete = futures_complete or all(
            future.done() for future in futures
        )
        all_stopped = futures_complete and all(service_status.values())
        if all_stopped:
            with self._lock:
                self._active = None
                self._candidate = None
                self._retiring.clear()
        return all_stopped


__all__ = ["_HotkeyCallbackGate", "_WindowsHotkeyCoordinator"]
