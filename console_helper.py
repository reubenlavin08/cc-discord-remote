"""Standalone helper: attach to a running Claude Code console, send text, capture screen.

Runs as a subprocess from bot.py. Isolates the AttachConsole/FreeConsole calls so the
bot's own console isn't disturbed.

Uses raw ctypes (not pywin32) because pywin32's GetStdHandle wrapper caches the
parent process's handles and breaks after AttachConsole. Opening CONOUT$ / CONIN$
via CreateFileW gives us fresh handles into the attached console.

Usage:
    python console_helper.py <pid> <prompt-file> <output-file> [--mode=send|look]
"""

import argparse
import ctypes
import sys
import time
from ctypes import byref, c_short, wintypes
from pathlib import Path

# ---------- Win32 bindings --------------------------------------------------

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
KEY_EVENT = 0x1
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class COORD(ctypes.Structure):
    _fields_ = [("X", c_short), ("Y", c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", c_short), ("Top", c_short), ("Right", c_short), ("Bottom", c_short)]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", COORD),
        ("dwCursorPosition", COORD),
        ("wAttributes", wintypes.WORD),
        ("srWindow", SMALL_RECT),
        ("dwMaximumWindowSize", COORD),
    ]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", ctypes.c_wchar),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class _EventUnion(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EventUnion)]


kernel32.AttachConsole.argtypes = [wintypes.DWORD]
kernel32.AttachConsole.restype = wintypes.BOOL
kernel32.FreeConsole.argtypes = []
kernel32.FreeConsole.restype = wintypes.BOOL
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetConsoleScreenBufferInfo.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO),
]
kernel32.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
kernel32.ReadConsoleOutputCharacterW.argtypes = [
    wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, COORD, ctypes.POINTER(wintypes.DWORD),
]
kernel32.ReadConsoleOutputCharacterW.restype = wintypes.BOOL
kernel32.WriteConsoleInputW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(INPUT_RECORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
kernel32.WriteConsoleInputW.restype = wintypes.BOOL

user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
user32.VkKeyScanW.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
MAPVK_VK_TO_VSC = 0

STABLE_SECONDS = 1.5
MAX_WAIT_SECONDS = 60.0
POLL_INTERVAL = 0.25


# ---------- helpers ---------------------------------------------------------

def _open_console_handle(name: str) -> int:
    h = kernel32.CreateFileW(
        name,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None,
    )
    if not h or h == INVALID_HANDLE_VALUE:
        raise OSError(f"CreateFileW({name}) failed: Win32 err {ctypes.get_last_error()}")
    return h


def _read_screen(h_out: int) -> str:
    info = CONSOLE_SCREEN_BUFFER_INFO()
    if not kernel32.GetConsoleScreenBufferInfo(h_out, byref(info)):
        raise OSError(f"GetConsoleScreenBufferInfo failed: Win32 err {ctypes.get_last_error()}")
    width = info.dwSize.X
    top = info.srWindow.Top
    bottom = info.srWindow.Bottom
    lines = []
    for y in range(top, bottom + 1):
        buf = ctypes.create_unicode_buffer(width + 1)
        read = wintypes.DWORD(0)
        if kernel32.ReadConsoleOutputCharacterW(h_out, buf, width, COORD(0, y), byref(read)):
            lines.append(buf.value[: read.value].rstrip())
        else:
            lines.append("")
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _key_event(ch: str, key_down: bool, vk: int) -> INPUT_RECORD:
    rec = INPUT_RECORD()
    rec.EventType = KEY_EVENT
    rec.Event.KeyEvent.bKeyDown = 1 if key_down else 0
    rec.Event.KeyEvent.wRepeatCount = 1
    rec.Event.KeyEvent.wVirtualKeyCode = vk
    # Real keyboards always set a scan code. Some TUIs (Claude Code's input library)
    # filter out events whose scan code is zero, so derive the scan code from the VK.
    rec.Event.KeyEvent.wVirtualScanCode = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) if vk else 0
    rec.Event.KeyEvent.uChar = ch
    rec.Event.KeyEvent.dwControlKeyState = 0
    return rec


