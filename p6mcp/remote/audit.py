"""Audit log of remote access: %LOCALAPPDATA%\\P6MCP\\remote\\audit_YYYYMMDD.jsonl, one JSON line per event.

Events: http (every request: method, path without its query, status, bytes, ms, ip), tool (tool, connection,
app, arguments digest, result ok / refused / error / too_large, ms, characters), oauth (request, approved, denied,
expired, registered, token, refresh, reuse, revoked, error). Never a token, a code, a query string or an
argument value: a digest of the arguments links a call to its edit log entry without copying schedule data.
Files older than KEEP_DAYS are removed once a day.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional

from p6mcp import config

KEEP_DAYS = 90
# the caller's IP of the running request, as the relay (or Cloudflare's edge) reports it; set by RemoteGuard
CLIENT_IP: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("p6_remote_client_ip", default=None)
_lock = threading.Lock()
_pruned: Optional[date] = None


def folder() -> str:
    return os.path.join(config.localdata_dir(), "remote")


def _key() -> bytes:
    """A random key of this PC (remote\\audit.key): the argument digests cannot be reversed by guessing short
    arguments ({"project": "X"}) without it."""
    path = os.path.join(folder(), "audit.key")
    try:
        with open(path, "rb") as fh:
            key = fh.read()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    import secrets
    key = secrets.token_bytes(32)
    try:
        os.makedirs(folder(), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(key)
    except OSError:
        pass
    return key


def digest(args: Any) -> str:
    """Short keyed digest of tool arguments (canonical JSON): the same arguments give the same digest on this PC."""
    import hmac
    text = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hmac.new(_key(), text.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def _prune(today: date) -> None:
    global _pruned
    if _pruned == today:
        return
    _pruned = today
    cutoff = (today - timedelta(days=KEEP_DAYS)).strftime("%Y%m%d")
    try:
        for name in os.listdir(folder()):
            if name.startswith("audit_") and name.endswith(".jsonl") and name[6:14] < cutoff:
                os.unlink(os.path.join(folder(), name))
    except OSError:
        pass


def write(event: str, **fields) -> None:
    """Append one event (never raises: the audit log must not break a request)."""
    now = datetime.now()
    line = {"ts": now.strftime("%Y-%m-%dT%H:%M:%S"), "event": event,
            **{k: v for k, v in fields.items() if v is not None}}
    try:
        os.makedirs(folder(), exist_ok=True)
        with _lock:
            _prune(now.date())
            with open(os.path.join(folder(), f"audit_{now:%Y%m%d}.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def read(day: Optional[date] = None) -> list[dict]:
    """Events of one day (today by default), oldest first: for the Settings page and tests."""
    path = os.path.join(folder(), f"audit_{(day or date.today()):%Y%m%d}.jsonl")
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


class Timer:
    def __init__(self):
        self.t0 = time.perf_counter()

    def ms(self) -> int:
        return int((time.perf_counter() - self.t0) * 1000)
