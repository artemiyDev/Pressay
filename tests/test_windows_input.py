from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
import threading
import time
from types import SimpleNamespace

import pytest

from pressay.windows_input import (
    _FOCUS_UNAVAILABLE,
    _UIA_REFETCH_DELAY_S,
    _UIAFingerprintWorker,
    _load_win32_api,
    _native_focused_control_fingerprint,
    ClipboardReplaceError,
    ForegroundTarget,
    FocusFingerprint,
    InputStatus,
    Win32Clipboard,
    Win32InputBackend,
    clipboard_paste_transaction,
    copy_last,
    copy_text,
    describe_focus,
    parse_focus_fingerprint,
    paste_last,
    send_text,
    target_looks_editable,
    targets_match,
    utf16_code_units,
    wait_for_physical_modifiers_clear,
)


def _uia_fingerprint(
    *,
    process_id: int = 200,
    runtime_id: tuple[int, ...] = (1, 2),
    automation_id: str = "field",
    class_name: str = "Edit",
    control_type: int = 50004,
    enabled: bool = True,
    keyboard_focusable: bool = True,
    value_writable: bool = True,
    text_editable: bool = False,
) -> tuple[object, ...]:
    return (
        "uia",
        process_id,
        *runtime_id,
        automation_id,
        class_name,
        control_type,
        enabled,
        keyboard_focusable,
        value_writable,
        text_editable,
    )


TARGET = ForegroundTarget(
    hwnd=100,
    pid=200,
    title="Editor",
    focused_control=_uia_fingerprint(),
    captured_at=1.0,
)
OTHER_TARGET = ForegroundTarget(
    hwnd=101,
    pid=201,
    title="Other",
    focused_control=_uia_fingerprint(process_id=201),
    captured_at=2.0,
)


def test_real_win32_input_structure_has_complete_union() -> None:
    if os.name != "nt":
        pytest.skip("Windows ABI check")
    from pressay.windows_input import _load_win32_api

    api = _load_win32_api()
    expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(api.INPUT) == expected_size


def test_target_guard_rejects_focus_change_inside_same_window() -> None:
    first = ForegroundTarget(
        hwnd=100,
        pid=200,
        title="Editor",
        focused_control=_uia_fingerprint(automation_id="field-a", runtime_id=(1, 2)),
    )
    second = ForegroundTarget(
        hwnd=100,
        pid=200,
        title="Editor",
        focused_control=_uia_fingerprint(automation_id="field-b", runtime_id=(1, 3)),
    )

    assert targets_match(first, second) is False


def test_unavailable_focus_is_always_fail_closed() -> None:
    unavailable = ForegroundTarget(
        hwnd=100,
        pid=200,
        title="Editor",
        focused_control=_FOCUS_UNAVAILABLE,
    )

    assert targets_match(unavailable, unavailable) is False


class _FakeUIAControl:
    ProcessId = 200
    AutomationId = "field"
    ClassName = "Edit"
    ControlType = 50004
    IsEnabled = True
    IsKeyboardFocusable = True

    @staticmethod
    def GetRuntimeId() -> tuple[int, int]:
        return (7, 11)

    @staticmethod
    def GetPattern(pattern_id: int) -> object | None:
        if pattern_id == 10002:
            return SimpleNamespace(IsReadOnly=False)
        return None


class _FakeUIAutomation:
    PatternId = SimpleNamespace(ValuePattern=10002, TextEditPattern=10032)

    def __init__(self, *, block: bool = False) -> None:
        self.loader_thread: int | None = None
        self.query_threads: list[int] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = block

    def InitializeUIAutomationInCurrentThread(self) -> None:
        self.loader_thread = threading.get_ident()

    def GetFocusedControl(self) -> _FakeUIAControl:
        self.query_threads.append(threading.get_ident())
        self.entered.set()
        if self.block:
            assert self.release.wait(timeout=2)
        return _FakeUIAControl()


def _configurable_uia_control(
    *,
    automation_id: str,
    control_type: int,
    keyboard_focusable: bool = False,
    value_writable: bool = False,
    text_editable: bool = False,
    caret_active: bool = False,
) -> SimpleNamespace:
    def get_pattern(pattern_id: int) -> object | None:
        if pattern_id == 10002 and value_writable:
            return SimpleNamespace(IsReadOnly=False)
        if pattern_id == 10032 and text_editable:
            return object()
        if pattern_id == 10024 and caret_active:
            return SimpleNamespace(GetCaretRange=lambda: (True, object()))
        return None

    return SimpleNamespace(
        ProcessId=200,
        AutomationId=automation_id,
        ClassName="Edit" if control_type == 50004 else "Pane",
        ControlType=control_type,
        IsEnabled=True,
        IsKeyboardFocusable=keyboard_focusable,
        GetRuntimeId=lambda: (7, 11),
        GetPattern=get_pattern,
    )


class _SequenceUIAutomation:
    PatternId = SimpleNamespace(ValuePattern=10002, TextEditPattern=10032)

    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.calls = 0

    def GetFocusedControl(self) -> object:
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_uia_refetch_replaces_coarse_pane_with_informative_edit() -> None:
    automation = _SequenceUIAutomation(
        _configurable_uia_control(automation_id="coarse", control_type=50033),
        _configurable_uia_control(
            automation_id="editor",
            control_type=50004,
            keyboard_focusable=True,
            value_writable=True,
        ),
    )
    delays: list[float] = []

    fingerprint = _UIAFingerprintWorker._read_fingerprint(
        automation, 200, sleeper=delays.append
    )
    target = ForegroundTarget(hwnd=100, pid=200, focused_control=fingerprint)

    assert automation.calls == 2
    assert delays == [_UIA_REFETCH_DELAY_S]
    assert describe_focus(target)["control_type"] == 50004
    assert describe_focus(target)["refetched"] is True
    assert target_looks_editable(target) is True


def test_uia_informative_first_read_skips_refetch_and_delay() -> None:
    automation = _SequenceUIAutomation(
        _configurable_uia_control(
            automation_id="editor",
            control_type=50004,
            keyboard_focusable=True,
            value_writable=True,
        )
    )
    delays: list[float] = []

    fingerprint = _UIAFingerprintWorker._read_fingerprint(
        automation, 200, sleeper=delays.append
    )

    assert automation.calls == 1
    assert delays == []
    assert describe_focus(
        ForegroundTarget(hwnd=100, pid=200, focused_control=fingerprint)
    )["refetched"] is False


