"""P6 schema detection.

The version P6 records (PREFER.database_version, e.g. 'PPMDB,2512.0000.0000.0003' = 25.12) is shown,
but the schema is identified by a fingerprint: SHA-256 of the sorted "table.column" names (lower
case) of every table, since a patch can change columns without a new major version. A database
whose fingerprint is known (KNOWN_SCHEMAS) or trusted by the user (config trusted_schemas) can be
written; any other one is read only (writes refused with a warning), as long as the tables the
server reads exist (REQUIRED).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Iterable

# Fingerprint -> label. Add an entry only after the whole test suite passed on that version.
KNOWN_SCHEMAS: dict[str, str] = {
    # 130 tables, PREFER.database_version PPMDB,2512.0000.0000.0003: the test suite's reference (2026-10-19)
    "30da82ab26eb42e927bdcabb59552a67f809d5f6c81af235512f3ab49263e26f": "P6 Professional 25.12 standalone",
}

# Tables and columns every read tool relies on (the others are checked with _cols at run time)
REQUIRED: dict[str, tuple[str, ...]] = {
    "PROJECT": ("proj_id", "proj_short_name", "project_flag"),
    "PROJWBS": ("wbs_id", "proj_id", "parent_wbs_id", "proj_node_flag", "wbs_short_name", "wbs_name"),
    "TASK": ("task_id", "proj_id", "wbs_id", "clndr_id", "task_code", "task_name", "status_code", "task_type"),
    "TASKPRED": ("task_pred_id", "task_id", "pred_task_id", "pred_type", "lag_hr_cnt"),
    "CALENDAR": ("clndr_id", "clndr_name", "day_hr_cnt"),
    "NEXTKEY": ("key_name", "key_seq_num"),
}


def columns(con: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t.upper(): {r[1].lower() for r in con.execute(f'PRAGMA table_info("{t}")')} for t in tables}


def fingerprint_of(cols: dict[str, set[str]]) -> str:
    lines = sorted(f"{t.lower()}.{c}" for t, cs in cols.items() for c in cs)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def missing_required(cols: dict[str, set[str]]) -> list[str]:
    out = []
    for t, need in REQUIRED.items():
        if t not in cols:
            out.append(t)
        else:
            out += [f"{t}.{c}" for c in need if c not in cols[t]]
    return out


def p6_version(con: sqlite3.Connection, cols: dict[str, set[str]]) -> dict:
    """{database_version (raw), version ('25.12'), min_client_version} from PREFER, None when absent."""
    out = {"database_version": None, "version": None, "min_client_version": None}
    pref = cols.get("PREFER", set())
    if "database_version" not in pref:
        return out
    sel = "database_version, " + ("min_pro_version" if "min_pro_version" in pref else "NULL")
    try:
        row = con.execute(f"SELECT {sel} FROM PREFER LIMIT 1").fetchone()
    except sqlite3.Error:
        return out
    if row:
        out["database_version"], out["min_client_version"] = row[0], row[1]
        m = re.search(r"(\d{2})(\d{2})\.\d+", str(row[0] or ""))
        out["version"] = f"{m[1]}.{m[2]}" if m else None
    return out


def detect(con: sqlite3.Connection, trusted: Iterable[str] = ()) -> dict:
    """{fingerprint, label, p6_version, known, trusted, writable, readable, missing, tables}."""
    cols = columns(con)
    fp = fingerprint_of(cols)
    missing = missing_required(cols)
    known = fp in KNOWN_SCHEMAS
    trust = fp in set(trusted)
    ver = p6_version(con, cols)
    label = KNOWN_SCHEMAS.get(fp) or (f"P6 {ver['version']} (schema not tested)" if ver["version"]
                                      else "unknown P6 schema")
    return {"fingerprint": fp, "label": label, "p6_version": ver, "known": known,
            "trusted": trust, "readable": not missing, "writable": not missing and (known or trust),
            "missing": missing, "tables": len(cols)}


def write_refusal(info: dict) -> str:
    if info["missing"]:
        return (f"This file does not look like a P6 Professional database (missing {info['missing'][:8]}). "
                "Writes are refused.")
    return (f"Unknown P6 schema (fingerprint {info['fingerprint'][:12]}): this P6 version was not tested, so "
            "the database is read only. Reads work. To allow writes anyway, accept this schema in MCP for Primavera P6 "
            "Settings (Database, Trust this schema) after backing up the database.")
