"""Tool tables of README.md and site/llms.txt, generated from the code: the tool list and the access of each tool
come from p6mcp.server (TOOL_HINTS) and p6mcp.policy (kind, Basic profile); the one-line summaries and the
groups below are written for people (the docstrings are written for the model). tests/test_docs.py fails when a
tool has no group or summary, or when a generated block is out of date.

    python tools/gen_docs.py          # rewrite the blocks
    python tools/gen_docs.py --check  # exit 1 when a block is out of date
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("P6MCP_CONFIG", os.path.join(ROOT, "no-config.json"))  # never read the user's settings

from p6mcp import policy  # noqa: E402
from p6mcp import server  # noqa: E402

GROUPS: list[tuple[str, list[str]]] = [
    ("Projects and structure", ["p6_list_projects", "p6_list_eps", "p6_project_summary", "p6_list_wbs",
                                "p6_list_calendars", "p6_list_udfs", "p6_schedule_options"]),
    ("Activities and relationships", ["p6_query_activities", "p6_get_activity", "p6_get_relationships",
                                      "p6_lookahead", "p6_milestones"]),
    ("Critical path and float", ["p6_critical_path", "p6_driving_path", "p6_float_paths", "p6_float_origin",
                                 "p6_float_analysis"]),
    ("Schedule quality", ["p6_health_check", "p6_health_check_compare", "p6_dangling", "p6_redundant_links",
                          "p6_progress_check"]),
    ("Progress and reporting", ["p6_progress_curve", "p6_group_summary", "p6_resource_summary",
                                "p6_export_dashboard"]),
    ("Comparison, what-if and risk", ["p6_compare_projects", "p6_update_integrity", "p6_cpm_validate",
                                      "p6_schedule_whatif", "p6_schedule_risk"]),
    ("XER files", ["p6_inspect_xer", "p6_import_xer", "p6_export_xer"]),
    ("Edits (off by default)", ["p6_update_activity", "p6_update_assignment", "p6_update_assignments",
                                "p6_update_udf", "p6_update_udfs", "p6_add_relationship", "p6_add_relationships",
                                "p6_update_relationship", "p6_update_relationships", "p6_delete_relationship",
                                "p6_delete_relationships", "p6_set_schedule_options", "p6_create_eps",
                                "p6_delete_activity", "p6_delete_project"]),
    ("Backups, log and diagnostics", ["p6_backup_database", "p6_list_backups", "p6_restore_backup", "p6_edit_log",
                                      "p6_diagnose", "p6_sql_select"]),
    ("Skill shop", ["p6_shop_search", "p6_shop_get", "p6_shop_install"]),
]

SUMMARY = {
    "p6_list_projects": "Projects with data date, start, finish, status, baseline and EPS; filter by EPS",
    "p6_list_eps": "EPS tree with depth, path and project counts",
    "p6_project_summary": "One-call overview: dates, activity counts, critical counts, % complete by units, physical "
                          "and duration, baseline variance and SPI",
    "p6_list_wbs": "WBS tree in P6 order with roll-ups: activities, dates, float, units % and baseline planned %",
    "p6_list_calendars": "Calendars with work week, hours per day and holiday exceptions",
    "p6_list_udfs": "User defined fields (UDF columns) with how much each one is used",
    "p6_schedule_options": "Schedule options of a project, of an XER file, or both side by side",
    "p6_query_activities": "Search activities by name, Activity ID, WBS, status, total float, dates, activity "
                           "code or UDF, with chosen columns",
    "p6_get_activity": "Everything about one activity: dates, durations, float, constraints, logic, resources, "
                       "codes, UDFs, notebooks",
    "p6_get_relationships": "Predecessors and successors of an activity, lags in hours and days",
    "p6_lookahead": "What starts, finishes or is in progress in the next weeks, critical work flagged",
    "p6_milestones": "Milestones against the baseline: variance in working and calendar days, float, critical",
    "p6_critical_path": "Critical activities as the project defines them (total float or longest path)",
    "p6_driving_path": "The chain of driving predecessors behind an activity's dates, back to its root cause",
    "p6_float_paths": "Float paths of the last P6 schedule run, in P6 order",
    "p6_float_origin": "Where an activity's total float comes from: the successor chain that sets its late dates",
    "p6_float_analysis": "Total and free float distribution by range",
    "p6_health_check": "DCMA 14-point check (the project's own P6 thresholds when set), critical path test, "
                       "CPLI, BEI and a weighted score",
    "p6_health_check_compare": "Health check now against an earlier run: what got better or worse",
    "p6_dangling": "Open starts and open finishes, each with a suggested link to close it",
    "p6_redundant_links": "Links that are candidates for removal (transitive, duplicate, to completed work)",
    "p6_progress_check": "Out-of-sequence progress and status inconsistencies",
    "p6_progress_curve": "S-curve by month or week (units or cost) against the baseline, earned value at the data "
                         "date",
    "p6_group_summary": "Activities, units and dates grouped by status, type, WBS level or activity code",
    "p6_resource_summary": "Units and costs per resource, and activities per status",
    "p6_export_dashboard": "Excel project controls dashboard: one A3 page with charts, plus detail sheets",
    "p6_compare_projects": "Two updates or a baseline compared activity by activity and link by link, with slip "
                           "causes and float erosion",
    "p6_update_integrity": "Review of an update against the previous one: changes a reviewer should question",
    "p6_cpm_validate": "Runs the built-in CPM engine and compares it with P6's last F9, activity by activity",
    "p6_schedule_whatif": "What-if in memory: add or remove links, change durations or constraints, see the new "
                          "finish",
    "p6_schedule_risk": "Monte Carlo schedule risk in memory: finish percentiles, on-time probability, "
                        "criticality index",
    "p6_inspect_xer": "Reads an XER file without importing it and runs the import pre-flight checks",
    "p6_import_xer": "Imports an XER as a new project, a baseline or an update of an existing project",
    "p6_export_xer": "Exports a project to an XER file, schedule options included",
    "p6_update_activity": "Edits an activity: name, durations, constraint, actual dates, physical % complete",
    "p6_update_assignment": "Sets budgeted, actual or remaining units of one resource assignment",
    "p6_update_assignments": "Sets the units of many assignments in one all-or-nothing change",
    "p6_update_udf": "Sets or clears one activity UDF value",
    "p6_update_udfs": "Sets or clears many activity UDF values in one change",
    "p6_add_relationship": "Adds a relationship (FS, SS, FF, SF, with lag)",
    "p6_add_relationships": "Adds many relationships in one all-or-nothing change",
    "p6_update_relationship": "Changes the type or lag of a relationship",
    "p6_update_relationships": "Changes many relationships in one change",
    "p6_delete_relationship": "Deletes a relationship",
    "p6_delete_relationships": "Deletes many relationships in one change",
    "p6_set_schedule_options": "Writes a project's schedule options, or copies them from an XER file",
    "p6_create_eps": "Creates an EPS node",
    "p6_delete_activity": "Deletes an activity and everything attached to it, as P6 does",
    "p6_delete_project": "Deletes one or more projects, as P6 does",
    "p6_backup_database": "Takes a backup of the P6 database now",
    "p6_list_backups": "Lists the backups, automatic (before writes) and manual",
    "p6_restore_backup": "Puts a backup back in place, after a safety backup of the current database",
    "p6_edit_log": "Every confirmed write, with its parameters, before and after values",
    "p6_diagnose": "Configuration, database, P6 version and P6 running state at a glance",
    "p6_sql_select": "Read-only SQL on the P6 database, as a last resort",
    "p6_shop_search": "Searches the skill shop (ready-made P6 workflows shared on GitHub)",
    "p6_shop_get": "Shows one skill of the shop in full",
    "p6_shop_install": "Installs or updates a skill from the shop, after your confirmation",
}

ACCESS = {policy.READ: "Read", policy.FILE: "Writes a file", policy.WRITE: "Write",
          policy.DESTRUCTIVE: "Delete or replace", policy.CONFIG: "App settings"}


def problems() -> list[str]:
    tools = set(server.TOOL_HINTS)
    grouped = [t for _g, names in GROUPS for t in names]
    out = [f"{t}: no group" for t in sorted(tools - set(grouped))]
    out += [f"{t}: grouped but not a tool" for t in sorted(set(grouped) - tools)]
    out += [f"{t}: listed twice" for t in sorted({t for t in grouped if grouped.count(t) > 1})]
    out += [f"{t}: no summary" for t in sorted(tools - set(SUMMARY))]
    out += [f"{t}: summary but not a tool" for t in sorted(set(SUMMARY) - tools)]
    return out


def counts() -> dict[str, int]:
    kinds = [policy.TOOL_KIND[t] for t in server.TOOL_HINTS]
    return {"total": len(kinds), "read": kinds.count(policy.READ), "basic": len(policy.BASIC_TOOLS),
            "file": kinds.count(policy.FILE), "write": kinds.count(policy.WRITE) + kinds.count(policy.DESTRUCTIVE),
            "config": kinds.count(policy.CONFIG)}


def readme_block() -> str:
    c = counts()
    lines = [f"{c['total']} tools: {c['read']} read-only ({c['basic']} of them in the Basic profile), {c['file']} "
             f"that write a file, {c['write']} write tools (off by default; a dry run, then your confirmation; a "
             f"backup first) and {c['config']} that changes the app's own settings. Generated from the code by "
             "`tools/gen_docs.py`.", ""]
    for group, names in GROUPS:
        lines += [f"**{group}**", "", "| Tool | What it does | Access | Basic |", "|---|---|---|---|"]
        for t in names:
            basic = "yes" if t in policy.BASIC_TOOLS else ""
            lines.append(f"| `{t}` | {SUMMARY[t]} | {ACCESS[policy.TOOL_KIND[t]]} | {basic} |")
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def llms_block() -> str:
    lines = []
    for group, names in GROUPS:
        lines += [f"### {group}", ""]
        lines += [f"- `{t}`: {SUMMARY[t]} ({ACCESS[policy.TOOL_KIND[t]].lower()})" for t in names]
        lines.append("")
    return "\n".join(lines).rstrip("\n")


TARGETS = {"README.md": readme_block, os.path.join("site", "llms.txt"): llms_block}
BLOCK = re.compile(r"(<!-- tools:start -->\n).*?(<!-- tools:end -->)", re.S)


def render(path: str, text: str) -> str:
    if not BLOCK.search(text):
        raise ValueError(f"{path}: no <!-- tools:start --> ... <!-- tools:end --> block")
    return BLOCK.sub(lambda m: m[1] + TARGETS[path]() + "\n" + m[2], text, count=1)


def main(check: bool) -> int:
    errs = problems()
    if errs:
        print("\n".join(errs))
        return 1
    stale = []
    for rel in TARGETS:
        path = os.path.join(ROOT, rel)
        with open(path, encoding="utf-8", newline="") as fh:
            raw = fh.read()
        text = raw.replace("\r\n", "\n")
        new = render(rel, text)
        if new != text:
            stale.append(rel)
            if not check:
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(new.replace("\n", "\r\n") if "\r\n" in raw else new)
    if stale:
        print(("out of date: " if check else "updated: ") + ", ".join(stale))
    return 1 if check and stale else 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv[1:]))
