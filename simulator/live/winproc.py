"""Which MT5 terminals are running, and a lock so one recorder writes a feed.

The recorder never starts a terminal. Starting one restores its charts and
can switch on any Expert Advisor attached to them, which could trade the
account it is logged into. So the recorder attaches only to a terminal the
trader opened, and this module is how it finds one.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

TERMINAL_EXE = "terminal64.exe"


def running_terminals() -> list[str]:
    """Full paths of every running terminal64.exe."""
    if sys.platform != "win32":
        return []
    from ctypes import wintypes

    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    size = 4096
    while True:
        pids = (wintypes.DWORD * size)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed)):
            return []
        if needed.value < ctypes.sizeof(pids):
            break
        size *= 2
    count = needed.value // ctypes.sizeof(wintypes.DWORD)

    found = []
    buf = ctypes.create_unicode_buffer(1024)
    for pid in pids[:count]:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            length = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(length)):
                path = buf.value
                if Path(path).name.lower() == TERMINAL_EXE:
                    found.append(path)
        finally:
            kernel32.CloseHandle(handle)
    return sorted(set(found))


def same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


class FileLock:
    """An OS-level lock on a file, released when the process dies."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None
