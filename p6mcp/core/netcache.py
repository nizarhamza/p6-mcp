"""Process-wide cache of the loaded schedule network (plan item S7): the CPM engine's network of a project
(_Cpm), its base run, the DCMA critical path test (_cpt) of the stored network and the link network (_Net),
so repeated engine calls in one session stop reloading and re-running the same network while the database has
not changed. Results are the same as without it (P6MCP_ENGINE_CACHE=0 turns it off for diagnosis).

Key: (database file in use, proj_id); the file is the one the read connection opened (EPPM working copy, XER
store or P6 database alike). An entry holds the signature it was built under: the file signature _file_sig taken
before the reading connection opened (so before its snapshot) and facts read in that snapshot (_facts). A value
is served only when the entry's signature equals the caller's (its own file signature and facts) and the file
signature now: no commit reached the file between the two snapshots, whoever wrote it (P6, this server,
another process, rollback-journal or WAL mode). Values are built outside the lock (a miss in two threads at once
may load twice) and published whole; the cache keeps its own objects and the callers get copies (core/engine.py
_cpm_load, _cpm_run, _net_load; core/explain.py _cpt). _state._engine_cache_clear() drops everything after every
confirmed write commit (_commit), a restore, an EPPM pull or push, and when _refresh() selects another database.
Only read-only connections of _connect inside a transaction use it; a connection with temp tables (the what-if's
_temp_network shadows TASK, TASKPRED and PROJECT) never does."""

from __future__ import annotations

import os
from typing import Callable, Optional

from p6mcp.core import state as _state
from p6mcp.core.db import _cols, _file_sig


ENGINE_CACHE_SIZE = 4  # projects kept, least recently used dropped first
ENGINE_CACHE_ENV = "P6MCP_ENGINE_CACHE"  # 0 / false / no / off: no cache (diagnosis)


