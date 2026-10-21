"""Edits of the configuration made by the Settings app (no Qt here, so it is tested on its own).

Every function takes the config dict and returns a new one; config.save() validates and writes it.
"""

from __future__ import annotations

import copy
import os
import socket
from typing import Optional

from p6mcp import config, instructions, policy, prompts

RESTART_KEYS = ("tool_profile", "allow_writes", "transport", "http_port", "instructions_disabled",
                "custom_instructions", "commands_disabled", "custom_commands")  # read when the server starts


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


# -- server instructions and P6 commands (Instructions and Commands pages)

def _toggle(data: dict, key: str, value: str, off: bool) -> dict:
    data = copy.deepcopy(data)
    items = [x for x in data.get(key) or [] if x != value]
    data[key] = items + [value] if off else items
    return data


def set_instruction_enabled(data: dict, line_id: str, on: bool) -> dict:
    """Turn a built-in instruction line on or off (the locked one stays on)."""
    line = instructions.BY_ID.get(line_id)
    if line is None:
        raise ValueError(f"No built-in instruction {line_id!r}")
    if line.locked and not on:
        raise ValueError("This instruction protects the database and cannot be turned off")
    return _toggle(data, "instructions_disabled", line_id, not on)


def put_instruction(data: dict, text: str, index: Optional[int] = None) -> dict:
    """Add the user's own instruction line (index None) or replace the text of line index."""
    text = " ".join((text or "").split())
    if not text:
        raise ValueError("The instruction is empty")
    if len(text) > instructions.MAX_CUSTOM_CHARS:
        raise ValueError(f"An instruction holds at most {instructions.MAX_CUSTOM_CHARS} characters")
    data = copy.deepcopy(data)
    items = data.setdefault("custom_instructions", [])
    if index is None:
        if len(items) >= instructions.MAX_CUSTOM:
            raise ValueError(f"At most {instructions.MAX_CUSTOM} instructions")
        items.append({"text": text, "enabled": True})
    else:
        items[index] = dict(items[index], text=text)
    return data


def set_custom_instruction_enabled(data: dict, index: int, on: bool) -> dict:
    data = copy.deepcopy(data)
    data["custom_instructions"][index]["enabled"] = bool(on)
    return data


def remove_instruction(data: dict, index: int) -> dict:
    data = copy.deepcopy(data)
    del data["custom_instructions"][index]
    return data


def move_instruction(data: dict, index: int, step: int) -> dict:
    """Move line index up (step -1) or down (step 1); out of range: unchanged."""
    items = list(data.get("custom_instructions") or [])
    j = index + step
    if not (0 <= index < len(items) and 0 <= j < len(items)):
        return data
    data = copy.deepcopy(data)
    items = data["custom_instructions"]
    items[index], items[j] = items[j], items[index]
    return data


def set_command_enabled(data: dict, name: str, on: bool) -> dict:
    """Turn a built-in command or one of the user's own commands on or off."""
    if name in prompts.BY_NAME:
        return _toggle(data, "commands_disabled", name, not on)
    data = copy.deepcopy(data)
    item = next((c for c in data.get("custom_commands") or [] if c.get("name") == name), None)
    if item is None:
        raise ValueError(f"No command {name!r}")
    item["enabled"] = bool(on)
    return data


def clean_command(item: dict) -> dict:
    """A custom command as the Commands page edits it, trimmed into the stored shape."""
    args = []
    for a in item.get("args") or []:
        name = (a.get("name") or "").strip()
        if not name:
            continue
        dflt = a.get("default")
        args.append({"name": name, "description": (a.get("description") or "").strip(),
                     "default": None if dflt is None else str(dflt).strip()})
    return {"name": (item.get("name") or "").strip().lower(), "title": (item.get("title") or "").strip(),
            "description": " ".join((item.get("description") or "").split()), "args": args,
            "template": (item.get("template") or "").strip("\n"), "enabled": bool(item.get("enabled", True))}


def put_command(data: dict, item: dict, old_name: Optional[str] = None) -> dict:
    """Add the user's own command (old_name None) or replace command old_name. Raises ValueError with
    every problem found."""
    item = clean_command(item)
    data = copy.deepcopy(data)
    items = data.setdefault("custom_commands", [])
    others = tuple(c.get("name") for c in items if c.get("name") != old_name)
    errs = prompts.check_custom(item, others)
    if errs:
        raise ValueError("; ".join(errs))
    idx = next((i for i, c in enumerate(items) if c.get("name") == old_name), None) if old_name else None
    if idx is None:
        if len(items) >= prompts.MAX_CUSTOM:
            raise ValueError(f"At most {prompts.MAX_CUSTOM} commands")
        items.append(item)
    else:
        items[idx] = item
    return data


def remove_command(data: dict, name: str) -> dict:
    data = copy.deepcopy(data)
    data["custom_commands"] = [c for c in data.get("custom_commands") or [] if c.get("name") != name]
    return data


def command_rows(data: dict) -> list[dict]:
    """Every command (built-in first, then the user's own) with whether clients get it under the saved
    tool settings of data: {name, title, description, custom, enabled, offered, missing_tools, command}."""
    profile, writes = data.get("tool_profile", "basic"), bool(data.get("allow_writes")) and \
        data.get("data_source") != "xer"
    off = set(data.get("commands_disabled") or [])
    rows = []
    cmds = [(c, c.name not in off) for c in prompts.COMMANDS]
    cmds += [(prompts.from_config(it), bool(it.get("enabled", True))) for it in data.get("custom_commands") or []]
    for cmd, on in cmds:
        missing = [t for t in cmd.tools() if not policy.listed(t, profile, writes)]
        rows.append({"name": cmd.name, "title": cmd.title, "description": cmd.description, "custom": cmd.custom,
                     "enabled": on, "offered": on and not missing, "missing_tools": missing, "command": cmd})
    return rows


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
