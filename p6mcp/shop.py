"""Skill shop: P6 skills shared through GitHub, with updates.py the only network calls P6 MCP makes.

- The catalog is the public repository nizarhamza/p6-mcp-shop: index.json and one folder per skill
  (skills/<name>/p6mcp.json = the whole skill, plus SKILL.md and its reference files to browse). Reads are
  anonymous, from raw.githubusercontent.com (no API rate limit), only when the Shop page or a shop tool asks.
- Sharing opens a prefilled GitHub issue in the browser (share_url): no token in the app, GitHub asks the
  user to sign in. The maintainer approves it with a label and the catalog's GitHub Action checks it with
  check_shared (this module, stdlib only, imported by the workflow) and publishes it.
- Without a GitHub account: export_zip / import_file (the zip is also Claude Desktop's skill upload file).
No Qt here: the Shop and Skills pages and the p6_shop_* tools drive it; the tests fake the network (opener).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import urllib.parse
import urllib.request
import zipfile
from typing import Optional

from p6mcp import __version__, policy, skills

REPO = "nizarhamza/p6-mcp-shop"
REPO_URL = f"https://github.com/{REPO}"
RAW_PREFIX = f"https://raw.githubusercontent.com/{REPO}/main/"  # every read is under this prefix
INDEX = "index.json"
ISSUE_TEMPLATE = "share-skill.yml"
ISSUE_FIELD = "skill"  # id of the issue form's JSON field
LICENSE = "Apache-2.0"
TIMEOUT = 8
MAX_BYTES = 256 * 1024  # one catalog file
MAX_URL = 7000  # longer prefilled issue URLs are refused by GitHub (414): the JSON goes to the clipboard
SCHEMA = 1
KIND = "p6mcp-skill"
META = "p6mcp.json"
MAX_ZIP_ENTRIES = 20
MAX_ZIP_BYTES = 1024 * 1024
# shop status of a catalog skill on this PC
AVAILABLE, INSTALLED, UPDATE, CONFLICT, NEWER_APP = "available", "installed", "update", "conflict", "newer_app"


def _version(v: str) -> tuple:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def needs_newer_app(item: dict) -> bool:
    return _version(item.get("min_app_version") or "0.0.0") > _version(__version__)


# ----------------------------------------------------------------- shared form of a skill

def meta(item: dict, **extra) -> dict:
    """p6mcp.json of a skill (export, issue, catalog): the shared fields plus what it needs."""
    tools = skills.item_tools(item)
    out = {"schema": SCHEMA, "kind": KIND, **skills.shared_item(item), "tools": tools,
           "writes": any(t in policy.TOOL_KIND and policy.is_write(t) for t in tools),
           "min_app_version": item.get("min_app_version") or __version__}
    out.update(extra)
    return out


def check_shared(item) -> list[str]:
    """Errors of a shared skill (p6mcp.json of the catalog, an issue or a file): the strict rules of
    skills.check_skill, its kind and schema, and the P6 MCP version it needs. The catalog's workflow calls it."""
    if not isinstance(item, dict):
        return ["a skill must be a JSON object"]
    errs = []
    if item.get("kind", KIND) != KIND or item.get("schema", SCHEMA) != SCHEMA:
        errs.append(f"not a P6 MCP skill (kind {KIND!r}, schema {SCHEMA})")
    if needs_newer_app(item):
        errs.append(f"this skill needs P6 MCP {item['min_app_version']} or later (this is {__version__})")
    return errs + skills.check_skill(skills.shared_item(item), strict=True)


def to_stored(item: dict, source: Optional[dict] = None) -> dict:
    """The custom_skills entry of a shared skill."""
    out = dict(skills.shared_item(item), enabled=True)
    if source:
        out["source"] = source
    return out


# ----------------------------------------------------------------- sharing

def issue_json(item: dict) -> str:
    return json.dumps(meta(item), indent=2, ensure_ascii=False)


def share_url(item: dict) -> tuple[str, Optional[str]]:
    """(URL of the prefilled share issue, text for the clipboard or None). A skill too long for a URL opens
    the form empty: the user pastes the clipboard into its Skill field."""
    errs = check_shared(meta(item))
    if errs:
        raise ValueError("; ".join(errs))
    text = issue_json(item)
    base = f"{REPO_URL}/issues/new?" + urllib.parse.urlencode(
        {"template": ISSUE_TEMPLATE, "title": f"Share: {item['name']}"})
    url = base + "&" + urllib.parse.urlencode({ISSUE_FIELD: text})
    return (url, None) if len(url) <= MAX_URL else (base, text)


# ----------------------------------------------------------------- catalog reads