def _engine_cache_on() -> bool:
    return os.environ.get(ENGINE_CACHE_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def _agg(con, table: str, where: str = "", args: tuple = ()) -> Optional[tuple]:
    """(rows, latest update_date, soft-deleted rows) of a table, None when it is missing."""
    cols = _cols(con, table)
    if not cols:
        return None
    sel = ", ".join(["COUNT(*)"] + [f"{f}({c})" for f, c in (("MAX", "update_date"), ("COUNT", "delete_session_id"))
                                     if c in cols])
    return tuple(con.execute(f"SELECT {sel} FROM {table} x" + (f" WHERE {where}" if where else ""), args).fetchone())


def _facts(con, pid: int) -> Optional[tuple]:
    """What the network depends on, read cheaply in con's snapshot: the PROJECT row (data date, last F9, options,
    calendar), PROJPROP 'scheduling', and rows, latest update_date and soft-deleted rows of the project's TASK,
    TASKPRED (either end in the project), TASKRSRC and PROJWBS, of CALENDAR and of RSRC (resource calendars).
    None when con holds temp tables (they may shadow the database tables): such a connection is never cached."""
    if con.execute("SELECT 1 FROM temp.sqlite_master WHERE type = 'table' LIMIT 1").fetchone():
        return None
    prj = con.execute("SELECT * FROM PROJECT x WHERE proj_id = ?", (pid,)).fetchone()
    pp = _cols(con, "PROJPROP")
    props = tuple(tuple(r) for r in con.execute(
        "SELECT * FROM PROJPROP x WHERE proj_id = ? AND prop_name = 'scheduling' ORDER BY rowid", (pid,))) \
        if {"proj_id", "prop_name"} <= pp else None
    own = "SELECT task_id FROM TASK WHERE proj_id = ?"
    pc = _cols(con, "TASKPRED")
    links = _agg(con, "TASKPRED", "proj_id = ? OR pred_proj_id = ?", (pid, pid)) if {"proj_id", "pred_proj_id"} <= pc \
        else _agg(con, "TASKPRED", f"task_id IN ({own}) OR pred_task_id IN ({own})", (pid, pid))
    asg = _agg(con, "TASKRSRC", "proj_id = ?", (pid,)) if "proj_id" in _cols(con, "TASKRSRC") \
        else _agg(con, "TASKRSRC", f"task_id IN ({own})", (pid,))
    return (None if prj is None else tuple(prj), props, _agg(con, "TASK", "proj_id = ?", (pid,)), links, asg,
            _agg(con, "PROJWBS", "proj_id = ?", (pid,)), _agg(con, "CALENDAR"), _agg(con, "RSRC"))


class _Ticket:
    """A call's claim on the cache for one project: key (file, proj_id), sig (file signature before the
    connection's snapshot, facts read in it) and cpm, the cache's own network once the call took it (copies
    only: _cpm_run builds the base run from it after the snapshot ends)."""
    __slots__ = ("key", "sig", "cpm")

    def __init__(self, key: tuple, sig: tuple):
        self.key, self.sig, self.cpm = key, sig, None


class _Entry:
    __slots__ = ("sig", "parts")

    def __init__(self, sig: tuple):
        self.sig, self.parts = sig, {}


def _count(name: str) -> None:
    with _state._engine_lock:
        _state._engine_stats[name] += 1


def _ticket(con, pid: int) -> Optional[_Ticket]:
    """The cache ticket of a project as con reads it, None when con may not use the cache: cache off, not a
    read connection of _connect, outside a transaction (no single snapshot), temp tables."""
    if not _engine_cache_on():
        return None
    f, fsig = getattr(con, "p6_file", None), getattr(con, "p6_sig", None)
    facts = _facts(con, pid) if f is not None and fsig is not None and con.in_transaction else None
    if facts is None:
        _count("bypassed")
        return None
    return _Ticket((f, pid), (fsig, facts))


def _cached(tk: _Ticket, kind: str, build: Callable[[], object]) -> object:
    """The cache's own value of kind for the ticket's project (never mutate it: hand out copies), built by build()
    when absent and published when the file is still as the caller's connection found it."""
    key, sig = tk.key, tk.sig
    now = _file_sig(key[0])
    with _state._engine_lock:
        e = _state._engine_cache.get(key)
        if e is not None and e.sig == sig and sig[0] == now and kind in e.parts:
            _state._engine_cache.move_to_end(key)
            _state._engine_stats["hits"] += 1
            return e.parts[kind]
        _state._engine_stats["misses"] += 1
    value = build()
    if sig[0] is not None and _file_sig(key[0]) == sig[0]:  # unchanged since before the caller's snapshot
        with _state._engine_lock:
            cache = _state._engine_cache
            e = cache.get(key)
            if e is None or e.sig != sig:
                e = cache[key] = _Entry(sig)
            value = e.parts.setdefault(kind, value)
            cache.move_to_end(key)
            while len(cache) > ENGINE_CACHE_SIZE:
                cache.popitem(last=False)
                _state._engine_stats["evicted"] += 1
    return value


def _peek(tk: _Ticket, kind: str) -> Optional[object]:
    """The cache's value of kind for the ticket when present and valid (counted as a hit), else None (nothing
    built, nothing counted)."""
    now = _file_sig(tk.key[0])
    with _state._engine_lock:
        e = _state._engine_cache.get(tk.key)
        if e is not None and e.sig == tk.sig and tk.sig[0] == now and kind in e.parts:
            _state._engine_cache.move_to_end(tk.key)
            _state._engine_stats["hits"] += 1
            return e.parts[kind]
    return None


def _engine_cache_info() -> dict:
    """p6_diagnose: whether the cache is on, its size and its counters since the server started."""
    with _state._engine_lock:
        st = dict(_state._engine_stats)
        n = len(_state._engine_cache)
    return {"enabled": _engine_cache_on(), "max_projects": ENGINE_CACHE_SIZE, "projects_cached": n,
            **{k: st.get(k, 0) for k in ("hits", "misses", "bypassed", "evicted", "clears")},
            "note": f"Network cache of the CPM engine and link analysis ({ENGINE_CACHE_ENV}=0 turns it off)."}
