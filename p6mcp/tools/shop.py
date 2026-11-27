"""Skill shop tools (p6mcp.shop)."""

from __future__ import annotations

import difflib
import os
from typing import Optional

from p6mcp import config as _config
from p6mcp import skills as _skills
from p6mcp import shop as _shop

from p6mcp.core import state as _state
from p6mcp.core.constants import MAX_ROWS
from p6mcp.registry import _plan_gate, _tool
from p6mcp.core.db import _check_page, _slice


# ----------------------------------------------------------------- skill shop (p6mcp.shop)

SHOP_ROW = ("name", "title", "description", "author", "version", "tools", "writes", "min_app_version")


def _shop_settings() -> tuple[str, dict]:
    """(config file, its content) the shop install edits; an unreadable file is refused."""
    path = _state.SETTINGS.config_path or _config.config_path()
    try:
        data = _config.read_file(path)[0]
    except _config.ConfigError as e:
        raise ValueError(f"The MCP for Primavera P6 settings cannot be read ({e}): fix them in MCP for Primavera P6 Settings first.") from e
    return path, data if data is not None else _config.validate({})


def _shop_entry(name: str) -> dict:
    idx = _shop.fetch_index()
    if idx["error"]:
        raise ValueError(f"The skill shop cannot be read ({idx['error']}). Check the internet connection.")
    entry = next((e for e in idx["skills"] if e["name"] == name), None)
    if entry is None:
        close = difflib.get_close_matches(name, [e["name"] for e in idx["skills"]], n=3)
        raise ValueError(f"No skill {name!r} in the shop" + (f"; close names: {', '.join(close)}" if close else "")
                         + ". Use p6_shop_search.")
    return entry


def _skill_clients() -> list:
    """Adapters whose skills folder already holds P6 skills, one per folder (Claude Code, Cursor and VS Code
    share ~/.claude/skills)."""
    from p6mcp import clients as _clients
    seen, out = set(), []
    for ad in _clients.ADAPTERS:
        folder = ad.skills_dir()
        if folder and os.path.normcase(folder) not in seen and ad.skills_state() != _clients.SKILLS_NONE:
            seen.add(os.path.normcase(folder))
            out.append(ad)
    return out


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
def p6_shop_search(query: str = "", offset: int = 0, limit: int = 50) -> dict:
    """Search the MCP for Primavera P6 skill shop: P6 skills (ready-made workflows) shared by users on GitHub and approved
    by the maintainer. query: words found in the name, title, description or author (empty = all). Rows:
    name, title, description, author, version, tools it calls, writes (it uses write tools), status on this
    PC (available, installed, update, conflict = the name is taken here, newer_app = needs a newer MCP for Primavera P6).
    Read a skill with p6_shop_get before installing it."""
    _check_page(offset, limit)
    idx = _shop.fetch_index()
    if idx["error"]:
        raise ValueError(f"The skill shop cannot be read ({idx['error']}). Check the internet connection.")
    _path, data = _shop_settings()
    rows = [dict({k: e.get(k) for k in SHOP_ROW}, status=_shop.status(data, e))
            for e in _shop.search(idx["skills"], query)]
    return dict(_slice(rows, offset, min(limit, MAX_ROWS)), catalog=_shop.REPO_URL)


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
def p6_shop_get(name: str) -> dict:
    """One skill of the shop in full: its instructions, inputs, reference files, the tools it calls (and
    which ones the current settings do not offer), author, version, license, the GitHub issue it was shared
    in and its status on this PC. Show the user what it does before p6_shop_install."""
    entry = _shop_entry(name)
    _path, data = _shop_settings()
    if entry.get("builtin"):  # an entry of a catalog older than P6 MCP 1.9 (built-in skills of 1.8)
        raise ValueError(f"{name} is a built-in skill of P6 MCP 1.8 in an older catalog: nothing to read here.")
    item = _shop.fetch_skill(entry)
    skill = _skills.from_config(_shop.to_stored(item))
    mine = _shop.installed_as(data, name)
    return {**_skills.shared_item(item), "author": item.get("author"), "version": item.get("version"),
            "license": item.get("license"), "tools": skill.tools(), "writes": item.get("writes"),
            "missing_tools": _skills.missing_tools(skill, _state.SETTINGS.tool_profile, _state.SETTINGS.allow_writes),
            "issue_url": f"{_shop.REPO_URL}/issues/{item['issue']}" if item.get("issue") else None,
            "status": _shop.status(data, entry), "installed_as": mine["name"] if mine else None}


@_tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
def p6_shop_install(name: str, install_as: Optional[str] = None, confirm: bool = False) -> dict:
    """Install a skill from the shop, or update it to the shop's version: it joins the user's skills (MCP for Primavera P6
    Settings, Skills page), is written to the skills folders of the clients that already have P6 skills, and
    is offered as an MCP prompt once the MCP client restarts. install_as: another name when the skill's name
    is taken here. Changes MCP for Primavera P6's settings only, never the P6 database."""
    entry = _shop_entry(name)
    if entry.get("builtin"):  # an entry of a catalog older than P6 MCP 1.9 (built-in skills of 1.8)
        raise ValueError(f"{name} is a built-in skill of P6 MCP 1.8 in an older catalog: nothing to install.")
    item = _shop.fetch_skill(entry)
    path, data = _shop_settings()
    if _shop.status(data, entry) == _shop.NEWER_APP:
        raise ValueError(f"{name} needs MCP for Primavera P6 {entry.get('min_app_version')} or later: update MCP for Primavera P6 first.")
    old = _shop.installed_as(data, name) if install_as is None else None
    new = _shop.install(data, item, as_name=install_as)  # refuses a name taken by another skill
    target = install_as or (old["name"] if old else name)
    ads = _skill_clients()
    tools = _skills.from_config(_shop.to_stored(item)).tools()
    plan = [f"{'Update' if old else 'Install'} the skill {target} (version {item.get('version') or 1} by "
            f"{item.get('author') or 'unknown'}) from the MCP for Primavera P6 skill shop into {path}",
            "Tools it calls: " + (", ".join(tools) or "none") + (" (write tools included)" if item.get("writes")
                                                                  else ""),
            "Refresh the P6 skills in: " + (", ".join(f"{ad.skills_dir()} ({ad.name})" for ad in ads) or
                                            "no client yet (Install skills on the Clients page of MCP for Primavera P6 Settings)")]
    gate = _plan_gate(plan, confirm)
    if gate:
        return {"applied": False, "error": gate.replace("The database changed", "The shop or the settings changed")}
    if not confirm:
        return {"dry_run": True, "would_do": plan,
                "skill": {k: item.get(k) for k in ("title", "description", "args", "template", "files")}}
    _config.save(new, path)
    refreshed, errors = [], []
    for ad in ads:
        try:
            ad.install_skills(*_skills.tool_settings(new))
            refreshed.append(ad.skills_dir())
        except (OSError, ValueError, RuntimeError) as e:
            errors.append(f"{ad.name}: {e}")
    out = {"applied": True, "skill": target, "version": item.get("version"), "settings_file": path,
           "skills_folders_refreshed": refreshed,
           "next": "Restart the MCP client to get the skill as a prompt; clients reading a skills folder pick it "
                   "up in a new chat."}
    if errors:
        out["errors"] = errors
    return out
