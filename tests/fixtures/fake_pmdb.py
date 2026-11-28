"""A fake SQL Server and a fake Oracle holding a P6 database (tests of p6mcp.pmdb, no real server).

The synthetic SQLite database is attached under a schema name (dbo, ADMUSER) to an empty in-memory database, so a
table is found only when the code qualifies it with the schema, as on a server where the login is not the owner.
Each fake speaks only its own dialect and refuses the other's:

- SQL Server (pymssql): %s parameters, TOP n, names as stored, decimal columns as Decimal, datetime values, binary
  columns as bytes, NVARCHAR text as str;
- Oracle (oracledb): :1, :2 parameters, FETCH FIRST n ROWS ONLY, upper case column names, NUMBER as int when whole,
  DATE as datetime, BLOB and CLOB columns as LOB objects (read()), '' read back as NULL (Oracle has no empty
  string), at most 1000 items in one IN list (ORA-01795), SET TRANSACTION READ ONLY accepted.

Both accept SELECT only (any other statement raises), check the login and record every statement they ran.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from decimal import Decimal

DATE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d(:\d\d)?$")
LOB_COLS = {"clndr_data", "task_memo", "prop_value", "rsrc_curv_data", "curv_data"}


class FakeLob:
    """What oracledb returns for a BLOB or CLOB column by default."""

    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


class FakeError(Exception):
    pass


class FakeCursor:
    def __init__(self, con: "FakeConnection"):
        self.con, self.cur, self.description, self._rows = con, con.db.cursor(), None, []

    def execute(self, sql: str, params=()):
        drv = self.con.driver
        self.con.statements.append(sql)
        text = " ".join(sql.split())
        if drv.dialect == "oracle" and text.upper() == "SET TRANSACTION READ ONLY":
            self.con.read_only = True
            self.description, self._rows = None, []
            return
        if not text.upper().startswith("SELECT "):
            raise FakeError(f"the fake {drv.dialect} database accepts SELECT only, got: {text[:60]}")
        for m in re.finditer(r"\b(?:FROM|JOIN)\s+([\w.]+)", text, re.I):  # the login does not own the tables
            if not m[1].upper().startswith(drv.schema.upper() + "."):
                raise FakeError("ORA-00942: table or view does not exist" if drv.dialect == "oracle"
                                else f"Invalid object name '{m[1]}'.")
        if "?" in text or re.search(r"\bLIMIT\b", text, re.I):
            raise FakeError("syntax error near '?' or LIMIT: not this dialect")
        if drv.dialect == "mssql":
            if re.search(r":\d", text) or "FETCH FIRST" in text.upper():
                raise FakeError("Incorrect syntax: SQL Server takes %s parameters and TOP n")
            text = text.replace("%s", "?")
            m = re.match(r"SELECT TOP (\d+) (.*)$", text, re.I)
            if m:
                text = f"SELECT {m[2]} LIMIT {m[1]}"
        else:
            if "%s" in text or re.match(r"SELECT TOP ", text, re.I):
                raise FakeError("ORA-00923: FROM keyword not found where expected")
            for m in re.finditer(r"IN \(([^()]*)\)", text):
                if m[1].count(",") >= 1000:
                    raise FakeError("ORA-01795: maximum number of expressions in a list is 1000")
            text = re.sub(r":\d+", "?", text)
            m = re.match(r"(.*) FETCH FIRST (\d+) ROWS ONLY$", text, re.I)
            if m:
                text = f"{m[1]} LIMIT {m[2]}"
        try:
            self.cur.execute(text, tuple(params))
        except sqlite3.Error as e:
            raise FakeError(f"{drv.dialect}: {e}") from None
        names = [d[0] for d in self.cur.description or []]
        self.description = [((n.upper() if drv.dialect == "oracle" else n), None, None, None, None, None, None)
                             for n in names]
        self._rows = [tuple(self._value(n, v) for n, v in zip(names, r, strict=True)) for r in self.cur.fetchall()]

    def _value(self, name: str, v):
        oracle = self.con.driver.dialect == "oracle"
        if isinstance(v, str) and DATE.match(v):
            return datetime.fromisoformat(v)
        if isinstance(v, float):
            if oracle:
                return int(v) if v.is_integer() else v
            return Decimal(repr(v))
        if isinstance(v, int) and not oracle and name.endswith(("_cnt", "_qty", "_cost", "_pct")):
            return Decimal(v)  # a decimal column holding a whole number
        if isinstance(v, bytes):
            return FakeLob(v) if oracle else v
        if oracle and isinstance(v, str) and (name.lower() in LOB_COLS or name.lower().endswith("_descr")):
            return FakeLob(v)
        if oracle and v == "":
            return None
        return v

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def fetchmany(self, n: int):
        rows, self._rows = self._rows[:n], self._rows[n:]
        return rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, driver: "FakeDriver"):
        self.driver, self.statements, self.read_only, self.closed = driver, [], False, False
        self.db = sqlite3.connect(":memory:", uri=True)  # uri: the ATTACH below opens the file read only
        self.db.execute(f"ATTACH DATABASE ? AS {driver.schema}", (f"file:{driver.path}?mode=ro",))
        driver.connections.append(self)

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        pass

    def commit(self):
        raise FakeError("the fake database is never committed to")

    def close(self):
        self.closed = True
        self.db.close()


class FakeDriver:
    """The driver module (pymssql or oracledb) of one fake server."""

    def __init__(self, path: str, dialect: str, schema: str, user: str = "reader", password: str = "Db-pass-1"):
        self.path, self.dialect, self.schema, self.user, self.password = path, dialect, schema, user, password
        self.connections: list[FakeConnection] = []
        self.calls: list[dict] = []

    def connect(self, **kw):
        self.calls.append({k: v for k, v in kw.items() if k != "password"})
        if self.dialect == "mssql":
            need = {"server", "port", "user", "password", "database"}
        elif "dsn" in kw:
            need = {"user", "password", "dsn"}
        else:
            need = {"user", "password", "host", "port", "service_name", "protocol"}
        if not need <= set(kw):
            raise TypeError(f"connect() missing {sorted(need - set(kw))}")
        if kw["user"] != self.user or kw["password"] != self.password:
            raise FakeError(f"Login failed for user '{kw['user']}' with password '{kw['password']}'")
        return FakeConnection(self)

    def statements(self) -> list[str]:
        return [s for c in self.connections for s in c.statements]


def profile(dialect: str, **over) -> dict:
    """A pmdb profile for the fake of this dialect."""
    p = {"name": "plant", "driver": dialect, "host": "db.test", "port": 1433 if dialect == "mssql" else 1521,
         "service": "PMDB" if dialect == "mssql" else "P6PDB", "schema": "dbo" if dialect == "mssql" else "ADMUSER",
         "user": "reader", "tls": False, "projects": [], "eps": [], "baselines": "current", "confirmed": False}
    p.update(over)
    return p


def driver(path: str, dialect: str) -> FakeDriver:
    return FakeDriver(path, dialect, "dbo" if dialect == "mssql" else "ADMUSER")
