"""XER-only mode: one or more XER files loaded into a read-only SQLite store with P6's table names,
so every read tool works without a P6 database (and without P6 installed).

- Tables and columns come from the XER itself (%T / %F); column types are inferred (ids and
  quantities numeric, codes, names and dates text) so comparisons behave as in a P6 database.
- Several files can be loaded together (two updates to compare): the ids of file k are shifted by
  k * ID_STEP so they never collide, and a Project ID already taken by an earlier file gets a
  "~2", "~3"... suffix (see the manifest, also in p6_diagnose).
- The XER SCHEDOPTIONS become PROJPROP 'scheduling' as an import writes them (lag calendar, float
  type...); PREFER and NEXTKEY get a minimal row set.
- The store is rebuilt only when a source file changes (key = paths, sizes, modification times) and
  lives in %LOCALAPPDATA%\\P6MCP\\xer_store. Writes are refused in this mode.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime
from typing import Iterable, Optional, Sequence

from p6mcp import config

ID_STEP = 1_000_000_000
STORE_VERSION = 3  # bump when the store layout changes: older stores are rebuilt
KEEP_STORES = 4
TEXT_HINTS = ("_code", "_name", "_type", "_flag", "_label", "_data", "_text", "_memo", "_char", "_date", "_descr",
              "guid", "_url", "_path", "_comments", "_abbrev", "_status")
NUM_HINTS = ("_id", "_cnt", "_qty", "_cost", "_pct", "_num", "_rate", "_amt", "_per_hr", "_wt", "_level",
             "_checksum", "_base", "_step", "float_path", "float_path_order", "udf_number")
INT_HINTS = ("_id", "_num", "_level", "_checksum", "_base", "_step", "float_path", "float_path_order")
_INT = re.compile(r"-?\d+")
_NUM = re.compile(r"-?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


class XerStoreError(RuntimeError):
    """The XER files cannot be loaded."""


def store_dir() -> str:
    return os.path.join(config.localdata_dir(), "xer_store")


def _key(paths: Sequence[str]) -> str:
    parts = [str(STORE_VERSION)]
    for p in paths:
        st = os.stat(p)
        parts.append(f"{os.path.normcase(os.path.abspath(p))}|{st.st_size}|{st.st_mtime_ns}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _col_type(name: str, values: Iterable[Optional[str]]) -> str:
    """P6's own types where the name tells (ids and counters INTEGER, hours, quantities, costs and
    percentages REAL, codes, names, flags and dates TEXT), else what every value parses as."""
    vals = [v for v in values if v is not None]
    numeric_name = name.endswith(NUM_HINTS) or name in NUM_HINTS
    if not numeric_name and (name.endswith(TEXT_HINTS) or name in TEXT_HINTS):
        return "TEXT"
    if vals and not all(_NUM.fullmatch(v) for v in vals):
        return "TEXT"
    if numeric_name:
        return "INTEGER" if name.endswith(INT_HINTS) or name in INT_HINTS else "REAL"
    if vals and all(_INT.fullmatch(v) for v in vals):
        return "INTEGER"
    return "REAL" if vals else "TEXT"


def xer_fields(path: str) -> dict[str, list[str]]:
    """Field names of every %T block, rows or not (the reader keeps only tables with rows)."""
    raw = open(path, "rb").read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("%T\t"):
            cur = line[3:].strip()
        elif line.startswith("%F\t") and cur:
            out.setdefault(cur, [f.strip().lower() for f in line[3:].split("\t")])
    return out


def _convert(v: Optional[str], typ: str):
    if v is None:
        return None
    if typ == "INTEGER":
        return int(v) if _INT.fullmatch(v) else (float(v) if _NUM.fullmatch(v) else v)
    if typ == "REAL":
        return float(v) if _NUM.fullmatch(v) else v
    return v


def _shift(col: str, value, offset: int):
    """Ids of file k move by k * ID_STEP (positive whole numbers only: -99 and 0 mean none)."""
    if offset and isinstance(value, int) and value > 0 and (col.endswith("_id") or col == "fk_id"):
        return value + offset
    return value


def build(paths: Sequence[str], dest: str) -> dict:
    """Load the XER files into a new SQLite database at dest. Returns the manifest."""
    from p6mcp import server as s  # the XER reader and the schedule option writer live in the server

    if not paths:
        raise XerStoreError("No XER file selected.")
    if os.path.exists(dest):
        os.remove(dest)
    con = sqlite3.connect(dest)
    con.row_factory = sqlite3.Row
    manifest = {"version": STORE_VERSION, "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "files": []}
    created: dict[str, dict[str, str]] = {}  # table -> column -> type
    taken: set[str] = set()
    try:
        for k, path in enumerate(paths):
            try:
                header, tables, meta = s._xer_read_meta(path)
            except (OSError, ValueError) as e:
                raise XerStoreError(f"{os.path.basename(path)}: {e}") from e
            if "PROJECT" not in tables or not tables["PROJECT"]:
                raise XerStoreError(f"{os.path.basename(path)} holds no project.")
            offset = k * ID_STEP
            info = {"path": os.path.abspath(path), "xer_version": header[1] if len(header) > 1 else None,
                    "id_offset": offset, "projects": [], "rows": {}, "warnings": []}
            fields = xer_fields(path)
            for t in [t for t in fields if t not in tables]:  # tables without rows keep their columns
                tables[t] = []
            for t, rows in tables.items():
                if t == "SCHEDOPTIONS":
                    continue
                cols = list(rows[0].keys()) if rows else list(fields.get(t, []))
                if not cols:
                    continue
                if t not in created:
                    types = {c: _col_type(c, (r.get(c) for r in rows)) for c in cols}
                    con.execute(f'CREATE TABLE "{t}" ({", ".join(f"{c} {typ}" for c, typ in types.items())})')
                    created[t] = types
                else:
                    for c in cols:
                        if c not in created[t]:
                            typ = _col_type(c, (r.get(c) for r in rows))
                            con.execute(f'ALTER TABLE "{t}" ADD COLUMN {c} {typ}')
                            created[t][c] = typ
                types = created[t]
                data = []
                for r in rows:
                    row = {c: _shift(c, _convert(r.get(c), types[c]), offset) for c in cols}
                    if t == "PROJECT":
                        name = row.get("proj_short_name") or f"PROJECT-{row.get('proj_id')}"
                        new, n = name, 1
                        while new in taken:
                            n += 1
                            new = f"{name}~{n}"
                        taken.add(new)
                        row["proj_short_name"] = new
                        info["projects"].append({"xer": name, "project_id": new, "proj_id": row.get("proj_id")})
                    data.append(row)
                if data:
                    con.executemany(f'INSERT INTO "{t}" ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                                    [tuple(d[c] for c in cols) for d in data])
                info["rows"][t] = len(rows)
            if meta.get("bad_rows"):
                info["warnings"].append(f"rows with a wrong field count: {meta['bad_rows']}")
            info["_tables"] = tables
            manifest["files"].append(info)
        _support_tables(con, s)
        _schedule_options(con, s, manifest)
        con.commit()
    except BaseException:
        con.close()
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    con.close()
    for f in manifest["files"]:
        f.pop("_tables", None)
    return manifest


def _support_tables(con: sqlite3.Connection, s) -> None:
    """Tables an XER does not carry but the read tools expect (empty, or one default row)."""
    have = {r[0].upper() for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "PREFER" not in have:
        con.execute("CREATE TABLE PREFER (prefer_id INTEGER, week_start_day_num INTEGER, name_sep_char TEXT, "
                    "task_code_len INTEGER, proj_short_len INTEGER, day_hr_cnt REAL)")
        con.execute("INSERT INTO PREFER VALUES (1, 1, '.', 20, 40, 8)")
    if "NEXTKEY" not in have:
        con.execute("CREATE TABLE NEXTKEY (key_name TEXT, key_seq_num INTEGER)")
    if "PROJPROP" not in have:
        con.execute("CREATE TABLE PROJPROP (proj_id INTEGER, prop_name TEXT, prop_value TEXT, create_date TEXT, "
                    "create_user TEXT, update_date TEXT, update_user TEXT)")
    for t, cols in (("TASK", ("task_id INTEGER", "proj_id INTEGER", "wbs_id INTEGER", "clndr_id INTEGER",
                              "task_code TEXT", "task_name TEXT", "status_code TEXT", "task_type TEXT")),
                    ("TASKPRED", ("task_pred_id INTEGER", "task_id INTEGER", "pred_task_id INTEGER",
                                  "proj_id INTEGER", "pred_proj_id INTEGER", "pred_type TEXT", "lag_hr_cnt REAL")),
                    ("PROJWBS", ("wbs_id INTEGER", "proj_id INTEGER", "parent_wbs_id INTEGER",
                                 "proj_node_flag TEXT", "wbs_short_name TEXT", "wbs_name TEXT", "seq_num INTEGER")),
                    ("CALENDAR", ("clndr_id INTEGER", "clndr_name TEXT", "day_hr_cnt REAL", "clndr_data TEXT",
                                  "proj_id INTEGER", "base_clndr_id INTEGER"))):
        if t not in have:  # the tables every read tool needs; the others are checked by the tools
            con.execute(f"CREATE TABLE {t} ({', '.join(cols)})")
    have = {r[0].upper() for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for t, c in (("TASK", "task_id"), ("TASK", "task_code"), ("TASKPRED", "task_id"), ("TASKPRED", "pred_task_id"),
                 ("PROJWBS", "wbs_id"), ("TASKRSRC", "task_id"), ("TASKACTV", "task_id"), ("UDFVALUE", "fk_id")):
        if t in have:
            con.execute(f"CREATE INDEX IF NOT EXISTS ix_{t}_{c} ON {t} ({c})")


def _schedule_options(con: sqlite3.Connection, s, manifest: dict) -> None:
    """XER SCHEDOPTIONS -> PROJPROP 'scheduling' of each project, through the server's own writer."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    s._cols_cache.clear()
    try:
        for f in manifest["files"]:
            x = f["_tables"]
            if not x.get("SCHEDOPTIONS"):
                continue
            for p in f["projects"]:
                xp = next((r for r in x["PROJECT"] if _shift("proj_id", int(r["proj_id"]), f["id_offset"])
                           == p["proj_id"]), None)
                if xp is None:
                    continue
                off = f["id_offset"]

                def key_map(raw: str, off=off):
                    return int(raw) + off if _INT.fullmatch(raw) and int(raw) > 0 else None
                try:
                    _res, warn = s._sched_write(con, p["proj_id"], (x, xp, key_map), {}, None, None, now)
                    f["warnings"] += warn
                except (ValueError, sqlite3.Error) as e:
                    f["warnings"].append(f"schedule options of {p['project_id']} not loaded: {e}")
    finally:
        s._cols_cache.clear()  # the cache describes the active database, not this store


