"""Database access: connections, schema tolerance (_cols, _alive), activity rows, dates, paging, backups and
the generic write helpers (NEXTKEY, insert / update, stamps)."""

from __future__ import annotations

import functools
import math
import os
import re
import sqlite3
import subprocess
import sys
import urllib.parse
from contextlib import closing
from datetime import datetime
from typing import Callable, Iterable, Mapping, Optional, Sequence, get_args

from p6mcp import config as _config
from p6mcp import confirm as _confirm
from p6mcp import policy as _policy
from p6mcp import schema as _schema

from p6mcp.core import state as _state
from p6mcp.core.state import _cols_cache, _ro_cons, _schema_cache
from p6mcp.core.constants import _cstr_label, HPD_SQL, MAX_ROWS, SQL_PRAGMAS, XER_READ_ONLY
from p6mcp.registry import ACT_FIELDS, ACT_HOURS, DATETIME_PATTERN, Detail


_SQL_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                getattr(sqlite3, "SQLITE_RECURSIVE", 33)}


_SQL_SECRET_COL = re.compile(r"pass(wd|word)|pwd", re.I)  # USERS.passwd, email_srv_passwd, EXTAPP, EXPPROJ...


def _sql_authorizer(action: int, arg1, arg2, dbname, source) -> int:
    """p6_sql_select: only reading is authorized (no write, ATTACH, schema change or PRAGMA that sets). For a remote
    client (claude.ai, ChatGPT) credential columns read as NULL: their values would leave the PC."""
    if action == sqlite3.SQLITE_READ and _confirm.CLIENT.get() is not None and _SQL_SECRET_COL.search(str(arg2)):
        return sqlite3.SQLITE_IGNORE
    if action in _SQL_ALLOWED:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and str(arg1).lower() in SQL_PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _schema_info() -> dict:
    """schema.detect() of the current database (cached), with the user's trusted schemas applied."""
    info = _schema_cache.get(_state.DB_PATH)
    if info is None:
        con = _connect()
        try:
            info = _schema_cache[_state.DB_PATH] = _schema.detect(con)
        finally:
            con.close()
    trusted = info["fingerprint"] in _state.SETTINGS.trusted_schemas
    return dict(info, trusted=trusted, writable=info["readable"] and (info["known"] or trusted))


def _write_refusal() -> Optional[str]:
    """Why this database may not be written now (settings and schema), None when it may."""
    if _state.SETTINGS.xer_files:
        return XER_READ_ONLY
    if _state.SETTINGS.eppm:  # the working copy is ours: no schema gate, writes as the settings allow
        if not _state.EPPM_MANIFEST:
            return "Nothing pulled yet from the P6 EPPM server: pull a project with p6_eppm_pull first."
        return _policy.write_refusal(_state.SETTINGS.tool_profile, _state.SETTINGS.allow_writes)
    why = _policy.write_refusal(_state.SETTINGS.tool_profile, _state.SETTINGS.allow_writes)
    if why:
        return why
    info = _schema_info()
    return None if info["writable"] else _schema.write_refusal(info)


# ----------------------------------------------------------------- helpers

