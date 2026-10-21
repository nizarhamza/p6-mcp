"""P6 commands: ready-made workflows that chain p6_* tools into one fixed report.

One definition serves two channels:
- the server offers them as MCP prompts (clients that support prompts list them by themselves);
- the Settings app writes them as each client's own command files (VS Code prompt files, Cursor
  commands, Cline workflows, Claude Code commands), see p6mcp.clients.base.Adapter.install_commands.

A command is offered only when every tool it names is offered under the current settings
(policy.listed): in the Basic profile most of them are not. Texts are ASCII, without double hyphens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from p6mcp import policy

PREFIX = "p6-"  # command files are named p6-<name>: /p6-weekly-update-review


@dataclass(frozen=True)
class Arg:
    name: str
    description: str
    default: Optional[str] = None  # None = required


@dataclass(frozen=True)
class Command:
    name: str
    title: str
    description: str
    args: tuple[Arg, ...]
    template: str  # str.format fields: the argument names and {rules}
    custom: bool = False  # written by the user (Commands page): {arg} fields only, the rules appended

    def tools(self) -> list[str]:
        return sorted(set(re.findall(r"\bp6_\w+", self.template)))


RULES = """Rules: name projects by Project ID and activities by Activity ID. Dates, float and the critical
path come from the last P6 schedule run (F9): if schedule_state says stale or edited since the last
schedule, say so first. Durations and float are in days. Read every page you rely on (offset =
next_offset). Writes take two calls: the first one (dry_run=true, the default) only returns the plan and a
confirmation_token; repeat it with dry_run=false and that token only after the user's explicit yes to
that plan. After a write, ask the user to open P6 and press F9."""

PROJECT = Arg("project", "Project ID as shown in P6")

COMMANDS: tuple[Command, ...] = (
    Command(
        "weekly-update-review", "Weekly update review",
        "Weekly schedule update review of a project: progress, critical path, status issues, look-ahead "
        "and baseline variance.",
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
    Command(
        "schedule-quality-audit", "Schedule quality audit",
        "DCMA 14 quality audit of a project with trend, dangling ends, redundant links and float profile; "
        "fixes proposed as dry runs only.",
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
    Command(
        "update-import-safely", "Update import (safe)",
        "Import an updated XER into an existing project safely: inspect, dry run, review, then apply only "
        "on the user's yes.",
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
    Command(
        "delay-whatif", "Delay what-if",
        "What-if impact of delaying one activity: driving path, float origin and the effect on the project "
        "finish and milestones (nothing is written).",
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
    Command(
        "baseline-variance", "Baseline variance",
        "Variance of a project against its baseline (or another project): finish variance, slipped "
        "activities, link changes and SPI.",
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
)
BY_NAME = {c.name: c for c in COMMANDS}


# -- the user's own commands (config `custom_commands`) and the built-in ones turned off (`commands_disabled`)
NAME_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
ARG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
MAX_CUSTOM = 50
MAX_NAME = 40
MAX_TEXT = 8000


def check_custom(item, others: tuple[str, ...] = ()) -> list[str]:
    """Errors of one custom command {name, title, description, args [{name, description, default}],
    template, enabled}. others: names already used (built-in names are always reserved)."""
    if not isinstance(item, dict):
        return ["a command must be an object"]
    errs = []
    name = item.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name) or len(name) > MAX_NAME:
        errs.append(f"name {name!r}: lowercase letters, digits and single hyphens, starting with a letter, "
                    f"at most {MAX_NAME} characters")
    elif name in BY_NAME:
        errs.append(f"name {name!r} is a built-in command")
    elif name in others:
        errs.append(f"name {name!r} is used twice")
    for key in ("title", "description"):
        if not isinstance(item.get(key, ""), str):
            errs.append(f"{key} must be text")
    text = item.get("template")
    if not isinstance(text, str) or not text.strip():
        errs.append("the command text is empty")
    elif len(text) > MAX_TEXT:
        errs.append(f"the command text is longer than {MAX_TEXT} characters")
    if not isinstance(item.get("enabled", True), bool):
        errs.append("enabled must be true or false")
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
    return errs


def check_config(cfg: dict) -> list[str]:
    """Shape errors of the command keys of a config dict (used by config.validate)."""
    errs = []
    off = cfg.get("commands_disabled", [])
    if not isinstance(off, list) or not all(isinstance(x, str) for x in off):
        errs.append("commands_disabled must be a list of command names")
    items = cfg.get("custom_commands", [])
    if not isinstance(items, list):
        return errs + ["custom_commands must be a list"]
    if len(items) > MAX_CUSTOM:
        errs.append(f"custom_commands holds at most {MAX_CUSTOM} commands")
    names: list[str] = []
    for i, it in enumerate(items):
        errs += [f"custom_commands[{i}]: {e}" for e in check_custom(it, tuple(names))]
        names.append(it.get("name") if isinstance(it, dict) else None)
    return errs


def from_config(item: dict) -> Command:
    """A Command from a custom command of the config (checked by config.validate)."""
    args = tuple(Arg(a["name"], a.get("description") or a["name"], a.get("default")) for a in item.get("args", []))
    title = (item.get("title") or "").strip() or item["name"].replace("-", " ").capitalize()
    desc = " ".join((item.get("description") or "").split()) or title
    return Command(item["name"], title, desc, args, item["template"], custom=True)


def custom_commands(cfg: Optional[dict]) -> list[Command]:
    return [from_config(it) for it in (cfg or {}).get("custom_commands") or [] if it.get("enabled", True)]


def all_commands(cfg: Optional[dict] = None) -> list[Command]:
    """Built-in commands that are on, then the user's own ones that are on (None: the built-in ones)."""
    off = set((cfg or {}).get("commands_disabled") or [])
    return [c for c in COMMANDS if c.name not in off] + custom_commands(cfg)


