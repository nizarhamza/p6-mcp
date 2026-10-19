"""Edits of the configuration made by the Settings app (no Qt here, so it is tested on its own).

Every function takes the config dict and returns a new one; config.save() validates and writes it.
"""

from __future__ import annotations

import copy
import os
import socket
from typing import Optional

from p6mcp import config, policy

RESTART_KEYS = ("tool_profile", "allow_writes", "transport", "http_port")  # read when the server starts


def _dbs(data: dict) -> list[dict]:
    return data.setdefault("databases", [])


def find(data: dict, name: str) -> Optional[dict]:
    return next((d for d in data.get("databases", []) if d.get("name") == name), None)


def unique_name(data: dict, base: str) -> str:
    base = (base or "Database").strip() or "Database"
    names = {d.get("name") for d in data.get("databases", [])}
    if base not in names:
        return base
    n = 2
    while f"{base} {n}" in names:
        n += 1
    return f"{base} {n}"


def name_from_path(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0] if path else "Database"


def add_database(data: dict, path: str, name: Optional[str] = None) -> tuple[dict, str]:
    """New entry (not confirmed). Returns (data, name used)."""
    data = copy.deepcopy(data)
    name = unique_name(data, name or name_from_path(path))
    _dbs(data).append({"name": name, "kind": "sqlite", "path": path, "confirmed": False})
    return data, name


def update_database(data: dict, name: str, new_name: Optional[str] = None, path: Optional[str] = None) -> dict:
    """Rename and/or repoint an entry. A new path clears the confirmation and the test result."""
    data = copy.deepcopy(data)
    entry = find(data, name)
    if entry is None:
        raise config.ConfigError(f"No database named {name!r}")
    if new_name is not None and new_name != name:
        new_name = new_name.strip()
        if not new_name:
            raise config.ConfigError("A database needs a name.")
        if find(data, new_name) is not None:
            raise config.ConfigError(f"The name {new_name!r} is already used.")
        entry["name"] = new_name
        if data.get("active_database") == name:
            data["active_database"] = new_name
    if path is not None and os.path.normcase(os.path.abspath(path)) != \
            os.path.normcase(os.path.abspath(entry.get("path") or "")):
        entry["path"] = path
        for k in ("confirmed", "tested_at", "schema_fingerprint", "schema_label", "projects"):
            entry.pop(k, None)
        entry["confirmed"] = False
        if data.get("active_database") == entry["name"]:
            data["active_database"] = None
    return data


def remove_database(data: dict, name: str) -> dict:
    data = copy.deepcopy(data)
    data["databases"] = [d for d in data.get("databases", []) if d.get("name") != name]
    if data.get("active_database") == name:
        data["active_database"] = None
    return data


def set_active(data: dict, name: str) -> dict:
    entry = find(data, name)
    if entry is None:
        raise config.ConfigError(f"No database named {name!r}")
    if not entry.get("confirmed"):
        raise config.ConfigError("Test the connection and confirm this database before using it.")
    data = copy.deepcopy(data)
    data["active_database"] = name
    return data


def confirm(data: dict, name: str, result: dict) -> dict:
    """The user confirms a successful test: entry confirmed and active."""
    entry = find(data, name)
    if entry is None:
        raise config.ConfigError(f"No database named {name!r}")
    if not result.get("ok"):
        raise config.ConfigError("Only a successful connection test can be confirmed.")
    if os.path.normcase(os.path.abspath(result.get("path", ""))) != os.path.normcase(os.path.abspath(entry["path"])):
        raise config.ConfigError("The test was run on another file: test this database again.")
    sch = result.get("schema") or {}
    return config.confirm_database(data, name, {"fingerprint": sch.get("fingerprint"), "label": sch.get("label"),
                                                "projects": result.get("projects")})


def trust_schema(data: dict, fingerprint: str, trusted: bool = True) -> dict:
    data = copy.deepcopy(data)
    fps = [f for f in data.get("trusted_schemas", []) if f != fingerprint]
    if trusted:
        fps.append(fingerprint)
    data["trusted_schemas"] = fps
    return data


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def free_port(start: int = 39217, tries: int = 50) -> Optional[int]:
    for p in range(start, min(start + tries, 65536)):
        if port_free(p):
            return p
    return None


def restart_needed(saved: dict, current: dict) -> list[str]:
    """Settings that only take effect after the MCP client restarts the server."""
    return [k for k in RESTART_KEYS if saved.get(k, config.DEFAULTS[k]) != current.get(k, config.DEFAULTS[k])]


def tool_rows(profile: str, allow_writes: bool) -> list[dict]:
    """The tools and whether clients get them under these settings, for the Tools page."""
    return [{"tool": t, "kind": k, "available": policy.listed(t, profile, allow_writes)}
            for t, k in sorted(policy.TOOL_KIND.items(), key=lambda x: (policy.KINDS.index(x[1]), x[0]))]


def default_backup_dir() -> str:
    return os.path.join(config.localdata_dir(), "backups")


def load_for_edit() -> tuple[dict, Optional[str]]:
    """(config dict to edit, error) : the file's content, or the defaults when it is missing or broken."""
    try:
        data, _ = config.read_file()
    except config.ConfigError as e:
        return copy.deepcopy(config.DEFAULTS), str(e)
    return (data if data is not None else copy.deepcopy(config.DEFAULTS)), None


def set_aside_broken_file() -> Optional[str]:
    """Rename an unreadable config.json to config.json.broken-<n> (kept for the user), so a fresh one can be saved."""
    path = config.config_path()
    if not os.path.exists(path):
        return None
    n, dest = 1, path + ".broken"
    while os.path.exists(dest):
        n += 1
        dest = f"{path}.broken-{n}"
    os.replace(path, dest)
    return dest