def test_uia_refetch_keeps_first_when_both_reads_are_uninformative() -> None:
    automation = _SequenceUIAutomation(
        _configurable_uia_control(automation_id="first", control_type=50033),
        _configurable_uia_control(automation_id="second", control_type=50033),
    )

    fingerprint = _UIAFingerprintWorker._read_fingerprint(
        automation, 200, sleeper=lambda _delay: None
    )
    target = ForegroundTarget(hwnd=100, pid=200, focused_control=fingerprint)

    assert fingerprint[4] == "first"
    assert describe_focus(target)["refetched"] is True
    assert target_looks_editable(target) is False


@pytest.mark.parametrize(
    ("value_writable", "text_editable", "caret_active"),
    ((True, False, False), (False, True, False), (False, False, True)),
)
def test_uia_positive_evidence_skips_refetch(
    value_writable: bool, text_editable: bool, caret_active: bool
) -> None:
    automation = _SequenceUIAutomation(
        _configurable_uia_control(
            automation_id="editor",
            control_type=50033,
            value_writable=value_writable,
            text_editable=text_editable,
            caret_active=caret_active,
        )
    )

    fingerprint = _UIAFingerprintWorker._read_fingerprint(
        automation, 200, sleeper=lambda _delay: pytest.fail("unexpected refetch")
    )

    assert automation.calls == 1
    assert describe_focus(
        ForegroundTarget(hwnd=100, pid=200, focused_control=fingerprint)
    )["refetched"] is False


def test_uia_refetch_error_keeps_first_fingerprint() -> None:
    automation = _SequenceUIAutomation(
        _configurable_uia_control(automation_id="first", control_type=50033),
        RuntimeError("provider failed"),
    )

    fingerprint = _UIAFingerprintWorker._read_fingerprint(
        automation, 200, sleeper=lambda _delay: None
    )

    assert fingerprint[4] == "first"
    assert describe_focus(
        ForegroundTarget(hwnd=100, pid=200, focused_control=fingerprint)
    )["refetched"] is True


def test_uia_refetch_unavailable_result_keeps_first_fingerprint() -> None:
    unavailable = _configurable_uia_control(
        automation_id="wrong-process", control_type=50004
    )
    unavailable.ProcessId = 201
    automation = _SequenceUIAutomation(
        _configurable_uia_control(automation_id="first", control_type=50033),
        unavailable,
    )

    fingerprint = _UIAFingerprintWorker._read_fingerprint(
        automation, 200, sleeper=lambda _delay: None
    )

    assert fingerprint[4] == "first"
    assert describe_focus(
        ForegroundTarget(hwnd=100, pid=200, focused_control=fingerprint)
    )["refetched"] is True


def test_target_match_ignores_refetch_diagnostic_metadata() -> None:
    control = _configurable_uia_control(
        automation_id="editor",
        control_type=50004,
        keyboard_focusable=True,
        value_writable=True,
    )
    refetched = _UIAFingerprintWorker._read_fingerprint(
        _SequenceUIAutomation(
            _configurable_uia_control(automation_id="coarse", control_type=50033),
            control,
        ),
        200,
        sleeper=lambda _delay: None,
    )
    direct = _UIAFingerprintWorker._read_fingerprint(
        _SequenceUIAutomation(control),
        200,
        sleeper=lambda _delay: pytest.fail("unexpected refetch"),
    )

    assert targets_match(
        ForegroundTarget(hwnd=100, pid=200, focused_control=refetched),
        ForegroundTarget(hwnd=100, pid=200, focused_control=direct),
    ) is True


def test_uia_import_and_all_queries_are_owned_by_one_worker_thread() -> None:
    automation = _FakeUIAutomation()
    loader_threads: list[int] = []

    def loader() -> _FakeUIAutomation:
        loader_threads.append(threading.get_ident())
        return automation

    worker = _UIAFingerprintWorker(loader=loader, default_timeout_s=0.2)
    first = worker.query(200)
    results: list[tuple[object, ...]] = []
    caller = threading.Thread(target=lambda: results.append(worker.query(200)))
    caller.start()
    caller.join(timeout=1)

    assert not caller.is_alive()
    assert first == results[0]
    assert first[0] == "uia"
    assert len(loader_threads) == 1
    assert automation.loader_thread == loader_threads[0]
    assert automation.query_threads == [loader_threads[0], loader_threads[0]]
    assert loader_threads[0] != threading.get_ident()


def test_uia_worker_allows_only_one_outstanding_query() -> None:
    automation = _FakeUIAutomation(block=True)
    worker = _UIAFingerprintWorker(loader=lambda: automation, default_timeout_s=0.5)
    first: list[tuple[object, ...]] = []
    caller = threading.Thread(target=lambda: first.append(worker.query(200)))
    caller.start()
    assert automation.entered.wait(timeout=1)

    started = time.monotonic()
    second = worker.query(200)
    elapsed = time.monotonic() - started
    automation.release.set()
    caller.join(timeout=1)

    assert second == _FOCUS_UNAVAILABLE
    assert elapsed < 0.1
    assert not caller.is_alive()
    assert first and first[0][0] == "uia"
    assert len(automation.query_threads) == 1


def test_uia_timeout_blocks_new_calls_then_recovers_after_provider_returns() -> None:
    automation = _FakeUIAutomation(block=True)
    worker = _UIAFingerprintWorker(loader=lambda: automation, default_timeout_s=0.04)

    first = worker.query(200)
    started = time.monotonic()
    second = worker.query(200)
    elapsed = time.monotonic() - started
    automation.release.set()

    deadline = time.monotonic() + 1
    while worker.unhealthy and time.monotonic() < deadline:
        time.sleep(0.005)
    recovered = worker.query(200)

    assert first == _FOCUS_UNAVAILABLE
    assert second == _FOCUS_UNAVAILABLE
    assert worker.unhealthy is False
    assert recovered[0] == "uia"
    assert elapsed < 0.02
    assert len(automation.query_threads) == 2


