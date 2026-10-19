"""Shared configuration of the server and the Settings app.

File: %APPDATA%\\P6MCP\\config.json (P6MCP_CONFIG overrides the path). Versioned: `schema_version`
plus one migration per version step, applied on load (the file itself is rewritten only by save()).
Secrets (database passwords, the HTTP token) never go in this file: see p6mcp.credentials.

Resolution order of the effective settings (`load()`): defaults, then the file, then environment
variables. The environment variables are an explicit selection made by whoever wrote the client
config, so they count as a confirmed database:
  P6_DB_PATH, P6_USER, P6_BACKUP_DIR, P6_LOG_DIR, P6_SKIP_PROCESS_CHECK=1 (as in server v2),
  P6MCP_TOOL_PROFILE=basic|full, P6MCP_ALLOW_WRITES=0|1.
Without a config file, P6_DB_PATH keeps the v2 layout (backups and edit log beside the database,
one backup per server process), so an existing deployment keeps its history.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

CONFIG_VERSION = 1
APP_DIR_NAME = "P6MCP"
TOOL_PROFILES = ("basic", "full")
TRANSPORTS = ("stdio", "http")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DB_KINDS = ("sqlite",)  # SQL Server / Oracle: later versions (read only)
BACKUP_SCOPES = ("every_write", "session")
SECRET_KEYS = ("password", "token", "secret", "pwd")  # never stored in the file

DEFAULTS: dict[str, Any] = {
    "schema_version": CONFIG_VERSION,
    "databases": [],              # [{name, kind, path, confirmed, tested_at, schema_fingerprint, ...}]
    "active_database": None,      # name of the active entry of databases
    "tool_profile": "basic",      # basic: read-only curated set; full: every tool
    "allow_writes": False,
    "backup_before_write": True,
    "backup_scope": "every_write",
    "backup_dir": None,           # None: %LOCALAPPDATA%\P6MCP\backups
    "backup_retention": 10,       # backups of one database kept by the automatic pruning (0 = keep all)
    "edit_log_dir": None,         # None: %LOCALAPPDATA%\P6MCP\editlog
    "block_when_p6_running": True,
    "p6_user": "ADMIN",           # update_user / create_user written by the edit tools
    "trusted_schemas": [],        # schema fingerprints the user accepted for writes (besides the known ones)
    "transport": "stdio",
    "http_port": 39217,
    "log_level": "INFO",
    "language": "auto",
    "run_at_startup": False,
    "start_minimized": True,
    "update_check": True,
}


class ConfigError(ValueError):
    """The config file cannot be read or does not validate."""


def appdata_dir() -> str:
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_DIR_NAME)


def localdata_dir() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~"),
                        APP_DIR_NAME)


def config_path() -> str:
    return os.environ.get("P6MCP_CONFIG") or os.path.join(appdata_dir(), "config.json")


# ----------------------------------------------------------------- migrations

def _migrate_0_to_1(d: dict) -> dict:
    """Version 0 = a hand-written file without schema_version: {"db_path": ...} at most."""
    d = dict(d)
    path = d.pop("db_path", None)
    if path and not d.get("databases"):
        # a path written by hand is an explicit choice, but it was never tested: the Settings app
        # asks for a test before it becomes usable
        d["databases"] = [{"name": "default", "kind": "sqlite", "path": path, "confirmed": False}]
        d["active_database"] = "default"
    d["schema_version"] = 1
    return d


MIGRATIONS = {0: _migrate_0_to_1}  # from version -> function giving version + 1


def migrate(d: dict) -> tuple[dict, list[str]]:
    """Bring a raw config dict to CONFIG_VERSION. Returns (dict, steps applied)."""
    v = d.get("schema_version", 0)
    if not isinstance(v, int) or v < 0:
        raise ConfigError(f"schema_version must be a whole number, got {v!r}")
    if v > CONFIG_VERSION:
        raise ConfigError(f"config.json was written by a newer P6 MCP (schema_version {v}, this version reads "
                          f"{CONFIG_VERSION}). Update P6 MCP.")
    steps = []
    while v < CONFIG_VERSION:
        d = MIGRATIONS[v](d)
        steps.append(f"{v}->{v + 1}")
        v += 1
    return d, steps


# ----------------------------------------------------------------- validation

def _secret_keys(obj: Any, path: str = "") -> list[str]:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if any(s in str(k).lower() for s in SECRET_KEYS) and v not in (None, ""):
                out.append(p)
            out += _secret_keys(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += _secret_keys(v, f"{path}[{i}]")
    return out


def validate(d: dict) -> dict:
    """Defaults filled in, types and enums checked. Unknown keys are kept (a newer Settings app may
    have written them) but ignored."""
    out = copy.deepcopy(DEFAULTS)
    out.update(d)
    errs = []

    def enum(key, allowed):
        if out[key] not in allowed:
            errs.append(f"{key} must be one of {list(allowed)}, got {out[key]!r}")

    enum("tool_profile", TOOL_PROFILES)
    enum("transport", TRANSPORTS)
    enum("log_level", LOG_LEVELS)
    enum("backup_scope", BACKUP_SCOPES)
    for key in ("allow_writes", "backup_before_write", "block_when_p6_running", "run_at_startup",
                "start_minimized", "update_check"):
        if not isinstance(out[key], bool):
            errs.append(f"{key} must be true or false, got {out[key]!r}")
    if not isinstance(out["backup_retention"], int) or isinstance(out["backup_retention"], bool) \
            or out["backup_retention"] < 0:
        errs.append("backup_retention must be a whole number >= 0")
    port = out["http_port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        errs.append("http_port must be a whole number between 1024 and 65535")
    for key in ("backup_dir", "edit_log_dir", "active_database"):
        if out[key] is not None and not isinstance(out[key], str):
            errs.append(f"{key} must be text or null")
    if not isinstance(out["p6_user"], str) or not out["p6_user"].strip():
        errs.append("p6_user must be non-empty text")
    if not isinstance(out["trusted_schemas"], list) or not all(isinstance(x, str) for x in out["trusted_schemas"]):
        errs.append("trusted_schemas must be a list of fingerprints")
    dbs = out["databases"]
    names = set()
    if not isinstance(dbs, list):
        errs.append("databases must be a list")
        dbs = []
    for i, db in enumerate(dbs):
        if not isinstance(db, dict):
            errs.append(f"databases[{i}] must be an object")
            continue
        name = db.get("name")
        if not isinstance(name, str) or not name.strip():
            errs.append(f"databases[{i}].name must be non-empty text")
        elif name in names:
            errs.append(f"databases[{i}].name {name!r} is used twice")
        names.add(name)
        if db.get("kind", "sqlite") not in DB_KINDS:
            errs.append(f"databases[{i}].kind must be one of {list(DB_KINDS)}")
        if not isinstance(db.get("path"), str) or not db["path"].strip():
            errs.append(f"databases[{i}].path must be the path of the .db file")
        if not isinstance(db.get("confirmed", False), bool):
            errs.append(f"databases[{i}].confirmed must be true or false")
    if out["active_database"] is not None and out["active_database"] not in names:
        errs.append(f"active_database {out['active_database']!r} is not in databases")
    leaks = _secret_keys(out)
    if leaks:
        errs.append(f"secrets must not be stored in config.json (use the Windows Credential Manager): {leaks}")
    if errs:
        raise ConfigError("; ".join(errs))
    return out


# ----------------------------------------------------------------- file access

_cache: dict[str, tuple] = {}  # path -> ((mtime_ns, size), validated dict, migration steps)


def read_file(path: Optional[str] = None) -> tuple[Optional[dict], list[str]]:
    """Validated content of the config file (None when there is no file) and the migration steps
    applied in memory. Cached until the file changes. Raises ConfigError when unreadable."""
    path = path or config_path()
    try:
        st = os.stat(path)
    except FileNotFoundError:
        _cache.pop(path, None)
        return None, []
    key = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit and hit[0] == key:
        return copy.deepcopy(hit[1]), list(hit[2])
    try:
        with open(path, encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as e:
        raise ConfigError(f"config.json is unreadable ({path}): {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"config.json must hold a JSON object ({path})")
    data, steps = migrate(raw)
    data = validate(data)
    _cache[path] = (key, data, steps)
    return copy.deepcopy(data), steps


def save(data: dict, path: Optional[str] = None) -> str:
    """Validate and write atomically (temporary file in the same folder, then os.replace). The
    previous file is kept as config.json.bak."""
    path = path or config_path()
    data = validate(dict(data, schema_version=CONFIG_VERSION))
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(path):
            try:
                with open(path, "rb") as src, open(path + ".bak", "wb") as dst:
                    dst.write(src.read())
            except OSError:
                pass  # the backup copy is a convenience, never a reason to fail the save
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _cache.pop(path, None)
    return path


# ----------------------------------------------------------------- effective settings

NO_DB_MESSAGE = "No database configured. Open P6 MCP Settings to select one."


@dataclass(frozen=True)
class Settings:
    """What the server uses for one tool call."""
    source: str                       # "config", "env" (P6_DB_PATH without config file) or "none"
    db_path: str = ""                 # "" when no usable database is selected
    db_name: Optional[str] = None
    db_problem: Optional[str] = None  # why db_path is empty or unusable (user-facing)
    tool_profile: str = "basic"
    allow_writes: bool = False
    backup_before_write: bool = True
    backup_scope: str = "every_write"
    backup_dir: str = ""
    backup_retention: int = 10
    edit_log_dir: str = ""
    legacy_log_dir: Optional[str] = None  # v2 edit log folder beside the database, still read
    block_when_p6_running: bool = True
    p6_user: str = "ADMIN"
    trusted_schemas: tuple[str, ...] = ()
    transport: str = "stdio"
    http_port: int = 39217
    log_level: str = "INFO"
    config_path: str = ""
    config_error: Optional[str] = None
    migrated: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict, compare=False, repr=False)


def _env_flag(name: str) -> Optional[bool]:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return None
    return v.strip().lower() in ("1", "true", "yes", "on")


def load() -> Settings:
    """Effective settings now (cheap: the file is cached until it changes). Never raises: a broken
    config file gives no database, with the reason in config_error and db_problem."""
    path = config_path()
    err = None
    try:
        data, steps = read_file(path)
    except ConfigError as e:
        data, steps, err = None, [], str(e)
    env_db = os.environ.get("P6_DB_PATH", "").strip()
    cfg = data or copy.deepcopy(DEFAULTS)
    source = "config" if data is not None else ("env" if env_db else "none")

    db_path, db_name, problem = "", None, None
    if err:
        problem = f"{err} Open P6 MCP Settings to fix it."
    if env_db:
        db_path, db_name, problem = os.path.abspath(env_db), "P6_DB_PATH", None
    elif not err:
        active = cfg.get("active_database")
        entry = next((d for d in cfg["databases"] if d.get("name") == active), None)
        if entry is None:
            problem = NO_DB_MESSAGE
        elif not entry.get("confirmed"):
            problem = (f"Database {active!r} was selected but not tested and confirmed. Open P6 MCP Settings, "
                       "click Test connection, then confirm it.")
        else:
            db_path, db_name = os.path.abspath(entry["path"]), active
    if db_path and not os.path.isfile(db_path):
        problem = f"The configured database was not found: {db_path}. Open P6 MCP Settings to select another one."
        db_path = ""

    legacy = source == "env"  # v2 layout: files beside the database, one backup per process
    db_dir = os.path.dirname(db_path) if db_path else ""
    backup_dir = os.environ.get("P6_BACKUP_DIR") or cfg.get("backup_dir") or (
        os.path.join(db_dir, "p6_mcp_backups") if legacy and db_dir else os.path.join(localdata_dir(), "backups"))
    log_dir = os.environ.get("P6_LOG_DIR") or cfg.get("edit_log_dir") or (
        os.path.join(db_dir, "p6_mcp_logs") if legacy and db_dir else os.path.join(localdata_dir(), "editlog"))
    legacy_log = os.path.join(db_dir, "p6_mcp_logs") if db_dir else None
    if legacy_log and os.path.normcase(os.path.abspath(legacy_log)) == os.path.normcase(os.path.abspath(log_dir)):
        legacy_log = None

    profile = os.environ.get("P6MCP_TOOL_PROFILE", "").strip().lower() or cfg["tool_profile"]
    if profile not in TOOL_PROFILES:
        profile = "basic"
    allow = _env_flag("P6MCP_ALLOW_WRITES")
    block = cfg["block_when_p6_running"] and os.environ.get("P6_SKIP_PROCESS_CHECK", "0") != "1"
    return Settings(
        source=source, db_path=db_path, db_name=db_name, db_problem=problem,
        tool_profile=profile, allow_writes=cfg["allow_writes"] if allow is None else allow,
        backup_before_write=cfg["backup_before_write"],
        backup_scope="session" if legacy and data is None else cfg["backup_scope"],
        # v2 layout: never prune a folder of backups the user built up before the config file existed
        backup_dir=backup_dir, backup_retention=0 if legacy and data is None else cfg["backup_retention"],
        edit_log_dir=log_dir,
        legacy_log_dir=legacy_log, block_when_p6_running=block,
        p6_user=os.environ.get("P6_USER") or cfg["p6_user"], trusted_schemas=tuple(cfg["trusted_schemas"]),
        transport=cfg["transport"], http_port=cfg["http_port"], log_level=cfg["log_level"],
        config_path=path, config_error=err, migrated=tuple(steps), raw=cfg)


def confirm_database(data: dict, name: str, test_result: dict) -> dict:
    """Mark a database entry as tested and confirmed by the user (called by the Settings app after
    Test connection and the user's explicit OK), and make it the active one."""
    data = copy.deepcopy(data)
    entry = next((d for d in data.get("databases", []) if d.get("name") == name), None)
    if entry is None:
        raise ConfigError(f"No database named {name!r}")
    entry.update(confirmed=True, tested_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 schema_fingerprint=test_result.get("fingerprint"), schema_label=test_result.get("label"),
                 projects=test_result.get("projects"))
    data["active_database"] = name
    return data
