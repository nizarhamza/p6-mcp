"""P6 skills: ready-made workflows that chain p6_* tools into one fixed report.

One definition serves two channels:
- the Settings app writes each skill as a folder p6-<name> with SKILL.md (Agent Skills format: name and
  description, then the instructions) and its reference files in the clients' skills folders, see
  p6mcp.clients.base.Adapter.install_skills; the client's model picks a skill from its description;
- the server offers them as MCP prompts (Claude Desktop's + menu, /mcp... commands of other clients).

A skill is offered only when every tool it names is offered under the current settings (policy.listed):
in the Basic profile most of them are not.

Since 1.9 no skill is built in: every skill is an entry of the config's custom_skills, the core set installed
from the skill shop by the Settings app at its first start (p6mcp.core_skills). Until then (config key
core_skills_installed false, also without a config file) the core set bundled with the app is offered instead,
so the server's MCP prompts work from the first start. Core texts are ASCII, without double hyphens
(house_problems).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional

from p6mcp import policy

PREFIX = "p6-"  # skill folders are named p6-<name>: p6-weekly-update-review


@dataclass(frozen=True)
class Arg:
    name: str
    description: str
    default: Optional[str] = None  # None = required


@dataclass(frozen=True)
class Skill:
    name: str
    title: str
    description: str  # what it does and when to use it: clients pick a skill by its description
    args: tuple[Arg, ...]
    template: str  # str.format fields: the argument names and {rules}
    custom: bool = False  # written by the user (Skills page): {arg} fields only, the rules appended
    files: tuple[tuple[str, str], ...] = ()  # reference files (path, text) written beside SKILL.md

    def tools(self) -> list[str]:
        return sorted(set(re.findall(r"\bp6_\w+", "\n".join([self.template] + [t for _p, t in self.files]))))


RULES = """## Rules for every P6 skill

Name projects by Project ID and activities by Activity ID. Dates, float and the critical path come from the
last P6 schedule run (F9): when a result carries schedule_state and it says stale, warn the user (in the
skill's warning block when it has one). Durations and float are in days unless a column says otherwise. When
the skill shows one page of a list, keep to that page; otherwise read every page you rely on (offset =
next_offset). Only for a skill that writes: writes take two calls; the first one (dry_run=true, the default)
only returns the plan and a confirmation_token; repeat it with dry_run=false and that token only after the
user's explicit yes to that plan. After a write, ask the user to open P6 and press F9."""

# No skill is built into MCP for Primavera P6 since 1.9: every one comes from the skill shop (p6mcp.shop), the core set
# (p6mcp.core_skills) installed at the first start of the Settings app. Kept empty for older callers.
BUILTIN: tuple[Skill, ...] = ()
BY_NAME = {s.name: s for s in BUILTIN}


# -- the skills of the config (`custom_skills`: shop skills, the core set included, and the user's own ones)
NAME_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")  # with PREFIX, a valid Agent Skills name
ARG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# reference files: a relative path (one folder at most), text formats only (never scripts)
FILE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*(/[a-z0-9][a-z0-9_.-]*)?\.(md|txt|csv|json)$", re.IGNORECASE)
RESERVED_FILES = ("skill.md", "p6mcp.json")  # SKILL.md is written from the skill; p6mcp.json by an export
WIN_RESERVED = {"con", "prn", "aux", "nul"} | {f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)}
MAX_CUSTOM = 50
MAX_NAME = 40
MAX_TEXT = 8000
MAX_TITLE = 200
MAX_DESCRIPTION = 1024  # Agent Skills specification
MAX_FILES = 5
MAX_FILE_TEXT = 12000  # 10,000 before 1.10.0 (skills with larger files carry min_app_version 1.10.0 or later)
MAX_JSON = 60000  # a whole skill as JSON: the shop's GitHub issue body holds 65,536 characters
SHARED_KEYS = ("name", "title", "description", "args", "template", "files")
CONTENT_KEYS = SHARED_KEYS[1:]  # what a skill says: its name, on/off state and source are not content


