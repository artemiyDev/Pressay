"""Live Win32 clipboard checks (real clipboard, real own window).

Runs a child process that owns a tkinter Entry window, so no user application
is touched.  The real clipboard is snapshotted before and restored after.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import uuid

import pytest

pytestmark = [
    pytest.mark.skipif(os.name != "nt", reason="Windows clipboard only"),
    # CI runners have no interactive desktop to bring a window to the front,
    # and these tests touch the real clipboard; run them on a developer box.
    pytest.mark.skipif(bool(os.environ.get("CI")), reason="needs an interactive desktop"),
]

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

_PREAMBLE = """
import sys, ctypes, json
sys.path.insert(0, {src!r})
from pressay.windows_input import Win32Clipboard
FMT_NAME = "PressayTestFormat"
FMT_DATA = b"\\x01\\x02\\x03"


def put_marker(marker):
    clip = Win32Clipboard()
    api = clip._api
    clip._open()
    try:
        api.user32.EmptyClipboard()
        encoded = marker.encode("utf-16-le") + b"\\x00\\x00"
        clip._set_global_data(clip.CF_UNICODETEXT, encoded)
        fid = api.user32.RegisterClipboardFormatW(FMT_NAME)
        clip._set_global_data(fid, FMT_DATA)
    finally:
        api.user32.CloseClipboard()


def read_state():
    clip = Win32Clipboard()
    snap = clip.capture_all_formats()
    text = None
    custom = None
    for e in snap.entries:
        if e.format_id == 13:
            text = e.data.decode("utf-16-le").rstrip("\\x00")
        if e.name == FMT_NAME:
            custom = e.data
    return text, custom
"""


def _run(body: str, *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    script = _PREAMBLE.format(src=_SRC) + textwrap.dedent(body)
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "live_child.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("import faulthandler; faulthandler.dump_traceback_later(40, exit=True)" + chr(10))
            handle.write(script)
        return subprocess.run(
            [sys.executable, path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )


@pytest.fixture()
def real_clipboard_guard():
    """Snapshot the user's real clipboard and put it back afterwards."""

    from pressay.windows_input import Win32Clipboard

    clipboard = Win32Clipboard()
    snapshot = clipboard.capture_all_formats()
    try:
        yield
    finally:
        if not snapshot.oversized:
            clipboard.restore_all_formats(snapshot)


def test_live_snapshot_write_restore_roundtrip(real_clipboard_guard) -> None:
    marker = f"PREV-{uuid.uuid4()}"
    result = _run(
        f"""
        from pressay.windows_input import clipboard_paste_transaction
        marker = {marker!r}
        put_marker(marker)
        clip = Win32Clipboard()
        out = clipboard_paste_transaction(
            "dictation text", clipboard=clip, paste=lambda: True, settle_s=0
        )
        text, custom = read_state()
        print(json.dumps({{
            "restored": out.restored, "reason": out.reason, "detail": out.detail,
            "text": text, "custom": custom.hex() if custom else None,
        }}))
        """
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data["restored"] is True, data
    assert data["text"] == marker
    assert data["custom"] == "010203"


def test_live_empty_previous_clipboard_is_cleared(real_clipboard_guard) -> None:
    result = _run(
        """
        from pressay.windows_input import clipboard_paste_transaction
        clip = Win32Clipboard()
        clip._open()
        clip._api.user32.EmptyClipboard()
        clip._api.user32.CloseClipboard()
        out = clipboard_paste_transaction(
            "dictation text", clipboard=clip, paste=lambda: True, settle_s=0
        )
        text, custom = read_state()
        print(json.dumps({"restored": out.restored, "text": text}))
        """
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data["restored"] is True and data["text"] is None


_WINDOW_BODY = """
from ctypes import wintypes
import time
from pressay.windows_input import Win32InputBackend, send_text, InputStatus

marker = {marker!r}
u32 = ctypes.WinDLL("user32", use_last_error=True)
u32.CreateWindowExW.restype = wintypes.HWND
u32.CreateWindowExW.argtypes = (
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
)
u32.SetForegroundWindow.argtypes = (wintypes.HWND,)
u32.SetFocus.argtypes = (wintypes.HWND,)
u32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
u32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
u32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT)
u32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
u32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
# top-level EDIT window: WS_OVERLAPPEDWINDOW | WS_VISIBLE | ES_AUTOHSCROLL
hwnd = u32.CreateWindowExW(0x8, "EDIT", "PressayLiveTestWindow", 0x00CF0000 | 0x10000000 | 0x80,
                           200, 200, 500, 80, None, None, None, None)
assert hwnd, ctypes.get_last_error()
msg = wintypes.MSG()


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


u32.ShowWindow(hwnd, 5)
u32.SetForegroundWindow(hwnd)
u32.SetFocus(hwnd)
pump(1.5)
backend = Win32InputBackend()
import threading
snap = []
st = threading.Thread(target=lambda: snap.append(backend.snapshot_foreground_target()))
st.start()
while st.is_alive():
    pump(0.01)  # UIA queries our window: the owning thread must keep pumping
target = snap[0]
if target.hwnd != hwnd:
    print(json.dumps({{"skip": "foreground not ours", "hwnd": target.hwnd, "ours": hwnd}}))
    sys.exit(0)
put_marker(marker)
import threading
outcome = {{}}

def worker():
    out = send_text(
        "\u0442\u0435\u0441\u0442 \u043c\u0433\u043d\u043e\u0432\u0435\u043d\u043d\u043e\u0439 \u0432\u0441\u0442\u0430\u0432\u043a\u0438 123",
        expected_target=target,
        backend=backend,
        clipboard=Win32Clipboard(),
        insert_method="paste",
        clipboard_settle_s=0.4,
    )
    outcome["status"] = str(out.status)
    outcome["success"] = out.success
    outcome["reason"] = out.reason
    outcome["detail"] = out.detail
    outcome["copied"] = out.copied

t = threading.Thread(target=worker)
t.start()
while t.is_alive():
    pump(0.01)
pump(0.2)
text, custom = read_state()
buf = ctypes.create_unicode_buffer(512)
u32.GetWindowTextW(hwnd, buf, 512)
print(json.dumps({{
    "entry": buf.value, "outcome": outcome,
    "clip_text": text, "custom": custom.hex() if custom else None,
}}))
u32.DestroyWindow(hwnd)
"""


def test_live_send_text_paste_restores_clipboard(real_clipboard_guard) -> None:
    marker = f"PREV-{uuid.uuid4()}"
    result = _run(_WINDOW_BODY.format(marker=marker), timeout=90)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip().splitlines()[-1])
    if "skip" in data:
        pytest.skip(f"cannot bring own window to foreground: {data}")
    print(data)
    assert data["outcome"]["success"] is True, data
    assert data["entry"].startswith("тест мгновенной вставки 123")
    assert data["clip_text"] == marker
    assert data["custom"] == "010203"