def ensure(paths: Sequence[str]) -> tuple[str, dict]:
    """(store path, manifest), building the store when it is missing or a source changed."""
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        raise XerStoreError(f"XER file not found: {missing[0]}")
    folder = store_dir()
    os.makedirs(folder, exist_ok=True)
    key = _key(paths)
    db, man = os.path.join(folder, f"{key}.db"), os.path.join(folder, f"{key}.json")
    if os.path.isfile(db) and os.path.isfile(man):
        with open(man, encoding="utf-8") as fh:
            return db, json.load(fh)
    fd, tmp = tempfile.mkstemp(prefix=".build.", suffix=".db", dir=folder)
    os.close(fd)
    manifest = build(paths, tmp)
    os.replace(tmp, db)
    with open(man, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    _prune(folder, keep={key})
    return db, manifest


def _prune(folder: str, keep: set[str]) -> None:
    stores = sorted((f for f in os.listdir(folder) if f.endswith(".db") and not f.startswith(".")),
                    key=lambda f: os.path.getmtime(os.path.join(folder, f)), reverse=True)
    for f in stores[KEEP_STORES:]:
        if f[:-3] in keep:
            continue
        for ext in (".db", ".json"):
            try:
                os.remove(os.path.join(folder, f[:-3] + ext))
            except OSError:
                pass
