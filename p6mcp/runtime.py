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

def status_path() -> str:
    return os.path.join(config.localdata_dir(), "http_status.json")


def write_status(state: str, port: Optional[int] = None, error: Optional[str] = None, **extra) -> None:
    data = {"state": state, "pid": os.getpid(), "ppid": os.getppid(), "port": port, "error": error,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **extra}
    os.makedirs(os.path.dirname(status_path()), exist_ok=True)
    tmp = f"{status_path()}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    for _attempt in range(20):  # a reader holding the file open blocks the rename on Windows for a moment
        try:
            os.replace(tmp, status_path())
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


def port_open(port: Optional[int], host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def read_status() -> dict:
    """The status file checked against reality: a 'running' server whose process is gone is 'stopped'."""
    try:
        with open(status_path(), encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return {"state": "stopped", "pid": None, "port": None, "error": None}
    if st.get("state") in ("running", "starting") and not pid_alive(st.get("pid")):
        st = dict(st, state="stopped", error=st.get("error") or "The HTTP server process has ended.")
    return st
