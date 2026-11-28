"""Shared fixtures: a synthetic database per test and a config file the server re-reads on each call.

The server module is imported once, with P6MCP_CONFIG pointing at a temporary config file. Tests
change the settings by rewriting that file (P6 helper below); tool calls go through
server.REGISTERED, the same gated callables MCP clients reach.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # Settings window tests run without a display
for _k in ("CODEX_HOME", "CONTINUE_GLOBAL_DIR"):  # client folders outside the fake profiles of the tests
    os.environ.pop(_k, None)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))

from make_db import build  # noqa: E402

ENV_KEYS = ("P6_DB_PATH", "P6_USER", "P6_BACKUP_DIR", "P6_LOG_DIR", "P6_SKIP_PROCESS_CHECK", "P6MCP_TOOL_PROFILE",
            "P6MCP_ALLOW_WRITES", "P6MCP_CONFIG")
_SESSION_DIR = None


def _session_dir(tmp_path_factory):
    global _SESSION_DIR
    if _SESSION_DIR is None:
        _SESSION_DIR = tmp_path_factory.mktemp("p6mcp_session")
    return _SESSION_DIR


@pytest.fixture(scope="session")
def golden(tmp_path_factory):
    return build(str(_session_dir(tmp_path_factory) / "golden.db"))


@pytest.fixture(scope="session")
def server(tmp_path_factory, golden):
    """The server module, imported with a full profile and writes allowed (every tool listed)."""
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    cfg = _session_dir(tmp_path_factory) / "config.json"
    os.environ["P6MCP_CONFIG"] = str(cfg)
    os.environ["P6_SKIP_PROCESS_CHECK"] = "1"
    from p6mcp import config
    config.save({"tool_profile": "full", "allow_writes": True}, str(cfg))
    import p6mcp.server as s
    yield s
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


PORT_SLOTS = 64  # free_port: a worker only takes ports whose remainder is its own slot


def free_port() -> int:
    """A port no other test of this run can take, for a test that starts a server.

    Never a fixed start number with settings_logic.free_port (it checks, then the server binds later): pytest-xdist
    workers, or two runs on the same machine, would race for the same port, and uvicorn's SO_REUSEADDR lets a second
    server bind it on Windows without an error. The OS picks a free port (bind to port 0); a worker keeps only ports
    whose remainder modulo PORT_SLOTS is its worker number, so two workers of one run never get the same port."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    slot = int(worker[2:]) % PORT_SLOTS if worker[2:].isdigit() else 0
    for _ in range(PORT_SLOTS * 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port % PORT_SLOTS == slot:
            return port
    raise RuntimeError("no free port")


def sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def fingerprint(path: str) -> str:
    from p6mcp import schema
    con = sqlite3.connect(path)
    try:
        return schema.detect(con)["fingerprint"]
    finally:
        con.close()


class P6:
    """One test's database, folders and settings."""

    def __init__(self, server, tmp_path, golden):
        from p6mcp import config
        from p6mcp.core import state
        state.OVERRIDE = None  # p6_use_database: a switch never outlives its test
        self.s, self.config = server, config
        self.db = str(tmp_path / "demo.db")
        shutil.copyfile(golden, self.db)
        self.backups = str(tmp_path / "backups")
        self.logs = str(tmp_path / "editlog")
        self.cfg = {"databases": [{"name": "demo", "kind": "sqlite", "path": self.db, "confirmed": True}],
                    "active_database": "demo", "tool_profile": "full", "allow_writes": True,
                    "backup_dir": self.backups, "edit_log_dir": self.logs, "backup_retention": 10,
                    "trusted_schemas": [fingerprint(golden)]}
        self.set()

    def set(self, **changes) -> None:
        self.cfg.update(changes)
        self.config.save(self.cfg, os.environ["P6MCP_CONFIG"])
        self.s._refresh()

    def call(self, tool: str, **kw):
        return self.s.REGISTERED[tool](**kw)

    def apply(self, tool: str, **kw):
        """Dry run, then apply with the token (and the confirmation text when asked)."""
        dry = self.call(tool, **kw)
        assert dry.get("dry_run") and dry.get("confirmation_token"), dry
        extra = {"confirmation_text": dry["confirmation_text_required"]} if "confirmation_text_required" in dry else {}
        return self.call(tool, **kw, dry_run=False, confirmation_token=dry["confirmation_token"], **extra)

    def sql(self, q: str, *args):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(q, args).fetchall()
        finally:
            con.close()

    def hash(self) -> str:
        return sha(self.db)


@pytest.fixture
def p6(server, tmp_path, golden):
    return P6(server, tmp_path, golden)


# markers (registered in pyproject.toml) set from the fixtures a test uses, so a new test gets them too
FIXTURE_MARKS = {"live": "slow",  # every fixture named live runs a real uvicorn server in a thread
                 "child_env": "slow",  # tests/test_remote_cloudflare.py: real child processes
                 "qtbot": "qt"}


def pytest_collection_modifyitems(config, items):
    for item in items:
        for name in sorted({FIXTURE_MARKS[f] for f in getattr(item, "fixturenames", ()) if f in FIXTURE_MARKS}):
            item.add_marker(name)


def pytest_sessionfinish(session, exitstatus):
    """Delete the Qt widgets the tests left while Python is fully alive.

    pytest-qt closes a test's widgets with deleteLater, which only an event loop runs: the last Settings window
    and its popups, the tray menu and approval windows were still alive at exit (11 top-level, 540 widgets in
    all). PySide6 then destroyed them with the QApplication in its atexit handler, which crashed now and then
    (access violation after "N passed", reported by the CI as exit code 1 with no error text)."""
    qw = sys.modules.get("PySide6.QtWidgets")
    if qw is None or qw.QApplication.instance() is None:
        return
    import gc

    import shiboken6
    from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool
    QThreadPool.globalInstance().waitForDone(30000)  # no Job delivers its result to a deleted page
    for w in qw.QApplication.topLevelWidgets():  # no close(): a close event could ask about unsaved changes
        if shiboken6.isValid(w):
            w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()
    gc.collect()
