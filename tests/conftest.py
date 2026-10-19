"""Shared fixtures: a synthetic database per test and a config file the server re-reads on each call.

The server module is imported once, with P6MCP_CONFIG pointing at a temporary config file. Tests
change the settings by rewriting that file (P6 helper below); tool calls go through
server.REGISTERED, the same gated callables MCP clients reach.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
