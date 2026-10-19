"""Database discovery (suggestions only) and the read-only connection test of the Settings app.

Nothing here selects a database: suggestions() lists candidates for the user to pick, and the
Settings app marks a database usable only after test_connection() succeeds and the user confirms.
"""

from __future__ import annotations

import glob
import os
import re
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Iterable, Optional

from p6mcp import schema

DEFAULT_NAME = "PPMDBSQLite.db"  # name P6 Professional proposes for a standalone database


def _bootstrap_files() -> list[tuple[str, str]]:
    """(version folder, path) of P6 Professional's PrmBootStrapV2.xml files of this user, newest version first."""
    out = []
    for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        root = os.path.join(base, "Oracle", "Primavera P6", "P6 Professional")
        for path in glob.glob(os.path.join(root, "*", "[Pp]rm[Bb]oot[Ss]trap[Vv]2.xml")) + \
                glob.glob(os.path.join(root, "[Pp]rm[Bb]oot[Ss]trap[Vv]2.xml")):
            ver = os.path.basename(os.path.dirname(path))
            out.append(("" if ver == "P6 Professional" else ver, path))

    def key(item):
        return tuple(int(x) for x in re.findall(r"\d+", item[0])) or (0,)
    return sorted(out, key=key, reverse=True)


def bootstrap_sqlite_paths(path: str) -> list[tuple[Optional[str], str]]:
    """(alias, database path) of the SQLite connections defined in one PrmBootStrapV2.xml."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return []
    out = []
    parents = {c: p for p in root.iter() for c in p}
    for el in root.iter():
        kids = {c.tag.lower(): (c.text or "").strip() for c in el}
        if kids.get("drivername", "").lower() == "sqlite" and kids.get("database"):
            alias = kids.get("name")
            parent = parents.get(el)
            if not alias and parent is not None:
                alias = next(((c.text or "").strip() for c in parent if c.tag.lower() == "name"), None)
            out.append((alias or None, kids["database"]))
    return out


def suggestions() -> list[dict]:
    """Candidate databases: P6's own connection list, then the usual default location. Each entry:
    {path, source, exists, size_bytes}. Duplicates are merged (first source kept)."""
    seen, out = set(), []

    def add(path: str, source: str):
        path = os.path.abspath(os.path.expandvars(path))
        k = os.path.normcase(path)
        if k in seen:
            return
        seen.add(k)
        exists = os.path.isfile(path)
        out.append({"path": path, "source": source, "exists": exists,
                    "size_bytes": os.path.getsize(path) if exists else None})

    for ver, bfile in _bootstrap_files():
        for alias, db in bootstrap_sqlite_paths(bfile):
            add(db, f"P6 Professional {ver}".strip() + (f", connection {alias}" if alias else ""))
    docs = os.path.join(os.environ.get("USERPROFILE") or os.path.expanduser("~"), "Documents")
    add(os.path.join(docs, DEFAULT_NAME), "default location")
    return out


def p6_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq PM.exe", "/NH"], capture_output=True, text=True,
                             timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return "PM.exe" in out
    except (OSError, subprocess.SubprocessError):
        return False


def test_connection(path: str, trusted: Iterable[str] = ()) -> dict:
    """Open the file read only and describe it. Never writes. Result keys: ok, error, path,
    size_bytes, schema (schema.detect), p6_version, projects, eps_nodes, baselines, p6_running,
    file_writable, writes_possible, warnings, tested_at."""
    res: dict = {"ok": False, "error": None, "path": os.path.abspath(path) if path else "", "warnings": [],
                 "tested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    if not path or not path.strip():
        res["error"] = "No file selected."
        return res
    path = res["path"]
    if not os.path.isfile(path):
        res["error"] = f"File not found: {path}"
        return res
    res["size_bytes"] = os.path.getsize(path)
    try:
        con = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        res["error"] = f"Cannot open the file: {e}"
        return res
    try:
        con.execute("PRAGMA query_only = ON")
        info = schema.detect(con, trusted)
        res["schema"] = info
        res["p6_version"] = info["p6_version"]["version"]
        if not info["readable"]:
            res["error"] = ("This file is not a P6 Professional database (missing "
                            f"{', '.join(info['missing'][:6])}).")
            return res
        alive = " AND delete_session_id IS NULL" if "delete_session_id" in schema.columns(con).get("PROJECT", ()) \
            else ""
        base = " AND orig_proj_id IS NULL" if "orig_proj_id" in schema.columns(con).get("PROJECT", ()) else ""
        q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
        res["projects"] = q(f"SELECT COUNT(*) FROM PROJECT WHERE COALESCE(project_flag, 'Y') = 'Y'{base}{alive}")
        res["eps_nodes"] = q(f"SELECT COUNT(*) FROM PROJECT WHERE project_flag = 'N'{alive}")
        res["baselines"] = q(f"SELECT COUNT(*) FROM PROJECT WHERE orig_proj_id IS NOT NULL{alive}") if base else 0
    except sqlite3.DatabaseError as e:
        msg = str(e)
        res["error"] = ("This file is not a SQLite database." if "not a database" in msg
                        else f"The database could not be read: {msg}")
        return res
    finally:
        con.close()
    res["ok"] = True
    res["p6_running"] = p6_running()
    res["file_writable"] = os.access(path, os.W_OK)
    res["writes_possible"] = bool(info["writable"] and res["file_writable"])
    if not info["known"] and not info["trusted"]:
        res["warnings"].append("schema_unknown")
    if not res["file_writable"]:
        res["warnings"].append("file_read_only")
    if res["p6_running"]:
        res["warnings"].append("p6_running")
    return res