def _one_line(text: str) -> bool:
    return "\n" not in text and "\r" not in text


def file_problem(path) -> Optional[str]:
    """Why path cannot name a reference file (None = it can)."""
    if not isinstance(path, str) or not FILE_RE.match(path):
        return (f"reference file {path!r}: a relative path (one folder at most) of letters, digits, '-', '_' and "
                "'.', ending in .md, .txt, .csv or .json")
    if path.lower() in RESERVED_FILES or any(p.split(".")[0].lower() in WIN_RESERVED for p in path.split("/")):
        return f"reference file {path!r}: this name is reserved"
    return None


def item_tools(item: dict) -> list[str]:
    """p6_* names in a stored skill's text and reference files."""
    texts = [item.get("template") or ""] + [f.get("text") or "" for f in item.get("files") or []
                                            if isinstance(f, dict)]
    return sorted(set(re.findall(r"\bp6_\w+", "\n".join(t for t in texts if isinstance(t, str)))))


def shared_item(item: dict) -> dict:
    """The fields of a skill that travel (shop, export): no enabled flag, no source."""
    return {k: item.get(k, [] if k in ("args", "files") else "") for k in SHARED_KEYS}


def content_sha(item: dict) -> str:
    """SHA-256 of what a stored skill says (title, description, inputs, text, reference files): recorded as
    source.content_sha when a shop skill is installed, so an edit made here is seen (edited)."""
    body = {k: item.get(k, [] if k in ("args", "files") else "") for k in CONTENT_KEYS}
    text = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def edited(item: dict) -> bool:
    """A stored shop skill whose content changed since its install (no content_sha recorded: edited, so an
    automatic update never replaces what it cannot compare). False for the user's own skills."""
    src = item.get("source")
    if not isinstance(src, dict):
        return False
    return src.get("content_sha") != content_sha(item)


def check_skill(item, others: tuple[str, ...] = (), strict: bool = False) -> list[str]:
    """Errors of one custom skill {name, title, description, args [{name, description, default}], template,
    files [{path, text}], enabled, source}. others: names already used.
    strict: the rules of a new or edited skill (Skills page, shop, file import): description required, on one
    line and at most MAX_DESCRIPTION characters, title on one line, every p6_* tool known, the whole skill at
    most MAX_JSON characters. Stored skills only get the base checks, so older settings keep loading."""
    if not isinstance(item, dict):
        return ["a skill must be an object"]
    errs = []
    name = item.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name) or len(name) > MAX_NAME:
        errs.append(f"name {name!r}: lowercase letters, digits and single hyphens, starting with a letter, "
                    f"at most {MAX_NAME} characters")
    elif name in others:
        errs.append(f"name {name!r} is used twice")
    for key in ("title", "description"):
        if not isinstance(item.get(key, ""), str):
            errs.append(f"{key} must be text")
    text = item.get("template")
    if not isinstance(text, str) or not text.strip():
        errs.append("the skill text is empty")
    elif len(text) > MAX_TEXT:
        errs.append(f"the skill text is longer than {MAX_TEXT} characters")
    if not isinstance(item.get("enabled", True), bool):
        errs.append("enabled must be true or false")
    src = item.get("source")
    if src is not None and not (isinstance(src, dict) and isinstance(src.get("shop"), str)):
        errs.append("source must be {shop, version, sha256, content_sha}")
    files = item.get("files", [])
    if not isinstance(files, list):
        errs.append("files must be a list")
        files = []
    if len(files) > MAX_FILES:
        errs.append(f"at most {MAX_FILES} reference files")
    paths: set[str] = set()
    for f in files:
        p = f.get("path") if isinstance(f, dict) else None
        why = file_problem(p)
        if why:
            errs.append(why)
        elif p.lower() in paths:
            errs.append(f"reference file {p!r} is used twice")
        else:
            paths.add(p.lower())
            if not isinstance(f.get("text"), str):
                errs.append(f"reference file {p!r}: the text must be text")
            elif len(f["text"]) > MAX_FILE_TEXT:
                errs.append(f"reference file {p!r} is longer than {MAX_FILE_TEXT} characters")
    args = item.get("args", [])
    if not isinstance(args, list):
        return errs + ["args must be a list"]
    seen = set()
    for a in args:
        an = a.get("name") if isinstance(a, dict) else None
        if not isinstance(an, str) or not ARG_RE.match(an) or an == "rules":
            errs.append(f"argument {an!r}: lowercase letters, digits and _, starting with a letter (not 'rules')")
        elif an in seen:
            errs.append(f"argument {an!r} is used twice")
        else:
            seen.add(an)
            if not isinstance(a.get("description", ""), str) or not (a.get("default") is None
                                                                        or isinstance(a.get("default"), str)):
                errs.append(f"argument {an!r}: description and default must be text")
    if strict and not errs:
        desc, title = item.get("description") or "", item.get("title") or ""
        if not desc.strip():
            errs.append("the description is empty: clients pick a skill by its description (what it does and "
                        "when to use it)")
        elif len(desc) > MAX_DESCRIPTION or not _one_line(desc):
            errs.append(f"the description must be one line of at most {MAX_DESCRIPTION} characters")
        if len(title) > MAX_TITLE or not _one_line(title):
            errs.append(f"the title must be one line of at most {MAX_TITLE} characters")
        unknown = [t for t in item_tools(item) if t not in policy.TOOL_KIND]
        if unknown:
            errs.append(f"unknown tools {', '.join(unknown)}: check the spelling (a skill made for a newer "
                        "MCP for Primavera P6 needs that version)")
        if len(json.dumps(shared_item(item), ensure_ascii=False)) > MAX_JSON:
            errs.append(f"the skill and its reference files are longer than {MAX_JSON} characters")
    return errs


