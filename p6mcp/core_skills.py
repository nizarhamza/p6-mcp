"""Core P6 skills: the set P6 MCP installs from the skill shop (p6mcp.shop) instead of built-in skills.

- p6mcp/data/core_skills holds a copy of the catalog's p6mcp.json of every core skill (<name>.json, the bytes
  GitHub serves) and core.json {schema, catalog, commit, skills [{name, version, sha256}]}, written by
  tools/bundle_core_skills.py from a catalog checkout. The copy is used only when GitHub cannot be reached,
  and offered by skills.all_skills until the core set is installed (the server's MCP prompts work meanwhile).
- install(): each core skill from the live shop, else from the copy, stored like any shop install (source
  {shop, version, sha256, content_sha}: the Shop page shows later updates). first_start() runs it once at the
  first start of the Settings app 1.9 (config schema 3, core_skills_installed): the names turned off in the
  old skills_disabled are carried as enabled false. Restore core skills (Skills and Shop pages) runs it again.
- updates(): with auto_update on, the tray brings every skill installed from the shop and not edited here
  (skills.edited) to the shop's version; an edited one is left alone (the Shop page shows its update).
No Qt here; the network goes through opener (the tests fake it).
"""

from __future__ import annotations

import functools
import json
import logging
import os
import urllib.request
from typing import Optional

from p6mcp import shop, skills

log = logging.getLogger("p6mcp.core_skills")
BUNDLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "core_skills")
BUNDLE_INDEX = "core.json"
BUNDLE_SCHEMA = 1


# ----------------------------------------------------------------- the bundled copy

@functools.lru_cache(maxsize=1)
def _bundle() -> tuple[dict, tuple]:
    """(core.json, ((name, item), ...)): the readable entries only (a bad file is logged and left out)."""
    try:
        with open(os.path.join(BUNDLE_DIR, BUNDLE_INDEX), encoding="utf-8") as fh:
            head = json.load(fh)
    except (OSError, ValueError) as e:
        log.warning("no bundled core skills (%s): %s", BUNDLE_DIR, e)
        return {}, ()
    out = []
    for row in head.get("skills") or []:
        name = row.get("name") if isinstance(row, dict) else None
        try:
            if not isinstance(name, str) or not skills.NAME_RE.match(name):
                raise ValueError(f"bad name {name!r}")
            with open(os.path.join(BUNDLE_DIR, name + ".json"), encoding="utf-8") as fh:
                item = json.load(fh)
            errs = shop.check_shared(item) + ([f"holds {item.get('name')!r}"] if item.get("name") != name else [])
            if errs:
                raise ValueError("; ".join(errs))
        except (OSError, ValueError) as e:
            log.warning("bundled core skill %r left out: %s", name, e)
            continue
        out.append((name, dict(item, sha256=row.get("sha256"))))
    return head, tuple(out)


def bundle() -> dict:
    """core.json of the bundled copy ({} when missing)."""
    return dict(_bundle()[0])


def names() -> list[str]:
    """Names of the bundled core skills, in core.json order."""
    return [n for n, _it in _bundle()[1]]


def bundled(name: str) -> Optional[dict]:
    """The bundled p6mcp.json of a core skill with the catalog's sha256 (the shape fetch_skill returns)."""
    return next((dict(it) for n, it in _bundle()[1] if n == name), None)


@functools.lru_cache(maxsize=1)
def _skills() -> tuple:
    return tuple(skills.from_config(shop.to_stored(it)) for _n, it in _bundle()[1])


def bundled_skills() -> list[skills.Skill]:
    """The bundled core set as Skill objects (offered while the core set is not installed)."""
    return list(_skills())


def clear_cache() -> None:
    """Read the bundle again (tests point BUNDLE_DIR elsewhere)."""
    _bundle.cache_clear()
    _skills.cache_clear()


# ----------------------------------------------------------------- installing the core set

def _set_enabled(data: dict, name: str, on: bool) -> None:
    stored = shop.installed_as(data, name)
    if stored is not None:
        stored["enabled"] = on


def _index(data: dict, opener) -> dict:
    """The live index, or none at all when the user turned the skill shop off (then only the bundled copy)."""
    if data.get("shop_enabled", True) is False:
        return {"skills": [], "error": "the skill shop is turned off", "url": shop.REPO_URL}
    return shop.fetch_index(opener)


def install(data: dict, opener=urllib.request.urlopen, index: Optional[dict] = None) -> tuple[dict, dict]:
    """data with the core set installed: the names of the bundle and of the live index entries flagged core,
    each one from the live shop, else (offline, catalog error, a skill needing a newer P6 MCP) from the
    bundled copy. A core skill installed from the shop already is updated in place (its on/off state kept) or
    kept when it is current and not edited; a skill of the user's holding a core name is left alone. After the
    first shop error the rest comes from the copy (no wait per skill); with the shop turned off, only the copy.
    Returns (data, report {shop, bundle, kept, conflicts, failed [{name, error}], error})."""
    idx = index if index is not None else _index(data, opener)
    rep: dict = {"shop": [], "bundle": [], "kept": [], "conflicts": [], "failed": [], "error": idx.get("error")}
    live = {e["name"]: e for e in idx.get("skills") or []}
    want = names() + sorted(n for n, e in live.items() if e.get("core") and n not in names())
    online = not idx.get("error")
    for name in want:
        mine = shop.installed_as(data, name)
        if mine is None and any(c.get("name") == name for c in data.get("custom_skills") or []):
            rep["conflicts"].append(name)  # the user's own skill with this name wins
            continue
        entry, item, origin = live.get(name), None, "shop"
        if online and entry and not entry.get("builtin") and not shop.needs_newer_app(entry):
            if mine is not None and not skills.edited(mine) and (mine.get("source") or {}).get("sha256") == \
                    entry.get("sha256"):
                rep["kept"].append(name)
                continue
            try:
                item = shop.fetch_skill(entry, opener)
            except Exception as e:  # offline meanwhile, a file the index does not match...
                online, rep["error"] = False, f"{name}: {type(e).__name__}: {e}"
        if item is None:
            item, origin = bundled(name), "bundle"
            if item is None:
                rep["failed"].append({"name": name, "error": rep["error"] or "not in the shop"})
                continue
            src = (mine or {}).get("source") or {}
            if mine is not None and not skills.edited(mine) and int(src.get("version") or 0) >= \
                    int(item.get("version") or 1):
                rep["kept"].append(name)  # never an older copy over a current shop install
                continue
        try:
            data = shop.install(data, item)
        except ValueError as e:  # at most MAX_CUSTOM skills...
            rep["failed"].append({"name": name, "error": str(e)})
            continue
        rep[origin].append(name)
    return data, rep


