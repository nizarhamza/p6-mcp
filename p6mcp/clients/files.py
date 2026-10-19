"""Safe access to MCP client config files: read with encoding kept, timestamped backup, atomic write."""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime
from typing import Optional

from p6mcp import config

BOM = "﻿"
KEEP_BACKUPS = 20


class ClientFileError(RuntimeError):
    """A client config file cannot be read or written safely."""


def read(path: str) -> tuple[Optional[str], bool]:
    """(text or None when the file does not exist, had a UTF-8 BOM)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None, False
    except OSError as e:
        raise ClientFileError(f"Cannot read {path}: {e}") from e
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ClientFileError(f"{path} is not UTF-8 text: left unchanged") from e
    bom = text.startswith(BOM)
    return (text[1:] if bom else text), bom


def backup_dir(client_id: str) -> str:
    return os.path.join(config.localdata_dir(), "client_backups", client_id)


def backup(client_id: str, path: str) -> Optional[str]:
    """Copy of the current file in %LOCALAPPDATA%\\P6MCP\\client_backups\\<client> (None when there is no file).
    The oldest copies beyond KEEP_BACKUPS of that file are removed."""
    if not os.path.isfile(path):
        return None
    folder = backup_dir(client_id)
    os.makedirs(folder, exist_ok=True)
    name = os.path.basename(path)
    dest = os.path.join(folder, f"{name}.{datetime.now():%Y%m%d_%H%M%S_%f}.bak")
    with open(path, "rb") as src, open(dest, "wb") as dst:
        dst.write(src.read())
    old = sorted(f for f in os.listdir(folder) if f.startswith(name + ".") and f.endswith(".bak"))
    for f in old[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(folder, f))
        except OSError:
            pass
    return dest


def write_atomic(path: str, text: str, bom: bool = False) -> None:
    """Temporary file in the same folder, flushed to disk, then renamed over the target. A client
    holding the file open can make the rename fail for a moment: retried a few times."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    data = ((BOM if bom else "") + text).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".p6mcp.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.25)
    except BaseException as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(e, OSError):
            raise ClientFileError(f"Cannot write {path}: {e}") from e
        raise