def _flush_events(h_in: int, events: list):
    if not events:
        return
    arr_type = INPUT_RECORD * len(events)
    arr = arr_type(*events)
    written = wintypes.DWORD(0)
    if not kernel32.WriteConsoleInputW(h_in, arr, len(events), byref(written)):
        raise OSError(f"WriteConsoleInputW failed: Win32 err {ctypes.get_last_error()}")


def _write_text(h_in: int, text: str):
    # Chunk the typing events so we never overflow the console's input buffer
    # (default ~256 records). For long pastes we flush every CHUNK_CHARS characters
    # and sleep briefly to let the TUI consume them.
    CHUNK_CHARS = 80

    pending: list = []
    chars_in_chunk = 0
    for ch in text:
        if ch == "\n":
            pending.append(_key_event("\r", True, VK_RETURN))
            pending.append(_key_event("\r", False, VK_RETURN))
            chars_in_chunk += 1
        else:
            try:
                vk = user32.VkKeyScanW(ch) & 0xFF if ch.isprintable() else 0
            except Exception:
                vk = 0
            pending.append(_key_event(ch, True, vk))
            pending.append(_key_event(ch, False, vk))
            chars_in_chunk += 1
        if chars_in_chunk >= CHUNK_CHARS:
            _flush_events(h_in, pending)
            pending = []
            chars_in_chunk = 0
            time.sleep(0.05)
    if pending:
        _flush_events(h_in, pending)

    # Brief pause so the TUI can process the typed characters before the submit Enter.
    time.sleep(0.15)
    _flush_events(h_in, [
        _key_event("\r", True, VK_RETURN),
        _key_event("\r", False, VK_RETURN),
    ])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pid", type=int)
    parser.add_argument("prompt_file")
    parser.add_argument("output_file")
    parser.add_argument("--mode", choices=("send", "look", "type", "esc"), default="send")
    args = parser.parse_args()

    out_path = Path(args.output_file)

    kernel32.FreeConsole()  # detach from our parent's console (bot's)
    if not kernel32.AttachConsole(args.pid):
        err = ctypes.get_last_error()
        out_path.write_text(
            f"AttachConsole({args.pid}) failed (Win32 err {err}). "
            f"Process may not have an attachable console (running detached or via Windows Terminal "
            f"with conhost.exe owning the buffer).\n",
            encoding="utf-8",
        )
        sys.exit(1)

    try:
        h_out = _open_console_handle("CONOUT$")
        h_in = _open_console_handle("CONIN$")

        try:
            if args.mode == "esc":
                # Send a single Escape keystroke — used to dismiss dialogs the TUI is showing.
                _flush_events(h_in, [
                    _key_event("\x1b", True, VK_ESCAPE),
                    _key_event("\x1b", False, VK_ESCAPE),
                ])
                out_path.write_text("esc-sent\n", encoding="utf-8")
                return

            if args.mode == "type":
                # Type only — no screen polling. Caller will watch the session JSONL.
                text = Path(args.prompt_file).read_text(encoding="utf-8")
                _write_text(h_in, text)
                out_path.write_text("typed\n", encoding="utf-8")
                return

            if args.mode == "send":
                text = Path(args.prompt_file).read_text(encoding="utf-8")
                _write_text(h_in, text)

                last = ""
                last_change = time.time()
                deadline = time.time() + MAX_WAIT_SECONDS
                while time.time() < deadline:
                    current = _read_screen(h_out)
                    if current != last:
                        last = current
                        last_change = time.time()
                    elif time.time() - last_change >= STABLE_SECONDS:
                        break
                    time.sleep(POLL_INTERVAL)
                final = last
            else:
                final = _read_screen(h_out)

            out_path.write_text(final, encoding="utf-8")
        finally:
            kernel32.CloseHandle(h_out)
            kernel32.CloseHandle(h_in)
    except Exception as e:
        out_path.write_text(
            f"Console operation failed: {type(e).__name__}: {e}\n", encoding="utf-8"
        )
        sys.exit(1)
    finally:
        kernel32.FreeConsole()


if __name__ == "__main__":
    main()
