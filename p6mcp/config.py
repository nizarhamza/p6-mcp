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

from p6mcp import instructions, skills

CONFIG_VERSION = 4
APP_DIR_NAME = "P6MCP"
TOOL_PROFILES = ("basic", "full")
TRANSPORTS = ("stdio", "http")
REMOTE_MODES = ("relay", "domain")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DB_KINDS = ("sqlite",)  # SQL Server / Oracle: later versions (read only)
DATA_SOURCES = ("database", "xer", "eppm")  # xer: read-only analysis of XER files; eppm: a P6 EPPM server
EPPM_AUTH = ("auto", "oauth", "login")  # auto: OAuth for Oracle Cloud hosts, else the /login session
EPPM_SQL_DRIVERS = ("mssql", "oracle")  # pymssql / oracledb, installed on demand
BACKUP_SCOPES = ("every_write", "session")
SECRET_KEYS = ("password", "token", "secret", "pwd")  # never stored in the file

DEFAULTS: dict[str, Any] = {
    "schema_version": CONFIG_VERSION,
    "databases": [],              # [{name, kind, path, confirmed, tested_at, schema_fingerprint, ...}]
    "active_database": None,      # name of the active entry of databases
    "data_source": "database",    # database, or xer (XER-only mode, read only)
    "xer_files": [],              # XER-only mode: the files loaded together (two updates to compare...)
    "xer_confirmed": False,       # the files were loaded and checked in the Settings app
    "eppm_profiles": [],          # P6 EPPM servers [{name, base_url, context, database, user, auth, ca_bundle,
                                  #   timeout_s, sql {driver, host, port, service, user, schema, table_prefix}
                                  #   or null, confirmed, tested_at, ...}]; passwords in the Credential Manager
    "eppm_active": None,          # name of the active entry of eppm_profiles (data_source eppm)
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
    "update_check": True,         # legacy key (<= 1.2.0): its False also turns auto_update off
    "auto_update": True,          # check, download and install new releases (tray app)
    "clients_ignored": [],        # client ids whose registration offer the user declined for good
    "instructions_disabled": [],  # built-in server instruction ids turned off (p6mcp.instructions)
    "custom_instructions": [],    # the user's own instruction lines [{text, enabled}]
    "skills_disabled": [],        # 1.8: built-in skill names turned off, carried as enabled false by the core
                                  #   set install (p6mcp.core_skills), then emptied
    "custom_skills": [],          # every skill [{name, title, description, args, template, files, enabled,
                                  #   source {shop, version, sha256, content_sha}}]: shop ones and the user's
    "core_skills_installed": False,  # the Settings app installed the core set (until then: the bundled copy)
    "shop_enabled": True,         # the skill shop (p6mcp.shop: reads the GitHub catalog when asked)
    # 3.2: remote access for claude.ai and ChatGPT (p6mcp.remote). No schema_version bump: an older version keeps
    # these keys as unknown ones and never starts the remote access server.
    "remote_enabled": False,      # master switch (the tray app runs the remote access server and the tunnel)
    "remote_privacy_ack": False,  # the user read what leaves the PC (first enable)
    "remote_mode": "relay",       # relay: Worker on the user's free workers.dev address; domain: their own domain
    "remote_public_host": None,   # public host name only (no scheme, no port), written by the Cloudflare setup
    "remote_port": 39218,         # local port of the remote access server (127.0.0.1 only)
    "remote_pair_port": 39219,    # 3.3: loopback port of the tray app for the Connect button of the browser page
    "remote_allow_writes": False,  # edits from remote clients, independent from allow_writes
    "remote_keep_awake": True,    # keep the PC awake while remote access is on
    "remote_ip_allowlist": False,  # accept only the published Anthropic and OpenAI ranges
    "remote_extra_redirect_uris": [],  # Advanced: OAuth callback URLs accepted besides claude.ai and chatgpt.com
    "remote_cf": None,            # ids of what the Cloudflare setup created (repair, removal): account_id, tunnel_id,
                                  #   service_id, script, zone_id, dns_record_id; no token or secret
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


def _migrate_1_to_2(d: dict) -> dict:
    """1.8.0: P6 commands became skills (custom_commands -> custom_skills with reference files,
    commands_disabled -> skills_disabled)."""
    d = dict(d)
    if "custom_commands" in d:
        items = d.pop("custom_commands")
        d["custom_skills"] = [dict(it, files=it.get("files", [])) if isinstance(it, dict) else it
                              for it in items] if isinstance(items, list) else items
    if "commands_disabled" in d:
        d["skills_disabled"] = d.pop("commands_disabled")
    d["schema_version"] = 2
    return d


def _migrate_2_to_3(d: dict) -> dict:
    """1.9.0: no built-in skills. The core set comes from the skill shop: the Settings app installs it at its
    next start (p6mcp.core_skills.run_first_start, which needs the network, so not here) and sets
    core_skills_installed; until then the bundled copy is offered. skills_disabled stays for that install."""
    d = dict(d)
    d["core_skills_installed"] = False
    d["schema_version"] = 3
    return d


def _migrate_3_to_4(d: dict) -> dict:
    """3.0.0: P6 EPPM servers (eppm_profiles, eppm_active, data_source eppm). Nothing to convert: the bump keeps
    an older version from reading a file whose data_source it does not know."""
    d = dict(d)
    d.setdefault("eppm_profiles", [])
    d.setdefault("eppm_active", None)
    d["schema_version"] = 4
    return d


MIGRATIONS = {0: _migrate_0_to_1, 1: _migrate_1_to_2, 2: _migrate_2_to_3,
              3: _migrate_3_to_4}  # from version -> version + 1


def migrate(d: dict) -> tuple[dict, list[str]]:
    """Bring a raw config dict to CONFIG_VERSION. Returns (dict, steps applied)."""
    v = d.get("schema_version", 0)
    if not isinstance(v, int) or v < 0:
        raise ConfigError(f"schema_version must be a whole number, got {v!r}")
    if v > CONFIG_VERSION:
        raise ConfigError(f"config.json was written by a newer MCP for Primavera P6 (schema_version {v}, this "
                          f"version reads {CONFIG_VERSION}). Update MCP for Primavera P6.")
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
    if "auto_update" not in d and d.get("update_check") is False:  # a user who turned the old check off
        out["auto_update"] = False
    errs = []

    def enum(key, allowed):
        if out[key] not in allowed:
            errs.append(f"{key} must be one of {list(allowed)}, got {out[key]!r}")

    enum("tool_profile", TOOL_PROFILES)
    enum("transport", TRANSPORTS)
    enum("log_level", LOG_LEVELS)
    enum("backup_scope", BACKUP_SCOPES)
    enum("data_source", DATA_SOURCES)
    if not isinstance(out["xer_files"], list) or not all(isinstance(x, str) and x.strip() for x in out["xer_files"]):
        errs.append("xer_files must be a list of file paths")
    if not isinstance(out["xer_confirmed"], bool):
        errs.append("xer_confirmed must be true or false")
    for key in ("allow_writes", "backup_before_write", "block_when_p6_running", "run_at_startup",
                "start_minimized", "update_check", "auto_update", "shop_enabled", "core_skills_installed"):
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
    if not isinstance(out["clients_ignored"], list) or not all(isinstance(x, str) for x in out["clients_ignored"]):
        errs.append("clients_ignored must be a list of client ids")
    errs += instructions.check(out) + skills.check_config(out)
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
    errs += _check_eppm(out)
    errs += _check_remote(out)
    leaks = _secret_keys(out)
    if leaks:
        errs.append(f"secrets must not be stored in config.json (use the Windows Credential Manager): {leaks}")
    if errs:
        raise ConfigError("; ".join(errs))
    return out


def remote_host_ok(host) -> bool:
    """A public host name as the remote settings store it: letters, digits, dots and hyphens, one dot at least."""
    import re
    return isinstance(host, str) and len(host) <= 253 and re.fullmatch(
        r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]", host) is not None


def _check_remote(out: dict) -> list[str]:
    errs = []
    if out["remote_mode"] not in REMOTE_MODES:
        errs.append(f"remote_mode must be one of {list(REMOTE_MODES)}, got {out['remote_mode']!r}")
    for key in ("remote_enabled", "remote_privacy_ack", "remote_allow_writes", "remote_keep_awake",
                "remote_ip_allowlist"):
        if not isinstance(out[key], bool):
            errs.append(f"{key} must be true or false, got {out[key]!r}")
    port = out["remote_port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        errs.append("remote_port must be a whole number between 1024 and 65535")
    elif port == out["http_port"]:
        errs.append("remote_port must differ from http_port")
    pport = out["remote_pair_port"]
    if not isinstance(pport, int) or isinstance(pport, bool) or not 1024 <= pport <= 65535:
        errs.append("remote_pair_port must be a whole number between 1024 and 65535")
    elif pport in (out["http_port"], port):
        errs.append("remote_pair_port must differ from http_port and remote_port")
    host = out["remote_public_host"]
    if host is not None and not remote_host_ok(host):
        errs.append("remote_public_host must be a host name in lower case (no https://, no port, no path), "
                    f"got {host!r}")
    cf = out["remote_cf"]
    cf_keys = ("account_id", "tunnel_id", "service_id", "script", "zone_id", "dns_record_id")
    if cf is not None and (not isinstance(cf, dict) or set(cf) - set(cf_keys)
                           or not all(v is None or isinstance(v, str) for v in cf.values())):
        errs.append(f"remote_cf must be null or an object with text ids among {list(cf_keys)}")
    uris = out["remote_extra_redirect_uris"]
    if not isinstance(uris, list) or not all(isinstance(u, str) and u.startswith("https://") for u in uris):
        errs.append("remote_extra_redirect_uris must be a list of https:// URLs")
    return errs


def _text(v, empty_ok: bool = False) -> bool:
    return isinstance(v, str) and (empty_ok or bool(v.strip()))


def _check_eppm(out: dict) -> list[str]:
    """eppm_profiles entries and eppm_active (milestone 1 of docs/EPPM_PLAN.md)."""
    errs, names = [], set()
    profs = out["eppm_profiles"]
    if not isinstance(profs, list):
        return ["eppm_profiles must be a list"]
    for i, p in enumerate(profs):
        at = f"eppm_profiles[{i}]"
        if not isinstance(p, dict):
            errs.append(f"{at} must be an object")
            continue
        name = p.get("name")
        if not _text(name):
            errs.append(f"{at}.name must be non-empty text")
        elif name in names:
            errs.append(f"{at}.name {name!r} is used twice")
        names.add(name)
        url = p.get("base_url")
        if not _text(url) or not url.strip().lower().startswith(("https://", "http://")):
            errs.append(f"{at}.base_url must start with https:// or http://")
        for key in ("database", "context", "user"):  # user may still be empty: the test asks for it
            if p.get(key) is not None and not _text(p.get(key), empty_ok=True):
                errs.append(f"{at}.{key} must be text")
        if p.get("auth", "auto") not in EPPM_AUTH:
            errs.append(f"{at}.auth must be one of {list(EPPM_AUTH)}")
        if p.get("ca_bundle") is not None and not _text(p.get("ca_bundle"), empty_ok=True):
            errs.append(f"{at}.ca_bundle must be the path of a certificate file or null")
        t = p.get("timeout_s", 120)
        if not isinstance(t, int) or isinstance(t, bool) or not 5 <= t <= 900:
            errs.append(f"{at}.timeout_s must be a whole number of seconds between 5 and 900")
        if not isinstance(p.get("confirmed", False), bool):
            errs.append(f"{at}.confirmed must be true or false")
        sql = p.get("sql")
        if sql is not None:
            if not isinstance(sql, dict):
                errs.append(f"{at}.sql must be an object or null")
                continue
            if sql.get("driver") not in EPPM_SQL_DRIVERS:
                errs.append(f"{at}.sql.driver must be one of {list(EPPM_SQL_DRIVERS)}")
            for key in ("host", "service"):
                if not _text(sql.get(key)):
                    errs.append(f"{at}.sql.{key} must be non-empty text")
            if not _text(sql.get("user", ""), empty_ok=True):
                errs.append(f"{at}.sql.user must be text")
            port = sql.get("port")
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                errs.append(f"{at}.sql.port must be a whole number between 1 and 65535")
            for key in ("schema", "table_prefix"):
                if sql.get(key) is not None and not _text(sql.get(key), empty_ok=True):
                    errs.append(f"{at}.sql.{key} must be text")
    act = out["eppm_active"]
    if act is not None and act not in names:
        errs.append(f"eppm_active {act!r} is not in eppm_profiles")
    return errs


def eppm_profile(cfg: dict, name: Optional[str] = None) -> Optional[dict]:
    """The named entry of eppm_profiles (the active one by default), or None."""
    name = cfg.get("eppm_active") if name is None else name
    return next((p for p in cfg.get("eppm_profiles", []) if p.get("name") == name), None)


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

NO_DB_MESSAGE = "No database configured. Open MCP for Primavera P6 Settings to select one."


@dataclass(frozen=True)
class Settings:
    """What the server uses for one tool call."""
    source: str                       # "config", "env" (P6_DB_PATH without config file) or "none"
    db_path: str = ""                 # "" when no usable database is selected
    db_name: Optional[str] = None
    db_problem: Optional[str] = None  # why db_path is empty or unusable (user-facing)
    xer_files: tuple[str, ...] = ()   # XER-only mode: db_path is then set by the server (p6mcp.xerstore)
    eppm: Optional[str] = None        # P6 EPPM mode: name of the confirmed active server profile
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
    remote_enabled: bool = False
    remote_mode: str = "relay"
    remote_public_host: Optional[str] = None
    remote_port: int = 39218
    remote_allow_writes: bool = False
    log_level: str = "INFO"
    config_path: str = ""
    config_error: Optional[str] = None
    migrated: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict, compare=False, repr=False)


def _env_flag(name: str) -> Optional[bool]:
    v = os.environ.get(name)
    if v is None or v.strip() == "" or "${" in v:  # unset, or an unresolved bundle template
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
    if "${" in env_db:  # an MCP bundle option left empty: the host passes the template unresolved
        env_db = ""
    cfg = data or copy.deepcopy(DEFAULTS)
    source = "config" if data is not None else ("env" if env_db else "none")

    db_path, db_name, problem = "", None, None
    if err:
        problem = f"{err} Open MCP for Primavera P6 Settings to fix it."
    xer_files: tuple[str, ...] = ()
    eppm: Optional[str] = None
    if env_db:
        db_path, db_name, problem = os.path.abspath(env_db), "P6_DB_PATH", None
    elif not err and cfg.get("data_source") == "xer":
        files = [os.path.abspath(f) for f in cfg.get("xer_files", [])]
        if not files:
            problem = "XER-only mode: no XER file selected. Open MCP for Primavera P6 Settings (Database) to add one."
        elif not cfg.get("xer_confirmed"):
            problem = ("XER-only mode: the XER files were selected but not loaded and checked. Open MCP for "
                       "Primavera P6 Settings (Database) and click Load.")
        elif any(not os.path.isfile(f) for f in files):
            problem = (f"XER file not found: {next(f for f in files if not os.path.isfile(f))}. Open MCP for "
                       "Primavera P6 Settings to fix the list.")
        else:
            xer_files, db_name = tuple(files), "XER files"
    elif not err and cfg.get("data_source") == "eppm":
        prof = eppm_profile(cfg)
        if prof is None:
            problem = "P6 EPPM mode: no server selected. Open MCP for Primavera P6 Settings (Database) to add one."
        elif not prof.get("confirmed"):
            problem = (f"P6 EPPM server {prof['name']!r} was added but its connection was not tested and confirmed. "
                       "Open MCP for Primavera P6 Settings (Database) and click Test connection.")
        else:  # the working copy of a pulled project becomes db_path (p6mcp.eppm, milestone 2)
            eppm, db_name = prof["name"], prof["name"]
            problem = (f"P6 EPPM server {prof['name']!r}: no project pulled yet. Pull one with p6_eppm_pull "
                       "(p6_eppm_projects lists the projects you can read).")
    elif not err:
        active = cfg.get("active_database")
        entry = next((d for d in cfg["databases"] if d.get("name") == active), None)
        if entry is None:
            problem = NO_DB_MESSAGE
        elif not entry.get("confirmed"):
            problem = (f"Database {active!r} was selected but not tested and confirmed. Open MCP for Primavera P6 "
                       "Settings, click Test connection, then confirm it.")
        else:
            db_path, db_name = os.path.abspath(entry["path"]), active
    if db_path and not os.path.isfile(db_path):
        problem = (f"The configured database was not found: {db_path}. Open MCP for Primavera P6 Settings to "
                   "select another one.")
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

    profile = os.environ.get("P6MCP_TOOL_PROFILE", "").strip().lower()
    if not profile or "${" in profile:
        profile = cfg["tool_profile"]
    if profile not in TOOL_PROFILES:
        profile = "basic"
    allow = _env_flag("P6MCP_ALLOW_WRITES")
    if xer_files:
        allow = False  # XER-only mode never writes (P6 EPPM mode writes its local working copy only)
    block = cfg["block_when_p6_running"] and os.environ.get("P6_SKIP_PROCESS_CHECK", "0") != "1"
    return Settings(
        source=source, db_path=db_path, db_name=db_name, db_problem=problem, xer_files=xer_files, eppm=eppm,
        tool_profile=profile, allow_writes=cfg["allow_writes"] if allow is None else allow,
        backup_before_write=cfg["backup_before_write"],
        backup_scope="session" if legacy and data is None else cfg["backup_scope"],
        # v2 layout: never prune a folder of backups the user built up before the config file existed
        backup_dir=backup_dir, backup_retention=0 if legacy and data is None else cfg["backup_retention"],
        edit_log_dir=log_dir,
        legacy_log_dir=legacy_log, block_when_p6_running=block,
        p6_user=os.environ.get("P6_USER") or cfg["p6_user"], trusted_schemas=tuple(cfg["trusted_schemas"]),
        transport=cfg["transport"], http_port=cfg["http_port"], log_level=cfg["log_level"],
        remote_enabled=cfg["remote_enabled"], remote_mode=cfg["remote_mode"],
        remote_public_host=cfg["remote_public_host"], remote_port=cfg["remote_port"],
        remote_allow_writes=cfg["remote_allow_writes"],
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