def _get(path: str, opener) -> bytes:
    url = RAW_PREFIX + path
    if not url.startswith(RAW_PREFIX) or ".." in path or "\\" in path:
        raise ValueError(f"refusing to read {path!r}: not a file of the catalog {REPO}")
    req = urllib.request.Request(url, headers={"User-Agent": f"p6mcp/{__version__}"})
    with opener(req, timeout=TIMEOUT) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"{path} of the catalog is larger than {MAX_BYTES // 1024} KB")
    return data


def fetch_index(opener=urllib.request.urlopen) -> dict:
    """{skills: [catalog entries], error}. Never raises (offline, catalog missing...)."""
    out = {"skills": [], "error": None, "url": REPO_URL}
    try:
        data = json.loads(_get(INDEX, opener).decode("utf-8"))
        rows = data.get("skills") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("index.json has no skills list")
        out["skills"] = sorted((e for e in rows if isinstance(e, dict) and isinstance(e.get("name"), str)
                                and skills.NAME_RE.match(e["name"])), key=lambda e: e["name"])
    except Exception as e:  # offline, 404, malformed file...
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def fetch_skill(entry: dict, opener=urllib.request.urlopen) -> dict:
    """The catalog's p6mcp.json of an index entry, checked against the index's sha256 (the raw cache can
    lag the index by a few minutes: one new read of the index on a mismatch) and check_shared."""
    name = entry["name"]
    if not skills.NAME_RE.match(name):
        raise ValueError(f"bad skill name {name!r} in the catalog")
    path = f"skills/{name}/{META}"
    data = _get(path, opener)
    if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
        fresh = next((e for e in fetch_index(opener)["skills"] if e["name"] == name), None)
        if not fresh or hashlib.sha256(data).hexdigest() != fresh.get("sha256"):
            raise ValueError(f"{path} does not match the catalog index (it may be updating: try again in a few "
                             "minutes)")
        entry = fresh
    try:
        item = json.loads(data.decode("utf-8"))
    except ValueError as e:
        raise ValueError(f"{path} is not valid JSON: {e}") from e
    errs = check_shared(item)
    if errs:
        raise ValueError(f"{name} from the shop: " + "; ".join(errs))
    if item.get("name") != name:
        raise ValueError(f"{path} holds the skill {item.get('name')!r}")
    item["sha256"] = entry.get("sha256")
    return item


def search(rows: list[dict], query: str = "") -> list[dict]:
    """Catalog entries whose name, title, description or author contain every word of query."""
    words = (query or "").lower().split()

    def text(e):
        return " ".join(str(e.get(k) or "") for k in ("name", "title", "description", "author")).lower()
    return [e for e in rows if all(w in text(e) for w in words)]


# ----------------------------------------------------------------- install on this PC

def status(data: dict, entry: dict) -> str:
    """available, installed (same version), update (a newer version), conflict (the name is a built-in
    skill or one of the user's own) or newer_app (needs a newer P6 MCP)."""
    mine = installed_as(data, entry["name"])
    if mine is not None:
        return INSTALLED if (mine.get("source") or {}).get("sha256") == entry.get("sha256") else UPDATE
    if needs_newer_app(entry):
        return NEWER_APP
    if entry["name"] in skills.BY_NAME or any(c.get("name") == entry["name"] for c in data.get("custom_skills") or []):
        return CONFLICT
    return AVAILABLE


def installed_as(data: dict, shop_name: str) -> Optional[dict]:
    """The custom skill installed from the shop skill shop_name (under its name or another one)."""
    return next((c for c in data.get("custom_skills") or [] if (c.get("source") or {}).get("shop") == shop_name),
                None)


def install(data: dict, item: dict, as_name: Optional[str] = None) -> dict:
    """data with the shop skill item (a fetch_skill result) added, or updated when it is installed already
    (under whatever name). A name held by another skill is refused unless as_name gives a free one. The
    stored item keeps source {shop, version, sha256} so the Shop page sees updates."""
    from p6mcp import settings_logic
    old = installed_as(data, item["name"]) if as_name is None else None
    name = as_name or (old["name"] if old else item["name"])
    if old is None and any(c.get("name") == name for c in data.get("custom_skills") or []):
        raise ValueError(f"You already have a skill named {name!r}: install this one under another name")
    stored = to_stored(dict(item, name=name))
    if old is not None:
        stored["enabled"] = bool(old.get("enabled", True))
    data = settings_logic.put_skill(data, stored, old_name=name if old is not None else None)
    src = {"shop": item["name"], "version": int(item.get("version") or 1), "sha256": item.get("sha256")}
    next(c for c in data["custom_skills"] if c["name"] == name)["source"] = src
    return data


# ----------------------------------------------------------------- files (no GitHub account needed)

