"""P6 Professional on SQL Server or Oracle (plan item P1): a read-only local copy of the projects the user chose.

P6 Professional connected to a SQL Server or Oracle database uses the PMDB schema, the one its standalone SQLite
database has too, so the chosen projects, their baselines and the global tables the read tools need are copied into
a local SQLite file with P6's table and column names, and every read tool runs on it unchanged. The database server
is never written: only SELECT statements run there (and, on Oracle, SET TRANSACTION READ ONLY, which makes every
table of one copy read from the same point in time).

The SQL side is the P6 EPPM mode's (p6mcp.eppm.sql: pymssql or oracledb installed on demand by p6mcp.eppm.drivers,
`connect`, `table` with the schema / owner, the parameter style and one-row limit of each dialect) and so is the
copy of one table (p6mcp.eppm.store `_copy_table`: driver values to the standalone database's forms, LOBs read,
IN lists cut to Oracle's 1000 items, number columns typed). What is EPPM-specific stays out: no REST, no project
rights read from the server (the database account's rights decide), no schedule options from REST (PROJPROP is
copied as P6 Professional stores it), no local ids, pending changes or push.

Profile (config `pmdb_profiles`, p6mcp.config._check_pmdb): name, driver mssql / oracle, host, port, service (the
SQL Server database or the Oracle service name), schema (the owner, e.g. dbo or ADMUSER; empty = the account's
default), user (database login; its password in the Credential Manager, `credentials.pmdb_key`), tls, projects
(Project IDs), eps (EPS IDs: every project below, templates and baselines left out), baselines (current: each
project's baseline; all: every baseline of the project; none). Files: %LOCALAPPDATA%\\P6MCP\\pmdb\\<profile>\\
pmdb_<profile>.db and copy.json beside it (when, what, rows, warnings). A copy fills a temporary file, then replaces
the old one, so a failed copy leaves the previous copy in place.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime
from typing import Any, Callable, Optional

from p6mcp import config
from p6mcp.eppm import sql as _sql
from p6mcp.eppm import store as _store

MANIFEST_FILE = "copy.json"
STORE_VERSION = 1
CONNECTION_KEYS = ("driver", "host", "port", "service", "schema", "user", "tls")
SELECTION_KEYS = ("projects", "eps", "baselines")
BASELINES = ("current", "all", "none")
DEFAULT_PORTS = {"mssql": 1433, "oracle": 1521}
DEFAULT_SCHEMA = {"mssql": "dbo", "oracle": "ADMUSER"}
PARAMS_MAX = 500  # parameters of one statement (SQL Server takes 2100)
NODE_EPS = "N"  # PROJECT.project_flag of an EPS node
TEMPLATE = "WS_Template"

# (table, WHERE template, optional): {S} = the copied project ids (baselines included), {TABLE} = a qualified name.
# Global tables whole; project tables for the copied ids; links only when both ends are copied.
_EPS_IDS = "SELECT proj_id FROM {PROJECT} WHERE project_flag = 'N'"
_TYPES = f"proj_id IS NULL OR proj_id IN ({{S}}) OR proj_id IN ({_EPS_IDS})"
TABLES: tuple[tuple[str, str, bool], ...] = (
    ("PROJECT", "proj_id IN ({S}) OR project_flag = 'N'", False),
    ("PROJWBS", f"proj_id IN ({{S}}) OR proj_id IN ({_EPS_IDS})", False),
    ("TASK", "proj_id IN ({S})", False),
    ("TASKPRED", "proj_id IN ({S}) AND pred_proj_id IN ({S})", False),
    ("CALENDAR", "proj_id IS NULL OR proj_id IN ({S})", False),
    ("PROJPROP", "proj_id IN ({S})", True),
    ("TASKRSRC", "proj_id IN ({S})", True),
    ("RSRC", "rsrc_id IN (SELECT rsrc_id FROM {TASKRSRC} WHERE proj_id IN ({S}))", True),
    ("RSRCRATE", "rsrc_id IN (SELECT rsrc_id FROM {TASKRSRC} WHERE proj_id IN ({S}))", True),
    ("ROLES", "", True),  # the role dictionary is small: whole (resources' primary roles, the XER export)
    ("ROLERATE", "", True),
    ("RSRCCURV", "", True),
    ("UMEASURE", "", True),
    ("CURRTYPE", "", True),
    ("OBS", "", True),
    ("ACTVTYPE", _TYPES, True),
    ("ACTVCODE", f"actv_code_type_id IN (SELECT actv_code_type_id FROM {{ACTVTYPE}} WHERE {_TYPES})", True),
    ("TASKACTV", "proj_id IN ({S})", True),
    ("UDFTYPE", "", True),
    ("UDFCODE", "", True),
    ("UDFVALUE", "proj_id IN ({S})", True),
    ("MEMOTYPE", "", True),
    ("TASKMEMO", "proj_id IN ({S})", True),
    ("TASKPROC", "proj_id IN ({S})", True),
    ("WBSSTEP", "proj_id IN ({S})", True),
    ("COSTTYPE", "", True),
    ("PROJCOST", "proj_id IN ({S})", True),
    ("TRSRCFIN", "proj_id IN ({S})", True),
    ("FINDATES", "", True),
    ("RISKTYPE", "", True),
    ("PROJRISK", "proj_id IN ({S})", True),
    ("TASKRISK", "proj_id IN ({S})", True),
    ("FINTMPL", "", True),
    ("BASETYPE", "", True),
    ("PREFER", "", True),
    ("NEXTKEY", "", True),
)
READ_ONLY = ("P6 Professional on SQL Server or Oracle is read only here: the tools read a local copy and the database "
             "server is never written. Make the change in P6, then refresh the copy (p6_pmdb_refresh, or Copy now in "
             "MCP for Primavera P6 Settings, Database).")


class CopyError(ValueError):
    """A test or a copy failed; the message is meant for the user (never holds the password)."""


# ----------------------------------------------------------------- profile

def digest(profile: dict) -> str:
    """Fingerprint of the connection settings: a test confirms only the settings it was run with."""
    keys = {k: profile.get(k) for k in CONNECTION_KEYS}
    keys["tls"] = bool(keys["tls"])
    return hashlib.sha256(json.dumps(keys, sort_keys=True, default=str).encode()).hexdigest()[:16]


def selection_digest(profile: dict) -> str:
    """Fingerprint of the connection and of what is copied: copy.json keeps it, p6_diagnose says when the settings
    changed since the copy."""
    keys = {"connection": digest(profile), "projects": sorted(_names(profile.get("projects"))),
            "eps": sorted(_names(profile.get("eps"))), "baselines": profile.get("baselines") or "current"}
    return hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()[:16]


def _names(values: Any) -> list[str]:
    """Project or EPS IDs of a profile, trimmed, upper case (P6 IDs ignore case), repeats dropped."""
    out: dict[str, None] = {}
    for v in values or []:
        v = str(v).strip()
        if v:
            out.setdefault(v.upper(), None)
    return list(out)


def split_ids(text: str) -> list[str]:
    """IDs typed in one field (comma, semicolon or line separated; an ID may hold spaces)."""
    return [p.strip() for p in re.split(r"[,;\n]+", text or "") if p.strip()]


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name).strip("._") or "database"


def store_dir(profile_name: str) -> str:
    return os.path.join(config.localdata_dir(), "pmdb", _safe(profile_name))


def store_path(profile_name: str) -> str:
    """pmdb_<profile>.db: a name of its own (the edit log keys entries by file name; this copy never has any)."""
    return os.path.join(store_dir(profile_name), f"pmdb_{_safe(profile_name)}.db")


def read_manifest(profile_name: str) -> Optional[dict]:
    try:
        with open(os.path.join(store_dir(profile_name), MANIFEST_FILE), encoding="utf-8") as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or m.get("store_version") != STORE_VERSION or not os.path.isfile(
            store_path(profile_name)):
        return None
    return m


def age_hours(manifest: Optional[dict], now: Optional[datetime] = None) -> Optional[float]:
    """Hours since the copy was made (one decimal), None without a copy."""
    try:
        at = datetime.fromisoformat(manifest["copied_at"])
    except (TypeError, KeyError, ValueError):
        return None
    return round(max(0.0, ((now or datetime.now()) - at).total_seconds() / 3600), 1)


# ----------------------------------------------------------------- SQL helpers

def _rows(cur) -> list[dict]:
    """The cursor's rows as dicts with lower case keys (Oracle gives upper case names), values as the copy stores
    them."""
    cols = [d[0].lower() for d in cur.description or []]
    return [dict(zip(cols, (_store._value(v) for v in r), strict=True)) for r in cur.fetchall()]


def _alive(row: dict) -> bool:
    return row.get("delete_session_id") is None and row.get("delete_date") is None


def _module(profile: dict, module_factory: Optional[Callable[[str], Any]]) -> Any:
    mod = module_factory(profile["driver"]) if module_factory else _sql.driver_module(profile["driver"])
    if mod is None:
        raise CopyError(f"The {_sql.DRIVERS[profile['driver']]} driver is not installed: install it from MCP for "
                        "Primavera P6 Settings (Database, Install the driver).")
    return mod


def _connect(profile: dict, password: str, mod: Any):
    if not password:
        raise CopyError("No password for the database login: enter it in MCP for Primavera P6 Settings (Database) "
                        "and click Test connection.")
    try:
        con = _sql.connect(profile, password, mod)
    except _sql.SqlError as e:
        raise CopyError(str(e)) from None
    if profile.get("driver") == "oracle":  # one point in time for every table of this connection
        try:
            con.cursor().execute("SET TRANSACTION READ ONLY")
        except Exception:  # noqa: BLE001  (each driver raises its own classes: the copy goes on without it)
            pass
    return con


def _clean(e: Exception, password: str) -> str:
    text = " ".join(str(e).split())
    return (text.replace(password, "***") if password else text)[:300]


def _p6_version(cur, profile: dict) -> dict:
    """PREFER.database_version, the version P6 records in the database ('PPMDB,2512...' = 25.12)."""
    from p6mcp import schema
    try:
        cur.execute(_sql.first_row(profile, f"database_version FROM {_sql.table(profile, 'PREFER')}"))
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001
        return {"database_version": None, "version": None}
    raw = rows[0][0] if rows else None
    return {"database_version": raw, "version": schema.version_label(raw)}


def _by_ids(cur, profile: dict, ids: list[int]) -> list[dict]:
    """PROJECT rows of these proj_ids (alive ones)."""
    if not ids:
        return []
    cur.execute(f"SELECT * FROM {_sql.table(profile, 'PROJECT')} WHERE "
                + _store._in_lists("proj_id IN ({S})", sorted(set(ids))))
    return [r for r in _rows(cur) if _alive(r)]


def resolve(cur, profile: dict) -> dict:
    """What a copy of this profile takes: {projects: [PROJECT rows], baselines: [PROJECT rows], eps: {EPS ID: [Project
    IDs]}, missing: {projects: [...], eps: [...]}, templates_skipped}. Project IDs are matched case ignored (one
    parameterised query per PARAMS_MAX names); EPS IDs on the EPS node's short name, then its name."""
    t_project, t_wbs = _sql.table(profile, "PROJECT"), _sql.table(profile, "PROJWBS")
    wanted = _names(profile.get("projects"))
    found: dict[int, dict] = {}
    seen: set[str] = set()
    for k in range(0, len(wanted), PARAMS_MAX):
        part = wanted[k:k + PARAMS_MAX]
        cur.execute(f"SELECT * FROM {t_project} WHERE project_flag = 'Y' AND UPPER(proj_short_name) IN "
                    f"({_sql.marks(profile, len(part))})", tuple(part))
        for r in _rows(cur):
            if _alive(r) and str(r.get("proj_short_name") or "").upper() in part:
                found[int(r["proj_id"])] = r
                seen.add(str(r["proj_short_name"]).upper())
    missing_p = [w for w in wanted if w not in seen]
    eps_out: dict[str, list[str]] = {}
    missing_e: list[str] = []
    skipped: list[str] = []
    eps_names = _names(profile.get("eps"))
    if eps_names:
        cur.execute(f"SELECT * FROM {t_project} WHERE project_flag = '{NODE_EPS}'")
        eps_rows = {int(r["proj_id"]) for r in _rows(cur) if _alive(r)}
        cur.execute(f"SELECT * FROM {t_wbs} WHERE proj_node_flag = 'Y'")
        nodes = [r for r in _rows(cur) if _alive(r)]
        children: dict[Any, list[dict]] = {}
        for n in nodes:
            children.setdefault(n.get("parent_wbs_id"), []).append(n)
        eps_nodes = [n for n in nodes if int(n["proj_id"]) in eps_rows]
        for name in eps_names:
            hit = [n for n in eps_nodes if str(n.get("wbs_short_name") or "").upper() == name] or \
                  [n for n in eps_nodes if str(n.get("wbs_name") or "").upper() == name]
            if not hit:
                missing_e.append(name)
                continue
            todo, seen_w, under = [hit[0]["wbs_id"]], {hit[0]["wbs_id"]}, []
            while todo:  # the EPS subtree (a cycle is never followed twice)
                for c in children.get(todo.pop(), []):
                    if int(c["proj_id"]) not in eps_rows:
                        under.append(c)
                    elif c["wbs_id"] not in seen_w:
                        seen_w.add(c["wbs_id"])
                        todo.append(c["wbs_id"])
            rows = {int(r["proj_id"]): r for r in _by_ids(cur, profile, [int(c["proj_id"]) for c in under])}
            status = {int(c["proj_id"]): c.get("status_code") for c in under}
            ids = []
            for pid, r in sorted(rows.items(), key=lambda x: str(x[1].get("proj_short_name") or "").upper()):
                if r.get("project_flag") != "Y" or r.get("orig_proj_id") is not None:
                    continue  # baselines come with their project (baselines setting)
                if status.get(pid) == TEMPLATE:
                    skipped.append(str(r.get("proj_short_name")))
                    continue
                found.setdefault(pid, r)
                ids.append(str(r.get("proj_short_name")))
            eps_out[str(hit[0].get("wbs_short_name") or name)] = ids
    mode = profile.get("baselines") or "current"
    base: dict[int, dict] = {}
    if mode == "current":
        ids = [int(r["sum_base_proj_id"]) for r in found.values() if r.get("sum_base_proj_id") not in (None, "")]
        base = {int(r["proj_id"]): r for r in _by_ids(cur, profile, ids) if int(r["proj_id"]) not in found}
    elif mode == "all" and found:
        cur.execute(f"SELECT * FROM {t_project} WHERE " + _store._in_lists("orig_proj_id IN ({S})", sorted(found)))
        base = {int(r["proj_id"]): r for r in _rows(cur) if _alive(r) and int(r["proj_id"]) not in found}
    return {"projects": sorted(found.values(), key=lambda r: str(r.get("proj_short_name") or "").upper()),
            "baselines": sorted(base.values(), key=lambda r: int(r["proj_id"])), "eps": eps_out,
            "missing": {"projects": missing_p, "eps": missing_e}, "templates_skipped": sorted(skipped)}


