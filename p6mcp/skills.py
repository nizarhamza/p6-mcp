"""P6 skills: ready-made workflows that chain p6_* tools into one fixed report.

One definition serves two channels:
- the Settings app writes each skill as a folder p6-<name> with SKILL.md (Agent Skills format: name and
  description, then the instructions) and its reference files in the clients' skills folders, see
  p6mcp.clients.base.Adapter.install_skills; the client's model picks a skill from its description;
- the server offers them as MCP prompts (Claude Desktop's + menu, /mcp... commands of other clients).

A skill is offered only when every tool it names is offered under the current settings (policy.listed):
in the Basic profile most of them are not. Texts are ASCII, without double hyphens.
"""

from __future__ import annotations

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


RULES = """Rules: name projects by Project ID and activities by Activity ID. Dates, float and the critical
path come from the last P6 schedule run (F9): if schedule_state says stale or edited since the last
schedule, say so first. Durations and float are in days. Read every page you rely on (offset =
next_offset). Writes take two calls: the first one (dry_run=true, the default) only returns the plan and a
confirmation_token; repeat it with dry_run=false and that token only after the user's explicit yes to
that plan. After a write, ask the user to open P6 and press F9."""

PROJECT = Arg("project", "Project ID as shown in P6")

BUILTIN: tuple[Skill, ...] = (
    Skill(
        "weekly-update-review", "Weekly update review",
        "Weekly schedule update review of a project: progress, critical path, status issues, look-ahead "
        "and baseline variance. Use when the user asks for a weekly update review, a status report or how "
        "a project is progressing.",
        (PROJECT, Arg("weeks", "Look-ahead length in weeks", "3")),
        """Weekly update review of project {project}.

Steps:
1. p6_project_summary(project="{project}"): data date, finish dates, % complete, SPI, schedule_state.
2. p6_progress_check(project="{project}"): out of sequence links and status inconsistencies.
3. p6_critical_path(project="{project}"): open critical activities and their float.
4. p6_lookahead(project="{project}", weeks={weeks}): work in progress, starting and finishing.
5. If the project has a baseline: p6_compare_projects(project="{project}", section="activities",
   min_variance_days=5) for the finish variance and the most slipped activities.

Report, in this order: status and data date (with any stale warning); progress (units %, physical %,
SPI); forecast finish against the must finish date and the baseline; critical path and the activities
that slipped; progress issues to fix in the next update; look-ahead for the next {weeks} weeks; a short
numbered list of actions.

{rules}"""),
    Skill(
        "schedule-quality-audit", "Schedule quality audit",
        "DCMA 14 quality audit of a project with trend, dangling ends, redundant links and float profile; "
        "fixes proposed as dry runs only. Use when the user asks to audit, check or score the quality of a "
        "schedule.",
        (PROJECT,),
        """Schedule quality audit of project {project}.

Steps:
1. p6_health_check(project="{project}"): full DCMA 14 run (it saves a snapshot).
2. p6_health_check_compare(project="{project}", before="previous", after="latest"): trend since the
   previous audit; skip it if there is no previous snapshot.
3. p6_dangling(project="{project}"): open starts and open finishes with suggested links.
4. p6_redundant_links(project="{project}"): links that are candidates for removal.
5. p6_float_analysis(project="{project}"): float distribution and high or negative float.

Report: a table of the DCMA checks (pass or fail, value, threshold, threshold source); what improved or
got worse since the previous audit; findings ranked by impact on the critical path and the finish date,
each with the Activity IDs concerned; proposed fixes. A fix that changes the database is shown as a dry
run only: do not apply it unless the user approves it explicitly.

{rules}"""),
    Skill(
        "update-import-safely", "Update import (safe)",
        "Import an updated XER into an existing project safely: inspect, dry run, review, then apply only "
        "on the user's yes. Use when the user wants to import or load an XER update into a project.",
        (Arg("xer_path", "Full path of the XER file"), PROJECT),
        """Update project {project} from the XER file {xer_path}, safely.

Steps:
1. p6_inspect_xer(path="{xer_path}"): projects in the file, data date, pre-flight errors and warnings.
   If there are pre-flight errors, stop and report them: the import would be refused.
2. p6_import_xer(path="{xer_path}", mode="update", project="{project}", dry_run=true): the dry run.
3. Review it: data_date (database against XER, older), would_refuse and refuse_reasons, counts per
   table, missing_from_xer (activities, links and assignments of the project that the XER does not
   contain) and the warnings.
4. Show the user a short summary of what the import would change and ask two questions: apply it (yes
   or no), and whether missing links or assignments must be removed (remove_missing).
5. Only after an explicit yes: call p6_import_xer again with the same arguments, dry_run=false and the
   confirmation_token of the dry run (and remove_missing if the user asked). Report the backup taken
   and stale_projects.
6. Ask the user to open P6 and press F9, then suggest the weekly update review.

{rules}"""),
    Skill(
        "delay-whatif", "Delay what-if",
        "What-if impact of delaying one activity: driving path, float origin and the effect on the project "
        "finish and milestones (nothing is written). Use when the user asks what happens if an activity is "
        "late or delayed.",
        (PROJECT, Arg("task_code", "Activity ID"), Arg("delay_days", "Extra working days", "10")),
        """What happens to project {project} if activity {task_code} takes {delay_days} more days?

Steps:
1. p6_get_activity(project="{project}", task_code="{task_code}"): remaining and original duration, total
   float, calendar.
2. p6_driving_path(project="{project}", target="{task_code}"): what drives it today.
3. p6_float_origin(project="{project}", task_code="{task_code}"): where its float comes from.
4. p6_schedule_whatif(project="{project}", edits=[{{"op": "duration", "task_code": "{task_code}",
   "remaining_days": <current remaining days + {delay_days}>}}]): use the remaining duration read in
   step 1 (for a not started activity also set original_days the same way).

Report: the activity today (dates, float, drivers); the project finish before and after (calendar and
working days); milestones and constrained activities that move or go negative; the activities that
become critical; whether the engine is trusted (engine.trusted) for this project. Nothing is written to
the database.

{rules}"""),
    Skill(
        "baseline-variance", "Baseline variance",
        "Variance of a project against its baseline (or another project): finish variance, slipped "
        "activities, link changes and SPI. Use when the user asks to compare a project with its baseline or "
        "with another version.",
        (PROJECT, Arg("baseline", "Project ID of the baseline (empty = the project baseline)", "")),
        """Variance of project {project} against the baseline "{baseline}" (when it is empty, use the project
baseline: leave other= and baseline= out of the calls below).

Steps:
1. p6_compare_projects(project="{project}", other="{baseline}", section="activities",
   min_variance_days=1): the summary (finish variance, activities added, removed, later and earlier)
   and the most slipped activities first.
2. p6_compare_projects(project="{project}", other="{baseline}", section="relationships"): logic added,
   removed and lags changed.
3. p6_progress_curve(project="{project}", baseline="{baseline}"): planned against actual and earned at
   the data date (to_date: spi, variance).

Report: project finish against the baseline (calendar days); the slipped activities grouped by cause
when visible (late start, longer duration, logic change); added and removed scope and logic; SPI and
planned against earned at the data date; a short conclusion on the recovery needed. If there is no
baseline, say so and stop.

{rules}"""),
    Skill(
        "export-dashboard", "Excel dashboard",
        "Excel project controls dashboard of a project (one A3 page and detail sheets), then a short "
        "summary of its figures (the database is never written). Use when the user asks for an Excel "
        "dashboard or a one-page project report.",
        (PROJECT, Arg("path", "Full path of the .xlsx file to write"),
         Arg("baseline", "Project ID of the baseline (empty = the project baseline)", ""),
         Arg("prepared_by", "Name printed under the title", ""),
         Arg("revision", "Revision printed under the title", ""),
         Arg("company", "Company name in the sidebar", ""),
         Arg("colors", "Colors 'primary,accent' in hex, for example 1F3864,C00000", "")),
        """Excel dashboard of project {project}, written to {path}.

Optional values: baseline "{baseline}" (empty = the project baseline), prepared_by "{prepared_by}",
revision "{revision}", company "{company}", colors "{colors}". Leave every empty one out of the calls
below.

Steps:
1. p6_project_summary(project="{project}", baseline="{baseline}"): data date, last schedule run,
   baseline and schedule_state. If the schedule is stale or was edited since the last schedule, say so
   and ask the user whether to export anyway or to press F9 in P6 first; wait for the answer. Without a
   baseline, say that the delayed count, the variance cards and the baseline bars will stay empty.
2. p6_export_dashboard(project="{project}", path="{path}", baseline="{baseline}",
   prepared_by="{prepared_by}", revision="{revision}", company="{company}", colors="{colors}"): writes
   the workbook (the database is never written). If a file already exists at that path the call is
   refused: ask the user whether to replace it, and only on a yes repeat it with overwrite=true. If the
   file is open in Excel, ask the user to close it, then run this step again.
3. Read the result: path, sheets (rows per sheet), counts, cards, dcma_failed, warnings.

Report: the file path; the 7 cards with their value and detail, as written on the Dashboard; the
numbers of activities, WBS nodes, milestones and critical activities; the DCMA checks that fail (or
that all pass); every warning. End with how to use the file: the Dashboard prints on one A3 landscape
page and is protected without a password; the Schedule Overview opens folded to WBS level 2 (outline
buttons unfold it); the Critical Path sheet lists the P6 float paths of the last F9 (when P6 stored
none, the near critical activities grouped by total float).

{rules}"""),
)
BY_NAME = {s.name: s for s in BUILTIN}