def check_config(cfg: dict) -> list[str]:
    """Shape errors of the skill keys of a config dict (used by config.validate)."""
    errs = []
    off = cfg.get("skills_disabled", [])
    if not isinstance(off, list) or not all(isinstance(x, str) for x in off):
        errs.append("skills_disabled must be a list of skill names")
    items = cfg.get("custom_skills", [])
    if not isinstance(items, list):
        return errs + ["custom_skills must be a list"]
    if len(items) > MAX_CUSTOM:
        errs.append(f"custom_skills holds at most {MAX_CUSTOM} skills")
    names: list[str] = []
    for i, it in enumerate(items):
        errs += [f"custom_skills[{i}]: {e}" for e in check_skill(it, tuple(names))]
        names.append(it.get("name") if isinstance(it, dict) else None)
    return errs


def from_config(item: dict) -> Skill:
    """A Skill from a custom skill of the config (checked by config.validate)."""
    args = tuple(Arg(a["name"], a.get("description") or a["name"], a.get("default")) for a in item.get("args", []))
    title = (item.get("title") or "").strip() or item["name"].replace("-", " ").capitalize()
    desc = " ".join((item.get("description") or "").split()) or title
    files = tuple((f["path"], f["text"]) for f in item.get("files") or [])
    return Skill(item["name"], title, desc, args, item["template"], custom=True, files=files)


def custom_skills(cfg: Optional[dict]) -> list[Skill]:
    return [from_config(it) for it in (cfg or {}).get("custom_skills") or [] if it.get("enabled", True)]


def pending_core(cfg: Optional[dict]) -> bool:
    """The Settings app has not installed the core set yet (None, a missing file: not yet)."""
    return not (cfg or {}).get("core_skills_installed")