def _selection_text(sel: dict) -> str:
    bits = []
    if sel["missing"]["projects"]:
        bits.append("Project ID(s) not found: " + ", ".join(sel["missing"]["projects"]))
    if sel["missing"]["eps"]:
        bits.append("EPS ID(s) not found: " + ", ".join(sel["missing"]["eps"]))
    return "; ".join(bits)


# ----------------------------------------------------------------- test connection

def test_profile(profile: dict, password: Optional[str],
                 module_factory: Optional[Callable[[str], Any]] = None) -> dict:
    """Test connection: sign in, read the P6 tables the tools need (SELECT ... WHERE 1 = 0), the P6 version from
    PREFER, the number of projects, and what the selection names. ok = the tables read; the selection is reported
    beside (a missing Project ID is fixed in the list, not in the connection). Never raises."""
    t0 = time.perf_counter()
    res: dict = {"ok": False, "digest": digest(profile), "driver": profile.get("driver"),
                 "tested_at": datetime.now().isoformat(timespec="seconds")}
    pw = password or ""
    try:
        if profile.get("driver") not in _sql.DRIVERS:
            raise CopyError(f"Choose SQL Server or Oracle (driver {profile.get('driver')!r}).")
        try:
            mod = _module(profile, module_factory)
        except CopyError as e:
            res.update(driver_missing=_sql.DRIVERS[profile["driver"]], error=str(e))
            return res
        con = _connect(profile, pw, mod)
        try:
            cur = con.cursor()
            missing = []
            for t in _sql.CORE_TABLES:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {_sql.table(profile, t)} WHERE 1 = 0")
                    cur.fetchall()
                except Exception:  # noqa: BLE001
                    missing.append(t)
            if missing:
                raise CopyError("These P6 tables cannot be read with this login: " + ", ".join(missing)
                                + ". Check the database or service name, the schema (owner) and the login's rights.")
            res["p6_version"] = _p6_version(cur, profile)
            cur.execute(f"SELECT COUNT(*) FROM {_sql.table(profile, 'PROJECT')} "
                        "WHERE project_flag = 'Y' AND orig_proj_id IS NULL")
            res["projects"] = int(cur.fetchall()[0][0])
            sel = resolve(cur, profile)
            res["selection"] = {"projects": [r["proj_short_name"] for r in sel["projects"]],
                                "baselines": len(sel["baselines"]), "eps": sel["eps"], "missing": sel["missing"],
                                "templates_skipped": sel["templates_skipped"]}
            res["ok"] = True
        finally:
            con.close()
    except CopyError as e:
        res["error"] = str(e)
    except (KeyError, TypeError, ValueError) as e:
        res["error"] = f"Incomplete settings: {e}"
    except Exception as e:  # noqa: BLE001  (a driver error after sign-in)
        res["error"] = f"Read failed: {_clean(e, pw)}"
    res["seconds"] = round(time.perf_counter() - t0, 2)
    return res


