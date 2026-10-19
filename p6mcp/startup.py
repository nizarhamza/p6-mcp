"""Run at Windows startup, per user and without admin rights: a value in
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run that starts the Settings/tray app."""

from __future__ import annotations

import os
import sys
from typing import Optional

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "P6MCP"


def command(minimized: bool = True) -> str:
    """The installed p6mcp-settings.exe, else this Python (windowless) with -m p6mcp.settings_app."""
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}"'
    else:
        head, tail = os.path.split(sys.executable)
        pyw = os.path.join(head, "pythonw.exe") if tail.lower() == "python.exe" else sys.executable
        cmd = f'"{pyw}" -m p6mcp.settings_app'
    return cmd + (" --minimized" if minimized else "")


def _winreg():
    if sys.platform != "win32":
        return None
    import winreg
    return winreg


def current(key: str = RUN_KEY) -> Optional[str]:
    """The command registered to run at startup, None when there is none."""
    wr = _winreg()
    if wr is None:
        return None
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, key, 0, wr.KEY_READ) as k:
            value, _ = wr.QueryValueEx(k, VALUE)
            return str(value)
    except OSError:
        return None


def enable(minimized: bool = True, key: str = RUN_KEY) -> str:
    wr = _winreg()
    if wr is None:
        raise OSError("Run at startup is only available on Windows")
    cmd = command(minimized)
    with wr.CreateKeyEx(wr.HKEY_CURRENT_USER, key, 0, wr.KEY_SET_VALUE) as k:
        wr.SetValueEx(k, VALUE, 0, wr.REG_SZ, cmd)
    return cmd


def disable(key: str = RUN_KEY) -> bool:
    """True when a value was removed."""
    wr = _winreg()
    if wr is None:
        return False
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, key, 0, wr.KEY_SET_VALUE) as k:
            wr.DeleteValue(k, VALUE)
            return True
    except OSError:
        return False


def sync(data: dict, key: str = RUN_KEY) -> Optional[str]:
    """Make the registry match the settings (called when the Settings window saves). Returns the
    command now registered, None when none."""
    if data.get("run_at_startup"):
        want = command(bool(data.get("start_minimized", True)))
        if current(key) != want:
            enable(bool(data.get("start_minimized", True)), key)
        return want
    disable(key)
    return None
