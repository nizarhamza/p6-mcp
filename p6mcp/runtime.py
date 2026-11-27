"""Runtime files shared by the server, the tray app and the Settings window: rotating logs and the
HTTP server's status file (%LOCALAPPDATA%\\P6MCP)."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import sys
import time
from datetime import datetime
from typing import Optional

from p6mcp import config

LOG_BYTES, LOG_COUNT = 1_000_000, 5


def logs_dir() -> str:
    return os.path.join(config.localdata_dir(), "logs")


def setup_logging(name: str, level: Optional[str] = None) -> logging.Logger:
    """Root logger to <logs>\\<name>.log (1 MB x 5 files). The level comes from config log_level."""
    os.makedirs(logs_dir(), exist_ok=True)
    level = (level or config.load().log_level or "INFO").upper()
    root = logging.getLogger()
    path = os.path.join(logs_dir(), f"{name}.log")
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) and getattr(h, "baseFilename", "") == path
               for h in root.handlers):
        h = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_COUNT, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(h)
    root.setLevel(getattr(logging, level, logging.INFO))
    return logging.getLogger(name)


# ----------------------------------------------------------------- HTTP server status

STATUS_NAMES = ("http", "remote")  # http_status.json: local HTTP server; remote_status.json: remote access server


def status_path(name: str = "http") -> str:
    if name not in STATUS_NAMES:
        raise ValueError(f"unknown status file {name!r}")
    return os.path.join(config.localdata_dir(), f"{name}_status.json")


def write_status(state: str, port: Optional[int] = None, error: Optional[str] = None, *, name: str = "http",
                 **extra) -> None:
    data = {"state": state, "pid": os.getpid(), "ppid": os.getppid(), "port": port, "error": error,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **extra}
    path = status_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    for _attempt in range(20):  # a reader holding the file open blocks the rename on Windows for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    logging.getLogger("p6mcp.runtime").warning("status file not updated (in use): %s", state)


def pid_alive(pid: Optional[int]) -> bool:
    """Without signalling the process (os.kill would terminate it on Windows)."""
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


# ----------------------------------------------------------------- setup in progress

SETUP_MUTEX = "P6MCP-Setup"  # the installer's SetupMutex, held while MCP for Primavera P6 setup runs


def setup_running(name: str = SETUP_MUTEX) -> bool:
    """True while MCP for Primavera P6 setup runs: a program started now would lock files it is replacing, and a
    silent setup then aborts half way (its rollback does not restore the files already replaced)."""
    if sys.platform != "win32":
        return False
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenMutexW(0x00100000, False, name)  # SYNCHRONIZE
    if not h:
        return False
    k32.CloseHandle(h)
    return True


def wait_for_setup_end(timeout: float = 300.0, poll: float = 0.5, name: str = SETUP_MUTEX) -> bool:
    """True once setup has ended (False after timeout seconds). Only for a program setup itself started after
    copying its files (--after-setup): its own [Run] entries run while it still holds its mutex."""
    deadline = time.monotonic() + timeout
    while setup_running(name):
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
    return True


def window_request_path() -> str:
    return os.path.join(config.localdata_dir(), "show_window_after_setup")


def request_window_after_setup() -> None:
    """The Settings window was asked for during setup: the app started after setup opens it."""
    try:
        os.makedirs(config.localdata_dir(), exist_ok=True)
        with open(window_request_path(), "w", encoding="utf-8") as fh:
            fh.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except OSError:
        pass


def take_window_request() -> bool:
    """True once after request_window_after_setup (the request is removed)."""
    try:
        os.unlink(window_request_path())
        return True
    except OSError:
        return False


def port_open(port: Optional[int], host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def read_status(name: str = "http") -> dict:
    """The status file checked against reality: a 'running' server whose process is gone is 'stopped'."""
    try:
        with open(status_path(name), encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return {"state": "stopped", "pid": None, "port": None, "error": None}
    if st.get("state") in ("running", "starting") and not pid_alive(st.get("pid")):
        what = "remote access" if name == "remote" else "HTTP"
        st = dict(st, state="stopped", error=st.get("error") or f"The {what} server process has ended.")
    return st