def saved_config() -> dict:
    """The saved settings (an unreadable or missing file: the defaults, as the server)."""
    from p6mcp import config
    try:
        return config.read_file()[0] or config.DEFAULTS
    except config.ConfigError:
        return config.DEFAULTS


def available(cmd: Command, profile: str, allow_writes: bool) -> bool:
    """Every tool the command names is offered under these settings."""
    return all(policy.listed(t, profile, allow_writes) for t in cmd.tools())


def offered(profile: str, allow_writes: bool, cfg: Optional[dict] = None) -> list[Command]:
    """Commands offered under these settings; cfg gives the user's own and the ones turned off."""
    return [c for c in all_commands(cfg) if available(c, profile, allow_writes)]


def render(cmd: Command, values: dict) -> str:
    """The command text with the argument values filled in (missing ones take their default)."""
    vals = {a.name: values.get(a.name) if values.get(a.name) is not None else (a.default or "") for a in cmd.args}
    if cmd.custom:  # the user's text: only {argument} fields are replaced, other braces are kept as written
        text = re.sub(r"\{([a-z][a-z0-9_]*)\}", lambda m: str(vals.get(m.group(1), m.group(0))), cmd.template)
        return text.rstrip() + "\n\n" + RULES
    return cmd.template.format(rules=RULES, **vals)


# -- command files for the clients' own command folders
STYLES = ("vscode", "claude_code", "plain")
MARKER = "<!-- p6mcp command: written by P6 MCP Settings, overwritten on the next install -->"


def _placeholders(cmd: Command, style: str) -> dict:
    if style == "vscode":  # VS Code asks for ${input:name:placeholder} values
        return {a.name: "${input:%s:%s}" % (a.name, a.description) for a in cmd.args}
    if style == "claude_code":  # named arguments of the command (frontmatter 'arguments')
        return {a.name: "$" + a.name for a in cmd.args}
    return {a.name: "<" + a.name + ">" for a in cmd.args}


def _inputs(cmd: Command, style: str) -> str:
    lines = []
    for a in cmd.args:
        dflt = f" (default {a.default})" if a.default else (" (may be empty)" if a.default == "" else "")
        lines.append(f"- {a.name}: {a.description}{dflt}")
    head = ("Inputs (take them from the user's message; ask for a required one that is missing, use the "
            "default when an optional one is empty):" if style != "vscode" else
            "Inputs (use the default when an optional one is empty):")
    return head + "\n" + "\n".join(lines)


def file_name(cmd: Command, style: str) -> str:
    return PREFIX + cmd.name + (".prompt.md" if style == "vscode" else ".md")


def command_file(cmd: Command, style: str) -> str:
    """The file a client reads as the command /p6-<name>."""
    if style not in STYLES:
        raise ValueError(f"style must be one of {STYLES}")
    body = MARKER + "\n\n" + _inputs(cmd, style) + "\n\n" + render(cmd, _placeholders(cmd, style)) + "\n"
    desc = cmd.description.replace('"', "'")
    if style == "vscode":
        front = f'---\ndescription: "{desc}"\nagent: agent\n---\n'
    elif style == "claude_code":
        hint = " ".join(f"<{a.name}>" if a.default is None else f"[{a.name}]" for a in cmd.args)
        names = " ".join(a.name for a in cmd.args)
        front = f'---\ndescription: "{desc}"\nargument-hint: "{hint}"\narguments: {names}\n---\n'
    else:
        front = f"# {cmd.title}\n\n{cmd.description}\n\n"
    return front + body
