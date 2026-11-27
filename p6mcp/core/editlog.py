"""The edit log (one JSON line per logged change) and the staleness of a project's last F9 read from it."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from typing import Callable, Optional, Sequence

from p6mcp import confirm as _confirm

from p6mcp.core import state as _state
from p6mcp.core.state import _log_cache
from p6mcp.registry import _CALL, TOOL_HINTS
from p6mcp.core.db import _alive, _cols


# ----------------------------------------------------------------- edit log (persistent)
# One JSON line per logged change in <db folder>/p6_mcp_logs/edits_YYYYMMDD.jsonl (P6_LOG_DIR overrides
# the folder). An entry names its database (file name), so copies sharing a folder keep apart.
# schedule_state reads it: a project is stale while an entry recorded under its current
# PROJECT.last_schedule_date exists and was not undone by a restore.

def _log_dir() -> str:
    return _state.LOG_DIR or os.path.join(os.path.dirname(os.path.abspath(_state.DB_PATH)), "p6_mcp_logs")


def _log_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _log_dirs() -> list[str]:
    """Folders read for this database's entries: the configured one, then the v2 folder beside the
    database when it differs (history written before the move to the configured folder)."""
    out = [_log_dir()]
    if _state.SETTINGS.legacy_log_dir and os.path.normcase(_state.SETTINGS.legacy_log_dir) != os.path.normcase(out[0]):
        out.append(_state.SETTINGS.legacy_log_dir)
    return out


def _log_write(entries: Sequence[dict]) -> str:
    folder = _log_dir()
    os.makedirs(folder, exist_ok=True)
    ctx = _CALL.get()
    if ctx is not None:  # a client call: record what was asked and which dry run it confirmed
        entries = [dict(e, params=ctx["params"], confirmation=(ctx.get("token") or "")[:12] or None)
                   for e in entries]
    client = _confirm.CLIENT.get()
    if client is not None:  # a remote client (claude.ai, ChatGPT): which connection made the change
        entries = [dict(e, client=client) for e in entries]
    path = os.path.join(folder, f"edits_{datetime.now():%Y%m%d}.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(e, ensure_ascii=False, default=str) + "\n" for e in entries))
    return path


def _log_entries() -> list[dict]:
    """Entries of this database, oldest first (files are cached until they change; an unreadable
    line, e.g. cut by a crash, is skipped)."""
    db, out = os.path.basename(_state.DB_PATH).lower(), []
    files = [os.path.join(folder, name) for folder in _log_dirs() if os.path.isdir(folder)
             for name in sorted(os.listdir(folder)) if re.fullmatch(r"edits_\d{8}\.jsonl", name)]
    for path in files:
        st = os.stat(path)
        hit = _log_cache.get(path)
        if hit is None or hit[0] != (st.st_mtime_ns, st.st_size):
            rows = []
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict) and "ts" in e:
                        rows.append(e)
            hit = _log_cache[path] = ((st.st_mtime_ns, st.st_size), rows)
        out += [e for e in hit[1] if str(e.get("db", "")).lower() == db]
    out.sort(key=lambda e: e["ts"])  # stable: entries of one commit keep their order
    return out


def _undone_fn(entries: Sequence[dict]) -> Callable[[dict], bool]:
    """True for an entry made after a backup that a later p6_restore_backup copied back in."""
    wins = [(e.get("backup_copied_at") or e["backup_taken_at"], e["ts"]) for e in entries
            if e.get("operation") == "restore_backup" and e.get("backup_taken_at")]
    return lambda e: e.get("operation") != "restore_backup" and any(a < e["ts"] < b for a, b in wins)


def _edit_state(pid: int, lsd: Optional[str]) -> Optional[dict]:
    """Confirmed writes that made the project stale under its current last_schedule_date, from the log."""
    es = _log_entries()
    undone, key = _undone_fn(es), str(pid)
    hit = [e for e in es if key in (e.get("stale") or {}) and e["stale"][key] == lsd and not undone(e)]
    if not hit:
        return None
    last = hit[-1]
    return {"at": last["ts"][:19], "change": last.get("change"), "writes": len({e.get("commit") for e in hit}),
            "log_entries": len(hit)}


def _caller_tool() -> Optional[str]:
    """Name of the p6_* tool on the call stack (the one being run)."""
    f = sys._getframe(1)
    while f is not None:
        if f.f_code.co_name in TOOL_HINTS:
            return f.f_code.co_name
        f = f.f_back
    return None


def _schedule_state(con, pid: int, explain: bool = False) -> dict:
    """Data date, last F9 time and whether this server changed the project since that F9, from the
    persistent edit log (entries recorded under the current PROJECT.last_schedule_date: a P6
    reschedule changes it and clears the flag); update_date is never used. note: None when the state
    needs nothing (current F9 recorded, no edit since: its text would only repeat the server
    instructions), the text when it does or with explain=True."""
    cols = _cols(con, "PROJECT")
    sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in ("last_recalc_date", "last_schedule_date"))
    row = con.execute(f"SELECT {sel} FROM PROJECT WHERE proj_id = ? AND COALESCE(project_flag, 'Y') = 'Y'"
                      + _alive(con, "PROJECT"), (pid,)).fetchone()
    if row is None:
        raise ValueError(f"Project id not found: {pid}")
    e = _edit_state(pid, row["last_schedule_date"])
    note = "Dates, float and paths come from the last F9 in P6."
    if e:
        note += " This server changed the schedule after that: open P6 and press F9 before trusting them."
    if not row["last_schedule_date"]:
        note += (" last_schedule_date is empty (never scheduled in a P6 version that records it,"
                 " or imported from an XER without it).")
    note += " Edits are tracked in the edit log next to the database (p6_edit_log)."
    return {"data_date": row["last_recalc_date"], "last_schedule_date": row["last_schedule_date"],
            "stale": bool(e), "edited_since_last_schedule": e,
            "note": note if explain or e or not row["last_schedule_date"] else None}