def export_bytes(item: dict) -> bytes:
    """Zip of p6-<name>/: SKILL.md, the reference files and p6mcp.json (import_file reads it back; the
    same zip is a skill Claude Desktop can upload)."""
    skill = skills.from_config(dict(skills.shared_item(item), enabled=True))
    folder = skills.folder_name(skill)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, text in skills.skill_files(skill).items():
            z.writestr(f"{folder}/{rel}", text)
        z.writestr(f"{folder}/{META}", json.dumps(meta(item, exported_by=f"P6 MCP {__version__}"), indent=2,
                                                  ensure_ascii=False) + "\n")
    return buf.getvalue()


def export_zip(item: dict, path: str) -> str:
    with open(path, "wb") as fh:
        fh.write(export_bytes(item))
    return path


def _frontmatter(text: str) -> tuple[dict, str]:
    """Simple SKILL.md frontmatter (one-line key: value, quoted or not) and the body."""
    m = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", text, re.S)
    if not m:
        return {}, text
    head = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if not sep or line[:1].isspace():
            continue
        v = v.strip()
        if v.startswith('"'):
            try:
                v = json.loads(v)
            except ValueError:
                v = v.strip('"')
        elif v.startswith("'") and v.endswith("'"):
            v = v[1:-1].replace("''", "'")
        head[k.strip()] = v
    return head, m.group(2)


def _from_plain(md: str, refs: dict[str, str]) -> dict:
    """A skill of the user's from a plain SKILL.md (no p6mcp.json): name, description and body; no arguments."""
    head, body = _frontmatter(md)
    name = str(head.get("name") or "").strip()
    if name.startswith(skills.PREFIX):
        name = name[len(skills.PREFIX):]
    body = re.sub(r"\A(<!--.*?-->\s*)+", "", body.strip(), flags=re.S)  # P6 MCP's own markers
    return {"name": name, "title": name.replace("-", " ").capitalize(), "description": head.get("description") or "",
            "args": [], "template": body.strip(), "files": [{"path": p, "text": t} for p, t in sorted(refs.items())]}


def import_bytes(raw: bytes, file_name: str = "skill.zip") -> dict:
    """The skill (custom_skills shape, no source) in a skill file: a zip with p6mcp.json (P6 MCP export),
    else a zip of any skill folder with SKILL.md and text reference files, or a p6mcp.json alone. Scripts
    and other files are refused. Raises ValueError with every problem."""
    if file_name.lower().endswith(".json"):
        try:
            item = json.loads(raw.decode("utf-8-sig"))
        except ValueError as e:
            raise ValueError(f"{file_name} is not valid JSON: {e}") from e
        errs = check_shared(item)
        if errs:
            raise ValueError("; ".join(errs))
        return to_stored(item)
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as e:
        raise ValueError(f"{file_name} is not a zip file") from e
    infos = [i for i in z.infolist() if not i.is_dir()]
    if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
        raise ValueError(f"{file_name} holds too many or too large files for a skill")
    names = [i.filename for i in infos]
    for n in names:
        if n.startswith(("/", "\\")) or "\\" in n or ":" in n or ".." in n.split("/"):
            raise ValueError(f"{file_name}: unsafe path {n!r}")
    tops = {n.split("/")[0] for n in names if "/" in n}
    root = tops.pop() + "/" if len(tops) == 1 and all("/" in n for n in names) else ""
    rel = {n[len(root):]: n for n in names}
    texts = {}
    for r, n in rel.items():
        if r.startswith("__MACOSX/") or r.split("/")[-1] in (".DS_Store", "Thumbs.db"):
            continue
        try:
            texts[r] = z.read(n).decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise ValueError(f"{file_name}: {r} is not UTF-8 text (scripts and binary files are not imported)") from e
    if META in texts:
        item = json.loads(texts[META])
        errs = check_shared(item)
        if errs:
            raise ValueError("; ".join(errs))
        return to_stored(item)
    md = next((r for r in texts if r.lower() == "skill.md"), None)
    if md is None:
        raise ValueError(f"{file_name} has no SKILL.md or {META}")
    refs = {r: t for r, t in texts.items() if r != md}
    bad = [r for r in refs if skills.file_problem(r)]
    if bad:
        raise ValueError(f"{file_name}: only text reference files (.md, .txt, .csv, .json) are imported, not "
                         + ", ".join(bad))
    item = _from_plain(texts[md], refs)
    errs = skills.check_skill(item, strict=True)
    if errs:
        raise ValueError("; ".join(errs))
    return dict(item, enabled=True)


def import_file(path: str) -> dict:
    import os
    if os.path.getsize(path) > MAX_ZIP_BYTES:
        raise ValueError(f"{os.path.basename(path)} is too large for a skill file")
    with open(path, "rb") as fh:
        return import_bytes(fh.read(), os.path.basename(path))