# ----------------------------------------------------------------- copy

def copy(profile: dict, password: str, module_factory: Optional[Callable[[str], Any]] = None) -> dict:
    """Copy the profile's projects, their baselines and the global tables into the local copy. Returns the manifest
    written to copy.json. Raises CopyError (the previous copy is kept)."""
    if not _names(profile.get("projects")) and not _names(profile.get("eps")):
        raise CopyError("Nothing to copy: name the projects (Project IDs) or an EPS in MCP for Primavera P6 Settings "
                        "(Database).")
    if (profile.get("baselines") or "current") not in BASELINES:
        raise CopyError(f"baselines must be one of {list(BASELINES)}")
    started = datetime.now()
    mod = _module(profile, module_factory)
    folder = store_dir(profile["name"])
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".copy.", suffix=".db", dir=folder)
    os.close(fd)
    warnings: list[str] = []
    counts: dict[str, int] = {}
    out: Optional[sqlite3.Connection] = None
    pw = password or ""
    try:
        con = _connect(profile, pw, mod)
        try:
            cur = con.cursor()
            try:
                sel = resolve(cur, profile)
                version = _p6_version(cur, profile)
            except CopyError:
                raise
            except Exception as e:  # noqa: BLE001
                raise CopyError(f"The project list cannot be read: {_clean(e, pw)}") from None
            why = _selection_text(sel)
            if why:
                raise CopyError(why + ". Fix the list in MCP for Primavera P6 Settings (Database).")
            if not sel["projects"]:
                raise CopyError("The EPS selection holds no project.")
            ids = sorted({int(r["proj_id"]) for r in sel["projects"] + sel["baselines"]})
            out = sqlite3.connect(tmp)
            for table, where, optional in TABLES:
                try:
                    n = _store._copy_table(cur, out, profile, table, where, ids)
                except Exception as e:  # noqa: BLE001  (each driver raises its own classes)
                    if hasattr(con, "rollback") and profile.get("driver") != "oracle":  # Oracle: keep the snapshot
                        try:
                            con.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                    if optional:
                        warnings.append(f"{table} not copied: {_clean(e, pw)[:160]}")
                        continue
                    raise CopyError(f"{table} cannot be read: {_clean(e, pw)[:200]}") from None
                if n is not None:
                    counts[table] = n
            t_pred = _sql.table(profile, "TASKPRED")
            try:
                cur.execute(f"SELECT COUNT(*) FROM {t_pred} WHERE "
                            + _store._in_lists("(proj_id IN ({S}) AND pred_proj_id NOT IN ({S})) OR "
                                               "(pred_proj_id IN ({S}) AND proj_id NOT IN ({S}))", ids))
                cross = int(cur.fetchall()[0][0])
            except Exception:  # noqa: BLE001
                cross = 0
            if cross:
                warnings.append(f"{cross} relationship(s) to or from projects that were not copied are left out")
            if sel["templates_skipped"]:
                warnings.append("template projects of the EPS left out: " + ", ".join(sel["templates_skipped"]))
        finally:
            con.close()
        _finish(out)
        out.commit()
        out.close()
        out = None
        dest = store_path(profile["name"])
        try:
            os.replace(tmp, dest)
        except OSError as e:
            raise CopyError(f"The local copy could not be replaced (is another tool reading it?): {e}") from None
    except BaseException:
        if out is not None:
            try:
                out.close()
            except sqlite3.Error:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    base_of = {int(r["proj_id"]): r for r in sel["baselines"]}
    manifest = {
        "store_version": STORE_VERSION, "profile": profile["name"], "driver": profile["driver"],
        "server": f"{profile.get('host')}:{profile.get('port')}/{profile.get('service')}",
        "schema": profile.get("schema") or None,
        "copied_at": started.isoformat(timespec="seconds"),
        "seconds": round((datetime.now() - started).total_seconds(), 2),
        "p6_version": version, "selection_digest": selection_digest(profile),
        "projects": [{"project_id": r["proj_short_name"], "proj_id": int(r["proj_id"]),
                      "baseline": (base_of.get(int(r["sum_base_proj_id"])) or {}).get("proj_short_name")
                      if r.get("sum_base_proj_id") not in (None, "") else None} for r in sel["projects"]],
        "baselines": [{"project_id": r["proj_short_name"], "proj_id": int(r["proj_id"]),
                       "baseline_of": r.get("orig_proj_id")} for r in sel["baselines"]],
        "eps": sel["eps"], "rows": counts, "warnings": warnings}
    path = os.path.join(store_dir(profile["name"]), MANIFEST_FILE)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, default=str)
    os.replace(path + ".tmp", path)
    return manifest


def _finish(out: sqlite3.Connection) -> None:
    """The tables an older or partial PMDB may lack (empty) and the indexes of the copy."""
    from p6mcp import xerstore
    xerstore._support_tables(out, None)
    have = {r[0].upper() for r in out.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for t, c in _store.INDEXES + (("PROJPROP", "proj_id"),):
        if t in have:
            out.execute(f"CREATE INDEX IF NOT EXISTS ix_{t}_{c} ON {t} ({c})")


def summary(manifest: Optional[dict], profile: Optional[dict] = None) -> Optional[dict]:
    """What p6_diagnose and the tools say about the copy (no password, no file content)."""
    if not manifest:
        return None
    out = {"copied_at": manifest.get("copied_at"), "age_hours": age_hours(manifest),
           "projects": [p["project_id"] for p in manifest.get("projects", [])],
           "baselines": [b["project_id"] for b in manifest.get("baselines", [])],
           "p6_version": (manifest.get("p6_version") or {}).get("version")}
    if profile is not None:
        out["settings_changed_since"] = manifest.get("selection_digest") != selection_digest(profile)
    return out