def first_start(data: dict, opener=urllib.request.urlopen, index: Optional[dict] = None) -> tuple[dict, dict]:
    """Config schema 3, once: the core set installed (install), the shop skills of 1.8 given their content_sha
    (1.8 dropped the source of an edited one), the names of skills_disabled installed turned off. When every
    core skill is installed (or held by a skill of the user's), skills_disabled is emptied and
    core_skills_installed set; otherwise both stay and the next start tries again."""
    data = json.loads(json.dumps(data))
    for c in data.get("custom_skills") or []:
        if isinstance(c.get("source"), dict) and not c["source"].get("content_sha"):
            c["source"]["content_sha"] = skills.content_sha(c)
    off = list(data.get("skills_disabled") or [])
    data, rep = install(data, opener, index)
    for name in off:
        if name in rep["shop"] + rep["bundle"]:
            _set_enabled(data, name, False)
    if not rep["failed"]:
        data["skills_disabled"] = []
        data["core_skills_installed"] = True
    rep["turned_off"] = [n for n in off if n in rep["shop"] + rep["bundle"]]
    return data, rep


def updates(data: dict, opener=urllib.request.urlopen, index: Optional[dict] = None) -> tuple[dict, dict]:
    """data with every skill installed from the shop, not edited here and older than the shop's version,
    updated (on/off state and local name kept). Returns (data, {updated, edited, failed, error}): edited =
    the ones left alone because they were changed here."""
    idx = index if index is not None else shop.fetch_index(opener)
    rep: dict = {"updated": [], "edited": [], "failed": [], "error": idx.get("error")}
    if idx.get("error"):
        return data, rep
    live = {e["name"]: e for e in idx.get("skills") or []}
    for stored in list(data.get("custom_skills") or []):
        entry = live.get((stored.get("source") or {}).get("shop"))
        if entry is None or entry.get("builtin") or shop.needs_newer_app(entry) or \
                shop.status(data, entry) != shop.UPDATE:
            continue
        if skills.edited(stored):
            rep["edited"].append(stored["name"])
            continue
        try:
            data = shop.install(data, shop.fetch_skill(entry, opener))
        except Exception as e:
            rep["failed"].append({"name": stored["name"], "error": f"{type(e).__name__}: {e}"})
            continue
        rep["updated"].append(stored["name"])
    return data, rep


# ----------------------------------------------------------------- settings file and clients' folders

def refresh_clients(data: dict) -> list[dict]:
    """Rewrite the P6 skills of every client whose skills folder already holds some (one write per folder:
    Claude Code, Cursor and VS Code share ~/.claude/skills), under the saved tool settings of data. Returns
    [{client, folder, written, removed}] or [{client, error}]."""
    from p6mcp import clients
    profile, writes = data.get("tool_profile", "basic"), bool(data.get("allow_writes")) and \
        data.get("data_source") != "xer"
    seen, out = set(), []
    for ad in clients.ADAPTERS:
        folder = ad.skills_dir()
        try:
            if not folder or os.path.normcase(folder) in seen or ad.skills_state() == clients.SKILLS_NONE:
                continue
            seen.add(os.path.normcase(folder))
            res = ad.install_skills(profile, writes)
            out.append({"client": ad.id, "folder": folder, "written": res["written"], "removed": res["removed"]})
        except Exception as e:  # one client's folder never stops the others
            out.append({"client": ad.id, "error": f"{type(e).__name__}: {e}"})
    return out


def run_first_start(opener=urllib.request.urlopen) -> Optional[dict]:
    """At every start of the Settings app: nothing once the core set is installed; else first_start, save the
    settings (a fresh install gets its config.json here) and rewrite the clients' P6 skills. None when there
    was nothing to do or the settings are unreadable (the window reports them)."""
    from p6mcp import config
    try:
        data = config.read_file()[0]
    except config.ConfigError:
        return None
    data = data if data is not None else config.validate({})
    if not skills.pending_core(data):
        return None
    new, rep = first_start(data, opener)
    config.save(new)
    rep["clients"] = refresh_clients(new)
    return rep


def run_updates(opener=urllib.request.urlopen) -> dict:
    """The tray's skill update (auto_update and shop_enabled on, core set installed): updates(), then save and
    rewrite the clients' P6 skills when a skill changed. Returns the updates() report plus clients, or
    {skipped: reason}."""
    from p6mcp import config
    data = config.read_file()[0]
    if data is None or not data.get("auto_update", True) or data.get("shop_enabled", True) is False:
        return {"skipped": "off", "updated": []}
    if skills.pending_core(data):
        return {"skipped": "core set not installed yet", "updated": []}
    new, rep = updates(data, opener)
    if rep["updated"]:
        config.save(new)
        rep["clients"] = refresh_clients(new)
    return rep