def bundled_core(cfg: Optional[dict]) -> list[Skill]:
    """While pending_core: the core skills bundled with the app, without the names turned off in skills_disabled
    (built-in skills of 1.8) and the ones the config already holds (by name or as a shop skill)."""
    from p6mcp import core_skills
    cfg = cfg or {}
    taken = set(cfg.get("skills_disabled") or [])
    for it in cfg.get("custom_skills") or []:
        taken |= {it.get("name"), (it.get("source") or {}).get("shop")}
    return [s for s in core_skills.bundled_skills() if s.name not in taken]


def all_skills(cfg: Optional[dict] = None) -> list[Skill]:
    """The skills of cfg that are on; before the core set is installed (pending_core), the bundled core set
    first (None: the bundled core set)."""
    return (bundled_core(cfg) if pending_core(cfg) else []) + custom_skills(cfg)


def saved_config() -> dict:
    """The saved settings (an unreadable or missing file: the defaults, as the server)."""
    from p6mcp import config
    try:
        return config.read_file()[0] or config.DEFAULTS
    except config.ConfigError:
        return config.DEFAULTS


def missing_tools(skill: Skill, profile: str, allow_writes: bool) -> list[str]:
    """Tools the skill names that are not offered under these settings (an unknown name, from a typo or a
    newer MCP for Primavera P6, is never offered)."""
    return [t for t in skill.tools() if t not in policy.TOOL_KIND or not policy.listed(t, profile, allow_writes)]


def available(skill: Skill, profile: str, allow_writes: bool) -> bool:
    """Every tool the skill names is offered under these settings."""
    return not missing_tools(skill, profile, allow_writes)


def offered(profile: str, allow_writes: bool, cfg: Optional[dict] = None) -> list[Skill]:
    """Skills offered under these settings; cfg gives the skills and which ones are on."""
    return [s for s in all_skills(cfg) if available(s, profile, allow_writes)]


# -- house rules of the skills MCP for Primavera P6 installs as its core set (bundle) and the catalog marks core
OUR_COMMENTS_RE = re.compile(r"<!-- p6mcp [^>]*-->\n?|^---$", re.M)  # MARKER, files list, frontmatter fences


def tool_calls(text: str) -> list[tuple[str, str]]:
    """(tool, argument text) of every p6_x(...) call written in text: parentheses matched, those inside double
    quoted strings skipped (SQL in p6_sql_select); a call left open is not one."""
    out = []
    for m in re.finditer(r"\b(p6_\w+)\(", text):
        depth, i, quoted = 1, m.end(), False
        while i < len(text) and depth:
            c = text[i]
            if c == '"':
                quoted = not quoted
            elif not quoted and c in "()":
                depth += 1 if c == "(" else -1
            i += 1
        if depth == 0:
            out.append((m.group(1), text[m.end():i - 1]))
    return out


def call_params(args: str) -> list[str]:
    """The name= arguments of a call's argument text (quoted strings left out: name=value inside SQL)."""
    return re.findall(r"(\w+)\s*=(?!=)", re.sub(r'"[^"]*"', '""', args))


def house_problems(skill: Skill, params=None) -> list[str]:
    """Problems of a core skill in its SKILL.md (the app's own comments and the frontmatter fences left out) and
    in every reference file:
    text not ASCII, a double hyphen, an unknown p6_* tool, a name= inside a p6_x(...) call that is not a
    parameter of that tool. params: tool -> its parameter names (the server's signatures; None skips that
    check). Quoted strings in a call are not read for name= (SQL in p6_sql_select)."""
    out = []
    for path, text in skill_files(skill).items():
        if path == "SKILL.md":
            text = OUR_COMMENTS_RE.sub("", text)
        if not text.isascii():
            out.append(f"{path}: not ASCII")
        if "--" in text:
            out.append(f"{path}: a double hyphen")
        unknown = sorted({t for t in re.findall(r"\bp6_\w+", text) if t not in policy.TOOL_KIND})
        if unknown:
            out.append(f"{path}: unknown tools {', '.join(unknown)}")
        for tool, args in tool_calls(text) if params else ():
            if tool not in policy.TOOL_KIND:
                continue
            names = set(params(tool))
            bad = [a for a in call_params(args) if a not in names]
            if bad:
                out.append(f"{path}: {tool}() has no parameter {', '.join(bad)}")
    return out


