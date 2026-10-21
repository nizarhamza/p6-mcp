"""Server instructions: the text every MCP client receives when it connects (serverInfo instructions).

Built-in lines ship with P6 MCP; the user can turn them off (except the locked write rule) and add their own
lines in the Settings app (Instructions page): config keys `instructions_disabled` (built-in ids) and
`custom_instructions` ([{text, enabled}]). The server builds the text once at startup, so a change takes
effect when the MCP client restarts the server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

HEADER = "Primavera P6 Professional database (standalone SQLite)."
CUSTOM_HEADER = "User instructions (follow them in every answer):"
MAX_CUSTOM = 30          # custom lines kept in the config
MAX_CUSTOM_CHARS = 1000  # characters of one custom line


@dataclass(frozen=True)
class Line:
    id: str
    text: str
    locked: bool = False  # always sent: the two-step write rule protects the database


BUILTIN: tuple[Line, ...] = (
    Line("names", "Name projects by Project ID (p6_list_projects) and activities by Activity ID."),
    Line("days", "Durations and float are in days of each activity's calendar."),
    Line("lag", "Lag days use the project's lag calendar option (see lag_rule)."),
    Line("f9", "Dates and float come from the last P6 schedule run (F9); edits do not reschedule."),
    Line("writes", "Writes take two calls: dry_run=true (default) returns the plan and a confirmation_token; after "
                   "the user's explicit yes, repeat the call with dry_run=false and that token (and confirmation_text "
                   "if asked). P6 must be closed. Then ask the user to press F9 in P6.", locked=True),
    Line("diagnose", "p6_diagnose explains configuration errors."),
    Line("planned", "Planned dates are never written; new activities only come from p6_import_xer."),
    Line("paging", "List tools return {total, offset, returned, next_offset, rows}: pass offset=next_offset for the "
                   "next page (null = last). fields= trims activity rows."),
    Line("overview", "Overview first: p6_project_summary."),
    Line("sql", "Use p6_sql_select only when no other tool fits."),
)
BY_ID = {ln.id: ln for ln in BUILTIN}


def enabled(line: Line, cfg: Optional[dict]) -> bool:
    return line.locked or line.id not in set((cfg or {}).get("instructions_disabled") or [])


def custom_lines(cfg: Optional[dict]) -> list[str]:
    """The user's own lines that are on, in their order (blank ones skipped)."""
    out = []
    for item in (cfg or {}).get("custom_instructions") or []:
        text = " ".join(str(item.get("text", "")).split())  # one line each: the list stays readable
        if text and item.get("enabled", True):
            out.append(text)
    return out


def build(cfg: Optional[dict] = None) -> str:
    """The instructions sent to clients under these settings (None: the built-in text)."""
    lines = [HEADER] + ["- " + ln.text for ln in BUILTIN if enabled(ln, cfg)]
    mine = custom_lines(cfg)
    if mine:
        lines += [CUSTOM_HEADER] + ["- " + t for t in mine]
    return "\n".join(lines)


def check(cfg: dict) -> list[str]:
    """Shape errors of the instruction keys of a config dict (used by config.validate)."""
    errs = []
    off = cfg.get("instructions_disabled", [])
    if not isinstance(off, list) or not all(isinstance(x, str) for x in off):
        errs.append("instructions_disabled must be a list of instruction ids")
    items = cfg.get("custom_instructions", [])
    if not isinstance(items, list):
        return errs + ["custom_instructions must be a list"]
    if len(items) > MAX_CUSTOM:
        errs.append(f"custom_instructions holds at most {MAX_CUSTOM} lines")
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not isinstance(it.get("text"), str) or not it["text"].strip():
            errs.append(f"custom_instructions[{i}] must be an object with a non-empty text")
        elif len(it["text"]) > MAX_CUSTOM_CHARS:
            errs.append(f"custom_instructions[{i}].text is longer than {MAX_CUSTOM_CHARS} characters")
        elif not isinstance(it.get("enabled", True), bool):
            errs.append(f"custom_instructions[{i}].enabled must be true or false")
    return errs
