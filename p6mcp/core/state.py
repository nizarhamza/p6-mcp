"""Process state of the server: the effective settings (re-read by _refresh() at every tool call), the caches
tied to the active database, the backup state and the read connections of the running tool call.

Other modules read the reassigned globals at call time through this module (`_state.DB_PATH`), never copy
them at import: _refresh() rebinds them."""

from __future__ import annotations

import dataclasses
import os
import threading
from collections import Counter, OrderedDict
from contextvars import ContextVar
from typing import Optional

from p6mcp import config as _config
from p6mcp import pmdb as _pmdb
from p6mcp import xerstore as _xerstore
from p6mcp.eppm import store as _eppm_store


# Effective settings: refreshed by _refresh() at the start of every tool call (config.json may change
# while the server runs). STARTUP = the settings the tool list was built with.
SETTINGS: _config.Settings = _config.load()
STARTUP: _config.Settings = SETTINGS
DB_PATH = SETTINGS.db_path
P6_USER = SETTINGS.p6_user
BACKUP_DIR = SETTINGS.backup_dir
LOG_DIR = SETTINGS.edit_log_dir
SKIP_PROC = not SETTINGS.block_when_p6_running
_backup_done = False
_last_backup: Optional[str] = None  # backup in force for this process's writes (named in the edit log)
_last_pruned: list[str] = []  # backups the retention rule removed after the last backup
_cols_cache: dict[str, set[str]] = {}
_log_cache: dict[str, tuple] = {}  # edit log file -> ((mtime_ns, size), entries)
_schema_cache: dict[str, dict] = {}  # db path -> schema.detect() (trust applied at read time)
XER_MANIFEST: Optional[dict] = None  # XER-only mode: files, projects (Project ID renames), warnings
EPPM_MANIFEST: Optional[dict] = None  # P6 EPPM mode: pull.json of the working copy (server, projects, rows)
PMDB_MANIFEST: Optional[dict] = None  # P6 Professional on SQL Server / Oracle: copy.json of the local copy
# The network cache (core/netcache.py): (database file, proj_id) -> entry, least recently used first; its lock
# (tool calls run on threads in the HTTP and remote servers) and its counters for p6_diagnose.
_engine_cache: "OrderedDict[tuple, object]" = OrderedDict()
_engine_lock = threading.Lock()
_engine_stats: Counter = Counter()


def _engine_cache_clear() -> None:
    """Forget every cached network: a write of this server, a restore, an EPPM pull or another database.
    Entries built meanwhile from an older snapshot never match again (their file signature is gone)."""
    with _engine_lock:
        if _engine_cache:
            _engine_stats["clears"] += 1
        _engine_cache.clear()


def _refresh() -> _config.Settings:
    """Re-read the settings (cheap while config.json is unchanged). A new database clears the caches
    tied to the old one."""
    global SETTINGS, DB_PATH, P6_USER, BACKUP_DIR, LOG_DIR, SKIP_PROC, _backup_done, _last_backup, XER_MANIFEST
    global EPPM_MANIFEST, PMDB_MANIFEST
    st = _config.load()
    XER_MANIFEST = EPPM_MANIFEST = PMDB_MANIFEST = None
    if st.pmdb:  # P6 Professional on SQL Server / Oracle: the local read-only copy, once there is one
        PMDB_MANIFEST = _pmdb.read_manifest(st.pmdb)
        if PMDB_MANIFEST:
            st = dataclasses.replace(st, db_path=_pmdb.store_path(st.pmdb), db_problem=None)
    if st.eppm:  # P6 EPPM mode: the working copy of the pulled projects, once there is one
        EPPM_MANIFEST = _eppm_store.read_manifest(st.eppm)
        if EPPM_MANIFEST:
            st = dataclasses.replace(st, db_path=_eppm_store.store_path(st.eppm), db_problem=None)
    if st.xer_files:  # XER-only mode: the read-only store built from the files (cached until they change)
        try:
            path, XER_MANIFEST = _xerstore.ensure(st.xer_files)
            st = dataclasses.replace(st, db_path=path)
        except (_xerstore.XerStoreError, OSError, ValueError) as e:
            st = dataclasses.replace(st, db_problem=f"XER-only mode: the XER files cannot be loaded: {e}")
    if os.path.normcase(st.db_path) != os.path.normcase(DB_PATH):
        _cols_cache.clear()
        _schema_cache.clear()
        _engine_cache_clear()
        _backup_done, _last_backup = False, None
    SETTINGS, DB_PATH, P6_USER, BACKUP_DIR, LOG_DIR = st, st.db_path, st.p6_user, st.backup_dir, st.edit_log_dir
    SKIP_PROC = not st.block_when_p6_running
    return st


def _shop_on(st: _config.Settings) -> bool:
    """The skill shop setting (on when no config file says otherwise)."""
    return st.raw.get("shop_enabled", True) is not False


_ro_cons: ContextVar[Optional[list]] = ContextVar("_ro_cons", default=None)  # read connections of the running tool