def _connect(readonly: bool = True) -> sqlite3.Connection:
    if not _state.DB_PATH:
        raise RuntimeError(_state.SETTINGS.db_problem or _config.NO_DB_MESSAGE)
    if not os.path.isfile(_state.DB_PATH):
        raise RuntimeError(f"The configured database was not found: {_state.DB_PATH}. Open MCP for Primavera P6 Settings to select "
                           "another one.")
    if readonly:
        con = sqlite3.connect(_ro_uri(_state.DB_PATH), uri=True, timeout=5)
        con.execute("PRAGMA query_only = ON")  # second lock besides mode=ro: no statement may write
        opened = _ro_cons.get()
        if opened is not None:  # inside a tool call: closed when the tool returns
            opened.append(con)
    else:
        con = sqlite3.connect(_state.DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _ro_uri(path: str) -> str:
    """Read-only SQLite URI of a file path ('#', '%', '?' and spaces escaped)."""
    return "file:" + urllib.parse.quote(path.replace("\\", "/"), safe="/:") + "?mode=ro"


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    if table not in _cols_cache:
        _cols_cache[table] = {r[1].lower() for r in con.execute(f"PRAGMA table_info({table})")}
    return _cols_cache[table]


def _alive(con: sqlite3.Connection, table: str, alias: str = "") -> str:
    """Filter for logically deleted rows, if the table has the column."""
    p = f"{alias}." if alias else ""
    return f" AND {p}delete_session_id IS NULL" if "delete_session_id" in _cols(con, table) else ""


def _dict(r: sqlite3.Row) -> dict:
    """Row -> dict with lowercase keys (P6 stores column names in upper case)."""
    return {k.lower(): r[k] for k in r.keys()}


def _now_str(con: sqlite3.Connection) -> str:
    """Match the date format already used in the database."""
    sample = con.execute(
        "SELECT update_date FROM TASK WHERE update_date IS NOT NULL LIMIT 1"
    ).fetchone()
    now = datetime.now()
    if sample and isinstance(sample[0], str) and "T" in sample[0]:
        return now.strftime("%Y-%m-%dT%H:%M:%S")
    return now.strftime("%Y-%m-%d %H:%M:%S")


def _p6_running() -> bool:
    if _state.SKIP_PROC or sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq PM.exe", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return "PM.exe" in out
    except Exception:
        return False


def _backup_dir() -> str:
    return _state.BACKUP_DIR or os.path.join(os.path.dirname(_state.DB_PATH), "p6_mcp_backups")


def _prune_backups(keep: int, protect: Iterable[str] = ()) -> list[str]:
    """Retention: keep the newest `keep` automatic backups of this database (plain backups, named
    <db stem>_<stamp>[_n].db); pre_restore and other kinds are never removed. keep 0 = keep all.
    Returns the files removed."""
    if keep <= 0:
        return []
    folder, stem = _backup_dir(), os.path.splitext(os.path.basename(_state.DB_PATH))[0].lower()
    protect = {os.path.normcase(os.path.abspath(p)) for p in protect if p}
    plain = []
    for name in os.listdir(folder):
        m = BACKUP_NAME.search(name)
        if m and not m[2] and name.lower()[:-len(m[0])] == stem:
            plain.append((m[1], name))
    removed = []
    for _, name in sorted(plain, reverse=True)[keep:]:
        path = os.path.join(folder, name)
        if os.path.normcase(os.path.abspath(path)) in protect:
            continue
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass  # in use or read only: kept, tried again after the next backup
    return removed


def _backup(kind: str = "") -> str:
    """Copy of the database in the backup folder: <db name>_<YYYYMMDD_HHMMSS>[_kind].db (never
    overwrites: _2, _3... appended)."""
    folder = _backup_dir()
    os.makedirs(folder, exist_ok=True)
    base = f"{os.path.splitext(os.path.basename(_state.DB_PATH))[0]}_{datetime.now():%Y%m%d_%H%M%S}" + (f"_{kind}" if kind else "")
    dest, n = os.path.join(folder, base + ".db"), 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(folder, f"{base}_{n}.db")
    try:
        with closing(_connect(readonly=True)) as src, closing(sqlite3.connect(dest)) as dst:
            src.backup(dst)  # consistent copy even if the file is busy
    except BaseException:
        if os.path.exists(dest):  # a partial copy must not look like a usable backup
            os.remove(dest)
        raise
    _state._backup_done, _state._last_backup = True, dest
    if not kind:
        _state._last_pruned = _prune_backups(_state.SETTINGS.backup_retention, protect=[dest])
    return dest


def _guard_write() -> Optional[str]:
    if _state.SETTINGS.eppm:  # P6 never opens the working copy of a P6 EPPM server
        return None
    if _p6_running():
        return "P6 (PM.exe) is running. Close P6 completely, then retry."
    return None


def _next_key(con: sqlite3.Connection, table: str, id_col: str) -> int:
    """Reserve the next ID from NEXTKEY (key <table>_<id_col>, case ignored), never below MAX(id)+1.
    No loose match: a suffix match could pick, and then lower, another table's counter."""
    row = con.execute(
        "SELECT key_name, key_seq_num FROM NEXTKEY WHERE lower(key_name) = lower(?)",
        (f"{table}_{id_col}",),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No NEXTKEY entry found for {table}.{id_col}; run p6_diagnose.")
    max_id = con.execute(f"SELECT COALESCE(MAX({id_col}),0) FROM {table}").fetchone()[0]
    new_id = max(int(row["key_seq_num"]), int(max_id) + 1)
    con.execute(
        "UPDATE NEXTKEY SET key_seq_num = ? WHERE key_name = ?", (new_id + 1, row["key_name"])
    )
    return new_id


def _project_id(con, project: str) -> int:
    r = con.execute(
        "SELECT proj_id FROM PROJECT WHERE proj_short_name = ?" + _alive(con, "PROJECT"),
        (project,),
    ).fetchone()
    if not r:
        raise ValueError(f"Project not found: {project}")
    return r[0]


def _task(con, project: str, task_code: str) -> sqlite3.Row:
    pid = _project_id(con, project)
    r = con.execute(
        f"SELECT t.*, {HPD_SQL} AS hpd FROM TASK t "
        "LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id "
        "WHERE t.proj_id = ? AND t.task_code = ?" + _alive(con, "TASK", "t"),
        (pid, task_code),
    ).fetchone()
    if not r:
        raise ValueError(f"Activity {task_code} not found in project {project}")
    return r


def _days(hours, hpd) -> Optional[float]:
    if hours is None:
        return None
    return round(float(hours) / float(hpd or 8), 2)


def _act_view(r: sqlite3.Row) -> dict:
    """Activity row with every ACT_FIELDS key (None when the column is missing); hours -> days
    with the activity calendar (hpd)."""
    d = _dict(r)
    hpd = d.get("hpd") or 8
    d["cstr_label"] = _cstr_label(d.get("cstr_type"))
    return {k: _days(d.get(ACT_HOURS[k]), hpd) if k in ACT_HOURS else d.get(k) for k in ACT_FIELDS}


def _brief_on(detail: str) -> bool:
    """detail= of the activity tools: 'full' (every key, the fixed shape) or 'brief' (no null values)."""
    if detail not in get_args(Detail):
        raise ValueError(f"detail must be one of {list(get_args(Detail))}")
    return detail == "brief"


def _brief(d: Mapping) -> dict:
    """A row without its null values (detail='brief')."""
    return {k: v for k, v in d.items() if v is not None}


def _act_row(fields: Optional[Sequence[str]] = None, detail: str = "full") -> Callable[[sqlite3.Row], dict]:
    """Row builder for activity lists: _act_view, or task_code plus the fields asked for (checked
    before any SQL runs); detail='brief' leaves the null values out."""
    brief = _brief_on(detail)
    if fields is None:
        return (lambda r: _brief(_act_view(r))) if brief else _act_view
    fields = [fields] if isinstance(fields, str) else list(fields)
    bad = [f for f in fields if f not in ACT_FIELDS]
    if bad:
        raise ValueError(f"Unknown field(s) {bad}; allowed: {list(ACT_FIELDS)}")
    keep = list(dict.fromkeys(["task_code", *fields]))

    def row_fn(r: sqlite3.Row) -> dict:
        v = _act_view(r)
        return {k: v[k] for k in keep if not (brief and v[k] is None)}
    return row_fn


def _act_from(con) -> str:
    """Base of every activity list: TASK t with hpd (CALENDAR c) and wbs_name (PROJWBS w).
    One parameter: proj_id. Add conditions with AND, then ORDER BY ending with t.task_id."""
    return (f"SELECT t.*, {HPD_SQL} AS hpd, w.wbs_name AS wbs_name FROM TASK t "
            "LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id "
            "LEFT JOIN PROJWBS w ON w.wbs_id = t.wbs_id" + _alive(con, "PROJWBS", "w")
            + " WHERE t.proj_id = ?" + _alive(con, "TASK", "t"))


def _bound(value: str, field: str, end: bool) -> str:
    """User date -> DB date string for a >= (start) or <= (end, whole day when no time) filter."""
    dt, has_time = _parse_dt(value, field)
    return _fmt_dt(dt) if has_time or not end else dt.strftime("%Y-%m-%d 23:59:59")


def _parse_dt(value: str, field: str) -> tuple[datetime, bool]:
    """User date -> (datetime, has_time). Accepts YYYY-MM-DD, YYYY-MM-DD HH:MM[:SS], 'T' allowed."""
    v = str(value).strip()
    if re.match(DATETIME_PATTERN, v):
        v = v.replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(v, fmt), fmt != "%Y-%m-%d"
            except ValueError:
                pass
    raise ValueError(f"{field}: {value!r} is not a date. Use 'YYYY-MM-DD HH:MM'.")


_DB_DT_SHAPE = re.compile(r"\d{4}-\d\d-\d\d(?: \d\d:\d\d(?::\d\d)?)?\Z")


def _db_dt(v) -> Optional[datetime]:
    """DB or XER date value -> datetime ('T' or space, with or without seconds); None if empty."""
    if v is None or isinstance(v, datetime):
        return v
    return _db_dt_text(str(v).strip().replace("T", " ")[:19])


@functools.lru_cache(maxsize=65536)
def _db_dt_text(s: str) -> Optional[datetime]:
    """Cached: an engine load reads the same few thousand date strings tens of thousands of times
    (strptime was the largest single cost of every engine tool). The zero-padded forms P6 writes
    go through fromisoformat; anything else keeps the strptime rules."""
    if _DB_DT_SHAPE.match(s):
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _baseline_pid(con, pid: int, baseline: Optional[str]) -> Optional[int]:
    """Project to compare with: the Project ID given (baselines included), else the project
    baseline (PROJECT.sum_base_proj_id), else None."""
    if baseline:
        r = con.execute("SELECT proj_id FROM PROJECT WHERE proj_short_name = ? "
                        "AND COALESCE(project_flag, 'Y') = 'Y'" + _alive(con, "PROJECT"), (baseline,)).fetchone()
        if not r:
            raise ValueError(f"Project or baseline not found: {baseline}")
        return r[0]
    if "sum_base_proj_id" not in _cols(con, "PROJECT"):
        return None
    r = con.execute("SELECT b.proj_id FROM PROJECT p JOIN PROJECT b ON b.proj_id = p.sum_base_proj_id "
                    "WHERE p.proj_id = ? AND b.proj_id <> p.proj_id" + _alive(con, "PROJECT", "b"),
                    (pid,)).fetchone()
    return r[0] if r else None


# ----------------------------------------------------------------- paging

def _check_page(offset: int, limit: int) -> tuple[int, int]:
    offset, limit = int(offset), int(limit)
    if offset < 0:
        raise ValueError("offset must be 0 or more")
    if limit < 1:
        raise ValueError("limit must be 1 or more")
    return offset, min(limit, MAX_ROWS)


def _env(total: Optional[int], offset: int, rows: list[dict], more: Optional[bool] = None, **extra) -> dict:
    """List envelope. next_offset is None on the last page (more= decides when total is unknown)."""
    end = offset + len(rows)
    nxt = end if (end < total if total is not None else more) else None
    return {"total": total, "offset": offset, "returned": len(rows), "next_offset": nxt, "rows": rows, **extra}


def _page(con, sql: str, args: Sequence | Mapping = (), offset: int = 0, limit: int = MAX_ROWS,
          row_fn: Callable[[sqlite3.Row], dict] = _dict) -> dict:
    """COUNT and one page of sql (which must carry its ORDER BY) in one read snapshot."""
    offset, limit = _check_page(offset, limit)
    own = not con.in_transaction
    if own:
        con.execute("BEGIN")
    try:
        total = con.execute(f"SELECT COUNT(*) FROM (\n{sql}\n)", args).fetchone()[0]
        rows = [row_fn(r) for r in con.execute(f"{sql}\nLIMIT {limit} OFFSET {offset}", args)]
    finally:
        if own:
            con.rollback()
    return _env(total, offset, rows)


def _slice(rows: list[dict], offset: int, limit: int) -> dict:
    """Same envelope for a list built in Python."""
    offset, limit = _check_page(offset, limit)
    return _env(len(rows), offset, rows[offset:offset + limit])


# ----------------------------------------------------------------- analysis: DCMA 14, progress, compare

def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _stamp_cols(con, table: str, now: str, create: bool) -> dict:
    cols = _cols(con, table)
    d = {}
    if "update_date" in cols:
        d["update_date"] = now
    if "update_user" in cols:
        d["update_user"] = _state.P6_USER
    if create:
        if "create_date" in cols:
            d["create_date"] = now
        if "create_user" in cols:
            d["create_user"] = _state.P6_USER
    return d


# ----------------------------------------------------------------- edit log, backups and restore

BACKUP_NAME = re.compile(r"_(\d{8}_\d{6})(?:_([a-z_]+?))?(?:_\d+)?\.db$", re.I)


def _backup_taken_at(path: str) -> str:
    """Time a backup was taken: the stamp of its file name, else its modification time."""
    m = BACKUP_NAME.search(os.path.basename(path))
    if m:
        return datetime.strptime(m[1], "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")


def _backup_file(name: str) -> str:
    """Path of a backup named as p6_list_backups shows it (a bare file name in the backup folder)."""
    if not name or os.path.basename(name) != name or not name.lower().endswith(".db"):
        raise ValueError("name: a .db file name from p6_list_backups (no folder)")
    path = os.path.join(_backup_dir(), name)
    if not os.path.isfile(path):
        raise ValueError(f"Backup not found in {_backup_dir()}: {name}")
    if os.path.abspath(path) == os.path.abspath(_state.DB_PATH):
        raise ValueError("That file is the database itself")
    return path


def _projects_of(path: str) -> dict[str, tuple]:
    """Project ID -> (data date, last schedule date) of a database file (read only), EPS nodes left out."""
    con = sqlite3.connect(_ro_uri(path), uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        tables = {r[0].upper() for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = {"PROJECT", "TASK", "TASKPRED", "PROJWBS"} - tables
        if missing:
            raise ValueError(f"{os.path.basename(path)} is not a P6 database (no {sorted(missing)})")
        cols = {r[1].lower() for r in con.execute("PRAGMA table_info(PROJECT)")}
        sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in ("last_recalc_date", "last_schedule_date"))
        alive = " AND delete_session_id IS NULL" if "delete_session_id" in cols else ""
        return {r[0]: (r[1], r[2]) for r in con.execute(
            f"SELECT proj_short_name, {sel} FROM PROJECT WHERE COALESCE(project_flag, 'Y') = 'Y'{alive}")}
    finally:
        con.close()


def _prefer(con, col: str) -> Optional[int]:
    """A PREFER setting such as task_code_len, None when this P6 version has no such column."""
    if col not in _cols(con, "PREFER"):
        return None
    r = con.execute(f"SELECT {col} FROM PREFER").fetchone()
    return int(r[0]) if r and r[0] else None


def _insert(con, table: str, row: dict) -> None:
    row = {k: v for k, v in row.items() if k in _cols(con, table)}
    con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                list(row.values()))


def _update(con, table: str, where: dict, row: dict) -> None:
    row = {k: v for k, v in row.items() if k in _cols(con, table) and k not in where}
    if row:
        con.execute(f"UPDATE {table} SET {', '.join(k + '=?' for k in row)} WHERE "
                    + " AND ".join(k + "=?" for k in where), [*row.values(), *where.values()])


def _ids_sql(ids) -> str:
    return "(" + ",".join(str(int(i)) for i in ids) + ")" if ids else "(NULL)"


def _all_rows(fn: Callable[..., dict], cap: int, **kw) -> tuple[dict, list[dict], bool]:
    """Rows of every page of a list tool, up to cap: (first page, rows, cut at cap)."""
    first = res = fn(offset=0, limit=MAX_ROWS, **kw)
    rows = list(res["rows"])
    while res["next_offset"] is not None and len(rows) < cap:
        res = fn(offset=res["next_offset"], limit=MAX_ROWS, **kw)
        rows += res["rows"]
    return first, rows[:cap], len(rows) > cap or res["next_offset"] is not None
