"""Read-only SQL connection to the PMDB of a P6 EPPM server (docs/EPPM_PLAN.md section 3.1).

Drivers are installed on demand (decision D3): `pymssql` for SQL Server, `oracledb` (thin mode) for Oracle. They are
imported here only when a profile has an `sql` block; a missing driver is reported, never a crash. The account must
be read only: this module only runs SELECT statements, and the pull checks the REST rights first.
"""

from __future__ import annotations

import importlib
import time
from typing import Any, Callable, Optional

DRIVERS = {"mssql": "pymssql", "oracle": "oracledb"}
CORE_TABLES = ("PROJECT", "PROJWBS", "TASK", "TASKPRED", "CALENDAR")


class SqlError(ValueError):
    """A SQL connection or read failed; the message is meant for the user."""


def driver_module(driver: str) -> Optional[Any]:
    """The imported driver module, or None when it is not installed."""
    name = DRIVERS.get(driver)
    if not name:
        raise SqlError(f"Unknown SQL driver {driver!r}: use one of {sorted(DRIVERS)}.")
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def table(sql: dict, name: str) -> str:
    """Qualified table name: schema, then the table prefix (T6_ on Oracle Cloud DACS)."""
    schema = (sql.get("schema") or "").strip()
    return (schema + "." if schema else "") + (sql.get("table_prefix") or "") + name


def connect(sql: dict, password: str, module: Any = None):
    if not (sql.get("user") or "").strip():
        raise SqlError("No SQL user name: enter the read-only SQL account.")
    mod = module or driver_module(sql["driver"])
    if mod is None:
        raise SqlError(f"The {DRIVERS[sql['driver']]} driver is not installed.")
    try:
        if sql["driver"] == "mssql":
            return mod.connect(server=sql["host"], port=int(sql["port"]), user=sql["user"], password=password,
                               database=sql["service"], login_timeout=20, timeout=120)
        dsn = f"{sql['host']}:{int(sql['port'])}/{sql['service']}"
        return mod.connect(user=sql["user"], password=password, dsn=dsn)
    except Exception as e:  # noqa: BLE001  (every driver raises its own classes)
        text = str(e).replace(password, "***") if password else str(e)
        raise SqlError(f"SQL connection failed: {' '.join(text.split())[:300]}") from e


def test_connection(sql: dict, password: str, module_factory: Optional[Callable[[str], Any]] = None) -> dict:
    """Connect, read the column list of TASK and count PROJECT rows. Never raises."""
    t0 = time.perf_counter()
    res: dict = {"ok": False, "driver": sql.get("driver")}
    try:
        mod = module_factory(sql["driver"]) if module_factory else driver_module(sql["driver"])
        if mod is None:
            res.update(driver_missing=DRIVERS[sql["driver"]],
                       error=f"The {DRIVERS[sql['driver']]} driver is not installed.")
            return res
        con = connect(sql, password, mod)
        try:
            cur = con.cursor()
            missing = []
            for t in CORE_TABLES:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {table(sql, t)} WHERE 1 = 0")
                    cur.fetchall()
                except Exception:  # noqa: BLE001
                    missing.append(t)
            if missing:
                raise SqlError("These P6 tables cannot be read with this account: " + ", ".join(missing)
                               + ". Check the schema, the table prefix and the account's rights.")
            cur.execute(f"SELECT COUNT(*) FROM {table(sql, 'PROJECT')}")
            res["project_rows"] = int(cur.fetchall()[0][0])
            one = "FETCH FIRST 1 ROWS ONLY" if sql["driver"] == "oracle" else ""
            top = "" if one else "TOP 1 "
            cur.execute(f"SELECT {top}* FROM {table(sql, 'TASK')} {one}".strip())
            cur.fetchall()
            res["task_columns"] = len(cur.description or [])
            res["ok"] = True
        finally:
            con.close()
    except SqlError as e:
        res["error"] = str(e)
    except (KeyError, TypeError, ValueError) as e:
        res["error"] = f"Incomplete SQL settings: {e}"
    res["seconds"] = round(time.perf_counter() - t0, 2)
    return res