# -- the user's own skills (config `custom_skills`) and the built-in ones turned off (`skills_disabled`)
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
MAX_FILE_TEXT = 10000
MAX_JSON = 60000  # a whole skill as JSON: the shop's GitHub issue body holds 65,536 characters
SHARED_KEYS = ("name", "title", "description", "args", "template", "files")


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


def check_skill(item, others: tuple[str, ...] = (), strict: bool = False) -> list[str]:
    """Errors of one custom skill {name, title, description, args [{name, description, default}], template,
    files [{path, text}], enabled, source}. others: names already used (built-in names are always reserved).
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
    elif name in BY_NAME:
        errs.append(f"name {name!r} is a built-in skill")
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
        errs.append("source must be {shop, version, sha256}")
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
                        "P6 MCP needs that version)")
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


def all_skills(cfg: Optional[dict] = None) -> list[Skill]:
    """Built-in skills that are on, then the user's own ones that are on (None: the built-in ones)."""
    off = set((cfg or {}).get("skills_disabled") or [])
    return [s for s in BUILTIN if s.name not in off] + custom_skills(cfg)


def saved_config() -> dict:
    """The saved settings (an unreadable or missing file: the defaults, as the server)."""
    from p6mcp import config
    try:
        return config.read_file()[0] or config.DEFAULTS
    except config.ConfigError:
        return config.DEFAULTS


def missing_tools(skill: Skill, profile: str, allow_writes: bool) -> list[str]:
    """Tools the skill names that are not offered under these settings (an unknown name, from a typo or a
    newer P6 MCP, is never offered)."""
    return [t for t in skill.tools() if t not in policy.TOOL_KIND or not policy.listed(t, profile, allow_writes)]


def available(skill: Skill, profile: str, allow_writes: bool) -> bool:
    """Every tool the skill names is offered under these settings."""
    return not missing_tools(skill, profile, allow_writes)


def offered(profile: str, allow_writes: bool, cfg: Optional[dict] = None) -> list[Skill]:
    """Skills offered under these settings; cfg gives the user's own and the ones turned off."""
    return [s for s in all_skills(cfg) if available(s, profile, allow_writes)]


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
    """Files a P6 MCP SKILL.md says it wrote beside it (SKILL.md first); [] when the marker is missing."""
    if MARKER not in skill_md_text:
        return []
    m = FILES_RE.search(skill_md_text)
    try:
        extra = [p for p in json.loads(m.group(1)) if isinstance(p, str) and not file_problem(p)] if m else []
    except ValueError:
        extra = []
    return ["SKILL.md"] + extra