def test_native_child_hwnd_focus_avoids_uia(monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows focus API check")
    import pressay.windows_input as windows_input

    real_api = _load_win32_api()

    class FakeUser32:
        @staticmethod
        def GetForegroundWindow() -> int:
            return 100

        @staticmethod
        def GetWindowThreadProcessId(hwnd: int, process_id_pointer: object) -> int:
            process_id = ctypes.cast(
                process_id_pointer,
                ctypes.POINTER(real_api.wintypes.DWORD),
            )
            process_id.contents.value = 200
            return 300

        @staticmethod
        def GetWindowTextLengthW(_hwnd: int) -> int:
            return len("Editor")

        @staticmethod
        def GetWindowTextW(_hwnd: int, buffer: object, _length: int) -> int:
            buffer.value = "Editor"
            return len(buffer.value)

        @staticmethod
        def GetGUIThreadInfo(_thread_id: int, info_pointer: object) -> bool:
            info = ctypes.cast(
                info_pointer,
                ctypes.POINTER(real_api.GUITHREADINFO),
            )
            info.contents.hwndFocus = 222
            return True

        @staticmethod
        def GetAncestor(_hwnd: int, _flags: int) -> int:
            return 100

        @staticmethod
        def GetClassNameW(_hwnd: int, buffer: object, _length: int) -> int:
            buffer.value = "Edit"
            return len(buffer.value)

        @staticmethod
        def GetWindowLongW(_hwnd: int, _index: int) -> int:
            return 0

    fake_api = SimpleNamespace(
        ctypes=ctypes,
        wintypes=real_api.wintypes,
        GUITHREADINFO=real_api.GUITHREADINFO,
        user32=FakeUser32(),
    )
    backend = object.__new__(Win32InputBackend)
    backend._api = fake_api
    monkeypatch.setattr(
        windows_input,
        "_uia_focused_control_fingerprint",
        lambda _process_id: pytest.fail("native child focus must not query UIA"),
    )

    target = backend.snapshot_foreground_target()

    assert target.hwnd == 100
    assert target.pid == 200
    assert target.focused_control == ("win32_focus", 200, 222, "Edit")


def test_native_button_focus_is_rejected_before_text_or_enter() -> None:
    if os.name != "nt":
        pytest.skip("Windows focus API check")
    real_api = _load_win32_api()

    class FakeUser32:
        @staticmethod
        def GetGUIThreadInfo(_thread_id: int, info_pointer: object) -> bool:
            info = ctypes.cast(
                info_pointer,
                ctypes.POINTER(real_api.GUITHREADINFO),
            )
            info.contents.hwndFocus = 222
            return True

        @staticmethod
        def GetWindowThreadProcessId(_hwnd: int, process_id_pointer: object) -> int:
            process_id = ctypes.cast(
                process_id_pointer,
                ctypes.POINTER(real_api.wintypes.DWORD),
            )
            process_id.contents.value = 200
            return 300

        @staticmethod
        def GetAncestor(_hwnd: int, _flags: int) -> int:
            return 100

        @staticmethod
        def GetClassNameW(_hwnd: int, buffer: object, _length: int) -> int:
            buffer.value = "Button"
            return len(buffer.value)

        @staticmethod
        def GetWindowLongW(_hwnd: int, _index: int) -> int:
            return 0

    api = SimpleNamespace(
        ctypes=ctypes,
        wintypes=real_api.wintypes,
        GUITHREADINFO=real_api.GUITHREADINFO,
        user32=FakeUser32(),
    )
    fingerprint = _native_focused_control_fingerprint(
        api,
        foreground_hwnd=100,
        foreground_thread_id=300,
        process_id=200,
    )
    assert fingerprint == _FOCUS_UNAVAILABLE
    target = ForegroundTarget(hwnd=100, pid=200, focused_control=fingerprint)
    backend = FakeBackend([target])
    clipboard = FakeClipboard("prior")

    outcome = send_text(
        "dictated",
        expected_target=target,
        backend=backend,
        clipboard=clipboard,
        press_enter=True,
        fallback_to_clipboard=False,
    )
    paste_outcome = paste_last(
        "remembered",
        expected_target=target,
        backend=backend,
        clipboard=clipboard,
        clipboard_settle_s=0,
    )

    assert outcome.reason == "focused_control_is_not_editable"
    assert paste_outcome.reason == "focused_control_is_not_editable"
    assert backend.unicode_batches == []
    assert backend.ctrl_v_calls == 0
    assert backend.enter_calls == 0
    assert clipboard.writes == []


def test_native_read_only_edit_is_rejected() -> None:
    if os.name != "nt":
        pytest.skip("Windows focus API check")
    real_api = _load_win32_api()

    class FakeUser32:
        @staticmethod
        def GetGUIThreadInfo(_thread_id: int, info_pointer: object) -> bool:
            info = ctypes.cast(
                info_pointer,
                ctypes.POINTER(real_api.GUITHREADINFO),
            )
            info.contents.hwndFocus = 222
            return True

        @staticmethod
        def GetWindowThreadProcessId(_hwnd: int, process_id_pointer: object) -> int:
            process_id = ctypes.cast(
                process_id_pointer,
                ctypes.POINTER(real_api.wintypes.DWORD),
            )
            process_id.contents.value = 200
            return 300

        @staticmethod
        def GetAncestor(_hwnd: int, _flags: int) -> int:
            return 100

        @staticmethod
        def GetClassNameW(_hwnd: int, buffer: object, _length: int) -> int:
            buffer.value = "Edit"
            return len(buffer.value)

        @staticmethod
        def GetWindowLongW(_hwnd: int, _index: int) -> int:
            return 0x0800  # ES_READONLY

    api = SimpleNamespace(
        ctypes=ctypes,
        wintypes=real_api.wintypes,
        GUITHREADINFO=real_api.GUITHREADINFO,
        user32=FakeUser32(),
    )

    fingerprint = _native_focused_control_fingerprint(
        api,
        foreground_hwnd=100,
        foreground_thread_id=300,
        process_id=200,
    )

    assert fingerprint == _FOCUS_UNAVAILABLE


def test_known_button_target_is_never_treated_as_successful_text_input() -> None:
    button = ForegroundTarget(
        hwnd=100,
        pid=200,
        title="Pressay",
        focused_control=_uia_fingerprint(
            automation_id="start",
            class_name="QPushButton",
            control_type=50000,
            value_writable=False,
        ),
    )
    backend = FakeBackend([button])
    clipboard = FakeClipboard()

    outcome = send_text("dictated", expected_target=button, backend=backend, clipboard=clipboard)

    assert outcome.success is False
    assert outcome.reason == "focused_control_is_not_editable"
    assert backend.unicode_batches == []


@pytest.mark.parametrize(
    ("control_type", "class_name"),
    ((50002, "CheckBox"), (50013, "RadioButton")),
)
def test_uia_non_text_controls_reject_text_and_enter(
    control_type: int,
    class_name: str,
) -> None:
    target = ForegroundTarget(
        hwnd=100,
        pid=200,
        focused_control=_uia_fingerprint(
            class_name=class_name,
            control_type=control_type,
            value_writable=False,
        ),
    )
    backend = FakeBackend([target])
    clipboard = FakeClipboard("prior")

    outcome = send_text(
        "dictated",
        expected_target=target,
        backend=backend,
        clipboard=clipboard,
        press_enter=True,
        fallback_to_clipboard=False,
    )
    paste_outcome = paste_last(
        "remembered",
        expected_target=target,
        backend=backend,
        clipboard=clipboard,
        clipboard_settle_s=0,
    )

    assert outcome.reason == "focused_control_is_not_editable"
    assert paste_outcome.reason == "focused_control_is_not_editable"
    assert backend.unicode_batches == []
    assert backend.ctrl_v_calls == 0
    assert backend.enter_calls == 0
    assert clipboard.writes == []


@pytest.mark.parametrize(
    ("control_type", "value_writable", "text_editable", "expected"),
    (
        (50004, True, False, True),   # Edit with writable ValuePattern
        (50030, False, True, True),   # Document with TextEditPattern
        (50025, False, True, True),   # Custom rich editor with TextEditPattern
        (50004, False, False, False), # Read-only Edit
        (50030, False, False, False), # Read-only Document
        (50025, False, False, False), # Arbitrary Custom control
        (50002, True, True, False),   # CheckBox despite misleading patterns
        (50013, True, True, False),   # RadioButton despite misleading patterns
    ),
)
def test_uia_editability_requires_text_control_and_writable_capability(
    control_type: int,
    value_writable: bool,
    text_editable: bool,
    expected: bool,
) -> None:
    target = ForegroundTarget(
        hwnd=100,
        pid=200,
        focused_control=_uia_fingerprint(
            control_type=control_type,
            value_writable=value_writable,
            text_editable=text_editable,
        ),
    )

    assert target_looks_editable(target) is expected


def test_parse_focus_fingerprint_rejects_missing_unavailable_and_unknown() -> None:
    assert parse_focus_fingerprint(None) is None
    assert parse_focus_fingerprint(_FOCUS_UNAVAILABLE) is None
    assert parse_focus_fingerprint(()) is None
    assert parse_focus_fingerprint(("something_else", 1)) is None
    assert parse_focus_fingerprint(_uia_fingerprint()[:9]) is None  # too short


def test_parse_focus_fingerprint_decodes_uia_format() -> None:
    fingerprint = _uia_fingerprint(
        process_id=321,
        class_name="RichEdit",
        control_type=50004,
        enabled=True,
        keyboard_focusable=False,
        value_writable=True,
        text_editable=False,
    )

    parsed = parse_focus_fingerprint(fingerprint)

    assert parsed == FocusFingerprint(
        kind="uia",
        process_id=321,
        control_type=50004,
        enabled=True,
        keyboard_focusable=False,
        value_writable=True,
        text_editable=False,
        class_name="RichEdit",
    )


def test_parse_focus_fingerprint_decodes_win32_focus_format() -> None:
    fingerprint = ("win32_focus", 200, 222, "Edit")

    parsed = parse_focus_fingerprint(fingerprint)

    assert parsed == FocusFingerprint(
        kind="win32_focus",
        process_id=200,
        class_name="Edit",
    )


def test_describe_focus_reports_none_for_missing_fingerprint() -> None:
    target = ForegroundTarget(hwnd=100, pid=200, focused_control=None)

    expected = {
        "focus_kind": "none",
        "control_type": None,
        "enabled": None,
        "keyboard_focusable": None,
        "value_writable": None,
        "text_editable": None,
        "caret_active": None,
        "win32_caret": None,
        "refetched": None,
    }
    assert describe_focus(target) == expected
    assert describe_focus(None) == expected


def test_describe_focus_reports_uia_control_type() -> None:
    target = ForegroundTarget(
        hwnd=100,
        pid=200,
        focused_control=_uia_fingerprint(control_type=50030),
    )

    assert describe_focus(target) == {
        "focus_kind": "uia",
        "control_type": 50030,
        "enabled": True,
        "keyboard_focusable": True,
        "value_writable": True,
        "text_editable": False,
        "caret_active": False,
        "win32_caret": False,
        "refetched": False,
    }


def test_describe_focus_reports_win32_focus_without_control_type() -> None:
    target = ForegroundTarget(
        hwnd=100,
        pid=200,
        focused_control=("win32_focus", 200, 222, "Edit"),
    )

    assert describe_focus(target) == {
        "focus_kind": "win32_focus",
        "control_type": None,
        "enabled": None,
        "keyboard_focusable": None,
        "value_writable": None,
        "text_editable": None,
        "caret_active": False,
        "win32_caret": False,
        "refetched": False,
    }


def test_describe_focus_keeps_reporting_the_unavailable_sentinel_tag() -> None:
    # The sentinel is a truthy, non-empty tuple; a failed UIA probe must keep
    # surfacing "focus_unavailable" in the log exactly as before this refactor.
    target = ForegroundTarget(hwnd=100, pid=200, focused_control=_FOCUS_UNAVAILABLE)

    assert describe_focus(target) == {
        "focus_kind": "focus_unavailable",
        "control_type": None,
        "enabled": None,
        "keyboard_focusable": None,
        "value_writable": None,
        "text_editable": None,
        "caret_active": None,
        "win32_caret": None,
        "refetched": None,
    }


@pytest.mark.parametrize("control_type", (50033, 50026))
@pytest.mark.parametrize("strict", (False, True))
def test_active_uia_caret_allows_non_toggle_controls_in_both_modes(
    control_type: int, strict: bool
) -> None:
    target = ForegroundTarget(
        hwnd=100,
        pid=200,
        focused_control=(
            "uia", 200, 1, 2, "field", "Custom", control_type,
            True, False, False, False, True, False,
        ),
    )

    assert target_looks_editable(target, strict=strict) is True


@pytest.mark.parametrize("control_type", (50002, 50013))
def test_active_uia_caret_does_not_allow_toggle_controls(control_type: int) -> None:
    target = ForegroundTarget(
        hwnd=100,
        pid=200,
        focused_control=(
            "uia", 200, 1, 2, "field", "Toggle", control_type,
            True, True, True, True, True, False,
        ),
    )

    assert target_looks_editable(target) is False
    assert target_looks_editable(target, strict=True) is False


@pytest.mark.parametrize("strict", (False, True))
def test_win32_caret_allows_text_in_both_modes(strict: bool) -> None:
    target = ForegroundTarget(
        hwnd=100,
        pid=200,
        focused_control=("win32_focus", 200, 222, "Custom", False, True),
    )

    assert target_looks_editable(target, strict=strict) is True


def test_parse_old_fingerprints_default_new_caret_evidence_to_false() -> None:
    uia = parse_focus_fingerprint(_uia_fingerprint())
    win32 = parse_focus_fingerprint(("win32_focus", 200, 222, "Edit"))

    assert uia is not None and (uia.caret_active, uia.win32_caret) == (False, False)
    assert win32 is not None and (win32.caret_active, win32.win32_caret) == (False, False)


def test_uia_fingerprint_records_active_textpattern2_caret() -> None:
    class Control(_FakeUIAControl):
        @staticmethod
        def GetPattern(pattern_id: int) -> object | None:
            if pattern_id == 10024:
                return SimpleNamespace(GetCaretRange=lambda: (True, object()))
            return _FakeUIAControl.GetPattern(pattern_id)

    class Automation(_FakeUIAutomation):
        def GetFocusedControl(self) -> Control:
            return Control()

    fingerprint = _UIAFingerprintWorker._read_fingerprint(Automation(), 200)

    assert fingerprint[-2:] == (True, False)


def test_press_enter_only_sends_enter_to_same_target() -> None:
    backend = FakeBackend()

    outcome = send_text("", expected_target=TARGET, backend=backend, press_enter=True)

    assert outcome.success is True
    assert outcome.method == "sendinput_enter"
    assert backend.enter_calls == 1
    assert backend.unicode_batches == []


def test_failed_press_enter_only_never_erases_clipboard() -> None:
    clipboard = FakeClipboard("IMPORTANT")
    backend = FakeBackend([OTHER_TARGET])

    outcome = send_text(
        "",
        expected_target=TARGET,
        backend=backend,
        clipboard=clipboard,
        press_enter=True,
    )

    assert outcome.success is False
    assert outcome.copied is False
    assert clipboard.text == "IMPORTANT"
    assert clipboard.writes == []


class FakeBackend:
    def __init__(self, targets: list[ForegroundTarget] | None = None) -> None:
        self.targets = list(targets or [TARGET])
        self.unicode_batches: list[tuple[int, ...]] = []
        self.ctrl_v_calls = 0
        self.enter_calls = 0
        self.modifiers_down = False
        self.send_unicode_result = True
        self.send_paste_result = True
        self.on_paste = None

    def snapshot_foreground_target(self) -> ForegroundTarget:
        if len(self.targets) > 1:
            return self.targets.pop(0)
        return self.targets[0]

    def is_physical_key_down(self, _vk_code: int) -> bool:
        return self.modifiers_down

    def send_unicode_units(self, units: tuple[int, ...]) -> bool:
        self.unicode_batches.append(tuple(units))
        return self.send_unicode_result

    def send_ctrl_v(self) -> bool:
        self.ctrl_v_calls += 1
        if self.on_paste is not None:
            self.on_paste()
        return self.send_paste_result

    def send_enter(self) -> bool:
        self.enter_calls += 1
        return True


class BlockingSnapshotBackend(FakeBackend):
    """Pause at the final foreground guard immediately before injection."""

    def __init__(self) -> None:
        super().__init__([TARGET])
        self.snapshot_calls = 0
        self.before_injection = threading.Event()
        self.release = threading.Event()

    def snapshot_foreground_target(self) -> ForegroundTarget:
        self.snapshot_calls += 1
        if self.snapshot_calls == 2:
            self.before_injection.set()
            assert self.release.wait(timeout=2)
        return super().snapshot_foreground_target()


class FakeClipboard:
    def __init__(self, text: str = "old", *, has_text: bool = True) -> None:
        self.text = text
        self.has_text = has_text
        self.sequence = 10
        self.writes: list[str] = []

    def sequence_number(self) -> int:
        return self.sequence

    def get_text(self) -> tuple[bool, str]:
        return self.has_text, self.text

    def set_text(self, text: str) -> None:
        self.text = text
        self.has_text = True
        self.sequence += 1
        self.writes.append(text)

    def external_change(self, text: str) -> None:
        self.text = text
        self.has_text = True
        self.sequence += 1


class AtomicClipboard(FakeClipboard):
    def __init__(self, text: str = "old", *, rich: object | None = None) -> None:
        super().__init__(text)
        self.original_text = text
        self.rich = rich
        self.change_before_write: str | None = None
        self.restored_object: object | None = None
        self.restore_error: Exception | None = None

    def capture_all_formats(self) -> object:
        return self.rich if self.rich is not None else object()

    def replace_text_if_sequence(self, text: str, expected_sequence: int) -> bool:
        if self.change_before_write is not None:
            value = self.change_before_write
            self.change_before_write = None
            self.external_change(value)
        if self.sequence != expected_sequence:
            return False
        self.set_text(text)
        return True

    def restore_all_formats(self, data_object: object) -> None:
        if self.restore_error is not None:
            raise self.restore_error
        self.restored_object = data_object
        self.text = self.original_text
        self.has_text = True
        self.sequence += 1
        self.writes.append("<restore-all>")


def test_target_identity_uses_hwnd_and_pid_but_not_title() -> None:
    renamed = ForegroundTarget(
        hwnd=100,
        pid=200,
        title="Renamed",
        focused_control=TARGET.focused_control,
    )
    reused = ForegroundTarget(
        hwnd=100,
        pid=999,
        title="Editor",
        focused_control=TARGET.focused_control,
    )

    assert targets_match(TARGET, renamed)
    assert not targets_match(TARGET, reused)
    assert not targets_match(TARGET, ForegroundTarget(hwnd=100, pid=200))
    assert not targets_match(TARGET, ForegroundTarget(hwnd=0, pid=0))


def test_utf16_units_preserve_non_bmp_characters() -> None:
    assert utf16_code_units("A😀") == (0x0041, 0xD83D, 0xDE00)


def test_modifier_wait_is_deterministic_and_times_out() -> None:
    @dataclass
    class FakeTime:
        now: float = 0.0

        def clock(self) -> float:
            return self.now

        def sleep(self, duration: float) -> None:
            self.now += duration

    fake_time = FakeTime()
    assert not wait_for_physical_modifiers_clear(
        lambda _vk: True,
        timeout_s=0.03,
        poll_interval_s=0.01,
        clock=fake_time.clock,
        sleeper=fake_time.sleep,
        modifier_keys=(1,),
    )
    assert fake_time.now == 0.03


def test_cancellation_while_waiting_for_modifiers_prevents_injection() -> None:
    backend = FakeBackend([TARGET])
    backend.modifiers_down = True
    waiting = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    outcomes: list[object] = []

    def blocked_sleep(_duration: float) -> None:
        waiting.set()
        assert release.wait(timeout=2)

    worker = threading.Thread(
        target=lambda: outcomes.append(
            send_text(
                "pending",
                TARGET,
                backend=backend,
                sleeper=blocked_sleep,
                cancelled=cancelled.is_set,
                fallback_to_clipboard=False,
            )
        ),
        daemon=True,
    )
    worker.start()
    assert waiting.wait(timeout=2)
    cancelled.set()
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert outcomes and outcomes[0].status is InputStatus.CANCELLED  # type: ignore[union-attr]
    assert backend.unicode_batches == []
    assert backend.ctrl_v_calls == 0
    assert backend.enter_calls == 0


def test_target_mismatch_never_injects_and_copies_fallback() -> None:
    backend = FakeBackend([OTHER_TARGET])
    clipboard = FakeClipboard()

    outcome = send_text(
        "safe text",
        TARGET,
        backend=backend,
        clipboard=clipboard,
        sleeper=lambda _seconds: None,
    )

    assert outcome.status is InputStatus.TARGET_MISMATCH
    assert not outcome.success
    assert outcome.copied
    assert outcome.reason == "foreground_target_changed"
    assert clipboard.text == "safe text"
    assert backend.unicode_batches == []
    assert backend.ctrl_v_calls == 0


def test_single_line_uses_guarded_unicode_batches_without_clipboard() -> None:
    backend = FakeBackend([TARGET])
    clipboard = FakeClipboard()

    outcome = send_text(
        "abcd",
        TARGET,
        backend=backend,
        clipboard=clipboard,
        batch_size=2,
        sleeper=lambda _seconds: None,
    )

    assert outcome.status is InputStatus.INSERTED_UNICODE
    assert outcome.success
    assert not outcome.copied
    assert backend.unicode_batches == [(ord("a"), ord("b")), (ord("c"), ord("d"))]
    assert clipboard.writes == []
    assert outcome.as_dict()["status"] == "inserted_unicode"


def test_press_enter_is_guarded_and_copy_text_is_compatible() -> None:
    backend = FakeBackend([TARGET])
    clipboard = FakeClipboard()

    outcome = send_text(
        "submit",
        expected_target=TARGET,
        backend=backend,
        clipboard=clipboard,
        press_enter=True,
        sleeper=lambda _seconds: None,
    )

    assert outcome.success
    assert backend.enter_calls == 1
    assert copy_text("copy", clipboard=clipboard).copied
    assert clipboard.text == "copy"


def test_target_is_rechecked_before_every_unicode_batch() -> None:
    backend = FakeBackend([TARGET, TARGET, OTHER_TARGET])
    clipboard = FakeClipboard()

    outcome = send_text(
        "abcd",
        TARGET,
        backend=backend,
        clipboard=clipboard,
        batch_size=2,
        sleeper=lambda _seconds: None,
    )

    assert outcome.status is InputStatus.TARGET_MISMATCH
    assert backend.unicode_batches == [(ord("a"), ord("b"))]
    assert clipboard.text == "abcd"
    assert outcome.copied


@pytest.mark.parametrize(
    ("text", "press_enter"),
    [
        ("unicode", False),
        ("one\ntwo", False),
        ("", True),
    ],
)
def test_cancellation_at_final_guard_prevents_every_injection_kind(
    text: str,
    press_enter: bool,
) -> None:
    backend = BlockingSnapshotBackend()
    clipboard = FakeClipboard("prior")
    cancelled = threading.Event()
    outcome: list[object] = []

    worker = threading.Thread(
        target=lambda: outcome.append(
            send_text(
                text,
                TARGET,
                backend=backend,
                clipboard=clipboard,
                clipboard_settle_s=0,
                press_enter=press_enter,
                cancelled=cancelled.is_set,
            )
        ),
        daemon=True,
    )
    worker.start()
    assert backend.before_injection.wait(timeout=2)
    cancelled.set()
    backend.release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert outcome and outcome[0].status is InputStatus.CANCELLED  # type: ignore[union-attr]
    assert backend.unicode_batches == []
    assert backend.ctrl_v_calls == 0
    assert backend.enter_calls == 0
    assert clipboard.text == "prior"


def test_multiline_paste_restores_prior_text_when_sequence_is_unchanged() -> None:
    backend = FakeBackend([TARGET])
    clipboard = FakeClipboard("prior clipboard")

    outcome = send_text(
        "one\ntwo",
        TARGET,
        backend=backend,
        clipboard=clipboard,
        clipboard_settle_s=0,
        sleeper=lambda _seconds: None,
    )

    assert outcome.status is InputStatus.PASTED_CLIPBOARD
    assert outcome.success
    assert not outcome.copied
    assert backend.ctrl_v_calls == 1
    assert clipboard.writes == ["one\ntwo", "prior clipboard"]
    assert clipboard.text == "prior clipboard"


def test_multiline_target_change_rolls_back_full_clipboard_before_paste() -> None:
    rich_object = object()
    clipboard = AtomicClipboard("prior rich clipboard", rich=rich_object)
    backend = FakeBackend([TARGET, OTHER_TARGET])

    outcome = send_text(
        "one\ntwo",
        TARGET,
        backend=backend,
        clipboard=clipboard,
        clipboard_settle_s=0,
        fallback_to_clipboard=False,
        sleeper=lambda _seconds: None,
    )

    assert outcome.status is InputStatus.TARGET_MISMATCH
    assert outcome.reason == "target_mismatch"
    assert outcome.copied is False
    assert backend.ctrl_v_calls == 0
    assert clipboard.text == "prior rich clipboard"
    assert clipboard.restored_object is rich_object
    assert clipboard.writes == ["one\ntwo", "<restore-all>"]


def test_guard_exception_rolls_back_full_clipboard_before_paste() -> None:
    rich_object = object()
    clipboard = AtomicClipboard("prior rich clipboard", rich=rich_object)
    paste_calls = 0

    def fail_guard() -> bool:
        raise RuntimeError("focus probe failed")

    def paste() -> bool:
        nonlocal paste_calls
        paste_calls += 1
        return True

    result = clipboard_paste_transaction(
        "transcript",
        clipboard=clipboard,
        paste=paste,
        guard=fail_guard,
        settle_s=0,
    )

    assert result.success is False
    assert result.reason == "target_guard_failed"
    assert result.copied is False
    assert result.restored is True
    assert result.detail == "focus probe failed"
    assert paste_calls == 0
    assert clipboard.text == "prior rich clipboard"
    assert clipboard.restored_object is rich_object


@pytest.mark.parametrize("paste_mode", ("false", "raise"))
def test_failed_paste_input_rolls_back_full_clipboard(paste_mode: str) -> None:
    rich_object = object()
    clipboard = AtomicClipboard("prior rich clipboard", rich=rich_object)

    def paste() -> bool:
        if paste_mode == "raise":
            raise RuntimeError("SendInput failed")
        return False

    result = clipboard_paste_transaction(
        "transcript",
        clipboard=clipboard,
        paste=paste,
        settle_s=0,
    )

    assert result.success is False
    assert result.reason == "paste_input_failed"
    assert result.copied is False
    assert result.restored is True
    assert clipboard.text == "prior rich clipboard"
    assert clipboard.restored_object is rich_object
    if paste_mode == "raise":
        assert result.detail == "SendInput failed"


def test_transient_sequence_check_failure_rolls_back_if_our_value_still_owns_clipboard() -> None:
    class TransientSequenceFailureClipboard(AtomicClipboard):
        def __init__(self) -> None:
            super().__init__("prior rich clipboard", rich=object())
            self.sequence_calls = 0

        def sequence_number(self) -> int:
            self.sequence_calls += 1
            # Snapshot uses calls 1/2, post-write capture uses call 3, and the
            # pre-paste ownership check is call 4. Rollback retries at call 5.
            if self.sequence_calls == 4:
                raise RuntimeError("transient sequence error")
            return super().sequence_number()

    clipboard = TransientSequenceFailureClipboard()
    paste_calls = 0

    def paste() -> bool:
        nonlocal paste_calls
        paste_calls += 1
        return True

    result = clipboard_paste_transaction(
        "transcript",
        clipboard=clipboard,
        paste=paste,
        settle_s=0,
    )

    assert result.success is False
    assert result.reason == "clipboard_sequence_failed"
    assert result.copied is False
    assert result.restored is True
    assert result.detail == "transient sequence error"
    assert paste_calls == 0
    assert clipboard.text == "prior rich clipboard"


def test_rollback_never_overwrites_external_clipboard_change() -> None:
    clipboard = AtomicClipboard("prior rich clipboard", rich=object())

    def changed_guard() -> bool:
        clipboard.external_change("new user value")
        return False

    result = clipboard_paste_transaction(
        "transcript",
        clipboard=clipboard,
        paste=lambda: pytest.fail("Ctrl+V must not be sent"),
        guard=changed_guard,
        settle_s=0,
    )

    assert result.success is False
    assert result.reason == "target_mismatch"
    assert result.copied is False
    assert result.restored is False
    assert "clipboard changed before rollback" in str(result.detail)
    assert clipboard.text == "new user value"
    assert clipboard.restored_object is None


def test_rollback_failure_is_reported_as_clipboard_failure_not_copy() -> None:
    clipboard = AtomicClipboard("prior rich clipboard", rich=object())
    clipboard.restore_error = RuntimeError("OLE restore failed")
    backend = FakeBackend([TARGET, OTHER_TARGET])

    outcome = send_text(
        "one\ntwo",
        TARGET,
        backend=backend,
        clipboard=clipboard,
        clipboard_settle_s=0,
        fallback_to_clipboard=False,
        sleeper=lambda _seconds: None,
    )

    assert outcome.status is InputStatus.CLIPBOARD_FAILED
    assert outcome.reason == "clipboard_restore_failed"
    assert outcome.copied is False
    assert "target_mismatch" in str(outcome.detail)
    assert "OLE restore failed" in str(outcome.detail)
    assert backend.ctrl_v_calls == 0


def test_clipboard_transaction_does_not_overwrite_external_change() -> None:
    backend = FakeBackend([TARGET])
    clipboard = FakeClipboard("prior clipboard")
    backend.on_paste = lambda: clipboard.external_change("new user value")

    result = clipboard_paste_transaction(
        "transcript",
        clipboard=clipboard,
        paste=backend.send_ctrl_v,
        settle_s=0,
    )

    assert result.success
    assert not result.restored
    assert not result.copied
    assert result.reason == "clipboard_changed_not_restored"
    assert clipboard.text == "new user value"
    assert clipboard.writes == ["transcript"]


def test_atomic_clipboard_refuses_write_after_new_user_copy() -> None:
    clipboard = AtomicClipboard("old")
    clipboard.change_before_write = "NEW USER COPY"
    paste_calls = 0

    def paste() -> bool:
        nonlocal paste_calls
        paste_calls += 1
        return True

    result = clipboard_paste_transaction(
        "transcript",
        clipboard=clipboard,
        paste=paste,
        settle_s=0,
    )

    assert result.success is False
    assert result.reason == "clipboard_changed_before_write"
    assert clipboard.text == "NEW USER COPY"
    assert clipboard.writes == []
    assert paste_calls == 0


def _partial_write_clipboard(
    monkeypatch: pytest.MonkeyPatch,
    *,
    external_change_after_close: bool = False,
) -> tuple[Win32Clipboard, object, object]:
    class FakeUser32:
        def __init__(self) -> None:
            self.sequence = 10
            self.opened = False
            self.empty_calls = 0
            self.close_calls = 0

        def OpenClipboard(self, _owner: object) -> bool:
            self.opened = True
            return True

        def CloseClipboard(self) -> bool:
            assert self.opened
            self.opened = False
            self.close_calls += 1
            if external_change_after_close:
                self.sequence += 1
            return True

        def EmptyClipboard(self) -> bool:
            assert self.opened
            self.empty_calls += 1
            self.sequence += 1
            return True

        def GetClipboardSequenceNumber(self) -> int:
            return self.sequence

    class FakeKernel32:
        global_alloc_calls = 0

        def GlobalAlloc(self, _flags: int, _size: int) -> int:
            self.global_alloc_calls += 1
            return 0  # exact failure immediately after EmptyClipboard

        @staticmethod
        def GlobalFree(_memory: object) -> int:
            return 0

    user32 = FakeUser32()
    kernel32 = FakeKernel32()
    clipboard = object.__new__(Win32Clipboard)
    clipboard._api = SimpleNamespace(
        ctypes=ctypes,
        user32=user32,
        kernel32=kernel32,
    )
    clipboard.open_timeout_s = 0.01
    clipboard.retry_interval_s = 0.001
    clipboard._com_transaction_depth = 1
    rich_object = object()
    return clipboard, rich_object, SimpleNamespace(user32=user32, kernel32=kernel32)


def test_win32_partial_empty_then_global_alloc_failure_restores_rich_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clipboard, rich_object, api = _partial_write_clipboard(monkeypatch)
    restored: list[object] = []
    monkeypatch.setattr(clipboard, "restore_all_formats", restored.append)

    with pytest.raises(ClipboardReplaceError) as caught:
        clipboard.replace_text_if_sequence(
            "transcript",
            10,
            rollback_data_object=rich_object,
        )

    assert caught.value.reason == "clipboard_write_failed"
    assert caught.value.restored is True
    assert api.user32.empty_calls == 1
    assert api.kernel32.global_alloc_calls == 1
    assert api.user32.close_calls == 1
    assert restored == [rich_object]


def test_win32_partial_write_never_restores_over_external_sequence_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clipboard, rich_object, api = _partial_write_clipboard(
        monkeypatch,
        external_change_after_close=True,
    )
    restored: list[object] = []
    monkeypatch.setattr(clipboard, "restore_all_formats", restored.append)

    with pytest.raises(ClipboardReplaceError) as caught:
        clipboard.replace_text_if_sequence(
            "transcript",
            10,
            rollback_data_object=rich_object,
        )

    assert caught.value.reason == "clipboard_changed_during_write"
    assert caught.value.restored is False
    assert api.user32.empty_calls == 1
    assert restored == []


def test_structured_partial_write_error_preserves_restored_state() -> None:
    class StructuredFailureClipboard(AtomicClipboard):
        supports_transactional_restore = True

        def replace_text_if_sequence(
            self,
            text: str,
            expected_sequence: int,
            *,
            rollback_data_object: object | None = None,
        ) -> bool:
            assert expected_sequence == self.sequence
            self.set_text(text)
            self.restore_all_formats(rollback_data_object)
            raise ClipboardReplaceError(
                "GlobalAlloc failed",
                reason="clipboard_write_failed",
                restored=True,
            )

    clipboard = StructuredFailureClipboard("prior rich clipboard", rich=object())
    paste_calls = 0

    def paste() -> bool:
        nonlocal paste_calls
        paste_calls += 1
        return True

    result = clipboard_paste_transaction(
        "transcript",
        clipboard=clipboard,
        paste=paste,
        settle_s=0,
    )

    assert result.success is False
    assert result.reason == "clipboard_write_failed"
    assert result.copied is False
    assert result.restored is True
    assert clipboard.text == "prior rich clipboard"
    assert paste_calls == 0


def test_full_clipboard_object_is_restored_after_paste() -> None:
    rich_object = object()
    clipboard = AtomicClipboard("plain fallback", rich=rich_object)

    result = clipboard_paste_transaction(
        "transcript",
        clipboard=clipboard,
        paste=lambda: True,
        settle_s=0,
    )

    assert result.success is True
    assert result.restored is True
    assert clipboard.restored_object is rich_object


def test_paste_last_works_without_a_saved_target() -> None:
    backend = FakeBackend([TARGET])
    clipboard = FakeClipboard("prior")

    outcome = paste_last(
        "remembered",
        backend=backend,
        clipboard=clipboard,
        clipboard_settle_s=0,
        sleeper=lambda _seconds: None,
    )

    assert outcome.status is InputStatus.PASTED_CLIPBOARD
    assert outcome.success
    assert backend.ctrl_v_calls == 1
    assert clipboard.text == "prior"


def test_copy_last_needs_no_target() -> None:
    clipboard = FakeClipboard()

    outcome = copy_last("remembered", clipboard=clipboard)

    assert outcome.status is InputStatus.COPIED
    assert outcome.success
    assert outcome.copied
    assert clipboard.text == "remembered"