def render(skill: Skill, values: dict, before_rules: str = "") -> str:
    """The skill text with the argument values filled in (missing ones take their default), then
    before_rules and the rules."""
    vals = {a.name: values.get(a.name) if values.get(a.name) is not None else (a.default or "") for a in skill.args}
    if skill.custom:  # the user's text: only {argument} fields are replaced, other braces are kept as written
        text = re.sub(r"\{([a-z][a-z0-9_]*)\}", lambda m: str(vals.get(m.group(1), m.group(0))), skill.template)
        return text.rstrip() + "\n\n" + before_rules + RULES
    return skill.template.format(rules=before_rules + RULES, **vals)


def prompt_text(skill: Skill, values: dict) -> str:
    """The MCP prompt: the rendered skill with its reference files inline (a prompt has no folder)."""
    refs = "".join(f"Reference file {p}:\n\n{t.rstrip()}\n\n" for p, t in skill.files)
    if refs:  # the rules then read as the skill's own, not as the end of the last reference file
        refs += "End of the reference files.\n\n"
    return render(skill, values, refs)


# -- skill folders for the clients' skills folders
MARKER = "<!-- p6mcp skill: written by P6 MCP Settings, overwritten on the next install -->"
FILES_RE = re.compile(r"<!-- p6mcp files: (\[.*?\]) -->")  # the reference files we wrote beside SKILL.md
# P6 command files of P6 MCP 1.4 to 1.7 (p6-<name>.md, .prompt.md), replaced by the skill folders
COMMAND_MARKER = "<!-- p6mcp command: written by P6 MCP Settings, overwritten on the next install -->"


def folder_name(skill: Skill) -> str:
    return PREFIX + skill.name


def _inputs(skill: Skill) -> str:
    lines = []
    for a in skill.args:
        dflt = f" (default {a.default})" if a.default else (" (may be empty)" if a.default == "" else "")
        lines.append(f"- {a.name}: {a.description}{dflt}")
    return ("Inputs (take them from the user's message; ask for a required one that is missing, use the "
            "default when an optional one is empty):\n" + "\n".join(lines))


def skill_md(skill: Skill) -> str:
    """SKILL.md of the folder p6-<name>: frontmatter with the portable fields only (name = folder name,
    description), the marker, then the instructions with <argument> placeholders."""
    head = f"---\nname: {folder_name(skill)}\ndescription: {json.dumps(skill.description, ensure_ascii=False)}\n---\n"
    parts = [MARKER]
    if skill.files:
        parts[0] += "\n<!-- p6mcp files: " + json.dumps([p for p, _t in skill.files]) + " -->"
    parts.append("# " + skill.title)
    if skill.args:
        parts.append(_inputs(skill))
    refs = ""
    if skill.files:
        refs = ("Reference files in this skill's folder (read the ones the steps need):\n"
                + "\n".join(f"- {p}" for p, _t in skill.files) + "\n\n")
    parts.append(render(skill, {a.name: "<" + a.name + ">" for a in skill.args}, refs))
    return head + "\n\n".join(parts) + "\n"


def skill_files(skill: Skill) -> dict[str, str]:
    """Relative path ('/' separated) -> content of every file of the skill's folder."""
    return {"SKILL.md": skill_md(skill), **{p: t for p, t in skill.files}}


def written_files(skill_md_text: str) -> list[str]:
    """Files a MCP for Primavera P6 SKILL.md says it wrote beside it (SKILL.md first); [] when the marker is missing."""
    if MARKER not in skill_md_text:
        return []
    m = FILES_RE.search(skill_md_text)
    try:
        extra = [p for p in json.loads(m.group(1)) if isinstance(p, str) and not file_problem(p)] if m else []
    except ValueError:
        extra = []
    return ["SKILL.md"] + extra
