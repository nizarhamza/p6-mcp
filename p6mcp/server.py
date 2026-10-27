"""
P6 Professional (standalone SQLite) MCP server.

Reads and edits a local Primavera P6 Professional SQLite database (PPMDB*.db).

Safety model:
  * Every write tool is a DRY RUN unless called with confirm=True.
  * A confirmed write refuses to run while P6 (PM.exe) is open.
  * A timestamped backup of the .db file is taken before the first write of each session.
  * New IDs come from the NEXTKEY table (never MAX(id)+1 alone).
  * Edit tools never write planned dates: P6 recalculates them. After writes, open P6 and press F9.
  * Lag days (read and write) use the project's schedule option for the lag calendar
    (predecessor calendar when none is stored, as in P6).
  * XER import (new project / baseline / update) and export: see p6_inspect_xer (pre-flight),
    p6_import_xer, p6_export_xer. An update never deletes activities, and deletes links or
    assignments missing from the XER only when asked (remove_missing). The XER schedule options
    are written to PROJPROP 'scheduling' (p6_set_schedule_options writes them on demand).
  * p6_delete_project deletes whole projects as a P6 delete does (hard delete, REFRDEL logged by
    P6's triggers) and checks for orphans before committing; p6_delete_activity does the same for
    one activity. The batch relationship tools apply many links in one transaction (all or nothing).
  * p6_cpm_validate / p6_schedule_whatif: a CPM engine in memory (never writes) that reproduces P6's F9 on
    the reference project; the DCMA critical path test uses it.
  * Every confirmed write is appended to the edit log (<db folder>\\p6_mcp_logs\\edits_YYYYMMDD.jsonl):
    the stale state of a project is read from it (survives restarts). p6_restore_backup puts a
    backup back after a safety backup.

Config: %APPDATA%\\P6MCP\\config.json, written by the Settings app (see p6mcp.config), re-read on every
tool call. Environment variables still work and win over the file:
  P6_DB_PATH      full path to the .db file (an explicit selection)
  P6_USER         value written to update_user / create_user (default: "ADMIN")
  P6_BACKUP_DIR   folder for backups
  P6_LOG_DIR      folder of the edit log
  P6_SKIP_PROCESS_CHECK  set to 1 to disable the PM.exe check (not recommended)
  P6MCP_TOOL_PROFILE basic|full, P6MCP_ALLOW_WRITES 0|1, P6MCP_CONFIG (path of the config file)

Tool policy (p6mcp.policy, enforced here): Basic profile = a read-only subset; write tools need the
Full profile, allow_writes, a known (or trusted) P6 schema, a dry run and its confirmation token;
destructive tools also need the confirmation text the dry run gives.
"""

from __future__ import annotations

import base64
import bisect
import contextvars
import dataclasses
import difflib
import functools
import html
import inspect
import itertools
import json
import math
import os
import re
import sqlite3
import statistics
import uuid
import subprocess
import sys
import urllib.parse
from collections import Counter
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta
from time import monotonic
from typing import (Annotated, Any, Callable, Iterable, Literal, Mapping, Optional, Sequence, TypedDict,
                    Union, get_args)

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP
try:  # mcp >= 1.8 (1.x and 2.x)
    from mcp.types import ToolAnnotations
except ImportError:  # older mcp or the test stub: tools are registered without hints
    ToolAnnotations = None
try:  # the only exception whose text mcp 2.x passes to the client
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:
    try:
        from mcp.server.fastmcp.exceptions import ToolError
    except ImportError:  # test stub: tools are called directly and raise their own errors
        ToolError = None
try:  # always installed with mcp
    from pydantic import Field
except ImportError:  # test stub without pydantic: annotations are never evaluated
    def Field(*_a, **_k):
        return None

from p6mcp import __version__ as _pkg_version
from p6mcp import config as _config
from p6mcp import confirm as _confirm
from p6mcp import instructions as _instructions
from p6mcp import policy as _policy
from p6mcp import prompts as _prompts
from p6mcp import schema as _schema
from p6mcp import xerstore as _xerstore

# Effective settings: refreshed by _refresh() at the start of every tool call (config.json may change
# while the server runs). STARTUP = the settings the tool list was built with.
SETTINGS: _config.Settings = _config.load()
STARTUP: _config.Settings = SETTINGS
DB_PATH = SETTINGS.db_path
P6_USER = SETTINGS.p6_user
BACKUP_DIR = SETTINGS.backup_dir
LOG_DIR = SETTINGS.edit_log_dir
SKIP_PROC = not SETTINGS.block_when_p6_running
MAX_ROWS = 200
HPD_SQL = "COALESCE(NULLIF(c.day_hr_cnt, 0), 8)"  # hours per day of calendar alias c (0 or NULL -> 8)

REL_TYPES = {"FS": "PR_FS", "SS": "PR_SS", "FF": "PR_FF", "SF": "PR_SF"}
REL_TYPES_REV = {v: k for k, v in REL_TYPES.items()}
CSTR_TYPES = {
    "none": None,
    "start_on_or_after": "CS_MSOA",
    "start_on_or_before": "CS_MSOB",
    "start_on": "CS_MSO",
    "finish_on_or_after": "CS_MEOA",
    "finish_on_or_before": "CS_MEOB",
    "finish_on": "CS_MEO",
    "mandatory_start": "CS_MANDSTART",
    "mandatory_finish": "CS_MANDFIN",
    "as_late_as_possible": "CS_ALAP",
}
CSTR_FINISH = {"CS_MEO", "CS_MEOA", "CS_MEOB", "CS_MANDFIN"}  # a date-only constraint date takes the last shift end
CSTR_LABELS = {"CS_MSO": "Start On", "CS_MSOA": "Start On or After", "CS_MSOB": "Start On or Before",
               "CS_MEO": "Finish On", "CS_MEOA": "Finish On or After", "CS_MEOB": "Finish On or Before",
               "CS_MANDSTART": "Mandatory Start", "CS_MANDFIN": "Mandatory Finish",
               "CS_ALAP": "As Late As Possible"}  # P6 names of the constraint codes (cstr_label keys)


def _cstr_label(code: Optional[str]) -> Optional[str]:
    return None if code is None else CSTR_LABELS.get(code, code)
# Schedule option sched_calendar_on_relationship_lag: calendar whose hours per day convert lag hours.
LAG_RULES = {"rcal_Predecessor": "predecessor calendar", "rcal_Successor": "successor calendar",
             "rcal_24Hour": "24-hour day", "rcal_ProjDefault": "project default calendar"}
ACT_LINKS_CAP = 50  # predecessors, successors, assignments, codes, UDFs, notebooks listed by p6_get_activity
SUMMARY_TYPES = "('TT_LOE', 'TT_WBS')"  # LOE and WBS summary activities: never listed as critical or longest path
HARD_CSTR = "('CS_MSO', 'CS_MEO', 'CS_MSOB', 'CS_MEOB', 'CS_MANDSTART', 'CS_MANDFIN')"  # DCMA hard constraints
DCMA_LIMIT_DAYS = 44.0  # DCMA default limit of high float, high duration and long lags (days of the calendar)
MAT_SQL = "MATERIALIZED " if sqlite3.sqlite_version_info >= (3, 35, 0) else ""  # CTE hint (SQLite 3.35+)
SQL_COUNT_CAP, SQL_COUNT_SECONDS = 1_000_000, 3  # p6_sql_select counts at most this many rows, for this long
SQL_PAGE_BYTES = 150_000  # p6_sql_select ends a page early past this much row JSON (one row at least)
SQL_QUERY_SECONDS = 30  # p6_sql_select stops a query (execute, skip and fetch) after this long
SQL_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list"}  # read-only ones
_SQL_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                getattr(sqlite3, "SQLITE_RECURSIVE", 33)}


def _sql_authorizer(action: int, arg1, arg2, dbname, source) -> int:
    """p6_sql_select: only reading is authorized (no write, ATTACH, schema change or PRAGMA that sets)."""
    if action in _SQL_ALLOWED:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and str(arg1).lower() in SQL_PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY
ACT_EXTRA = ("cstr_type2", "cstr_date2", "restart_date", "reend_date", "expect_end_date", "suspend_date",
             "resume_date")  # TASK columns added by p6_get_activity to the list fields
UDF_VALUE = {"FT_TEXT": "udf_text", "FT_STATICTYPE": "udf_text", "FT_START_DATE": "udf_date",
             "FT_END_DATE": "udf_date", "FT_INT": "udf_number", "FT_MONEY": "udf_number",
             "FT_FLOAT_2_DECIMALS": "udf_number", "FT_CODE": "udf_code"}  # UDF value column per logical type
UDF_KINDS = {"FT_TEXT": "text", "FT_STATICTYPE": "indicator", "FT_START_DATE": "start_date",
             "FT_END_DATE": "finish_date", "FT_INT": "integer", "FT_MONEY": "cost",
             "FT_FLOAT_2_DECIMALS": "number", "FT_CODE": "code"}  # data type names shown to clients
UDF_TEXT_MAX = 255  # UDFVALUE.udf_text length (P6 limit)
UDF_INDICATOR = re.compile(r"UDF_G[0-4]", re.I)  # indicator values as P6 stores them (UDF_G0 to UDF_G4)
UDF_PROJECT_TABLES = ("PROJECT", "PROJWBS", "TASK", "TASKRSRC")  # UDF subjects whose values belong to a project
CAL_EPOCH = date(1899, 12, 30)  # P6 calendar exception dates are day serials from this date
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # Python weekday order
CAL_SCAN_DAYS = 7320  # add_work_hours gives up after 20 years without working time
CAL_WINDOW_DAYS = 3660
# p6_list_calendars(include_exceptions=True): calendars and exception dates per page, dates per calendar (< 200 KB)
CAL_EXC_ROWS, CAL_EXC_PAGE, CAL_EXC_EACH = 50, 800, 100
LOOKAHEAD_DAYS = 14  # p6_lookahead window without days= or weeks=
FLOAT_EDGES = (-20.0, -10.0, -5.0, 0.0, 5.0, 10.0, 20.0, 44.0)  # p6_float_analysis default bucket edges (days)
STATUS_NAMES = {"TK_NotStart": "Not Started", "TK_Active": "In Progress", "TK_Complete": "Completed"}
TYPE_NAMES = {"TT_Task": "Task Dependent", "TT_Rsrc": "Resource Dependent", "TT_Mile": "Start Milestone",
              "TT_FinMile": "Finish Milestone", "TT_LOE": "Level of Effort", "TT_WBS": "WBS Summary"}

# Parameter types shown to MCP clients (module level: the SDK evaluates the string annotations).
# The tools keep their own runtime checks because tests and internal calls bypass validation.
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
DATETIME_PATTERN = r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$"  # what _parse_dt accepts
Limit = Annotated[int, Field(ge=1, le=MAX_ROWS, description="Rows per page (max 200).")]
Offset = Annotated[int, Field(ge=0, description="Rows to skip: pass next_offset from the previous page.")]
ProjectId = Annotated[str, Field(min_length=1, description="Project ID as shown in P6 (p6_list_projects).")]
ActivityId = Annotated[str, Field(min_length=1, description="Activity ID (task_code).")]
Status = Literal["TK_NotStart", "TK_Active", "TK_Complete"]
RelType = Literal["FS", "SS", "FF", "SF"]
Direction = Literal["pred", "succ", "both"]
ImportMode = Literal["new", "update", "baseline"]
CpMode = Literal["project", "total_float", "longest_path"]
Removable = Literal["TASKPRED", "TASKRSRC"]
CstrName = Literal["none", "start_on_or_after", "start_on_or_before", "start_on", "finish_on_or_after",
                   "finish_on_or_before", "finish_on", "mandatory_start", "mandatory_finish",
                   "as_late_as_possible"]
XerTable = Literal["CURRTYPE", "FINTMPL", "OBS", "UDFTYPE", "PROJECT", "CALENDAR", "SCHEDOPTIONS", "PROJWBS",
                   "RSRC", "ACTVTYPE", "RSRCRATE", "TASK", "ACTVCODE", "TASKPRED", "TASKRSRC", "TASKACTV",
                   "UDFVALUE"]
DateStr = Annotated[str, Field(pattern=DATE_PATTERN, description="YYYY-MM-DD")]
DateTimeStr = Annotated[str, Field(pattern=DATETIME_PATTERN, description="YYYY-MM-DD HH:MM")]
Pct = Annotated[float, Field(ge=0, le=100)]
NonNegDays = Annotated[float, Field(ge=0)]
NonNegQty = Annotated[float, Field(ge=0, description="Units as P6 stores them (hours for labor).")]
Depth = Annotated[int, Field(ge=0, description="Levels below the start node (0 = the node only).")]
PathNo = Annotated[int, Field(ge=1, description="Float path number (1 = most critical).")]
BaselineDates = Literal["current", "planned"]  # baseline finish: actual else early, or its planned finish
DurationBasis = Literal["remaining", "original"]
CompareSection = Literal["activities", "relationships"]
DcmaCheck = Literal["logic", "leads", "lags", "long_lags", "fs_links", "hard_constraints", "high_float",
                    "negative_float", "high_duration", "invalid_dates", "resources", "missed_tasks",
                    "critical_path_test", "cpli", "bei"]
ThresholdCheck = Literal[tuple(c for c in get_args(DcmaCheck) if c != "critical_path_test")]  # not run: no threshold
Thresholds = Annotated[dict[ThresholdCheck, float], Field(description="Threshold per check, e.g. {'hard_constraints': 1}.")]
SampleSize = Annotated[int, Field(ge=0, le=50, description="Sample rows per check (max 50).")]
GroupBy = Literal["status", "task_type", "wbs", "code"]
Period = Literal["month", "week"]
Measure = Literal["units", "cost"]
Level = Annotated[int, Field(ge=1, description="Hierarchy level (1 = top).")]
WindowDays = Annotated[int, Field(ge=1, le=366, description="Window length in calendar days (max 366).")]
WindowWeeks = Annotated[int, Field(ge=1, le=52, description="Window length in weeks (max 52).")]
FloatEdges = Annotated[list[float], Field(min_length=1, description="Bucket edges in days, increasing; 0 is always added.")]
ProjectIds = Annotated[list[ProjectId], Field(description="Project IDs (earlier updates of the same schedule).")]
ActivityIds = Annotated[list[ActivityId], Field(description="Activity IDs (task_code).")]
SchedValues = Annotated[dict[str, Any], Field(description="Option name -> value (names as p6_schedule_options shows them).")]
LevelPriority = Annotated[list[str], Field(description="Leveling priorities, 'field:ASC' or 'field:DESC', e.g. ['priority_type:ASC'].")]
class _NewEpsKeys(TypedDict):
    eps_id: str
    eps_name: str


class NewEps(_NewEpsKeys, total=False):
    """EPS node p6_import_xer creates (or reuses when eps_id exists) for a new project."""
    parent_eps: str


ProjectList = Annotated[Union[str, list[str]], Field(description="Project ID, or a list of Project IDs.")]
# Parameters the server adds to every write tool (two-step flow, see p6mcp.confirm)
DryRun = Annotated[bool, Field(description="true (default): preview only, returns the plan and a confirmation_token. "
                                           "false: apply the previewed change (needs confirmation_token).")]
ConfirmToken = Annotated[str, Field(description="confirmation_token returned by the dry run of this same call.")]
ConfirmText = Annotated[str, Field(description="The exact confirmation_text_required given by the dry run.")]
LegacyConfirm = Annotated[bool, Field(description="Deprecated alias of dry_run=false (still needs the token).")]
UdfOp = Literal["eq", "ne", "lt", "le", "gt", "ge", "contains", "empty", "not_empty"]
UdfValue = Annotated[Union[str, float], Field(description="Text, number, 'YYYY-MM-DD[ HH:MM]' date, indicator "
                                                          "(UDF_G0 to UDF_G4) or code short name, per the UDF type.")]


class _UdfFilterKeys(TypedDict):
    udf: str


class UdfFilter(_UdfFilterKeys, total=False):
    """Activities whose UDF value matches: op (default eq) against value (not for empty / not_empty)."""
    op: UdfOp
    value: Union[str, float]


UdfFilters = Annotated[list[UdfFilter], Field(description="UDF conditions, all must hold.")]
UdfNames = Annotated[list[str], Field(description="UDF labels (as in P6 columns) to add to each row under 'udfs'.")]
# Activity list row: the one list of field names (rows, fields= projection, MCP schema)
ActField = Literal["task_code", "task_name", "status_code", "task_type", "wbs_name",
                   "early_start_date", "early_end_date", "late_start_date", "late_end_date",
                   "act_start_date", "act_end_date", "target_start_date", "target_end_date",
                   "cstr_type", "cstr_label", "cstr_date", "phys_complete_pct", "driving_path_flag",
                   "orig_dur_d", "rem_dur_d", "total_float_d", "free_float_d", "float_path", "float_path_order"]
ActFields = Annotated[list[ActField], Field(description="Fields to return (task_code always first); default all.")]
ACT_FIELDS: tuple[str, ...] = get_args(ActField)
ACT_HOURS = {"orig_dur_d": "target_drtn_hr_cnt", "rem_dur_d": "remain_drtn_hr_cnt",
             "total_float_d": "total_float_hr_cnt", "free_float_d": "free_float_hr_cnt"}  # day fields, from hours
assert set(get_args(CstrName)) == set(CSTR_TYPES)
assert set(get_args(RelType)) == set(REL_TYPES)
assert set(ACT_HOURS) <= set(ACT_FIELDS)
# DCMA 14 check -> (DCMA default threshold, rule, PROJPROP 'schedule_check' target, its limit in hours, on/off flag).
# rule: max = pct at most the threshold, min = pct at least, ratio = value at least; threshold None = report only.
DCMA_CHECKS: dict[str, tuple] = {
    "logic": (5, "max", "logic_target", None, "check_logic"),
    "leads": (0, "max", "negative_lag_target", None, "check_negative_lag"),
    "lags": (5, "max", "lag_target", None, "check_lag"),
    "long_lags": (None, "max", "long_lags_target", "long_lags_constraint", "check_long_lags"),
    "fs_links": (90, "min", "relationship_target", None, "check_relationships"),
    "hard_constraints": (5, "max", "hard_constraint_target", None, "check_hard_constraints"),
    "high_float": (5, "max", "large_float_target", "large_float_constraint", "check_large_float"),
    "negative_float": (0, "max", "negative_float_target", None, "check_negative_float"),
    "high_duration": (5, "max", "large_duration_target", "large_durations_constraint", "check_large_duration"),
    "invalid_dates": (0, "max", "progress_date_target", None, "check_progress_duration"),
    "resources": (None, "max", "resouces_target", None, "check_resources"),  # P6's spelling
    "missed_tasks": (5, "max", "late_activities_target", None, "check_late_activities"),
    "critical_path_test": (None, None, "cpt_target", None, "check_cpt"),
    "cpli": (0.95, "ratio", "cpli_target", None, "check_cpli"),
    "bei": (0.95, "ratio", "bei_target", None, "check_bei"),
}
assert list(DCMA_CHECKS) == list(get_args(DcmaCheck))

# built-in lines the user kept plus their own ones (Settings, Instructions page), fixed at startup
INSTRUCTIONS = _instructions.build(STARTUP.raw)

SERVER_VERSION = _pkg_version  # reported to clients as serverInfo.version where the SDK supports it
_version_kw = {"version": SERVER_VERSION} if "version" in inspect.signature(FastMCP.__init__).parameters else {}
mcp = FastMCP("primavera-p6", instructions=INSTRUCTIONS, **_version_kw)  # keywords: positional order differs in 1.x and 2.x
_backup_done = False
_last_backup: Optional[str] = None  # backup in force for this process's writes (named in the edit log)
_last_pruned: list[str] = []  # backups the retention rule removed after the last backup
_cols_cache: dict[str, set[str]] = {}
_log_cache: dict[str, tuple] = {}  # edit log file -> ((mtime_ns, size), entries)
_schema_cache: dict[str, dict] = {}  # db path -> schema.detect() (trust applied at read time)
XER_MANIFEST: Optional[dict] = None  # XER-only mode: files, projects (Project ID renames), warnings
XER_READ_ONLY = ("XER-only mode is read only: the XER files are analysed, never changed. Select a P6 database in "
                 "P6 MCP Settings to write.")
TOOL_HINTS: dict[str, dict[str, bool]] = {}
REGISTERED: dict[str, Callable] = {}  # tool -> what a client call runs (policy gate + two-step write flow)
LISTED: set[str] = set()  # tools offered to clients (fixed at startup)
TOKENS = _confirm.TokenStore()
# Context of the client call being run (two-step flow): {tool, params, token, expected_plan, plan}.
# Direct calls (tests, internal use) have none and keep the plain confirm=True behaviour.
_CALL: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("p6_call", default=None)
WRITE_DOC = ("\n\nTwo steps: (1) call with dry_run=true (default): nothing is written, you get the plan and a "
             "confirmation_token. (2) Show the plan to the user; only after their explicit yes, call again with the "
             "same arguments, dry_run=false and that confirmation_token.")
DESTRUCTIVE_DOC = " This tool deletes or replaces data: step 2 also needs confirmation_text, given by the dry run."


def _refresh() -> _config.Settings:
    """Re-read the settings (cheap while config.json is unchanged). A new database clears the caches
    tied to the old one."""
    global SETTINGS, DB_PATH, P6_USER, BACKUP_DIR, LOG_DIR, SKIP_PROC, _backup_done, _last_backup, XER_MANIFEST
    st = _config.load()
    XER_MANIFEST = None
    if st.xer_files:  # XER-only mode: the read-only store built from the files (cached until they change)
        try:
            path, XER_MANIFEST = _xerstore.ensure(st.xer_files)
            st = dataclasses.replace(st, db_path=path)
        except (_xerstore.XerStoreError, OSError, ValueError) as e:
            st = dataclasses.replace(st, db_problem=f"XER-only mode: the XER files cannot be loaded: {e}")
    if os.path.normcase(st.db_path) != os.path.normcase(DB_PATH):
        _cols_cache.clear()
        _schema_cache.clear()
        _backup_done, _last_backup = False, None
    SETTINGS, DB_PATH, P6_USER, BACKUP_DIR, LOG_DIR = st, st.db_path, st.p6_user, st.backup_dir, st.edit_log_dir
    SKIP_PROC = not st.block_when_p6_running
    return st


def _before_call(tool: str) -> _config.Settings:
    """Settings of this call; raises when the policy refuses the tool or no database is usable."""
    st = _refresh()
    if st.xer_files and _policy.is_write(tool):
        raise ValueError(XER_READ_ONLY)
    why = _policy.refusal(tool, st.tool_profile, st.allow_writes)
    if why:
        raise ValueError(why)
    if not DB_PATH and tool not in _policy.NO_DB_TOOLS:
        raise RuntimeError(st.db_problem or _config.NO_DB_MESSAGE)
    return st


def _schema_info() -> dict:
    """schema.detect() of the current database (cached), with the user's trusted schemas applied."""
    info = _schema_cache.get(DB_PATH)
    if info is None:
        con = _connect()
        try:
            info = _schema_cache[DB_PATH] = _schema.detect(con)
        finally:
            con.close()
    trusted = info["fingerprint"] in SETTINGS.trusted_schemas
    return dict(info, trusted=trusted, writable=info["readable"] and (info["known"] or trusted))


def _write_refusal() -> Optional[str]:
    """Why this database may not be written now (settings and schema), None when it may."""
    if SETTINGS.xer_files:
        return XER_READ_ONLY
    why = _policy.write_refusal(SETTINGS.tool_profile, SETTINGS.allow_writes)
    if why:
        return why
    info = _schema_info()
    return None if info["writable"] else _schema.write_refusal(info)


def _plan_gate(plan: Sequence, confirm: bool) -> Optional[str]:
    """Two-step flow: a dry run records the digest of its plan (the token is bound to it); an apply
    call must rebuild the same plan, else the change is refused (returned message) before commit."""
    ctx = _CALL.get()
    if ctx is None:
        return None
    d = _confirm.plan_digest(plan)
    if not confirm:
        ctx["plan"] = d
        return None
    if ctx.get("expected_plan") is not None and ctx["expected_plan"] != d:
        ctx["plan_mismatch"] = True
        return ("The database changed since the dry run: the plan is different now. Nothing was written. Run the "
                "dry run again, show the new plan to the user and confirm it.")
    return None


def _confirm_text(tool: str, params: dict) -> str:
    """Text a destructive call must repeat (built from its arguments, so it names what goes)."""
    if tool == "p6_delete_project":
        p = params.get("project")
        return "DELETE PROJECT " + ", ".join([p] if isinstance(p, str) else [str(x) for x in p or []])
    if tool == "p6_delete_activity":
        return f"DELETE ACTIVITY {params.get('project')} {params.get('task_code')}"
    if tool == "p6_restore_backup":
        return f"RESTORE {params.get('name')}"
    raise KeyError(tool)


def _preview(tool: str, params: dict) -> dict:
    """Dry run of a write tool that has no confirm parameter of its own."""
    if tool == "p6_backup_database":
        why = _write_refusal()
        if why:
            raise RuntimeError(why)
        plan = [f"Copy {os.path.basename(DB_PATH)} ({os.path.getsize(DB_PATH) / 1048576:.1f} MB) to {_backup_dir()}"]
        _plan_gate(plan, False)
        return {"dry_run": True, "would_do": plan}
    raise ValueError(f"{tool} has no dry run")


def _gated(fn: Callable) -> Callable:
    """The callable clients reach: policy check on every call; for write tools the two-step flow
    (dry_run, confirmation_token, confirmation_text) on top of the tool's own confirm logic."""
    name = fn.__name__
    kind = _policy.kind(name)
    if kind in (_policy.READ, _policy.FILE):
        @functools.wraps(fn)
        def call(*a, **k):
            _before_call(name)
            return fn(*a, **k)
        return call

    sig = inspect.signature(fn)
    has_confirm = "confirm" in sig.parameters
    destructive = kind == _policy.DESTRUCTIVE

    @functools.wraps(fn)
    def call(*a, dry_run: bool = True, confirmation_token: Optional[str] = None,
             confirmation_text: Optional[str] = None, **k):
        _before_call(name)
        ba = sig.bind(*a, **k)
        ba.apply_defaults()
        params = dict(ba.arguments)
        apply = bool(params.pop("confirm", False)) or dry_run is False
        ctx = {"tool": name, "params": params, "token": None, "expected_plan": None, "plan": None}
        if not apply:
            tok = _CALL.set(ctx)
            try:
                res = fn(**params, confirm=False) if has_confirm else _preview(name, params)
            finally:
                _CALL.reset(tok)
            if isinstance(res, dict) and res.get("dry_run") and not res.get("would_refuse"):
                g = TOKENS.issue(name, params, DB_PATH, ctx["plan"] or _confirm.plan_digest(res.get("would_do") or []))
                res["confirmation_token"] = g.token
                res["token_expires_in_s"] = int(TOKENS.ttl)
                extra = ""
                if destructive:
                    res["confirmation_text_required"] = _confirm_text(name, params)
                    extra = f", confirmation_text=\"{res['confirmation_text_required']}\""
                res["next"] = (f"Show this plan to the user. Only after their explicit yes, call {name} again with the "
                               f"same arguments plus dry_run=false, confirmation_token=\"{g.token}\"{extra}. P6 must "
                               "be closed.")
            return res
        if destructive:
            want = _confirm_text(name, params)
            if (confirmation_text or "").strip() != want:
                raise ValueError(f"{name} deletes or replaces data: pass confirmation_text=\"{want}\" exactly, as the "
                                 "dry run asks.")
        g = TOKENS.check(name, params, DB_PATH, confirmation_token)
        ctx.update(token=g.token, expected_plan=g.plan)
        tok = _CALL.set(ctx)
        try:
            res = fn(**params, confirm=True) if has_confirm else fn(**params)
        finally:
            _CALL.reset(tok)
        if ctx.get("plan_mismatch") or not (isinstance(res, dict) and res.get("applied") is False):
            TOKENS.consume(g.token)
        return res

    # The SDK reads __signature__ as it is (no string evaluation): give it evaluated annotations.
    ev = inspect.signature(fn, eval_str=True)
    ps = [p.replace(annotation=LegacyConfirm) if p.name == "confirm" else p for p in ev.parameters.values()]
    P = inspect.Parameter
    ps += [P("dry_run", P.POSITIONAL_OR_KEYWORD, default=True, annotation=DryRun),
           P("confirmation_token", P.POSITIONAL_OR_KEYWORD, default=None, annotation=Optional[ConfirmToken])]
    if destructive:
        ps.append(P("confirmation_text", P.POSITIONAL_OR_KEYWORD, default=None, annotation=Optional[ConfirmText]))
    call.__signature__ = ev.replace(parameters=ps)
    call.__doc__ = (fn.__doc__ or "").rstrip() + WRITE_DOC + (DESTRUCTIVE_DOC if destructive else "")
    return call
_ro_cons: ContextVar[Optional[list]] = ContextVar("_ro_cons", default=None)  # read connections of the running tool


def _tool(read_only: bool = True, destructive: bool = False, idempotent: bool = True):
    """Register an MCP tool with its annotations (recorded in TOOL_HINTS even without mcp) and its
    gated callable (REGISTERED). Only the tools the startup settings allow are offered to clients
    (policy.listed). The SDK gets a wrapper that turns any error into ToolError, so the client sees
    the reason (mcp 2.x reports any other exception as a bare 'Error executing tool'). The module
    keeps the plain function: direct calls and tests still get ValueError and friends."""
    hints = {"readOnlyHint": read_only, "destructiveHint": destructive and not read_only,
             "idempotentHint": idempotent, "openWorldHint": False}

    def deco(tool_fn):
        name = tool_fn.__name__
        TOOL_HINTS[name] = hints

        @functools.wraps(tool_fn)
        def fn(*a, **k):  # closes the read-only connections the call opened (nested tool calls close their own)
            opened = _ro_cons.get()
            token = None
            if opened is None:
                opened = []
                token = _ro_cons.set(opened)
            mark = len(opened)
            try:
                return tool_fn(*a, **k)
            finally:
                for c in opened[mark:]:
                    try:
                        c.close()
                    except sqlite3.Error:
                        pass
                del opened[mark:]
                if token is not None:
                    _ro_cons.reset(token)
        gated = REGISTERED[name] = _gated(fn)
        if not _policy.listed(name, STARTUP.tool_profile, STARTUP.allow_writes):
            return fn
        LISTED.add(name)
        reg = gated
        if ToolError is not None:
            @functools.wraps(gated)
            def reg(*a, **k):
                try:
                    return gated(*a, **k)
                except ToolError:
                    raise
                except Exception as e:
                    msg = str(e) if isinstance(e, (ValueError, RuntimeError)) else f"{type(e).__name__}: {e}"
                    raise ToolError(msg) from e
        if ToolAnnotations is None:
            mcp.tool()(reg)
        else:
            mcp.tool(annotations=ToolAnnotations.model_validate(hints))(reg)
        return fn
    return deco


PROMPTS: dict[str, Callable[..., str]] = {}  # P6 command -> its MCP prompt function (offered under the settings)


def _register_prompts() -> None:
    """Offer the P6 commands (p6mcp.prompts: built-in ones left on and the user's own) whose tools are all
    offered, as MCP prompts."""
    for cmd in _prompts.offered(STARTUP.tool_profile, STARTUP.allow_writes, STARTUP.raw):
        def fn(_cmd=cmd, **values) -> str:
            return _prompts.render(_cmd, values)
        P = inspect.Parameter
        fn.__signature__ = inspect.Signature(
            [P(a.name, P.KEYWORD_ONLY, default=P.empty if a.default is None else a.default, annotation=str)
             for a in cmd.args], return_annotation=str)
        fn.__annotations__ = {**{a.name: str for a in cmd.args}, "return": str}  # read by the SDK's pydantic
        fn.__name__ = cmd.name.replace("-", "_")
        fn.__doc__ = cmd.description
        PROMPTS[cmd.name] = fn
        if hasattr(mcp, "prompt"):
            kw = {"title": cmd.title} if "title" in inspect.signature(mcp.prompt).parameters else {}
            mcp.prompt(name=cmd.name, description=cmd.description, **kw)(fn)


# ----------------------------------------------------------------- helpers

def _connect(readonly: bool = True) -> sqlite3.Connection:
    if not DB_PATH:
        raise RuntimeError(SETTINGS.db_problem or _config.NO_DB_MESSAGE)
    if not os.path.isfile(DB_PATH):
        raise RuntimeError(f"The configured database was not found: {DB_PATH}. Open P6 MCP Settings to select "
                           "another one.")
    if readonly:
        con = sqlite3.connect(_ro_uri(DB_PATH), uri=True, timeout=5)
        con.execute("PRAGMA query_only = ON")  # second lock besides mode=ro: no statement may write
        opened = _ro_cons.get()
        if opened is not None:  # inside a tool call: closed when the tool returns
            opened.append(con)
    else:
        con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _ro_uri(path: str) -> str:
    """Read-only SQLite URI of a file path ('#', '%', '?' and spaces escaped)."""
    return "file:" + urllib.parse.quote(path.replace("\\", "/"), safe="/:") + "?mode=ro"


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    if table not in _cols_cache:
        _cols_cache[table] = {r[1].lower() for r in con.execute(f"PRAGMA table_info({table})")}
    return _cols_cache[table]


def _alive(con: sqlite3.Connection, table: str, alias: str = "") -> str:
    """Filter for logically deleted rows, if the table has the column."""
    p = f"{alias}." if alias else ""
    return f" AND {p}delete_session_id IS NULL" if "delete_session_id" in _cols(con, table) else ""


def _dict(r: sqlite3.Row) -> dict:
    """Row -> dict with lowercase keys (P6 stores column names in upper case)."""
    return {k.lower(): r[k] for k in r.keys()}


def _now_str(con: sqlite3.Connection) -> str:
    """Match the date format already used in the database."""
    sample = con.execute(
        "SELECT update_date FROM TASK WHERE update_date IS NOT NULL LIMIT 1"
    ).fetchone()
    now = datetime.now()
    if sample and isinstance(sample[0], str) and "T" in sample[0]:
        return now.strftime("%Y-%m-%dT%H:%M:%S")
    return now.strftime("%Y-%m-%d %H:%M:%S")


def _p6_running() -> bool:
    if SKIP_PROC or sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq PM.exe", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return "PM.exe" in out
    except Exception:
        return False


def _backup_dir() -> str:
    return BACKUP_DIR or os.path.join(os.path.dirname(DB_PATH), "p6_mcp_backups")


def _prune_backups(keep: int, protect: Iterable[str] = ()) -> list[str]:
    """Retention: keep the newest `keep` automatic backups of this database (plain backups, named
    <db stem>_<stamp>[_n].db); pre_restore and other kinds are never removed. keep 0 = keep all.
    Returns the files removed."""
    if keep <= 0:
        return []
    folder, stem = _backup_dir(), os.path.splitext(os.path.basename(DB_PATH))[0].lower()
    protect = {os.path.normcase(os.path.abspath(p)) for p in protect if p}
    plain = []
    for name in os.listdir(folder):
        m = BACKUP_NAME.search(name)
        if m and not m[2] and name.lower()[:-len(m[0])] == stem:
            plain.append((m[1], name))
    removed = []
    for _, name in sorted(plain, reverse=True)[keep:]:
        path = os.path.join(folder, name)
        if os.path.normcase(os.path.abspath(path)) in protect:
            continue
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass  # in use or read only: kept, tried again after the next backup
    return removed


def _backup(kind: str = "") -> str:
    """Copy of the database in the backup folder: <db name>_<YYYYMMDD_HHMMSS>[_kind].db (never
    overwrites: _2, _3... appended)."""
    global _backup_done, _last_backup, _last_pruned
    folder = _backup_dir()
    os.makedirs(folder, exist_ok=True)
    base = f"{os.path.splitext(os.path.basename(DB_PATH))[0]}_{datetime.now():%Y%m%d_%H%M%S}" + (f"_{kind}" if kind else "")
    dest, n = os.path.join(folder, base + ".db"), 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(folder, f"{base}_{n}.db")
    try:
        with closing(_connect(readonly=True)) as src, closing(sqlite3.connect(dest)) as dst:
            src.backup(dst)  # consistent copy even if the file is busy
    except BaseException:
        if os.path.exists(dest):  # a partial copy must not look like a usable backup
            os.remove(dest)
        raise
    _backup_done, _last_backup = True, dest
    if not kind:
        _last_pruned = _prune_backups(SETTINGS.backup_retention, protect=[dest])
    return dest


# ----------------------------------------------------------------- edit log (persistent)
# One JSON line per logged change in <db folder>/p6_mcp_logs/edits_YYYYMMDD.jsonl (P6_LOG_DIR overrides
# the folder). An entry names its database (file name), so copies sharing a folder keep apart.
# schedule_state reads it: a project is stale while an entry recorded under its current
# PROJECT.last_schedule_date exists and was not undone by a restore.

def _log_dir() -> str:
    return LOG_DIR or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "p6_mcp_logs")


def _log_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _log_dirs() -> list[str]:
    """Folders read for this database's entries: the configured one, then the v2 folder beside the
    database when it differs (history written before the move to the configured folder)."""
    out = [_log_dir()]
    if SETTINGS.legacy_log_dir and os.path.normcase(SETTINGS.legacy_log_dir) != os.path.normcase(out[0]):
        out.append(SETTINGS.legacy_log_dir)
    return out


def _log_write(entries: Sequence[dict]) -> str:
    folder = _log_dir()
    os.makedirs(folder, exist_ok=True)
    ctx = _CALL.get()
    if ctx is not None:  # a client call: record what was asked and which dry run it confirmed
        entries = [dict(e, params=ctx["params"], confirmation=(ctx.get("token") or "")[:12] or None)
                   for e in entries]
    path = os.path.join(folder, f"edits_{datetime.now():%Y%m%d}.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(e, ensure_ascii=False, default=str) + "\n" for e in entries))
    return path


def _log_entries() -> list[dict]:
    """Entries of this database, oldest first (files are cached until they change; an unreadable
    line, e.g. cut by a crash, is skipped)."""
    db, out = os.path.basename(DB_PATH).lower(), []
    files = [os.path.join(folder, name) for folder in _log_dirs() if os.path.isdir(folder)
             for name in sorted(os.listdir(folder)) if re.fullmatch(r"edits_\d{8}\.jsonl", name)]
    for path in files:
        st = os.stat(path)
        hit = _log_cache.get(path)
        if hit is None or hit[0] != (st.st_mtime_ns, st.st_size):
            rows = []
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict) and "ts" in e:
                        rows.append(e)
            hit = _log_cache[path] = ((st.st_mtime_ns, st.st_size), rows)
        out += [e for e in hit[1] if str(e.get("db", "")).lower() == db]
    out.sort(key=lambda e: e["ts"])  # stable: entries of one commit keep their order
    return out


def _undone_fn(entries: Sequence[dict]) -> Callable[[dict], bool]:
    """True for an entry made after a backup that a later p6_restore_backup copied back in."""
    wins = [(e.get("backup_copied_at") or e["backup_taken_at"], e["ts"]) for e in entries
            if e.get("operation") == "restore_backup" and e.get("backup_taken_at")]
    return lambda e: e.get("operation") != "restore_backup" and any(a < e["ts"] < b for a, b in wins)


def _edit_state(pid: int, lsd: Optional[str]) -> Optional[dict]:
    """Confirmed writes that made the project stale under its current last_schedule_date, from the log."""
    es = _log_entries()
    undone, key = _undone_fn(es), str(pid)
    hit = [e for e in es if key in (e.get("stale") or {}) and e["stale"][key] == lsd and not undone(e)]
    if not hit:
        return None
    last = hit[-1]
    return {"at": last["ts"][:19], "change": last.get("change"), "writes": len({e.get("commit") for e in hit}),
            "log_entries": len(hit)}


def _caller_tool() -> Optional[str]:
    """Name of the p6_* tool on the call stack (the one being run)."""
    f = sys._getframe(1)
    while f is not None:
        if f.f_code.co_name in TOOL_HINTS:
            return f.f_code.co_name
        f = f.f_back
    return None


def _guard_write() -> Optional[str]:
    if _p6_running():
        return "P6 (PM.exe) is running. Close P6 completely, then retry."
    return None


def _next_key(con: sqlite3.Connection, table: str, id_col: str) -> int:
    """Reserve the next ID from NEXTKEY (key <table>_<id_col>, case ignored), never below MAX(id)+1.
    No loose match: a suffix match could pick, and then lower, another table's counter."""
    row = con.execute(
        "SELECT key_name, key_seq_num FROM NEXTKEY WHERE lower(key_name) = lower(?)",
        (f"{table}_{id_col}",),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No NEXTKEY entry found for {table}.{id_col}; run p6_diagnose.")
    max_id = con.execute(f"SELECT COALESCE(MAX({id_col}),0) FROM {table}").fetchone()[0]
    new_id = max(int(row["key_seq_num"]), int(max_id) + 1)
    con.execute(
        "UPDATE NEXTKEY SET key_seq_num = ? WHERE key_name = ?", (new_id + 1, row["key_name"])
    )
    return new_id


def _project_id(con, project: str) -> int:
    r = con.execute(
        "SELECT proj_id FROM PROJECT WHERE proj_short_name = ?" + _alive(con, "PROJECT"),
        (project,),
    ).fetchone()
    if not r:
        raise ValueError(f"Project not found: {project}")
    return r[0]


def _task(con, project: str, task_code: str) -> sqlite3.Row:
    pid = _project_id(con, project)
    r = con.execute(
        f"SELECT t.*, {HPD_SQL} AS hpd FROM TASK t "
        "LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id "
        "WHERE t.proj_id = ? AND t.task_code = ?" + _alive(con, "TASK", "t"),
        (pid, task_code),
    ).fetchone()
    if not r:
        raise ValueError(f"Activity {task_code} not found in project {project}")
    return r


def _days(hours, hpd) -> Optional[float]:
    if hours is None:
        return None
    return round(float(hours) / float(hpd or 8), 2)


def _act_view(r: sqlite3.Row) -> dict:
    """Activity row with every ACT_FIELDS key (None when the column is missing); hours -> days
    with the activity calendar (hpd)."""
    d = _dict(r)
    hpd = d.get("hpd") or 8
    d["cstr_label"] = _cstr_label(d.get("cstr_type"))
    return {k: _days(d.get(ACT_HOURS[k]), hpd) if k in ACT_HOURS else d.get(k) for k in ACT_FIELDS}


def _act_row(fields: Optional[Sequence[str]] = None) -> Callable[[sqlite3.Row], dict]:
    """Row builder for activity lists: _act_view, or task_code plus the fields asked for (checked
    before any SQL runs)."""
    if fields is None:
        return _act_view
    fields = [fields] if isinstance(fields, str) else list(fields)
    bad = [f for f in fields if f not in ACT_FIELDS]
    if bad:
        raise ValueError(f"Unknown field(s) {bad}; allowed: {list(ACT_FIELDS)}")
    keep = list(dict.fromkeys(["task_code", *fields]))

    def row_fn(r: sqlite3.Row) -> dict:
        v = _act_view(r)
        return {k: v[k] for k in keep}
    return row_fn


def _act_from(con) -> str:
    """Base of every activity list: TASK t with hpd (CALENDAR c) and wbs_name (PROJWBS w).
    One parameter: proj_id. Add conditions with AND, then ORDER BY ending with t.task_id."""
    return (f"SELECT t.*, {HPD_SQL} AS hpd, w.wbs_name AS wbs_name FROM TASK t "
            "LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id "
            "LEFT JOIN PROJWBS w ON w.wbs_id = t.wbs_id" + _alive(con, "PROJWBS", "w")
            + " WHERE t.proj_id = ?" + _alive(con, "TASK", "t"))


def _bound(value: str, field: str, end: bool) -> str:
    """User date -> DB date string for a >= (start) or <= (end, whole day when no time) filter."""
    dt, has_time = _parse_dt(value, field)
    return _fmt_dt(dt) if has_time or not end else dt.strftime("%Y-%m-%d 23:59:59")


def _parse_dt(value: str, field: str) -> tuple[datetime, bool]:
    """User date -> (datetime, has_time). Accepts YYYY-MM-DD, YYYY-MM-DD HH:MM[:SS], 'T' allowed."""
    v = str(value).strip()
    if re.match(DATETIME_PATTERN, v):
        v = v.replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(v, fmt), fmt != "%Y-%m-%d"
            except ValueError:
                pass
    raise ValueError(f"{field}: {value!r} is not a date. Use 'YYYY-MM-DD HH:MM'.")


def _db_dt(v) -> Optional[datetime]:
    """DB or XER date value -> datetime ('T' or space, with or without seconds); None if empty."""
    if v is None or isinstance(v, datetime):
        return v
    s = str(v).strip().replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _blob_text(v) -> str:
    """TEXT or BLOB column -> str (BLOBs end with a NUL; utf-8, else cp1252)."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    b = bytes(v).rstrip(b"\x00")
    try:
        return b.decode("utf-8-sig")
    except UnicodeDecodeError:
        return b.decode("cp1252", errors="replace")


def _p6_tree(text: str, strict: bool = False) -> dict:
    """Parse P6 '(0||name(k|v|...)(children))' text (CALENDAR.clndr_data, PROJPROP values)
    into {'name', 'attrs', 'kids', 'num'}. The name may be empty. strict (before a rewrite with
    _p6_dump): a key repeated in one node is refused instead of keeping its last value."""
    s = re.sub(r"\s*[\r\n]+\s*", "", text).strip()
    pos = 0

    def eat(ch: str) -> None:
        nonlocal pos
        if s[pos] != ch:
            raise IndexError
        pos += 1

    def node() -> dict:
        nonlocal pos
        eat("(")
        start = pos
        while s[pos].isdigit():
            pos += 1
        num = s[start:pos]
        eat("|"); eat("|")
        j = s.index("(", pos)
        name, pos = s[pos:j], j + 1
        j = s.index(")", pos)
        parts = s[pos:j].split("|") if j > pos else []
        if len(parts) % 2:
            raise IndexError
        pos = j + 1
        eat("(")
        kids = []
        while s[pos] != ")":
            kids.append(node())
        pos += 1
        eat(")")
        keys = parts[::2]
        if strict and len(set(keys)) != len(keys):  # a dict keeps one: never rewrite such a node
            raise IndexError
        return {"name": name, "attrs": dict(zip(keys, parts[1::2])), "kids": kids, "num": num}

    try:
        root = node()
        if pos != len(s):
            raise IndexError
    except (IndexError, ValueError):
        raise ValueError(f"unreadable P6 data at char {pos}") from None
    return root


def _p6_dump(node: Mapping) -> str:
    """Inverse of _p6_tree (no line breaks): '(n||name(k|v|...)(kids))', keys in their order."""
    attrs = "|".join(f"{k}|{v}" for k, v in node["attrs"].items())
    return f"({node.get('num') or '0'}||{node['name']}({attrs})({''.join(_p6_dump(k) for k in node['kids'])}))"


def _p6_find(node: Optional[dict], name: str) -> Optional[dict]:
    """First node called name, depth first (the node itself included)."""
    if not node:
        return None
    if node["name"] == name:
        return node
    for k in node["kids"]:
        hit = _p6_find(k, name)
        if hit:
            return hit
    return None


def _props(con, pid: int, prop_name: str) -> Optional[dict]:
    """Parsed PROJPROP value of a project (e.g. 'scheduling', 'schedule_check'). None when the
    project has no such row; an unreadable value gives an empty tree with 'error' set."""
    r = con.execute("SELECT prop_value FROM PROJPROP WHERE proj_id = ? AND prop_name = ?"
                    + _alive(con, "PROJPROP"), (pid, prop_name)).fetchone()
    if r is None:
        return None
    try:
        return _p6_tree(_blob_text(r[0]))
    except ValueError as e:
        return {"name": None, "attrs": {}, "kids": [], "error": f"PROJPROP {prop_name}: {e}"}


def _baseline_pid(con, pid: int, baseline: Optional[str]) -> Optional[int]:
    """Project to compare with: the Project ID given (baselines included), else the project
    baseline (PROJECT.sum_base_proj_id), else None."""
    if baseline:
        r = con.execute("SELECT proj_id FROM PROJECT WHERE proj_short_name = ? "
                        "AND COALESCE(project_flag, 'Y') = 'Y'" + _alive(con, "PROJECT"), (baseline,)).fetchone()
        if not r:
            raise ValueError(f"Project or baseline not found: {baseline}")
        return r[0]
    if "sum_base_proj_id" not in _cols(con, "PROJECT"):
        return None
    r = con.execute("SELECT b.proj_id FROM PROJECT p JOIN PROJECT b ON b.proj_id = p.sum_base_proj_id "
                    "WHERE p.proj_id = ? AND b.proj_id <> p.proj_id" + _alive(con, "PROJECT", "b"),
                    (pid,)).fetchone()
    return r[0] if r else None


def _schedule_state(con, pid: int) -> dict:
    """Data date, last F9 time and whether this server changed the project since that F9, from the
    persistent edit log (entries recorded under the current PROJECT.last_schedule_date: a P6
    reschedule changes it and clears the flag); update_date is never used."""
    cols = _cols(con, "PROJECT")
    sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in ("last_recalc_date", "last_schedule_date"))
    row = con.execute(f"SELECT {sel} FROM PROJECT WHERE proj_id = ? AND COALESCE(project_flag, 'Y') = 'Y'"
                      + _alive(con, "PROJECT"), (pid,)).fetchone()
    if row is None:
        raise ValueError(f"Project id not found: {pid}")
    e = _edit_state(pid, row["last_schedule_date"])
    note = "Dates, float and paths come from the last F9 in P6."
    if e:
        note += " This server changed the schedule after that: open P6 and press F9 before trusting them."
    if not row["last_schedule_date"]:
        note += (" last_schedule_date is empty (never scheduled in a P6 version that records it,"
                 " or imported from an XER without it).")
    note += " Edits are tracked in the edit log next to the database (p6_edit_log)."
    return {"data_date": row["last_recalc_date"], "last_schedule_date": row["last_schedule_date"],
            "stale": bool(e), "edited_since_last_schedule": e, "note": note}


# ----------------------------------------------------------------- paging

def _check_page(offset: int, limit: int) -> tuple[int, int]:
    offset, limit = int(offset), int(limit)
    if offset < 0:
        raise ValueError("offset must be 0 or more")
    if limit < 1:
        raise ValueError("limit must be 1 or more")
    return offset, min(limit, MAX_ROWS)


def _env(total: Optional[int], offset: int, rows: list[dict], more: Optional[bool] = None, **extra) -> dict:
    """List envelope. next_offset is None on the last page (more= decides when total is unknown)."""
    end = offset + len(rows)
    nxt = end if (end < total if total is not None else more) else None
    return {"total": total, "offset": offset, "returned": len(rows), "next_offset": nxt, "rows": rows, **extra}


def _page(con, sql: str, args: Sequence | Mapping = (), offset: int = 0, limit: int = MAX_ROWS,
          row_fn: Callable[[sqlite3.Row], dict] = _dict) -> dict:
    """COUNT and one page of sql (which must carry its ORDER BY) in one read snapshot."""
    offset, limit = _check_page(offset, limit)
    own = not con.in_transaction
    if own:
        con.execute("BEGIN")
    try:
        total = con.execute(f"SELECT COUNT(*) FROM (\n{sql}\n)", args).fetchone()[0]
        rows = [row_fn(r) for r in con.execute(f"{sql}\nLIMIT {limit} OFFSET {offset}", args)]
    finally:
        if own:
            con.rollback()
    return _env(total, offset, rows)


def _slice(rows: list[dict], offset: int, limit: int) -> dict:
    """Same envelope for a list built in Python."""
    offset, limit = _check_page(offset, limit)
    return _env(len(rows), offset, rows[offset:offset + limit])


# ----------------------------------------------------------------- calendars

def _hm(v: Optional[str]) -> int:
    m = re.fullmatch(r"(\d{1,2}):(\d\d)", (v or "").strip())
    if not m:
        raise ValueError(f"bad shift time {v!r}")
    return int(m[1]) * 60 + int(m[2])


def _shifts_of(node: dict) -> tuple:
    """Shift children (s|HH:MM|f|HH:MM, either order) -> sorted ((start, end), ...) in minutes."""
    out = []
    for k in node["kids"]:
        s, f = _hm(k["attrs"].get("s")), _hm(k["attrs"].get("f"))
        f = 1440 if f == 0 else f  # f|00:00 is 24:00
        if f <= s:
            raise ValueError(f"shift ends before it starts: {k['attrs']}")
        out.append((s, f))
    return tuple(sorted(out))


@functools.lru_cache(maxsize=256)
def _cal_parse(data: str) -> tuple[Optional[tuple], tuple]:
    """CALENDAR.clndr_data -> (week, exceptions), immutable because cached. week: shifts per Python
    weekday (P6 day '1' is Sunday), None without a DaysOfWeek node. exceptions: ((date, shifts), ...),
    serial dates from 1899-12-30, no shifts = non-work day."""
    tree = _p6_tree(data)
    try:
        week, node = None, _p6_find(tree, "DaysOfWeek")
        if node is not None:
            w: list[tuple] = [()] * 7
            for d in node["kids"]:
                n = int(d["name"])
                if not 1 <= n <= 7:
                    raise ValueError(f"day {n}")
                w[(n + 5) % 7] = _shifts_of(d)
            week = tuple(w)
        node = _p6_find(tree, "Exceptions")
        exc = tuple((CAL_EPOCH + timedelta(days=int(float(e["attrs"]["d"]))), _shifts_of(e))
                    for e in (node["kids"] if node else ()))
    except (KeyError, TypeError, ValueError, OverflowError) as e:
        raise ValueError(f"unreadable calendar data: {e}") from None
    return week, exc


class _Cal:
    """Working time of one calendar (minutes from midnight), base calendar already merged."""
    __slots__ = ("clndr_id", "name", "hpd", "week", "exc", "inherited", "week_from_base", "error", "_cum")

    def __init__(self, clndr_id: int, name: str, hpd: float, week: Optional[tuple], exc: dict,
                 inherited: frozenset = frozenset(), week_from_base: bool = False, error: Optional[str] = None):
        self.clndr_id, self.name, self.hpd, self.week = clndr_id, name, hpd, week or ((),) * 7
        self.exc, self.inherited, self.week_from_base, self.error = exc, inherited, week_from_base, error
        self._cum: Optional[tuple] = None  # (first day ordinal, cumulative working seconds) for work_hours

    def shifts(self, d: date) -> tuple:
        return self.exc[d] if d in self.exc else self.week[d.weekday()]

    def is_workday(self, d: date) -> bool:
        return bool(self.shifts(d))

    def day_hours(self, d: date) -> float:
        return sum(f - s for s, f in self.shifts(d)) / 60

    def first_start(self, d: date) -> Optional[datetime]:
        sh = self.shifts(d)
        return datetime.combine(d, time()) + timedelta(minutes=sh[0][0]) if sh else None

    def last_end(self, d: date) -> Optional[datetime]:
        sh = self.shifts(d)  # 24:00 gives the next day at 00:00
        return datetime.combine(d, time()) + timedelta(minutes=sh[-1][1]) if sh else None

    def _day_secs(self, d: date, a: Optional[datetime] = None, b: Optional[datetime] = None) -> float:
        """Working seconds of day d between a and b (None: the day's edges)."""
        day0 = datetime.combine(d, time())
        secs = 0.0
        for s, f in self.shifts(d):
            lo, hi = day0 + timedelta(minutes=s), day0 + timedelta(minutes=f)
            lo, hi = (max(a, lo) if a else lo), (min(b, hi) if b else hi)
            if hi > lo:
                secs += (hi - lo).total_seconds()
        return secs

    def _cum_secs(self, d: date) -> float:
        """Working seconds of the days before d, from an index built on demand (CAL_WINDOW_DAYS margin)."""
        o = d.toordinal()
        c0, cum = self._cum if self._cum else (o, [])
        if not c0 <= o < c0 + len(cum):  # cum[i] = seconds of the days c0 .. c0 + i - 1
            lo, hi = min(o, c0) - CAL_WINDOW_DAYS // 10, max(o, c0 + len(cum)) + CAL_WINDOW_DAYS // 10
            cum = [0.0]
            for k in range(lo, hi + 1):
                cum.append(cum[-1] + self._day_secs(date.fromordinal(k)))
            self._cum, c0 = (lo, cum), lo
        return cum[o - c0]

    def work_hours(self, a: datetime, b: datetime) -> float:
        """Working hours between a and b (negative when b is before a)."""
        if b < a:
            return -self.work_hours(b, a)
        da, db = a.date(), b.date()
        if da == db:
            return self._day_secs(da, a, b) / 3600
        self._cum_secs(da)  # both ends in one index: a rebuild changes its origin
        mid = self._cum_secs(db) - self._cum_secs(da + timedelta(days=1))
        return (self._day_secs(da, a, None) + mid + self._day_secs(db, None, b)) / 3600

    def work_days(self, a: datetime, b: datetime) -> float:
        return self.work_hours(a, b) / self.hpd

    def count_workdays(self, d1: date, d2: date) -> int:
        """Work days from d1 to d2, both included."""
        return sum(1 for i in range((d2 - d1).days + 1) if self.is_workday(d1 + timedelta(days=i)))

    def add_work_hours(self, start: datetime, hours: float) -> datetime:
        """End of `hours` of work begun at start; lands on a shift end when it fills it (P6 finish)."""
        if hours < 0:
            raise ValueError("hours must be 0 or more")
        left = round(hours * 60)
        if not left:
            return start
        d, t0 = start.date(), (start - datetime.combine(start.date(), time())).total_seconds() / 60
        for _ in range(CAL_SCAN_DAYS):
            for s, f in self.shifts(d):
                lo = max(s, t0)
                if f > lo:
                    if left <= f - lo:
                        return datetime.combine(d, time()) + timedelta(minutes=lo + left)
                    left -= f - lo
            d, t0 = d + timedelta(days=1), 0
        raise ValueError(f"calendar {self.name!r} has no working time within {CAL_SCAN_DAYS} days")

    def subtract_work_hours(self, end: datetime, hours: float) -> datetime:
        """Start of `hours` of work that finish at end; lands on a shift start when it fills it
        (backward pass mirror of add_work_hours)."""
        if hours < 0:
            raise ValueError("hours must be 0 or more")
        left = round(hours * 60)
        if not left:
            return end
        d, t1 = end.date(), (end - datetime.combine(end.date(), time())).total_seconds() / 60
        for _ in range(CAL_SCAN_DAYS):
            for s, f in reversed(self.shifts(d)):
                hi = min(f, t1)
                if hi > s:
                    if left <= hi - s:
                        return datetime.combine(d, time()) + timedelta(minutes=hi - left)
                    left -= hi - s
            d, t1 = d - timedelta(days=1), 1440
        raise ValueError(f"calendar {self.name!r} has no working time within {CAL_SCAN_DAYS} days")

    def work_hours_between(self, a: datetime, b: datetime) -> float:
        """Working hours from a to b (negative when b is before a); same as work_hours."""
        return self.work_hours(a, b)

    def is_work_time(self, dt: datetime) -> bool:
        """dt falls inside a shift: [start, finish), so a shift finish is not work time."""
        m = (dt - datetime.combine(dt.date(), time())).total_seconds() / 60
        return any(s <= m < f for s, f in self.shifts(dt.date()))

    def next_work_start(self, dt: datetime) -> datetime:
        """dt when it is work time, else the next shift start after it."""
        d, m = dt.date(), (dt - datetime.combine(dt.date(), time())).total_seconds() / 60
        for _ in range(CAL_SCAN_DAYS):
            for s, f in self.shifts(d):
                if m < f:
                    return dt if s <= m else datetime.combine(d, time()) + timedelta(minutes=s)
            d, m = d + timedelta(days=1), -1
        raise ValueError(f"calendar {self.name!r} has no working time within {CAL_SCAN_DAYS} days")

    def prev_work_end(self, dt: datetime) -> datetime:
        """dt when work runs just before it (inside a shift, or at its finish), else the last shift
        finish before it: a shift start gives the previous finish (backward mirror of next_work_start)."""
        d, m = dt.date(), (dt - datetime.combine(dt.date(), time())).total_seconds() / 60
        for _ in range(CAL_SCAN_DAYS):
            for s, f in reversed(self.shifts(d)):
                if m > s:
                    return dt if m <= f else datetime.combine(d, time()) + timedelta(minutes=f)
            d, m = d - timedelta(days=1), 1441
        raise ValueError(f"calendar {self.name!r} has no working time within {CAL_SCAN_DAYS} days")


def _calendars(con, only: Optional[int] = None) -> dict[int, _Cal]:
    """Live calendars by clndr_id (only= one calendar and its bases). A base calendar gives its week
    when the calendar has no DaysOfWeek node, and its exceptions (the calendar's own dates win).
    An unreadable clndr_data gives a calendar without working time (nothing taken from its base)
    and .error set."""
    sql = ("SELECT clndr_id, clndr_name, base_clndr_id, day_hr_cnt, clndr_data FROM CALENDAR WHERE "
           + ("clndr_id = ?" if only is not None else "1=1") + _alive(con, "CALENDAR"))
    if only is None:
        rows = {r["clndr_id"]: r for r in con.execute(sql)}
    else:
        rows, cid = {}, only
        while cid is not None and cid not in rows and len(rows) <= 10:
            r = con.execute(sql, (cid,)).fetchone()
            if r is None:
                break
            rows[cid], cid = r, r["base_clndr_id"]
    out: dict[int, _Cal] = {}

    def build(cid: int, depth: int = 0) -> _Cal:
        if cid in out:
            return out[cid]
        r = rows[cid]
        try:
            (week, exc), err = _cal_parse(_blob_text(r["clndr_data"])), None
        except ValueError as e:
            week, exc, err = None, (), str(e)
        own, inherited, from_base = dict(exc), frozenset(), False
        if err is None and r["base_clndr_id"] in rows and r["base_clndr_id"] != cid and depth < 10:
            b = build(r["base_clndr_id"], depth + 1)
            if week is None:
                week, from_base = b.week, True
            inherited = frozenset(set(b.exc) - set(own))
            own = {**b.exc, **own}
        out[cid] = _Cal(cid, r["clndr_name"], float(r["day_hr_cnt"] or 8), week, own, inherited, from_base, err)
        return out[cid]

    for cid in rows:
        build(cid)
    return out


def _calendar(con, clndr_id: int) -> _Cal:
    cal = _calendars(con, int(clndr_id)).get(int(clndr_id))
    if cal is None:
        raise ValueError(f"Calendar not found: {clndr_id}")
    return cal


def _resolve_dt(cal: Optional[_Cal], value: str, field: str, edge: str) -> tuple[str, list[str]]:
    """User date of an activity -> (DB date string, notes). An explicit time is kept (noted when the
    day is not a work day of cal). A date alone takes the first shift start (edge 'start') or the
    last shift end (edge 'finish', 24:00 = next day 00:00) of that day, and is refused on a non-work
    day. Without a readable calendar a date alone starts at 08:00 and a finish needs a time."""
    dt, has_time = _parse_dt(value, field)
    d = dt.date()
    if has_time:
        if cal is not None and not cal.is_workday(d):
            return _fmt_dt(dt), [f"{field} {_fmt_dt(dt)} is on a non-work day of calendar {cal.name!r} (kept)"]
        return _fmt_dt(dt), []
    if cal is None:
        if edge == "finish":
            raise ValueError(f"{field}: give the time ('YYYY-MM-DD HH:MM'): the activity has no readable calendar")
        return _fmt_dt(dt.replace(hour=8)), [f"{field}: date only and no readable calendar, 08:00 used"]
    if not cal.is_workday(d):
        raise ValueError(f"{field}: {d} is not a work day on calendar {cal.name!r}: give the time ('YYYY-MM-DD HH:MM')")
    v = cal.first_start(d) if edge == "start" else cal.last_end(d)
    return _fmt_dt(v), [f"{field}: date only, {'first shift start' if edge == 'start' else 'last shift end'} "
                        f"of calendar {cal.name!r} used ({_fmt_dt(v)})"]


def _shift_txt(shifts: tuple) -> list[str]:
    return [f"{s // 60:02d}:{s % 60:02d}-{f // 60:02d}:{f % 60:02d}" for s, f in shifts]


def _cal_row(r: sqlite3.Row, cal: _Cal, win: Optional[tuple], span: Optional[tuple], exc_cap: Optional[int]) -> dict:
    """p6_list_calendars row: stored values, the parsed week, exceptions and warnings; exc_cap = how
    many exception dates to list (None: no list)."""
    hpd = float(r["day_hr_cnt"] or 8)
    hours = [sum(f - s for s, f in sh) / 60 for sh in cal.week]
    week_h = sum(hours)
    warn = [] if r["day_hr_cnt"] else ["day_hr_cnt is empty, 8 h per day used"]
    if r["base_clndr_id"] is not None and r["base_name"] is None:
        warn.append(f"base calendar {r['base_clndr_id']} not found")
    if not week_h:
        warn.append("no working time in the work week")
    if r["week_hr_cnt"] is not None and abs(float(r["week_hr_cnt"]) - week_h) > 1e-6:
        warn.append(f"stored week_hr_cnt {float(r['week_hr_cnt']):g} differs from the work week {week_h:g} h")
    warn += [f"a work day has {h:g} h, hours_per_day is {hpd:g}" for h in sorted({h for h in hours if h})
             if abs(h - hpd) > 1e-6]
    non_work = sum(1 for v in cal.exc.values() if not v)
    row = {"clndr_id": r["clndr_id"], "name": r["clndr_name"], "type": r["clndr_type"],
           "project": r["cal_project"], "base_calendar": r["base_name"],
           "project_default": None if r["project_default"] is None else bool(r["project_default"]),
           "activities": r["activities"], "hours_per_day": hpd, "week_hours_stored": r["week_hr_cnt"],
           "work_week": {WEEKDAYS[(n + 5) % 7]: _shift_txt(cal.week[(n + 5) % 7]) for n in range(1, 8)},
           "week_hours": week_h, "week_inherited": cal.week_from_base,
           "exceptions": {"total": len(cal.exc), "inherited": len(cal.inherited), "non_work": non_work,
                          "changed_hours": len(cal.exc) - non_work}}
    if win:
        wh = cal.work_hours(datetime.combine(win[0], time()), datetime.combine(win[1] + timedelta(days=1), time()))
        row["window"] = {"from": win[0].isoformat(), "to": win[1].isoformat(), "work_hours": round(wh, 4),
                         "work_days": round(wh / hpd, 2), "workdays": cal.count_workdays(*win)}
    if exc_cap is not None:
        lo, hi = win or span or (None, None)
        dates = sorted(d for d in cal.exc if (lo is None or d >= lo) and (hi is None or d <= hi))
        row["exception_dates"] = [{"date": d.isoformat(), "work": _shift_txt(cal.exc[d]),
                                   "inherited": d in cal.inherited} for d in dates[:exc_cap]]
        row["exception_dates_truncated"] = len(dates) > exc_cap
    if cal.error:
        row["error"] = cal.error
    row["warnings"] = warn
    return row


# ----------------------------------------------------------------- schedule options and lag rule

def _sched_options(con, pid: int) -> tuple[dict[str, str], str]:
    """(options, source) from the project's PROJPROP 'scheduling' row, keys lower case. The level
    priority list is flattened for display only."""
    tree = _props(con, pid, "scheduling")
    if tree is None:
        return {}, "none stored for this project"
    if tree.get("error"):
        return {}, f"unreadable: {tree['error']}"
    opts = {k.lower(): v for k, v in tree["attrs"].items()}
    lpl = _p6_find(tree, "LevelPriorityList")
    if lpl is not None:
        opts["levelprioritylist"] = _lpl_text(_lpl_of(lpl))
    return opts, "PROJPROP scheduling"


def _lag_rule(con, pid: int) -> tuple[str, str]:
    """(rule, source): the project's sched_calendar_on_relationship_lag, else rcal_Predecessor."""
    opts, src = _sched_options(con, pid)
    v = opts.get("sched_calendar_on_relationship_lag")
    if v in LAG_RULES:
        return v, "project schedule options (PROJPROP scheduling)"
    if v is None:
        return "rcal_Predecessor", f"P6 default ({src})"
    return "rcal_Predecessor", f"P6 default (unrecognised value {v!r})"


def _lag_info(con, pid: int) -> dict:
    rule, src = _lag_rule(con, pid)
    return {"rule": rule, "source": src, "basis": LAG_RULES[rule]}


def _lag_hpd(con, pred, succ, proj_id: Optional[int] = None, rule: Optional[str] = None) -> tuple[float, str]:
    """(hours per lag day, basis) under the lag rule of proj_id (default: the successor's project,
    which also decides cross-project links). pred and succ carry 'hpd'; rule= overrides the lookup."""
    pid = succ["proj_id"] if proj_id is None else proj_id
    rule = rule or _lag_rule(con, pid)[0]
    if rule == "rcal_Successor":
        h = succ["hpd"]
    elif rule == "rcal_24Hour":
        h = 24
    elif rule == "rcal_ProjDefault":
        r = con.execute(f"SELECT {HPD_SQL} FROM PROJECT p LEFT JOIN CALENDAR c ON c.clndr_id = p.clndr_id "
                        "WHERE p.proj_id = ?" + _alive(con, "PROJECT", "p"), (pid,)).fetchone()
        h = r[0] if r else None
    else:
        rule, h = "rcal_Predecessor", pred["hpd"]
    return float(h or 8), LAG_RULES[rule]


def _lag_cal_of(rule: str, p_cal, s_cal, proj_cal) -> Optional[_Cal]:
    """Calendar a lag is counted on under the lag rule (None: 24-hour days; default: the predecessor's)."""
    return {"rcal_Successor": s_cal, "rcal_24Hour": None, "rcal_ProjDefault": proj_cal}.get(rule, p_cal)


def _lag_hpd_of(rule: str, p_hpd, s_hpd, proj_cal) -> float:
    """Hours per lag day under the lag rule, from loaded calendars (_lag_hpd reads the database)."""
    return {"rcal_Successor": s_hpd, "rcal_24Hour": 24.0,
            "rcal_ProjDefault": proj_cal.hpd if proj_cal else 8.0}.get(rule, p_hpd) or 8.0


def _lag_left(lag: float, ref: Optional[datetime], dd: Optional[datetime], lc: Optional[_Cal]) -> float:
    """A positive lag minus the part elapsed between the predecessor's actual date ref and the data
    date dd (on the lag calendar lc, else in elapsed hours), as F9 does when the lag counts from the
    actual dates (sched_lag_early_start_flag not 'N')."""
    if lag <= 0 or dd is None or ref is None or ref >= dd:
        return lag
    return max(0.0, lag - (lc.work_hours(ref, dd) if lc is not None and not lc.error
                           else (dd - ref).total_seconds() / 3600))


def _lag_hpd_sql(con, pid: int) -> tuple[str, str]:
    """(SQL hours per lag day, basis) under the lag rule of pid, for links with the predecessor's
    calendar as cp and the successor's as cs."""
    rule = _lag_rule(con, pid)[0]
    if rule in ("rcal_Predecessor", "rcal_Successor"):
        return f"COALESCE(NULLIF({'cp' if rule == 'rcal_Predecessor' else 'cs'}.day_hr_cnt, 0), 8)", LAG_RULES[rule]
    h, basis = _lag_hpd(con, {"hpd": None}, {"hpd": None}, pid, rule)
    return repr(h), basis


def _xer_project(x: dict, xer_project: Optional[str]) -> dict:
    projects = [p for p in x.get("PROJECT", []) if xer_project in (None, p["proj_short_name"])]
    if len(projects) != 1:
        raise ValueError("Choose the project with xer_project=: "
                         f"{[p['proj_short_name'] for p in x.get('PROJECT', [])]}")
    return projects[0]


def _xer_sched(x: dict, xp: dict) -> dict[str, Optional[str]]:
    """SCHEDOPTIONS of XER project xp (or the only row, when it has no proj_id) without its ids; {}
    when absent. Another project's row is never used."""
    out = {k: v for k, v in _xer_sched_row(x, xp).items() if k not in ("schedoptions_id", "proj_id")}
    if out.get("levelprioritylist"):
        lpl = _xer_lpl(out["levelprioritylist"])
        out["levelprioritylist"] = out["levelprioritylist"].strip() if lpl is None else _lpl_text(lpl)
    return out


def _xer_sched_row(x: dict, xp: dict) -> dict:
    """The SCHEDOPTIONS row of XER project xp, or the only row when it has no proj_id; {} otherwise."""
    rows = x.get("SCHEDOPTIONS", [])
    return next((r for r in rows if r.get("proj_id") == xp["proj_id"]),
                rows[0] if len(rows) == 1 and rows[0].get("proj_id") is None else None) or {}


def _opt_eq(a, b) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def _sched_diff(db_opts: dict, xer_opts: dict, db_code: Callable[[str], Optional[str]],
                xer_code: Callable[[str], Optional[str]]) -> list[dict]:
    """Options side by side: different first, then only in the XER, only in the database, equal, and
    the level priority list last ('field:ORDER' on both sides, compared when both are readable).
    Numbers compare as floats; the key activity id is shown as an Activity ID ('-99' = none; an id
    the XER does not contain resolves to none, as P6 cannot use it either)."""
    def label(v, code, where):
        if v is None or str(v).strip() in ("", "-99"):
            return None if v is None else "(none)"
        return code(str(v).strip()) or f"{v} (not in this {where}: none)"

    def none_like(v):
        return v is not None and (v == "(none)" or v.endswith(": none)"))

    out = []
    for k in sorted(set(db_opts) | set(xer_opts)):
        d, x = db_opts.get(k), xer_opts.get(k)
        if k == "key_activity_for_multiple_longest_paths":  # present but empty (an XER cell) = none
            d = label("" if d is None and k in db_opts else d, db_code, "database")
            x = label("" if x is None and k in xer_opts else x, xer_code, "XER")
        if k == "levelprioritylist":
            readable = all(v is not None and _xer_lpl(v, ":") is not None for v in (d, x))
            rank, same = 4, (d == x if readable else None)
        elif k == "key_activity_for_multiple_longest_paths" and d is not None and x is not None:
            same = d == x or (none_like(d) and none_like(x))
            rank = 3 if same else 0
        elif k not in db_opts or k not in xer_opts:
            rank, same = (1 if k in xer_opts else 2), None
        else:
            same = _opt_eq(d, x)
            rank = 3 if same else 0
        out.append((rank, {"option": k, "db": d, "xer": x, "same": same}))
    return [o for _, o in sorted(out, key=lambda t: t[0])]


def _db_task_code(con, task_id: str) -> Optional[str]:
    try:
        r = con.execute("SELECT task_code FROM TASK WHERE task_id = ?" + _alive(con, "TASK"), (int(task_id),)).fetchone()
    except ValueError:
        return None
    return r[0] if r else None


# PROJPROP option rows written by p6_set_schedule_options and p6_import_xer: key -> (kind, P6 default),
# in P6's key order. Kinds: flag (Y/N), int, float (written as P6 does: 25.0), num (int when whole),
# task (key activity: a task_id, -99 = none) or a tuple of allowed values. Keys P6 stores that are not
# listed here are kept as they are. Defaults (a project without the row) follow the P6 template rows.
SCHED_SPEC: dict[str, tuple[Any, str]] = {
    "enable_multiple_longest_path_calc": ("flag", "N"),
    "key_activity_for_multiple_longest_paths": ("task", "-99"),
    "max_multiple_longest_path": ("int", "10"),
    "sched_float_type": (("FT_FF", "FT_SF", "FT_SM"), "FT_FF"),
    "level_outer_assign_priority": ("int", "5"),
    "use_total_float_multiple_longest_paths": ("flag", "Y"),
    "sched_calendar_on_relationship_lag": (tuple(LAG_RULES), "rcal_Predecessor"),
    "limit_multiple_longest_path_calc": ("flag", "Y"),
    "sched_open_critical_flag": ("flag", "N"),
    "sched_lag_early_start_flag": ("flag", "Y"),
    "sched_progress_override": ("flag", "N"),
    "sched_retained_logic": ("flag", "Y"),
    "level_over_alloc_pct": ("float", "25.0"),
    "sched_outer_depend_type": (("SD_Both", "SD_None"), "SD_Both"),
    "sched_use_expect_end_flag": ("flag", "Y"),
    "level_within_float_flag": ("flag", "N"),
    "level_all_rsrc_flag": ("flag", "Y"),
    "level_keep_sched_date_flag": ("flag", "Y"),
    "level_float_thrs_cnt": ("num", "0"),
    "level_outer_assign_flag": ("flag", "N"),
    "sched_use_project_end_date_for_float": ("flag", "Y"),
    "sched_setplantoforecast": ("flag", "N"),
}
# PROJPROP 'schedule_check' (Check Schedule thresholds read by p6_health_check, see DCMA_CHECKS): targets
# in %, *_constraint limits in hours, bei_target a ratio, check_* switches.
CHECK_SPEC: dict[str, tuple[Any, str]] = {k: ("flag" if k.startswith("check_") else "float" if k == "bei_target"
                                             else "num", v) for k, v in (
    ("check_cpli", "Y"), ("soft_constraint_target", "5"), ("logic_target", "5"), ("long_lags_target", "5"),
    ("progress_date_target", "1"), ("check_links_to_closed_projects", "Y"), ("check_large_duration", "Y"),
    ("check_soft_constraints", "Y"), ("large_float_target", "1"), ("long_lags_constraint", "352"),
    ("large_float_constraint", "352"), ("late_activities_target", "5"), ("bei_target", "0.95"),
    ("large_duration_target", "5"), ("check_hard_constraints", "Y"), ("resouces_target", "1"),
    ("check_negative_float", "Y"), ("check_lag", "Y"), ("cpt_constraint", "1"), ("cpt_target", "5"),
    ("check_resources", "Y"), ("check_logic", "Y"), ("check_bei", "Y"), ("relationship_target", "90"),
    ("check_negative_lag", "Y"), ("check_long_lags", "Y"), ("check_large_float", "Y"),
    ("check_progress_duration", "Y"), ("negative_float_target", "1"), ("hard_constraint_target", "1"),
    ("cpli_target", "5"), ("check_cpt", "Y"), ("check_late_activities", "Y"), ("check_relationships", "Y"),
    ("negative_lag_target", "1"), ("lag_target", "5"), ("large_durations_constraint", "352"))}
PROP_SPEC = {"scheduling": SCHED_SPEC, "schedule_check": CHECK_SPEC}
LPL_ORDERS = ("ASC", "DESC")


def _lpl_of(node: Mapping) -> list[tuple[str, str]]:
    """LevelPriorityList node -> [(field, order)]: each child is '(0||field(sort_type|ASC)())' (older
    P6 rows write '(ASC|ASC)': the order is the last value)."""
    return [(k["name"], (list(k["attrs"].values()) or ["ASC"])[-1]) for k in node["kids"]]


def _lpl_text(lpl: Sequence[tuple[str, str]]) -> str:
    return ";".join(f"{f}:{o}" for f, o in lpl)


def _xer_lpl(text: str, sep: str = ",") -> Optional[list[tuple[str, str]]]:
    """Level priority list text -> [(field, order)], None when unreadable. XER SCHEDOPTIONS writes
    'priority_type,ASC_BY_FIELD/ASC' followed by a line break per entry (field, then the order after
    the last '/'); this server shows and takes 'field:ORDER', entries separated by ';' (line breaks
    are read as separators too)."""
    out = []
    for part in (p.strip() for p in re.split(r"[;\r\n]+", str(text))):
        if not part:
            continue
        field, s, rest = part.partition(sep)
        order = (rest.rsplit("/", 1)[-1] if s else "ASC").strip().upper()
        if not re.fullmatch(r"\w+", field.strip()) or order not in LPL_ORDERS:
            return None
        out.append((field.strip(), order))
    return out or None


def _norm_opt(prop: str, key: str, v: Any, lenient: bool = False) -> tuple[str, Optional[str]]:
    """(text as P6 stores it, warning) for one option value; ValueError when invalid. lenient (values
    P6 wrote in an XER): an allowed-value token this server does not know is written with a warning."""
    kind = PROP_SPEC[prop].get(key, ("text",))[0]
    s = ("Y" if v else "N") if isinstance(v, bool) else "" if v is None else str(v).strip()
    if kind == "flag":
        if s.upper() not in ("Y", "N"):
            raise ValueError(f"{key}: Y or N (or true/false), not {v!r}")
        return s.upper(), None
    if kind in ("int", "num", "float"):
        try:
            f = float(s)
        except ValueError:
            raise ValueError(f"{key}: a number, not {v!r}") from None
        if not math.isfinite(f) or f < 0:
            raise ValueError(f"{key}: a number 0 or more, not {v!r}")
        if kind == "int" and not f.is_integer():
            raise ValueError(f"{key}: a whole number, not {v!r}")
        return (repr(f) if kind == "float" or not f.is_integer() else str(int(f))), None
    if isinstance(kind, tuple):
        hit = next((k for k in kind if k.lower() == s.lower()), None)
        if hit:
            return hit, None
        if lenient and re.fullmatch(r"\w+", s):
            return s, f"{key}: XER value {s!r} is not one this server knows ({', '.join(kind)}); written as is"
        raise ValueError(f"{key}: one of {list(kind)}, not {v!r}")
    if re.search(r"[|()\r\n]", s):
        raise ValueError(f"{key}: P6 option values cannot contain | ( ) or line breaks: {v!r}")
    return s, None


def _key_task(con, pid: int, code: Any) -> str:
    """Key activity for multiple float paths: Activity ID of project pid -> task_id text ('-99' = none)."""
    c = "" if code is None else str(code).strip()
    if c.lower() in ("", "-99", "none", "(none)"):
        return "-99"
    tid = _task_id_by_code(con, pid, c)
    if tid is None:
        raise ValueError(f"key activity: Activity ID {c!r} not found in the project")
    return str(tid)


def _xer_sched_values(x: dict, xp: dict, key_map: Callable[[str], Optional[int]]
                      ) -> tuple[dict[str, str], Optional[list[tuple[str, str]]], list[str]]:
    """XER SCHEDOPTIONS of project xp -> (PROJPROP 'scheduling' values, level priority list, warnings).
    key_map turns the XER key activity task_id into the database task_id (None when it has no match:
    -99 is written, with a warning). Option names are kept as the XER spells them (lower case)."""
    row = _xer_sched_row(x, xp)
    vals, warn, lpl = {}, [], None
    for k, v in row.items():
        if k in ("schedoptions_id", "proj_id"):
            continue
        if k == "levelprioritylist":
            lpl = _xer_lpl(v) if v else None
            if v and lpl is None:
                warn.append(f"XER level priority list {v!r} not readable: the stored list is kept")
            continue
        if k == "key_activity_for_multiple_longest_paths":
            raw = "" if v is None else str(v).strip()
            tid = None if raw in ("", "-99") else key_map(raw)
            if raw not in ("", "-99") and tid is None:
                warn.append(f"key_activity_for_multiple_longest_paths: XER task_id {raw} has no activity in the "
                            "project (not in the XER, or no Activity ID match): -99 (none) written")
            vals[k] = "-99" if tid is None else str(tid)
            continue
        if v is None:
            continue
        vals[k], w = _norm_opt("scheduling", k, v, lenient=True)
        if w:
            warn.append(w)
    return vals, lpl, warn


# SCHEDOPTIONS block written by p6_export_xer: the column layout of P6's own XER exports.
SCHED_XER_COLS = ("schedoptions_id", "proj_id", "sched_outer_depend_type", "sched_open_critical_flag",
                  "sched_lag_early_start_flag", "sched_retained_logic", "sched_setplantoforecast", "sched_float_type",
                  "sched_calendar_on_relationship_lag", "sched_use_expect_end_flag", "sched_progress_override",
                  "level_float_thrs_cnt", "level_outer_assign_flag", "level_outer_assign_priority",
                  "level_over_alloc_pct", "level_within_float_flag", "level_keep_sched_date_flag",
                  "level_all_rsrc_flag", "sched_use_project_end_date_for_float", "enable_multiple_longest_path_calc",
                  "limit_multiple_longest_path_calc", "max_multiple_longest_path",
                  "use_total_float_multiple_longest_paths", "key_activity_for_multiple_longest_paths",
                  "LevelPriorityList")


def _xer_sched_export(con, pid: int, tasks: set) -> tuple[Optional[list], list[str]]:
    """(SCHEDOPTIONS values in SCHED_XER_COLS order, warnings) from the project's PROJPROP 'scheduling'
    (the reverse of _xer_sched_values); None when the project has none or it is unreadable. The key
    activity is the exported task_id (empty = none, also for an activity outside the project); the
    level priority list is written as P6 does, 'field,ASC_BY_FIELD/ORDER' plus a line break per entry."""
    tree = _props(con, pid, "scheduling")
    if tree is None:
        return None, ["SCHEDOPTIONS not exported: the project has no schedule options stored (PROJPROP "
                      "scheduling); P6 applies its defaults on import."]
    if tree.get("error"):
        return None, [f"SCHEDOPTIONS not exported: {tree['error']}"]
    opts = {k.lower(): v for k, v in tree["attrs"].items()}
    warn = []
    key = str(opts.get("key_activity_for_multiple_longest_paths") or "").strip()
    known = key.isdigit() and int(key) in tasks
    if key not in ("", "-99") and not known:
        warn.append(f"SCHEDOPTIONS: key activity task_id {key} is not an activity of the project: written as none")
    opts["key_activity_for_multiple_longest_paths"] = key if known else ""
    node = _p6_find(tree, "LevelPriorityList")
    opts["levelprioritylist"] = "".join(f"{f},ASC_BY_FIELD/{o}\n" for f, o in _lpl_of(node)) if node else None
    extra = sorted(set(opts) - {c.lower() for c in SCHED_XER_COLS})
    if extra:
        warn.append(f"SCHEDOPTIONS: stored options with no XER column, not exported: {extra}")
    opts["schedoptions_id"], opts["proj_id"] = 1, pid
    return [opts.get(c.lower()) for c in SCHED_XER_COLS], warn


def _set_props(con, pid: int, prop: str, values: Mapping[str, str], lpl: Optional[Sequence[tuple[str, str]]],
               now: str) -> dict:
    """Write PROJPROP (pid, prop): the stored tree (or P6's defaults when the project has no row) with
    values set and, for 'scheduling', the level priority list replaced when lpl is given. Keys keep
    their order, new ones follow in P6's order, unknown keys and other nodes are kept; the text form
    (TEXT or BLOB) of the stored row is kept, a new row is TEXT as P6 writes it. Nothing is written
    when the text is unchanged. Returns {action, options: [{option, old, new, changed}]}."""
    spec = PROP_SPEC[prop]
    row = con.execute("SELECT * FROM PROJPROP WHERE proj_id = ? AND prop_name = ?", (pid, prop)).fetchone()
    live = row is not None and not _dict(row).get("delete_session_id")
    raw = row["prop_value"] if live else None
    if live:
        try:
            tree = _p6_tree(_blob_text(raw), strict=True)
        except ValueError as e:
            raise ValueError(f"PROJPROP {prop} of this project is not readable ({e}): fix it in P6 "
                             "(Schedule Options) before writing it here") from None
    else:
        tree = {"num": "0", "name": "", "attrs": {k: d for k, (_, d) in spec.items()}, "kids": []}
        if prop == "scheduling":
            tree["kids"] = [{"num": "0", "name": "LevelResourceList", "attrs": {}, "kids": []},
                            {"num": "0", "name": "LevelPriorityList", "attrs": {}, "kids": [
                                {"num": "0", "name": "priority_type", "attrs": {"sort_type": "ASC"}, "kids": []}]}]
    old = dict(tree["attrs"])
    order = [k for k in spec if k in values] + [k for k in values if k not in spec]
    for k in order:
        tree["attrs"][k] = values[k]
    rows = [{"option": k, "old": old.get(k), "new": tree["attrs"][k], "changed": old.get(k) != tree["attrs"][k]}
            for k in tree["attrs"]]
    if prop == "scheduling":
        node = _p6_find(tree, "LevelPriorityList")
        before = _lpl_text(_lpl_of(node)) if node else None
        if lpl is not None:
            if node is None:
                node = {"num": "0", "name": "LevelPriorityList", "attrs": {}, "kids": []}
                tree["kids"].append(node)
            node["kids"] = [{"num": "0", "name": f, "attrs": {"sort_type": o}, "kids": []} for f, o in lpl]
        after = _lpl_text(_lpl_of(node)) if node else None
        rows.append({"option": "levelprioritylist", "old": before, "new": after, "changed": before != after})
    text = _p6_dump(tree)
    if live and _blob_text(raw) == text:
        return {"action": "unchanged", "options": rows}
    value = text
    if isinstance(raw, (bytes, bytearray)):  # keep a BLOB row a BLOB, with its trailing NUL if it had one
        value = text.encode("utf-8") + (b"\x00" if bytes(raw).endswith(b"\x00") else b"")
    cols = _cols(con, "PROJPROP")
    if row is None:
        _insert(con, "PROJPROP", {"proj_id": pid, "prop_name": prop, "prop_value": value,
                                  **_stamp_cols(con, "PROJPROP", now, create=True)})
        action = "inserted"
    else:
        upd = {"prop_value": value, **_stamp_cols(con, "PROJPROP", now, create=False)}
        upd.update({c: None for c in ("delete_session_id", "delete_date") if c in cols})
        _update(con, "PROJPROP", {"proj_id": pid, "prop_name": prop}, upd)
        action = "updated" if live else "restored"
    return {"action": action, "options": rows}


# ----------------------------------------------------------------- WBS tree and activity aggregates

def _name_sep(con, pid: int) -> str:
    """Code separator: PROJECT.name_sep_char, else PREFER.name_sep_char, else '.'."""
    for table, sql, args in (("PROJECT", "SELECT name_sep_char FROM PROJECT WHERE proj_id = ?", (pid,)),
                             ("PREFER", "SELECT name_sep_char FROM PREFER", ())):
        if "name_sep_char" in _cols(con, table):
            r = con.execute(sql, args).fetchone()
            if r and r[0]:
                return r[0]
    return "."


def _wbs_tree(con, pid: int, sep: Optional[str] = None) -> list[dict]:
    """WBS nodes reachable from the project node (proj_node_flag 'Y'), in P6 order: pre-order,
    siblings by seq_num then wbs_id. Keys: wbs_id, parent_wbs_id, depth (project node 0), wbs_code
    (short names joined with sep, default _name_sep), wbs_short_name, wbs_name."""
    seg = "printf('%010d.%010d', COALESCE({0}.seq_num, 0) + 1000000000, {0}.wbs_id)"  # fixed width: sorts as text
    sql = (f"WITH RECURSIVE pw AS {MAT_SQL}(SELECT wbs_id, parent_wbs_id, seq_num, proj_node_flag, wbs_short_name, "
           "wbs_name FROM PROJWBS WHERE proj_id = :pid" + _alive(con, "PROJWBS") + "), "
           "tree(wbs_id, depth, sort_key, wbs_code) AS ("
           f"SELECT wbs_id, 0, {seg.format('pw')}, wbs_short_name FROM pw WHERE proj_node_flag = 'Y' "
           f"UNION ALL SELECT c.wbs_id, t.depth + 1, t.sort_key || '/' || {seg.format('c')}, "
           "t.wbs_code || :sep || c.wbs_short_name FROM pw c JOIN tree t ON c.parent_wbs_id = t.wbs_id "
           "WHERE t.depth < 100) "
           "SELECT t.wbs_id AS wbs_id, p.parent_wbs_id AS parent_wbs_id, t.depth AS depth, t.wbs_code AS wbs_code, "
           "p.wbs_short_name AS wbs_short_name, p.wbs_name AS wbs_name FROM tree t JOIN pw p ON p.wbs_id = t.wbs_id "
           "ORDER BY t.sort_key")
    return [_dict(r) for r in con.execute(sql, {"pid": pid, "sep": sep or _name_sep(con, pid)})]


AGG_MIN, AGG_MAX = ("tf_min", "start"), ("tf_max", "finish")  # the other _act_agg_sql keys add up


def _act_agg_sql(con) -> str:
    """SELECT list of additive aggregates over TASK t (CALENDAR c for days): counts by status, open
    (not completed, SUMMARY_TYPES left out, as the analysis tools), total float of the open
    activities in days (min, max, sum, n), negative float, durations in days, start, finish,
    budget, actual and remaining units (labor plus nonlabor). Partial results combine with
    _merge_agg."""
    tf, cols = f"t.total_float_hr_cnt / {HPD_SQL}", _cols(con, "TASK")
    op = f"t.status_code <> 'TK_Complete' AND t.task_type NOT IN {SUMMARY_TYPES}"

    def qty(kind: str) -> str:
        ks = [f"COALESCE(t.{kind}_{u}_qty, 0)" for u in ("work", "equip") if f"{kind}_{u}_qty" in cols]
        return f"SUM({' + '.join(ks)})" if ks else "NULL"
    return (f"COUNT(*) AS n, SUM(t.status_code = 'TK_NotStart') AS not_started, "
            "SUM(t.status_code = 'TK_Active') AS in_progress, SUM(t.status_code = 'TK_Complete') AS completed, "
            f"SUM({op}) AS open, SUM({op} AND t.total_float_hr_cnt < 0) AS negative, "
            f"SUM({op} AND t.total_float_hr_cnt IS NOT NULL) AS tf_n, SUM(CASE WHEN {op} THEN {tf} END) AS tf_sum, "
            f"MIN(CASE WHEN {op} THEN {tf} END) AS tf_min, MAX(CASE WHEN {op} THEN {tf} END) AS tf_max, "
            f"SUM(t.target_drtn_hr_cnt / {HPD_SQL}) AS orig_d, SUM(t.remain_drtn_hr_cnt / {HPD_SQL}) AS rem_d, "
            "MIN(COALESCE(t.act_start_date, t.early_start_date)) AS start, "
            "MAX(COALESCE(t.act_end_date, t.early_end_date)) AS finish, "
            f"{qty('target')} AS budget, {qty('act')} AS actual, {qty('remain')} AS remain")


def _merge_agg(acc: Mapping, part: Mapping) -> dict:
    """Two _act_agg_sql results combined: min/max keys keep the extreme, the others add; None ignored."""
    out = dict(acc)
    for k, v in part.items():
        a = out.get(k)
        if v is None or a is None:
            out[k] = a if v is None else v
        else:
            out[k] = min(a, v) if k in AGG_MIN else max(a, v) if k in AGG_MAX else a + v
    return out


def _rollup(a: Mapping) -> dict:
    """WBS rollup fields from merged aggregates (an empty subtree gives zeros and None). units_pct
    is P6's Units % Complete: actual / at completion (actual + remaining)."""
    n = lambda k: a.get(k) or 0  # noqa: E731
    actual = float(n("actual"))
    at_completion = actual + float(n("remain"))
    return {"activities": n("n"), "open": n("open"), "not_started": n("not_started"),
            "in_progress": n("in_progress"), "completed": n("completed"),
            "min_tf_d": None if a.get("tf_min") is None else round(a["tf_min"], 2),
            "negative_float": n("negative"), "start": a.get("start"), "finish": a.get("finish"),
            "budget_units": round(float(n("budget")), 2), "actual_units": round(actual, 2),
            "units_pct": round(100 * actual / at_completion, 2) if at_completion else None}


# ----------------------------------------------------------------- activity codes

def _eps_chain(con, pid: int) -> list[int]:
    """proj_ids of the EPS nodes above a project, nearest first (depth cap 50)."""
    r = con.execute("SELECT parent_wbs_id FROM PROJWBS WHERE proj_id = ? AND proj_node_flag = 'Y'"
                    + _alive(con, "PROJWBS"), (pid,)).fetchone()
    out, wid = [], r and r[0]
    while wid is not None and len(out) < 50:
        r = con.execute("SELECT proj_id, parent_wbs_id FROM PROJWBS WHERE wbs_id = ? AND proj_node_flag = 'Y'"
                        + _alive(con, "PROJWBS"), (wid,)).fetchone()
        if r is None or r[0] in out:
            break
        out.append(r[0])
        wid = r[1]
    return out


def _code_type(con, pid: int, name: str) -> dict:
    """Activity code type called name (case ignored) that project pid sees: its own (AS_Project), else
    one of its EPS nodes (nearest first), else a global type. Raises when there is none, or when two
    share the best rank. shadowed: scopes of the other types with that name."""
    eps = _eps_chain(con, pid)

    def rank(p) -> Optional[int]:
        return 0 if p == pid else 1 + eps.index(p) if p in eps else 1000 if p is None else None
    seen = [r for r in con.execute("SELECT actv_code_type_id, actv_code_type, actv_code_type_scope, proj_id FROM ACTVTYPE "
                                   "WHERE 1=1" + _alive(con, "ACTVTYPE")) if rank(r["proj_id"]) is not None]
    hits = sorted((r for r in seen if (r["actv_code_type"] or "").lower() == str(name).strip().lower()),
                  key=lambda r: (rank(r["proj_id"]), r["actv_code_type_id"]))
    if not hits:
        raise ValueError(f"Activity code type {name!r} not found for this project; types: "
                         f"{sorted({r['actv_code_type'] for r in seen})}")
    best = [r for r in hits if rank(r["proj_id"]) == rank(hits[0]["proj_id"])]
    if len(best) > 1:
        raise ValueError(f"Several activity code types called {name!r} at the same level: ids "
                         f"{[r['actv_code_type_id'] for r in best]}")
    r = hits[0]
    return {"type": r["actv_code_type"], "type_id": r["actv_code_type_id"], "scope": r["actv_code_type_scope"],
            "shadowed": [h["actv_code_type_scope"] for h in hits[1:]]}


def _code_tree(con, type_id: int) -> dict[int, dict]:
    """Codes of a type by actv_code_id: id, short_name, name, level (1 = top), chain (ids from the
    top), path (short names joined with PREFER.name_sep_char, else '.') and sort (P6 order: seq_num,
    then id, at each level). A parent outside the type or a cycle ends the chain."""
    sep = "."
    if "name_sep_char" in _cols(con, "PREFER"):
        r = con.execute("SELECT name_sep_char FROM PREFER").fetchone()
        sep = (r and r[0]) or "."
    rows = {r["actv_code_id"]: r for r in con.execute(
        "SELECT actv_code_id, parent_actv_code_id, short_name, actv_code_name, seq_num FROM ACTVCODE "
        "WHERE actv_code_type_id = ?" + _alive(con, "ACTVCODE"), (type_id,))}
    out: dict[int, dict] = {}
    for cid, r in rows.items():
        chain, c = [], cid
        while c in rows and c not in chain and len(chain) < 50:
            chain.insert(0, c)
            c = rows[c]["parent_actv_code_id"]
        out[cid] = {"id": cid, "short_name": r["short_name"], "name": r["actv_code_name"], "level": len(chain),
                    "chain": tuple(chain), "path": sep.join(rows[c]["short_name"] or "" for c in chain),
                    "sort": tuple((rows[c]["seq_num"] or 0, c) for c in chain)}
    return out


def _code_filter(con, pid: int, code_type: Optional[str], code_value: Optional[str] = None,
                 include_children: bool = True) -> tuple[str, list, Optional[dict]]:
    """(condition on TASK t, args, code_filter info): activities with a code of code_type, any value
    when code_value is None, else the codes whose path, else short name, else name equals code_value
    (case ignored, every match kept: 'MEC' finds CON.MEC and COM.MEC) and, with include_children,
    their descendants. ('', [], None) without code_type."""
    if not code_type:
        if code_value is not None:
            raise ValueError("code_value needs code_type (the activity code type, e.g. 'Discipline')")
        return "", [], None
    info = _code_type(con, pid, code_type)
    tree = _code_tree(con, info["type_id"])
    order = lambda ids: sorted(ids, key=lambda i: tree[i]["sort"])  # noqa: E731
    ids: set = set()
    if code_value is not None:
        v, hit = str(code_value).strip().lower(), []
        for key in ("path", "short_name", "name"):
            hit = order(c["id"] for c in tree.values() if (c[key] or "").lower() == v)
            if hit:
                break
        if not hit:
            raise ValueError(f"No {info['type']} code {code_value!r}; codes: {[tree[i]['path'] for i in order(tree)][:20]}")
        ids = set(hit)
        kids = {c["id"] for c in tree.values() if ids & set(c["chain"][:-1])} if include_children else set()
        info.update(value=code_value, include_child_codes=bool(include_children), matched=[tree[i]["path"] for i in hit],
                    children=[tree[i]["path"] for i in order(kids - ids)][:ACT_LINKS_CAP])
        ids |= kids
    info["codes"] = len(ids) if code_value is not None else len(tree)
    sql = (" AND EXISTS (SELECT 1 FROM TASKACTV ta WHERE ta.task_id = t.task_id AND ta.actv_code_type_id = ?"
           + (f" AND ta.actv_code_id IN {_ids_sql(ids)}" if code_value is not None else "")
           + _alive(con, "TASKACTV", "ta") + ")")
    return sql, [info["type_id"]], info


# ----------------------------------------------------------------- read tools

@_tool()
def p6_list_projects(include_baselines: bool = False, offset: Offset = 0, limit: Limit = 200) -> dict:
    """Projects, one page: {total, offset, returned, next_offset, rows}. Row: Project ID, name, data
    date, planned start, scheduled finish, status (of the project WBS node). EPS nodes are never
    listed; baselines only with include_baselines=True (baseline_of is set)."""
    con = _connect()
    want = ["proj_id", "proj_short_name", "last_recalc_date", "plan_start_date", "scd_end_date"]
    have = [c for c in want if c in _cols(con, "PROJECT")]
    status = "w.status_code" if "status_code" in _cols(con, "PROJWBS") else "NULL"
    q = (f"SELECT {', '.join('p.' + c + ' AS ' + c for c in have)}, w.wbs_name AS project_name, "
         f"{status} AS status_code, o.proj_short_name AS baseline_of "
         "FROM PROJECT p LEFT JOIN PROJWBS w ON w.proj_id = p.proj_id AND w.proj_node_flag = 'Y'"
         + _alive(con, "PROJWBS", "w") + " LEFT JOIN PROJECT o ON o.proj_id = p.orig_proj_id "
         "WHERE COALESCE(p.project_flag, 'Y') = 'Y'" + _alive(con, "PROJECT", "p")
         + ("" if include_baselines else " AND p.orig_proj_id IS NULL")
         + " ORDER BY p.proj_short_name, p.proj_id")
    return _page(con, q, (), offset, limit)


@_tool()
def p6_list_wbs(project: ProjectId, parent: Optional[str] = None, max_depth: Optional[Depth] = None,
                rollups: bool = True, offset: Offset = 0, limit: Limit = 100) -> dict:
    """WBS tree in P6 order (each node before its children), one page: {total, offset, returned,
    next_offset, rows, root}. Row: wbs_id, parent_wbs_id, depth (project node 0), wbs_code (as P6
    shows it, e.g. 'PRJ.5.1'), wbs_short_name, wbs_name, children (direct), and with rollups=True
    the rollup of its whole subtree: activities, open (not completed, LOE and WBS summary left
    out), not_started, in_progress, completed, min_tf_d and negative_float (open activities, days
    of each calendar), start, finish (actual, else
    early), budget_units, actual_units (labor plus nonlabor), units_pct (actual / at completion, as
    P6 Units % Complete). parent= starts at that WBS code, full or relative to the project node
    ('5.1'); max_depth counts levels below the start node. root repeats the start node (unplaced =
    activities outside the tree, when it is the project node)."""
    if max_depth is not None and int(max_depth) < 0:
        raise ValueError("max_depth must be 0 or more")
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # tree, rollups and the stored count from one snapshot
    try:
        sep = _name_sep(con, pid)
        nodes = _wbs_tree(con, pid, sep)
        if not nodes:
            raise ValueError(f"Project {project} has no project WBS node.")
        own: dict[int, dict] = {}
        if rollups:
            own = {r["wbs_id"]: {k: r[k] for k in r.keys() if k != "wbs_id"} for r in con.execute(
                f"SELECT t.wbs_id AS wbs_id, {_act_agg_sql(con)} FROM TASK t LEFT JOIN CALENDAR c "
                "ON c.clndr_id = t.clndr_id WHERE t.proj_id = ?" + _alive(con, "TASK", "t") + " GROUP BY t.wbs_id",
                (pid,))}
        stored = con.execute("SELECT COUNT(*) FROM PROJWBS WHERE proj_id = ?" + _alive(con, "PROJWBS"),
                             (pid,)).fetchone()[0]
    finally:
        con.rollback()
    i = 0
    if parent:
        top = nodes[0]["wbs_code"]
        index: dict[str, int] = {}
        for k, n in enumerate(nodes):
            index.setdefault(n["wbs_code"], k)
        p = parent.strip()
        i = index.get(p, index.get(top + sep + p))
        if i is None:
            rel = [n["wbs_code"][len(top) + len(sep):] for n in nodes[1:]]
            near = (difflib.get_close_matches(p, rel, n=10)
                    or [r for r, n in zip(rel, nodes[1:]) if n["depth"] == 1][:10])
            raise ValueError(f"WBS code {parent!r} not found in {project}. Close codes: {near}")
    d0 = nodes[i]["depth"]
    end = next((k for k in range(i + 1, len(nodes)) if nodes[k]["depth"] <= d0), len(nodes))
    sub = [n for n in nodes[i:end] if max_depth is None or n["depth"] - d0 <= int(max_depth)]
    kids = Counter(n["parent_wbs_id"] for n in nodes)
    ids = {n["wbs_id"] for n in nodes}
    roll: dict[int, dict] = {}
    if rollups:
        for n in reversed(nodes):  # descendants come first in reverse pre-order
            w, par = n["wbs_id"], n["parent_wbs_id"]
            roll[w] = _merge_agg(roll.get(w, {}), own.get(w, {}))
            if par in ids:
                roll[par] = _merge_agg(roll.get(par, {}), roll[w])

    def row(n: dict) -> dict:
        out = {**n, "children": kids[n["wbs_id"]]}
        if rollups:
            out["rollup"] = _rollup(roll[n["wbs_id"]])
        return out

    res = _slice(sub, offset, limit)
    res["rows"] = [row(n) for n in res["rows"]]
    res["root"] = row(nodes[i])
    if rollups and i == 0:
        res["root"]["unplaced"] = sum(v["n"] for k, v in own.items() if k not in ids)
    if stored != len(nodes):
        res["note"] = f"{stored - len(nodes)} WBS rows not reachable from the project node."
    return res


@_tool()
def p6_query_activities(
    project: ProjectId,
    name_contains: Optional[str] = None,
    code_prefix: Optional[str] = None,
    wbs_name_contains: Optional[str] = None,
    status: Optional[Status] = None,
    max_total_float_days: Optional[float] = None,
    start_after: Optional[DateStr] = None,
    finish_before: Optional[DateStr] = None,
    code_type: Optional[str] = None,
    code_value: Optional[str] = None,
    include_child_codes: bool = True,
    udf_filter: Optional[UdfFilters] = None,
    udfs: Optional[UdfNames] = None,
    fields: Optional[ActFields] = None,
    offset: Offset = 0,
    limit: Limit = 50,
) -> dict:
    """Search activities, one page: {total, offset, returned, next_offset, rows}, ordered by start
    (actual, else early) then Activity ID. status: TK_NotStart, TK_Active or TK_Complete. Dates as
    YYYY-MM-DD (finish_before includes that day). wbs_name_contains matches the name of the
    activity's own WBS node only, not its parents: for a whole branch use p6_list_wbs, or an
    activity code filter. Durations and float in days of each activity's
    calendar. code_type (activity code type name, e.g. 'Discipline'; the project's own type wins over
    an EPS or global one) keeps activities with a code of that type; code_value narrows it to a code
    by path ('CON.MEC'), short name ('MEC': every match) or name, with its child codes unless
    include_child_codes=False; code_filter tells what was matched. fields= keeps task_code plus the
    fields named, e.g. ['task_name', 'early_start_date', 'early_end_date', 'total_float_d'], for
    smaller pages. UDFs (user defined columns, p6_list_udfs): udf_filter = [{udf: label, op, value}],
    all to hold; op eq (default), ne, lt, le, gt, ge, contains (text), empty, not_empty; a date alone
    covers the whole day; udf_filter (info) tells what was used. udfs = labels added to each row as
    udfs {label: value} (None when empty)."""
    if status is not None and status not in get_args(Status):
        raise ValueError(f"status must be one of {list(get_args(Status))}")
    row_fn = _act_row(fields)
    if isinstance(udfs, str):
        udfs = [udfs]
    con = _connect()
    pid = _project_id(con, project)
    cflt, cargs, cinfo = _code_filter(con, pid, code_type, code_value, include_child_codes)
    uflt, uargs, uinfo = _udf_filter(con, udf_filter)
    utypes = list({y["udf_type_id"]: y for y in (_udf_type(con, "TASK", u) for u in udfs or ())}.values())
    if utypes:
        base_fn = row_fn
        row_fn = lambda r: {**base_fn(r), "_tid": r["task_id"]}  # noqa: E731
    q = _act_from(con) + cflt + uflt
    args: list[Any] = [pid, *cargs, *uargs]
    if name_contains:
        q += " AND t.task_name LIKE ?"; args.append(f"%{name_contains}%")
    if code_prefix:
        q += " AND t.task_code LIKE ?"; args.append(f"{code_prefix}%")
    if wbs_name_contains:
        q += " AND w.wbs_name LIKE ?"; args.append(f"%{wbs_name_contains}%")
    if status:
        q += " AND t.status_code = ?"; args.append(status)
    if max_total_float_days is not None:
        q += f" AND t.total_float_hr_cnt <= ? * {HPD_SQL}"
        args.append(max_total_float_days)
    if start_after:
        q += " AND COALESCE(t.act_start_date, t.early_start_date) >= ?"
        args.append(_bound(start_after, "start_after", end=False))
    if finish_before:
        q += " AND COALESCE(t.act_end_date, t.early_end_date) <= ?"
        args.append(_bound(finish_before, "finish_before", end=True))
    q += " ORDER BY COALESCE(t.act_start_date, t.early_start_date), t.task_code, t.task_id"
    con.execute("BEGIN")  # the page and its UDF values from one snapshot
    try:
        res = _page(con, q, args, offset, limit, row_fn)
        if utypes:
            vals = _udf_values(con, "TASK", [r["_tid"] for r in res["rows"]], utypes)
            for r in res["rows"]:
                r["udfs"] = vals[r.pop("_tid")]
    finally:
        con.rollback()
    if cinfo:
        res["code_filter"] = cinfo
    if uinfo:
        res["udf_filter"] = uinfo
    return res


def _rel_sql(con, direction: str) -> str:
    """Links of activity :tid with the hours per day of both calendars, in a stable order."""
    parts = []
    for d, other, me in (("predecessor", "pred_task_id", "task_id"), ("successor", "task_id", "pred_task_id")):
        if direction in ("both", d[:4]):
            parts.append(
                f"SELECT '{d}' AS direction, r.task_pred_id, r.pred_type, r.lag_hr_cnt, r.proj_id AS rel_proj_id, "
                "o.task_code AS other_code, o.task_name AS other_name, po.proj_short_name AS other_project, "
                "COALESCE(NULLIF(cp.day_hr_cnt, 0), 8) AS pred_hpd, COALESCE(NULLIF(cs.day_hr_cnt, 0), 8) AS succ_hpd "
                f"FROM TASKPRED r JOIN TASK o ON o.task_id = r.{other} JOIN PROJECT po ON po.proj_id = o.proj_id "
                "JOIN TASK p ON p.task_id = r.pred_task_id JOIN TASK s ON s.task_id = r.task_id "
                "LEFT JOIN CALENDAR cp ON cp.clndr_id = p.clndr_id LEFT JOIN CALENDAR cs ON cs.clndr_id = s.clndr_id "
                f"WHERE r.{me} = :tid" + _alive(con, "TASKPRED", "r") + _alive(con, "TASK", "o")
                + _alive(con, "PROJECT", "po"))
    return ("SELECT * FROM (\n" + "\nUNION ALL\n".join(parts)
            + "\n) ORDER BY direction, other_project, other_code, pred_type, task_pred_id")


def _rel_row_fn(con, own_pid: int) -> Callable[[sqlite3.Row], dict]:
    """Row builder: lag in hours and in days under the lag rule of the link's (successor's) project."""
    rules: dict[int, str] = {}

    def row_fn(r: sqlite3.Row) -> dict:
        pid = own_pid if r["rel_proj_id"] is None else r["rel_proj_id"]
        for p in {pid, own_pid} - set(rules):
            rules[p] = _lag_rule(con, p)[0]
        hpd, basis = _lag_hpd(con, {"hpd": r["pred_hpd"]}, {"hpd": r["succ_hpd"]}, pid, rules[pid])
        out = {"direction": r["direction"], "task_pred_id": r["task_pred_id"], "other_code": r["other_code"],
               "other_name": r["other_name"], "other_project": r["other_project"],
               "type": REL_TYPES_REV.get(r["pred_type"], r["pred_type"]),
               "lag_h": r["lag_hr_cnt"], "lag_d": _days(r["lag_hr_cnt"], hpd)}
        if rules[pid] != rules[own_pid]:
            out["lag_basis"] = basis
        return out
    return row_fn


def _rel_page(con, t: sqlite3.Row, direction: str, offset: int = 0, limit: int = 100) -> dict:
    return _page(con, _rel_sql(con, direction), {"tid": t["task_id"]}, offset, limit,
                 _rel_row_fn(con, t["proj_id"]))


def _wbs_of(con, t: sqlite3.Row) -> dict:
    """wbs_name, wbs_code (as in p6_list_wbs) and wbs_path (names below the project node) of an
    activity, walking parent_wbs_id up to the project node (depth cap 50)."""
    chain, wid = [], t["wbs_id"]
    while wid is not None and len(chain) < 50:
        r = con.execute("SELECT parent_wbs_id, proj_node_flag, wbs_short_name, wbs_name FROM PROJWBS "
                        "WHERE wbs_id = ? AND proj_id = ?" + _alive(con, "PROJWBS"), (wid, t["proj_id"])).fetchone()
        if r is None:
            break
        chain.insert(0, r)
        wid = None if r["proj_node_flag"] == "Y" else r["parent_wbs_id"]
    return {"wbs_name": chain[-1]["wbs_name"] if chain else None,
            "wbs_code": _name_sep(con, t["proj_id"]).join(r["wbs_short_name"] or "" for r in chain) or None,
            "wbs_path": [r["wbs_name"] for r in chain if r["proj_node_flag"] != "Y"]}


def _memo_text(v) -> str:
    """Notebook TEXT or BLOB -> plain text: HTML tags removed, entities decoded, spaces collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", _blob_text(v))).split())


def _plus(a, b) -> Optional[float]:
    return None if a is None and b is None else float(a or 0) + float(b or 0)


def _udf_row(r: sqlite3.Row) -> dict:
    t = r["logical_data_type"]
    value = r[UDF_VALUE[t]] if t in UDF_VALUE else next(
        (r[k] for k in ("udf_text", "udf_number", "udf_date", "udf_code") if r[k] is not None), None)
    return {"label": r["udf_type_label"], "type": t, "value": value}


# ----------------------------------------------------------------- user defined fields (UDFs)

def _udf_type(con, table: str, udf: Any) -> sqlite3.Row:
    """UDFTYPE row of a subject table (TASK, PROJECT...) by label, the column title in P6: exact, else
    case and outer spaces ignored. Several matches or none raise (close labels listed)."""
    if not isinstance(udf, str) or not udf.strip():
        raise ValueError("udf: the UDF label (its column title in P6) is required")
    rows = con.execute("SELECT * FROM UDFTYPE WHERE table_name = ?" + _alive(con, "UDFTYPE")
                       + " ORDER BY udf_type_id", (table,)).fetchall()
    hit = [r for r in rows if r["udf_type_label"] == udf] or [
        r for r in rows if (r["udf_type_label"] or "").strip().casefold() == udf.strip().casefold()]
    if len(hit) == 1:
        return hit[0]
    if hit:
        raise ValueError(f"Several {table} UDFs are labelled {udf!r} (udf_type_id {[r['udf_type_id'] for r in hit]})")
    labels = [r["udf_type_label"] or "" for r in rows]
    near = difflib.get_close_matches(udf.strip(), labels, n=8, cutoff=0.4) or labels[:20]
    raise ValueError(f"No {table} UDF labelled {udf!r}. Close labels: {near} (p6_list_udfs lists them)")


def _udf_col(con, ltype: str) -> str:
    """SQL value of UDFVALUE alias v for a logical type: code short name, dates with a space."""
    if ltype == "FT_CODE":
        if not _cols(con, "UDFCODE") or "udf_code_id" not in _cols(con, "UDFVALUE"):
            return "NULL"
        return ("(SELECT uc.short_name FROM UDFCODE uc WHERE uc.udf_code_id = v.udf_code_id"
                + _alive(con, "UDFCODE", "uc") + ")")
    if ltype in ("FT_START_DATE", "FT_END_DATE"):
        return "REPLACE(v.udf_date, 'T', ' ')"
    return "v." + UDF_VALUE.get(ltype, "udf_text")


def _udf_out(ltype: str, v: Any) -> Any:
    """Stored value as shown: integers as int, dates with a space, the rest as stored."""
    if v is None:
        return None
    if ltype == "FT_INT" and isinstance(v, float) and v.is_integer():
        return int(v)
    if ltype in ("FT_START_DATE", "FT_END_DATE") and isinstance(v, str):
        return v.replace("T", " ")
    return v


UDF_CMP = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}


def _udf_filter(con, filters: Any) -> tuple[str, list, list[dict]]:
    """(conditions on TASK t, args, udf_filter info) of activity UDF conditions, all to hold. Numbers
    compare as numbers (eq / ne within 1e-9), dates as datetimes (a date alone is the whole day: eq
    any time that day, lt before it, le up to its end), text, indicators and codes ignoring case;
    contains = substring (text, indicator, code); empty = no value; ne needs a value (empty ones are
    not 'different')."""
    if not filters:
        return "", [], []
    if isinstance(filters, Mapping):
        filters = [filters]
    if not isinstance(filters, (list, tuple)):
        raise ValueError("udf_filter: a list of {udf, op, value}")
    sql, args, info = "", [], []
    for f in filters:
        if not isinstance(f, Mapping):
            raise ValueError("udf_filter: a list of {udf, op, value}")
        bad = sorted(set(f) - {"udf", "op", "value"})
        if bad:
            raise ValueError(f"udf_filter: unknown key(s) {bad}; allowed: ['op', 'udf', 'value']")
        op = f.get("op") or "eq"
        if op not in get_args(UdfOp):
            raise ValueError(f"udf_filter op must be one of {list(get_args(UdfOp))}")
        y = _udf_type(con, "TASK", f.get("udf"))
        lt, label = y["logical_data_type"], y["udf_type_label"]
        kind, col, v = UDF_KINDS.get(lt, "text"), _udf_col(con, lt), f.get("value")
        sub = ("SELECT 1 FROM UDFVALUE v WHERE v.udf_type_id = ? AND v.fk_id = t.task_id"
               + _alive(con, "UDFVALUE", "v"))
        a: list[Any] = [y["udf_type_id"]]
        if op in ("empty", "not_empty"):
            if v is not None:
                raise ValueError(f"udf_filter {label!r} {op} takes no value")
            sql += f" AND {'NOT ' if op == 'empty' else ''}EXISTS ({sub} AND NULLIF({col}, '') IS NOT NULL)"
        else:
            if v is None or v == "":
                raise ValueError(f"udf_filter {label!r} {op}: value is required (op 'empty' finds no value)")
            if op == "contains":
                if kind not in ("text", "indicator", "code"):
                    raise ValueError(f"udf_filter {label!r}: contains is for text, indicator and code UDFs ({kind})")
                cond = f"{col} LIKE ?"
                a.append(f"%{v}%")
            elif kind in ("number", "integer", "cost"):
                x = None if isinstance(v, bool) else _num(v)
                if x is None:
                    raise ValueError(f"udf_filter {label!r} is a {kind} UDF: value must be a number, not {v!r}")
                cond = {"eq": f"ABS({col} - ?) <= 1e-9", "ne": f"ABS({col} - ?) > 1e-9"}.get(op, f"{col} {UDF_CMP[op]} ?")
                a.append(x)
            elif kind in ("start_date", "finish_date"):
                dt, has_time = _parse_dt(v, f"udf_filter {label!r}")
                if has_time:
                    cond = f"{col} {UDF_CMP[op]} ?"
                    a.append(_fmt_dt(dt))
                else:
                    lo, hi = dt.strftime("%Y-%m-%d 00:00:00"), dt.strftime("%Y-%m-%d 23:59:59")
                    cond, b = {"eq": (f"{col} BETWEEN ? AND ?", [lo, hi]), "ne": (f"{col} NOT BETWEEN ? AND ?", [lo, hi]),
                               "lt": (f"{col} < ?", [lo]), "le": (f"{col} <= ?", [hi]),
                               "gt": (f"{col} > ?", [hi]), "ge": (f"{col} >= ?", [lo])}[op]
                    a += b
            else:
                cond = f"{col} {UDF_CMP[op]} ? COLLATE NOCASE"
                a.append(str(v).strip())
            sql += f" AND EXISTS ({sub} AND {cond})"
        args += a
        info.append({"udf": label, "data_type": kind, "op": op, "value": v})
    return sql, args, info


def _udf_values(con, table: str, ids: Sequence[int], types: Sequence[sqlite3.Row]) -> dict[int, dict[str, Any]]:
    """{fk_id: {label: value}} of the given UDF types for rows ids of table (None when empty)."""
    out = {i: {y["udf_type_label"]: None for y in types} for i in ids}
    if not ids or not types:
        return out
    by_id = {y["udf_type_id"]: y for y in types}
    code = ("(SELECT uc.short_name FROM UDFCODE uc WHERE uc.udf_code_id = v.udf_code_id" + _alive(con, "UDFCODE", "uc") + ")"
            if _cols(con, "UDFCODE") and "udf_code_id" in _cols(con, "UDFVALUE") else "NULL")
    for r in con.execute(f"SELECT v.fk_id, v.udf_type_id, v.udf_text, v.udf_number, v.udf_date, {code} AS udf_code "
                         f"FROM UDFVALUE v WHERE v.udf_type_id IN {_ids_sql(by_id)} AND v.fk_id IN {_ids_sql(ids)}"
                         + _alive(con, "UDFVALUE", "v")):
        y = by_id[r["udf_type_id"]]
        lt = y["logical_data_type"]
        if r["fk_id"] in out:
            out[r["fk_id"]][y["udf_type_label"]] = _udf_out(lt, r[UDF_VALUE.get(lt, "udf_text")])
    return out


def _memo_row(r: sqlite3.Row) -> dict:
    text = _memo_text(r["task_memo"])
    return {"topic": r["memo_type"], "text": text[:500], "truncated": len(text) > 500}


def _act_details(con, task_id: int) -> tuple[dict, list[str]]:
    """({assignments, codes, udfs, notebooks}, names of the lists cut at ACT_LINKS_CAP) of one
    activity. A list whose tables this P6 version lacks is empty."""
    al = functools.partial(_alive, con)
    unit, code, role = ("NULL", ""), ("NULL", ""), ("NULL", "")
    if _cols(con, "UMEASURE") and "unit_id" in _cols(con, "RSRC"):
        unit = ("u.unit_abbrev", " LEFT JOIN UMEASURE u ON u.unit_id = r.unit_id" + al("UMEASURE", "u"))
    if _cols(con, "UDFCODE") and "udf_code_id" in _cols(con, "UDFVALUE"):
        code = ("uc.short_name", " LEFT JOIN UDFCODE uc ON uc.udf_code_id = v.udf_code_id" + al("UDFCODE", "uc"))
    if _cols(con, "ROLES") and "role_id" in _cols(con, "TASKRSRC"):
        role = ("ro.role_short_name", " LEFT JOIN ROLES ro ON ro.role_id = a.role_id" + al("ROLES", "ro"))
    specs = (
        ("assignments", ("TASKRSRC", "RSRC"),
         f"SELECT a.taskrsrc_id, r.rsrc_short_name, r.rsrc_name, r.rsrc_type, {role[0]} AS role, {unit[0]} AS unit, a.target_qty, "
         "a.act_reg_qty, a.act_ot_qty, a.remain_qty, a.target_cost, a.act_reg_cost, a.act_ot_cost, a.remain_cost "
         "FROM TASKRSRC a LEFT JOIN RSRC r ON r.rsrc_id = a.rsrc_id" + al("RSRC", "r") + unit[1] + role[1]
         + " WHERE a.task_id = ?" + al("TASKRSRC", "a") + " ORDER BY r.rsrc_short_name, a.taskrsrc_id",
         lambda r: {"taskrsrc_id": r["taskrsrc_id"], "resource": r["rsrc_short_name"], "name": r["rsrc_name"], "type": r["rsrc_type"], "role": r["role"],
                    "unit": r["unit"], "target_qty": r["target_qty"], "actual_qty": _plus(r["act_reg_qty"], r["act_ot_qty"]),
                    "remain_qty": r["remain_qty"], "target_cost": r["target_cost"],
                    "actual_cost": _plus(r["act_reg_cost"], r["act_ot_cost"]), "remain_cost": r["remain_cost"]}),
        ("codes", ("TASKACTV", "ACTVTYPE", "ACTVCODE"),
         "SELECT y.actv_code_type, v.short_name, v.actv_code_name FROM TASKACTV a "
         "JOIN ACTVTYPE y ON y.actv_code_type_id = a.actv_code_type_id" + al("ACTVTYPE", "y")
         + " JOIN ACTVCODE v ON v.actv_code_id = a.actv_code_id" + al("ACTVCODE", "v")
         + " WHERE a.task_id = ?" + al("TASKACTV", "a") + " ORDER BY y.actv_code_type, a.actv_code_type_id",
         lambda r: {"type": r["actv_code_type"], "code": r["short_name"], "name": r["actv_code_name"]}),
        ("udfs", ("UDFVALUE", "UDFTYPE"),
         f"SELECT y.udf_type_label, y.logical_data_type, v.udf_text, v.udf_number, v.udf_date, {code[0]} AS udf_code "
         "FROM UDFVALUE v JOIN UDFTYPE y ON y.udf_type_id = v.udf_type_id" + al("UDFTYPE", "y") + code[1]
         + " WHERE y.table_name = 'TASK' AND v.fk_id = ?" + al("UDFVALUE", "v")
         + " ORDER BY y.udf_type_label, v.udf_type_id", _udf_row),
        ("notebooks", ("TASKMEMO", "MEMOTYPE"),
         "SELECT mt.memo_type, m.task_memo FROM TASKMEMO m LEFT JOIN MEMOTYPE mt ON mt.memo_type_id = m.memo_type_id"
         + al("MEMOTYPE", "mt") + " WHERE m.task_id = ?" + al("TASKMEMO", "m") + " ORDER BY mt.seq_num, m.memo_id",
         _memo_row),
    )
    out, cut = {}, []
    for key, tables, sql, fn in specs:
        rows = (con.execute(f"{sql} LIMIT {ACT_LINKS_CAP + 1}", (task_id,)).fetchall()
                if all(_cols(con, tb) for tb in tables) else [])
        out[key] = [fn(r) for r in rows[:ACT_LINKS_CAP]]
        if len(rows) > ACT_LINKS_CAP:
            cut.append(key)
    return out, cut


@_tool()
def p6_get_activity(project: ProjectId, task_code: ActivityId) -> dict:
    """Full detail of one activity: the list fields (see p6_query_activities), secondary
    constraint, remaining, expected, suspend and resume dates, calendar (hours_per_day converts
    hours to days), WBS (wbs_code as in p6_list_wbs, wbs_path names), predecessors and successors
    (first 50 of each, with predecessor_count / successor_count; page further with
    p6_get_relationships), assignments (resource and role, quantities and costs as stored),
    activity codes, UDFs and notebooks (first 50 of each), lag_rule and schedule_state."""
    con = _connect()
    t = _task(con, project, task_code)
    d = _dict(t)
    out = {**_act_view(t), **{k: d.get(k) for k in ACT_EXTRA}}
    out["cstr_label2"] = _cstr_label(d.get("cstr_type2"))
    cal = con.execute("SELECT clndr_name FROM CALENDAR WHERE clndr_id = ?", (t["clndr_id"],)).fetchone()
    out["calendar"] = {"clndr_id": t["clndr_id"], "name": cal[0] if cal else None, "hours_per_day": float(t["hpd"])}
    out.update(_wbs_of(con, t))
    for key, dr in (("predecessors", "pred"), ("successors", "succ")):
        pg = _rel_page(con, t, dr, 0, ACT_LINKS_CAP)
        out[key], out[key[:-1] + "_count"] = pg["rows"], pg["total"]
    if max(out["predecessor_count"], out["successor_count"]) > ACT_LINKS_CAP:
        out["links_note"] = f"Only the first {ACT_LINKS_CAP} of each list: page with p6_get_relationships."
    details, cut = _act_details(con, t["task_id"])
    out.update(details)
    if cut:
        out["details_note"] = f"Only the first {ACT_LINKS_CAP} rows of: {', '.join(cut)}."
    out["lag_rule"] = _lag_info(con, t["proj_id"])
    out["schedule_state"] = _schedule_state(con, t["proj_id"])
    return out


@_tool()
def p6_list_udfs(table: str = "TASK", project: Optional[ProjectId] = None, name_contains: Optional[str] = None,
                 offset: Offset = 0, limit: Limit = 100) -> dict:
    """User defined fields (UDFs, the 'User Defined' columns of P6) of one subject, one page: {total,
    offset, returned, next_offset, rows, table, project, used, subjects}. table: TASK (activities,
    default), PROJECT, PROJWBS, TASKRSRC, RSRC... (subjects lists those that have UDFs). Row:
    udf_type_id, label, data_type (text, indicator, start_date, finish_date, integer, number, cost,
    code), p6_type, formula (P6 computes the value; never written), values (stored values, in project
    when given), projects (projects holding values, without project), distinct (text, indicator and
    code values), min / max (numbers and dates), sum (numbers), codes (code UDFs: codes defined).
    used = UDFs with at least one value. Values: p6_query_activities(udfs=..., udf_filter=...)."""
    con = _connect()
    if not _cols(con, "UDFTYPE") or not _cols(con, "UDFVALUE"):
        raise ValueError("This database has no UDFTYPE / UDFVALUE tables")
    table = str(table or "TASK").strip().upper()
    con.execute("BEGIN")  # page, used count and subjects from one snapshot
    try:
        subjects = [r[0] for r in con.execute("SELECT DISTINCT table_name FROM UDFTYPE WHERE table_name IS NOT NULL"
                                              + _alive(con, "UDFTYPE") + " ORDER BY table_name")]
        if table not in subjects:
            raise ValueError(f"No UDF on table {table!r}; tables with UDFs: {subjects}")
        vflt, vargs, pid = "", [], None
        if project:
            if table not in UDF_PROJECT_TABLES:
                raise ValueError(f"project= applies to {list(UDF_PROJECT_TABLES)} UDFs, not {table} (global)")
            pid = _project_id(con, project)
            vflt, vargs = " AND v.proj_id = ?", [pid]
        ycols = _cols(con, "UDFTYPE")
        formula = "y.formula" if "formula" in ycols else "NULL"
        codes = ("(SELECT COUNT(*) FROM UDFCODE uc WHERE uc.udf_type_id = y.udf_type_id" + _alive(con, "UDFCODE", "uc") + ")"
                 if "udf_type_id" in _cols(con, "UDFCODE") else "NULL")
        code_id = "v.udf_code_id" if "udf_code_id" in _cols(con, "UDFVALUE") else "NULL"
        base = ("FROM UDFTYPE y LEFT JOIN UDFVALUE v ON v.udf_type_id = y.udf_type_id" + vflt + _alive(con, "UDFVALUE", "v")
                + " WHERE y.table_name = ?" + _alive(con, "UDFTYPE", "y"))
        nargs: list[Any] = [*vargs, table]
        if name_contains:
            base += " AND y.udf_type_label LIKE ?"
            nargs.append(f"%{name_contains}%")
        q = (f"SELECT y.udf_type_id, y.udf_type_label, y.logical_data_type, {formula} AS formula, "
             f"COUNT(v.fk_id) AS n, COUNT(DISTINCT v.proj_id) AS projects, "
             f"COUNT(DISTINCT COALESCE(v.udf_text, {code_id})) AS n_distinct, MIN(v.udf_number) AS mn, "
             "MAX(v.udf_number) AS mx, SUM(v.udf_number) AS sm, MIN(REPLACE(v.udf_date, 'T', ' ')) AS dmin, "
             f"MAX(REPLACE(v.udf_date, 'T', ' ')) AS dmax, {codes} AS codes "
             + base + " GROUP BY y.udf_type_id ORDER BY y.udf_type_label, y.udf_type_id")

        def row(r: sqlite3.Row) -> dict:
            lt = r["logical_data_type"]
            kind = UDF_KINDS.get(lt, lt)
            num, dated = kind in ("integer", "number", "cost"), kind in ("start_date", "finish_date")
            out = {"udf_type_id": r["udf_type_id"], "label": r["udf_type_label"], "data_type": kind, "p6_type": lt,
                   "formula": r["formula"], "values": r["n"]}
            if pid is None:
                out["projects"] = r["projects"]
            out.update(distinct=r["n_distinct"] if kind in ("text", "indicator", "code") else None,
                       min=_udf_out(lt, r["mn"]) if num else r["dmin"] if dated else None,
                       max=_udf_out(lt, r["mx"]) if num else r["dmax"] if dated else None,
                       sum=_udf_out(lt, round(r["sm"], 6)) if num and r["sm"] is not None else None,
                       codes=r["codes"] if kind == "code" else None)
            return out

        res = _page(con, q, nargs, offset, limit, row)
        used = con.execute(f"SELECT COUNT(*) FROM (SELECT y.udf_type_id {base} GROUP BY y.udf_type_id "
                           "HAVING COUNT(v.fk_id) > 0)", nargs).fetchone()[0]
    finally:
        con.rollback()
    res.update(table=table, project=project, used=used, subjects=subjects)
    return res


@_tool()
def p6_get_relationships(project: ProjectId, task_code: ActivityId, direction: Direction = "both",
                         offset: Offset = 0, limit: Limit = 100) -> dict:
    """Relationships of an activity, one page: {total, offset, returned, next_offset, rows, lag_rule}.
    direction: pred, succ or both. Lag in hours (lag_h) and days (lag_d); lag_rule tells which
    calendar converts lag days (project schedule option, predecessor calendar by default)."""
    if direction not in get_args(Direction):
        raise ValueError("direction must be pred, succ or both")
    con = _connect()
    t = _task(con, project, task_code)
    res = _rel_page(con, t, direction, offset, limit)
    res["lag_rule"] = _lag_info(con, t["proj_id"])
    return res


def _critical_clause(con, pid: int, mode: str, threshold_days: Optional[float]
                     ) -> tuple[str, list, str, Optional[float], str]:
    """(condition on TASK t / CALENDAR c, args, definition, threshold_h, mode_used) of 'critical'.
    project follows PROJECT.critical_path_type: CT_TotFloat = total float <= critical_drtn_hr_cnt
    hours, CT_DrivPath = longest path, anything else total float (said in the definition).
    threshold_days forces total float <= that many days of each activity's calendar."""
    if mode not in get_args(CpMode):
        raise ValueError(f"mode must be one of {list(get_args(CpMode))}")
    if threshold_days is not None:
        if mode == "longest_path":
            raise ValueError("float_threshold_days applies to total float, not to mode='longest_path'")
        return (f"t.total_float_hr_cnt <= ? * {HPD_SQL}", [float(threshold_days)],
                f"total float <= {float(threshold_days):g} d (days of each activity's calendar)", None, "total_float")
    cols = _cols(con, "PROJECT")
    p = con.execute("SELECT " + ", ".join(c if c in cols else f"NULL AS {c}" for c in
                                          ("critical_path_type", "critical_drtn_hr_cnt"))
                    + " FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone()
    hours, src = float(p["critical_drtn_hr_cnt"] or 0), ""
    if mode == "project":
        ctype = p["critical_path_type"]
        mode = "longest_path" if ctype == "CT_DrivPath" else "total_float"
        src = (f"project setting {ctype}: " if ctype in ("CT_DrivPath", "CT_TotFloat")
               else f"project setting {ctype!r} unknown, total float used: ")
    if mode == "longest_path":
        if "driving_path_flag" in _cols(con, "TASK"):
            return "t.driving_path_flag = 'Y'", [], src + "longest path (driving_path_flag)", None, "longest_path"
        src += "no driving_path_flag column, total float used: "
    return ("t.total_float_hr_cnt <= ?", [hours], src + f"total float <= {hours:g} h (critical_drtn_hr_cnt)",
            hours, "total_float")


@_tool()
def p6_critical_path(project: ProjectId, mode: CpMode = "project", float_threshold_days: Optional[float] = None,
                     include_completed: bool = False, fields: Optional[ActFields] = None,
                     offset: Offset = 0, limit: Limit = 50) -> dict:
    """Critical activities of the last P6 schedule run (F9), one page: {total, offset, returned,
    next_offset, rows, definition, mode_used, threshold_h, counts, schedule_state, note}.
    mode: project (default) follows the project setting (total float <= its hours threshold, or
    longest path); total_float uses that hours threshold; longest_path uses P6's driving path flag.
    float_threshold_days forces total float <= that many days (not with longest_path). LOE and WBS
    summary activities are left out (even when P6 flags them), completed ones too unless
    include_completed=True. Order: total float then start, or float path order for the longest
    path. counts (open activities except LOE and WBS summary, any page): open, total_float,
    longest_path, negative_float; excluded_summary = LOE and WBS summary activities that meet the
    definition and the status filter but are not listed. fields= as in p6_query_activities."""
    row_fn = _act_row(fields)
    con = _connect()
    pid = _project_id(con, project)
    cond, args, definition, thr_h, used = _critical_clause(con, pid, mode, float_threshold_days)
    tf_cond, tf_args = _critical_clause(con, pid, "total_float", float_threshold_days)[:2]
    definition += "; LOE and WBS summary activities left out"
    tcols = _cols(con, "TASK")
    types, open_ = f" AND t.task_type NOT IN {SUMMARY_TYPES}", " AND t.status_code <> 'TK_Complete'"
    status = "" if include_completed else open_
    start = "COALESCE(t.act_start_date, t.early_start_date)"
    if used == "longest_path" and {"float_path", "float_path_order"} <= tcols:
        order = f"COALESCE(t.float_path, 1e9), t.float_path_order, {start}, t.task_code, t.task_id"
    elif used == "longest_path":
        order = f"{start}, t.task_code, t.task_id"
    else:
        order = f"t.total_float_hr_cnt, {start}, t.task_code, t.task_id"
    q = _act_from(con) + types + status + f" AND {cond} ORDER BY {order}"
    lp = "SUM(t.driving_path_flag = 'Y')" if "driving_path_flag" in tcols else "NULL"
    base = "FROM TASK t LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id WHERE t.proj_id = ?" + _alive(con, "TASK", "t")
    con.execute("BEGIN")  # page and counts from one snapshot
    try:
        res = _page(con, q, [pid, *args], offset, limit, row_fn)
        c = con.execute(f"SELECT COUNT(*), SUM({tf_cond}), {lp}, SUM(t.total_float_hr_cnt < 0) {base}" + types + open_,
                        [*tf_args, pid]).fetchone()
        x = con.execute(f"SELECT COUNT(*) {base} AND t.task_type IN {SUMMARY_TYPES}{status} AND {cond}",
                        [pid, *args]).fetchone()[0]
    finally:
        con.rollback()
    res.update(definition=definition, mode_used=used, threshold_h=thr_h,
               counts={"open": c[0], "total_float": c[1] or 0, "longest_path": c[2] if lp == "NULL" else c[2] or 0,
                       "negative_float": c[3] or 0, "excluded_summary": x},
               schedule_state=_schedule_state(con, pid),
               note="Float and the longest path come from the last F9; completed activities carry no total float.")
    return res


@_tool()
def p6_float_paths(project: ProjectId, path: Optional[PathNo] = None, include_completed: bool = False,
                   fields: Optional[ActFields] = None, offset: Offset = 0, limit: Limit = 50) -> dict:
    """Float paths of the last P6 schedule run (Schedule Options, multiple float paths), one page
    in P6 order (path, then order on the path): {total, offset, returned, next_offset, rows, paths,
    source, schedule_state}. Path 1 is the most critical. paths summarises each path whatever the
    page: activities, first_order, last_order, min_tf_d, max_tf_d, finish. path= keeps one path.
    Completed activities only with include_completed=True. When P6 stored no float path, rows are
    the longest path activities (driving_path_flag, LOE and WBS summary left out as in
    p6_critical_path) by start and source is 'driving_path_flag'."""
    row_fn = _act_row(fields)
    con = _connect()
    pid = _project_id(con, project)
    tcols = _cols(con, "TASK")
    status = "" if include_completed else " AND t.status_code <> 'TK_Complete'"
    stored = {"float_path", "float_path_order"} <= tcols and con.execute(
        "SELECT 1 FROM TASK t WHERE t.proj_id = ? AND t.float_path IS NOT NULL" + _alive(con, "TASK", "t"),
        (pid,)).fetchone() is not None
    if stored:
        cond, args = status + " AND t.float_path IS NOT NULL", [pid]
        if path is not None:
            cond += " AND t.float_path = ?"; args.append(int(path))
        con.execute("BEGIN")
        try:
            res = _page(con, _act_from(con) + cond + " ORDER BY t.float_path, t.float_path_order, t.task_id",
                        args, offset, limit, row_fn)
            r2 = lambda v: None if v is None else round(v, 2)  # noqa: E731
            res["paths"] = [{"path": r["path"], "activities": r["n"], "first_order": r["first_order"],
                             "last_order": r["last_order"], "min_tf_d": r2(r["min_tf"]), "max_tf_d": r2(r["max_tf"]),
                             "finish": r["finish"]} for r in con.execute(
                f"SELECT t.float_path AS path, COUNT(*) AS n, MIN(t.float_path_order) AS first_order, "
                f"MAX(t.float_path_order) AS last_order, MIN(t.total_float_hr_cnt / {HPD_SQL}) AS min_tf, "
                f"MAX(t.total_float_hr_cnt / {HPD_SQL}) AS max_tf, MAX(COALESCE(t.act_end_date, t.early_end_date)) "
                "AS finish FROM TASK t LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id WHERE t.proj_id = ?"
                + _alive(con, "TASK", "t") + cond + " GROUP BY t.float_path ORDER BY t.float_path", args)]
        finally:
            con.rollback()
        res["source"] = "float_path"
    else:
        if path is not None:
            raise ValueError(f"P6 stored no float paths for {project}: enable multiple float paths in the "
                             "schedule options and press F9, or call without path=.")
        if "driving_path_flag" not in tcols:
            raise ValueError("This database stores neither float paths nor driving_path_flag.")
        q = (_act_from(con) + status + f" AND t.task_type NOT IN {SUMMARY_TYPES} AND t.driving_path_flag = 'Y' "
             "ORDER BY COALESCE(t.act_start_date, t.early_start_date), t.task_code, t.task_id")
        res = _page(con, q, [pid], offset, limit, row_fn)
        res.update(paths=[], source="driving_path_flag",
                   note="P6 stored no float paths for this project: longest path activities (LOE and WBS summary "
                        "left out) listed by start.")
    res["schedule_state"] = _schedule_state(con, pid)
    return res


# ----------------------------------------------------------------- analysis: DCMA 14, progress, compare

def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _dcma_settings(con, pid: int, overrides: Mapping, high_float_days: Optional[float]) -> dict:
    """Threshold and limit of each DCMA check: the project's PROJPROP 'schedule_check' when stored (its
    *_constraint limits are hours; a check_* flag 'N' disables the check), else the DCMA defaults (limits
    44 d of each calendar). overrides (by check name; they also evaluate a disabled check) and
    high_float_days (days) win."""
    if "critical_path_test" in overrides:
        raise ValueError("critical_path_test is not run (needs a CPM engine): it takes no threshold")
    bad = sorted(set(overrides) - set(DCMA_CHECKS))
    if bad:
        raise ValueError(f"Unknown check(s) in thresholds: {bad}; checks: {list(get_args(ThresholdCheck))}")
    nan = [k for k, v in overrides.items() if _num(v) is None]
    if nan:
        raise ValueError(f"thresholds must be numbers: {nan}")
    hfd = None if high_float_days is None else _num(high_float_days)
    if high_float_days is not None and (hfd is None or hfd < 0):
        raise ValueError("high_float_days must be 0 or more")
    tree = _props(con, pid, "schedule_check")
    p = {k.lower(): v for k, v in tree["attrs"].items()} if tree and not tree.get("error") else {}
    out: dict[str, Any] = {"source": "PROJPROP schedule_check" if p else "DCMA defaults", "threshold": {}, "limit": {},
                           "disabled": [], "notes": [f"{tree['error']}: DCMA defaults used"] if tree and tree.get("error") else []}
    for name, (default, _, key, lim, flag) in DCMA_CHECKS.items():
        thr = default if _num(p.get(key)) is None else _num(p[key])
        if name == "cpli" and _num(p.get(key)) is not None:  # P6 stores a tolerance in % (5 beside bei_target 0.95)
            thr = round(1 - thr / 100, 4)
            out["notes"].append(f"cpli_target {p[key]} read as a tolerance in %: CPLI at least {thr:g}")
        if p.get(flag) == "N" and name in overrides:
            out["notes"].append(f"{name}: disabled in the project's Check Schedule settings, evaluated with thresholds=")
        elif p.get(flag) == "N":
            thr = None
            out["disabled"].append(name)
        out["threshold"][name] = _num(overrides[name]) if name in overrides else thr
        if lim:
            out["limit"][name] = ("h", _num(p[lim])) if _num(p.get(lim)) is not None else ("d", DCMA_LIMIT_DAYS)
    if hfd is not None:
        out["limit"]["high_float"] = ("d", hfd)
    return out


def _chk(count: Optional[int], denom: Optional[int], threshold: Optional[float], rule: Optional[str],
         sample: Optional[list] = None, **extra) -> dict:
    """One DCMA check: count as a % of denom, compared with threshold by rule ('max', 'min'; 'ratio'
    compares extra['value']), unrounded; pct and value are rounded afterwards. pass is None when not
    evaluated (report only, no denominator)."""
    pct, value = 100 * count / denom if count is not None and denom else None, extra.get("value")
    if threshold is None:
        ok = None
    elif rule == "ratio":
        ok = None if value is None else value >= threshold
    else:
        ok = None if pct is None else pct >= threshold if rule == "min" else pct <= threshold
    if value is not None:
        extra["value"] = round(value, 4)
    return {"count": count, "denominator": denom, "pct": None if pct is None else round(pct, 2), "threshold": threshold,
            "pass": ok, **extra, "sample": sample or []}


def _limit(lim: tuple, col: str, hpd_sql: str, key: str, args: dict) -> tuple[str, dict]:
    """(condition col > limit, extra keys) for a ('h', hours) or ('d', days of hpd_sql) limit."""
    args[key] = lim[1]
    if lim[0] == "h":
        return f"{col} > :{key}", {"limit_h": lim[1]}
    return f"{col} > :{key} * {hpd_sql}", {"limit_d": lim[1]}


def _cpli(con, prj: sqlite3.Row, threshold: Optional[float]) -> dict:
    """CPLI = (critical path length + total float) / critical path length: length from the data date to
    the scheduled finish, float from the scheduled finish to the must finish by date; working days of
    the project calendar, calendar days beside it."""
    out = {"count": None, "denominator": None, "pct": None, "threshold": threshold, "pass": None, "value": None,
           "sample": []}
    dd, end, must = (_db_dt(prj[k]) for k in ("last_recalc_date", "scd_end_date", "plan_end_date"))
    if not (dd and end and must) or end <= dd:
        return {**out, "note": "not computed: needs a data date, a scheduled finish after it and a must finish by "
                               "date (PROJECT.plan_end_date)"}
    cd = lambda a, b: (b - a).total_seconds() / 86400  # noqa: E731
    cpl, tf = cd(dd, end), cd(end, must)
    value = (cpl + tf) / cpl
    calendar_days = {"cpl": round(cpl, 2), "tf": round(tf, 2), "value": round(value, 4)}
    cal = None
    try:
        cal = _calendar(con, prj["clndr_id"]) if prj["clndr_id"] is not None else None
    except ValueError:
        pass
    wd = None
    if cal is not None and not cal.error and cal.work_hours(dd, end) > 0:
        cpl, tf = cal.work_days(dd, end), cal.work_days(end, must)
        value = (cpl + tf) / cpl
        wd = {"cpl": round(cpl, 2), "tf": round(tf, 2), "value": round(value, 4)}
    return {**out, "value": round(value, 4), "pass": None if threshold is None else value >= threshold,
            "working_days": wd, "calendar_days": calendar_days,
            "basis": f"working days of the project calendar {cal.name!r}" if wd else
                     "calendar days (no readable project calendar)",
            "note": "approximation from the project dates of the last F9 (data date, scd_end_date, plan_end_date)"}


HC_ACT_FIELDS = ["task_name", "status_code", "task_type", "early_start_date", "early_end_date", "act_start_date",
                 "act_end_date", "cstr_type", "cstr_label", "cstr_date", "orig_dur_d", "rem_dur_d", "total_float_d"]


def _dcma(con, pid: int, bid: Optional[int], cfg: dict, baseline_dates: str, basis: str
          ) -> tuple[dict, int, int, dict, dict]:
    """(checks, open activities, links, {no_predecessor, no_successor: {count, sample}}, items) of the
    DCMA 14 assessment; run inside one read snapshot. items: check -> (sql, args, row_fn, key_fn) of
    its offending items in sample order (drill-down pages, health snapshots)."""
    al = functools.partial(_alive, con)
    pcols, tcols = _cols(con, "PROJECT"), _cols(con, "TASK")
    prj = con.execute("SELECT " + ", ".join(c if c in pcols else f"NULL AS {c}" for c in (
        "last_recalc_date", "scd_end_date", "plan_end_date", "clndr_id")) + " FROM PROJECT WHERE proj_id = ?",
        (pid,)).fetchone()
    thr, lim, rule = cfg["threshold"], cfg["limit"], (lambda name: DCMA_CHECKS[name][1])
    args: dict[str, Any] = {"pid": pid, "dd": prj["last_recalc_date"], "bid": bid}
    acts = ("FROM TASK t LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id WHERE t.proj_id = :pid" + al("TASK", "t")
            + f" AND t.task_type NOT IN {SUMMARY_TYPES}")
    op = " AND t.status_code <> 'TK_Complete'"
    work = op + " AND t.task_type IN ('TT_Task', 'TT_Rsrc')"

    def n(frm: str, cond: str = "1=1") -> int:
        return con.execute(f"SELECT COUNT(*) {frm} AND ({cond})", args).fetchone()[0]

    items: dict[str, tuple] = {}
    act_row = _act_row(HC_ACT_FIELDS)

    def act(name: str, base: str, cond: str, denom: int, cols: str = "", flags: Optional[Callable] = None,
            **extra) -> dict:
        rows = con.execute(f"SELECT t.task_code, t.task_name {acts}{base} AND ({cond}) "
                           "ORDER BY t.task_code, t.task_id LIMIT 15", args)
        items[name] = (f"SELECT t.*, {HPD_SQL} AS hpd{cols} {acts}{base} AND ({cond}) ORDER BY t.task_code, t.task_id",
                       args, (lambda r: {**act_row(r), **flags(r)}) if flags else act_row, lambda r: r["task_code"])
        return _chk(n(acts + base, cond), denom, thr[name], rule(name), [f"{r[0]} {r[1]}" for r in rows], **extra)

    n_open, n_work, n_all = n(acts + op), n(acts + work), n(acts)
    lag_hpd, lag_basis = _lag_hpd_sql(con, pid)
    links = ("FROM TASKPRED r JOIN TASK t ON t.task_id = r.task_id JOIN TASK p ON p.task_id = r.pred_task_id "
             "LEFT JOIN CALENDAR cp ON cp.clndr_id = p.clndr_id LEFT JOIN CALENDAR cs ON cs.clndr_id = t.clndr_id "
             "LEFT JOIN PROJECT pp ON pp.proj_id = p.proj_id WHERE t.proj_id = :pid"
             + al("TASKPRED", "r") + al("TASK", "t") + al("TASK", "p"))
    n_links = n(links)
    olinks = links + op + f" AND t.task_type NOT IN {SUMMARY_TYPES} AND p.task_type NOT IN {SUMMARY_TYPES}"
    n_olinks = n(olinks)

    def rel(name: str, cond: str, **extra) -> dict:
        sql = (f"SELECT r.task_pred_id, p.task_code AS pc, p.proj_id AS ppid, pp.proj_short_name AS pproj, "
               f"t.task_code AS sc, r.pred_type, r.lag_hr_cnt, {lag_hpd} AS h {olinks} AND ({cond}) "
               "ORDER BY t.task_code, p.task_code, r.task_pred_id")
        key = lambda r: (f"{r['pproj'] + ':' if r['ppid'] != pid else ''}{r['pc']} -> {r['sc']} "  # noqa: E731
                         f"{REL_TYPES_REV.get(r['pred_type'], r['pred_type'])}")
        smp = [f"{key(r)} lag {_days(r['lag_hr_cnt'] or 0, r['h']):g} d" for r in con.execute(sql + " LIMIT 15", args)]
        items[name] = (sql, args, lambda r: {
            "task_pred_id": r["task_pred_id"], "predecessor": r["pc"], **({"pred_project": r["pproj"]} if r["ppid"] != pid else {}),
            "successor": r["sc"], "type": REL_TYPES_REV.get(r["pred_type"], r["pred_type"]),
            "lag_d": _days(r["lag_hr_cnt"] or 0, r["h"]), "lag_h": r["lag_hr_cnt"]}, key)
        return _chk(n(olinks, cond), n_olinks, thr[name], rule(name), smp, **extra)

    checks: dict[str, dict] = {}
    # 1 logic: missing predecessor or successor (LOE and WBS summary give no logic); only the project start
    # and finish milestones are exempt
    has = {k: (f"EXISTS (SELECT 1 FROM TASKPRED r JOIN TASK o ON o.task_id = r.{other} WHERE r.{me} = t.task_id"
               + al("TASKPRED", "r") + al("TASK", "o") + f" AND o.task_type NOT IN {SUMMARY_TYPES}" + "{})")
           for k, other, me in (("pred", "pred_task_id", "task_id"), ("succ", "task_id", "pred_task_id"))}
    hp, hs = has["pred"].format(""), has["succ"].format("")
    allq = "FROM TASK t WHERE t.proj_id = :pid" + al("TASK", "t")
    ms = {"start": con.execute(f"SELECT t.task_id, t.task_code {allq} AND t.task_type = 'TT_Mile' AND NOT {hp} ORDER BY "
                               "COALESCE(t.act_start_date, t.early_start_date, '9999'), t.task_code, t.task_id LIMIT 1",
                               args).fetchone(),
          "finish": con.execute(f"SELECT t.task_id, t.task_code {allq} AND t.task_type = 'TT_FinMile' AND NOT {hs} ORDER BY "
                                "COALESCE(t.act_end_date, t.early_end_date, '') DESC, t.task_code, t.task_id LIMIT 1",
                                args).fetchone()}
    args.update(sms=ms["start"] and ms["start"][0], fms=ms["finish"] and ms["finish"][0])
    no_pred, no_succ = f"NOT {hp} AND t.task_id IS NOT :sms", f"NOT {hs} AND t.task_id IS NOT :fms"

    def listed(base: str, cond: str) -> dict:
        rows = con.execute(f"SELECT t.task_code, t.task_name {acts}{base} AND ({cond}) ORDER BY t.task_code, t.task_id "
                           "LIMIT 15", args)
        return {"count": n(acts + base, cond), "sample": [f"{r[0]} {r[1]}" for r in rows]}

    def dangling(cond: str) -> dict:  # milestones have one date: any link type drives it
        return listed(op + " AND t.task_type NOT IN ('TT_Mile', 'TT_FinMile')", cond)

    ends = {"no_predecessor": listed(op, no_pred), "no_successor": listed(op, no_succ)}
    checks["logic"] = act("logic", op, f"({no_pred}) OR ({no_succ})", n_open,
                          cols=f", ({no_pred}) AS np, ({no_succ}) AS ns",
                          flags=lambda r: {"missing": [k for k, c in (("predecessor", "np"), ("successor", "ns")) if r[c]]},
                          no_predecessor=ends["no_predecessor"]["count"], no_successor=ends["no_successor"]["count"],
                          exempt=[{"task_code": r[1], "milestone": f"project {k} (no {'predecessor' if k == 'start' else 'successor'})"}
                                  for k, r in ms.items() if r],
                          dangling={"open_start": dangling(f"{hp} AND NOT " + has["pred"].format(
                                        " AND r.pred_type IN ('PR_FS', 'PR_SS')")),
                                    "open_finish": dangling(f"{hs} AND NOT " + has["succ"].format(
                                        " AND r.pred_type IN ('PR_FS', 'PR_FF')")),
                                    "note": "information only (not in count or pass): start driven only by FF/SF "
                                            "predecessors, finish driving only SS/SF successors; milestones left out"})
    # 2 to 5: relationships whose successor is open
    checks["leads"] = rel("leads", "r.lag_hr_cnt < 0")
    # ladder pairs: SS + FF between the same two activities with the same lag (information only)
    lad = ("SELECT COUNT(*), SUM(lag > 0) FROM (SELECT COALESCE(r.lag_hr_cnt, 0) AS lag " + olinks
           + " AND r.pred_type IN ('PR_SS', 'PR_FF') GROUP BY r.task_id, r.pred_task_id, COALESCE(r.lag_hr_cnt, 0) "
             "HAVING COUNT(DISTINCT r.pred_type) = 2)")
    n_lad, n_lad_lag = (int(v or 0) for v in con.execute(lad, args).fetchone())
    pct = lambda a, b: None if not b else round(100 * a / b, 2)  # noqa: E731
    checks["lags"] = rel("lags", "r.lag_hr_cnt > 0")
    lg_ex = (checks["lags"]["count"] - 2 * n_lad_lag, n_olinks - 2 * n_lad)
    checks["lags"] = {**{k: v for k, v in checks["lags"].items() if k != "sample"}, "ladder_pairs": n_lad,
                      "lags_excluding_ladders": {"count": lg_ex[0], "denominator": lg_ex[1], "pct": pct(*lg_ex)},
                      "sample": checks["lags"]["sample"]}
    cond, extra = _limit(lim["long_lags"], "r.lag_hr_cnt", lag_hpd, "ll", args)
    checks["long_lags"] = rel("long_lags", cond, **extra, basis=f"lag days of the {lag_basis}" if "limit_d" in extra
                              else "lag hours (PROJPROP long_lags_constraint)")
    checks["fs_links"] = rel("fs_links", "r.pred_type = 'PR_FS'")
    checks["fs_links"] = {**{k: v for k, v in checks["fs_links"].items() if k != "sample"}, "ladder_pairs": n_lad,
                          "fs_pct_excluding_ladders": pct(checks["fs_links"]["count"], n_olinks - 2 * n_lad),
                          "ladder_note": "information only: SS + FF pairs with the same lag left out",
                          "sample": checks["fs_links"]["sample"]}
    cond = f"t.cstr_type IN {HARD_CSTR}" + (f" OR t.cstr_type2 IN {HARD_CSTR}" if "cstr_type2" in tcols else "")
    checks["hard_constraints"] = act("hard_constraints", op, cond, n_open)
    cond, extra = _limit(lim["high_float"], "t.total_float_hr_cnt", HPD_SQL, "hf", args)
    checks["high_float"] = act("high_float", op, cond, n_open, **extra)
    checks["negative_float"] = act("negative_float", op, "t.total_float_hr_cnt < 0", n_open)
    col = "t.remain_drtn_hr_cnt" if basis == "remaining" else "t.target_drtn_hr_cnt"
    cond, extra = _limit(lim["high_duration"], col, HPD_SQL, "hd", args)
    checks["high_duration"] = act("high_duration", work, cond, n_work, **extra,
                                  basis=f"{basis} duration of open TT_Task and TT_Rsrc activities")
    if args["dd"]:
        late = "julianday(t.act_start_date) > julianday(:dd) OR julianday(t.act_end_date) > julianday(:dd)"
        early = ("t.status_code <> 'TK_Complete' AND (julianday(t.early_start_date) < julianday(:dd) "
                 "OR julianday(t.early_end_date) < julianday(:dd))")
        checks["invalid_dates"] = act("invalid_dates", "", f"{late} OR ({early})", n_all,
                                      cols=f", ({late}) AS aft, ({early}) AS bef",
                                      flags=lambda r: {"actual_after_data_date": bool(r["aft"]),
                                                       "forecast_before_data_date": bool(r["bef"])},
                                      actual_after_data_date=n(acts, late), forecast_before_data_date=n(acts, early))
    else:
        checks["invalid_dates"] = _chk(None, n_all, thr["invalid_dates"], "max", note="no data date")
    if _cols(con, "TASKRSRC"):
        checks["resources"] = act("resources", work, "COALESCE(t.target_drtn_hr_cnt, 0) > 0 AND NOT EXISTS (SELECT 1 FROM "
                                  "TASKRSRC a WHERE a.task_id = t.task_id" + al("TASKRSRC", "a") + ")", n_work,
                                  basis="open TT_Task/TT_Rsrc with a duration and no TASKRSRC assignment")
        if thr["resources"] is None:
            checks["resources"]["note"] = ("not evaluated: DCMA sets no threshold and the project stores none; "
                                           "thresholds={'resources': 0} evaluates it")
    else:
        checks["resources"] = _chk(None, n_work, thr["resources"], "max", note="no TASKRSRC table")
    # 11 and 14: against the baseline, activities matched by Activity ID
    bfin = "COALESCE(b.act_end_date, b.early_end_date)" if baseline_dates == "current" else "b.target_end_date"
    if bid is not None and args["dd"]:
        cur = (f"WITH a AS {MAT_SQL}(SELECT t.task_id, t.task_code, t.status_code, t.act_end_date FROM TASK t "
               "WHERE t.proj_id = :pid" + al("TASK", "t") + ") ")
        due = ("FROM TASK b LEFT JOIN a ON a.task_code = b.task_code WHERE b.proj_id = :bid" + al("TASK", "b")
               + f" AND b.task_type NOT IN {SUMMARY_TYPES} AND julianday({bfin}) <= julianday(:dd)")
        miss = f"a.task_id IS NULL OR a.status_code <> 'TK_Complete' OR date(a.act_end_date) > date({bfin})"  # by day
        n_due, n_miss = (con.execute(cur + f"SELECT COUNT(*) {due}" + c, args).fetchone()[0] for c in ("", f" AND ({miss})"))
        msql = (cur + f"SELECT b.task_code, b.task_name, {bfin} AS bf, a.status_code, a.act_end_date {due} AND ({miss}) "
                "ORDER BY b.task_code, b.task_id")
        items["missed_tasks"] = (msql, args, lambda r: {
            "task_code": r["task_code"], "task_name": r["task_name"], "baseline_finish": r["bf"],
            "status_code": r["status_code"], "act_end_date": r["act_end_date"],
            "issue": "not in the project" if r["status_code"] is None else "finished later"
            if r["status_code"] == "TK_Complete" else "not finished"}, lambda r: r["task_code"])
        rows = [(r["task_code"], r["bf"], r["status_code"], r["act_end_date"])
                for r in con.execute(msql + " LIMIT 15", args).fetchall()]
        smp = [f"{r[0]} baseline finish {r[1]}: " + ("not in the project" if r[2] is None else f"finished {r[3]}"
                                                     if r[2] == "TK_Complete" else "not finished") for r in rows]
        checks["missed_tasks"] = _chk(n_miss, n_due, thr["missed_tasks"], "max", smp, basis=(
            f"baseline finish ({'actual, else early' if baseline_dates == 'current' else 'planned'}) at or before the "
            "data date: not finished, or finished on a later day"))
        done = n(acts, "t.status_code = 'TK_Complete'")
        bei = _chk(done, n_due, thr["bei"], "ratio", value=done / n_due if n_due else None,
                   basis="completed activities / activities whose baseline finish is at or before the data date")
    else:
        why = "no baseline: pass baseline= or set a project baseline in P6" if bid is None else "no data date"
        checks["missed_tasks"] = _chk(None, None, thr["missed_tasks"], "max", note=why)
        bei = _chk(None, None, thr["bei"], "ratio", note=why)
    checks["critical_path_test"] = _chk(None, None, None, None, note=(
        "not run here: full reports only (CPM engine, see p6_cpm_validate)"))
    checks["cpli"] = _cpli(con, prj, thr["cpli"])
    checks["bei"] = bei
    for name in cfg["disabled"]:
        checks[name]["note"] = "; ".join(filter(None, (checks[name].get("note"), "disabled in the project's Check "
                                                       "Schedule settings (PROJPROP schedule_check)")))
    return checks, n_open, n_links, ends, items


HcCheck = Literal[tuple(list(get_args(DcmaCheck)) + ["key_dates"])]  # drill-down names: the DCMA 14 + information checks
HC_NO_ITEMS = {"critical_path_test", "cpli", "bei"}  # project-level values, no item list
HC_TOTALS = ("open_incomplete_activities", "relationships", "open_successor_links", "non_fs_links")


def _hc_dur(d: dict) -> float:
    """Remaining hours of an activity in the key date paths (0 for completed activities and milestones)."""
    if d["status_code"] == "TK_Complete" or d["task_type"] in ("TT_Mile", "TT_FinMile"):
        return 0.0
    return float(d["remain_drtn_hr_cnt"] or 0)


def _downstream(net: _Net, src: int, start: datetime, finish: datetime) -> dict[int, datetime]:
    """Earliest finish of every activity src reaches (links through LOE and WBS summary activities left
    out) when src starts and finishes at the given dates and nothing else holds them: a forward pass
    from src alone, remaining durations on each activity's calendar, lags on the lag rule calendar
    (FS: lag after the predecessor finish, then the duration; SS: after its start, then the duration;
    FF: after its finish; SF: after its start)."""
    seen, stack = {src}, [src]
    while stack:
        n = stack.pop()
        for lk in net.by_pred.get(n, []):
            s = lk["succ"]
            if s in net.t and s not in seen and not net.summary(net.t[s]):
                seen.add(s)
                stack.append(s)
    indeg = dict.fromkeys(seen, 0)
    for n in seen:
        for lk in net.by_pred.get(n, []):
            if lk["succ"] in seen:
                indeg[lk["succ"]] += 1
    order, ready = [], [n for n in seen if not indeg[n]]
    while ready:
        n = ready.pop()
        order.append(n)
        for lk in net.by_pred.get(n, []):
            if lk["succ"] in seen:
                indeg[lk["succ"]] -= 1
                if not indeg[lk["succ"]]:
                    ready.append(lk["succ"])

    def shift(cal, dt, h):
        if cal is None or cal.error:
            return dt + timedelta(hours=h)
        return cal.add_work_hours(dt, h) if h >= 0 else cal.subtract_work_hours(dt, -h)
    st, fin = {src: start}, {src: finish}
    for n in order:  # nodes of a cycle never become ready: left out
        if n not in fin:
            continue
        p = net.t[n]
        for lk in net.by_pred.get(n, []):
            s = lk["succ"]
            if s not in seen:
                continue
            d, lc = net.t[s], net.lag_cal(p, net.t[s])
            a = shift(lc, (fin[n] if LINK_FROM[lk["type"]] == "f" else st[n]), lk["lag"])
            c = d["cal"] if d["cal"] is not None and not d["cal"].error else None
            f = (shift(c, a, _hc_dur(d)) if LINK_TO[lk["type"]] == "s" else a)
            if s not in fin or f > fin[s]:
                fin[s] = f
                st[s] = shift(c, f, -_hc_dur(d))
    return fin


def _key_dates(net: _Net, thr: Optional[float]) -> tuple[dict, list[dict]]:
    """Information check key_dates: constraint dates set against the project must finish date and
    against each other, with the least working time the network needs after each one."""
    pid, tol = net.pid, timedelta(minutes=5)
    own = [d for d in net.t.values() if d["proj_id"] == pid and not net.summary(d) and d["status_code"] != "TK_Complete"]
    has_succ = lambda d: any(not net.summary(net.t[lk["succ"]]) for lk in net.by_pred.get(d["task_id"], []))  # noqa: E731
    ends = sorted((d for d in own if not has_succ(d)), key=lambda d: (d["task_type"] == "TT_FinMile", d["ef"] or datetime.min,
                                                                    d["task_code"]))
    fm = ends[-1] if ends else None
    cons = [(d, ty, dt) for d in own for ty, dt in ((d["cstr_type"], d["cd"]), (d["cstr_type2"], d["cd2"]))
            if ty and ty != "CS_ALAP" and dt is not None]
    rows = []
    fcal = (fm and fm["cal"]) or net.pcal

    def add(cal, a: datetime, h: float) -> datetime:
        if cal is None or cal.error:
            return a + timedelta(hours=h)
        return cal.add_work_hours(a, max(h, 0.0))

    def base(d, ty, dt) -> dict:
        return {"task_code": d["task_code"], "task_name": d["task_name"], "task_type": d["task_type"], "cstr_type": ty,
                "cstr_label": _cstr_label(ty), "cstr_date": _fmt_dt(dt)}
    def span(d, ty, dt) -> tuple[datetime, datetime]:
        if ty in CSTR_FINISH:
            if d["cal"] is None or d["cal"].error:
                return dt - timedelta(hours=_hc_dur(d)), dt
            return d["cal"].subtract_work_hours(dt, _hc_dur(d)), dt
        return dt, add(d["cal"], dt, _hc_dur(d))
    for d, ty, dt in cons:
        start, fin = span(d, ty, dt)
        if net.must and dt > net.must + tol:
            rows.append({**base(d, ty, dt), "issue": "after_must_finish", "must_finish": net.prj["plan_end_date"],
                         "detail": "the constraint date is later than the project must finish date"})
        if fm is None or net.must is None:
            continue
        dd_ = _downstream(net, d["task_id"], start, fin)
        if fm["task_id"] not in dd_:
            continue
        proj = dd_[fm["task_id"]]
        need = fcal.work_days(fin, proj) if fcal is not None and not fcal.error else (proj - fin).total_seconds() / 86400
        if proj > net.must + tol:
            rows.append({**base(d, ty, dt), "issue": "downstream_exceeds_must_finish", "finish_milestone": fm["task_code"],
                         "downstream_d": round(need, 2), "projected_finish": _fmt_dt(proj),
                         "must_finish": net.prj["plan_end_date"],
                         "detail": f"the work and lags after its constraint date reach {fm['task_code']} on "
                                   f"{_fmt_dt(proj)} ({round(need, 2)} d later), after the must finish date"})
    miles = [(d, ty, dt) for d, ty, dt in cons if d["task_type"] in ("TT_Mile", "TT_FinMile")]
    for d, ty, dt in miles:
        dd_ = _downstream(net, d["task_id"], dt, dt)
        for e, ty2, dt2 in miles:
            if e["task_id"] == d["task_id"] or e["task_id"] not in dd_:
                continue
            earliest = dd_[e["task_id"]]
            if earliest > dt2 + tol:
                cal = e["cal"] if e["cal"] is not None and not e["cal"].error else None
                gap = cal.work_days(dt, earliest) if cal else (earliest - dt).total_seconds() / 86400
                rows.append({**base(e, ty2, dt2), "issue": "milestones_too_close", "other": d["task_code"],
                             "other_cstr_date": _fmt_dt(dt), "path_d": round(gap, 2),
                             "earliest_allowed": _fmt_dt(earliest),
                             "detail": f"the work and lags between {d['task_code']} ({_fmt_dt(dt)}) and it take "
                                       f"{round(gap, 2)} d: it cannot be met before {_fmt_dt(earliest)}"})
    if fm is not None and net.must and net.end and net.end > net.must + tol:
        rows.append({"task_code": fm["task_code"], "task_name": fm["task_name"], "task_type": fm["task_type"],
                     "issue": "project_finish_after_must_finish", "project_finish": net.prj["scd_end_date"],
                     "must_finish": net.prj["plan_end_date"],
                     "detail": "the last F9 finishes the project after its must finish date"})
    rows.sort(key=lambda r: (r["task_code"], r["issue"], r.get("other") or ""))
    n_items = len({r["task_code"] for r in rows})
    # count = issue rows (what check='key_dates' pages); pct = constraints (plus the must finish) with an issue
    hit = len({(r["task_code"], r.get("cstr_type")) for r in rows})
    denom = len(cons) + (1 if net.must else 0)
    chk = _chk(len(rows), denom, thr, "max",
               [f"{r['task_code']} {r['issue']}" + (f" ({r['other']})" if r.get("other") else "") for r in rows[:15]],
               activities=n_items, finish_milestone=fm and fm["task_code"], must_finish=net.prj["plan_end_date"],
               note="information only; items: check='key_dates'")
    chk["pct"] = round(100 * hit / denom, 2) if denom else None
    return chk, rows


def _hc_snap_dir() -> str:
    return os.path.join(_log_dir(), "health")


def _hc_snapshots(project: Optional[str] = None, this_db: bool = True) -> list[dict]:
    """Health snapshots (metadata and content), oldest first."""
    folder, out = _hc_snap_dir(), []
    if not os.path.isdir(folder):
        return out
    db = os.path.basename(DB_PATH).lower()
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                snap = json.load(fh)
        except (OSError, ValueError):
            continue
        if (project is None or snap.get("project") == project) and (not this_db or str(snap.get("db", "")).lower() == db):
            out.append(snap)
    out.sort(key=lambda s: (s.get("ts", ""), s.get("id", "")))
    return out


def _hc_snapshot_save(snap: dict) -> str:
    folder = _hc_snap_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, snap["id"] + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, default=str)
    return path


@_tool()
def p6_health_check(project: ProjectId, baseline: Optional[ProjectId] = None, baseline_dates: BaselineDates = "current",
                    duration_basis: DurationBasis = "remaining", thresholds: Optional[Thresholds] = None,
                    high_float_days: Optional[NonNegDays] = None, check: Optional[HcCheck] = None,
                    offset: Offset = 0, limit: Limit = 100, save_snapshot: bool = True) -> dict:
    """DCMA 14 point assessment of the last P6 schedule run (F9). checks: logic, leads, lags, long_lags,
    fs_links, hard_constraints, high_float, negative_float, high_duration, invalid_dates, resources,
    missed_tasks, critical_path_test (this server's CPM engine: the earliest lowest-float open task
    delayed 100 working days must move the project finish as much; not run when the engine does not
    match the F9, see p6_cpm_validate), cpli, bei; each {count, denominator, pct, threshold,
    pass, sample (15)}; then key_dates (information only: constraint dates against the must finish
    date, the work after them and each other; count = issues, pct = constraints with an issue). Open = not completed; LOE and WBS summary activities
    are left out, also as either end of a link; relationship checks count links whose successor is
    open. pct must stay at most the threshold, except fs_links (at least), cpli and bei (value at
    least), compared before rounding; pass is None when not evaluated. Thresholds: the project's Check
    Schedule settings (PROJPROP schedule_check, limits in hours) when stored, else DCMA defaults
    (limits 44 d of each calendar; resources has none: not evaluated unless thresholds= sets it);
    threshold_source says which. thresholds= overrides by check name (not critical_path_test);
    high_float_days sets the high float limit. lags and fs_links also give ladder_pairs (SS + FF
    between the same activities with the same lag) and their values without them (information). Logic
    exempts only the project start and finish milestones (listed); dangling starts and finishes
    (milestones left out, see p6_dangling) are information only. missed_tasks and bei need a baseline:
    baseline= (Project ID), else the project baseline; baseline_dates 'current' (actual, else early
    finish) or 'planned' (its planned finish); a task is missed when not finished or finished on a
    later day. duration_basis: remaining or original. cpli: working days of the project calendar
    (calendar days beside it). check= returns that check's full list of items as a page {total,
    offset, returned, next_offset, rows} with result (the check) instead of the whole report. A full
    run saves a snapshot for p6_health_check_compare (snapshot.id; save_snapshot=False skips it). The
    earlier top keys stay: no_predecessor, no_successor, hard_constraints, high_float, negative_float
    {count, sample}; leads_negative_lag, positive_lags, non_fs_links (links whose successor is open)."""
    if baseline_dates not in get_args(BaselineDates):
        raise ValueError(f"baseline_dates must be one of {list(get_args(BaselineDates))}")
    if duration_basis not in get_args(DurationBasis):
        raise ValueError(f"duration_basis must be one of {list(get_args(DurationBasis))}")
    if check is not None and check not in get_args(HcCheck):
        raise ValueError(f"check must be one of {list(get_args(HcCheck))}")
    if check in HC_NO_ITEMS:
        raise ValueError(f"{check} is a project-level value without an item list")
    offset, limit = _check_page(offset, limit)
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # settings and every check from one snapshot
    cfg = _dcma_settings(con, pid, dict(thresholds or {}), high_float_days)
    bid = _baseline_pid(con, pid, baseline)
    try:
        checks, n_open, n_links, ends, items = _dcma(con, pid, bid, cfg, baseline_dates, duration_basis)
        if check in (None, "key_dates"):  # a drill-down of another check does not need the network
            kd, kd_rows = _key_dates(_Net(con, pid), None)
            checks["key_dates"] = kd
        if check is None and "critical_path_test" not in cfg["disabled"]:
            checks["critical_path_test"] = _cpt(con, pid)
        bl = con.execute("SELECT proj_short_name, " + ("last_recalc_date" if "last_recalc_date" in _cols(con, "PROJECT")
                         else "NULL") + " FROM PROJECT WHERE proj_id = ?", (bid,)).fetchone()
        if check == "key_dates":
            page = _slice(kd_rows, offset, limit)
        elif check:
            if check not in items:
                raise ValueError(f"{check}: no item list here ({checks[check].get('note') or 'not evaluated'})")
            sql, args, row_fn, _ = items[check]
            page = _page(con, sql, args, offset, limit, row_fn)
        else:
            keys = {name: [key(r) for r in con.execute(sql, args)] for name, (sql, args, _, key) in items.items()}
            keys["key_dates"] = [f"{r['task_code']} {r['issue']}" + (f" ({r['other']})" if r.get("other") else "")
                                 for r in kd_rows]
    finally:
        con.rollback()
    state = _schedule_state(con, pid)
    if check:
        return {"project": project, "check": check, "result": {k: v for k, v in checks[check].items() if k != "sample"},
                **page, "threshold_source": cfg["source"], "schedule_state": state,
                "note": "Items of the last F9 in P6. Open = not completed, LOE and WBS summary left out."}
    fs = checks["fs_links"]
    res = {"project": project, "open_incomplete_activities": n_open, "relationships": n_links, **ends,
           "leads_negative_lag": checks["leads"]["count"], "positive_lags": checks["lags"]["count"],
           "non_fs_links": fs["denominator"] - fs["count"],
           **{k: {"count": checks[k]["count"], "sample": checks[k]["sample"]}
              for k in ("hard_constraints", "high_float", "negative_float")}, "checks": checks,
           "summary": {k: [c for c, v in checks.items() if v["pass"] is ok] for k, ok in
                       (("failed", False), ("passed", True), ("not_evaluated", None))},
           "threshold_source": cfg["source"]}
    if thresholds:
        res["thresholds_overridden"] = sorted(thresholds)
    if cfg["notes"]:
        res["threshold_notes"] = cfg["notes"]
    res.update(duration_basis=duration_basis,
               baseline=bl and {"project": bl[0], "data_date": bl[1], "dates": baseline_dates,
                                "source": "baseline=" if baseline else "project baseline (PROJECT.sum_base_proj_id)"},
               schedule_state=state,
               note="Values of the last F9 in P6: reschedule first. Open = not completed, LOE and WBS summary left out.")
    if save_snapshot:
        now = datetime.now()
        snap = {"id": f"{re.sub(r'[^A-Za-z0-9_.-]', '_', project)}_{now:%Y%m%d_%H%M%S}_{now.microsecond // 1000:03d}",
                "project": project, "proj_id": pid, "db": os.path.basename(DB_PATH), "ts": _log_ts(),
                "last_schedule_date": state["last_schedule_date"], "data_date": state["data_date"],
                "stale": state["stale"], "params": {"baseline": res["baseline"] and res["baseline"]["project"],
                                                    "baseline_dates": baseline_dates, "duration_basis": duration_basis,
                                                    "thresholds": dict(thresholds or {}), "high_float_days": high_float_days},
                "totals": {"open_incomplete_activities": n_open, "relationships": n_links,
                           "open_successor_links": fs["denominator"], "non_fs_links": res["non_fs_links"]},
                "checks": {k: {f: v.get(f) for f in ("count", "denominator", "pct", "value", "threshold", "pass")}
                           for k, v in checks.items()},
                "items": keys}
        try:
            _hc_snapshot_save(snap)
            res["snapshot"] = {"id": snap["id"]}
        except OSError as e:
            res["snapshot"] = {"error": f"snapshot not saved: {e}"}
    return res


def _hc_pick(project: str, ref: str, snaps: list[dict], name: str) -> dict:
    if ref in ("latest", "previous"):
        k = 1 if ref == "latest" else 2
        if len(snaps) < k:
            raise ValueError(f"{name}={ref!r}: {project} has {len(snaps)} health snapshot(s) of this database; run "
                             "p6_health_check first (each full run saves one)")
        return snaps[-k]
    for s in _hc_snapshots(None, this_db=False):
        if s.get("id") == ref:
            return s
    raise ValueError(f"{name}: no health snapshot {ref!r} (ids: p6_health_check_compare lists them in snapshots)")


@_tool()
def p6_health_check_compare(project: ProjectId, before: str = "previous", after: str = "latest") -> dict:
    """Compare two health check snapshots of a project (each full p6_health_check run saves one):
    before / after = 'latest', 'previous' (the one before latest) or a snapshot id (any database), or
    after='current' to run the health check now (before 'latest' or 'previous' is then the latest snapshot
    saved before that run). totals: open activities, relationships, links with an
    open successor, non-FS links {before, after, delta}. checks: per check count, pct and value
    {before, after, delta}, pass {before, after}, and the items that entered and left it (activity IDs or
    links 'pred -> succ type', first 50 of each, with entered_count / left_count). changed: checks whose
    count moved. warnings: parameters that differ (baseline, thresholds...). snapshots: the project's
    snapshots of this database (id, ts, last_schedule_date)."""
    con = _connect()
    _project_id(con, project)
    con.close()
    if after == "current":  # before 'latest' or 'previous' = the latest snapshot saved before this run
        snap = p6_health_check(project).get("snapshot") or {}
        if "id" not in snap:
            raise ValueError(f"after='current': {snap.get('error', 'the health check snapshot was not saved')}")
        after, before = snap["id"], "previous" if before == "latest" else before
    snaps = _hc_snapshots(project)
    a, b = _hc_pick(project, before, snaps, "before"), _hc_pick(project, after, snaps, "after")
    meta = lambda s: {k: s.get(k) for k in ("id", "ts", "db", "last_schedule_date", "data_date", "stale")}  # noqa: E731
    delta = lambda x, y: None if x is None or y is None else round(y - x, 4)  # noqa: E731
    totals = {k: {"before": a["totals"].get(k), "after": b["totals"].get(k),
                  "delta": delta(a["totals"].get(k), b["totals"].get(k))} for k in HC_TOTALS}
    checks = {}
    for k in dict.fromkeys(list(a["checks"]) + list(b["checks"])):
        ca, cb = a["checks"].get(k, {}), b["checks"].get(k, {})
        ia, ib = a.get("items", {}).get(k), b.get("items", {}).get(k)
        row = {f: {"before": ca.get(f), "after": cb.get(f), "delta": delta(ca.get(f), cb.get(f))}
               for f in ("count", "pct", "value") if ca.get(f) is not None or cb.get(f) is not None}
        row["pass"] = {"before": ca.get("pass"), "after": cb.get("pass")}
        if ia is not None and ib is not None:
            sa, sb = set(ia), set(ib)
            row.update(entered=sorted(sb - sa)[:50], left=sorted(sa - sb)[:50], entered_count=len(sb - sa),
                       left_count=len(sa - sb))
        checks[k] = row
    warn = [f"{k} differs: {a['params'].get(k)!r} -> {b['params'].get(k)!r}" for k in
            dict.fromkeys(list(a.get("params", {})) + list(b.get("params", {})))
            if a.get("params", {}).get(k) != b.get("params", {}).get(k)]
    if a.get("project") != b.get("project"):
        warn.append(f"different projects: {a.get('project')} -> {b.get('project')}")
    if a.get("db") != b.get("db"):
        warn.append(f"different databases: {a.get('db')} -> {b.get('db')}")
    if b.get("stale") or a.get("stale"):
        warn.append("a snapshot was taken while the project was edited since its last F9")
    return {"project": project, "before": meta(a), "after": meta(b), "totals": totals, "checks": checks,
            "changed": [k for k, v in checks.items() if (v.get("count") or {}).get("delta")],
            "warnings": warn, "snapshots": [meta(s) for s in snaps[-20:]]}


DanglingKind = Literal["start", "finish", "both"]


def _dangling_suggest(net: _Net, d: dict, kind: str) -> Optional[dict]:
    """Proposed link (p6_add_relationships item) for an open start or finish: the missing half of a ladder
    pair, else an FS from or to the closest open activity of the same WBS by early dates."""
    me = d["task_code"]
    if kind == "start":  # driven only by FF / SF predecessors
        for lk in sorted(net.by_succ.get(d["task_id"], []), key=lambda x: x["id"]):
            p = net.t[lk["pred"]]
            if lk["type"] == "PR_FF" and not net.summary(p) and p["proj_id"] == net.pid:
                return {"items": [{"predecessor_code": p["task_code"], "successor_code": me, "rel_type": "SS",
                                   "lag_days": _days(lk["lag"], net.lag_hpd(p, d))}],
                        "alternative": [{"predecessor_code": p["task_code"], "successor_code": me, "rel_type": "FS",
                                         "lag_days": 0}],
                        "reason": f"ladder: {p['task_code']} drives its finish (FF): add the matching SS (same lag), "
                                  "or an FS if the work cannot overlap"}
        cands = [x for x in net.t.values() if x["proj_id"] == net.pid and x["wbs_id"] == d["wbs_id"]
                 and x["task_id"] != d["task_id"] and not net.summary(x) and x["ef"] and d["es"] and x["ef"] <= d["es"]
                 and not any(lk["pred"] == x["task_id"] for lk in net.by_succ.get(d["task_id"], []))]
        if cands:
            x = max(cands, key=lambda x: (x["ef"], x["task_code"]))
            return {"items": [{"predecessor_code": x["task_code"], "successor_code": me, "rel_type": "FS", "lag_days": 0}],
                    "reason": f"closest activity of the same WBS finishing before its early start ({x['early_end_date']})"}
        return None
    for lk in sorted(net.by_pred.get(d["task_id"], []), key=lambda x: x["id"]):
        s = net.t[lk["succ"]]
        if lk["type"] == "PR_SS" and not net.summary(s) and s["proj_id"] == net.pid:
            return {"items": [{"predecessor_code": me, "successor_code": s["task_code"], "rel_type": "FF",
                               "lag_days": _days(lk["lag"], net.lag_hpd(d, s))}],
                    "alternative": [{"predecessor_code": me, "successor_code": s["task_code"], "rel_type": "FS",
                                     "lag_days": 0}],
                    "reason": f"ladder: it drives the start of {s['task_code']} (SS): add the matching FF (same lag), "
                              "or an FS if the work cannot overlap"}
    cands = [x for x in net.t.values() if x["proj_id"] == net.pid and x["wbs_id"] == d["wbs_id"]
             and x["task_id"] != d["task_id"] and not net.summary(x) and x["status_code"] != "TK_Complete"
             and x["es"] and d["ef"] and x["es"] >= d["ef"]
             and not any(lk["succ"] == x["task_id"] for lk in net.by_pred.get(d["task_id"], []))]
    if cands:
        x = min(cands, key=lambda x: (x["es"], x["task_code"]))
        return {"items": [{"predecessor_code": me, "successor_code": x["task_code"], "rel_type": "FS", "lag_days": 0}],
                "reason": f"closest activity of the same WBS starting after its early finish ({x['early_start_date']})"}
    return None


@_tool()
def p6_dangling(project: ProjectId, kind: DanglingKind = "both", offset: Offset = 0, limit: Limit = 100) -> dict:
    """Dangling ends, as the dangling block of p6_health_check: open activities (not milestones, not LOE
    or WBS summary) whose start has predecessors but none FS or SS (open start: driven only by FF/SF),
    or whose finish has successors but none FS or FF (open finish: drives only SS/SF); links to
    summary activities do not count. kind: start, finish or both. Row: kind, task_code, name, early
    dates, total_float_d, links (the existing ones on that side: code, type, lag_d), suggestion
    {items (format of p6_add_relationships), alternative, reason} or None: the missing SS or FF of a
    ladder pair (SS + FF with the same lag), else an FS from (to) the closest activity of the same WBS
    finishing before its start (starting after its finish). Proposals only: nothing is written."""
    if kind not in get_args(DanglingKind):
        raise ValueError("kind must be start, finish or both")
    con = _connect()
    pid = _project_id(con, project)
    net = _net_snapshot(con, pid)
    rows = []
    for d in sorted(net.t.values(), key=lambda d: (d["task_code"], d["task_id"])):
        if d["proj_id"] != pid or net.summary(d) or d["status_code"] == "TK_Complete" \
                or d["task_type"] in ("TT_Mile", "TT_FinMile"):
            continue
        for k, mine, other, good in (("start", net.by_succ, "pred", ("PR_FS", "PR_SS")),
                                     ("finish", net.by_pred, "succ", ("PR_FS", "PR_FF"))):
            if kind not in (k, "both"):
                continue
            real = [lk for lk in mine.get(d["task_id"], []) if not net.summary(net.t[lk[other]])]
            if real and not any(lk["type"] in good for lk in real):
                rows.append({"kind": k, **{f: v for f, v in net.act_view(d).items() if f not in ("calendar",)},
                             "links": [{"code": net.t[lk[other]]["task_code"], "type": REL_TYPES_REV.get(lk["type"]),
                                        "lag_d": _days(lk["lag"], net.lag_hpd(net.t[lk["pred"]], net.t[lk["succ"]]))}
                                       for lk in real],
                             "suggestion": _dangling_suggest(net, d, k)})
    rows.sort(key=lambda r: (r["kind"] != "start", r["task_code"]))
    res = _slice(rows, offset, limit)
    res.update(counts={k: sum(r["kind"] == k for r in rows) for k in ("start", "finish") if kind in (k, "both")},
               with_suggestion=sum(r["suggestion"] is not None for r in rows), schedule_state=_schedule_state(con, pid),
               note="Suggestions are proposals: check them, then pass the items to p6_add_relationships (dry run first).")
    return res


# Out-of-sequence progress on a link r (successor t, predecessor p)
OOS_SQL = {"FS": "r.pred_type = 'PR_FS' AND COALESCE(r.lag_hr_cnt, 0) >= 0 AND t.act_start_date IS NOT NULL "
                 "AND p.status_code <> 'TK_Complete'",
           "FS_lead": "r.pred_type = 'PR_FS' AND r.lag_hr_cnt < 0 AND t.act_start_date IS NOT NULL "
                      "AND p.status_code <> 'TK_Complete'",
           "SS": "r.pred_type = 'PR_SS' AND t.act_start_date IS NOT NULL AND p.act_start_date IS NULL",
           "FF": "r.pred_type = 'PR_FF' AND t.status_code = 'TK_Complete' AND p.status_code <> 'TK_Complete'",
           "SF": "r.pred_type = 'PR_SF' AND t.status_code = 'TK_Complete' AND p.act_start_date IS NULL"}


@_tool()
def p6_progress_check(project: ProjectId, sample_size: SampleSize = 15) -> dict:
    """Progress consistency of a project (statused schedule). out_of_sequence: links whose successor
    progressed before its predecessor allows it: FS (lag 0 or more: successor started, predecessor not
    finished), FS_lead (same with a negative lag), SS (successor started, predecessor not), FF
    (successor finished, predecessor not), SF (successor finished, predecessor not started); each with
    links, distinct successors, how many of those have a predecessor not started, and a sample.
    status_checks: active without actual start, completed with remaining duration or no actual
    finish, 100% physical but not completed, not started with actuals or %, actual finish before
    actual start, suspended without resume, actual date after the data date. logic_option: the
    project's out-of-sequence scheduling option (retained logic, progress override), which decides
    what F9 does with these links."""
    if not 0 <= int(sample_size) <= 50:
        raise ValueError("sample_size must be 0 to 50")
    k = int(sample_size)
    con = _connect()
    pid = _project_id(con, project)
    cols, al = _cols(con, "TASK"), functools.partial(_alive, con)
    links = ("FROM TASKPRED r JOIN TASK t ON t.task_id = r.task_id JOIN TASK p ON p.task_id = r.pred_task_id "
             "LEFT JOIN PROJECT pp ON pp.proj_id = p.proj_id WHERE t.proj_id = :pid"
             f" AND t.task_type NOT IN {SUMMARY_TYPES} AND p.task_type NOT IN {SUMMARY_TYPES}"  # no link ends
             + al("TASKPRED", "r") + al("TASK", "t") + al("TASK", "p"))
    acts = "FROM TASK t WHERE t.proj_id = :pid" + al("TASK", "t")
    con.execute("BEGIN")
    try:
        dd = None
        if "last_recalc_date" in _cols(con, "PROJECT"):
            dd = con.execute("SELECT last_recalc_date FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone()[0]
        args = {"pid": pid, "dd": dd}
        oos = {}
        for name, cond in OOS_SQL.items():
            c = con.execute("SELECT COUNT(*), COUNT(DISTINCT t.task_id), SUM(p.act_start_date IS NULL), COUNT(DISTINCT "
                            f"CASE WHEN p.act_start_date IS NULL THEN t.task_id END) {links} AND {cond}", args).fetchone()
            rows = con.execute(f"SELECT p.task_code AS pc, p.proj_id AS ppid, pp.proj_short_name AS pproj, p.status_code AS ps, "
                               f"t.task_code AS sc, t.status_code AS ss {links} AND {cond} ORDER BY t.task_code, p.task_code, "
                               f"r.task_pred_id LIMIT {k}", args)
            oos[name] = {"links": c[0], "successors": c[1],
                         "predecessor_not_started": {"links": c[2] or 0, "successors": c[3]},
                         "sample": [f"{r['pproj'] + ':' if r['ppid'] != pid else ''}{r['pc']} ({r['ps']}) -> {r['sc']} ({r['ss']})"
                                    for r in rows]}
        anyc = " OR ".join(f"({c})" for c in OOS_SQL.values())
        tot = con.execute(f"SELECT COUNT(*), COUNT(DISTINCT t.task_id) {links} AND ({anyc})", args).fetchone()
        st = {"active_without_start": "t.status_code = 'TK_Active' AND t.act_start_date IS NULL",
              "complete_with_remaining_or_no_finish": "t.status_code = 'TK_Complete' AND "
                                                      "(COALESCE(t.remain_drtn_hr_cnt, 0) > 0 OR t.act_end_date IS NULL)",
              "pct_100_not_complete": "t.phys_complete_pct >= 100 AND t.status_code <> 'TK_Complete'",
              "not_started_with_actuals": "t.status_code = 'TK_NotStart' AND (t.act_start_date IS NOT NULL OR "
                                          "t.act_end_date IS NOT NULL OR COALESCE(t.phys_complete_pct, 0) > 0)",
              "finish_before_start": "julianday(t.act_end_date) < julianday(t.act_start_date)"}
        if {"suspend_date", "resume_date"} <= cols:
            st["suspended_without_resume"] = "t.suspend_date IS NOT NULL AND t.resume_date IS NULL"
        if dd:
            st["actual_after_data_date"] = ("julianday(t.act_start_date) > julianday(:dd) OR "
                                            "julianday(t.act_end_date) > julianday(:dd)")
        status = {}
        for name, cond in st.items():
            rows = con.execute(f"SELECT t.task_code, t.task_name, t.status_code {acts} AND ({cond}) "
                               f"ORDER BY t.task_code, t.task_id LIMIT {k}", args)
            status[name] = {"count": con.execute(f"SELECT COUNT(*) {acts} AND ({cond})", args).fetchone()[0],
                            "sample": [f"{r[0]} {r[1]} ({r[2]})" for r in rows]}
        opts, src = _sched_options(con, pid)
    finally:
        con.rollback()
    rl, po = opts.get("sched_retained_logic"), opts.get("sched_progress_override")
    mode = ("retained logic" if rl == "Y" else "progress override" if po == "Y" else "actual dates" if rl == po == "N"
            else "retained logic (P6 default, no option stored)")
    return {"project": project, "data_date": dd, "out_of_sequence": oos,
            "out_of_sequence_total": {"links": tot[0], "successors": tot[1]}, "status_checks": status,
            "logic_option": {"mode": mode, "sched_retained_logic": rl, "sched_progress_override": po, "source": src,
                             "note": "Retained logic keeps the remaining work of an out-of-sequence successor after its "
                                     "predecessors at F9; progress override ignores those links."},
            "schedule_state": _schedule_state(con, pid),
            "note": "Statuses and dates as stored (last update and F9). Predecessors in other projects are shown as "
                    "PROJECT:ACTIVITY."}


# ----------------------------------------------------------------- network analysis (driving, float origin, redundant links)
# A link's gap is the working time from the predecessor's date plus lag to the successor's date, by
# type: FS finish -> start, SS start -> start, FF finish -> finish, SF start -> finish. The lag is laid
# on the calendar of the project's lag rule. Early dates (driving): the gap is counted on the successor
# calendar; late dates (float origin): on the predecessor calendar. The dates are the stored F9 dates
# of every status: P6 also places completed activities (retained logic). With the schedule option
# sched_lag_early_start_flag 'Y' (lag from the early start), the part of a positive lag that elapsed
# between the predecessor's actual start (SS, SF) or actual finish (FS, FF) and the data date is
# dropped. Oracle: every open activity of the reference project is explained on both passes (2026-10-19).

GAP_TOL_H = 5 / 60  # a gap within 5 minutes counts as 0 (driving, controlling)
LINK_FROM = {"PR_FS": "f", "PR_SS": "s", "PR_FF": "f", "PR_SF": "s"}  # predecessor date of each type
LINK_TO = {"PR_FS": "s", "PR_SS": "s", "PR_FF": "f", "PR_SF": "f"}  # successor date
LATE_CSTR = {"CS_MSO", "CS_MSOB", "CS_MANDSTART", "CS_MEO", "CS_MEOB", "CS_MANDFIN"}  # bound the late dates
EARLY_CSTR = {"CS_MSO", "CS_MSOA", "CS_MANDSTART", "CS_MEO", "CS_MEOA", "CS_MANDFIN"}  # bound the early dates


class _Net:
    """The links of one project (successor or predecessor in it) with their two activities, from one
    read snapshot: stored F9 dates, calendars, lag rule and data date."""

    def __init__(self, con, pid: int):
        pcols, tcols = _cols(con, "PROJECT"), _cols(con, "TASK")
        self.pid = pid
        self.prj = con.execute("SELECT proj_short_name, " + ", ".join(c if c in pcols else f"NULL AS {c}" for c in (
            "last_recalc_date", "plan_end_date", "scd_end_date", "clndr_id")) + " FROM PROJECT WHERE proj_id = ?",
            (pid,)).fetchone()
        self.dd = _db_dt(self.prj["last_recalc_date"])
        self.must = _db_dt(self.prj["plan_end_date"])
        self.end = _db_dt(self.prj["scd_end_date"])
        self.rule = _lag_rule(con, pid)[0]
        opts = _sched_options(con, pid)[0]
        self.lag_from_early = opts.get("sched_lag_early_start_flag", "Y") != "N"
        self.cals = _calendars(con)
        self.pcal = self.cals.get(self.prj["clndr_id"])
        al = functools.partial(_alive, con)
        links = con.execute("SELECT r.task_pred_id, r.task_id, r.pred_task_id, r.pred_type, COALESCE(r.lag_hr_cnt, 0) AS lag "
                            "FROM TASKPRED r JOIN TASK s ON s.task_id = r.task_id JOIN TASK p ON p.task_id = r.pred_task_id "
                            "WHERE (s.proj_id = ? OR p.proj_id = ?)" + al("TASKPRED", "r") + al("TASK", "s") + al("TASK", "p")
                            + " ORDER BY r.task_pred_id", (pid, pid)).fetchall()
        self.links = [dict(id=r[0], succ=r[1], pred=r[2], type=r[3], lag=float(r[4])) for r in links]
        ids = {i for lk in self.links for i in (lk["succ"], lk["pred"])}
        sel = ", ".join(f"t.{c}" if c in tcols else f"NULL AS {c}" for c in (
            "task_id", "task_code", "task_name", "proj_id", "wbs_id", "task_type", "status_code", "clndr_id", "early_start_date",
            "early_end_date", "late_start_date", "late_end_date", "act_start_date", "act_end_date", "total_float_hr_cnt",
            "free_float_hr_cnt", "remain_drtn_hr_cnt", "cstr_type", "cstr_date", "cstr_type2", "cstr_date2"))
        q = (f"SELECT {sel}, {HPD_SQL} AS hpd, p.proj_short_name AS project, c.clndr_name AS calendar FROM TASK t "
             "JOIN PROJECT p ON p.proj_id = t.proj_id LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id WHERE ")
        rows = con.execute(q + "t.proj_id = ?" + al("TASK", "t"), (pid,)).fetchall()
        other = sorted(ids - {r["task_id"] for r in rows})
        for i in range(0, len(other), 500):
            chunk = other[i:i + 500]
            rows += con.execute(q + f"t.task_id IN ({','.join('?' * len(chunk))})" + al("TASK", "t"), chunk).fetchall()
        self.t: dict[int, dict] = {}
        for r in rows:
            d = _dict(r)
            for k, c in (("es", "early_start_date"), ("ef", "early_end_date"), ("ls", "late_start_date"),
                         ("lf", "late_end_date"), ("as_", "act_start_date"), ("af", "act_end_date"),
                         ("cd", "cstr_date"), ("cd2", "cstr_date2")):
                d[k] = _db_dt(d[c])
            d["cal"] = self.cals.get(d["clndr_id"])
            self.t[d["task_id"]] = d
        self.by_succ: dict[int, list] = {}
        self.by_pred: dict[int, list] = {}
        for lk in self.links:
            self.by_succ.setdefault(lk["succ"], []).append(lk)
            self.by_pred.setdefault(lk["pred"], []).append(lk)
        self.code = {d["task_code"]: i for i, d in self.t.items() if d["proj_id"] == pid}

    def task(self, code: str) -> dict:
        if code not in self.code:
            raise ValueError(f"Activity {code} not found in project {self.prj['proj_short_name']}")
        return self.t[self.code[code]]

    @staticmethod
    def summary(d: dict) -> bool:
        return d["task_type"] in ("TT_LOE", "TT_WBS")

    def lag_cal(self, p: dict, s: dict) -> Optional[_Cal]:
        """Calendar of the lag (None: 24-hour days)."""
        return _lag_cal_of(self.rule, p["cal"], s["cal"], self.pcal)

    def lag_hpd(self, p: dict, s: dict) -> float:
        return _lag_hpd_of(self.rule, p["hpd"], s["hpd"], self.pcal)

    def gap(self, lk: dict, late: bool = False) -> Optional[float]:
        """Gap of a link in hours on early (late=False) or late dates; None when a date or calendar is missing."""
        p, s = self.t.get(lk["pred"]), self.t.get(lk["succ"])
        if p is None or s is None:
            return None
        a = p[("l" if late else "e") + LINK_FROM[lk["type"]]]
        b = s[("l" if late else "e") + LINK_TO[lk["type"]]]
        cal, lc = (p["cal"] if late else s["cal"]), self.lag_cal(p, s)
        if a is None or b is None or cal is None or cal.error or (lc is not None and lc.error):
            return None
        lag = lk["lag"]
        if self.lag_from_early:
            lag = _lag_left(lag, p["as_"] if LINK_FROM[lk["type"]] == "s" else p["af"], self.dd, lc)
        if lc is None:
            anchor = a + timedelta(hours=lag)
        else:
            anchor = lc.add_work_hours(a, lag) if lag >= 0 else lc.subtract_work_hours(a, -lag)
        return cal.work_hours(anchor, b)

    def zero(self, cal: Optional[_Cal], a: Optional[datetime], b: Optional[datetime]) -> bool:
        """a and b have no working time between them (5 minutes tolerance)."""
        if a is None or b is None:
            return False
        if cal is None or cal.error:
            return abs((b - a).total_seconds()) <= GAP_TOL_H * 3600
        return abs(cal.work_hours(a, b)) <= GAP_TOL_H

    def link_view(self, lk: dict, gap: Optional[float], other: str) -> dict:
        p, s = self.t[lk["pred"]], self.t[lk["succ"]]
        o = self.t[lk[other]]
        hpd = (s if other == "pred" else p)["hpd"] or 8.0
        return {("predecessor" if other == "pred" else "successor"): o["task_code"],
                **({"other_project": o["project"]} if o["proj_id"] != self.pid else {}),
                "task_pred_id": lk["id"], "type": REL_TYPES_REV.get(lk["type"], lk["type"]),
                "lag_d": _days(lk["lag"], self.lag_hpd(p, s)), "gap_h": None if gap is None else round(gap, 2),
                "gap_d": None if gap is None else _days(gap, hpd)}

    def act_view(self, d: dict, late: bool = False) -> dict:
        out = {"task_code": d["task_code"], "task_name": d["task_name"], "task_type": d["task_type"],
               "status_code": d["status_code"]}
        if d["proj_id"] != self.pid:
            out["project"] = d["project"]
        out.update({"early_start": d["early_start_date"], "early_finish": d["early_end_date"]} if not late else
                   {"late_start": d["late_start_date"], "late_finish": d["late_end_date"]})
        out.update(total_float_d=_days(d["total_float_hr_cnt"], d["hpd"]), calendar=d["calendar"])
        if d["cstr_type"]:
            out.update(cstr_type=d["cstr_type"], cstr_label=_cstr_label(d["cstr_type"]), cstr_date=d["cstr_date"])
        return out

    def cstr_hits(self, d: dict, kinds: set, late: bool) -> list[dict]:
        """Constraints of d (primary, secondary) of kinds whose date matches the bounded date of d."""
        out = []
        for ty, dt in ((d["cstr_type"], d["cd"]), (d["cstr_type2"], d["cd2"])):
            if ty in kinds and dt is not None:
                own = d[("l" if late else "e") + ("f" if ty in CSTR_FINISH else "s")]
                if self.zero(d["cal"], dt, own):
                    out.append({"cstr_type": ty, "cstr_label": _cstr_label(ty), "cstr_date": _fmt_dt(dt)})
        return out


def _net_snapshot(con, pid: int) -> _Net:
    own = not con.in_transaction
    if own:
        con.execute("BEGIN")
    try:
        return _Net(con, pid)
    finally:
        if own:
            con.rollback()


def _root_driver(net: _Net, d: dict, hidden: list) -> dict:
    """What starts the driving chain at d (no listed driving predecessor)."""
    base = {"task_code": d["task_code"], "date": d["early_start_date"]}
    cs = net.cstr_hits(d, EARLY_CSTR, late=False)
    if cs:
        return {**base, "type": "constraint", **cs[0]}
    if net.dd and d["es"] and d["cal"] and not d["cal"].error and d["cal"].work_hours(net.dd, d["es"]) <= GAP_TOL_H:
        return {**base, "type": "data_date", "data_date": _fmt_dt(net.dd)}
    if hidden:
        return {**base, "type": "completed_predecessor", "predecessors": hidden[:10],
                "detail": "driven by completed activities (pass include_completed=True to follow them)"}
    if d["as_"] is not None and d["status_code"] != "TK_NotStart":
        return {**base, "type": "actual_date", "actual_start": d["act_start_date"],
                "detail": "in progress: remaining work placed by P6 from its progress"}
    if not net.by_succ.get(d["task_id"]):
        return {**base, "type": "open_start", "detail": "no predecessor"}
    gaps = [g for g in (net.gap(lk) for lk in net.by_succ[d["task_id"]]) if g is not None]
    return {**base, "type": "unexplained", "min_gap_h": round(min(gaps), 2) if gaps else None,
            "detail": "no predecessor link with a zero gap, no constraint or data date at its start (leveling, "
                      "expected finish or an option this server does not model)"}


@_tool()
def p6_driving_path(project: ProjectId, target: ActivityId, max_depth: Annotated[int, Field(ge=1)] = 500,
                    include_completed: bool = False, offset: Offset = 0, limit: Limit = 100) -> dict:
    """Driving predecessors of an activity, traced backward on the early dates of the last F9: a link
    drives when its gap is 0 (within 5 minutes): working time from the predecessor's date plus lag
    (lag calendar rule) to the successor's date, by type (FS finish to start, SS start to start, FF
    finish to finish, SF start to finish), on the successor calendar. Every driving predecessor is
    followed (several can drive), down to max_depth levels. Page of rows, one per activity, by level (0 =
    target): task_code, name, early dates, total_float_d, calendar, constraint, and the link it drives
    through (successor, type, lag_d, gap_h, gap_d; also_drives for other path activities it drives).
    drivers_at_root: what starts each chain (data_date, constraint, actual_date, open_start,
    completed_predecessor, unexplained). Completed activities are left out unless include_completed
    (P6 places them too, retained logic). Read only; values of the last F9."""
    max_depth = int(max_depth)
    if max_depth < 1:
        raise ValueError("max_depth must be 1 or more")
    con = _connect()
    pid = _project_id(con, project)
    net = _net_snapshot(con, pid)
    tgt = net.task(target)
    level = {tgt["task_id"]: 0}
    edges: dict[int, list] = {}  # predecessor -> [(link, gap)] into path activities
    roots, order, i, cut = [], [tgt["task_id"]], 0, False
    while i < len(order):
        n = order[i]
        i += 1
        drv, hidden = [], []
        for lk in net.by_succ.get(n, []):
            g = net.gap(lk)
            if g is None or abs(g) > GAP_TOL_H:
                continue
            p = net.t[lk["pred"]]
            if p["status_code"] == "TK_Complete" and not include_completed:
                hidden.append(p["task_code"])
                continue
            drv.append((lk, g))
        if not drv:
            roots.append(_root_driver(net, net.t[n], hidden))
            continue
        for lk, g in drv:
            edges.setdefault(lk["pred"], []).append((lk, g))
            if lk["pred"] not in level:
                if level[n] + 1 > max_depth:
                    cut = True
                    continue
                level[lk["pred"]] = level[n] + 1
                order.append(lk["pred"])
    rows = []
    for tid, lv in level.items():
        d = net.t[tid]
        row = {"level": lv, **net.act_view(d)}
        es = sorted((e for e in edges.get(tid, []) if e[0]["succ"] in level),
                    key=lambda e: (level[e[0]["succ"]], net.t[e[0]["succ"]]["task_code"]))
        if es:
            row.update(net.link_view(es[0][0], es[0][1], "succ"))
            if len(es) > 1:
                row["also_drives"] = [net.link_view(lk, g, "succ") for lk, g in es[1:]]
        rows.append(row)
    rows.sort(key=lambda r: (r["level"], r["early_finish"] or "", r["task_code"]))
    res = _slice(rows, offset, limit)
    res.update(target=net.act_view(tgt), drivers_at_root=roots, data_date=net.prj["last_recalc_date"],
               lag_rule=_lag_info(con, pid), tolerance_h=round(GAP_TOL_H, 4), schedule_state=_schedule_state(con, pid))
    if cut:
        res["note"] = f"Stopped at max_depth {max_depth}: earlier drivers not listed."
    return res


def _late_origin(net: _Net, d: dict) -> list[dict]:
    """Origins of the late dates of d when no successor link controls them."""
    out = [{"type": "activity_constraint", "task_code": d["task_code"], **c} for c in net.cstr_hits(d, LATE_CSTR, late=True)]
    if out:
        return out
    if net.must and net.zero(d["cal"], d["lf"], net.must):
        return [{"type": "project_must_finish", "task_code": d["task_code"], "date": net.prj["plan_end_date"],
                 "detail": "PROJECT.plan_end_date (Must Finish By)"}]
    if net.end and net.zero(d["cal"], d["lf"], net.end) or net.zero(d["cal"], d["lf"], d["ef"]):
        return [{"type": "open_end", "task_code": d["task_code"], "date": d["late_end_date"],
                 "detail": "no controlling successor: late finish at the project finish (or its own early finish)"}]
    return [{"type": "unexplained", "task_code": d["task_code"], "date": d["late_end_date"],
             "detail": "no successor link with a zero late gap, no constraint, must finish or project finish date"}]


@_tool()
def p6_float_origin(project: ProjectId, task_code: ActivityId, max_depth: Annotated[int, Field(ge=1)] = 500,
                    offset: Offset = 0, limit: Limit = 100) -> dict:
    """Where the late dates (and so the total float) of an activity come from, on the last F9: walks
    forward along the successor links that set the late date (late gap 0 within 5 minutes: predecessor
    late date plus lag to the successor late date, by type, on the predecessor calendar), following
    every controlling successor, until the source. origin: type activity_constraint (a constraint
    bounding late dates: start/finish on, on or before, mandatory), project_must_finish
    (PROJECT.plan_end_date), open_end (no controlling successor: project finish), unexplained, or mixed
    (several sources, all in sources), or none (max_depth reached before any source), with date,
    activity and constraint; explanation says it in
    words. own_constraint: the activity's own constraint and whether it controls. Page of chain rows by
    level (0 = the activity): late dates, total_float_d, and the controlled_by link (successor, type,
    lag_d, gap). Read only."""
    max_depth = int(max_depth)
    if max_depth < 1:
        raise ValueError("max_depth must be 1 or more")
    con = _connect()
    pid = _project_id(con, project)
    net = _net_snapshot(con, pid)
    d0 = net.task(task_code)
    if d0["status_code"] == "TK_Complete" or d0["lf"] is None:
        raise ValueError(f"{task_code} is completed or has no late dates: no float to explain")
    level, via, order, i, sources, cut = {d0["task_id"]: 0}, {}, [d0["task_id"]], 0, [], False
    while i < len(order):
        n = order[i]
        i += 1
        d = net.t[n]
        ctl = [(lk, g) for lk in net.by_pred.get(n, []) for g in (net.gap(lk, late=True),)
               if g is not None and abs(g) <= GAP_TOL_H]  # completed successors too: P6 places them (retained logic)
        own = [{"type": "activity_constraint", "task_code": d["task_code"], **c} for c in net.cstr_hits(d, LATE_CSTR, True)]
        sources += own
        if not ctl and not own:
            sources += _late_origin(net, d)
        for lk, g in ctl:
            via.setdefault(n, []).append((lk, g))
            if lk["succ"] not in level:
                if level[n] + 1 > max_depth:
                    cut = True
                    continue
                level[lk["succ"]] = level[n] + 1
                order.append(lk["succ"])
    rows = []
    for tid, lv in level.items():
        row = {"level": lv, **net.act_view(net.t[tid], late=True)}
        vs = sorted(via.get(tid, []), key=lambda e: (level.get(e[0]["succ"], 0), net.t[e[0]["succ"]]["task_code"]))
        if vs:
            row["controlled_by"] = [net.link_view(lk, g, "succ") for lk, g in vs]
        rows.append(row)
    rows.sort(key=lambda r: (r["level"], r["task_code"]))
    kinds = sorted({s["type"] for s in sources})
    origin = {"type": kinds[0] if len(kinds) == 1 else "mixed" if kinds else "none", "sources": sources}
    if len(sources) == 1:
        origin.update({k: v for k, v in sources[0].items() if k != "type"})
    own = [{"cstr_type": ty, "cstr_label": _cstr_label(ty), "cstr_date": dt,
            "controls": any(s["type"] == "activity_constraint" and s["task_code"] == task_code and s["cstr_type"] == ty
                            for s in sources)}
           for ty, dt in ((d0["cstr_type"], d0["cstr_date"]), (d0["cstr_type2"], d0["cstr_date2"])) if ty]
    path = [d0["task_code"]]
    n = d0["task_id"]
    while via.get(n):
        lk, _ = sorted(via[n], key=lambda e: net.t[e[0]["succ"]]["task_code"])[0]
        nxt = net.t[lk["succ"]]
        path.append(f"{REL_TYPES_REV.get(lk['type'])} {_days(lk['lag'], net.lag_hpd(net.t[n], nxt)):g} d -> {nxt['task_code']}")
        n = lk["succ"]
        if len(path) > 12:
            path.append("...")
            break
    words = {"activity_constraint": "a constraint", "project_must_finish": "the project must finish date "
             + str(net.prj["plan_end_date"]), "open_end": "the project finish (open end)", "unexplained": "an unexplained source"}
    expl = (f"Total float {_days(d0['total_float_hr_cnt'], d0['hpd'])} d of {task_code} comes from "
            + (" and ".join(sorted({words[s['type']] + (f" ({s['cstr_label']} {s['cstr_date']} on {s['task_code']})"
                                                         if s['type'] == 'activity_constraint' else '') for s in sources}))
               or "nothing found") + ", through " + " ".join(path) + ".")
    for c in own:
        expl += (f" Its own constraint {c['cstr_label']} {c['cstr_date']} "
                 + ("controls its late dates." if c["controls"] else "does NOT control its late dates."))
    gaps = sorted(((lk, net.gap(lk, late=True)) for lk in net.by_pred.get(d0["task_id"], [])),
                  key=lambda e: (e[1] is None, e[1] or 0))
    res = _slice(rows, offset, limit)
    res.update(activity=net.act_view(d0, late=True), origin=origin, own_constraint=own, explanation=expl,
               successor_gaps=[net.link_view(lk, g, "succ") for lk, g in gaps[:10]],
               must_finish=net.prj["plan_end_date"], project_finish=net.prj["scd_end_date"],
               lag_rule=_lag_info(con, pid), tolerance_h=round(GAP_TOL_H, 4), schedule_state=_schedule_state(con, pid))
    if cut:
        res["note"] = f"Stopped at max_depth {max_depth}: the source may lie further."
    return res


RedundantCategory = Literal["completed_predecessor", "marked_deleted", "transitive", "duplicate"]
REDUNDANT_CATEGORIES = list(get_args(RedundantCategory))
TRANSITIVE_DEPTH = 10  # links per FS chain explored for transitive links


def _transitive(net: _Net) -> dict[int, str]:
    """FS links A -> C of the project (open successor, no summary end) implied by an FS chain A -> ... -> C
    of 2 to TRANSITIVE_DEPTH links through non-summary activities whose lags add up to at least the
    direct lag (hours). Search pruned at activities starting after the latest direct successor."""
    out: dict[int, str] = {}
    fs = {}
    for lk in net.links:
        p, s = net.t.get(lk["pred"]), net.t.get(lk["succ"])
        if lk["type"] == "PR_FS" and p and s and not net.summary(p) and not net.summary(s):
            fs.setdefault(lk["pred"], []).append(lk)
    for a, direct in fs.items():
        tg = {}
        for lk in direct:
            s = net.t[lk["succ"]]
            if s["proj_id"] == net.pid and s["status_code"] != "TK_Complete":
                tg.setdefault(lk["succ"], []).append(lk)
        if not tg:
            continue
        horizon = max((net.t[c]["es"] for c in tg if net.t[c]["es"]), default=None)
        best = {a: (0.0, None)}  # node -> (max lag sum of a path from a, (prev, link))
        best2: dict[int, tuple] = {}  # node -> same over paths of 2 links or more
        frontier = {a}
        for _ in range(TRANSITIVE_DEPTH):
            nxt = set()
            for n in frontier:
                for lk in fs.get(n, []):
                    c = lk["succ"]
                    cs = net.t[c]
                    if c == a or (horizon and cs["es"] and cs["es"] > horizon):
                        continue
                    v = best[n][0] + lk["lag"]
                    if n != a and (c not in best2 or v > best2[c][0] + 1e-9):
                        best2[c] = (v, (n, lk))
                    if c not in best or v > best[c][0] + 1e-9:
                        best[c] = (v, (n, lk))
                        nxt.add(c)
            frontier = nxt
            if not frontier:
                break
        for c, lks in tg.items():
            if c not in best2:
                continue
            v, (n, last) = best2[c]
            chain, guard = [last], 0
            while n != a and guard < TRANSITIVE_DEPTH + 2:
                n, lk = best[n][1]
                chain.insert(0, lk)
                guard += 1
            codes = [net.t[a]["task_code"]] + [net.t[x["succ"]]["task_code"] for x in chain]
            for lk in lks:
                if v + GAP_TOL_H >= lk["lag"]:
                    out[lk["id"]] = f"implied by the FS chain {' -> '.join(codes)} (lags {v:g} h >= {lk['lag']:g} h)"
    return out


@_tool()
def p6_redundant_links(project: ProjectId, categories: Optional[list[RedundantCategory]] = None,
                       name_pattern: str = "(deleted)", offset: Offset = 0, limit: Limit = 100) -> dict:
    """Relationships of the project (successor in it) that are candidates for removal, one row per link
    with every category it falls in: completed_predecessor (predecessor completed, successor open),
    marked_deleted (either activity name contains name_pattern, case ignored), transitive (an FS link
    implied by a chain of 2 to 10 FS links whose lags add up to at least its own, through non-summary
    activities), duplicate (same pair and type linked twice: every copy but the first; SS + FF pairs are
    different types, never flagged). Row: task_pred_id (feed p6_delete_relationships ids=), predecessor,
    successor, type, lag_d, categories, detail, drives (its early gap is 0: removing it moves dates;
    P6 also places completed activities), sole_predecessor / sole_successor (removing it alone leaves
    the open successor without predecessor, or the predecessor without successor). side_effects: open
    activities left without any predecessor or successor if every listed link (all pages) were
    removed. counts per category. Read only."""
    cats = REDUNDANT_CATEGORIES if categories is None else list(dict.fromkeys(
        [categories] if isinstance(categories, str) else categories))
    bad = [c for c in cats if c not in REDUNDANT_CATEGORIES]
    if bad or not cats:
        raise ValueError(f"categories: some of {REDUNDANT_CATEGORIES}")
    if "marked_deleted" in cats and not (name_pattern or "").strip():
        raise ValueError("name_pattern must not be empty")
    con = _connect()
    pid = _project_id(con, project)
    net = _net_snapshot(con, pid)
    own = [lk for lk in net.links if net.t[lk["succ"]]["proj_id"] == pid]
    hit: dict[int, dict[str, str]] = {}
    if "completed_predecessor" in cats:
        for lk in own:
            p, s = net.t[lk["pred"]], net.t[lk["succ"]]
            if p["status_code"] == "TK_Complete" and s["status_code"] != "TK_Complete":
                hit.setdefault(lk["id"], {})["completed_predecessor"] = f"{p['task_code']} finished {p['act_end_date']}"
    if "marked_deleted" in cats:
        pat = name_pattern.lower()
        for lk in own:
            m = [net.t[lk[k]]["task_code"] for k in ("pred", "succ") if pat in (net.t[lk[k]]["task_name"] or "").lower()]
            if m:
                hit.setdefault(lk["id"], {})["marked_deleted"] = f"name of {' and '.join(m)} contains {name_pattern!r}"
    if "transitive" in cats:
        for i, txt in _transitive(net).items():
            hit.setdefault(i, {})["transitive"] = txt
    if "duplicate" in cats:
        first: dict[tuple, int] = {}
        for lk in own:
            k = (lk["succ"], lk["pred"], lk["type"])
            if k in first:
                hit.setdefault(lk["id"], {})["duplicate"] = f"same pair and type as task_pred_id {first[k]}"
            else:
                first[k] = lk["id"]
    byid = {lk["id"]: lk for lk in own}
    real = lambda lks, end: [x for x in lks if not net.summary(net.t[x[end]])]  # noqa: E731
    rows = []
    for i, cs in hit.items():
        lk = byid[i]
        p, s = net.t[lk["pred"]], net.t[lk["succ"]]
        g = net.gap(lk)
        open_s = s["status_code"] != "TK_Complete" and not net.summary(s)
        open_p = p["status_code"] != "TK_Complete" and not net.summary(p)
        rows.append({"task_pred_id": i, "predecessor": p["task_code"], "successor": s["task_code"],
                     **({"pred_project": p["project"]} if p["proj_id"] != pid else {}),
                     "type": REL_TYPES_REV.get(lk["type"], lk["type"]), "lag_d": _days(lk["lag"], net.lag_hpd(p, s)),
                     "categories": [c for c in REDUNDANT_CATEGORIES if c in cs], "detail": cs,
                     "drives": g is not None and abs(g) <= GAP_TOL_H,
                     "sole_predecessor": open_s and not net.summary(p) and len(real(net.by_succ.get(lk["succ"], []), "pred")) == 1,
                     "sole_successor": open_p and not net.summary(s) and len(real(net.by_pred.get(lk["pred"], []), "succ")) == 1})
    rank = {c: k for k, c in enumerate(REDUNDANT_CATEGORIES)}
    rows.sort(key=lambda r: (rank[r["categories"][0]], r["successor"], r["predecessor"], r["task_pred_id"]))
    gone = set(hit)
    side: dict[str, Any] = {"no_predecessor": [], "no_successor": []}
    for key, ids, mine, other in (("no_predecessor", {byid[i]["succ"] for i in gone}, net.by_succ, "pred"),
                                  ("no_successor", {byid[i]["pred"] for i in gone}, net.by_pred, "succ")):
        for t in sorted(ids, key=lambda x: net.t[x]["task_code"]):
            d = net.t[t]
            if d["status_code"] == "TK_Complete" or net.summary(d):
                continue
            if not [x for x in mine.get(t, []) if x["id"] not in gone and not net.summary(net.t[x[other]])]:
                side[key].append({"project": d["project"], "task_code": d["task_code"], "task_name": d["task_name"],
                                  "task_type": d["task_type"]})
    side["count"] = len(side["no_predecessor"]) + len(side["no_successor"])
    side["note"] = "if every listed link (all pages) were removed; LOE and WBS summary links do not count"
    res = _slice(rows, offset, limit)
    res.update(counts={c: sum(c in r["categories"] for r in rows) for c in cats}, categories=cats,
               side_effects=side, schedule_state=_schedule_state(con, pid),
               note="Candidates only: check drives and side_effects, then pass the task_pred_id values to "
                    "p6_delete_relationships(ids=...).")
    return res


# ----------------------------------------------------------------- CPM engine (read-only what-if)
# Forward and backward pass of one project in memory, from the data as stored (nothing is written).
# Rules measured on the reference project against P6's F9 (2026-10-19: every open activity identical on ES, EF, LS,
# LF and total float; every activity identical on early dates):
#   * every status is placed (retained logic): a completed activity sits at the latest of the data date
#     and its predecessors' dates; a link between two completed activities carries no lag forward.
#   * a positive lag from a started (SS, SF) or finished (FS, FF) predecessor loses the working time
#     elapsed between that actual date and the data date (option sched_lag_early_start_flag 'Y').
#   * an early start is the next work start of the activity calendar; a late finish the previous work
#     end; durations and lags on the calendars (lag: the project's lag rule).
#   * open ends finish late at the must finish date when sched_use_project_end_date_for_float is 'Y'
#     and one is set, else at the project's early finish; total float per sched_float_type (FT_FF:
#     late finish - early finish, working hours of the activity calendar).
# Approximations (named in p6_cpm_validate causes): LOE and WBS summary activities span their links /
# WBS; As Late As Possible takes its late dates; expected finish, progress override, mandatory
# constraints as P6 documents them; resource leveling and suspend / resume are not modelled; links to
# other projects use the other activity's stored dates.

CPM_TOL = timedelta(minutes=1)  # dates equal within a minute
CPM_TF_TOL_H = 0.05  # total float equal within 3 minutes
CPM_TRUST = 0.99  # match rate on open activities needed to trust what-if results and the critical path test
START_CSTR = {"CS_MSOA", "CS_MSO", "CS_MANDSTART"}  # bound the early start
FIN_CSTR = {"CS_MEOA", "CS_MEO", "CS_MANDFIN"}  # bound the early finish
LATE_START_CSTR = {"CS_MSOB", "CS_MSO", "CS_MANDSTART"}
LATE_FIN_CSTR = {"CS_MEOB", "CS_MEO", "CS_MANDFIN"}


class _Cpm:
    """Network of one project for the CPM engine: every activity, links inside the project (links to
    other projects kept with the other activity's stored dates), calendars and schedule options."""

    def __init__(self, con, pid: int):
        tcols, pcols = _cols(con, "TASK"), _cols(con, "PROJECT")
        self.pid = pid
        self.prj = con.execute("SELECT proj_short_name, " + ", ".join(c if c in pcols else f"NULL AS {c}" for c in (
            "last_recalc_date", "plan_end_date", "scd_end_date", "clndr_id", "critical_drtn_hr_cnt")) +
            " FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone()
        self.dd = _db_dt(self.prj["last_recalc_date"])
        self.must = _db_dt(self.prj["plan_end_date"])
        self.opts = _sched_options(con, pid)[0]
        self.rule = _lag_rule(con, pid)[0]
        self.cals = _calendars(con)
        self.pcal = self.cals.get(self.prj["clndr_id"])
        cols = ("task_id", "task_code", "task_name", "proj_id", "wbs_id", "task_type", "status_code", "clndr_id",
                "early_start_date", "early_end_date", "late_start_date", "late_end_date", "act_start_date", "act_end_date",
                "total_float_hr_cnt", "remain_drtn_hr_cnt", "cstr_type", "cstr_date", "cstr_type2", "cstr_date2",
                "expect_end_date", "suspend_date", "resume_date")
        sel = ", ".join(f"t.{c}" if c in tcols else f"NULL AS {c}" for c in cols)
        al = functools.partial(_alive, con)
        self.t: dict[int, dict] = {}
        for r in con.execute(f"SELECT {sel}, {HPD_SQL} AS hpd FROM TASK t LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id "
                             "WHERE t.proj_id = ?" + al("TASK", "t"), (pid,)):
            d = _dict(r)
            for k in ("early_start_date", "early_end_date", "late_start_date", "late_end_date", "act_start_date",
                      "act_end_date", "cstr_date", "cstr_date2", "expect_end_date", "suspend_date", "resume_date"):
                d[k] = _db_dt(d[k])
            d["cal"] = self.cals.get(d["clndr_id"])
            d["rem"] = float(d["remain_drtn_hr_cnt"] or 0)
            self.t[d["task_id"]] = d
        self.code = {d["task_code"]: i for i, d in self.t.items()}
        self.links: list[dict] = []
        self.ext_pred: list[dict] = []  # links from other projects: (other's stored early date, link)
        self.ext_succ: list[dict] = []
        for r in con.execute(
                "SELECT r.task_pred_id, r.task_id, r.pred_task_id, r.pred_type, COALESCE(r.lag_hr_cnt, 0), s.proj_id, p.proj_id, "
                "p.early_start_date, p.early_end_date, s.late_start_date, s.late_end_date, "
                f"COALESCE(NULLIF(cp.day_hr_cnt, 0), 8), p.clndr_id, s.clndr_id FROM TASKPRED r JOIN TASK s ON s.task_id = r.task_id "
                "JOIN TASK p ON p.task_id = r.pred_task_id LEFT JOIN CALENDAR cp ON cp.clndr_id = p.clndr_id "
                "WHERE (s.proj_id = ? OR p.proj_id = ?)" + al("TASKPRED", "r") + al("TASK", "s") + al("TASK", "p"), (pid, pid)):
            lk = {"id": r[0], "succ": r[1], "pred": r[2], "type": r[3], "lag": float(r[4])}
            if r[5] == pid and r[6] == pid:
                self.links.append(lk)
            elif r[5] == pid:
                self.ext_pred.append({**lk, "es": _db_dt(r[7]), "ef": _db_dt(r[8]), "cal": self.cals.get(r[12])})
            else:
                self.ext_succ.append({**lk, "ls": _db_dt(r[9]), "lf": _db_dt(r[10]), "cal": self.cals.get(r[13])})

    def copy(self) -> "_Cpm":
        other = object.__new__(_Cpm)
        other.__dict__.update(self.__dict__)
        other.t = {i: dict(d) for i, d in self.t.items()}
        other.links = [dict(lk) for lk in self.links]
        return other

    # ------------------------------------------------------------------ helpers
    def lag_cal(self, p_cal, s_cal) -> Optional[_Cal]:
        return _lag_cal_of(self.rule, p_cal, s_cal, self.pcal)

    def lag_hpd(self, p: dict, s: dict) -> float:
        return _lag_hpd_of(self.rule, p["hpd"], s["hpd"], self.pcal)

    @staticmethod
    def shift(cal: Optional[_Cal], dt: datetime, h: float) -> datetime:
        if cal is None or cal.error:
            return dt + timedelta(hours=h)
        return cal.add_work_hours(dt, h) if h >= 0 else cal.subtract_work_hours(dt, -h)

    def eff_lag(self, lk: dict, p: dict, lc: Optional[_Cal]) -> float:
        if self.opts.get("sched_lag_early_start_flag", "Y") == "N":
            return lk["lag"]
        ref = p.get("act_start_date") if LINK_FROM[lk["type"]] == "s" else p.get("act_end_date")
        return _lag_left(lk["lag"], ref, self.dd, lc)

    def duration_fixed(self, d: dict) -> Optional[str]:
        """Why a change of d's remaining duration cannot move its finish (None when it can)."""
        if d["status_code"] == "TK_Complete":
            return "completed"
        if d["task_type"] in ("TT_Mile", "TT_FinMile"):
            return "milestone"
        if self.opts.get("sched_use_expect_end_flag") == "Y" and d.get("expect_end_date"):
            return "expected finish date used by the schedule options"
        if "CS_MANDFIN" in (d["cstr_type"], d["cstr_type2"]):
            return "mandatory finish constraint"
        return None

    def rem(self, d: dict, es: Optional[datetime] = None) -> float:
        if d["status_code"] == "TK_Complete" or d["task_type"] in ("TT_Mile", "TT_FinMile"):
            return 0.0
        if (self.opts.get("sched_use_expect_end_flag") == "Y" and d.get("expect_end_date") and es is not None
                and d["cal"] is not None and not d["cal"].error):
            return max(0.0, d["cal"].work_hours(es, d["expect_end_date"]))
        return d["rem"]

    def order(self) -> tuple[list[int], dict, dict]:
        by_s, by_p = {}, {}
        for lk in self.links:
            if lk["succ"] in self.t and lk["pred"] in self.t:
                by_s.setdefault(lk["succ"], []).append(lk)
                by_p.setdefault(lk["pred"], []).append(lk)
        indeg = {i: len(by_s.get(i, [])) for i in self.t}
        out, ready = [], sorted(i for i in self.t if not indeg[i])
        while ready:
            n = ready.pop()
            out.append(n)
            for lk in by_p.get(n, []):
                indeg[lk["succ"]] -= 1
                if not indeg[lk["succ"]]:
                    ready.append(lk["succ"])
        if len(out) < len(self.t):
            loop = sorted(self.t[i]["task_code"] for i in self.t if i not in set(out))[:10]
            raise ValueError(f"The network has a loop (P6 refuses to schedule it): {loop}")
        return out, by_s, by_p

    # ------------------------------------------------------------------ passes
    def run(self) -> dict[int, dict]:
        """{task_id: {es, ef, ls, lf, tf_h}} of every activity of the project."""
        if self.dd is None:
            raise ValueError("The project has no data date (PROJECT.last_recalc_date): schedule it once in P6")
        order, by_s, by_p = self.order()
        T, dd = self.t, self.dd
        retained = self.opts.get("sched_retained_logic", "Y") != "N"
        ext_p, ext_s = {}, {}  # links to other projects by own activity
        for lk in self.ext_pred:
            ext_p.setdefault(lk["succ"], []).append(lk)
        for lk in self.ext_succ:
            ext_s.setdefault(lk["pred"], []).append(lk)
        res: dict[int, dict] = {}
        for n in order:
            d = T[n]
            cal = d["cal"] if d["cal"] is not None and not d["cal"].error else None
            nws = (lambda x: cal.next_work_start(x)) if cal else (lambda x: x)
            sb, fb = [], []
            for lk in by_s.get(n, []):
                p, r = T[lk["pred"]], res[lk["pred"]]
                a = r["ef"] if LINK_FROM[lk["type"]] == "f" else r["es"]
                lc = self.lag_cal(p["cal"], d["cal"])
                b = a if (p["status_code"] == "TK_Complete" and d["status_code"] == "TK_Complete") \
                    else self.shift(lc, a, self.eff_lag(lk, p, lc))
                (sb if LINK_TO[lk["type"]] == "s" else fb).append(b)
            for lk in ext_p.get(n, ()):
                if lk["es"] or lk["ef"]:
                    a = lk["ef"] if LINK_FROM[lk["type"]] == "f" else lk["es"]
                    if a is not None:
                        (sb if LINK_TO[lk["type"]] == "s" else fb).append(self.shift(self.lag_cal(lk["cal"], d["cal"]), a, lk["lag"]))
            if d["status_code"] == "TK_Active" and not retained:
                sb, fb = [], []  # progress override: an in-progress activity ignores its predecessors
            mand = None
            for ty, dt in ((d["cstr_type"], d["cstr_date"]), (d["cstr_type2"], d["cstr_date2"])):
                if dt is None or d["status_code"] == "TK_Complete":
                    continue
                if ty in ("CS_MANDSTART", "CS_MANDFIN"):
                    mand = (ty, dt)
                elif ty in START_CSTR:
                    sb.append(dt)
                elif ty in FIN_CSTR:
                    fb.append(dt)
            if d["status_code"] == "TK_Complete":
                x = max([dd] + sb + fb)
                res[n] = {"es": x, "ef": x}
                continue
            if d["task_type"] == "TT_FinMile":
                x = max([dd] + sb + fb)
                res[n] = {"es": x, "ef": x}
                continue
            st = nws(max([dd] + sb))
            rem = self.rem(d, st)
            fin = self.shift(cal, st, rem)
            if fb and max(fb) > fin:
                fin = max(fb)
                st = self.shift(cal, fin, -rem) if rem else fin
            if mand:
                st, fin = (nws(mand[1]), self.shift(cal, nws(mand[1]), rem)) if mand[0] == "CS_MANDSTART" else \
                    ((self.shift(cal, mand[1], -rem) if rem else mand[1]), mand[1])
            if d["task_type"] == "TT_Mile":
                fin = st
            res[n] = {"es": st, "ef": fin}
        # backward
        project_end = max((r["ef"] for i, r in res.items() if T[i]["task_type"] not in ("TT_LOE", "TT_WBS")), default=dd)
        use_must = self.must is not None and self.opts.get("sched_use_project_end_date_for_float", "Y") != "N"
        open_lf = self.must if use_must else project_end
        for n in reversed(order):
            d, r = T[n], res[n]
            cal = d["cal"] if d["cal"] is not None and not d["cal"].error else None
            rem = self.rem(d, r["es"])
            lsb, lfb = [], []
            for lk in by_p.get(n, []):
                q, rq = T[lk["succ"]], res[lk["succ"]]
                a = rq["ls"] if LINK_TO[lk["type"]] == "s" else rq["lf"]
                lc = self.lag_cal(d["cal"], q["cal"])
                (lfb if LINK_FROM[lk["type"]] == "f" else lsb).append(self.shift(lc, a, -self.eff_lag(lk, d, lc)))
            for lk in ext_s.get(n, ()):
                a = lk["ls"] if LINK_TO[lk["type"]] == "s" else lk["lf"]
                if a is not None:
                    lc = self.lag_cal(d["cal"], lk["cal"])
                    (lfb if LINK_FROM[lk["type"]] == "f" else lsb).append(self.shift(lc, a, -self.eff_lag(lk, d, lc)))
            if not lfb and not lsb:
                lfb.append(open_lf)
            mand = None
            for ty, dt in ((d["cstr_type"], d["cstr_date"]), (d["cstr_type2"], d["cstr_date2"])):
                if dt is None or d["status_code"] == "TK_Complete":
                    continue
                if ty in ("CS_MANDSTART", "CS_MANDFIN"):
                    mand = (ty, dt)
                elif ty in LATE_START_CSTR:
                    lsb.append(dt)
                elif ty in LATE_FIN_CSTR:
                    lfb.append(dt)
            cands = lfb + [self.shift(cal, cal.next_work_start(x) if cal else x, rem) if rem else x for x in lsb]
            lf = min(cands)
            if rem and cal:
                lf = cal.prev_work_end(lf)
                ls = cal.subtract_work_hours(lf, rem)
            else:
                if d["task_type"] == "TT_FinMile" and d["status_code"] != "TK_Complete" and cal:
                    lf = cal.prev_work_end(lf)
                ls = lf if not rem else self.shift(cal, lf, -rem)
            if mand:
                ls, lf = (mand[1], self.shift(cal, mand[1], rem)) if mand[0] == "CS_MANDSTART" else \
                    (self.shift(cal, mand[1], -rem), mand[1])
            r.update(ls=ls, lf=lf)
        ft = self.opts.get("sched_float_type", "FT_FF")
        for n, r in res.items():
            d = T[n]
            cal = d["cal"] if d["cal"] is not None and not d["cal"].error else None
            wh = (lambda a, b: cal.work_hours(a, b)) if cal else (lambda a, b: (b - a).total_seconds() / 3600)
            if d["cstr_type"] == "CS_ALAP" and d["status_code"] == "TK_NotStart":
                r["es"], r["ef"] = r["ls"], r["lf"]  # as late as possible (approximation: on its late dates)
            fin, st = wh(r["ef"], r["lf"]), wh(r["es"], r["ls"])
            r["tf_h"] = None if d["status_code"] == "TK_Complete" else (
                st if ft == "FT_SF" else min(st, fin) if ft not in ("FT_FF", "FT_SF") else fin)
        self._wbs_spans(res)
        return res

    def _wbs_spans(self, res: dict) -> None:
        """WBS summary activities span the activities of their own WBS node (approximation: child
        nodes are not included, float is not recomputed). LOE activities keep their own dates."""
        by_wbs: dict = {}
        for i, x in self.t.items():
            if x["task_type"] not in ("TT_LOE", "TT_WBS"):
                by_wbs.setdefault(x["wbs_id"], []).append(res[i])
        for n, d in self.t.items():
            if d["task_type"] == "TT_WBS":
                kids = by_wbs.get(d["wbs_id"])
                if kids:
                    res[n].update(es=min(k["es"] for k in kids), ef=max(k["ef"] for k in kids),
                                  ls=min(k["ls"] for k in kids), lf=max(k["lf"] for k in kids))

    def finish(self, res: dict) -> tuple[Optional[dict], datetime]:
        """(finish milestone or last activity, project early finish)."""
        own = [d for d in self.t.values() if d["task_type"] not in ("TT_LOE", "TT_WBS")]
        succ = {lk["pred"] for lk in self.links}
        ends = [d for d in own if d["task_id"] not in succ and d["status_code"] != "TK_Complete"] or own
        fm = max(ends, key=lambda d: (res[d["task_id"]]["ef"], d["task_type"] == "TT_FinMile", d["task_code"]), default=None)
        return fm, max((res[d["task_id"]]["ef"] for d in own), default=self.dd)


def _cpm_compare(cpm: _Cpm, res: dict, stale: bool, never: bool = False) -> dict:
    """Engine results against the stored F9: rows of mismatches with a probable cause, match rates."""
    fields = (("es", "early_start_date"), ("ef", "early_end_date"), ("ls", "late_start_date"), ("lf", "late_end_date"))
    T, mism, stats = cpm.t, {}, Counter()
    for n, d in T.items():
        done = d["status_code"] == "TK_Complete"
        bad = {}
        for k, col in fields:
            if done and k in ("ls", "lf"):
                continue  # late dates of completed activities: not used by float, reported apart
            a, b = res[n][k], d[col]
            if b is None or a is None or abs(a - b) > CPM_TOL:
                bad[k] = {"stored": b and _fmt_dt(b), "engine": a and _fmt_dt(a)}
        if not done:
            b, a = d["total_float_hr_cnt"], res[n]["tf_h"]
            if b is None or a is None or abs(float(b) - a) > CPM_TF_TOL_H:
                bad["tf_d"] = {"stored": _days(b, d["hpd"]), "engine": _days(a, d["hpd"])}
        grp = "completed" if done else "open"
        stats[grp] += 1
        if bad:
            stats[grp + "_bad"] += 1
            mism[n] = bad
        if done:
            for k, col in (("ls", "late_start_date"), ("lf", "late_end_date")):
                if res[n][k] is not None and d[col] is not None and abs(res[n][k] - d[col]) > CPM_TOL:
                    stats["completed_late_bad"] += 1
                    break
    by_s, by_p = {}, {}
    for lk in cpm.links:
        by_s.setdefault(lk["succ"], []).append(lk["pred"])
        by_p.setdefault(lk["pred"], []).append(lk["succ"])
    ext = {lk["succ"] for lk in cpm.ext_pred} | {lk["pred"] for lk in cpm.ext_succ}
    rows = []
    for n, bad in mism.items():
        d = T[n]
        if stale:
            cause = "the project was edited since its last F9: press F9 in P6 first"
        elif never:
            cause = "no F9 recorded in this database (last_schedule_date empty): the stored dates are no oracle"
        elif d["task_type"] in ("TT_LOE", "TT_WBS"):
            cause = "LOE / WBS summary activity (approximated)"
        elif d["cstr_type"] == "CS_ALAP":
            cause = "As Late As Possible constraint (approximated)"
        elif d["expect_end_date"] is not None:
            cause = "expected finish date"
        elif d["suspend_date"] is not None or d["resume_date"] is not None:
            cause = "suspend / resume dates (not modelled)"
        elif n in ext:
            cause = "link to another project (its stored dates are used)"
        elif any(p in mism and set(mism[p]) & {"es", "ef"} for p in by_s.get(n, [])) and set(bad) & {"es", "ef"}:
            cause = "inherits an early date difference from a predecessor"
        elif any(q in mism and set(mism[q]) & {"ls", "lf"} for q in by_p.get(n, [])) and set(bad) & {"ls", "lf", "tf_d"}:
            cause = "inherits a late date difference from a successor"
        elif d["cstr_type"] or d["cstr_type2"]:
            cause = f"constraint {_cstr_label(d['cstr_type'] or d['cstr_type2'])}"
        else:
            cause = "unknown (resource leveling, or an option this engine does not model)"
        rows.append({"task_code": d["task_code"], "task_name": d["task_name"], "task_type": d["task_type"],
                     "status_code": d["status_code"], "differences": bad, "probable_cause": cause})
    rows.sort(key=lambda r: (r["status_code"] == "TK_Complete", r["task_code"]))
    rate = lambda g: None if not stats[g] else round(1 - stats[g + "_bad"] / stats[g], 4)  # noqa: E731
    return {"rows": rows, "match_rate": rate("open"), "match_rate_completed": rate("completed"),
            "counts": {"open": stats["open"], "open_mismatched": stats["open_bad"], "completed": stats["completed"],
                       "completed_mismatched": stats["completed_bad"],
                       "completed_late_dates_differing": stats["completed_late_bad"]}}


@_tool()
def p6_cpm_validate(project: ProjectId, offset: Offset = 0, limit: Limit = 50) -> dict:
    """Run this server's CPM engine on the unchanged network and compare it with P6's last F9: early
    start and finish, late start and finish and total float of every open activity (completed ones:
    early dates; their late dates, unused by float, are counted apart). match_rate = share of open
    activities identical on all five (dates within a minute, float within 3 minutes); trusted when it
    is at least 0.99 (p6_schedule_whatif and the DCMA critical path test rely on it). Page of
    mismatched activities: differences (stored, engine) and probable_cause. rules: what the engine
    models. Read only."""
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")
    try:
        cpm = _Cpm(con, pid)
    finally:
        con.rollback()
    state = _schedule_state(con, pid)
    t0 = monotonic()
    res = cpm.run()
    secs = round(monotonic() - t0, 2)
    cmp = _cpm_compare(cpm, res, state["stale"], not state["last_schedule_date"])
    fm, end = cpm.finish(res)
    out = _slice(cmp.pop("rows"), offset, limit)
    out.update(project=project, **cmp, trusted=cmp["match_rate"] is not None and cmp["match_rate"] >= CPM_TRUST,
               project_finish={"engine": _fmt_dt(end), "stored": cpm.prj["scd_end_date"],
                               "finish_activity": fm and fm["task_code"]},
               options={k: cpm.opts.get(k) for k in ("sched_retained_logic", "sched_progress_override",
                                                      "sched_lag_early_start_flag", "sched_calendar_on_relationship_lag",
                                                      "sched_float_type", "sched_use_project_end_date_for_float",
                                                      "sched_use_expect_end_flag", "sched_open_critical_flag")},
               seconds=secs, schedule_state=state,
               rules="retained logic placing every status (lag between two completed activities ignored), lag elapsed "
                     "before the data date dropped, calendars per activity, lag rule calendar, must finish date for open "
                     "ends, total float per sched_float_type; LOE, WBS summary, ALAP approximated; leveling not modelled")
    return out


WhatIfOp = Literal["add_link", "delete_link", "update_link", "duration", "constraint"]


class WhatIfEdit(TypedDict, total=False):
    """One what-if edit. op add_link / delete_link / update_link: predecessor_code, successor_code,
    rel_type, lag_days, current_type (or task_pred_id for delete / update); op duration: task_code,
    remaining_days and/or original_days; op constraint: task_code, constraint (as p6_update_activity),
    constraint_date."""
    op: WhatIfOp
    predecessor_code: str
    successor_code: str
    task_pred_id: int
    rel_type: RelType
    current_type: RelType
    lag_days: float
    task_code: str
    remaining_days: float
    original_days: float
    constraint: CstrName
    constraint_date: str


WhatIfEdits = Annotated[list[WhatIfEdit], Field(description="Edits applied in memory, in order.")]
CompareTo = Literal["current", "engine"]


def _whatif_apply(cpm: _Cpm, e: Mapping) -> str:
    """Apply one edit to the in-memory network; returns its description."""
    op = e.get("op")
    if op not in get_args(WhatIfOp):
        raise ValueError(f"op must be one of {list(get_args(WhatIfOp))}")
    act = lambda code: cpm.t[cpm.code[code]] if code in cpm.code else (_ for _ in ()).throw(  # noqa: E731
        ValueError(f"Activity {code} not found in project {cpm.prj['proj_short_name']} (what-if edits stay inside it)"))
    if op in ("add_link", "delete_link", "update_link"):
        if op != "add_link" and e.get("task_pred_id") is not None:
            hits = [lk for lk in cpm.links if lk["id"] == e["task_pred_id"]]
            if not hits:
                raise ValueError(f"task_pred_id {e['task_pred_id']} is not a link inside the project")
        else:
            p, s = act(e.get("predecessor_code")), act(e.get("successor_code"))
            ty = e.get("current_type") if op != "add_link" else e.get("rel_type") or "FS"
            code = _rel_code(ty, "type") if ty else None
            hits = [lk for lk in cpm.links if lk["pred"] == p["task_id"] and lk["succ"] == s["task_id"]
                    and (code is None or lk["type"] == code)]
            if op == "add_link":
                if p["task_id"] == s["task_id"]:
                    raise ValueError("An activity cannot be linked to itself.")
                if hits:
                    raise ValueError(f"{p['task_code']} -> {s['task_code']} {ty} already exists")
                lag = _lag_arg(e.get("lag_days") or 0) * cpm.lag_hpd(p, s)
                cpm.links.append({"id": None, "succ": s["task_id"], "pred": p["task_id"], "type": code, "lag": lag})
                return f"add {REL_TYPES_REV[code]} {p['task_code']} -> {s['task_code']} lag {e.get('lag_days') or 0:g} d"
            if not hits:
                raise ValueError(f"No link {p['task_code']} -> {s['task_code']}" + (f" {ty}" if ty else ""))
            if len(hits) > 1:
                raise ValueError(f"Several links {p['task_code']} -> {s['task_code']}: give current_type")
        lk = hits[0]
        p, s = cpm.t[lk["pred"]], cpm.t[lk["succ"]]
        txt = f"{REL_TYPES_REV.get(lk['type'])} {p['task_code']} -> {s['task_code']}"
        if op == "delete_link":
            cpm.links.remove(lk)
            return f"delete {txt}"
        ch = []
        if e.get("rel_type"):
            new = _rel_code(e["rel_type"], "rel_type")
            if new != lk["type"] and any(x is not lk and x["pred"] == lk["pred"] and x["succ"] == lk["succ"] and x["type"] == new
                                         for x in cpm.links):
                raise ValueError(f"A {e['rel_type']} link already joins {p['task_code']} -> {s['task_code']}")
            lk["type"] = new
            ch.append(f"type {e['rel_type']}")
        if e.get("lag_days") is not None:
            lk["lag"] = _lag_arg(e["lag_days"]) * cpm.lag_hpd(p, s)
            ch.append(f"lag {e['lag_days']:g} d")
        if not ch:
            raise ValueError("update_link: give rel_type and/or lag_days")
        return f"update {txt}: {', '.join(ch)}"
    d = act(e.get("task_code"))
    if op == "duration":
        ch = []
        for k in ("remaining_days", "original_days"):
            if e.get(k) is not None:
                v = _num(e[k])
                if v is None or v < 0:
                    raise ValueError(f"{k} must be 0 or more")
        if e.get("remaining_days") is not None:
            d["rem"] = float(e["remaining_days"]) * (d["hpd"] or 8.0)
            ch.append(f"remaining {e['remaining_days']:g} d")
        elif e.get("original_days") is not None and d["status_code"] == "TK_NotStart":
            d["rem"] = float(e["original_days"]) * (d["hpd"] or 8.0)  # not started: remaining follows the original
            ch.append(f"original {e['original_days']:g} d (remaining follows)")
        if not ch:
            raise ValueError("duration: give remaining_days (or original_days for a not started activity)")
        fixed = cpm.duration_fixed(d)
        if fixed:
            ch.append(f"no effect on its dates ({fixed})")
        return f"{d['task_code']}: " + ", ".join(ch)
    name = e.get("constraint")
    if name not in CSTR_TYPES:
        raise ValueError(f"constraint must be one of {list(CSTR_TYPES)}")
    code = CSTR_TYPES[name]
    dt = None
    if code and code != "CS_ALAP":
        if not e.get("constraint_date"):
            raise ValueError("constraint_date is required for this constraint")
        dt = _db_dt(_resolve_dt(d["cal"] if d["cal"] and not d["cal"].error else None, e["constraint_date"], "constraint_date",
                                "finish" if code in CSTR_FINISH else "start")[0])
    d["cstr_type"], d["cstr_date"] = code, dt
    return f"{d['task_code']}: constraint {_cstr_label(code) or 'none'} {_fmt_dt(dt) if dt else ''}".strip()


@_tool()
def p6_schedule_whatif(project: ProjectId, edits: WhatIfEdits, compare_to: CompareTo = "current",
                       milestones: Optional[list[ActivityId]] = None, offset: Offset = 0, limit: Limit = 50) -> dict:
    """What-if scheduling in memory (the database is never written): applies the edits in order (add,
    delete or update links, durations, constraints; lag days and durations in days of the lag rule and
    activity calendars), runs this server's CPM engine and compares with compare_to: 'current' (P6's
    last F9 as stored) or 'engine' (the engine on the unchanged network: isolates the edits from any
    engine difference). project_finish {before, after, delta_cd (calendar days), delta_wd (working days
    of the finish activity)}; milestones (given, else the finish activity and every constrained
    activity) with finish and total float before / after; page of changed activities (early dates or
    total float moved by more than a minute), largest finish change first. engine: match rate of the
    unchanged network against the F9 (trusted at 0.99). Changes to make for real: the write tools,
    then F9 in P6."""
    if compare_to not in get_args(CompareTo):
        raise ValueError("compare_to must be current or engine")
    if not isinstance(edits, (list, tuple)) or not edits:
        raise ValueError("edits: a list of at least one edit")
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")
    try:
        base = _Cpm(con, pid)
    finally:
        con.rollback()
    state = _schedule_state(con, pid)
    r0 = base.run()
    chk = _cpm_compare(base, r0, state["stale"])
    trial = base.copy()
    done = []
    for i, e in enumerate(edits):
        if not isinstance(e, Mapping):
            raise ValueError(f"edit {i}: an object with op")
        try:
            done.append(_whatif_apply(trial, e))
        except ValueError as err:
            raise ValueError(f"edit {i} ({e.get('op')}): {err}") from None
    r1 = trial.run()
    T = base.t
    if compare_to == "current":
        before = {n: {"es": d["early_start_date"], "ef": d["early_end_date"], "tf_h": None if d["total_float_hr_cnt"] is None
                      else float(d["total_float_hr_cnt"])} for n, d in T.items()}
        fin_before = _db_dt(base.prj["scd_end_date"])
    else:
        before = r0
        fin_before = base.finish(r0)[1]
    fm, fin_after = trial.finish(r1)
    fcal = fm["cal"] if fm and fm["cal"] and not fm["cal"].error else None
    delta = lambda a, b: None if a is None or b is None else round((b - a).total_seconds() / 86400, 2)  # noqa: E731
    wd = lambda a, b: None if a is None or b is None or fcal is None else round(fcal.work_days(a, b), 2)  # noqa: E731
    fm0 = base.finish(r0)[0]
    keys = list(dict.fromkeys(milestones or ([x["task_code"] for x in (fm0, fm) if x] + sorted(
        d["task_code"] for d in trial.t.values() if d["cstr_type"] and d["status_code"] != "TK_Complete"))))
    ms = []
    for code in keys:
        if code not in base.code:
            raise ValueError(f"milestones: activity {code} not found in project {project}")
        n = base.code[code]
        b, a = before[n], r1[n]
        ms.append({"task_code": code, "task_name": T[n]["task_name"], "finish_before": b["ef"] and _fmt_dt(b["ef"]),
                   "finish_after": _fmt_dt(a["ef"]), "delta_cd": delta(b["ef"], a["ef"]),
                   "total_float_d_before": _days(b["tf_h"], T[n]["hpd"]), "total_float_d_after": _days(a["tf_h"], T[n]["hpd"])})
    changed = []
    for n, d in T.items():
        b, a = before[n], r1[n]
        moved = any(b[k] is None or abs(a[k] - b[k]) > CPM_TOL for k in ("es", "ef"))
        tfm = (b["tf_h"] is None) != (a["tf_h"] is None) or (b["tf_h"] is not None and abs(a["tf_h"] - b["tf_h"]) > CPM_TF_TOL_H)
        if moved or tfm:
            changed.append({"task_code": d["task_code"], "task_name": d["task_name"], "status_code": d["status_code"],
                            "early_start_before": b["es"] and _fmt_dt(b["es"]), "early_start_after": _fmt_dt(a["es"]),
                            "early_finish_before": b["ef"] and _fmt_dt(b["ef"]), "early_finish_after": _fmt_dt(a["ef"]),
                            "finish_delta_cd": delta(b["ef"], a["ef"]),
                            "total_float_d_before": _days(b["tf_h"], d["hpd"]), "total_float_d_after": _days(a["tf_h"], d["hpd"])})
    changed.sort(key=lambda r: (-abs(r["finish_delta_cd"] or 0), r["task_code"]))
    out = _slice(changed, offset, limit)
    out.update(project=project, edits=done, compare_to=compare_to,
               project_finish={"before": fin_before and _fmt_dt(fin_before), "after": _fmt_dt(fin_after),
                               "delta_cd": delta(fin_before, fin_after), "delta_wd": wd(fin_before, fin_after),
                               "finish_activity": fm and fm["task_code"]},
               milestones=ms, engine={"match_rate": chk["match_rate"], "trusted": chk["match_rate"] is not None
                                      and chk["match_rate"] >= CPM_TRUST, "open_mismatched": chk["counts"]["open_mismatched"]},
               schedule_state=state, note="In memory only: nothing was written. "
               + ("" if compare_to == "engine" else "Before = P6's last F9; with an engine mismatch, compare_to='engine' "
                                                    "isolates the edits."))
    return out


def _cpt(con, pid: int) -> dict:
    """DCMA critical path test with the engine: delay the earliest most critical open activity by 100
    working days and check that the project finish moves as much (90 % or more of its own shift)."""
    out = {"count": None, "denominator": None, "pct": None, "threshold": None, "pass": None, "sample": []}
    try:
        base = _Cpm(con, pid)
        r0 = base.run()
    except ValueError as e:
        return {**out, "note": f"not run: {e}"}
    chk = _cpm_compare(base, r0, False)
    if chk["match_rate"] is None or chk["match_rate"] < CPM_TRUST:
        return {**out, "engine_match_rate": chk["match_rate"],
                "note": f"not run: the CPM engine matches the last F9 on {chk['match_rate']} of open activities "
                        f"(needs {CPM_TRUST}); see p6_cpm_validate"}
    cand = [d for d in base.t.values() if d["task_type"] in ("TT_Task", "TT_Rsrc") and base.duration_fixed(d) is None
            and r0[d["task_id"]]["tf_h"] is not None]
    if not cand:
        return {**out, "note": "not run: no open task"}
    low = min(r0[d["task_id"]]["tf_h"] for d in cand)
    crit = sorted((d for d in cand if r0[d["task_id"]]["tf_h"] <= low + 1), key=lambda d: (r0[d["task_id"]]["es"], d["task_code"]))
    d = crit[0]
    delay = 100 * (d["hpd"] or 8.0)
    trial = base.copy()
    trial.t[d["task_id"]]["rem"] = trial.t[d["task_id"]]["rem"] + delay
    r1 = trial.run()
    f0, f1 = base.finish(r0)[1], trial.finish(r1)[1]
    act_shift = (r1[d["task_id"]]["ef"] - r0[d["task_id"]]["ef"]).total_seconds() / 86400
    fin_shift = (f1 - f0).total_seconds() / 86400
    if act_shift <= 0:
        return {**out, "activity": d["task_code"], "engine_match_rate": chk["match_rate"],
                "note": f"not evaluated: delaying {d['task_code']} did not move its own finish"}
    ok = fin_shift >= 0.9 * act_shift
    return {**out, "pass": ok, "activity": d["task_code"], "delay_d": 100,
            "activity_finish_shift_cd": round(act_shift, 2), "project_finish_shift_cd": round(fin_shift, 2),
            "engine_match_rate": chk["match_rate"]}


def _cmp_acts(con, dates: str) -> str:
    """CTEs a (project :pid) and b (other :oid) of the activity comparison: finish = actual, else early
    ('planned' takes b's planned finish), hpd, original duration and total float hours."""
    def side(p: str, fin: str) -> str:
        return (f"SELECT t.task_id, t.task_code, t.task_name, t.status_code, t.clndr_id, {HPD_SQL} AS hpd, "
                f"t.target_drtn_hr_cnt AS orig_h, t.total_float_hr_cnt AS tf_h, {fin} AS fin FROM TASK t "
                f"LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id WHERE t.proj_id = :{p}" + _alive(con, "TASK", "t"))
    cur = "COALESCE(t.act_end_date, t.early_end_date)"
    return (f"WITH a AS {MAT_SQL}({side('pid', cur)}), "
            f"b AS {MAT_SQL}({side('oid', cur if dates == 'current' else 't.target_end_date')}) ")


def _cmp_links(con) -> str:
    """CTEs la (project :pid) and lb (other :oid): links whose successor is in the project, keyed on
    (pred, succ, type, pred_project); pred_project is NULL when the predecessor is in that project."""
    def side(p: str) -> str:
        return ("SELECT r.task_pred_id AS rid, pt.task_code AS pred, st.task_code AS succ, r.pred_type AS type, "
                f"CASE WHEN pt.proj_id = :{p} THEN NULL ELSE pp.proj_short_name END AS pred_project, r.lag_hr_cnt AS lag_h, "
                "COALESCE(NULLIF(cp.day_hr_cnt, 0), 8) AS pred_hpd, COALESCE(NULLIF(cs.day_hr_cnt, 0), 8) AS succ_hpd "
                "FROM TASKPRED r JOIN TASK st ON st.task_id = r.task_id JOIN TASK pt ON pt.task_id = r.pred_task_id "
                "LEFT JOIN PROJECT pp ON pp.proj_id = pt.proj_id LEFT JOIN CALENDAR cp ON cp.clndr_id = pt.clndr_id "
                f"LEFT JOIN CALENDAR cs ON cs.clndr_id = st.clndr_id WHERE st.proj_id = :{p}"
                + _alive(con, "TASKPRED", "r") + _alive(con, "TASK", "st") + _alive(con, "TASK", "pt"))

    def one(p: str) -> str:  # a key linked twice counts once: the lowest id (bare columns follow MIN)
        return (f"SELECT MIN(rid) AS rid, pred, succ, type, pred_project, lag_h, pred_hpd, succ_hpd FROM ({side(p)}) "
                "GROUP BY pred, succ, type, pred_project")
    return f"WITH la AS {MAT_SQL}({one('pid')}), lb AS {MAT_SQL}({one('oid')}) "


LINK_KEY = "lb.pred = la.pred AND lb.succ = la.succ AND lb.type = la.type AND lb.pred_project IS la.pred_project"


@_tool()
def p6_compare_projects(project: ProjectId, other: Optional[ProjectId] = None, section: CompareSection = "activities",
                        dates: BaselineDates = "current", min_variance_days: NonNegDays = 0,
                        offset: Offset = 0, limit: Limit = 50) -> dict:
    """Compare a project with another project or baseline (other=; default: the project baseline),
    activities matched by Activity ID, links by (predecessor, successor, type, predecessor project).
    summary (whole projects): activities matched, added (only in project), removed (only in other),
    finishing later or earlier (finish = actual, else early; dates='planned' takes the other's
    planned finish), average and maximum variance in calendar days, status, original duration and
    name changes; relationships added, removed and with a changed lag; project data date and finish
    dates. Then one page of section: 'activities' (added and removed first, then by absolute
    variance; matched ones only from min_variance_days calendar days; variance_cd in calendar days,
    variance_wd in working days of the activity's calendar) or 'relationships' (added, removed,
    lag_changed; lag days under each project's lag rule)."""
    if section not in get_args(CompareSection):
        raise ValueError(f"section must be one of {list(get_args(CompareSection))}")
    if dates not in get_args(BaselineDates):
        raise ValueError(f"dates must be one of {list(get_args(BaselineDates))}")
    minv = _num(min_variance_days)
    if minv is None or minv < 0:
        raise ValueError("min_variance_days must be 0 or more")
    con = _connect()
    pid = _project_id(con, project)
    oid = _baseline_pid(con, pid, other)
    if oid is None:
        bls = []
        if "orig_proj_id" in _cols(con, "PROJECT"):
            bls = [r[0] for r in con.execute("SELECT proj_short_name FROM PROJECT WHERE orig_proj_id = ?"
                                             + _alive(con, "PROJECT") + " ORDER BY proj_short_name, proj_id", (pid,))]
        raise ValueError(f"{project} has no project baseline: pass other= (a Project ID or baseline). "
                         f"Baselines of {project}: {bls or 'none'}")
    args = {"pid": pid, "oid": oid, "minv": minv}
    pcols = _cols(con, "PROJECT")
    pf = ("last_recalc_date", "scd_end_date", "plan_end_date")
    acts, lnk = _cmp_acts(con, dates), _cmp_links(con)
    var = "julianday(a.fin) - julianday(b.fin)"
    rule = {p: _lag_rule(con, p)[0] for p in (pid, oid)}
    con.execute("BEGIN")  # summary and page from one snapshot
    try:
        prj = {p: con.execute("SELECT proj_short_name, " + ", ".join(c if c in pcols else f"NULL AS {c}" for c in pf)
                              + " FROM PROJECT WHERE proj_id = ?", (p,)).fetchone() for p in (pid, oid)}
        s = con.execute(acts + "SELECT COUNT(b_id), SUM(b_id IS NULL), SUM(v > 0), SUM(v < 0), SUM(v = 0), "
                        "AVG(CASE WHEN v > 0 THEN v END), MAX(CASE WHEN v > 0 THEN v END), MIN(CASE WHEN v < 0 THEN v END), "
                        "SUM(b_id IS NOT NULL AND st IS NOT b_st), SUM(b_id IS NOT NULL AND orig <> b_orig), "
                        "SUM(b_id IS NOT NULL AND nm IS NOT b_nm) FROM (SELECT b.task_id AS b_id, "
                        f"a.status_code AS st, b.status_code AS b_st, COALESCE(a.orig_h, 0) AS orig, "
                        f"COALESCE(b.orig_h, 0) AS b_orig, a.task_name AS nm, b.task_name AS b_nm, {var} AS v "
                        "FROM a LEFT JOIN b ON b.task_code = a.task_code)", args).fetchone()
        removed = con.execute(acts + "SELECT COUNT(*) FROM b WHERE NOT EXISTS (SELECT 1 FROM a WHERE a.task_code = "
                              "b.task_code)", args).fetchone()[0]
        lsum = con.execute(lnk + "SELECT (SELECT COUNT(*) FROM la JOIN lb ON " + LINK_KEY + "), "
                           "(SELECT COUNT(*) FROM la WHERE NOT EXISTS (SELECT 1 FROM lb WHERE " + LINK_KEY + ")), "
                           "(SELECT COUNT(*) FROM lb WHERE NOT EXISTS (SELECT 1 FROM la WHERE " + LINK_KEY + ")), "
                           "(SELECT COUNT(*) FROM la JOIN lb ON " + LINK_KEY + " WHERE COALESCE(la.lag_h, 0) <> "
                           "COALESCE(lb.lag_h, 0))", args).fetchone()
        if section == "activities":
            cals = _calendars(con)
            q = acts + ("SELECT * FROM (SELECT CASE WHEN b.task_id IS NULL THEN 'added' ELSE 'matched' END AS change, "
                        "a.task_id AS tid, a.task_code, a.task_name, b.task_name AS other_name, a.status_code, "
                        "b.status_code AS other_status, a.clndr_id, a.hpd, b.hpd AS other_hpd, a.orig_h, b.orig_h AS other_orig_h, "
                        f"a.tf_h, b.tf_h AS other_tf_h, a.fin, b.fin AS other_fin, {var} AS v FROM a LEFT JOIN b "
                        "ON b.task_code = a.task_code UNION ALL SELECT 'removed', b.task_id, b.task_code, NULL, b.task_name, "
                        "NULL, b.status_code, NULL, NULL, b.hpd, NULL, b.orig_h, NULL, b.tf_h, NULL, b.fin, NULL FROM b "
                        "WHERE NOT EXISTS (SELECT 1 FROM a WHERE a.task_code = b.task_code)) "
                        "WHERE change <> 'matched' OR :minv <= 0 OR ABS(v) >= :minv "
                        "ORDER BY change = 'matched', ROUND(ABS(COALESCE(v, 0)), 6) DESC, task_code, tid")

            def row_fn(r: sqlite3.Row) -> dict:
                cal = cals.get(r["clndr_id"])
                a, b = _db_dt(r["fin"]), _db_dt(r["other_fin"])
                wd = round(cal.work_days(b, a), 2) if cal is not None and not cal.error and a and b else None
                return {"change": r["change"], "task_code": r["task_code"], "task_name": r["task_name"],
                        "other_task_name": r["other_name"], "status": r["status_code"], "other_status": r["other_status"],
                        "finish": r["fin"], "other_finish": r["other_fin"],
                        "variance_cd": None if r["v"] is None else round(r["v"], 2), "variance_wd": wd,
                        "orig_dur_d": _days(r["orig_h"], r["hpd"]), "other_orig_dur_d": _days(r["other_orig_h"], r["other_hpd"]),
                        "total_float_d": _days(r["tf_h"], r["hpd"]), "other_total_float_d": _days(r["other_tf_h"], r["other_hpd"])}
        else:
            q = lnk + ("SELECT * FROM (SELECT CASE WHEN lb.rid IS NULL THEN 'added' ELSE 'lag_changed' END AS change, "
                       "la.rid, la.pred, la.succ, la.type, la.pred_project, la.lag_h, lb.lag_h AS other_lag_h, la.pred_hpd, "
                       "la.succ_hpd, lb.pred_hpd AS other_pred_hpd, lb.succ_hpd AS other_succ_hpd FROM la LEFT JOIN lb ON "
                       + LINK_KEY + " WHERE lb.rid IS NULL OR COALESCE(la.lag_h, 0) <> COALESCE(lb.lag_h, 0) UNION ALL "
                       "SELECT 'removed', lb.rid, lb.pred, lb.succ, lb.type, lb.pred_project, NULL, lb.lag_h, NULL, NULL, "
                       "lb.pred_hpd, lb.succ_hpd FROM lb WHERE NOT EXISTS (SELECT 1 FROM la WHERE " + LINK_KEY + ")) "
                       "ORDER BY change, succ, pred, type, rid")

            def lag_d(h, ph, sh, p: int):
                return None if ph is None else _days(h or 0, _lag_hpd(con, {"hpd": ph}, {"hpd": sh}, p, rule[p])[0])

            def row_fn(r: sqlite3.Row) -> dict:
                return {"change": r["change"], "pred_code": r["pred"], "succ_code": r["succ"],
                        "type": REL_TYPES_REV.get(r["type"], r["type"]), "pred_project": r["pred_project"],
                        "lag_d": lag_d(r["lag_h"], r["pred_hpd"], r["succ_hpd"], pid),
                        "other_lag_d": lag_d(r["other_lag_h"], r["other_pred_hpd"], r["other_succ_hpd"], oid),
                        "lag_h": r["lag_h"], "other_lag_h": r["other_lag_h"]}
        res = _page(con, q, args, offset, limit, row_fn)
    finally:
        con.rollback()
    r2 = lambda v: None if v is None else round(v, 2)  # noqa: E731
    pa, pb = prj[pid], prj[oid]
    res = {"project": project, "other": pb[0], "other_source": "other=" if other else "project baseline",
           "dates": dates, "section": section, **res,
           "summary": {"activities": {"matched": s[0], "added": s[1] or 0, "removed": removed, "later": s[2] or 0,
                                      "earlier": s[3] or 0, "same_finish": s[4] or 0, "later_avg_cd": r2(s[5]),
                                      "later_max_cd": r2(s[6]), "earlier_max_cd": r2(s[7]), "status_changed": s[8] or 0,
                                      "orig_dur_changed": s[9] or 0, "name_changed": s[10] or 0},
                       "relationships": {"matched": lsum[0], "added": lsum[1], "removed": lsum[2], "lag_changed": lsum[3]}},
           "projects": {k: {"project": pa[k], "other": pb[k]} for k in pf},
           "basis": ("finish = actual, else early finish; other: " + ("same" if dates == "current" else "planned finish")
                     + "; variance = finish - other finish (positive = later)"),
           "schedule_state": _schedule_state(con, pid)}
    return res


# ----------------------------------------------------------------- reporting: groups, look-ahead, S-curve, summary

def _group_row(a: Mapping) -> dict:
    """p6_group_summary figures of merged _act_agg_sql aggregates (plus crit)."""
    r2 = lambda v: None if v is None else round(v, 2)  # noqa: E731
    return {**_rollup(a), "critical": a.get("crit") or 0, "max_tf_d": r2(a.get("tf_max")),
            "avg_tf_d": r2(a["tf_sum"] / a["tf_n"]) if a.get("tf_n") else None,
            "orig_dur_d": r2(a.get("orig_d")), "rem_dur_d": r2(a.get("rem_d"))}


@_tool()
def p6_group_summary(project: ProjectId, by: GroupBy = "status", code_type: Optional[str] = None,
                     code_level: Optional[Level] = None, wbs_level: Level = 1, include_completed: bool = True,
                     offset: Offset = 0, limit: Limit = 100) -> dict:
    """Activities of a project grouped, one page: {total, offset, returned, next_offset, rows, by,
    level, code_type, totals, critical_definition, schedule_state, note}. by: status, task_type, wbs
    (the WBS node at wbs_level below the project node; an activity on a shallower node keeps its
    own) or code (code_type= names the activity code type; the code at code_level, default the code
    assigned; '(none)' = no code of that type, listed last). Order: status, type name, P6 WBS or
    code order. Row: group, name, activities, open (not completed, LOE and WBS summary left out),
    by status, min_tf_d, max_tf_d, avg_tf_d (total float of the open activities, days of each
    calendar), negative_float and critical (open ones, project definition as p6_critical_path),
    orig_dur_d and rem_dur_d (sums of
    days), start, finish, budget_units, actual_units (labor plus nonlabor), units_pct (actual / at
    completion). totals: the same over every activity, so the groups always add up to it.
    include_completed=False leaves completed activities out."""
    if by not in get_args(GroupBy):
        raise ValueError(f"by must be one of {list(get_args(GroupBy))}")
    if by == "code" and not code_type:
        raise ValueError("by='code' needs code_type (the activity code type, e.g. 'Discipline')")
    for name, v in (("wbs_level", wbs_level), ("code_level", code_level)):
        if v is not None and int(v) < 1:
            raise ValueError(f"{name} must be 1 or more")
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # settings, aggregates and the WBS or code tree from one snapshot
    crit, args, definition = _critical_clause(con, pid, "project", None)[:3]
    info, join = None, ""
    if by == "code":
        info = _code_type(con, pid, code_type)
        join = " LEFT JOIN TASKACTV ta ON ta.task_id = t.task_id AND ta.actv_code_type_id = ?" + _alive(con, "TASKACTV", "ta")
        args = [*args, info["type_id"]]
    key = {"status": "t.status_code", "task_type": "t.task_type", "wbs": "t.wbs_id", "code": "ta.actv_code_id"}[by]
    sql = (f"SELECT {key} AS k, {_act_agg_sql(con)}, SUM(t.status_code <> 'TK_Complete' AND t.task_type NOT IN "
           f"{SUMMARY_TYPES} AND ({crit})) AS crit FROM TASK t LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id{join} "
           "WHERE t.proj_id = ?" + _alive(con, "TASK", "t")
           + ("" if include_completed else " AND t.status_code <> 'TK_Complete'") + " GROUP BY k")
    try:
        raw = {r["k"]: {k: r[k] for k in r.keys() if k != "k"} for r in con.execute(sql, [*args, pid])}
        nodes = _wbs_tree(con, pid) if by == "wbs" else []
        tree = _code_tree(con, info["type_id"]) if info else {}
    finally:
        con.rollback()
    groups: dict = {}  # label -> [sort key, row head, merged aggregates]

    def add(label: str, sort: tuple, head: dict, agg: Mapping) -> None:
        g = groups.setdefault(label, [sort, {"group": label, **head}, {}])
        g[2] = _merge_agg(g[2], agg)

    level = None
    if by == "wbs":
        level, index = int(wbs_level), {n["wbs_id"]: (i, n) for i, n in enumerate(nodes)}
        for w, agg in raw.items():
            if w not in index:
                add("(not in the WBS tree)", (len(nodes),), {"name": None, "wbs_id": None, "depth": None}, agg)
                continue
            i, n = index[w]
            while n["depth"] > level:
                i, n = index[n["parent_wbs_id"]]
            add(n["wbs_code"], (i,), {"name": n["wbs_name"], "wbs_id": n["wbs_id"], "depth": n["depth"]}, agg)
    elif by == "code":
        level = None if code_level is None else int(code_level)
        for cid, agg in raw.items():
            c = tree.get(cid)
            if c is None:
                add("(none)" if cid is None else f"(code {cid} not found)", (2 if cid is None else 1,),
                    {"name": None, "level": None}, agg)
                continue
            if level is not None and c["level"] > level:
                c = tree[c["chain"][level - 1]]
            add(c["path"], (0, c["sort"]), {"name": c["name"], "level": c["level"]}, agg)
    else:
        order, names = (list(STATUS_NAMES), STATUS_NAMES) if by == "status" else ([], TYPE_NAMES)
        for k, agg in raw.items():
            add(k, (order.index(k), "") if k in order else (len(order), str(k)), {"name": names.get(k)}, agg)
    rows = [{**head, **_group_row(agg)} for _, head, agg in sorted(groups.values(), key=lambda g: g[0])]
    res = _slice(rows, offset, limit)
    res.update(by=by, level=level, code_type=info, totals=_group_row(functools.reduce(_merge_agg, raw.values(), {})),
               critical_definition=definition + "; open activities, LOE and WBS summary left out",
               schedule_state=_schedule_state(con, pid),
               note="Values of the last F9. Float of open activities in days of each calendar; units are labor plus "
                    "nonlabor. totals cover every activity grouped"
                    + ("" if include_completed else " (completed left out)") + ".")
    return res


@_tool()
def p6_lookahead(project: ProjectId, days: Optional[WindowDays] = None, weeks: Optional[WindowWeeks] = None,
                 from_date: Optional[DateTimeStr] = None, include_in_progress: bool = True,
                 include_completed: bool = False, wbs_name_contains: Optional[str] = None,
                 code_type: Optional[str] = None, code_value: Optional[str] = None, include_child_codes: bool = True,
                 fields: Optional[ActFields] = None, offset: Offset = 0, limit: Limit = 50) -> dict:
    """Look-ahead of a project, one page: {total, offset, returned, next_offset, rows, window,
    data_date, counts, schedule_state, note}. The window starts at the data date (or from_date; a
    date alone starts at 00:00), lasts days= (default 14) or weeks= calendar days and is half-open
    [from, to). Listed: not started activities starting in it, open ones finishing in it, not
    started ones spanning it (start before, finish after), every in-progress activity
    (include_in_progress=False keeps those finishing in the window) and, with include_completed=True,
    completed ones whose actual dates overlap it. Open activities use their remaining dates (early
    when empty) of the last F9. LOE and WBS summary activities are left out. Each row has window: the
    categories it meets (in_progress, starting, finishing, spanning, completed); counts gives them
    over all pages. wbs_name_contains, code_type / code_value / include_child_codes and fields= as in
    p6_query_activities. Order: start, finish, Activity ID."""
    if days is not None and weeks is not None:
        raise ValueError("give days or weeks, not both")
    n = int(days) if days is not None else 7 * int(weeks) if weeks is not None else LOOKAHEAD_DAYS
    if not 1 <= n <= 366:
        raise ValueError("the window must be 1 to 366 days (days=, or weeks= up to 52)")
    frm = _parse_dt(from_date, "from_date")[0] if from_date else None
    row_fn = _act_row(fields)
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # data date, code filter, page and counts from one snapshot
    dd = (con.execute("SELECT last_recalc_date FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone()[0]
          if "last_recalc_date" in _cols(con, "PROJECT") else None)
    frm = frm or _db_dt(dd)
    if frm is None:
        raise ValueError(f"{project} has no data date: pass from_date")
    to = frm + timedelta(days=n)
    cflt, cargs, cinfo = _code_filter(con, pid, code_type, code_value, include_child_codes)
    f, e = f"'{_fmt_dt(frm)}'", f"'{_fmt_dt(to)}'"  # strftime output: safe as SQL literals
    rs, rf = "COALESCE(t.restart_date, t.early_start_date)", "COALESCE(t.reend_date, t.early_end_date)"
    cat = {"in_progress": "t.status_code = 'TK_Active'",
           "starting": f"t.status_code = 'TK_NotStart' AND {rs} >= {f} AND {rs} < {e}",
           "finishing": f"t.status_code <> 'TK_Complete' AND {rf} >= {f} AND {rf} < {e}",
           "spanning": f"t.status_code = 'TK_NotStart' AND {rs} < {f} AND {rf} >= {e}",
           "completed": f"t.status_code = 'TK_Complete' AND t.act_start_date < {e} AND t.act_end_date >= {f}"}
    on = [k for k in cat if {"in_progress": include_in_progress, "completed": include_completed}.get(k, True)]
    inner = (_act_from(con) + f" AND t.task_type NOT IN {SUMMARY_TYPES}" + cflt
             + (" AND w.wbs_name LIKE ?" if wbs_name_contains else "") + " AND (" + " OR ".join(f"({cat[k]})" for k in on) + ")")
    args = [pid, *cargs] + ([f"%{wbs_name_contains}%"] if wbs_name_contains else [])
    q = (f"SELECT t.*, {', '.join(f'({c}) AS w_{k}' for k, c in cat.items())} FROM ({inner}) t "
         f"ORDER BY COALESCE(t.act_start_date, {rs}), {rf}, t.task_code, t.task_id")

    def row(r: sqlite3.Row) -> dict:
        return {**row_fn(r), "window": [k for k in cat if r[f"w_{k}"]]}
    try:
        res = _page(con, q, args, offset, limit, row)
        c = con.execute("SELECT " + ", ".join(f"SUM({x})" for x in cat.values()) + f" FROM ({inner}) t", args).fetchone()
    finally:
        con.rollback()
    res.update(window={"from": _fmt_dt(frm), "to": _fmt_dt(to), "days": n,
                       "basis": "calendar days, half-open [from, to), from " + ("from_date" if from_date else "the data date")},
               data_date=dd, counts={k: c[i] or 0 for i, k in enumerate(cat)})
    if cinfo:
        res["code_filter"] = cinfo
    res.update(schedule_state=_schedule_state(con, pid),
               note="Dates of the last F9. The data date has a time (e.g. 18:00): pass from_date for whole days.")
    return res


def _cum(items: Sequence[tuple], edges: Sequence[datetime]) -> list[float]:
    """Cumulative quantity at each edge (increasing) of items (qty, start, end) spread linearly from
    start to end; a zero-length item counts after its start."""
    out, step = [0.0] * len(edges), [0.0] * (len(edges) + 1)
    for q, s, e in items:
        i = bisect.bisect_right(edges, s)  # first edge after the start
        j = bisect.bisect_left(edges, e, i) if e > s else i  # first edge at or after the end
        span = (e - s).total_seconds()
        for k in range(i, j):
            out[k] += q * (edges[k] - s).total_seconds() / span
        step[j] += q
    acc = 0.0
    for k in range(len(edges)):
        acc += step[k]
        out[k] += acc
    return out


def _value_at(items: Sequence[tuple], t: datetime) -> float:
    """Cumulative quantity of items at t (exact for the linear spread of _cum)."""
    return _cum(items, [t])[0]


def _measure_rows(con, pid: int, measure: str, resource: Optional[str] = None) -> list[sqlite3.Row]:
    """Assignments of a project: target, actual and remain quantity (measure 'units': labor and
    nonlabor resources; 'cost': every resource), planned, actual and remaining dates and what
    _perf_pct needs of the activity. resource= one resource (any type)."""
    cols, tc, k = _cols(con, "TASKRSRC"), _cols(con, "TASK"), "qty" if measure == "units" else "cost"
    act = " + ".join(f"COALESCE(tr.{c}, 0)" for c in (f"act_reg_{k}", f"act_ot_{k}") if c in cols) or "0"
    rtype = "COALESCE(tr.rsrc_type, r.rsrc_type)" if "rsrc_type" in cols else "r.rsrc_type"
    tu = lambda kind: " + ".join(f"COALESCE(t.{kind}_{x}_qty, 0)" for x in ("work", "equip")  # noqa: E731
                                 if f"{kind}_{x}_qty" in tc) or "0"
    sql = (f"SELECT COALESCE(tr.target_{k}, 0) AS target, {act} AS actual, COALESCE(tr.remain_{k}, 0) AS remain, "
           "tr.target_start_date, tr.target_end_date, tr.act_start_date, tr.act_end_date, tr.restart_date, "
           "tr.reend_date, t.phys_complete_pct, t.status_code, t.target_drtn_hr_cnt, t.remain_drtn_hr_cnt, "
           + ("t.complete_pct_type" if "complete_pct_type" in tc else "NULL") + f" AS pct_type, {tu('act')} AS task_act, "
           f"{tu('remain')} AS task_remain FROM TASKRSRC tr JOIN TASK t ON t.task_id = tr.task_id "
           "LEFT JOIN RSRC r ON r.rsrc_id = tr.rsrc_id" + _alive(con, "RSRC", "r") + " WHERE t.proj_id = ?"
           + _alive(con, "TASKRSRC", "tr") + _alive(con, "TASK", "t"))
    if resource:
        return con.execute(sql + " AND r.rsrc_short_name = ? ORDER BY tr.taskrsrc_id", (pid, resource)).fetchall()
    if measure == "units":
        sql += f" AND {rtype} IN ('RT_Labor', 'RT_Equip')"
    return con.execute(sql + " ORDER BY tr.taskrsrc_id", (pid,)).fetchall()


def _perf_pct(r: Mapping) -> float:
    """Activity % complete (0 to 100) of a _measure_rows row by its percent complete type, as P6
    earned value with 'activity percent complete': CP_Phys (also the default) physical %, CP_Drtn
    (original - remaining) / original duration, CP_Units actual / at completion units of the
    activity (labor plus nonlabor); a completed activity without duration or units counts 100."""
    kind, done = r["pct_type"], 100.0 * (r["status_code"] == "TK_Complete")
    if kind == "CP_Drtn":
        od = float(r["target_drtn_hr_cnt"] or 0)
        v = 100 * (od - float(r["remain_drtn_hr_cnt"] or 0)) / od if od else done
    elif kind == "CP_Units":
        a = float(r["task_act"] or 0)
        at = a + float(r["task_remain"] or 0)
        v = 100 * a / at if at else done
    else:
        v = float(r["phys_complete_pct"] or 0)
    return min(100.0, max(0.0, v))


def _curve_items(rows: Sequence, dd: datetime) -> dict:
    """Curves of _measure_rows at data date dd, as (qty, start, end): planned (target over the planned
    dates), actual (actual from the actual start to the actual finish, clipped at dd; dates missing =
    at dd), remaining (remain from the remaining start, not before dd, to the remaining finish). Plus
    budget, actual_qty, remain_qty and earned (target x _perf_pct) totals, clipped (actual dates after
    dd), clamped (remaining start before dd) and unspread (target without planned dates)."""
    out: dict[str, Any] = {"planned": [], "actual": [], "remaining": [], "budget": 0.0, "actual_qty": 0.0,
                           "remain_qty": 0.0, "earned": 0.0, "clipped": 0, "clamped": 0, "unspread": 0.0}
    for r in rows:
        q, a, rem = float(r["target"]), float(r["actual"]), float(r["remain"])
        out["budget"] += q
        out["actual_qty"] += a
        out["remain_qty"] += rem
        out["earned"] += _perf_pct(r) / 100 * q
        s, e = _db_dt(r["target_start_date"]), _db_dt(r["target_end_date"])
        if q and s and e:
            out["planned"].append((q, s, max(s, e)))
        elif q:
            out["unspread"] += q
        if a:
            s, e = _db_dt(r["act_start_date"]), _db_dt(r["act_end_date"])
            out["clipped"] += bool((s and s > dd) or (e and e > dd))
            e = min(e or dd, dd)
            out["actual"].append((a, min(s or e, e), e))
        if rem:
            s = _db_dt(r["restart_date"]) or dd
            out["clamped"] += s < dd
            s = max(s, dd)
            out["remaining"].append((rem, s, max(_db_dt(r["reend_date"]) or s, s)))
    return out


def _period_edges(lo: datetime, hi: datetime, period: str, week_day: int = 1) -> list[datetime]:
    """Period starts from the one holding lo to the first one after hi: months (day 1) or weeks
    (P6 week start day, 1 = Sunday). Raises above MAX_ROWS periods."""
    if period == "month":
        d = datetime(lo.year, lo.month, 1)
        nxt = lambda d: datetime(d.year + d.month // 12, d.month % 12 + 1, 1)  # noqa: E731
    else:
        d = datetime.combine(lo.date() - timedelta(days=(lo.weekday() - (week_day + 5) % 7) % 7), time())
        nxt = lambda d: d + timedelta(days=7)  # noqa: E731
    out = [d]
    while out[-1] <= hi:
        if len(out) > MAX_ROWS:
            raise ValueError(f"more than {MAX_ROWS} periods: use period='month' or narrow from_date / to_date")
        out.append(nxt(out[-1]))
    return out


@_tool()
def p6_progress_curve(project: ProjectId, period: Period = "month", baseline: Optional[ProjectId] = None,
                      measure: Measure = "units", resource: Optional[str] = None, from_date: Optional[DateStr] = None,
                      to_date: Optional[DateStr] = None, updates: Optional[ProjectIds] = None) -> dict:
    """Progress S-curve per month or week in the schedule's own weights: measure 'units' (labor and
    nonlabor assignments, the default) or 'cost' (every resource); resource= one resource ID only.
    periods: per period and cumulative values with % of the budget for baseline (baseline= Project
    ID, default the project baseline; its planned dates), planned (the project's current planned
    dates), actual (actual quantities, up to the data date) and forecast (actuals, then remaining
    from the data date). to_date: exact values at the data date: baseline planned (planned value),
    planned, actual and actual %, earned and earned % (earned value: budget x activity % complete
    by its percent complete type, P6 earned value with 'activity percent complete'), variance
    (earned - baseline planned), spi (earned / baseline planned) and spi_actual (actual / baseline
    planned, the units-based index). updates= earlier updates of
    the same schedule (Project IDs): their earned and actual totals at their own data date
    (earned_points). from_date / to_date (YYYY-MM-DD) trim the periods; cumulatives stay exact.
    checks: totals reached by the last period, actuals clipped at the data date, remaining starts
    moved to it, quantities without dates, WBS nodes with another earned value technique. Spread is
    linear over calendar time: an approximation, see note."""
    if period not in get_args(Period):
        raise ValueError(f"period must be one of {list(get_args(Period))}")
    if measure not in get_args(Measure):
        raise ValueError(f"measure must be one of {list(get_args(Measure))}")
    lo = _parse_dt(from_date, "from_date")[0] if from_date else None
    hi = _parse_dt(to_date, "to_date")[0] if to_date else None
    if lo and hi and hi < lo:
        raise ValueError("to_date is before from_date")
    con = _connect()
    pid = _project_id(con, project)
    if resource and not con.execute("SELECT 1 FROM RSRC WHERE rsrc_short_name = ?" + _alive(con, "RSRC"),
                                    (resource,)).fetchone():
        raise ValueError(f"Resource not found: {resource}")
    upd = [(u, _project_id(con, u)) for u in (updates or [])]
    bid = _baseline_pid(con, pid, baseline)
    has_dd = "last_recalc_date" in _cols(con, "PROJECT")

    def dd_of(p: int) -> Optional[datetime]:
        if not has_dd:
            return None
        return _db_dt(con.execute("SELECT last_recalc_date FROM PROJECT WHERE proj_id = ?", (p,)).fetchone()[0])
    con.execute("BEGIN")  # every project from one snapshot
    try:
        dd = dd_of(pid)
        if dd is None:
            raise ValueError(f"{project} has no data date (PROJECT.last_recalc_date)")
        cur = _curve_items(_measure_rows(con, pid, measure, resource), dd)
        base = bdd = bname = None
        if bid is not None:
            bdd, bname = dd_of(bid), con.execute("SELECT proj_short_name FROM PROJECT WHERE proj_id = ?", (bid,)).fetchone()[0]
            base = _curve_items(_measure_rows(con, bid, measure, resource), bdd or dd)
        pts = [(u, dd_of(p), _curve_items(_measure_rows(con, p, measure, resource), dd_of(p) or dd)) for u, p in upd]
        week_day = _prefer(con, "week_start_day_num") or 1
        fin = {t: con.execute(f"SELECT COUNT(*) FROM {t} WHERE proj_id = ?" + _alive(con, t), (pid,)).fetchone()[0]
               if "proj_id" in _cols(con, t) else 0 for t in ("TRSRCFIN", "TASKFIN")}
        ev_other = 0  # earned value technique of the WBS nodes: only EC_Cmp_pct (activity percent complete) is applied
        if "ev_compute_type" in _cols(con, "PROJWBS"):
            ev_other = con.execute("SELECT COUNT(*) FROM PROJWBS WHERE proj_id = ? AND ev_compute_type <> 'EC_Cmp_pct'"
                                   + _alive(con, "PROJWBS"), (pid,)).fetchone()[0]
    finally:
        con.rollback()
    what = (f"resource {resource}" if resource else "labor and nonlabor assignments" if measure == "units"
            else "every assignment")
    if not cur["budget"]:
        raise ValueError(f"{project} has no planned {measure} on {what}: use measure='cost' or resource=")
    curves = {**({"baseline": base["planned"]} if base else {}), "planned": cur["planned"], "actual": cur["actual"],
              "forecast": cur["actual"] + cur["remaining"]}
    dates = [d for v in curves.values() for _, s, e in v for d in (s, e)]
    if not dates:
        raise ValueError(f"no assignment of {project} has dates")
    edges = _period_edges(lo or min(dates), hi or max(dates), period, week_day)
    cums = {k: _cum(v, edges) for k, v in curves.items()}
    budget = {k: base["budget"] if k == "baseline" else cur["budget"] for k in curves}
    r2 = lambda v: None if v is None else round(v, 2)  # noqa: E731
    periods = []
    for i, a in enumerate(edges[:-1]):
        row = {"period": f"{a:%Y-%m}" if period == "month" else f"{a:%Y-%m-%d}", "start": f"{a:%Y-%m-%d}"}
        for k, c in cums.items():
            done = k != "actual" or a <= dd  # no actuals after the data date
            row[k] = r2(c[i + 1] - c[i]) if done else None
            row[f"{k}_cum"] = r2(c[i + 1]) if done else None
            row[f"{k}_pct"] = r2(100 * c[i + 1] / budget[k]) if done and budget[k] else None
        periods.append(row)
    bp = _value_at(base["planned"], dd) if base else None
    act, ev, tol = cur["actual_qty"], cur["earned"], 1e-6 * max(1.0, cur["budget"])
    reached = (abs(cums["planned"][-1] + cur["unspread"] - cur["budget"]) <= tol
               and abs(cums["actual"][-1] - act) <= tol and abs(cums["forecast"][-1] - act - cur["remain_qty"]) <= tol
               and (not base or abs(cums["baseline"][-1] + base["unspread"] - base["budget"]) <= tol))
    point = lambda u, d, c: {"project": u, "data_date": d and _fmt_dt(d), "earned": r2(c["earned"]),  # noqa: E731
                             "actual": r2(c["actual_qty"]), "budget": r2(c["budget"]),
                             "pct": r2(100 * c["earned"] / c["budget"]) if c["budget"] else None}
    note = ("Approximation: quantities are spread linearly over calendar time between assignment dates; P6 spreads over "
            "working time and resource curves. " + ("The database holds no period actuals (TRSRCFIN and TASKFIN are empty), "
            "so" if not any(fin.values()) else "Period actuals (TRSRCFIN, TASKFIN) are not used:") + " the actual curve is "
            "rebuilt from actual dates; only values at the data date are exact. Earned value (budget x activity % complete) "
            "exists at a data date only: to_date, and earned_points of earlier updates (updates=). Planned uses the current "
            "project planned dates, which move with the schedule for unstarted work; use a baseline for a true plan."
            + ("" if base else " No baseline: pass baseline= or set a project baseline in P6.")
            + (f" {ev_other} WBS nodes use another earned value technique than activity percent complete: earned "
               "ignores it." if ev_other else ""))
    return {"project": project, "data_date": _fmt_dt(dd), "period": period,
            "measure": {"kind": measure, "resources": what, "budget": r2(cur["budget"]), "actual": r2(act),
                        "remaining": r2(cur["remain_qty"]), "at_completion": r2(act + cur["remain_qty"])},
            "baseline": base and {"project": bname, "source": "baseline=" if baseline else "project baseline "
                                  "(PROJECT.sum_base_proj_id)", "data_date": bdd and _fmt_dt(bdd), "budget": r2(base["budget"])},
            "to_date": {"baseline_planned": r2(bp), "planned": r2(_value_at(cur["planned"], dd)), "actual": r2(act),
                        "actual_pct": r2(100 * act / cur["budget"]), "earned": r2(ev), "earned_pct": r2(100 * ev / cur["budget"]),
                        "variance": r2(None if bp is None else ev - bp), "spi": round(ev / bp, 4) if bp else None,
                        "spi_actual": round(act / bp, 4) if bp else None},
            "periods": periods,
            "earned_points": sorted([point(project, dd, cur)] + [point(u, d, c) for u, d, c in pts],
                                    key=lambda p: p["data_date"] or ""),
            "checks": {"totals_reached": reached, "clipped_actuals": cur["clipped"], "clamped_remaining": cur["clamped"],
                       "unspread_planned": r2(cur["unspread"]), "trsrcfin_rows": fin["TRSRCFIN"], "taskfin_rows": fin["TASKFIN"],
                       "wbs_other_ev_technique": ev_other},
            "schedule_state": _schedule_state(con, pid), "note": note}


@_tool()
def p6_project_summary(project: ProjectId, baseline: Optional[ProjectId] = None) -> dict:
    """One-call overview of a project (values of the last F9): project (name, calendar), dates (data
    date, planned start, must finish by, scheduled finish, last schedule run, first start and last
    finish of the activities, scheduled finish minus must finish by in calendar days), activities (by
    status, by type, open, open milestones), relationships (successor in the project; from and to
    other projects), critical (counts of open activities, LOE and WBS summary left out, as
    p6_critical_path), percent_complete (primary = units: actual / at completion units, labor plus
    nonlabor, P6 Units % Complete as p6_list_wbs; p6_progress_curve gives % of the budget instead;
    physical: physical % weighted by budget units; duration; activities completed;
    units_vs_physical_mismatch: activities with units whose physical % and units % differ by more
    than 0.01 point), baseline (the project baseline, baselines attached, the one used: baseline=
    else the project baseline; finish variance in calendar days, baseline planned units % at the
    data date and spi_units = earned units / baseline planned units, earned value and linear spread
    as p6_progress_curve), counts (WBS, assignments, resources, calendars, code assignments) and
    schedule_state."""
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # settings and every figure from one snapshot
    pc, tc, al = _cols(con, "PROJECT"), _cols(con, "TASK"), functools.partial(_alive, con)
    pf = ("proj_short_name", "clndr_id", "last_recalc_date", "plan_start_date", "plan_end_date", "scd_end_date",
          "last_schedule_date", "def_complete_pct_type")
    crit, cargs, definition, thr_h = _critical_clause(con, pid, "project", None)[:4]
    tf, tf_args = _critical_clause(con, pid, "total_float", None)[:2]
    bid, pbid = _baseline_pid(con, pid, baseline), _baseline_pid(con, pid, None)

    def units(kind: str) -> str:
        return "(" + (" + ".join(f"COALESCE(t.{kind}_{x}_qty, 0)" for x in ("work", "equip") if f"{kind}_{x}_qty" in tc)
                      or "0") + ")"
    op = f"t.status_code <> 'TK_Complete' AND t.task_type NOT IN {SUMMARY_TYPES}"
    lp = "t.driving_path_flag = 'Y'" if "driving_path_flag" in tc else "NULL"
    dur = "t.task_type NOT IN ('TT_Mile', 'TT_FinMile', 'TT_LOE', 'TT_WBS')"
    phys = f"COALESCE(t.phys_complete_pct, 0) / 100.0 * {units('target')}"
    at = f"({units('act')} + {units('remain')})"
    acts = "FROM TASK t WHERE t.proj_id = ?" + al("TASK", "t")
    links = ("FROM TASKPRED r JOIN TASK s ON s.task_id = r.task_id JOIN TASK p ON p.task_id = r.pred_task_id WHERE 1=1"
             + al("TASKPRED", "r") + al("TASK", "s") + al("TASK", "p"))
    one = lambda sql, args=(pid,): con.execute(sql, args).fetchone()[0]  # noqa: E731
    try:
        p = con.execute("SELECT " + ", ".join(c if c in pc else f"NULL AS {c}" for c in pf) + " FROM PROJECT WHERE proj_id = ?",
                        (pid,)).fetchone()
        node = con.execute("SELECT wbs_name FROM PROJWBS WHERE proj_id = ? AND proj_node_flag = 'Y'" + al("PROJWBS"),
                           (pid,)).fetchone()
        cal = con.execute(f"SELECT c.clndr_name, {HPD_SQL} FROM CALENDAR c WHERE c.clndr_id = ?" + al("CALENDAR", "c"),
                          (p["clndr_id"],)).fetchone()
        by = {k: {r[0]: r[1] for r in con.execute(f"SELECT t.{k}, COUNT(*) {acts} GROUP BY t.{k} ORDER BY t.{k}", (pid,))}
              for k in ("status_code", "task_type")}
        a = con.execute(f"SELECT COUNT(*), SUM({op}), SUM({op} AND t.task_type IN ('TT_Mile', 'TT_FinMile')), "
                        f"SUM({op} AND ({crit})), SUM({op} AND ({tf})), SUM({op} AND {lp}), "
                        f"SUM({op} AND t.total_float_hr_cnt < 0), SUM({units('target')}), SUM({units('act')}), "
                        f"SUM({units('remain')}), SUM({phys}), SUM({at} > 0 AND ABS(COALESCE(t.phys_complete_pct, 0) "
                        f"- 100.0 * {units('act')} / {at}) > 0.01), "
                        f"SUM(CASE WHEN {dur} THEN COALESCE(t.target_drtn_hr_cnt, 0) END), "
                        f"SUM(CASE WHEN {dur} THEN COALESCE(t.target_drtn_hr_cnt, 0) - COALESCE(t.remain_drtn_hr_cnt, 0) END), "
                        "SUM(t.status_code = 'TK_Complete'), MIN(COALESCE(t.act_start_date, t.early_start_date)), "
                        f"MAX(COALESCE(t.act_end_date, t.early_end_date)) {acts}", [*cargs, *tf_args, pid]).fetchone()
        rel = con.execute(f"SELECT COUNT(*), SUM(p.proj_id <> s.proj_id) {links} AND s.proj_id = ?", (pid,)).fetchone()
        rel_out = one(f"SELECT COUNT(*) {links} AND p.proj_id = ? AND s.proj_id <> ?", (pid, pid))
        asg = (con.execute("SELECT COUNT(*), COUNT(DISTINCT a.rsrc_id) FROM TASKRSRC a JOIN TASK t ON t.task_id = a.task_id "
                           "WHERE t.proj_id = ?" + al("TASKRSRC", "a") + al("TASK", "t"), (pid,)).fetchone()
               if _cols(con, "TASKRSRC") else (None, None))
        counts = {"wbs": one("SELECT COUNT(*) FROM PROJWBS WHERE proj_id = ?" + al("PROJWBS")), "assignments": asg[0],
                  "resources": asg[1], "calendars_used": one(f"SELECT COUNT(DISTINCT t.clndr_id) {acts}"),
                  "code_assignments": None}
        if _cols(con, "TASKACTV"):
            counts["code_assignments"] = one("SELECT COUNT(*) FROM TASKACTV a JOIN TASK t ON t.task_id = a.task_id WHERE "
                                             "t.proj_id = ?" + al("TASKACTV", "a") + al("TASK", "t"))
        attached = ([r[0] for r in con.execute("SELECT proj_short_name FROM PROJECT WHERE orig_proj_id = ?" + al("PROJECT")
                                               + " ORDER BY proj_short_name, proj_id", (pid,))] if "orig_proj_id" in pc else [])
        name_of = lambda b: b and one("SELECT proj_short_name FROM PROJECT WHERE proj_id = ?", (b,))  # noqa: E731
        bl, dd = None, _db_dt(p["last_recalc_date"])
        if bid is not None:
            bfin = _db_dt(one("SELECT scd_end_date FROM PROJECT WHERE proj_id = ?", (bid,))) if "scd_end_date" in pc else None
            bl = {"finish": bfin, "items": _curve_items(_measure_rows(con, bid, "units"), dd) if dd else None,
                  "cur": _curve_items(_measure_rows(con, pid, "units"), dd) if dd else None}
        names = (name_of(pbid), name_of(bid))
    finally:
        con.rollback()
    r2, r4 = (lambda v: None if v is None else round(v, 2)), (lambda v: None if v is None else round(v, 4))  # noqa: E731
    budget, actual, remain = (float(v or 0) for v in a[7:10])
    pct = {"units": 100 * actual / (actual + remain) if actual + remain else None,
           "physical": 100 * float(a[10] or 0) / budget if budget else None,
           "duration": 100 * float(a[13] or 0) / float(a[12]) if a[12] else None,
           "activities_completed": 100 * (a[14] or 0) / a[0] if a[0] else None}
    primary = "units" if pct["units"] is not None else "duration"
    sf, mf = _db_dt(p["scd_end_date"]), _db_dt(p["plan_end_date"])
    cd = lambda x, y: r2((x - y).total_seconds() / 86400) if x and y else None  # noqa: E731
    base = {"project_baseline": names[0], "attached": attached, "used": names[1],
            "source": None if bid is None else "baseline=" if baseline else "project baseline (PROJECT.sum_base_proj_id)"}
    if bl:
        bp = _value_at(bl["items"]["planned"], dd) if bl["items"] else None
        bb = bl["items"]["budget"] if bl["items"] else 0
        base.update(finish_variance_cd=cd(sf, bl["finish"]),
                    planned_units_pct_at_data_date=r4(100 * bp / bb) if bp is not None and bb else None,
                    spi_units=round(bl["cur"]["earned"] / bp, 4) if bp else None)
    notes = ["Values of the last F9 and of the last progress update."]
    if bid is None:
        notes.append("No baseline: pass baseline= or set a project baseline in P6.")
    return {"project": {"project_id": project, "name": node and node[0], "calendar": cal and cal[0],
                        "hours_per_day": cal and float(cal[1])},
            "dates": {"data_date": p["last_recalc_date"], "planned_start": p["plan_start_date"],
                      "must_finish_by": p["plan_end_date"], "scheduled_finish": p["scd_end_date"],
                      "last_schedule_run": p["last_schedule_date"], "activity_start": a[15], "activity_finish": a[16],
                      "finish_vs_must_finish_by_cd": cd(sf, mf)},
            "activities": {"total": a[0], "by_status": by["status_code"], "by_type": by["task_type"], "open": a[1] or 0,
                           "milestones_open": a[2] or 0},
            "relationships": {"total": rel[0], "from_other_projects": rel[1] or 0, "to_other_projects": rel_out},
            "critical": {"definition": definition, "threshold_h": thr_h, "critical": a[3] or 0, "total_float": a[4] or 0,
                         "longest_path": None if lp == "NULL" else a[5] or 0, "negative_float": a[6] or 0},
            "percent_complete": {"primary": primary, "primary_pct": r4(pct[primary]), **{k: r4(v) for k, v in pct.items()},
                                 "budget_units": r2(budget), "actual_units": r2(actual), "remaining_units": r2(remain),
                                 "units_vs_physical_mismatch": a[11] or 0,
                                 "activity_percent_type": p["def_complete_pct_type"],
                                 "formulas": {"units": "actual / (actual + remaining) units, labor plus nonlabor",
                                              "physical": "sum(physical % x budget units) / budget units",
                                              "duration": "sum(original - remaining hours) / sum(original hours), "
                                                          "milestones, LOE and WBS summary left out",
                                              "activities_completed": "completed / all activities",
                                              "units_vs_physical_mismatch": "activities with units where |physical % - "
                                                                            "100 x actual / (actual + remaining)| > 0.01"}},
            "baseline": base, "counts": counts, "schedule_state": _schedule_state(con, pid), "notes": notes}


def _float_edges(edges_days: Optional[Sequence[float]]) -> tuple[list[float], list[str]]:
    """Bucket edges in days (default FLOAT_EDGES), finite and increasing, with 0 added when missing."""
    e = [float(x) + 0.0 for x in (FLOAT_EDGES if edges_days is None else edges_days)]  # + 0.0: -0.0 becomes 0.0
    if not e or not all(math.isfinite(x) for x in e) or any(b <= a for a, b in zip(e, e[1:])):
        raise ValueError("edges_days must be finite numbers in increasing order, e.g. [-10, -5, 0, 5, 10, 20, 44]")
    if 0.0 in e:
        return e, []
    return sorted(e + [0.0]), ["0 added to edges_days: zero float has its own bucket"]


def _float_labels(edges: Sequence[float]) -> list[tuple[str, Optional[float], Optional[float]]]:
    """(label, from_d, to_d) of the float buckets of increasing edges holding 0, in order: below the
    first edge, [a, b) below zero, exactly 0, (a, b] above zero, above the last edge."""
    pairs = list(zip(edges, edges[1:]))
    return ([(f"< {edges[0]:g}", None, edges[0])] + [(f"[{a:g}, {b:g})", a, b) for a, b in pairs if b <= 0]
            + [("= 0", 0.0, 0.0)] + [(f"({a:g}, {b:g}]", a, b) for a, b in pairs if a >= 0]
            + [(f"> {edges[-1]:g}", edges[-1], None)])


def _float_bucket(hours: Optional[float], hpd: float, edges: Sequence[float]) -> Optional[str]:
    """Label of the bucket of a float in hours for edges in days of hpd hours (0 must be an edge); None
    without a value. Hours are compared with edge x hpd, never rounded: 0.2 h is above zero."""
    if hours is None:
        return None
    h, hpd = float(hours), float(hpd or 8)
    if h == 0:
        return "= 0"
    if h < edges[0] * hpd:
        return f"< {edges[0]:g}"
    if h > edges[-1] * hpd:
        return f"> {edges[-1]:g}"
    for a, b in zip(edges, edges[1:]):
        if b <= 0 and a * hpd <= h < b * hpd:
            return f"[{a:g}, {b:g})"
        if a >= 0 and a * hpd < h <= b * hpd:
            return f"({a:g}, {b:g}]"
    raise ValueError(f"edges {list(edges)} do not hold 0")


@_tool()
def p6_float_analysis(project: ProjectId, edges_days: Optional[FloatEdges] = None, include_milestones: bool = True,
                      wbs_name_contains: Optional[str] = None, code_type: Optional[str] = None,
                      code_value: Optional[str] = None, include_child_codes: bool = True,
                      sample_size: SampleSize = 5) -> dict:
    """Total and free float distribution of the open activities (not completed, LOE and WBS summary
    left out) of the last F9, in days of each activity's calendar. Buckets from edges_days (default
    -20, -10, -5, 0, 5, 10, 20, 44; 0 is always added): below the first edge, [a, b) below zero,
    exactly 0, (a, b] above zero, above the last edge, so every value falls in exactly one bucket
    (compared in hours, never rounded). Each bucket: total_float and free_float counts, their % of
    the activities, and samples (the sample_size lowest floats). no_value counts activities without
    a float; all_ranges_covered checks that bucket counts plus no_value equal the activities. stats:
    min, max, mean and median in days, total floats that are not whole days, negative, zero,
    critical (project definition). include_milestones=False leaves milestones out;
    wbs_name_contains and code_type / code_value / include_child_codes filter as in
    p6_query_activities (code_filter in the result, as there)."""
    if not 0 <= int(sample_size) <= 50:
        raise ValueError("sample_size must be 0 to 50")
    k = int(sample_size)
    edges, notes = _float_edges(edges_days)
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # settings and rows from one snapshot (closed with the connection)
    cflt, cargs, cinfo = _code_filter(con, pid, code_type, code_value, include_child_codes)
    crit, crit_args, definition = _critical_clause(con, pid, "project", None)[:3]
    inner = (_act_from(con) + f" AND t.status_code <> 'TK_Complete' AND t.task_type NOT IN {SUMMARY_TYPES}"
             + ("" if include_milestones else " AND t.task_type NOT IN ('TT_Mile', 'TT_FinMile')") + cflt
             + (" AND w.wbs_name LIKE ?" if wbs_name_contains else ""))
    args = [*crit_args, pid, *cargs] + ([f"%{wbs_name_contains}%"] if wbs_name_contains else [])
    rows = con.execute(f"SELECT t.task_code, t.total_float_hr_cnt AS tf, t.free_float_hr_cnt AS ff, t.hpd, ({crit}) AS crit "
                       f"FROM ({inner}) t ORDER BY t.task_code, t.task_id", args).fetchall()
    labels = _float_labels(edges)
    count = {x: Counter() for x in ("tf", "ff")}
    smp: dict[str, dict[str, list]] = {"tf": {}, "ff": {}}
    days: dict[str, list[float]] = {"tf": [], "ff": []}
    whole = 0
    for r in rows:
        for x in ("tf", "ff"):
            lab = _float_bucket(r[x], r["hpd"], edges)
            if lab is None:
                continue
            count[x][lab] += 1
            d = float(r[x]) / float(r["hpd"])
            days[x].append(d)
            smp[x].setdefault(lab, []).append((d, r["task_code"]))
        whole += r["tf"] is not None and float(r["tf"]) % float(r["hpd"]) != 0
    n = len(rows)
    r2, r3 = (lambda v: None if v is None else round(v, 2)), (lambda v: None if v is None else round(v, 3))  # noqa: E731

    def stat(x: str) -> dict:
        v = days[x]
        return {f"{x}_min_d": r3(min(v)) if v else None, f"{x}_max_d": r3(max(v)) if v else None,
                f"{x}_mean_d": r3(statistics.fmean(v)) if v else None, f"{x}_median_d": r3(statistics.median(v)) if v else None}
    buckets = [{"label": lab, "from_d": lo, "to_d": hi, "total_float": count["tf"][lab], "free_float": count["ff"][lab],
                "tf_pct": r2(100 * count["tf"][lab] / n) if n else None, "ff_pct": r2(100 * count["ff"][lab] / n) if n else None,
                **{f"{x}_sample": [f"{c} ({v:g} d)" for v, c in sorted(smp[x].get(lab, []))[:k]] for x in ("tf", "ff")}}
               for lab, lo, hi in labels]
    no_value = {"total_float": n - len(days["tf"]), "free_float": n - len(days["ff"])}
    return {"project": project,
            "scope": {"activities": n, "definition": "open activities (not completed), LOE and WBS summary left out"
                      + ("" if include_milestones else ", milestones left out"), "wbs_name_contains": wbs_name_contains},
            **({"code_filter": cinfo} if cinfo else {}), "edges_days": edges, "buckets": buckets, "no_value": no_value,
            "stats": {**stat("tf"), **stat("ff"), "tf_not_whole_days": whole,
                      "negative_tf": sum(1 for v in days["tf"] if v < 0), "zero_tf": count["tf"]["= 0"],
                      "critical": sum(1 for r in rows if r["crit"]), "critical_definition": definition},
            "all_ranges_covered": all(sum(count[x].values()) + no_value[y] == n
                                      for x, y in (("tf", "total_float"), ("ff", "free_float"))),
            "notes": notes, "schedule_state": _schedule_state(con, pid),
            "note": "Float of the last F9 in days of each activity's calendar (hours / hours per day)."}


@_tool()
def p6_resource_summary(project: ProjectId, resource: Optional[str] = None, offset: Offset = 0,
                        limit: Limit = 100) -> dict:
    """Assignments of a project per resource, one page: {total, offset, returned, next_offset, rows,
    totals, note}: labor and nonlabor resources first, then material and others, each by largest
    planned quantity. Row: rsrc_id, resource (ID; '(no resource)' for role only assignments), name,
    type, assignments, activities, by_status (activities by status, each counted once), planned,
    actual, remaining and at completion quantity and cost, units_pct (actual / at completion),
    start and finish (actual, else remaining, else planned dates), cost_unlinked (assignments whose
    cost is not computed from units). totals: quantities and units_pct of the labor and nonlabor
    assignments only (hours, as p6_progress_curve units; other_type_assignments counts the ones
    left out), costs of every assignment. Quantities as stored, not time-phased: no loading against
    max units. resource= one resource ID."""
    con = _connect()
    pid = _project_id(con, project)
    if resource and not con.execute("SELECT 1 FROM RSRC WHERE rsrc_short_name = ?" + _alive(con, "RSRC"),
                                    (resource,)).fetchone():
        raise ValueError(f"Resource not found: {resource}")
    cols = _cols(con, "TASKRSRC")
    act = lambda k: " + ".join(f"COALESCE(tr.{c}, 0)" for c in (f"act_reg_{k}", f"act_ot_{k}") if c in cols) or "0"  # noqa: E731
    lab = ("COALESCE(tr.rsrc_type, r.rsrc_type)" if "rsrc_type" in cols else "r.rsrc_type") + " IN ('RT_Labor', 'RT_Equip')"

    def agg(qty_lab: bool = False) -> str:  # qty_lab: quantity sums of labor and nonlabor assignments only (totals)
        qs = (lambda e: f"SUM(CASE WHEN {lab} THEN {e} END)") if qty_lab else (lambda e: f"SUM({e})")  # noqa: E731
        st = lambda c: f"COUNT(DISTINCT CASE WHEN t.status_code = '{c}' THEN tr.task_id END)"  # noqa: E731
        return (f"COUNT(*) AS n, COUNT(DISTINCT tr.task_id) AS acts, {st('TK_NotStart')} AS ns, {st('TK_Active')} AS ip, "
                f"{st('TK_Complete')} AS cp, {qs('COALESCE(tr.target_qty, 0)')} AS pq, {qs(act('qty'))} AS aq, "
                f"{qs('COALESCE(tr.remain_qty, 0)')} AS rq, SUM(COALESCE(tr.target_cost, 0)) AS pc, SUM({act('cost')}) AS ac, "
                "SUM(COALESCE(tr.remain_cost, 0)) AS rc, MIN(COALESCE(tr.act_start_date, tr.restart_date, "
                "tr.target_start_date)) AS start, MAX(COALESCE(tr.act_end_date, tr.reend_date, tr.target_end_date)) AS finish, "
                + ("SUM(tr.cost_qty_link_flag = 'N')" if "cost_qty_link_flag" in cols else "NULL") + " AS unlinked"
                + (f", SUM(NOT COALESCE({lab}, 0)) AS other" if qty_lab else ""))
    frm = ("FROM TASKRSRC tr JOIN TASK t ON t.task_id = tr.task_id LEFT JOIN RSRC r ON r.rsrc_id = tr.rsrc_id"
           + _alive(con, "RSRC", "r") + " WHERE t.proj_id = ?" + _alive(con, "TASKRSRC", "tr") + _alive(con, "TASK", "t")
           + (" AND r.rsrc_short_name = ?" if resource else ""))
    args = [pid] + ([resource] if resource else [])
    rtype = "COALESCE(r.rsrc_type, MAX(tr.rsrc_type))" if "rsrc_type" in cols else "r.rsrc_type"
    q = (f"SELECT tr.rsrc_id AS rsrc_id, r.rsrc_short_name AS short, r.rsrc_name AS name, {rtype} AS type, {agg()} {frm} "
         f"GROUP BY tr.rsrc_id ORDER BY CASE WHEN {rtype} IN ('RT_Labor', 'RT_Equip') THEN 0 ELSE 1 END, pq DESC, n DESC, "
         "short, tr.rsrc_id")
    r2 = lambda v: round(float(v or 0), 2)  # noqa: E731

    def figures(r: sqlite3.Row) -> dict:
        aq, rq, ac, rc = (r2(r[k]) for k in ("aq", "rq", "ac", "rc"))
        return {"assignments": r["n"], "activities": r["acts"],
                "by_status": {"not_started": r["ns"] or 0, "in_progress": r["ip"] or 0, "completed": r["cp"] or 0},
                "planned_qty": r2(r["pq"]), "actual_qty": aq, "remaining_qty": rq, "at_completion_qty": r2(aq + rq),
                "units_pct": r2(100 * aq / (aq + rq)) if aq + rq else None, "planned_cost": r2(r["pc"]), "actual_cost": ac,
                "remaining_cost": rc, "at_completion_cost": r2(ac + rc), "start": r["start"], "finish": r["finish"],
                "cost_unlinked": r["unlinked"]}

    def row(r: sqlite3.Row) -> dict:
        return {"rsrc_id": r["rsrc_id"], "resource": r["short"] if r["rsrc_id"] is not None else "(no resource)",
                "name": r["name"], "type": r["type"], **figures(r)}
    con.execute("BEGIN")  # page and totals from one snapshot
    try:
        res = _page(con, q, args, offset, limit, row)
        tot = con.execute(f"SELECT COUNT(DISTINCT tr.rsrc_id) AS resources, {agg(True)} {frm}", args).fetchone()
    finally:
        con.rollback()
    res.update(totals={"resources": tot["resources"], **figures(tot), "other_type_assignments": tot["other"] or 0},
               note="Quantities and costs as stored on the assignments (resource units: hours for labor and nonlabor), "
                    "not time-phased: no loading against max units. Quantity totals cover labor and nonlabor only: "
                    "material quantities stay in their own rows and unit. Values of the last F9 and progress update.")
    return res


@_tool()
def p6_list_calendars(project: Optional[ProjectId] = None, name_contains: Optional[str] = None,
                      from_date: Optional[DateStr] = None, to_date: Optional[DateStr] = None,
                      include_exceptions: bool = False, offset: Offset = 0, limit: Limit = 50) -> dict:
    """Calendars, one page: {total, offset, returned, next_offset, rows, project}. Each row: type
    (CA_Base global, CA_Project, CA_Rsrc), hours_per_day (converts hours to days), work_week, computed
    week_hours, exception counts (inherited from the base calendar included) and warnings.
    project= keeps that project's calendars (its own, those its activities use, its default) with
    activity counts. from_date/to_date (YYYY-MM-DD, both included, max 3660 days) add working hours
    and days in that window. include_exceptions lists exception dates (window, else project dates,
    else all) with pages of at most 50 calendars, 100 dates per calendar and 800 per page:
    exception_dates_truncated marks a calendar with dates left out (narrow with project=,
    from_date/to_date or name_contains)."""
    offset, limit = _check_page(offset, min(int(limit), CAL_EXC_ROWS) if include_exceptions else limit)
    con = _connect()
    win = span = None
    if from_date or to_date:
        if not (from_date and to_date):
            raise ValueError("give both from_date and to_date")
        win = (_parse_dt(from_date, "from_date")[0].date(), _parse_dt(to_date, "to_date")[0].date())
        if win[1] < win[0]:
            raise ValueError("to_date is before from_date")
        if (win[1] - win[0]).days > CAL_WINDOW_DAYS:
            raise ValueError(f"window longer than {CAL_WINDOW_DAYS} days")
    args: dict[str, Any] = {}
    counts = "SELECT t.clndr_id, COUNT(*) AS n FROM TASK t WHERE 1=1" + _alive(con, "TASK", "t")
    where = "WHERE 1=1" + _alive(con, "CALENDAR", "c")
    pdef, order = "NULL", "c.clndr_type, c.clndr_name, c.clndr_id"
    if project:
        pid = _project_id(con, project)
        cols = _cols(con, "PROJECT")
        p = con.execute("SELECT " + ", ".join(c if c in cols else f"NULL AS {c}" for c in
                                              ("clndr_id", "plan_start_date", "scd_end_date"))
                        + " FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone()
        a, b = _db_dt(p["plan_start_date"]), _db_dt(p["scd_end_date"])
        span = (a.date(), b.date()) if a and b else None
        args.update(pid=pid, pdef=p["clndr_id"])
        counts += " AND t.proj_id = :pid"
        where += " AND (c.proj_id = :pid OR a.n > 0 OR c.clndr_id = :pdef)"
        pdef, order = "c.clndr_id = :pdef", "activities DESC, c.clndr_name, c.clndr_id"
    if name_contains:
        where += " AND c.clndr_name LIKE :name"
        args["name"] = f"%{name_contains}%"
    sql = ("SELECT c.clndr_id, c.clndr_name, c.clndr_type, c.base_clndr_id, c.day_hr_cnt, c.week_hr_cnt, "
           "b.clndr_name AS base_name, pj.proj_short_name AS cal_project, COALESCE(a.n, 0) AS activities, "
           f"{pdef} AS project_default FROM CALENDAR c "
           "LEFT JOIN CALENDAR b ON b.clndr_id = c.base_clndr_id" + _alive(con, "CALENDAR", "b")
           + " LEFT JOIN PROJECT pj ON pj.proj_id = c.proj_id" + _alive(con, "PROJECT", "pj")
           + f" LEFT JOIN ({counts} GROUP BY t.clndr_id) a ON a.clndr_id = c.clndr_id {where} ORDER BY {order}")
    cals, left = _calendars(con), [CAL_EXC_PAGE]  # exception dates the page may still list

    def row(r: sqlite3.Row) -> dict:
        out = _cal_row(r, cals.get(r["clndr_id"]) or _calendar(con, r["clndr_id"]), win, span,
                       min(CAL_EXC_EACH, left[0]) if include_exceptions else None)
        left[0] -= len(out.get("exception_dates", ()))
        return out
    res = _page(con, sql, args, offset, limit, row)
    res["project"] = project
    if any(r.get("exception_dates_truncated") for r in res["rows"]):
        res["note"] = (f"exception dates capped at {CAL_EXC_EACH} per calendar and {CAL_EXC_PAGE} per page "
                       "(exception_dates_truncated): narrow with project=, from_date/to_date or name_contains.")
    return res


@_tool()
def p6_schedule_options(project: Optional[ProjectId] = None, xer_path: Optional[str] = None,
                        xer_project: Optional[str] = None) -> dict:
    """Schedule options (P6 Tools > Schedule > Options) stored for a project, found in an XER file
    (SCHEDOPTIONS), or both side by side with the differences first. Read only: nothing is written,
    and p6_import_xer does not import them. The key activity is shown as an Activity ID. lag_rule
    gives the calendar used for lag days. xer_project picks the project when the XER has several."""
    if not project and not xer_path:
        raise ValueError("give project, xer_path or both")
    con = _connect()
    db_opts, db_src, lag = {}, None, (None, None)
    if project:
        pid = _project_id(con, project)
        db_opts, db_src = _sched_options(con, pid)
        lag = _lag_rule(con, pid)
    xer_opts, xer_info, xcodes = {}, None, {}
    if xer_path:
        _, x = _xer_read(xer_path)
        xp = _xer_project(x, xer_project)
        xer_opts = _xer_sched(x, xp)
        xer_info = {"path": xer_path, "project": xp["proj_short_name"], "has_schedoptions": bool(xer_opts)}
        xcodes = {t["task_id"]: t["task_code"] for t in x.get("TASK", []) if t.get("proj_id") == xp["proj_id"]}
    rows = _sched_diff(db_opts, xer_opts, lambda v: _db_task_code(con, v), xcodes.get)
    keys = lambda o: set(o) - {"levelprioritylist"}  # noqa: E731
    both = keys(db_opts) & keys(xer_opts)
    notes = ["Read only: nothing is written.",
             "P6 schedules with the options stored in the database; options it does not store take P6 "
             "defaults, which are not shown (the lag rule default is the predecessor calendar)."]
    if xer_path and not xer_opts:
        notes.append("The XER has no SCHEDOPTIONS row for this project.")
    return {"project": project, "db_source": db_src, "xer": xer_info, "options": rows,
            "summary": {"compared": len(both), "different": sum(1 for o in rows if o["same"] is False),
                        "only_in_db": len(keys(db_opts) - keys(xer_opts)),
                        "only_in_xer": len(keys(xer_opts) - keys(db_opts))},
            "lag_rule": {"db_effective": lag[0], "db_source": lag[1],
                         "xer": xer_opts.get("sched_calendar_on_relationship_lag")},
            "notes": notes}


@_tool()
def p6_sql_select(sql: str, offset: Offset = 0, limit: Limit = 200) -> dict:
    """Last resort: use only when no other p6_ tool answers the question. Runs one read-only
    SELECT (or WITH ... SELECT, or PRAGMA table_info(T) to see a table's columns) on the P6
    database, one page: {total, offset, returned, next_offset, rows}. Only reading is authorized;
    a query is stopped after 30 s. Add an ORDER BY ending with a unique column so pages are
    stable. total is None (with a note) when it was not counted: PRAGMA, more than 1000000 rows,
    or a count taking over 3 s. Name the columns you need: a page ends early (note, next_offset)
    past about 150000 bytes of rows, which SELECT * on TASK reaches near 70 rows."""
    q = sql.strip()
    while q.endswith(";"):
        q = q[:-1].rstrip()
    if not q.lower().startswith(("select", "with", "pragma table_info")):
        raise ValueError("Only SELECT / WITH / PRAGMA table_info statements are allowed.")
    offset, limit = _check_page(offset, limit)
    con = _connect()
    con.execute("BEGIN")  # page and count from one snapshot
    deadline = monotonic() + SQL_QUERY_SECONDS
    con.set_progress_handler(lambda: monotonic() > deadline, 10000)  # True interrupts the statement
    con.set_authorizer(_sql_authorizer)
    try:
        try:
            cur = con.execute(q)
            skipped = 0
            while skipped < offset:  # in chunks: skipped rows are never held all at once
                got = len(cur.fetchmany(min(1000, offset - skipped)))
                if not got:
                    break
                skipped += got
            rows = [_dict(r) for r in cur.fetchmany(limit + 1)]
        except sqlite3.DatabaseError as e:
            if "interrupt" in str(e).lower():
                raise ValueError(f"Query stopped after {SQL_QUERY_SECONDS} s: add conditions, or use a p6_ tool "
                                 "made for the question.") from e
            if "not authorized" in str(e).lower():
                raise ValueError("Only reading is allowed in p6_sql_select (no write, ATTACH, schema change or "
                                 "setting PRAGMA).") from e
            raise
        more, rows = len(rows) > limit, rows[:limit]
        total = None
        if skipped < offset or not more:
            total = skipped + len(rows)
        elif not q.lower().startswith("pragma"):
            count_end = min(deadline, monotonic() + SQL_COUNT_SECONDS)
            con.set_progress_handler(lambda: monotonic() > count_end, 10000)  # True interrupts the count
            try:
                n = con.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM (\n{q}\n) LIMIT {SQL_COUNT_CAP + 1})").fetchone()
                total = n[0] if n[0] <= SQL_COUNT_CAP else None
            except sqlite3.Error:
                pass
    finally:
        con.set_progress_handler(None, 0)
        con.set_authorizer(None)
        con.rollback()
        con.close()
    size, keep = 0, len(rows)  # JSON bytes as a client receives them (indent 2, inside rows)
    for i, r in enumerate(rows):
        size += len(json.dumps(r, default=str, ensure_ascii=False).encode("utf-8")) + 6 * len(r) + 12
        if size > SQL_PAGE_BYTES and i:
            keep = i
            break
    notes = []
    if keep < len(rows):
        rows, more = rows[:keep], True
        notes.append(f"page ended after {keep} rows (about {SQL_PAGE_BYTES} bytes): select fewer columns, or follow "
                     "next_offset.")
    res = _env(total, offset, rows, more=more)
    if total is None:
        notes.append(f"total not counted (PRAGMA, more than {SQL_COUNT_CAP} rows, or over {SQL_COUNT_SECONDS} s "
                     "to count); follow next_offset until it is null.")
    if notes:
        res["note"] = " ".join(notes)
    return res


@_tool()
def p6_diagnose() -> dict:
    """Start here when a tool reports a configuration problem. Shows the server settings in force
    (database, tool profile, writes allowed, restart needed), the P6 schema and version detected,
    whether P6 is running, the backup and edit log folders. Works without a database."""
    st = SETTINGS
    now_listed = {t for t in TOOL_HINTS if _policy.listed(t, st.tool_profile, st.allow_writes)}
    out: dict[str, Any] = {
        "server_version": SERVER_VERSION,
        "settings": {"source": st.source, "config_file": st.config_path, "config_error": st.config_error,
                     "config_migrated": list(st.migrated), "database": st.db_name, "db_path": DB_PATH or None,
                     "tool_profile": st.tool_profile, "allow_writes": st.allow_writes,
                     "backup_before_write": st.backup_before_write, "backup_scope": st.backup_scope,
                     "backup_retention": st.backup_retention, "block_when_p6_running": st.block_when_p6_running},
        "tools_listed": len(LISTED),
        "restart_required": now_listed != LISTED,
        "p6_running": _p6_running(),
        "openpyxl": _openpyxl_version(),  # p6_export_dashboard needs it (None: not installed)
    }
    if out["restart_required"]:
        out["restart_note"] = ("The tool profile or the write permission changed since the server started: restart "
                               "your MCP client to refresh its tool list.")
    if XER_MANIFEST is not None:
        out["xer_mode"] = {"files": [{"file": os.path.basename(f["path"]), "path": f["path"],
                                      "xer_version": f.get("xer_version"), "projects": f["projects"],
                                      "warnings": f["warnings"][:10]} for f in XER_MANIFEST["files"]],
                           "note": "Read only. A Project ID already used by an earlier file gets a ~2 suffix."}
    if not DB_PATH:
        out["database_problem"] = st.db_problem or _config.NO_DB_MESSAGE
        return out
    con = _connect()
    try:
        nk = [_dict(r) for r in con.execute(
            "SELECT key_name, key_seq_num FROM NEXTKEY WHERE lower(key_name) LIKE '%task_pred_id%' "
            "OR lower(key_name) LIKE '%task_task_id%'")]
        info = _schema_info()
        out.update({
            "db_path": DB_PATH,
            "schema": {k: info[k] for k in ("label", "p6_version", "fingerprint", "known", "trusted", "readable",
                                            "writable", "missing", "tables")},
            "writes": _write_refusal() or "allowed",
            "tables": {t: sorted(_cols(con, t))[:60] for t in ("TASK", "TASKPRED", "PROJECT", "CALENDAR")},
            "nextkey_rows": nk,
            "backup_taken_this_session": _backup_done,
            "backup_dir": _backup_dir(),
            "backups_pruned_last": _last_pruned,
            "edit_log_dir": _log_dir(),
        })
    finally:
        con.close()
    return out


# ----------------------------------------------------------------- write tools

def _commit(con: sqlite3.Connection, plan: list[str], confirm: bool, projects: Iterable[int] = (),
            log: Optional[Sequence[dict]] = None) -> dict:
    """Dry run (rollback) or commit. projects = proj_ids whose schedule the change makes stale.
    log = the edit log entries of the change, one per changed item: {operation, proj_id or project,
    table, keys, before, after, summary}; default one entry summarising plan[0]. A confirmed commit
    appends them (with tool, time, backup in force and the stale projects' last_schedule_date)."""
    mismatch = _plan_gate(plan, confirm)
    if not confirm:
        con.rollback(); con.close()
        return {"dry_run": True, "would_do": plan,
                "next": "Call again with confirm=True to apply (P6 must be closed)."}
    if mismatch:
        con.rollback(); con.close()
        return {"applied": False, "error": mismatch}
    err = _guard_write()
    if err:
        con.rollback(); con.close()
        return {"applied": False, "error": err}
    lsd = "last_schedule_date" if "last_schedule_date" in _cols(con, "PROJECT") else "NULL"
    snap = {}
    for p in sorted({int(p) for p in projects if p is not None}):
        r = con.execute(f"SELECT proj_short_name, {lsd} FROM PROJECT WHERE proj_id = ?" + _alive(con, "PROJECT"),
                        (p,)).fetchone()
        if r:
            snap[p] = (r[0], r[1])
    tool = _caller_tool()
    items = list(log) if log else [{"operation": tool[3:] if tool else "write",
                                    "proj_id": next(iter(snap), None), "summary": plan[0]}]
    names: dict[int, Optional[str]] = {p: v[0] for p, v in snap.items()}
    for it in items:  # project names now: a delete removes them at commit
        p = it.get("proj_id")
        if p is not None and p not in names:
            r = con.execute("SELECT proj_short_name FROM PROJECT WHERE proj_id = ?", (p,)).fetchone()
            names[p] = r[0] if r else None
    con.commit(); con.close()
    ts, commit_id = _log_ts(), uuid.uuid4().hex[:12]
    stale = {str(p): v[1] for p, v in snap.items()}
    entries = [{"ts": ts, "commit": commit_id, "db": os.path.basename(DB_PATH), "tool": tool,
                "operation": it.get("operation"), "project": it.get("project") or names.get(it.get("proj_id")),
                "proj_id": it.get("proj_id"), "table": it.get("table"), "keys": it.get("keys"),
                "before": it.get("before"), "after": it.get("after"), "summary": it.get("summary"),
                "change": plan[0][:160], "backup": _last_backup, "stale": stale,
                "stale_projects": [v[0] for v in snap.values()]} for it in items]
    res = {"applied": True, "done": plan, "next": "Open P6 and press F9 to reschedule."}
    if snap:
        res["stale_projects"] = [v[0] for v in snap.values()]
    try:
        res["edit_log"] = {"file": _log_write(entries), "entries": len(entries)}
    except OSError as e:  # committed already: say so rather than fail
        res["edit_log"] = {"error": f"edit log not written: {e}"}
    return res


def _open_for_write(confirm: bool) -> tuple[sqlite3.Connection, Optional[str]]:
    why = _write_refusal()  # dry runs too: a preview of a write that could never be applied misleads
    if why:
        raise RuntimeError(why)
    backup = None
    if confirm:
        err = _guard_write()
        if err:
            raise RuntimeError(err)
        if SETTINGS.backup_before_write and (SETTINGS.backup_scope == "every_write" or not _backup_done):
            backup = _backup()
    con = _connect(readonly=False)
    try:
        con.execute("BEGIN IMMEDIATE")  # fails fast if something else holds a write lock
    except BaseException:
        con.close()
        raise
    return con, backup


def _release(con: sqlite3.Connection) -> None:
    """Roll back anything uncommitted and close. Safe on an already closed connection."""
    try:
        con.rollback()
    except sqlite3.ProgrammingError:
        pass
    con.close()


@contextmanager
def _write_session(confirm: bool):
    """Open a write transaction that is always released, even if the tool raises."""
    con, backup = _open_for_write(confirm)
    try:
        yield con, backup
    finally:
        _release(con)


def _stamp_cols(con, table: str, now: str, create: bool) -> dict:
    cols = _cols(con, table)
    d = {}
    if "update_date" in cols: d["update_date"] = now
    if "update_user" in cols: d["update_user"] = P6_USER
    if create:
        if "create_date" in cols: d["create_date"] = now
        if "create_user" in cols: d["create_user"] = P6_USER
    return d


REL_LOG_COLS = ("pred_type", "lag_hr_cnt")  # TASKPRED values recorded in the edit log


def _rel_log(op: str, pred, succ, task_pred_id: int, before, after, summary: str) -> dict:
    """Edit log entry of one relationship change (before / after: TASKPRED rows or dicts)."""
    def pick(r) -> Optional[dict]:
        d = _dict(r) if isinstance(r, sqlite3.Row) else r
        return None if d is None else {k: d[k] for k in REL_LOG_COLS if k in d}
    return {"operation": op, "proj_id": succ["proj_id"], "table": "TASKPRED",
            "keys": {"task_pred_id": task_pred_id, "task_id": succ["task_id"], "pred_task_id": pred["task_id"],
                     "successor": succ["task_code"], "predecessor": pred["task_code"],
                     "pred_proj_id": pred["proj_id"]},
            "before": pick(before), "after": pick(after), "summary": summary}


def _find_rel(con, succ, pred, current_type: Optional[str]):
    """The one relationship pred -> succ (P6 allows one per type, e.g. an SS + FF pair).
    Returns (row, None) or (None, error)."""
    rows = con.execute("SELECT * FROM TASKPRED r WHERE r.task_id=? AND r.pred_task_id=?"
                       + _alive(con, "TASKPRED", "r"), (succ["task_id"], pred["task_id"])).fetchall()
    if current_type:
        if current_type.upper() not in REL_TYPES:
            raise ValueError("current_type must be FS, SS, FF or SF")
        rows = [r for r in rows if r["pred_type"] == REL_TYPES[current_type.upper()]]
    if not rows:
        return None, "Relationship not found."
    if len(rows) > 1:
        return None, ("Several relationships link these activities "
                      f"({[REL_TYPES_REV.get(r['pred_type']) for r in rows]}): pass current_type.")
    return rows[0], None


class _Refused(ValueError):
    """A link item refused for a reason the single-link tools return as {applied: False, error}."""


MAX_BATCH = 500  # items per batch call


def _rel_code(v: Any, name: str) -> str:
    if not isinstance(v, str) or v.upper() not in REL_TYPES:
        raise ValueError(f"{name} must be FS, SS, FF or SF")
    return REL_TYPES[v.upper()]


def _lag_arg(v: Any) -> float:
    f = _num(v)
    if f is None:
        raise ValueError("lag_days must be a number")
    return f


def _task_by_id(con, task_id: int) -> sqlite3.Row:
    r = con.execute(f"SELECT t.*, {HPD_SQL} AS hpd, p.proj_short_name AS proj_short_name FROM TASK t "
                    "LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id JOIN PROJECT p ON p.proj_id = t.proj_id "
                    "WHERE t.task_id = ?" + _alive(con, "TASK", "t"), (task_id,)).fetchone()
    if r is None:
        raise ValueError(f"Activity id {task_id} not found")
    return r


def _rel_ends(con, project: str, it: Mapping) -> tuple[sqlite3.Row, sqlite3.Row]:
    """(pred, succ) of a link item: successor in project, predecessor in predecessor_project or project."""
    for k in ("predecessor_code", "successor_code"):
        if not isinstance(it.get(k), str) or not it[k]:
            raise ValueError(f"{k} is required")
    succ = _task(con, project, it["successor_code"])
    pred = _task(con, it.get("predecessor_project") or project, it["predecessor_code"])
    return pred, succ


def _rel_add(con, project: str, it: Mapping) -> dict:
    """Insert one link {predecessor_code, successor_code, rel_type='FS', lag_days=0, predecessor_project}."""
    code = _rel_code(it.get("rel_type") or "FS", "rel_type")
    lag_days = _lag_arg(it.get("lag_days") or 0)
    pred, succ = _rel_ends(con, project, it)
    if pred["task_id"] == succ["task_id"]:
        raise ValueError("An activity cannot be linked to itself.")
    if con.execute("SELECT 1 FROM TASKPRED r WHERE r.task_id=? AND r.pred_task_id=? AND r.pred_type=?"
                   + _alive(con, "TASKPRED", "r"), (succ["task_id"], pred["task_id"], code)).fetchone():
        raise _Refused("This relationship already exists, use p6_update_relationship.")
    hpd, basis = _lag_hpd(con, pred, succ)
    new_id = _next_key(con, "TASKPRED", "task_pred_id")
    row = {"task_pred_id": new_id, "task_id": succ["task_id"], "pred_task_id": pred["task_id"],
           "proj_id": succ["proj_id"], "pred_proj_id": pred["proj_id"], "pred_type": code, "lag_hr_cnt": lag_days * hpd}
    row.update(_stamp_cols(con, "TASKPRED", _now_str(con), create=True))
    row = {k: v for k, v in row.items() if k in _cols(con, "TASKPRED")}
    con.execute(f"INSERT INTO TASKPRED ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    plan = (f"Add {REL_TYPES_REV[code]} {pred['task_code']} -> {succ['task_code']}, "
            f"lag {lag_days:g} d = {lag_days * hpd:g} h ({basis}) (task_pred_id {new_id})")
    return {"plan": plan, "projects": [succ["proj_id"], pred["proj_id"]], "task_pred_id": new_id,
            "log": [_rel_log("add_relationship", pred, succ, new_id, None, row, plan)]}


def _rel_update(con, project: str, it: Mapping) -> dict:
    """Change type and/or lag of one link {predecessor_code, successor_code, rel_type, lag_days,
    current_type, predecessor_project}."""
    code = _rel_code(it["rel_type"], "rel_type") if it.get("rel_type") else None
    lag_days = None if it.get("lag_days") is None else _lag_arg(it["lag_days"])
    pred, succ = _rel_ends(con, project, it)
    r, err = _find_rel(con, succ, pred, it.get("current_type"))
    if err:
        raise _Refused(err)
    sets = _stamp_cols(con, "TASKPRED", _now_str(con), create=False)
    plan = []
    if code and code != r["pred_type"]:
        if con.execute("SELECT 1 FROM TASKPRED r WHERE r.task_id=? AND r.pred_task_id=? AND r.pred_type=?"
                       + _alive(con, "TASKPRED", "r"), (succ["task_id"], pred["task_id"], code)).fetchone():
            raise _Refused(f"A {REL_TYPES_REV[code]} relationship already links these activities: change or "
                           "delete that one instead.")
        sets["pred_type"] = code
        plan.append(f"type {REL_TYPES_REV.get(r['pred_type'])} -> {REL_TYPES_REV[code]}")
    if lag_days is not None:
        hpd, basis = _lag_hpd(con, pred, succ)
        sets["lag_hr_cnt"] = lag_days * hpd
        plan.append(f"lag {_days(r['lag_hr_cnt'], hpd)} d -> {lag_days:g} d = {lag_days * hpd:g} h ({basis})")
    if not plan:
        raise _Refused("Nothing to change.")
    con.execute(f"UPDATE TASKPRED SET {', '.join(k + '=?' for k in sets)} WHERE task_pred_id=?",
                [*sets.values(), r["task_pred_id"]])
    plan = f"{pred['task_code']} -> {succ['task_code']}: " + ", ".join(plan)
    return {"plan": plan, "projects": [succ["proj_id"], pred["proj_id"]], "task_pred_id": r["task_pred_id"],
            "log": [_rel_log("update_relationship", pred, succ, r["task_pred_id"], r, {**_dict(r), **sets}, plan)]}


def _rel_delete_row(con, r: sqlite3.Row, pred: sqlite3.Row, succ: sqlite3.Row) -> dict:
    """Hard-delete one TASKPRED row (as P6 and p6_delete_relationship do; P6's trigger logs REFRDEL).
    _pred / _succ: task_ids for side effects (not reported)."""
    con.execute("DELETE FROM TASKPRED WHERE task_pred_id=?", (r["task_pred_id"],))
    hpd = _lag_hpd(con, pred, succ)[0]
    plan = f"Delete {REL_TYPES_REV.get(r['pred_type'])} {pred['task_code']} -> {succ['task_code']}"
    return {"plan": plan, "projects": [succ["proj_id"], pred["proj_id"]], "task_pred_id": r["task_pred_id"],
            "predecessor": pred["task_code"], "successor": succ["task_code"],
            "type": REL_TYPES_REV.get(r["pred_type"], r["pred_type"]), "lag_d": _days(r["lag_hr_cnt"] or 0, hpd),
            "_pred": pred["task_id"], "_succ": succ["task_id"],
            "log": [_rel_log("delete_relationship", pred, succ, r["task_pred_id"], r, None, plan)]}


def _rel_delete(con, project: str, it: Mapping) -> dict:
    """Delete one link {predecessor_code, successor_code, type (or current_type), predecessor_project}."""
    pred, succ = _rel_ends(con, project, it)
    r, err = _find_rel(con, succ, pred, it.get("type") or it.get("current_type"))
    if err:
        raise _Refused(err)
    return _rel_delete_row(con, r, pred, succ)


def _single_link(fn: Callable, project: str, it: Mapping, confirm: bool) -> dict:
    """One link change as its own transaction (the single-link tools)."""
    with _write_session(confirm) as (con, backup):
        try:
            x = fn(con, project, it)
        except _Refused as e:
            return {"applied": False, "error": str(e)}
        plan = [x["plan"]]
        if backup: plan.append(f"Backup: {backup}")
        return _commit(con, plan, confirm, projects=x["projects"], log=x["log"])


@_tool(read_only=False, destructive=True)
def p6_add_relationship(
    project: ProjectId, predecessor_code: ActivityId, successor_code: ActivityId,
    rel_type: RelType = "FS", lag_days: float = 0,
    predecessor_project: Optional[ProjectId] = None, confirm: bool = False,
) -> dict:
    """Add a relationship predecessor -> successor. rel_type FS/SS/FF/SF. lag_days is converted to
    hours with the calendar named by the project schedule option for relationship lag (predecessor
    calendar when none is stored). predecessor_project for cross-project links. Two activities may be
    linked once per type (e.g. SS and FF together). Dry run unless confirm=True."""
    _rel_code(rel_type, "rel_type")
    return _single_link(_rel_add, project, {"predecessor_code": predecessor_code, "successor_code": successor_code,
                                            "rel_type": rel_type, "lag_days": lag_days,
                                            "predecessor_project": predecessor_project}, confirm)


@_tool(read_only=False, destructive=True)
def p6_update_relationship(
    project: ProjectId, predecessor_code: ActivityId, successor_code: ActivityId,
    rel_type: Optional[RelType] = None, lag_days: Optional[float] = None,
    predecessor_project: Optional[ProjectId] = None, current_type: Optional[RelType] = None,
    confirm: bool = False,
) -> dict:
    """Change type and/or lag of an existing relationship. lag_days uses the same lag calendar rule
    as p6_add_relationship. current_type selects which one when the two activities are linked by
    several types. Dry run unless confirm=True."""
    if rel_type:
        _rel_code(rel_type, "rel_type")
    return _single_link(_rel_update, project, {"predecessor_code": predecessor_code, "successor_code": successor_code,
                                               "rel_type": rel_type, "lag_days": lag_days, "current_type": current_type,
                                               "predecessor_project": predecessor_project}, confirm)


@_tool(read_only=False, destructive=True)
def p6_delete_relationship(
    project: ProjectId, predecessor_code: ActivityId, successor_code: ActivityId,
    predecessor_project: Optional[ProjectId] = None, current_type: Optional[RelType] = None,
    confirm: bool = False,
) -> dict:
    """Delete a relationship. current_type selects which one when the two activities are linked
    by several types. Dry run unless confirm=True."""
    return _single_link(_rel_delete, project, {"predecessor_code": predecessor_code, "successor_code": successor_code,
                                               "current_type": current_type,
                                               "predecessor_project": predecessor_project}, confirm)


# ----------------------------------------------------------------- batch relationship tools

class _LinkEnds(TypedDict):
    predecessor_code: str
    successor_code: str


class LinkRef(_LinkEnds, total=False):
    """A relationship to delete: type needed when the two activities are linked by several types."""
    type: RelType
    predecessor_project: str


class LinkNew(_LinkEnds, total=False):
    """A relationship to add (rel_type default FS, lag_days default 0)."""
    rel_type: RelType
    lag_days: float
    predecessor_project: str


class LinkEdit(_LinkEnds, total=False):
    """A relationship to change: rel_type and/or lag_days; current_type picks it among several types."""
    rel_type: RelType
    lag_days: float
    current_type: RelType
    predecessor_project: str


LinkRefs = Annotated[list[LinkRef], Field(description="Links by Activity IDs.")]
LinkIds = Annotated[list[int], Field(description="task_pred_id values (p6_get_relationships, p6_redundant_links).")]
LinkNews = Annotated[list[LinkNew], Field(min_length=1, description="Links to add.")]
LinkEdits = Annotated[list[LinkEdit], Field(min_length=1, description="Links to change.")]
_ENDS = {"predecessor_code", "successor_code"}
LINK_KEYS = {"delete": set(LinkRef.__annotations__) | _ENDS | {"current_type"},
             "add": set(LinkNew.__annotations__) | _ENDS, "update": set(LinkEdit.__annotations__) | _ENDS}


def _items_arg(items: Any) -> list:
    if not isinstance(items, (list, tuple)):
        raise ValueError("items: a list of objects")
    if len(items) > MAX_BATCH:
        raise ValueError(f"At most {MAX_BATCH} items per call")
    return list(items)


def _item_keys(it: Any, kind: str) -> Mapping:
    if not isinstance(it, Mapping):
        raise ValueError("an item must be an object with predecessor_code and successor_code")
    bad = sorted(set(it) - LINK_KEYS[kind])
    if bad:
        raise ValueError(f"unknown key(s) {bad}; allowed: {sorted(LINK_KEYS[kind])}")
    return it


def _side_effects(con, lost_pred: Iterable[int], lost_succ: Iterable[int], gone: Iterable[int] = ()) -> dict:
    """Open activities (not completed, not LOE or WBS summary) that lost a predecessor or a successor in
    this transaction and now have none (links to LOE and WBS summary activities do not count, as in
    p6_health_check). gone: deleted activities, left out."""
    gone = set(gone)
    out: dict[str, Any] = {}
    for key, ids, me, other in (("no_predecessor", lost_pred, "task_id", "pred_task_id"),
                                ("no_successor", lost_succ, "pred_task_id", "task_id")):
        ids = sorted(set(ids) - gone)
        rows = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows += con.execute(
                "SELECT p.proj_short_name, t.task_code, t.task_name, t.task_type FROM TASK t JOIN PROJECT p ON "
                f"p.proj_id = t.proj_id WHERE t.task_id IN ({','.join('?' * len(chunk))})" + _alive(con, "TASK", "t")
                + f" AND t.status_code <> 'TK_Complete' AND t.task_type NOT IN {SUMMARY_TYPES} AND NOT EXISTS "
                f"(SELECT 1 FROM TASKPRED r JOIN TASK o ON o.task_id = r.{other} WHERE r.{me} = t.task_id"
                + _alive(con, "TASKPRED", "r") + _alive(con, "TASK", "o") + f" AND o.task_type NOT IN {SUMMARY_TYPES})",
                chunk).fetchall()
        out[key] = [{"project": r[0], "task_code": r[1], "task_name": r[2], "task_type": r[3]}
                    for r in sorted(rows, key=lambda r: (r[0], r[1]))]
    out["count"] = len(out["no_predecessor"]) + len(out["no_successor"])
    out["note"] = ("open activities left without any predecessor or successor by this change (LOE and WBS "
                   "summary links do not count)")
    return out


def _link_batch(project: str, items: list, kind: str, confirm: bool, fn: Callable, head: str) -> dict:
    """Batch of link changes (one transaction, all or nothing) with side_effects and lag_rule. An item
    {'task_pred_id': n} deletes that link (it must touch the project)."""
    with _write_session(confirm) as (con, backup):
        pid = _project_id(con, project)
        lost_pred, lost_succ, done, removed = set(), set(), {}, []

        def apply_one(i: int, it) -> dict:
            if kind == "delete" and isinstance(it, Mapping) and set(it) == {"task_pred_id"}:
                tpid = it["task_pred_id"]
                if isinstance(tpid, bool) or not isinstance(tpid, int):
                    raise ValueError(f"task_pred_id must be an integer, not {tpid!r}")
                if tpid in done:
                    return {"plan": f"task_pred_id {tpid}: already removed by item {done[tpid]}", "projects": []}
                r = con.execute("SELECT * FROM TASKPRED WHERE task_pred_id = ?" + _alive(con, "TASKPRED"),
                                (tpid,)).fetchone()
                if r is None:
                    raise ValueError(f"task_pred_id {tpid} not found")
                pred, succ = _task_by_id(con, r["pred_task_id"]), _task_by_id(con, r["task_id"])
                if pid not in (pred["proj_id"], succ["proj_id"]):
                    raise ValueError(f"task_pred_id {tpid} links {pred['proj_short_name']} {pred['task_code']} -> "
                                     f"{succ['proj_short_name']} {succ['task_code']}: not a link of {project}")
                x = _rel_delete_row(con, r, pred, succ)
            elif kind == "delete":
                keys = _item_keys(it, kind)
                try:
                    x = fn(con, project, keys)
                except ValueError:  # the same link named twice (by id, then by pair): already gone, not an error
                    pred, succ = _rel_ends(con, project, keys)
                    ty = keys.get("type") or keys.get("current_type")
                    prior = [j for p, s, t, j in removed if p == pred["task_id"] and s == succ["task_id"]
                             and (ty is None or t == str(ty).upper())]
                    if not prior:
                        raise
                    return {"plan": f"{pred['task_code']} -> {succ['task_code']}: already removed by item {prior[0]}",
                            "projects": []}
            else:
                x = fn(con, project, _item_keys(it, kind))
            if kind == "delete":
                removed.append((x["_pred"], x["_succ"], x["type"], i))
                done[x["task_pred_id"]] = i
                lost_pred.add(x["_succ"])
                lost_succ.add(x["_pred"])
            return x

        b = _batch(con, items, apply_one)
        for rep, it in zip(b["items"], items):
            rep["item"] = it
        side = _side_effects(con, lost_pred, lost_succ)
        lag = _lag_info(con, pid)
        res = _batch_commit(con, b, confirm, head, backup)
        res.update(side_effects=side, lag_rule=lag)
        return res


@_tool(read_only=False, destructive=True)
def p6_delete_relationships(project: ProjectId, ids: Optional[LinkIds] = None, pairs: Optional[LinkRefs] = None,
                            confirm: bool = False) -> dict:
    """Delete many relationships in one transaction (all or nothing; dry run unless confirm=True). ids:
    task_pred_id values of links that touch the project (either end); pairs: {predecessor_code,
    successor_code, type (needed when several types link the pair), predecessor_project}. items: one
    report per id, then per pair (the link removed: task_pred_id, predecessor, successor, type, lag_d;
    or why it cannot be resolved, and then confirm writes nothing). side_effects: open activities left
    without any predecessor or successor (LOE and WBS summary links do not count). Hard delete, as P6
    and p6_delete_relationship do. One edit log entry per link."""
    items = [{"task_pred_id": n} for n in _items_arg(ids or [])] + _items_arg(pairs or [])
    if len(items) > MAX_BATCH:
        raise ValueError(f"At most {MAX_BATCH} items per call")
    return _link_batch(project, items, "delete", confirm, _rel_delete, f"Delete {len(items)} relationship(s)")


@_tool(read_only=False, destructive=True)
def p6_add_relationships(project: ProjectId, items: LinkNews, confirm: bool = False) -> dict:
    """Add many relationships in one transaction (all or nothing; dry run unless confirm=True). Item:
    {predecessor_code, successor_code, rel_type (FS default), lag_days (0 default, days of the lag
    calendar rule as in p6_add_relationship), predecessor_project}. items: one report per item
    (task_pred_id from NEXTKEY, or the error); side_effects as in p6_delete_relationships (empty for
    additions). One edit log entry per link."""
    items = _items_arg(items)
    return _link_batch(project, items, "add", confirm, _rel_add, f"Add {len(items)} relationship(s)")


@_tool(read_only=False, destructive=True)
def p6_update_relationships(project: ProjectId, items: LinkEdits, confirm: bool = False) -> dict:
    """Change type and/or lag of many relationships in one transaction (all or nothing; dry run unless
    confirm=True). Item: {predecessor_code, successor_code, rel_type, lag_days, current_type (picks
    the link when several types link the pair), predecessor_project}, as p6_update_relationship.
    items: one report per item; side_effects as in p6_delete_relationships (empty: no link is
    removed). One edit log entry per link."""
    items = _items_arg(items)
    return _link_batch(project, items, "update", confirm, _rel_update, f"Change {len(items)} relationship(s)")


@_tool(read_only=False, destructive=True)
def p6_update_activity(
    project: ProjectId, task_code: ActivityId,
    task_name: Optional[str] = None,
    original_duration_days: Optional[NonNegDays] = None,
    remaining_duration_days: Optional[NonNegDays] = None,
    constraint: Optional[CstrName] = None,
    constraint_date: Optional[DateTimeStr] = None,
    actual_start: Optional[DateTimeStr] = None,
    actual_finish: Optional[DateTimeStr] = None,
    physical_pct: Optional[Pct] = None,
    allow_after_data_date: bool = False,
    confirm: bool = False,
) -> dict:
    """Edit one activity. Planned dates are not editable: move work with durations,
    logic or constraints, then F9 in P6. constraint: one of
    none, start_on_or_after, start_on_or_before, start_on, finish_on_or_after,
    finish_on_or_before, finish_on, mandatory_start, mandatory_finish, as_late_as_possible.
    Dates as 'YYYY-MM-DD HH:MM' ('T' and seconds accepted). A date alone takes the first shift
    start of the activity calendar (actual start, start constraints) or its last shift end (actual
    finish, finish constraints) and is refused on a non-work day; an explicit time is kept.
    Durations must be 0 or more, physical_pct 0 to 100, the actual finish not before the actual
    start; actual dates after the data date need allow_after_data_date=True. Setting actual_start
    makes it In Progress, actual_finish makes it Completed. A milestone has one date: either actual
    date sets both and makes it Completed (a date alone: first shift start for a start milestone,
    last shift end for a finish milestone). Dry run unless confirm=True."""
    for name, v in (("original_duration_days", original_duration_days),
                    ("remaining_duration_days", remaining_duration_days)):
        if v is not None and not (math.isfinite(float(v)) and float(v) >= 0):
            raise ValueError(f"{name}: a duration must be 0 or more")
    if physical_pct is not None and not 0 <= float(physical_pct) <= 100:
        raise ValueError("physical_pct must be between 0 and 100")
    if constraint is not None and constraint not in CSTR_TYPES:
        raise ValueError(f"constraint must be one of {list(CSTR_TYPES)}")
    code = CSTR_TYPES.get(constraint) if constraint is not None else None
    if code and code != "CS_ALAP" and not constraint_date:
        raise ValueError("constraint_date is required for this constraint")
    dates = {"actual_start": (actual_start, "start"), "actual_finish": (actual_finish, "finish"),
             "constraint_date": (constraint_date if code and code != "CS_ALAP" else None,
                                 "finish" if code in CSTR_FINISH else "start")}
    for k, (v, _) in dates.items():
        if v:
            _parse_dt(v, k)  # a malformed date fails before any lock or backup
    with _write_session(confirm) as (con, backup):
        t = _task(con, project, task_code)
        hpd = float(t["hpd"])
        sets: dict[str, Any] = {}
        plan: list[str] = []
        notes: list[str] = []
        cal = _calendars(con, int(t["clndr_id"])).get(int(t["clndr_id"])) if t["clndr_id"] is not None else None
        cal = None if cal is None or cal.error else cal

        def norm(field: str) -> Optional[str]:
            v, edge = dates[field]
            if not v:
                return None
            out, n = _resolve_dt(cal, v, field, edge)
            notes.extend(n)
            return out

        if task_name:
            sets["task_name"] = task_name; plan.append(f"name -> {task_name}")
        if original_duration_days is not None:
            sets["target_drtn_hr_cnt"] = original_duration_days * hpd
            plan.append(f"orig dur {_days(t['target_drtn_hr_cnt'], hpd)} -> {original_duration_days} d")
            if t["status_code"] == "TK_NotStart" and remaining_duration_days is None:
                sets["remain_drtn_hr_cnt"] = original_duration_days * hpd
                plan.append(f"rem dur -> {original_duration_days} d (not started: follows the original)")
        if remaining_duration_days is not None:
            sets["remain_drtn_hr_cnt"] = remaining_duration_days * hpd
            plan.append(f"rem dur {_days(t['remain_drtn_hr_cnt'], hpd)} -> {remaining_duration_days} d")
        if constraint is not None:
            sets["cstr_type"] = code
            sets["cstr_date"] = norm("constraint_date")
            plan.append(f"constraint {t['cstr_type']} -> {code} {sets['cstr_date'] or ''}".strip())
        mile = {"TT_Mile": "start", "TT_FinMile": "finish"}.get(t["task_type"])
        if mile and (actual_start or actual_finish):  # one date: P6 stores actual start = actual finish
            got = [_resolve_dt(cal, dates[f][0], f, mile) for f in ("actual_start", "actual_finish") if dates[f][0]]
            if len({v for v, _ in got}) > 1:
                raise ValueError(f"{task_code} is a {mile} milestone: its actual start and finish are one date, "
                                 f"not {' and '.join(v for v, _ in got)}")
            v = got[0][0]
            notes.extend(got[0][1])
            sets.update(act_start_date=v, act_end_date=v, status_code="TK_Complete", remain_drtn_hr_cnt=0)
            plan.append(f"actual start and finish -> {v} ({mile} milestone, Completed)")
        if actual_start and not mile:
            sets["act_start_date"] = norm("actual_start")
            started = t["status_code"] == "TK_NotStart" and not actual_finish
            if t["status_code"] == "TK_NotStart":
                sets["status_code"] = "TK_Active"
            plan.append(f"actual start -> {sets['act_start_date']}" + (" (In Progress)" if started else ""))
        if actual_finish and not mile:
            if not (actual_start or t["act_start_date"]):
                raise ValueError("Set actual_start before (or together with) actual_finish.")
            sets["act_end_date"] = norm("actual_finish")
            sets["status_code"] = "TK_Complete"
            sets["remain_drtn_hr_cnt"] = 0
            plan.append(f"actual finish -> {sets['act_end_date']} (Completed)")
        if physical_pct is not None:
            sets["phys_complete_pct"] = physical_pct; plan.append(f"physical % -> {physical_pct}")
        if not sets:
            return {"applied": False, "error": "Nothing to change."}
        start = _db_dt(sets.get("act_start_date") or t["act_start_date"])
        finish = _db_dt(sets.get("act_end_date") or t["act_end_date"])
        if start and finish and finish < start:
            raise ValueError(f"actual finish {_fmt_dt(finish)} is before actual start {_fmt_dt(start)}")
        dd = None
        if "last_recalc_date" in _cols(con, "PROJECT"):
            dd = _db_dt(con.execute("SELECT last_recalc_date FROM PROJECT WHERE proj_id = ?", (t["proj_id"],)).fetchone()[0])
        late = [f"{k} {sets[c]}" for k, c in (("actual_start", "act_start_date"), ("actual_finish", "act_end_date"))
                if c in sets and dd and _db_dt(sets[c]) > dd]
        if late and not allow_after_data_date:
            raise ValueError(f"{' and '.join(late)} after the data date {_fmt_dt(dd)}: actuals belong before it. "
                             "Pass allow_after_data_date=True if this is intended.")
        notes += [f"{x} is after the data date {_fmt_dt(dd)} (allowed)" for x in late]
        if physical_pct is not None and float(physical_pct) == 100 and not finish:
            notes.append("physical % 100 but no actual finish: the activity is not Completed until actual_finish is set")
        sets.update(_stamp_cols(con, "TASK", _now_str(con), create=False))
        sets = {k: v for k, v in sets.items() if k in _cols(con, "TASK")}
        con.execute(f"UPDATE TASK SET {', '.join(k + '=?' for k in sets)} WHERE task_id=?",
                    [*sets.values(), t["task_id"]])
        plan = [f"{task_code}: " + "; ".join(plan)] + [f"Note: {n}" for n in notes]
        if backup: plan.append(f"Backup: {backup}")
        sched = set(sets) & {"target_drtn_hr_cnt", "remain_drtn_hr_cnt", "cstr_type", "cstr_date",
                             "act_start_date", "act_end_date", "status_code"}  # name and physical % do not move dates
        changed = [k for k in sets if k not in ("update_date", "update_user")]
        log = [{"operation": "update_activity", "proj_id": t["proj_id"], "table": "TASK",
                "keys": {"task_id": t["task_id"], "task_code": task_code},
                "before": {k: t[k] for k in changed}, "after": {k: sets[k] for k in changed}, "summary": plan[0]}]
        return _commit(con, plan, confirm, projects=(t["proj_id"],) if sched else (), log=log)


# ----------------------------------------------------------------- resource assignment units

FIXED_DRTN_TYPES = ("DT_FixedDrtn", "DT_FixedDUR2")  # P6 keeps the duration when units change: units/time follow
ASG_QTY = (("budgeted_units", "target_qty"), ("actual_units", "act_reg_qty"), ("remaining_units", "remain_qty"))
ASG_COST = {"target_qty": "target_cost", "act_reg_qty": "act_reg_cost", "remain_qty": "remain_cost"}
ASG_RATE = {"target_qty": ("target_qty_per_hr", "target_drtn_hr_cnt"),
            "remain_qty": ("remain_qty_per_hr", "remain_drtn_hr_cnt")}
ASG_TASK = {"target_qty": "target", "act_reg_qty": "act", "remain_qty": "remain"}  # TASK.<x>_work_qty / <x>_equip_qty
ASG_TASK_KIND = {"RT_Labor": "work", "RT_Equip": "equip"}  # material units are not rolled up to the activity


class AsgEdit(TypedDict, total=False):
    """An assignment (taskrsrc_id, or task_code with resource or role) and the units to set."""
    task_code: str
    resource: str
    role: str
    taskrsrc_id: int
    budgeted_units: float
    actual_units: float
    remaining_units: float
    at_completion_units: float


AsgEdits = Annotated[list[AsgEdit], Field(min_length=1, description="Assignments to change.")]
ASG_KEYS = set(AsgEdit.__annotations__)


def _asg_find(con, project: str, it: Mapping) -> tuple[sqlite3.Row, sqlite3.Row, str]:
    """(TASKRSRC row with rsrc_short_name and role_short_name, its activity, label) of an assignment item."""
    al = functools.partial(_alive, con)
    has_role = bool(_cols(con, "ROLES")) and "role_id" in _cols(con, "TASKRSRC")
    sql = ("SELECT a.*, r.rsrc_short_name AS rsrc_short_name, "
           + ("ro.role_short_name" if has_role else "NULL") + " AS role_short_name FROM TASKRSRC a "
           "LEFT JOIN RSRC r ON r.rsrc_id = a.rsrc_id" + al("RSRC", "r")
           + (" LEFT JOIN ROLES ro ON ro.role_id = a.role_id" + al("ROLES", "ro") if has_role else ""))
    label = lambda a: (a["rsrc_short_name"] or f"role {a['role_short_name'] or '?'}") + f" (taskrsrc_id {a['taskrsrc_id']})"  # noqa: E731
    tid = it.get("taskrsrc_id")
    if tid is not None:
        if isinstance(tid, bool) or not isinstance(tid, int):
            raise ValueError(f"taskrsrc_id must be an integer, not {tid!r}")
        a = con.execute(sql + " WHERE a.taskrsrc_id = ?" + al("TASKRSRC", "a"), (tid,)).fetchone()
        if a is None:
            raise ValueError(f"Assignment taskrsrc_id {tid} not found")
        t = _task_by_id(con, a["task_id"])
        if t["proj_id"] != _project_id(con, project) or it.get("task_code") not in (None, t["task_code"]):
            raise ValueError(f"taskrsrc_id {tid} is an assignment of {t['proj_short_name']} {t['task_code']}, "
                             f"not of {project} {it.get('task_code') or ''}".rstrip())
        return a, t, label(a)
    if not isinstance(it.get("task_code"), str) or not it["task_code"]:
        raise ValueError("task_code (with resource or role) or taskrsrc_id is required")
    t = _task(con, project, it["task_code"])
    rows = con.execute(sql + " WHERE a.task_id = ?" + al("TASKRSRC", "a") + " ORDER BY a.taskrsrc_id",
                       (t["task_id"],)).fetchall()
    if not rows:
        raise ValueError(f"{t['task_code']} has no resource assignment")
    if not it.get("resource") and not it.get("role"):
        raise ValueError(f"resource or role is required; assignments of {t['task_code']}: {[label(a) for a in rows]}")
    hits = rows
    for key, col in (("resource", "rsrc_short_name"), ("role", "role_short_name")):
        if it.get(key):
            want = str(it[key])
            exact = [a for a in hits if a[col] == want]
            hits = exact or [a for a in hits if (a[col] or "").casefold() == want.casefold()]
    if len(hits) != 1:
        raise ValueError(f"{'No' if not hits else 'Several'} assignment(s) of {t['task_code']} match "
                         f"{ {k: it[k] for k in ('resource', 'role') if it.get(k)} }: "
                         f"{[label(a) for a in (hits or rows)]}" + (" (pass taskrsrc_id)" if hits else ""))
    return hits[0], t, label(hits[0])


def _asg_update(con, project: str, it: Mapping) -> dict:
    """Set the budgeted, actual and/or remaining (or at completion) units of one assignment, with P6's
    recalculation: linked costs, units/time (fixed duration), remaining = budget for a not started
    activity when the project links them, the activity's labor / nonlabor totals."""
    bad = sorted(set(it) - ASG_KEYS)
    if bad:
        raise ValueError(f"unknown key(s) {bad}; allowed: {sorted(ASG_KEYS)}")
    vals: dict[str, float] = {}
    for k in ("budgeted_units", "actual_units", "remaining_units", "at_completion_units"):
        if it.get(k) is not None:
            f = None if isinstance(it[k], bool) else _num(it[k])
            if f is None or f < 0:
                raise ValueError(f"{k} must be a number, 0 or more (not {it[k]!r})")
            vals[k] = f
    if "remaining_units" in vals and "at_completion_units" in vals:
        raise ValueError("Give remaining_units or at_completion_units (= actual + remaining), not both.")
    if not vals:
        raise _Refused("Nothing to change: give budgeted_units, actual_units, remaining_units or at_completion_units.")
    a, t, label = _asg_find(con, project, it)
    code, st = t["task_code"], t["status_code"]
    dt = t["duration_type"] if "duration_type" in _cols(con, "TASK") else None
    if dt is not None and dt not in FIXED_DRTN_TYPES:
        raise ValueError(f"{code} has duration type {dt}: P6 would recompute its duration from the units, and this "
                         "tool never changes durations. Only Fixed Duration types are handled; edit it in P6.")
    acols = _cols(con, "TASKRSRC")
    old = {c: float(a[c] or 0) for _, c in ASG_QTY}
    ot = float(a["act_ot_qty"] or 0) if "act_ot_qty" in acols else 0.0
    new = dict(old)
    notes: list[str] = []
    if "actual_units" in vals:
        if st == "TK_NotStart" and vals["actual_units"] > 0:
            raise ValueError(f"{code} is not started: set its actual start first (p6_update_activity), then actual units.")
        if vals["actual_units"] < ot:
            raise ValueError(f"actual_units {vals['actual_units']:g} is less than the overtime actual {ot:g}")
        new["act_reg_qty"] = vals["actual_units"] - ot
    actual = new["act_reg_qty"] + ot
    if "at_completion_units" in vals:
        if vals["at_completion_units"] < actual:
            raise ValueError(f"at_completion_units {vals['at_completion_units']:g} is less than the actual units {actual:g}")
        new["remain_qty"] = vals["at_completion_units"] - actual
    if "remaining_units" in vals:
        new["remain_qty"] = vals["remaining_units"]
    if st == "TK_Complete" and new["remain_qty"] > 0:
        raise ValueError(f"{code} is completed: its remaining units stay 0 (at completion = actual {actual:g}).")
    if "budgeted_units" in vals:
        new["target_qty"] = vals["budgeted_units"]
    link = None
    if st == "TK_NotStart" and "rem_target_link_flag" in _cols(con, "PROJECT"):
        link = con.execute("SELECT rem_target_link_flag FROM PROJECT WHERE proj_id = ?", (t["proj_id"],)).fetchone()[0]
    if link == "Y":  # P6 option "Link Budget and At Completion for not started activities"
        given_rem = "remaining_units" in vals or "at_completion_units" in vals
        if "budgeted_units" in vals and given_rem and abs(new["remain_qty"] - new["target_qty"]) > 1e-9:
            raise ValueError(f"{code} is not started and its project links budget and at completion units: give one "
                             f"value (budget {new['target_qty']:g} and at completion {new['remain_qty']:g} differ).")
        if "budgeted_units" in vals and not given_rem:
            new["remain_qty"] = new["target_qty"]
            notes.append("remaining follows the budget (not started, budget and at completion linked)")
        elif given_rem and "budgeted_units" not in vals:
            new["target_qty"] = new["remain_qty"]
            notes.append("budget follows at completion (not started, budget and at completion linked)")
    changed = [c for _, c in ASG_QTY if abs(new[c] - old[c]) > 1e-9]
    if not changed:
        raise _Refused(f"Nothing to change: {code} {label} already has these units.")
    sets: dict[str, Any] = {c: new[c] for c in changed}
    price = a["cost_per_qty"] if "cost_per_qty" in acols else None
    linked = "cost_qty_link_flag" in acols and a["cost_qty_link_flag"] == "Y" and price is not None
    if linked:
        sets.update({ASG_COST[c]: new[c] * float(price) for c in changed if ASG_COST[c] in acols})
    elif any(ASG_COST[c] in acols for c in changed):
        notes.append("costs unchanged (cost not linked to units on this assignment)")
    for c in changed:
        rate, drtn = ASG_RATE.get(c, (None, None))
        if rate in acols:  # fixed duration: the units/time absorb the change (resource calendar hours kept)
            hrs = float(t[drtn] or 0)
            sets[rate] = (float(a[rate]) * new[c] / old[c] if old[c] > 0 and a[rate] is not None
                          else new[c] / hrs if hrs > 0 else 0.0)
    kind = ASG_TASK_KIND.get(a["rsrc_type"]) if "rsrc_type" in acols else None
    tsets = {}
    for c in changed:
        col = f"{ASG_TASK[c]}_{kind}_qty"
        if kind and col in _cols(con, "TASK"):
            tsets[col] = float(t[col] or 0) + new[c] - old[c]
    if not kind:
        notes.append(f"resource type {a['rsrc_type']}: not rolled up to the activity's labor or nonlabor units")
    now = _now_str(con)
    full = {**sets, **_stamp_cols(con, "TASKRSRC", now, create=False)}
    con.execute(f"UPDATE TASKRSRC SET {', '.join(k + '=?' for k in full)} WHERE taskrsrc_id=?",
                [*full.values(), a["taskrsrc_id"]])
    if tsets:
        full = {**tsets, **_stamp_cols(con, "TASK", now, create=False)}
        con.execute(f"UPDATE TASK SET {', '.join(k + '=?' for k in full)} WHERE task_id=?", [*full.values(), t["task_id"]])
    names = {"target_qty": "budget", "act_reg_qty": "actual", "remain_qty": "remaining"}
    fmt = lambda v: f"{v:.6g}"  # noqa: E731
    parts = [f"{names[c]} {fmt(old[c] + (ot if c == 'act_reg_qty' else 0))} -> {fmt(new[c] + (ot if c == 'act_reg_qty' else 0))}"
             for c in changed]
    parts.append(f"at completion {fmt(old['act_reg_qty'] + ot + old['remain_qty'])} -> {fmt(actual + new['remain_qty'])}")
    costs = [f"{ASG_COST[c].replace('_', ' ')} {fmt(float(a[ASG_COST[c]] or 0))} -> {fmt(sets[ASG_COST[c]])}"
             for c in changed if ASG_COST[c] in sets]
    if costs:
        parts.append(f"{', '.join(costs)} ({fmt(float(price))}/unit)")
    plan = f"{code} {label}: " + ", ".join(parts) + "".join(f"; {n}" for n in notes)
    view = lambda q: {"budgeted_units": q["target_qty"], "actual_units": q["act_reg_qty"] + ot,  # noqa: E731
                      "remaining_units": q["remain_qty"], "at_completion_units": q["act_reg_qty"] + ot + q["remain_qty"]}
    log = [{"operation": "update_assignment", "proj_id": t["proj_id"], "table": "TASKRSRC",
            "keys": {"taskrsrc_id": a["taskrsrc_id"], "task_id": t["task_id"], "task_code": code,
                     "resource": a["rsrc_short_name"], "role": a["role_short_name"]},
            "before": {**{k: a[k] for k in sets}, **{f"TASK.{k}": t[k] for k in tsets}},
            "after": {**sets, **{f"TASK.{k}": v for k, v in tsets.items()}}, "summary": plan}]
    return {"plan": plan, "projects": [], "log": log,
            "assignment": {"taskrsrc_id": a["taskrsrc_id"], "task_code": code, "resource": a["rsrc_short_name"],
                           "role": a["role_short_name"], "before": view(old), "after": view(new)}}


@_tool(read_only=False, destructive=True)
def p6_update_assignment(
    project: ProjectId, task_code: Optional[ActivityId] = None, resource: Optional[str] = None,
    role: Optional[str] = None, taskrsrc_id: Optional[int] = None,
    budgeted_units: Optional[NonNegQty] = None, actual_units: Optional[NonNegQty] = None,
    remaining_units: Optional[NonNegQty] = None, at_completion_units: Optional[NonNegQty] = None,
    confirm: bool = False,
) -> dict:
    """Set the units of one resource assignment, as P6 recalculates them. Pick it by task_code plus
    resource (Resource ID) or role, or by taskrsrc_id (p6_get_activity assignments). at_completion_units
    = actual + remaining (sets remaining; not with remaining_units). Rules: actual units need a started
    activity, a completed one keeps 0 remaining; a not started activity whose project links budget and
    at completion keeps them equal (one value moves both); costs = units x price/unit when the cost is
    linked to units; units/time follow (Fixed Duration types only: durations never change); the
    activity's labor / nonlabor unit totals follow. Dates are not affected. Dry run unless confirm=True."""
    return _single_link(_asg_update, project, {k: v for k, v in (
        ("task_code", task_code), ("resource", resource), ("role", role), ("taskrsrc_id", taskrsrc_id),
        ("budgeted_units", budgeted_units), ("actual_units", actual_units), ("remaining_units", remaining_units),
        ("at_completion_units", at_completion_units)) if v is not None}, confirm)


@_tool(read_only=False, destructive=True)
def p6_update_assignments(project: ProjectId, items: AsgEdits, confirm: bool = False) -> dict:
    """Set the units of many assignments in one transaction (all or nothing; dry run unless
    confirm=True). Item: {task_code, resource, role, taskrsrc_id, budgeted_units, actual_units,
    remaining_units, at_completion_units}, as p6_update_assignment. items: one report per item
    (assignment before / after, or the error). One edit log entry per assignment."""
    items = _items_arg(items)
    with _write_session(confirm) as (con, backup):
        _project_id(con, project)

        def apply_one(i: int, it) -> dict:
            if not isinstance(it, Mapping):
                raise ValueError("an item must be an object")
            return _asg_update(con, project, it)

        b = _batch(con, items, apply_one)
        for rep, it in zip(b["items"], items):
            rep["item"] = it
        return _batch_commit(con, b, confirm, f"Update units of {len(items)} assignment(s)", backup)


# ----------------------------------------------------------------- activity UDF values

class _UdfEditKeys(TypedDict):
    task_code: str
    udf: str


class UdfEdit(_UdfEditKeys, total=False):
    """An activity UDF value to set (value) or remove (clear=True)."""
    value: Union[str, float]
    clear: bool


UdfEdits = Annotated[list[UdfEdit], Field(min_length=1, description="Activity UDF values to set or clear.")]
UDF_KEYS = set(UdfEdit.__required_keys__) | set(UdfEdit.__optional_keys__)


def _udf_store(con, y: sqlite3.Row, value: Any) -> tuple[str, Any, Any, list[str]]:
    """(UDFVALUE column, value to store, value as shown, notes) of a user value for UDF type y."""
    lt, label = y["logical_data_type"], y["udf_type_label"]
    kind = UDF_KINDS.get(lt)
    if kind is None:
        raise ValueError(f"{label!r}: UDF data type {lt} is not supported")
    if kind == "text":
        if not isinstance(value, str):
            raise ValueError(f"{label!r} is a text UDF: give a string, not {value!r}")
        if len(value) > UDF_TEXT_MAX:
            raise ValueError(f"{label!r}: text longer than {UDF_TEXT_MAX} characters ({len(value)})")
        return "udf_text", value, value, []
    if kind == "indicator":
        v = value.strip().upper() if isinstance(value, str) else None
        if not v or not UDF_INDICATOR.fullmatch(v):
            raise ValueError(f"{label!r} is an indicator UDF: give UDF_G0 to UDF_G4 as P6 stores the icons, not {value!r}")
        return "udf_text", v, v, []
    if kind in ("start_date", "finish_date"):
        if not isinstance(value, str):
            raise ValueError(f"{label!r} is a date UDF: give 'YYYY-MM-DD[ HH:MM]', not {value!r}")
        s = _fmt_dt(_parse_dt(value, label)[0])
        return "udf_date", s, s, []
    if kind == "code":
        if "udf_code_id" not in _cols(con, "UDFVALUE") or "udf_type_id" not in _cols(con, "UDFCODE"):
            raise ValueError(f"{label!r} is a code UDF and this database has no UDFCODE table")
        rows = con.execute("SELECT udf_code_id, short_name, udf_code_name FROM UDFCODE WHERE udf_type_id = ?"
                           + _alive(con, "UDFCODE") + " ORDER BY udf_code_id", (y["udf_type_id"],)).fetchall()
        v = str(value).strip()
        hit = ([r for r in rows if r["short_name"] == v] or [r for r in rows if (r["short_name"] or "").casefold() == v.casefold()]
               or [r for r in rows if (r["udf_code_name"] or "").casefold() == v.casefold()])
        if len(hit) != 1:
            raise ValueError(f"{label!r}: {'several codes match' if hit else 'no code'} {value!r}; codes: "
                             f"{[r['short_name'] for r in (hit or rows)][:30]}")
        return "udf_code_id", hit[0]["udf_code_id"], hit[0]["short_name"], []
    f = None if isinstance(value, bool) else _num(value)
    if f is None:
        raise ValueError(f"{label!r} is a{'n' if kind == 'integer' else ''} {kind} UDF: give a number, not {value!r}")
    if kind == "integer":
        if not f.is_integer():
            raise ValueError(f"{label!r} is an integer UDF: {value!r} is not a whole number")
        return "udf_number", f, int(f), []
    if kind == "number" and round(f, 2) != f:
        return "udf_number", round(f, 2), round(f, 2), [f"rounded to 2 decimals as P6 stores it ({f!r})"]
    return "udf_number", f, f, []


def _udf_shown(con, y: sqlite3.Row, r: Optional[sqlite3.Row]) -> Any:
    """Value of a UDFVALUE row as shown (code short name), None for no row."""
    if r is None:
        return None
    lt = y["logical_data_type"]
    if lt == "FT_CODE":
        if r["udf_code_id"] is None or "udf_type_id" not in _cols(con, "UDFCODE"):
            return None
        c = con.execute("SELECT short_name FROM UDFCODE WHERE udf_code_id = ?", (r["udf_code_id"],)).fetchone()
        return c[0] if c else f"udf_code_id {r['udf_code_id']}"
    return _udf_out(lt, r[UDF_VALUE.get(lt, "udf_text")])


def _udf_set(con, project: str, it: Mapping) -> dict:
    """Set or clear one activity UDF value (UDFVALUE row of udf_type_id + task_id): insert, update (a
    soft-deleted row is revived) or delete (hard, as P6; its trigger logs REFRDEL)."""
    bad = sorted(set(it) - UDF_KEYS)
    if bad:
        raise ValueError(f"unknown key(s) {bad}; allowed: {sorted(UDF_KEYS)}")
    if not isinstance(it.get("task_code"), str) or not it["task_code"]:
        raise ValueError("task_code is required")
    clear = it.get("clear", False)
    if not isinstance(clear, bool):
        raise ValueError("clear must be true or false")
    value = it.get("value")
    if clear and value is not None:
        raise ValueError("Give value or clear=True, not both.")
    if not clear and (value is None or value == ""):
        raise ValueError("value is required (clear=True removes the value)")
    t = _task(con, project, it["task_code"])
    y = _udf_type(con, "TASK", it.get("udf"))
    label, code, tid, uid = y["udf_type_label"], t["task_code"], t["task_id"], y["udf_type_id"]
    if "formula" in _cols(con, "UDFTYPE") and y["formula"]:
        raise ValueError(f"{label!r} is a formula UDF ({y['formula']}): P6 computes its value")
    vcols = _cols(con, "UDFVALUE")
    old = con.execute("SELECT * FROM UDFVALUE WHERE udf_type_id = ? AND fk_id = ?", (uid, tid)).fetchone()
    dead = old is not None and "delete_session_id" in vcols and old["delete_session_id"] is not None
    live = None if dead else old
    before = _udf_shown(con, y, live)
    keys = {"udf_type_id": uid, "fk_id": tid, "task_code": code, "udf": label}
    now = _now_str(con)
    if clear:
        if live is None:
            raise _Refused(f"Nothing to clear: {code} has no {label!r} value.")
        col = "udf_code_id" if y["logical_data_type"] == "FT_CODE" else UDF_VALUE.get(y["logical_data_type"], "udf_text")
        con.execute("DELETE FROM UDFVALUE WHERE udf_type_id = ? AND fk_id = ?", (uid, tid))
        plan = f"{code} {label!r}: {before!r} -> cleared"
        log = [{"operation": "clear_udf", "proj_id": t["proj_id"], "table": "UDFVALUE", "keys": keys,
                "before": {col: live[col]}, "after": None, "summary": plan}]
        after = None
    else:
        col, stored, after, notes = _udf_store(con, y, value)
        if live is not None:
            cur = live[col]
            same = (cur is not None and abs(float(cur) - float(stored)) <= 1e-9 if col == "udf_number"
                    else _db_dt(cur) == _db_dt(stored) if col == "udf_date" else cur == stored)
            if same:
                raise _Refused(f"Nothing to change: {code} {label!r} is already {after!r}.")
        if old is not None:
            sets = {col: stored, "proj_id": t["proj_id"], **_stamp_cols(con, "UDFVALUE", now, create=False)}
            if dead:
                sets.update({c: None for c in ("delete_session_id", "delete_date") if c in vcols})
            con.execute(f"UPDATE UDFVALUE SET {', '.join(k + ' = ?' for k in sets)} WHERE udf_type_id = ? AND fk_id = ?",
                        [*sets.values(), uid, tid])
        else:
            row = {"udf_type_id": uid, "fk_id": tid, "proj_id": t["proj_id"], col: stored,
                   **_stamp_cols(con, "UDFVALUE", now, create=True)}
            con.execute(f"INSERT INTO UDFVALUE ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        plan = f"{code} {label!r}: {before!r} -> {after!r}" + "".join(f"; {n}" for n in notes)
        log = [{"operation": "update_udf", "proj_id": t["proj_id"], "table": "UDFVALUE", "keys": keys,
                "before": None if live is None else {col: live[col]}, "after": {col: stored}, "summary": plan}]
    return {"plan": plan, "projects": [], "log": log,
            "udf": {"task_code": code, "udf": label, "data_type": UDF_KINDS.get(y["logical_data_type"]),
                    "before": before, "after": after}}


def _udf_next(res: dict) -> dict:
    if res.get("applied"):
        res["next"] = "Open P6 to see the values (UDFs do not move dates: no F9 needed)."
    return res


@_tool(read_only=False, destructive=True)
def p6_update_udf(project: ProjectId, task_code: ActivityId, udf: str, value: Optional[UdfValue] = None,
                  clear: bool = False, confirm: bool = False) -> dict:
    """Set one activity UDF value (a 'User Defined' column of P6, labels in p6_list_udfs), or remove it
    with clear=True. value per data type: text (255 characters max), number (2 decimals), integer,
    cost, start_date / finish_date 'YYYY-MM-DD[ HH:MM]' (a date alone is stored at 00:00), indicator
    UDF_G0..UDF_G4 (as P6 stores the icons), code (short name or name of a UDF code). Formula UDFs
    are refused (P6 computes them). Dates and float are not affected (no F9 needed). Dry run unless
    confirm=True."""
    return _udf_next(_single_link(_udf_set, project, {"task_code": task_code, "udf": udf, "clear": bool(clear),
                                                      **({"value": value} if value is not None else {})}, confirm))


@_tool(read_only=False, destructive=True)
def p6_update_udfs(project: ProjectId, items: UdfEdits, confirm: bool = False) -> dict:
    """Set or clear many activity UDF values in one transaction (all or nothing; dry run unless
    confirm=True). Item: {task_code, udf, value} or {task_code, udf, clear: true}, as p6_update_udf.
    items: one report per item (udf before / after, or the error). One edit log entry per value."""
    items = _items_arg(items)
    with _write_session(confirm) as (con, backup):
        _project_id(con, project)

        def apply_one(i: int, it) -> dict:
            if not isinstance(it, Mapping):
                raise ValueError("an item must be an object")
            return _udf_set(con, project, it)

        b = _batch(con, items, apply_one)
        for rep, it in zip(b["items"], items):
            rep["item"] = it
        return _udf_next(_batch_commit(con, b, confirm, f"Set {len(items)} activity UDF value(s) in {project}", backup))


# ----------------------------------------------------------------- delete one activity

# Rows that only point at an activity (an issue raised on it): kept, the link cleared. Every other
# table with a task_id, pred_task_id or taskrsrc_id column holds rows of the activity and loses them.
ACT_DEL_CLEAR = (("PROJISSU", "task_id"),)
ACT_DEL_ORDER = ("TASKACTV", "UDFVALUE", "TASKRSRC", "TASKPRED", "TASK")  # children first, as DEL_ORDER
ACT_DEL_OWN = {"task_id": "_dt", "pred_task_id": "_dt", "taskrsrc_id": "_dr"}  # as DEL_OWN, activities only
ACT_DEL_UDF = {"TASK": "_dt", "TASKRSRC": "_dr"}
ACT_SNAPSHOT = ("task_code", "task_name", "task_type", "status_code", "wbs_id", "clndr_id", "target_drtn_hr_cnt",
                "remain_drtn_hr_cnt", "cstr_type", "cstr_date", "act_start_date", "act_end_date", "phys_complete_pct")


def _act_del_where(con, t: str) -> list[str]:
    return _owned_where(con, t, ACT_DEL_OWN, ACT_DEL_UDF)


def _act_delete_rows(con, task_ids: Iterable[int]) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Hard-delete activities and every row of the schema that belongs to them: rows carrying their
    task_id or pred_task_id (links both ways, codes, notebooks, steps, expenses, documents, risks...),
    their assignments and the assignments' rows (taskrsrc_id: period actuals, timesheet hours, codes),
    UDF values of both, and the read marks of their discussions. ACT_DEL_CLEAR rows stay with the link
    cleared. Returns (deleted per table, cleared per column, rows still naming them: must be empty)."""
    _temp_id_sets(con, ("_dt", "_dr"))
    con.executemany("INSERT OR IGNORE INTO temp._dt VALUES (?)", [(int(i),) for i in task_ids])
    con.execute("INSERT OR IGNORE INTO temp._dr SELECT taskrsrc_id FROM TASKRSRC WHERE task_id IN (SELECT id FROM temp._dt)")
    cleared, counts = {}, {}
    for t, c in ACT_DEL_CLEAR:
        if c in _cols(con, t):
            n = con.execute(f"UPDATE {t} SET {c} = NULL WHERE {c} IN (SELECT id FROM temp._dt)").rowcount
            if n:
                cleared[f"{t}.{c}"] = n
    if "discussion_id" in _cols(con, "DISCUSSION_READ") and {"discussion_id", "task_id"} <= _cols(con, "DISCUSSION"):
        n = con.execute("DELETE FROM DISCUSSION_READ WHERE discussion_id IN (SELECT discussion_id FROM DISCUSSION "
                        "WHERE task_id IN (SELECT id FROM temp._dt))").rowcount
        if n:
            counts["DISCUSSION_READ"] = n
    tables = _db_tables(con)
    for t in [t for t in tables if t not in ACT_DEL_ORDER] + [t for t in ACT_DEL_ORDER if t in tables]:
        conds = _act_del_where(con, t)
        if conds:
            n = con.execute(f"DELETE FROM {t} WHERE {' OR '.join(conds)}").rowcount
            if n:
                counts[t] = n
    left = {}
    for t in tables:
        conds = _act_del_where(con, t)
        if conds:
            n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE {' OR '.join(conds)}").fetchone()[0]
            if n:
                left[t] = n
    return counts, cleared, left


@_tool(read_only=False, destructive=True)
def p6_delete_activity(project: ProjectId, task_code: ActivityId, force: bool = False, confirm: bool = False) -> dict:
    """Delete one activity and everything that belongs to it, as P6 does (hard delete; P6's triggers
    log REFRDEL): relationships both ways, resource assignments (with their period actuals, timesheet
    hours and codes), activity code assignments, UDF values, notebooks, steps, expenses, documents,
    risks, feedback, discussions and any other table of the schema with its task_id; an issue raised
    on it is kept with the activity cleared. Dry run unless confirm=True: lists what would be removed
    (would_remove: every relationship with type and lag; assignments, codes, UDFs, notebooks first 50;
    steps and expenses), rows per table (counts) and side_effects (open activities left without any
    predecessor or successor). Refused when the activity has an actual start or finish unless
    force=True (the dry run says would_refuse). Activities linked to it need F9."""
    con = _connect()
    try:
        t = _task(con, project, task_code)
        actual = {k: t[k] for k in ("act_start_date", "act_end_date") if t[k]}
    finally:
        con.close()
    reasons = ([f"{task_code} has actual dates {actual}: progress would be lost. Pass force=True to delete it anyway."]
               if actual and not force else [])
    if reasons and confirm:
        raise ValueError("Delete refused (nothing written). " + reasons[0])
    with _write_session(confirm) as (con, backup):
        t = _task(con, project, task_code)
        actual = {k: t[k] for k in ("act_start_date", "act_end_date") if t[k]}  # again, under the write lock
        if actual and not force and not reasons:
            reasons = [f"{task_code} has actual dates {actual}: progress would be lost. Pass force=True to delete it anyway."]
            if confirm:
                raise ValueError("Delete refused (nothing written). " + reasons[0])
        tid, pid = t["task_id"], t["proj_id"]
        links = [_rel_row_fn(con, pid)(r) for r in con.execute(_rel_sql(con, "both"), {"tid": tid})]
        ends = con.execute("SELECT task_id, pred_task_id FROM TASKPRED WHERE (task_id = ? OR pred_task_id = ?)"
                           + _alive(con, "TASKPRED"), (tid, tid)).fetchall()
        lost_pred = {r[0] for r in ends if r[1] == tid}  # its successors lose a predecessor
        lost_succ = {r[1] for r in ends if r[0] == tid}
        other_pids = {r[0] for r in con.execute(
            f"SELECT proj_id FROM TASK WHERE task_id IN ({','.join('?' * len(lost_pred | lost_succ)) or 'NULL'})",
            sorted(lost_pred | lost_succ))}
        details, cut = _act_details(con, tid)
        steps = ([r[0] for r in con.execute("SELECT proc_name FROM TASKPROC WHERE task_id = ?" + _alive(con, "TASKPROC")
                                            + " ORDER BY seq_num, proc_id", (tid,))]
                 if {"proc_name", "seq_num", "proc_id"} <= _cols(con, "TASKPROC") else [])
        expenses = ([{"name": r[0], "target_cost": r[1], "actual_cost": r[2]} for r in con.execute(
                     "SELECT cost_name, target_cost, act_cost FROM PROJCOST WHERE task_id = ?" + _alive(con, "PROJCOST")
                     + " ORDER BY cost_item_id", (tid,))]
                    if {"cost_name", "target_cost", "act_cost", "cost_item_id"} <= _cols(con, "PROJCOST") else [])
        warnings = []
        if _sched_options(con, pid)[0].get("key_activity_for_multiple_longest_paths") == str(tid):
            warnings.append("It is the key activity of the project's schedule options (multiple float paths): "
                            "pick another one in P6 or with p6_set_schedule_options.")
        snap = {k: t[k] for k in ACT_SNAPSHOT if k in _cols(con, "TASK")}
        counts, cleared, left = _act_delete_rows(con, [tid])
        if left:
            raise RuntimeError(f"Rows still naming the activity after the delete: {left} (nothing written)")
        side = _side_effects(con, lost_pred, lost_succ, gone={tid})
        npred = sum(1 for x in links if x["direction"] == "predecessor")
        plan = [f"Delete activity {task_code} '{t['task_name']}' ({TYPE_NAMES.get(t['task_type'], t['task_type'])}, "
                f"{STATUS_NAMES.get(t['status_code'], t['status_code'])}) from {project}",
                f"Relationships removed: {len(links)} ({npred} predecessors, {len(links) - npred} successors)",
                "Rows deleted: " + ", ".join(f"{k} {n}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))]
        if cleared:
            plan.append("Kept, activity cleared: " + ", ".join(f"{k} {n}" for k, n in cleared.items()))
        if actual:
            plan.append(f"Actual dates {actual} deleted with it (force=True)" if force else f"Note: actual dates {actual}")
        if side["count"]:
            plan.append(f"Left without predecessor: {len(side['no_predecessor'])}, without successor: "
                        f"{len(side['no_successor'])} (side_effects)")
        if backup: plan.append(f"Backup: {backup}")
        log = [{"operation": "delete_activity", "proj_id": pid, "table": "TASK",
                "keys": {"task_id": tid, "task_code": task_code},
                "before": {"task": snap, "rows": counts, "cleared": cleared,
                           "relationships": [{k: x[k] for k in ("direction", "task_pred_id", "other_code", "other_project",
                                                                "type", "lag_h")} for x in links]},
                "after": None, "summary": plan[0]}]
        res = _commit(con, plan, confirm, projects={pid} | other_pids, log=log)
        res.update(activity={"task_code": task_code, "task_name": t["task_name"], "task_type": t["task_type"],
                             "status_code": t["status_code"], "actual": actual or None},
                   would_remove={"relationships": links, **details, "steps": steps, "expenses": expenses},
                   counts={k: {"deleted": n} for k, n in counts.items()}, cleared=cleared, side_effects=side,
                   warnings=warnings)
        if cut:
            res["details_note"] = f"Only the first {ACT_LINKS_CAP} rows listed of: {', '.join(cut)} (counts has all)."
        if not confirm:
            res.update(would_refuse=bool(reasons), refuse_reasons=reasons)
            if reasons:
                res["next"] = "confirm=True would be refused: " + reasons[0]
        return res


@_tool(read_only=False, destructive=False, idempotent=False)
def p6_backup_database() -> dict:
    """Take a manual backup copy of the P6 database now (kept apart from the automatic retention)."""
    why = _write_refusal()
    if why:
        raise RuntimeError(why)
    return {"backup": _backup("manual")}


# ----------------------------------------------------------------- batch writes

def _batch(con, items: Sequence, apply_one: Callable[[int, Any], dict]) -> dict:
    """Apply apply_one(index, item) to every item inside the open write transaction, each in its own
    savepoint. apply_one changes the database and returns {plan: str, log: [entries], projects:
    [proj_ids], **extra}, or raises ValueError: that item alone is rolled back and reported. Ids of
    inserts come from _next_key in the same transaction. Returns {items (one report per item: index,
    ok, plan or error, extra keys), plan, log, projects, failed}."""
    out: dict[str, Any] = {"items": [], "plan": [], "log": [], "projects": set(), "failed": 0}
    for i, item in enumerate(items):
        con.execute("SAVEPOINT p6_batch_item")
        try:
            r = apply_one(i, item)
        except ValueError as e:
            con.execute("ROLLBACK TO p6_batch_item")
            con.execute("RELEASE p6_batch_item")
            out["items"].append({"index": i, "ok": False, "error": str(e)})
            out["failed"] += 1
            continue
        except BaseException:
            con.execute("ROLLBACK TO p6_batch_item")
            con.execute("RELEASE p6_batch_item")
            raise
        con.execute("RELEASE p6_batch_item")
        extra = {k: v for k, v in r.items() if k not in ("plan", "log", "projects") and not k.startswith("_")}
        out["items"].append({"index": i, "ok": True, "plan": r["plan"], **extra})
        out["plan"].append(r["plan"])
        out["log"] += r.get("log") or []
        out["projects"] |= {p for p in r.get("projects") or () if p is not None}
    return out


def _batch_commit(con, b: dict, confirm: bool, head: str, backup: Optional[str], tail: Sequence[str] = ()) -> dict:
    """All or nothing: commit every item of _batch result b (one edit log entry per item), or, when an
    item failed, nothing (a dry run reports would_refuse; confirm returns applied False). The report
    per item is under items."""
    n, bad = len(b["items"]), b["failed"]
    plan = [head] + [f"  {p}" for p in b["plan"]] + list(tail)
    reasons = [f"{bad} of {n} item(s) cannot be applied (see items): nothing is written"] if bad else []
    if not n:
        reasons = ["no items"]
    if reasons and confirm:
        return {"applied": False, "error": " ".join(reasons) + " (all or nothing).", "items": b["items"]}
    if backup:
        plan.append(f"Backup: {backup}")
    res = _commit(con, plan, confirm, projects=b["projects"], log=b["log"])
    res["items"] = b["items"]
    if not confirm:
        res.update(would_refuse=bool(reasons), refuse_reasons=reasons)
        if reasons:
            res["next"] = "confirm=True would be refused: " + " ".join(reasons)
    return res


# ----------------------------------------------------------------- edit log, backups and restore

BACKUP_NAME = re.compile(r"_(\d{8}_\d{6})(?:_([a-z_]+?))?(?:_\d+)?\.db$", re.I)


def _backup_taken_at(path: str) -> str:
    """Time a backup was taken: the stamp of its file name, else its modification time."""
    m = BACKUP_NAME.search(os.path.basename(path))
    if m:
        return datetime.strptime(m[1], "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")


@_tool()
def p6_edit_log(project: Optional[ProjectId] = None, since: Optional[DateTimeStr] = None,
                offset: Offset = 0, limit: Limit = 50) -> dict:
    """Confirmed writes of this server on this database, newest first, from the persistent edit log
    (p6_mcp_logs/edits_YYYYMMDD.jsonl next to the database, kept across restarts): {total, offset,
    returned, next_offset, rows, log_dir}. Row: ts, tool, operation, project, table, keys (task_id,
    task_pred_id...), before, after (changed values as stored: hours, codes), summary, backup in
    force, commit (one id per confirmed call), stale_projects, undone (made after a backup that
    p6_restore_backup copied back). project= keeps its entries (also where it is a stale project;
    restores are always listed) and adds schedule_state; since='YYYY-MM-DD[ HH:MM]' keeps entries
    at or after it."""
    offset, limit = _check_page(offset, limit)
    lo = _fmt_dt(_parse_dt(since, "since")[0]) if since else None
    es = _log_entries()
    undone = _undone_fn(es)
    state, keep = None, None
    if project:
        con = _connect()
        try:
            pid = _project_id(con, project)
            state = _schedule_state(con, pid)
        except ValueError:
            pid = None  # deleted or renamed: match the name recorded in the log
        finally:
            con.close()
        key = None if pid is None else str(pid)
        keep = lambda e: (e.get("operation") == "restore_backup" or (pid is not None and e.get("proj_id") == pid)  # noqa: E731
                          or (key is not None and key in (e.get("stale") or {}))
                          or e.get("project") == project or project in (e.get("stale_projects") or ()))
    rows = [{**{k: e.get(k) for k in ("ts", "tool", "operation", "project", "table", "keys", "before", "after",
                                      "summary", "backup", "commit", "stale_projects")}, "undone": undone(e)}
            for e in reversed(es) if (keep is None or keep(e)) and (lo is None or e["ts"] >= lo)]
    res = _slice(rows, offset, limit)
    res["log_dir"] = _log_dir()
    if state is not None:
        res["schedule_state"] = state
    return res


@_tool()
def p6_list_backups(offset: Offset = 0, limit: Limit = 200) -> dict:
    """Backup files (.db) in the backup folder, newest first: {total, offset, returned, next_offset,
    rows, folder}. Row: name (pass it to p6_restore_backup), taken_at (from the file name, else the
    file time), size_bytes, size_mb, kind ('backup', 'pre_restore' = safety copy taken by a
    restore), this_database (named after the current database file)."""
    folder = _backup_dir()
    stem = os.path.splitext(os.path.basename(DB_PATH))[0].lower()
    rows = []
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if not name.lower().endswith(".db") or not os.path.isfile(path):
                continue
            m = BACKUP_NAME.search(name)
            size = os.path.getsize(path)
            rows.append({"name": name, "taken_at": _backup_taken_at(path), "size_bytes": size,
                         "size_mb": round(size / 1048576, 1), "kind": (m and m[2]) or "backup",
                         "this_database": name.lower().startswith(stem + "_")})
    rows.sort(key=lambda r: (r["taken_at"], r["name"]), reverse=True)
    res = _slice(rows, offset, limit)
    res["folder"] = folder
    return res


def _backup_file(name: str) -> str:
    """Path of a backup named as p6_list_backups shows it (a bare file name in the backup folder)."""
    if not name or os.path.basename(name) != name or not name.lower().endswith(".db"):
        raise ValueError("name: a .db file name from p6_list_backups (no folder)")
    path = os.path.join(_backup_dir(), name)
    if not os.path.isfile(path):
        raise ValueError(f"Backup not found in {_backup_dir()}: {name}")
    if os.path.abspath(path) == os.path.abspath(DB_PATH):
        raise ValueError("That file is the database itself")
    return path


def _projects_of(path: str) -> dict[str, tuple]:
    """Project ID -> (data date, last schedule date) of a database file (read only), EPS nodes left out."""
    con = sqlite3.connect(_ro_uri(path), uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        tables = {r[0].upper() for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = {"PROJECT", "TASK", "TASKPRED", "PROJWBS"} - tables
        if missing:
            raise ValueError(f"{os.path.basename(path)} is not a P6 database (no {sorted(missing)})")
        cols = {r[1].lower() for r in con.execute("PRAGMA table_info(PROJECT)")}
        sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in ("last_recalc_date", "last_schedule_date"))
        alive = " AND delete_session_id IS NULL" if "delete_session_id" in cols else ""
        return {r[0]: (r[1], r[2]) for r in con.execute(
            f"SELECT proj_short_name, {sel} FROM PROJECT WHERE COALESCE(project_flag, 'Y') = 'Y'{alive}")}
    finally:
        con.close()


@_tool(read_only=False, destructive=True, idempotent=False)
def p6_restore_backup(name: str, confirm: bool = False) -> dict:
    """Replace the database with a backup from the backup folder (name as p6_list_backups shows it).
    Dry run: the backup's projects compared with the current ones (only_in_backup, only_in_database,
    changed data date or last schedule date). confirm=True: refused while P6 is running; checks the
    backup (PRAGMA quick_check), takes a safety backup of the current database (kind pre_restore),
    copies the backup in with the SQLite backup API and logs the restore in the edit log (logged
    edits made after the backup was taken become undone). Afterwards open P6 and check the projects."""
    global _backup_done
    why = _write_refusal()
    if why:
        raise RuntimeError(why)
    path = _backup_file(name)
    taken = _backup_taken_at(path)
    # the copy's end in milliseconds (file time) when it matches the name: a commit made earlier in the same
    # second as the backup is in it, a commit made after it is undone by the restore
    mt = datetime.fromtimestamp(os.path.getmtime(path))
    copied = (mt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
              if timedelta(0) <= mt - datetime.strptime(taken, "%Y-%m-%d %H:%M:%S") < timedelta(minutes=10) else taken)
    cur, old = _projects_of(DB_PATH), _projects_of(path)
    changed = sorted(p for p in set(cur) & set(old) if cur[p] != old[p])
    diff = {"only_in_backup": sorted(set(old) - set(cur))[:50], "only_in_database": sorted(set(cur) - set(old))[:50],
            "changed": [{"project": p, "database": {"data_date": cur[p][0], "last_schedule_date": cur[p][1]},
                         "backup": {"data_date": old[p][0], "last_schedule_date": old[p][1]}} for p in changed[:50]],
            "counts": {"backup_projects": len(old), "database_projects": len(cur),
                       "only_in_backup": len(set(old) - set(cur)), "only_in_database": len(set(cur) - set(old)),
                       "changed": len(changed)}}
    if os.path.splitext(name)[0].lower().startswith(os.path.splitext(os.path.basename(DB_PATH))[0].lower() + "_"):
        warn = []
    else:
        warn = [f"{name} is not named after {os.path.basename(DB_PATH)}: it may come from another database"]
    plan = [f"Restore {name} (taken {taken}, {os.path.getsize(path) / 1048576:.1f} MB) over {DB_PATH}",
            f"Projects: {diff['counts']['only_in_backup']} only in the backup, {diff['counts']['only_in_database']} only "
            f"in the database (lost), {diff['counts']['changed']} with another data date or last schedule date",
            "A safety backup of the current database is taken first."]
    base = {"backup": {"name": name, "taken_at": taken, "size_bytes": os.path.getsize(path)}, "projects": diff,
            "warnings": warn}
    mismatch = _plan_gate(plan, confirm)
    if mismatch:
        return {"applied": False, "error": mismatch, **base}
    if not confirm:
        return {"dry_run": True, "would_do": plan, **base,
                "next": "Call again with confirm=True to restore (P6 must be closed)."}
    err = _guard_write()
    if err:
        raise RuntimeError(err)
    src = sqlite3.connect(_ro_uri(path), uri=True, timeout=5)
    try:
        bad = src.execute("PRAGMA quick_check(5)").fetchall()
        if [r[0] for r in bad] != ["ok"]:
            raise ValueError(f"Backup {name} fails PRAGMA quick_check: {[r[0] for r in bad]} (nothing restored)")
        safety = _backup("pre_restore")
        err = _guard_write()  # P6 may have been opened during the safety copy
        if err:
            raise RuntimeError(err + f" Nothing restored (safety backup {safety} kept).")
        dst = sqlite3.connect(DB_PATH, timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    _backup_done = False  # the next write backs up the restored state
    _cols_cache.clear()
    _schema_cache.clear()
    entry = {"ts": _log_ts(), "commit": uuid.uuid4().hex[:12], "db": os.path.basename(DB_PATH),
             "tool": "p6_restore_backup", "operation": "restore_backup", "project": None, "proj_id": None,
             "table": None, "keys": {"backup": name}, "before": None, "after": None, "backup_taken_at": taken,
             "backup_copied_at": copied,
             "summary": plan[0], "change": plan[0][:160], "backup": safety, "stale": {}, "stale_projects": []}
    res = {"applied": True, "done": plan + [f"Safety backup: {safety}"], **base, "safety_backup": safety,
           "next": "Open P6 and check the projects (press F9 where the data needs it)."}
    try:
        res["edit_log"] = {"file": _log_write([entry]), "entries": 1}
    except OSError as e:
        res["edit_log"] = {"error": f"edit log not written: {e}"}
    return res


# ----------------------------------------------------------------- EPS

def _eps_nodes(con) -> list[dict]:
    return [_dict(r) for r in con.execute(
        "SELECT w.wbs_id AS wbs_id, w.parent_wbs_id AS parent_wbs_id, w.wbs_short_name AS eps_id, "
        "w.wbs_name AS eps_name, w.proj_id AS proj_id "
        "FROM PROJWBS w JOIN PROJECT p ON p.proj_id = w.proj_id "
        "WHERE p.project_flag = 'N' AND w.proj_node_flag = 'Y'"
        + _alive(con, "PROJWBS", "w") + _alive(con, "PROJECT", "p")
        + " ORDER BY w.seq_num, w.wbs_short_name, w.wbs_id")]


def _eps_wbs_id(con, eps: Optional[str]) -> int:
    nodes = _eps_nodes(con)
    if not nodes:
        raise ValueError("No EPS node in this database. Create one in P6 or with p6_create_eps "
                         "(p6_import_xer: create_eps=).")
    if eps is None:
        if len(nodes) == 1:
            return nodes[0]["wbs_id"]
        raise ValueError(f"Several EPS nodes exist, choose one with eps=: {[n['eps_id'] for n in nodes]}")
    for n in nodes:
        if n["eps_id"] == eps:
            return n["wbs_id"]
    raise ValueError(f"EPS {eps!r} not found: {[n['eps_id'] for n in nodes]}")


def _guid() -> str:
    """P6 style GUID: base64 of 16 random bytes, without padding."""
    return base64.b64encode(uuid.uuid4().bytes).decode()[:22]


@_tool()
def p6_list_eps(offset: Offset = 0, limit: Limit = 200) -> dict:
    """EPS nodes, one page: {total, offset, returned, next_offset, rows}. eps_id is the short name
    used by p6_import_xer and p6_create_eps; parent_wbs_id links a node to its parent EPS."""
    return _slice(_eps_nodes(_connect()), offset, limit)


def _create_eps(con, eps_id: str, eps_name: str, parent_wbs: Optional[int], now: str) -> int:
    """Insert an EPS node (PROJECT row with project_flag N plus its PROJWBS node); returns the node wbs_id.
    Refuses a name already used by a project, baseline or EPS node (tools look PROJECT up by name)."""
    if con.execute("SELECT 1 FROM PROJECT WHERE proj_short_name = ?" + _alive(con, "PROJECT"), (eps_id,)).fetchone():
        raise ValueError(f"{eps_id!r} is already used by a project or EPS node: choose another EPS ID.")
    root_obs = con.execute("SELECT obs_id FROM OBS WHERE parent_obs_id IS NULL"
                           + _alive(con, "OBS") + " ORDER BY seq_num, obs_id").fetchone()
    pid = _next_key(con, "PROJECT", "proj_id")
    _insert(con, "PROJECT", {"proj_id": pid, "proj_short_name": eps_id, "project_flag": "N",
                             "guid": _guid(), **_stamp_cols(con, "PROJECT", now, create=True)})
    wid = _next_key(con, "PROJWBS", "wbs_id")
    _insert(con, "PROJWBS", {"wbs_id": wid, "proj_id": pid, "parent_wbs_id": parent_wbs,
                             "obs_id": root_obs[0] if root_obs else None, "seq_num": 0,
                             "proj_node_flag": "Y", "sum_data_flag": "N", "status_code": "WS_Open",
                             "wbs_short_name": eps_id, "wbs_name": eps_name, "guid": _guid(),
                             **_stamp_cols(con, "PROJWBS", now, create=True)})
    return wid


@_tool(read_only=False, destructive=True)
def p6_create_eps(eps_id: str, eps_name: str, parent_eps: Optional[str] = None,
                  confirm: bool = False) -> dict:
    """Create an EPS node (top level, or under parent_eps). Dry run unless confirm=True.
    Check the result in P6 (Enterprise > EPS) after the first use."""
    with _write_session(confirm) as (con, backup):
        if any(n["eps_id"] == eps_id for n in _eps_nodes(con)):
            return {"applied": False, "error": f"EPS {eps_id!r} already exists."}
        if con.execute("SELECT 1 FROM PROJECT WHERE proj_short_name = ?" + _alive(con, "PROJECT"), (eps_id,)).fetchone():
            return {"applied": False, "error": f"{eps_id!r} is already a Project ID: choose another EPS ID."}
        parent = _eps_wbs_id(con, parent_eps) if parent_eps else None
        _create_eps(con, eps_id, eps_name, parent, _now_str(con))
        plan = [f"Create EPS {eps_id} '{eps_name}'" + (f" under {parent_eps}" if parent_eps else " (top level)")]
        if backup: plan.append(f"Backup: {backup}")
        return _commit(con, plan, confirm, log=[{"operation": "create_eps", "table": "PROJECT", "keys": {"eps_id": eps_id},
                                                 "after": {"eps_name": eps_name, "parent_eps": parent_eps},
                                                 "summary": plan[0]}])


# ----------------------------------------------------------------- schedule options (write)

def _task_id_by_code(con, pid: int, code: Optional[str]) -> Optional[int]:
    if not code:
        return None
    r = con.execute("SELECT task_id FROM TASK WHERE proj_id = ? AND task_code = ?" + _alive(con, "TASK"),
                    (pid, code)).fetchone()
    return r[0] if r else None


def _project_row(con, project: str) -> dict:
    """Live PROJECT row of a project (not an EPS node), lowercase keys."""
    r = con.execute("SELECT * FROM PROJECT WHERE proj_short_name = ?" + _alive(con, "PROJECT"), (project,)).fetchone()
    if r is None:
        raise ValueError(f"Project not found: {project}")
    if (_dict(r).get("project_flag") or "Y") != "Y":
        raise ValueError(f"{project} is an EPS node, not a project")
    return _dict(r)


def _opt_rows(con, rows: Iterable[dict]) -> list[dict]:
    """_set_props option rows for the caller: key activity as Activity ID, changed options first."""
    def lab(v):
        if v is None or v in ("", "-99"):
            return None if v is None else "(none)"
        return _db_task_code(con, v) or f"{v} (no such activity)"
    out = [{**r, "old": lab(r["old"]), "new": lab(r["new"])}
           if r["option"] == "key_activity_for_multiple_longest_paths" else r for r in rows]
    return sorted(out, key=lambda r: not r["changed"])


def _sched_write(con, pid: int, xer: Optional[tuple], options: Mapping, key_activity: Optional[str],
                 lpl: Optional[list], now: str) -> tuple[Optional[dict], list[str]]:
    """PROJPROP 'scheduling' of pid from an XER (x, xp, key_map), then explicit options (the key
    activity as an Activity ID). Returns (_set_props result, None when nothing is asked; warnings)."""
    warn, vals = [], {}
    if xer:
        x, xp, key_map = xer
        vals, xlpl, warn = _xer_sched_values(x, xp, key_map)
        lpl = xlpl if lpl is None else lpl
    stored = (_props(con, pid, "scheduling") or {}).get("attrs", {})
    key = "key_activity_for_multiple_longest_paths"
    for k, v in options.items():
        if k == key:
            vals[k] = _key_task(con, pid, v)
        elif k not in SCHED_SPEC and k not in stored:
            raise ValueError(f"Unknown schedule option {k!r}: {sorted(SCHED_SPEC)} (or levelprioritylist)")
        else:
            vals[k] = _norm_opt("scheduling", k, v)[0]
    if key_activity is not None:
        vals[key] = _key_task(con, pid, key_activity)
    if not vals and lpl is None:
        return None, warn
    merged = {**(stored or {k: d for k, (_, d) in SCHED_SPEC.items()}), **vals}
    if merged.get("sched_retained_logic") == "Y" and merged.get("sched_progress_override") == "Y":
        raise ValueError("sched_retained_logic and sched_progress_override cannot both be Y: P6 schedules progressed "
                         "activities with Retained Logic, Progress Override (sched_retained_logic N) or Actual Dates "
                         "(both N)")
    return _set_props(con, pid, "scheduling", vals, lpl, now), warn


def _lpl_arg(level_priority_list) -> Optional[list[tuple[str, str]]]:
    if level_priority_list is None:
        return None
    items = [level_priority_list] if isinstance(level_priority_list, str) else list(level_priority_list)
    lpl = _xer_lpl(";".join(str(i) for i in items), ":")
    if lpl is None:
        raise ValueError(f"level_priority_list: entries 'field:ASC' or 'field:DESC', not {level_priority_list!r}")
    return lpl


@_tool(read_only=False, destructive=True)
def p6_set_schedule_options(project: ProjectId, options: Optional[SchedValues] = None,
                            from_xer: Optional[str] = None, xer_project: Optional[str] = None,
                            key_activity: Optional[str] = None, level_priority_list: Optional[LevelPriority] = None,
                            check_options: Optional[SchedValues] = None, confirm: bool = False) -> dict:
    """Write a project's schedule options (P6 Tools > Schedule > Options, stored in PROJPROP
    'scheduling'). Dry run unless confirm=True; the result lists old and new values, changes first.
    from_xer: take every option from that XER's SCHEDOPTIONS (xer_project picks the project); the
    XER key activity is mapped by Activity ID, -99 (none) with a warning when it has no match.
    options: {name: value} with the names p6_schedule_options shows (Y/N flags, numbers, enumerations
    such as sched_float_type FT_FF/FT_SF/FT_SM, sched_calendar_on_relationship_lag rcal_Predecessor/
    rcal_Successor/rcal_24Hour/rcal_ProjDefault, sched_outer_depend_type SD_Both/SD_None), applied
    after from_xer. key_activity: Activity ID of the key activity for multiple float paths ('none'
    clears it). level_priority_list: leveling priorities, e.g. ['priority_type:ASC'].
    check_options: Check Schedule thresholds (PROJPROP 'schedule_check', read by p6_health_check),
    e.g. {'logic_target': 5, 'check_cpli': 'N', 'large_float_constraint': 352} (limits in hours).
    Options not given keep their stored value (P6 defaults for a project that stores none); keys this
    server does not know are kept. With from_xer, a confirmed run returns verify (the
    p6_schedule_options summary, 0 different expected). Open P6, check the options, then press F9."""
    if all(v is None for v in (options, from_xer, key_activity, level_priority_list, check_options)):
        raise ValueError("give options, from_xer, key_activity, level_priority_list or check_options")
    opts = {str(k).strip().lower(): v for k, v in (options or {}).items()}
    lpl = _lpl_arg(level_priority_list)
    if "levelprioritylist" in opts:
        if lpl is not None:
            raise ValueError("give the level priority list once: level_priority_list or options['levelprioritylist']")
        lpl = _lpl_arg(str(opts.pop("levelprioritylist")).split(";"))
    chk_in = {str(k).strip().lower(): v for k, v in (check_options or {}).items()}
    xer = None
    if from_xer:
        _, x = _xer_read(from_xer)
        xp = _xer_project(x, xer_project)
        if not _xer_sched(x, xp):
            raise ValueError(f"{from_xer} has no SCHEDOPTIONS row for project {xp['proj_short_name']}")
        xer = (x, xp)
    with _write_session(confirm) as (con, backup):
        pid = _project_row(con, project)["proj_id"]
        now = _now_str(con)
        xarg = None
        if xer:
            xcode = {t["task_id"]: t["task_code"] for t in xer[0].get("TASK", []) if t.get("proj_id") == xer[1]["proj_id"]}
            xarg = (*xer, lambda xid: _task_id_by_code(con, pid, xcode.get(xid)))
        sched, warnings = _sched_write(con, pid, xarg, opts, key_activity, lpl, now)
        chk = None
        if chk_in:
            stored = (_props(con, pid, "schedule_check") or {}).get("attrs", {})
            bad = [k for k in chk_in if k not in CHECK_SPEC and k not in stored]
            if bad:
                raise ValueError(f"Unknown check_options {bad}: {sorted(CHECK_SPEC)}")
            chk = _set_props(con, pid, "schedule_check", {k: _norm_opt("schedule_check", k, v)[0]
                                                          for k, v in chk_in.items()}, None, now)
        plan, counts, shown = [], Counter(), {}
        for label, prop, r in (("Schedule options", "scheduling", sched),
                               ("Check Schedule thresholds", "schedule_check", chk)):
            if r is None:
                continue
            counts[r["action"]] += 1
            shown[prop] = _opt_rows(con, r["options"])
            ch = [o for o in shown[prop] if o["changed"]]
            plan.append(f"{label} of {project} (PROJPROP {prop}): {len(ch)} of {len(r['options'])} values change, "
                        f"row {r['action']}")
            plan += [f"  {o['option']}: {o['old']} -> {o['new']}" for o in ch]
        if xer:
            plan.append(f"Source: {os.path.basename(from_xer)} (project {xer[1]['proj_short_name']}), then the "
                        "options given")
        if backup: plan.append(f"Backup: {backup}")
        log = [{"operation": "set_schedule_options", "proj_id": pid, "table": "PROJPROP",
                "keys": {"proj_id": pid, "prop_name": prop},
                "before": {o["option"]: o["old"] for o in rows if o["changed"]},
                "after": {o["option"]: o["new"] for o in rows if o["changed"]},
                "summary": f"PROJPROP {prop}: {sum(o['changed'] for o in rows)} values change"}
               for prop, rows in shown.items()]
        res = _commit(con, plan, confirm, projects=(pid,) if sched and sched["action"] != "unchanged" else (),
                      log=log)
        res.update(counts={"PROJPROP": dict(counts)}, warnings=warnings,
                   options=shown.get("scheduling"), check_options=shown.get("schedule_check"))
        if res.get("applied"):
            if xer:
                res["verify"] = p6_schedule_options(project, from_xer, xer_project)["summary"]
            res["next"] = "Open P6, check Tools > Schedule > Options, then press F9."
        return res


# ----------------------------------------------------------------- delete projects

# A P6 GUI delete (compared on this database, 116 projects and their EPS on 2026-10-19) hard-deletes
# the rows, children first, in this order per project; the AFTER DELETE triggers log each row in
# REFRDEL. Nothing is soft-deleted (delete_session_id stays unused) and NEXTKEY is not touched.
DEL_ORDER = ("TASKACTV", "UDFVALUE", "TASKRSRC", "TASKPRED", "TASK", "ACTVCODE", "ACTVTYPE", "PROJWBS",
             "CALENDAR", "PROJECT")
# Never deleted from: delete logs, key counters, cross references and P6 sessions.
DEL_SKIP = {"REFRDEL", "NEXTKEY", "PKXREF", "USESSION"}
# Column -> set of deleted ids it belongs to (temp tables built per run). Every table of the schema
# that has one of these columns loses the matching rows: TASKPRED by either end (links to or from
# other projects), ACTVCODE and TASKACTV through the project's activity code types.
DEL_OWN = {"proj_id": "_dp", "task_id": "_dt", "pred_task_id": "_dt", "wbs_id": "_dw", "taskrsrc_id": "_dr",
           "actv_code_type_id": "_dct"}
# UDFVALUE.fk_id per UDFTYPE.table_name.
DEL_UDF = {"PROJECT": "_dp", "PROJWBS": "_dw", "TASK": "_dt", "TASKRSRC": "_dr"}
# Columns of rows that stay but may name a deleted project: cleared (project baseline, copy source...).
DEL_CLEAR = (("PROJECT", "sum_base_proj_id"), ("PROJECT", "source_proj_id"), ("PROJISSU", "base_proj_id"))
# Orphan check: column -> (parent table, key). UDFVALUE.fk_id is checked per UDFTYPE.table_name.
ORPHAN_REFS = {"proj_id": ("PROJECT", "proj_id"), "orig_proj_id": ("PROJECT", "proj_id"),
               "sum_base_proj_id": ("PROJECT", "proj_id"), "pred_proj_id": ("PROJECT", "proj_id"),
               "base_proj_id": ("PROJECT", "proj_id"), "task_id": ("TASK", "task_id"),
               "pred_task_id": ("TASK", "task_id"), "wbs_id": ("PROJWBS", "wbs_id"),
               "parent_wbs_id": ("PROJWBS", "wbs_id"), "taskrsrc_id": ("TASKRSRC", "taskrsrc_id"),
               "actv_code_type_id": ("ACTVTYPE", "actv_code_type_id"), "actv_code_id": ("ACTVCODE", "actv_code_id"),
               "parent_actv_code_id": ("ACTVCODE", "actv_code_id"), "clndr_id": ("CALENDAR", "clndr_id"),
               "base_clndr_id": ("CALENDAR", "clndr_id")}


def _db_tables(con) -> list[str]:
    """Tables of the database that may hold project data (not the delete logs and key counters)."""
    return [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE "
                                      "'sqlite_%' ORDER BY name") if r[0] not in DEL_SKIP and not r[0].startswith("DLT")]


def _orphans(con) -> dict[str, int]:
    """Rows whose id column names a row that does not exist: {'TABLE.col -> PARENT': count}, non-zero
    only. Covers every table with a column of ORPHAN_REFS and UDFVALUE.fk_id per UDFTYPE.table_name."""
    out = {}
    for t in _db_tables(con):
        for c in sorted(_cols(con, t)):
            if c in ORPHAN_REFS and ORPHAN_REFS[c] != (t, c):
                pt, pk = ORPHAN_REFS[c]
                n = con.execute(f"SELECT COUNT(*) FROM {t} x WHERE x.{c} IS NOT NULL AND NOT EXISTS "
                                f"(SELECT 1 FROM {pt} p WHERE p.{pk} = x.{c})").fetchone()[0]
                if n:
                    out[f"{t}.{c} -> {pt}"] = n
    if {"fk_id", "udf_type_id"} <= _cols(con, "UDFVALUE") and _cols(con, "UDFTYPE"):
        for tn, pk in (("PROJECT", "proj_id"), ("PROJWBS", "wbs_id"), ("TASK", "task_id"), ("TASKRSRC", "taskrsrc_id"),
                       ("RSRC", "rsrc_id")):
            n = con.execute(f"SELECT COUNT(*) FROM UDFVALUE v JOIN UDFTYPE u ON u.udf_type_id = v.udf_type_id "
                            f"WHERE u.table_name = ? AND NOT EXISTS (SELECT 1 FROM {tn} p WHERE p.{pk} = v.fk_id)",
                            (tn,)).fetchone()[0]
            if n:
                out[f"UDFVALUE.fk_id -> {tn}"] = n
    return out


def _del_sets(con, pids: Iterable[int]) -> None:
    """Temp tables of the ids a delete removes: projects, their activities, WBS, assignments, code types."""
    _temp_id_sets(con, ("_dp", "_dt", "_dw", "_dr", "_dct"))
    con.executemany("INSERT OR IGNORE INTO temp._dp VALUES (?)", [(int(p),) for p in pids])
    con.execute("INSERT OR IGNORE INTO temp._dt SELECT task_id FROM TASK WHERE proj_id IN (SELECT id FROM temp._dp)")
    con.execute("INSERT OR IGNORE INTO temp._dw SELECT wbs_id FROM PROJWBS WHERE proj_id IN (SELECT id FROM temp._dp)")
    con.execute("INSERT OR IGNORE INTO temp._dr SELECT taskrsrc_id FROM TASKRSRC WHERE proj_id IN "
                "(SELECT id FROM temp._dp) OR task_id IN (SELECT id FROM temp._dt)")
    if "proj_id" in _cols(con, "ACTVTYPE"):
        con.execute("INSERT OR IGNORE INTO temp._dct SELECT actv_code_type_id FROM ACTVTYPE WHERE proj_id IN "
                    "(SELECT id FROM temp._dp)")


def _temp_id_sets(con, names: Iterable[str]) -> None:
    """Empty temp tables of ids (id INTEGER PRIMARY KEY) for a delete."""
    for t in names:
        con.execute(f"DROP TABLE IF EXISTS temp.{t}")
        con.execute(f"CREATE TEMP TABLE {t} (id INTEGER PRIMARY KEY)")


def _owned_where(con, t: str, own: Mapping[str, str], udf: Mapping[str, str]) -> list[str]:
    """Conditions (OR) that select the rows of table t belonging to the ids in the temp sets: a column
    of own (column -> temp set), or a UDFVALUE whose UDF type is on a table of udf (table -> temp set)."""
    conds = [f"{c} IN (SELECT id FROM temp.{s})" for c, s in own.items()
             if c in _cols(con, t) and not (c == "actv_code_type_id" and t == "ACTVTYPE")]
    if t == "UDFVALUE" and _cols(con, "UDFTYPE"):
        conds += [f"(fk_id IN (SELECT id FROM temp.{s}) AND udf_type_id IN (SELECT udf_type_id FROM UDFTYPE "
                  f"WHERE table_name = '{tn}'))" for tn, s in udf.items()]
    return conds


def _del_where(con, t: str) -> list[str]:
    """Conditions (OR) that select the rows of table t belonging to the deleted projects' ids."""
    return _owned_where(con, t, DEL_OWN, DEL_UDF)


def _delete_rows(con, pids: Iterable[int]) -> tuple[dict[str, int], dict[str, int]]:
    """Hard-delete the projects pids and every row of the schema that belongs to them, children first
    (P6's order, then the other tables). Returns (rows deleted per table, rows deleted per table that
    carry the proj_id of a project not deleted)."""
    _del_sets(con, pids)
    tables = _db_tables(con)
    order = [t for t in tables if t not in DEL_ORDER] + [t for t in DEL_ORDER if t in tables]
    counts, foreign = {}, {}
    for t in order:
        conds = _del_where(con, t)
        if not conds:
            continue
        where = " OR ".join(conds)
        if "proj_id" in _cols(con, t):
            n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE ({where}) AND proj_id IS NOT NULL AND proj_id NOT IN "
                            "(SELECT id FROM temp._dp)").fetchone()[0]
            if n:
                foreign[t] = n
        n = con.execute(f"DELETE FROM {t} WHERE {where}").rowcount
        if n:
            counts[t] = n
    return counts, foreign


def _cross_links(con) -> dict:
    """Relationships between a deleted activity and one that stays (temp sets of _del_sets)."""
    q = ("FROM TASKPRED r JOIN TASK s ON s.task_id = r.task_id JOIN TASK p ON p.task_id = r.pred_task_id "
         "JOIN PROJECT sp ON sp.proj_id = s.proj_id JOIN PROJECT pp ON pp.proj_id = p.proj_id WHERE "
         "(r.task_id IN (SELECT id FROM temp._dt)) <> (r.pred_task_id IN (SELECT id FROM temp._dt))")
    n = con.execute("SELECT COUNT(*) " + q).fetchone()[0]
    kept = sorted({r[0] for r in con.execute(
        "SELECT CASE WHEN r.task_id IN (SELECT id FROM temp._dt) THEN p.proj_id ELSE s.proj_id END " + q)})
    rows = con.execute("SELECT pp.proj_short_name AS pp, p.task_code AS pc, sp.proj_short_name AS sp, "
                       "s.task_code AS sc, r.pred_type AS ty " + q + " ORDER BY r.task_pred_id LIMIT 15").fetchall()
    return {"count": n, "sample": [f"{r['pp']} {r['pc']} -> {r['sp']} {r['sc']} {REL_TYPES_REV.get(r['ty'], r['ty'])}"
                                   for r in rows],
            "other_proj_ids": kept,
            "note": "deleted with the project (P6 does the same); the other project needs F9"}


def _eps_ancestors(con, pids: Iterable[int]) -> list[dict]:
    """EPS nodes above the projects pids, deepest first: {wbs_id, proj_id, eps_id, depth}."""
    nodes = {n["wbs_id"]: n for n in _eps_nodes(con)}
    out: dict[int, dict] = {}
    for (parent,) in con.execute("SELECT parent_wbs_id FROM PROJWBS WHERE proj_node_flag = 'Y' AND proj_id IN "
                                 f"{_ids_sql(pids)}").fetchall():
        chain, w = [], parent
        while w in nodes and w not in chain and len(chain) < 100:
            chain.append(w)
            w = nodes[w]["parent_wbs_id"]
        for d, w in enumerate(reversed(chain)):
            out[w] = {**nodes[w], "depth": d}
    return sorted(out.values(), key=lambda n: -n["depth"])


def _empty_eps(con, candidates: list[dict]) -> list[dict]:
    """Candidate EPS nodes (deepest first) left without any project or child EPS node, counting the
    nodes this pass removes as gone."""
    gone: set[int] = set()
    for n in candidates:
        kids = {r[0] for r in con.execute("SELECT wbs_id FROM PROJWBS WHERE parent_wbs_id = ?"
                                          + _alive(con, "PROJWBS"), (n["wbs_id"],))}
        if not kids - gone:
            gone.add(n["wbs_id"])
    return [n for n in candidates if n["wbs_id"] in gone]


def _delete_targets(con, names: list[str], include_baselines: bool, allow_templates: bool) -> tuple[list[dict], list[dict]]:
    """(projects named, their baselines) as {proj_id, project, kind, activities, status}; ValueError for
    unknown names, EPS nodes and templates (unless allowed)."""
    found, missing, errs = [], [], []
    for name in names:
        r = con.execute("SELECT p.proj_id, p.project_flag, p.orig_proj_id, w.status_code FROM PROJECT p LEFT JOIN "
                        "PROJWBS w ON w.proj_id = p.proj_id AND w.proj_node_flag = 'Y'" + _alive(con, "PROJWBS", "w")
                        + " WHERE p.proj_short_name = ?" + _alive(con, "PROJECT", "p"), (name,)).fetchone()
        if r is None:
            missing.append(name)
        elif (r["project_flag"] or "Y") != "Y":
            errs.append(f"{name} is an EPS node (delete_empty_eps removes EPS nodes left empty)")
        elif r["status_code"] == "WS_Template" and not allow_templates:
            errs.append(f"{name} is a template (status WS_Template): pass allow_templates=True to delete it")
        else:
            found.append({"proj_id": r["proj_id"], "project": name,
                          "kind": "baseline" if r["orig_proj_id"] else "project", "status": r["status_code"]})
    if missing:
        errs.insert(0, f"Project not found: {missing}")
    if errs:
        raise ValueError("; ".join(errs))
    ids = {p["proj_id"] for p in found}
    bls = [{"proj_id": r[0], "project": r[1], "kind": "baseline", "of": r[2], "status": None} for r in con.execute(
        "SELECT b.proj_id, b.proj_short_name, o.proj_short_name FROM PROJECT b JOIN PROJECT o ON o.proj_id = "
        f"b.orig_proj_id WHERE b.orig_proj_id IN {_ids_sql(ids)}" + _alive(con, "PROJECT", "b")
        + " ORDER BY b.proj_short_name") if r[0] not in ids]
    for p in found + bls:
        p["activities"] = con.execute("SELECT COUNT(*) FROM TASK WHERE proj_id = ?" + _alive(con, "TASK"),
                                      (p["proj_id"],)).fetchone()[0]
    return found, bls


@_tool(read_only=False, destructive=True)
def p6_delete_project(project: ProjectList, include_baselines: bool = True, allow_templates: bool = False,
                      delete_empty_eps: bool = False, confirm: bool = False) -> dict:
    """Delete one or more projects (Project IDs, a list deletes them in one transaction with one
    backup) and everything that belongs to them, as P6 does: WBS, activities, relationships (links to
    or from other projects too, listed in cross_project_links), assignments, activity code
    assignments, project activity code types and codes, UDF values, notebooks, steps, issues,
    thresholds, documents, PROJPROP rows, project calendars and every other table with a proj_id,
    task_id, wbs_id or assignment id of the project. Rows are hard-deleted like a P6 GUI delete
    (P6's triggers log them in REFRDEL). Dry run unless confirm=True: it performs the delete in a
    transaction, returns rows per table, and rolls back.
    include_baselines: also delete the baselines of these projects (False: they are detached and
    become ordinary projects in the same EPS). Projects whose project baseline is deleted lose that
    link (sum_base_proj_id cleared). Templates (WS_Template) are refused unless allow_templates=True.
    delete_empty_eps: remove the EPS nodes above these projects that are left without any project
    or child EPS (eps.would_delete). orphan_check compares rows pointing to missing parents before
    and after (new must be 0); new orphans or rows of other projects (other than links) refuse the
    confirmed run. Close P6 first; activities of other projects linked to these need F9."""
    names = [project] if isinstance(project, str) else list(dict.fromkeys(project or []))
    if not names or not all(isinstance(n, str) and n for n in names):
        raise ValueError("project: a Project ID or a list of Project IDs")
    con = _connect()
    try:  # names checked before any lock or backup
        _delete_targets(con, names, include_baselines, allow_templates)
    finally:
        con.close()
    with _write_session(confirm) as (con, backup):
        t0 = monotonic()
        named, bls = _delete_targets(con, names, include_baselines, allow_templates)
        targets = named + (bls if include_baselines else [])
        pids = [p["proj_id"] for p in targets]
        before = _orphans(con)
        eps_cand = _eps_ancestors(con, pids) if delete_empty_eps else []
        _del_sets(con, pids)
        cross = _cross_links(con)
        plan = [f"Delete {len(named)} project(s): " + ", ".join(f"{p['project']} ({p['activities']} activities)"
                                                             for p in named)]
        cleared, warnings = Counter(), []
        if bls and include_baselines:
            plan.append(f"Delete {len(bls)} baseline(s): " + ", ".join(f"{b['project']} of {b['of']}" for b in bls))
        elif bls:
            now = _now_str(con)
            for b in bls:
                node = con.execute("SELECT w.wbs_id, w.parent_wbs_id, o.parent_wbs_id FROM PROJWBS w JOIN PROJECT bp ON "
                                   "bp.proj_id = w.proj_id JOIN PROJWBS o ON o.proj_id = bp.orig_proj_id AND "
                                   "o.proj_node_flag = 'Y' WHERE w.proj_id = ? AND w.proj_node_flag = 'Y'",
                                   (b["proj_id"],)).fetchone()
                _update(con, "PROJECT", {"proj_id": b["proj_id"]},
                        {"orig_proj_id": None, **_stamp_cols(con, "PROJECT", now, create=False)})
                if node and (node[1] is None or node[1] in {r[0] for r in con.execute(
                        "SELECT id FROM temp._dw")} or not con.execute("SELECT 1 FROM PROJWBS WHERE wbs_id = ?",
                                                                       (node[1],)).fetchone()):
                    _update(con, "PROJWBS", {"wbs_id": node[0]}, {"parent_wbs_id": node[2]})
                cleared["PROJECT.orig_proj_id"] += 1
            plan.append(f"Keep {len(bls)} baseline(s) as ordinary projects (include_baselines=False): "
                        + ", ".join(b["project"] for b in bls))
        for t, c in DEL_CLEAR:
            if c in _cols(con, t):
                where = f"{c} IN (SELECT id FROM temp._dp)" + (" AND proj_id NOT IN (SELECT id FROM temp._dp)"
                                                               if "proj_id" in _cols(con, t) else "")
                n = con.execute(f"UPDATE {t} SET {c} = NULL WHERE {where}").rowcount
                if n:
                    cleared[f"{t}.{c}"] += n
        counts, foreign = _delete_rows(con, pids)
        if cross["count"]:
            plan.append(f"Delete {cross['count']} relationship(s) with activities of other projects")
        eps_rows, eps_left = [], None
        if delete_empty_eps:
            empty = _empty_eps(con, eps_cand)
            if empty:
                more, more_foreign = _delete_rows(con, [n["proj_id"] for n in empty])
                for t, n in more.items():
                    counts[t] = counts.get(t, 0) + n
                for t, n in more_foreign.items():
                    foreign[t] = foreign.get(t, 0) + n
                eps_rows = [n["eps_id"] for n in empty]
                plan.append(f"Delete {len(empty)} EPS node(s) left empty: {', '.join(eps_rows)}")
            eps_left = len(_eps_nodes(con))
            if not eps_left:
                warnings.append("No EPS node is left: p6_import_xer needs create_eps= for a new project, and P6 "
                                "asks for an EPS before creating one.")
        after = _orphans(con)
        new = {k: n - before.get(k, 0) for k, n in after.items() if n > before.get(k, 0)}
        foreign_bad = {t: n for t, n in foreign.items() if t != "TASKPRED"}
        reasons = []
        if new:
            reasons.append(f"The delete would leave rows pointing to deleted rows: {new}")
        if foreign_bad:
            reasons.append(f"The delete would remove rows of other projects: {foreign_bad} (for example project "
                           "calendars or code types used elsewhere): fix them in P6 first")
        plan.append("Rows deleted: " + ", ".join(f"{t} {n}" for t, n in sorted(counts.items(), key=lambda kv: -kv[1])))
        if cleared:
            plan.append("References cleared: " + ", ".join(f"{k} {n}" for k, n in cleared.items()))
        if backup: plan.append(f"Backup: {backup}")
        extra = {"projects": named, "baselines": {"included": include_baselines, "rows": bls},
                 "counts": {t: {"deleted": n} for t, n in counts.items()}, "cleared": dict(cleared),
                 "cross_project_links": cross,
                 "eps": {"would_delete" if not confirm else "deleted": eps_rows, "eps_nodes_left": eps_left}
                 if delete_empty_eps else None,
                 "orphan_check": {"new": new, "total_new": sum(new.values()), "existing_before": before,
                                  "note": "rows whose proj_id, task_id, wbs_id, assignment, code, calendar or UDF "
                                          "fk_id names a missing row; existing_before were there already"},
                 "warnings": warnings, "seconds": round(monotonic() - t0, 1)}
        if reasons and confirm:
            raise ValueError("Delete refused (nothing written). " + " ".join(reasons))
        log = [{"operation": "delete_project", "proj_id": p["proj_id"], "project": p["project"], "table": "PROJECT",
                "keys": {"proj_id": p["proj_id"]}, "before": {"activities": p.get("activities")}, "after": None,
                "summary": plan[0][:300]} for p in targets]
        res = _commit(con, plan, confirm, projects=cross["other_proj_ids"], log=log)
        res.update(extra)
        if not confirm:
            res.update(would_refuse=bool(reasons), refuse_reasons=reasons)
            if reasons:
                res["next"] = "confirm=True would be refused: " + " ".join(reasons)
        elif res.get("applied"):
            res["next"] = "Open P6: the projects are gone. Reschedule (F9) the projects listed in cross_project_links."
        return res


# ----------------------------------------------------------------- XER import / export

# Tables exported, in dependency order (this is also the order P6 writes them). SCHEDOPTIONS is not a
# table of the database: export builds it from PROJPROP 'scheduling', import writes it back there.
XER_EXPORT_TABLES = list(get_args(XerTable))
# Tables imported row by row.
XER_TABLES = [t for t in XER_EXPORT_TABLES if t != "SCHEDOPTIONS"]
# Primary key of each table that receives new ids from NEXTKEY.
XER_PK = {"CURRTYPE": "curr_id", "FINTMPL": "fintmpl_id", "OBS": "obs_id", "UDFTYPE": "udf_type_id",
          "PROJECT": "proj_id", "CALENDAR": "clndr_id", "PROJWBS": "wbs_id", "RSRC": "rsrc_id",
          "ACTVTYPE": "actv_code_type_id", "RSRCRATE": "rsrc_rate_id", "TASK": "task_id",
          "ACTVCODE": "actv_code_id", "TASKPRED": "task_pred_id", "TASKRSRC": "taskrsrc_id"}
# Id columns that point to a table carried by the XER. Any other *_id column refers to data
# the XER does not contain (roles, accounts, curves, locations...) and is cleared on import.
XER_FK = {"proj_id": "PROJECT", "pred_proj_id": "PROJECT", "orig_proj_id": "PROJECT",
          "sum_base_proj_id": "PROJECT", "source_proj_id": "PROJECT",
          "wbs_id": "PROJWBS", "parent_wbs_id": "PROJWBS", "obs_id": "OBS", "parent_obs_id": "OBS",
          "clndr_id": "CALENDAR", "base_clndr_id": "CALENDAR", "rsrc_id": "RSRC",
          "parent_rsrc_id": "RSRC", "curr_id": "CURRTYPE", "fintmpl_id": "FINTMPL",
          "actv_code_type_id": "ACTVTYPE", "actv_code_id": "ACTVCODE",
          "parent_actv_code_id": "ACTVCODE", "udf_type_id": "UDFTYPE",
          "task_id": "TASK", "pred_task_id": "TASK"}
# UDFVALUE.fk_id points to the table named in UDFTYPE.table_name.
UDF_FK_TABLES = {"PROJECT", "PROJWBS", "TASK", "RSRC", "TASKRSRC"}
# Global rows (no proj_id) matched by name on import and reused: (key columns, query on those keys).
XER_MATCH = {
    "CURRTYPE": (("curr_short_name",), "SELECT curr_id FROM CURRTYPE WHERE curr_short_name = ?"),
    "FINTMPL": (("fintmpl_name",), "SELECT fintmpl_id FROM FINTMPL WHERE fintmpl_name = ?"),
    "OBS": (("obs_name",), "SELECT obs_id FROM OBS WHERE obs_name = ?"),
    "UDFTYPE": (("table_name", "udf_type_label"),
                "SELECT udf_type_id FROM UDFTYPE WHERE table_name = ? AND udf_type_label = ?"),
    "CALENDAR": (("clndr_type", "clndr_name"),
                 "SELECT clndr_id FROM CALENDAR WHERE proj_id IS NULL AND clndr_type = ? AND clndr_name = ?"),
    "RSRC": (("rsrc_short_name",), "SELECT rsrc_id FROM RSRC WHERE rsrc_short_name = ?"),
    "ACTVTYPE": (("actv_code_type_scope", "actv_code_type"), "SELECT actv_code_type_id FROM ACTVTYPE WHERE "
                 "proj_id IS NULL AND actv_code_type_scope = ? AND actv_code_type = ?"),
}
# Never copied from an XER row: set by triggers / this server, or meaningless across databases.
XER_SKIP_COLS = {"create_date", "create_user", "update_date", "update_user",
                 "delete_session_id", "delete_date", "guid"}
_XER_DATE = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d")
_XER_NL = "\x7f\x7f"  # P6 writes line breaks inside memo fields as two DEL characters


def _xer_decode(v: str) -> Optional[str]:
    if v == "":
        return None
    v = v.replace(_XER_NL, "\n").replace('""', '"')
    return v + ":00" if _XER_DATE.fullmatch(v) else v


def _xer_encode(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        v = _blob_text(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            return ""
        s = f"{v:.6f}".rstrip("0").rstrip(".")  # 6 decimals as before, never "7." or "-0"
        return "0" if s in ("-0", "") else s
    v = str(v)
    if re.fullmatch(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d(:\d\d(\.\d+)?)?", v):
        return v[:10] + " " + v[11:16]
    return v.replace("\r\n", "\n").replace("\n", _XER_NL).replace("\t", " ").replace('"', '""')


def _xer_read_meta(path: str) -> tuple[list[str], dict[str, list[dict]], dict]:
    """XER file -> (header, tables, meta). A UTF-8 BOM is skipped; text is UTF-8, else cp1252
    (utf8_error_at = file offset of the first non UTF-8 byte). A repeated %T block adds to its
    table, %R lines before their %F are not read, and rows with more or fewer values than fields
    are counted (missing values read as empty, extra ones dropped)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if bom else raw
    meta: dict[str, Any] = {"bytes": len(raw), "bom": bom, "encoding": "utf-8", "utf8_error_at": None}
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        meta.update(encoding="cp1252", utf8_error_at=e.start + 3 * bom)
        text = body.decode("cp1252", errors="replace")
    header: list[str] = []
    tables: dict[str, list[dict]] = {}
    cur, fields, end = None, None, False
    blocks, early, mismatch = Counter(), Counter(), Counter()
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("ERMHDR"):
            header = line.split("\t")
        elif line.startswith("%T\t"):
            cur, fields = line[3:].strip(), None
            blocks[cur] += 1
            tables.setdefault(cur, [])
        elif line.startswith("%F\t"):
            fields = [f.lower() for f in line[3:].split("\t")]
        elif line.startswith("%R\t") and cur:
            if fields is None:
                early[cur] += 1
                continue
            vals = line[3:].split("\t")
            if len(vals) != len(fields):
                mismatch[cur] += 1
                vals = (vals + [""] * len(fields))[:len(fields)]
            tables[cur].append({f: _xer_decode(v) for f, v in zip(fields, vals)})
        elif line.startswith("%E"):
            end = True
    if not header:
        raise ValueError(f"{path} is not an XER file (no ERMHDR line).")
    meta.update(replacement_chars=text.count("\ufffd"),
                non_ascii_bytes=len(body.translate(None, bytes(range(128)))),
                has_end_marker=end, field_count_mismatch=dict(mismatch), rows_before_fields=dict(early),
                duplicate_table_blocks=sorted(t for t, n in blocks.items() if n > 1))
    return header, tables, meta


def _xer_read(path: str) -> tuple[list[str], dict[str, list[dict]]]:
    return _xer_read_meta(path)[:2]


def _xer_only(x: dict, xpid) -> dict:
    """Rows of XER project xpid plus global rows (no proj_id). ACTVCODE has no proj_id: only the
    codes of a kept code type stay (another project's types are dropped, so are their codes)."""
    out = {t: [r for r in rows if r.get("proj_id") in (None, xpid)] for t, rows in x.items()}
    if "ACTVCODE" in out:
        kept = {r.get("actv_code_type_id") for r in out.get("ACTVTYPE", [])}
        out["ACTVCODE"] = [r for r in out["ACTVCODE"] if r.get("actv_code_type_id") in kept]
    return out


def _prefer(con, col: str) -> Optional[int]:
    """A PREFER setting such as task_code_len, None when this P6 version has no such column."""
    if col not in _cols(con, "PREFER"):
        return None
    r = con.execute(f"SELECT {col} FROM PREFER").fetchone()
    return int(r[0]) if r and r[0] else None


def _xer_preflight(header: list[str], x: dict, meta: dict, xer_project: Optional[str] = None,
                   con: Optional[sqlite3.Connection] = None, preview: bool = True) -> dict:
    """Checks of one XER project before import, each {check, count, sample (15), note}. errors: data
    the import would write wrong (no project WBS node, activities without WBS or calendar, duplicate
    Activity IDs, assignments of activities not in the file, rows with a wrong number of values); a
    confirmed p6_import_xer refuses them. warnings: values cleared or reattached, tables not imported, codes
    longer than PREFER allows. info: rows skipped on purpose, encoding. With con: dropped_columns
    (XER fields the database lacks) and, with preview, db_preview (global items matched by name
    or created, as the import would)."""
    xp = _xer_project(x, xer_project)
    all_codes = x.get("ACTVCODE", [])
    x = _xer_only(x, xp["proj_id"])
    kept_codes = {id(r) for r in x.get("ACTVCODE", [])}
    out: dict[str, Any] = {"project": xp["proj_short_name"], "errors": [], "warnings": [], "info": [],
                           "unhandled_tables": {t: len(r) for t, r in x.items()
                                                if t not in XER_TABLES and t != "SCHEDOPTIONS"},
                           "dropped_columns": {}, "db_preview": None}

    def add(level: str, check: str, items: Sequence, note: str = "") -> None:
        if items:
            out[level].append({"check": check, "count": len(items), "sample": [str(i) for i in items[:15]],
                               **({"note": note} if note else {})})

    ids = lambda t: {r.get(XER_PK.get(t, "")) for r in x.get(t, [])} - {None}  # noqa: E731
    fields = {t: set().union(*rows) for t, rows in x.items()}
    tasks, wbs, cals, rsrc, asg = (x.get(t, []) for t in ("TASK", "PROJWBS", "CALENDAR", "RSRC", "TASKRSRC"))
    wbs_ids, cal_ids, task_ids, rsrc_ids = ids("PROJWBS"), ids("CALENDAR"), ids("TASK"), ids("RSRC")
    code = {t.get("task_id"): t.get("task_code") for t in tasks}
    node_ids = {w.get("wbs_id") for w in wbs if w.get("proj_node_flag") == "Y"}
    fc = meta.get("field_count_mismatch", {})
    add("errors", "project_wbs_node_missing", [] if node_ids else [xp["proj_short_name"]], "no project WBS node")
    add("errors", "task_wbs_missing", [t.get("task_code") for t in tasks if t.get("wbs_id") not in wbs_ids],
        "would be imported without a WBS")
    add("errors", "task_calendar_missing", [t.get("task_code") for t in tasks if t.get("clndr_id") not in cal_ids],
        "would be imported without a calendar")
    add("errors", "duplicate_task_code", sorted(str(c) for c, n in Counter(code.values()).items() if n > 1),
        "Activity IDs must be unique in a project")
    add("errors", "taskrsrc_task_missing", [f"taskrsrc {r.get('taskrsrc_id')}: task {r.get('task_id')}" for r in asg
                                            if r.get("task_id") not in task_ids], "assignment of an activity not in the file")
    add("errors", "field_count_mismatch", [f"{t}: {n} rows" for t, n in fc.items() if t in XER_TABLES],
        "rows with more or fewer values than fields")
    add("warnings", "field_count_mismatch_not_imported", [f"{t}: {n} rows" for t, n in fc.items() if t not in XER_TABLES])
    add("warnings", "rows_before_fields", [f"{t}: {n} rows" for t, n in meta.get("rows_before_fields", {}).items()],
        "%R lines before the %F line of their table are not read")
    add("warnings", "duplicate_table_blocks", meta.get("duplicate_table_blocks", []), "the rows of every block are read")
    add("warnings", "end_marker_missing", [] if meta.get("has_end_marker", True) else ["%E"], "the file may be truncated")
    add("warnings", "undecodable_bytes", [meta["replacement_chars"]] if meta.get("replacement_chars") else [],
        f"characters not readable as {meta.get('encoding')}: check names and notes")
    add("warnings", "resource_missing",
        [f"activity {t.get('task_code')}" for t in tasks if t.get("rsrc_id") is not None and t["rsrc_id"] not in rsrc_ids]
        + [f"taskrsrc {r.get('taskrsrc_id')}" for r in asg if r.get("rsrc_id") is not None and r["rsrc_id"] not in rsrc_ids],
        "the resource is cleared")
    add("warnings", "wbs_parent_missing", [w.get("wbs_short_name") for w in wbs if w.get("wbs_id") not in node_ids
                                           and w.get("parent_wbs_id") not in wbs_ids], "attached to the project node")
    cal_ok = cal_ids | {None}
    add("warnings", "calendar_missing",
        [f"base of {c.get('clndr_name')}" for c in cals if c.get("base_clndr_id") not in cal_ok]
        + [f"resource {r.get('rsrc_short_name')}" for r in rsrc if r.get("clndr_id") not in cal_ok]
        + (["project"] if xp.get("clndr_id") not in cal_ok else []), "cleared on import")
    links = Counter((r.get("pred_task_id"), r.get("task_id"), r.get("pred_type")) for r in x.get("TASKPRED", []))
    add("warnings", "duplicate_link", [f"{code.get(p, p)} -> {code.get(s, s)} {REL_TYPES_REV.get(k, k)}"
                                       for (p, s, k), n in links.items() if n > 1], "P6 keeps one link per type")
    cleared = Counter()
    for t in XER_TABLES:
        for k in sorted(fields.get(t, ())):
            if k.endswith("_id") and k not in XER_FK and k not in XER_SKIP_COLS and k not in ("fk_id", XER_PK.get(t)):
                cleared[f"{t}.{k}"] = sum(1 for r in x[t] if r.get(k) is not None)
    add("warnings", "foreign_ids_cleared", [f"{k}: {n}" for k, n in cleared.items() if n],
        "ids of data an XER does not carry (roles, accounts, curves...) are cleared")
    add("warnings", "tables_not_imported", [f"{t}: {n} rows" for t, n in out["unhandled_tables"].items()])
    add("info", "activity_codes_other_types", [f"{c.get('short_name')} (type {c.get('actv_code_type_id')})"
                                               for c in all_codes if id(c) not in kept_codes],
        "codes of another project's code types or of a type not in the file: not imported")
    add("info", "schedule_options",["SCHEDOPTIONS"] if x.get("SCHEDOPTIONS") else [],
        "written to the project's schedule options (PROJPROP scheduling) unless import_schedule_options=False "
        "(default off in baseline mode); compare with p6_schedule_options")
    add("info", "external_links", [f"{code.get(r.get('pred_task_id'), r.get('pred_task_id'))} -> "
                                   f"{code.get(r.get('task_id'), r.get('task_id'))}" for r in x.get("TASKPRED", [])
                                   if r.get("task_id") not in task_ids or r.get("pred_task_id") not in task_ids],
        "relationships with an activity outside the file are skipped")
    types_, codes_ = ids("ACTVTYPE"), ids("ACTVCODE")
    add("info", "activity_codes_unmapped", [f"task {r.get('task_id')}" for r in x.get("TASKACTV", [])
                                            if r.get("task_id") not in task_ids or r.get("actv_code_type_id") not in types_
                                            or r.get("actv_code_id") not in codes_], "skipped")
    utab = {u.get("udf_type_id"): u.get("table_name") for u in x.get("UDFTYPE", [])}
    pks = {t: ids(t) for t in UDF_FK_TABLES}
    add("info", "udf_values_unmapped", [f"{utab.get(v.get('udf_type_id'))} {v.get('fk_id')}" for v in x.get("UDFVALUE", [])
                                        if v.get("fk_id") not in pks.get(utab.get(v.get("udf_type_id")), ())], "skipped")
    add("info", "project_baseline_not_imported", [] if xp.get("sum_base_proj_id") is None else [xp["sum_base_proj_id"]],
        "the project baseline link (sum_base_proj_id) is not carried over")
    add("info", "encoding", [] if meta.get("encoding") == "utf-8" else [
        f"{meta.get('encoding')}, first non UTF-8 byte at offset {meta.get('utf8_error_at')}"], "the file is not UTF-8")
    if con is None:
        out["note"] = "No database: dropped_columns, code lengths and db_preview not checked."
        return out
    out["dropped_columns"] = {t: sorted(fields[t] - _cols(con, t)) for t in XER_TABLES
                              if fields.get(t) and fields[t] - _cols(con, t)}
    long = []
    for key, rows, col in (("task_code_len", tasks, "task_code"), ("proj_short_len", [xp], "proj_short_name"),
                           ("wbs_short_len", [w for w in wbs if w.get("wbs_id") not in node_ids], "wbs_short_name"),
                           ("rsrc_short_len", rsrc, "rsrc_short_name")):
        n = _prefer(con, key)
        long += [f"{col} {r.get(col)!r} ({len(r.get(col))} > {n})" for r in rows if n and len(r.get(col) or "") > n]
    add("warnings", "codes_too_long", long, "longer than this database allows (PREFER); for the Project ID "
                                            "pass new_short_name")
    out["project_id_in_db"] = con.execute("SELECT 1 FROM PROJECT WHERE proj_short_name = ?" + _alive(con, "PROJECT"),
                                          (xp["proj_short_name"],)).fetchone() is not None
    if preview:
        out["db_preview"] = {}
        for t, (keys, sql) in XER_MATCH.items():
            rows = [r for r in x.get(t, []) if r.get("proj_id") is None]
            if rows:
                hit = sum(1 for r in rows if con.execute(sql + _alive(con, t), [r.get(k) for k in keys]).fetchone())
                out["db_preview"][t] = {"matched": hit, "would_create": len(rows) - hit}
    return out


def _insert(con, table: str, row: dict) -> None:
    row = {k: v for k, v in row.items() if k in _cols(con, table)}
    con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                list(row.values()))


def _update(con, table: str, where: dict, row: dict) -> None:
    row = {k: v for k, v in row.items() if k in _cols(con, table) and k not in where}
    if row:
        con.execute(f"UPDATE {table} SET {', '.join(k + '=?' for k in row)} WHERE "
                    + " AND ".join(k + "=?" for k in where), [*row.values(), *where.values()])


class _XerImport:
    """One import run. idmap[table][xer id] = database id."""

    def __init__(self, con, tables: dict[str, list[dict]], mode: str):
        self.con, self.x, self.mode = con, tables, mode
        self.idmap: dict[str, dict[str, int]] = {t: {} for t in XER_TABLES}
        self.created: dict[str, set[str]] = {t: set() for t in XER_TABLES}  # xer ids inserted, not matched
        self.touched: dict[str, set[int]] = {"TASKPRED": set(), "TASKRSRC": set()}  # db ids matched or inserted
        self.stats: dict[str, dict[str, int]] = {}
        self.warnings: list[str] = []
        self.now = _now_str(con)

    def count(self, table: str, what: str, n: int = 1) -> None:
        self.stats.setdefault(table, {}).setdefault(what, 0)
        self.stats[table][what] += n

    def prep(self, table: str, xrow: dict) -> dict:
        """XER row -> database row: ids remapped, foreign ids not in the XER cleared."""
        out = {}
        for k, v in xrow.items():
            if k in XER_SKIP_COLS or k == XER_PK.get(table):
                continue
            if k in XER_FK:
                v = self.idmap[XER_FK[k]].get(v) if v is not None else None
            elif k.endswith("_id") and k != "fk_id":
                v = None
            out[k] = v
        return out

    def new_row(self, table: str, xrow: dict, extra: Optional[dict] = None) -> int:
        row = self.prep(table, xrow)
        row.update(extra or {})
        new_id = _next_key(self.con, table, XER_PK[table])
        row[XER_PK[table]] = new_id
        if "guid" in _cols(self.con, table):
            row["guid"] = _guid()
        row.update(_stamp_cols(self.con, table, self.now, create=True))
        _insert(self.con, table, row)
        self.count(table, "inserted")
        return new_id

    def upd_row(self, table: str, db_id: int, xrow: dict, extra: Optional[dict] = None) -> None:
        row = self.prep(table, xrow)
        row.update(extra or {})
        row.update(_stamp_cols(self.con, table, self.now, create=False))
        _update(self.con, table, {XER_PK[table]: db_id}, row)
        self.count(table, "updated")

    def match_or_create(self, table: str, rows: list[dict], key, find_sql: Optional[str],
                        parent_col: Optional[str] = None) -> None:
        """Reuse the database row found by find_sql(key(row)), else insert (always when find_sql is None).
        Rows referring to a parent in the same table are processed after their parent."""
        pk = XER_PK[table]
        pending = list(rows)
        while pending:
            progressed, later = False, []
            for r in pending:
                par = r.get(parent_col) if parent_col else None
                if par is not None and par not in self.idmap[table] and \
                        any(p[pk] == par for p in pending if p is not r):
                    later.append(r)
                    continue
                hit = find_sql and self.con.execute(find_sql + _alive(self.con, table), key(r)).fetchone()
                if hit:
                    self.idmap[table][r[pk]] = hit[0]
                    self.count(table, "matched")
                else:
                    self.idmap[table][r[pk]] = self.new_row(table, r)
                    self.created[table].add(r[pk])
                progressed = True
            if not progressed:  # parent cycle: insert the rest without parents
                for r in later:
                    self.idmap[table][r[pk]] = self.new_row(table, r)
                    self.created[table].add(r[pk])
                break
            pending = later

    def match_global(self, table: str, rows: list[dict], parent_col: Optional[str] = None) -> None:
        keys, sql = XER_MATCH[table]
        self.match_or_create(table, rows, lambda r: [r.get(k) for k in keys], sql, parent_col)


def _import_target(con, mode: str, project: Optional[str], xp: dict) -> Optional[dict]:
    """PROJECT row (lowercase keys) that an update or baseline import applies to; None in mode new."""
    if mode == "new":
        return None
    name = project or (xp["proj_short_name"] if mode == "update" else None)
    if not name:
        raise ValueError("baseline mode needs project= (the project the baseline belongs to)")
    return _project_row(con, name)


def _removable(remove_missing, mode: str) -> list[str]:
    rm = [str(t).upper() for t in ([remove_missing] if isinstance(remove_missing, str) else remove_missing or [])]
    if "TASK" in rm:
        raise ValueError("Activities are never deleted by an import: delete them in P6 (remove_missing takes "
                         f"{list(get_args(Removable))}).")
    bad = [t for t in rm if t not in get_args(Removable)]
    if bad:
        raise ValueError(f"remove_missing takes {list(get_args(Removable))}, not {bad}")
    if rm and mode != "update":
        raise ValueError("remove_missing only applies to mode='update'")
    return list(dict.fromkeys(rm))


def _missing_rows(imp: _XerImport, pid: int) -> tuple[dict, dict[str, list[int]]]:
    """Live rows of project pid that the update did not match or create: activities (never
    removed), relationships with both activities in pid (links to other projects are never
    touched) and assignments. Rows of an activity missing from the XER are counted apart
    (on_missing_activities) and never removed. Returns (report, removable ids per table)."""
    con, al = imp.con, functools.partial(_alive, imp.con)
    seen = set(imp.idmap["TASK"].values())
    acts = [r for r in con.execute("SELECT task_id, task_code, task_name, status_code FROM TASK WHERE proj_id = ?"
                                   + al("TASK") + " ORDER BY task_code, task_id", (pid,)) if r["task_id"] not in seen]
    gone = {r["task_id"] for r in acts}
    links = [r for r in con.execute(
        "SELECT r.task_pred_id, r.task_id, r.pred_task_id, r.pred_type, r.lag_hr_cnt, p.task_code AS pred_code, "
        "s.task_code AS succ_code FROM TASKPRED r JOIN TASK s ON s.task_id = r.task_id JOIN TASK p ON p.task_id = "
        "r.pred_task_id WHERE s.proj_id = ? AND p.proj_id = ?" + al("TASKPRED", "r") + al("TASK", "s") + al("TASK", "p")
        + " ORDER BY r.task_pred_id", (pid, pid)) if r["task_pred_id"] not in imp.touched["TASKPRED"]]
    rels = [r for r in links if not {r["task_id"], r["pred_task_id"]} & gone]
    asg = [r for r in con.execute(
        "SELECT a.taskrsrc_id, a.task_id, t.task_code, rs.rsrc_short_name FROM TASKRSRC a JOIN TASK t ON t.task_id = "
        "a.task_id" + al("TASK", "t") + " LEFT JOIN RSRC rs ON rs.rsrc_id = a.rsrc_id WHERE a.proj_id = ?"
        + al("TASKRSRC", "a") + " ORDER BY a.taskrsrc_id", (pid,)) if r["taskrsrc_id"] not in imp.touched["TASKRSRC"]]
    kept = [r for r in asg if r["task_id"] not in gone]
    report = {
        "TASK": {"count": len(acts), "sample": [f"{r['task_code']} {r['task_name']} ({r['status_code']})" for r in acts[:15]],
                 "note": "activities are never deleted by an import: delete them in P6 if they are obsolete"},
        "TASKPRED": {"count": len(rels), "by_type": dict(Counter(r["pred_type"] for r in rels)),
                     "sample": [f"{r['pred_code']} -> {r['succ_code']} {REL_TYPES_REV.get(r['pred_type'], r['pred_type'])}"
                                f" lag {float(r['lag_hr_cnt'] or 0):g} h" for r in rels[:15]],
                     "on_missing_activities": len(links) - len(rels)},
        "TASKRSRC": {"count": len(kept), "sample": [f"{r['task_code']} {r['rsrc_short_name'] or '(no resource)'}"
                                                    for r in kept[:15]], "on_missing_activities": len(asg) - len(kept)}}
    return report, {"TASKPRED": [r["task_pred_id"] for r in rels], "TASKRSRC": [r["taskrsrc_id"] for r in kept]}


def _remove_rows(imp: _XerImport, table: str, ids: list[int]) -> None:
    """Delete TASKPRED or TASKRSRC rows by id (P6 delete triggers log them); an assignment's UDF
    values and cost, hour and code rows go first."""
    con = imp.con
    if table == "TASKRSRC":
        deps = [("UDFVALUE", f"fk_id IN {_ids_sql(ids)} AND udf_type_id IN (SELECT udf_type_id FROM UDFTYPE "
                             "WHERE table_name = 'TASKRSRC')")] if _cols(con, "UDFTYPE") else []
        deps += [(t, f"taskrsrc_id IN {_ids_sql(ids)}") for t in ("TRSRCFIN", "RSRCHOUR", "ASGNMNTACAT")
                 if "taskrsrc_id" in _cols(con, t)]
        for t, where in deps:
            n = con.execute(f"DELETE FROM {t} WHERE {where}").rowcount
            if n:
                imp.count(t, "deleted", n)
    imp.count(table, "deleted", con.execute(f"DELETE FROM {table} WHERE {XER_PK[table]} IN {_ids_sql(ids)}").rowcount)


@_tool(read_only=False, destructive=True, idempotent=False)
def p6_import_xer(
    path: str,
    mode: ImportMode = "new",
    project: Optional[ProjectId] = None,
    new_short_name: Optional[str] = None,
    eps: Optional[str] = None,
    baseline_type: Optional[str] = None,
    set_as_project_baseline: bool = False,
    xer_project: Optional[str] = None,
    allow_older_data_date: bool = False,
    remove_missing: Optional[list[Removable]] = None,
    import_schedule_options: Optional[bool] = None,
    create_eps: Optional[NewEps] = None,
    confirm: bool = False,
) -> dict:
    """Import a P6 XER file. Dry run (full import then rollback, returns the counts) unless confirm=True.

    mode:
      new       Create a new project (also for a standalone baseline copy). eps = EPS id
                (p6_list_eps); new_short_name renames it (required if the project ID already exists).
      update    Update an existing project: `project` (default: the XER project ID). WBS matched by
                path, activities by Activity ID, relationships by pred/succ/type, assignments by
                activity + resource, codes and UDFs by type. Matches are updated, new items inserted.
                missing_from_xer reports the project's activities, relationships and assignments
                that are not in the XER. Nothing is deleted unless remove_missing lists 'TASKPRED'
                and/or 'TASKRSRC'; activities are never deleted. An XER whose data date is older
                than the project's is refused unless allow_older_data_date=True.
      baseline  Create a baseline attached to existing `project` (stored like P6 baselines,
                hidden from the EPS). new_short_name names it (default: the XER project ID).
                baseline_type = a BASETYPE name; set_as_project_baseline makes it the project baseline.

    import_schedule_options: write the XER SCHEDOPTIONS into the project's schedule options
    (PROJPROP scheduling, as p6_set_schedule_options from_xer; the key activity is mapped to the
    imported activity, -99 with a warning when the XER has no such activity). Default True for new
    and update, False for baseline; when off, the options that differ are listed in warnings.
    create_eps {eps_id, eps_name, parent_eps?} (mode new): the EPS node to put the project in,
    created in the same transaction when it does not exist (reused when it does). Without it, a
    database with no EPS node makes the dry run set would_refuse and confirm=True refuse.

    The file is checked first (preflight, as p6_inspect_xer): with pre-flight errors, or an older
    data date not allowed, confirm=True is refused before anything is locked or backed up, and the
    dry run sets would_refuse and refuse_reasons (it imports nothing when the XER has no project WBS
    node). The target of update and baseline must be a project, not an EPS node. data_date shows
    database -> XER.
    Global data (currencies, OBS, UDF types, calendars, resources, code types and codes) is matched
    by name and reused; missing items are created. Resource rates are only imported for new resources.
    xer_project selects the project when the XER contains several. Press F9 in P6 afterwards."""
    if mode not in get_args(ImportMode):
        raise ValueError("mode must be new, update or baseline")
    remove = _removable(remove_missing, mode)
    new_eps = None
    if create_eps is not None:
        if mode != "new":
            raise ValueError("create_eps only applies to mode='new'")
        new_eps = dict(create_eps)
        if not all(isinstance(new_eps.get(k), str) and new_eps[k].strip() for k in ("eps_id", "eps_name")):
            raise ValueError("create_eps needs eps_id and eps_name, e.g. {'eps_id': 'PV-01', 'eps_name': 'Solar PV'}")
        if eps is not None and eps != new_eps["eps_id"]:
            raise ValueError(f"eps={eps!r} and create_eps eps_id {new_eps['eps_id']!r} differ: give one")
    header, x, meta = _xer_read_meta(path)
    xp = _xer_project(x, xer_project)
    xpid = xp["proj_id"]
    x = _xer_only(x, xpid)  # rows of the chosen project and global rows
    con = _connect()
    try:  # read-only checks: a refused confirm takes no lock and no backup
        pf = _xer_preflight(header, x, meta, xer_project, con)
        target = _import_target(con, mode, project, xp)
        no_eps = mode == "new" and not _eps_nodes(con)
        if new_eps and new_eps.get("parent_eps") and not any(n["eps_id"] == new_eps["eps_id"] for n in _eps_nodes(con)):
            _eps_wbs_id(con, new_eps["parent_eps"])  # parent must exist
    finally:
        con.close()
    db_dd = _db_dt(target.get("last_recalc_date")) if mode == "update" else None
    x_dd = _db_dt(xp.get("last_recalc_date"))
    data_date = {"database": db_dd and _fmt_dt(db_dd), "xer": x_dd and _fmt_dt(x_dd),
                 "older": bool(db_dd and x_dd and x_dd < db_dd)}
    reasons = []
    if pf["errors"]:
        reasons.append("XER pre-flight failed (" + ", ".join(f"{e['check']} {e['count']}" for e in pf["errors"])
                       + "): fix the file, see preflight or p6_inspect_xer.")
    if no_eps and not new_eps:
        reasons.append("No EPS node in this database: pass create_eps={'eps_id': ..., 'eps_name': ...} to create one "
                       "in the same transaction (or p6_create_eps first).")
    if data_date["older"] and not allow_older_data_date:
        reasons.append(f"The XER data date {data_date['xer']} is older than the project's {data_date['database']}: "
                       "the update would move the data date back. Pass allow_older_data_date=True to import it anyway.")
    if reasons and confirm:
        raise ValueError("Import refused. " + " ".join(reasons))
    if any(e["check"] == "project_wbs_node_missing" for e in pf["errors"]):  # nothing to import into
        return {"dry_run": True, "would_do": [f"Import {os.path.basename(path)}: not possible, the XER has no "
                                              "project WBS node"], "counts": {}, "warnings": [], "data_date": data_date,
                "preflight": pf, "would_refuse": True, "refuse_reasons": reasons,
                "next": "confirm=True would be refused: " + " ".join(reasons)}

    with _write_session(confirm) as (con, backup):
        imp = _XerImport(con, x, mode)
        target = _import_target(con, mode, project, xp)
        short = new_short_name or xp["proj_short_name"]
        if mode != "update":
            if con.execute("SELECT 1 FROM PROJECT WHERE proj_short_name = ?" + _alive(con, "PROJECT"),
                           (short,)).fetchone():
                raise ValueError(f"Project ID {short!r} already exists: pass new_short_name=, "
                                 "or mode='update' to update it.")
            limit = _prefer(con, "proj_short_len") or 40
            if len(short) > limit:
                raise ValueError(f"Project ID is limited to {limit} characters in this database: pass a shorter "
                                 "new_short_name.")
        eps_txt = None
        if mode == "new":
            hit = new_eps and next((n for n in _eps_nodes(con) if n["eps_id"] == new_eps["eps_id"]), None)
            if hit:
                parent_wbs, eps_txt = hit["wbs_id"], f"EPS {hit['eps_id']} exists: used"
            elif new_eps:
                par = new_eps.get("parent_eps")
                parent_wbs = _create_eps(con, new_eps["eps_id"], new_eps["eps_name"],
                                         _eps_wbs_id(con, par) if par else None, imp.now)
                eps_txt = (f"Create EPS {new_eps['eps_id']} '{new_eps['eps_name']}'"
                           + (f" under {par}" if par else " (top level)"))
            elif not _eps_nodes(con):  # dry run only: confirm=True was refused above
                parent_wbs, eps_txt = None, "No EPS node: the project would have no parent (refused with confirm=True)"
            else:
                parent_wbs = _eps_wbs_id(con, eps)
        elif mode == "baseline":
            parent_wbs = con.execute("SELECT parent_wbs_id FROM PROJWBS WHERE proj_id = ? AND "
                                     "proj_node_flag = 'Y'", (target["proj_id"],)).fetchone()[0]

        # ---- global data, matched by name
        imp.match_global("CURRTYPE", x.get("CURRTYPE", []))
        imp.match_global("FINTMPL", x.get("FINTMPL", []))
        imp.match_global("OBS", x.get("OBS", []), parent_col="parent_obs_id")
        imp.match_global("UDFTYPE", x.get("UDFTYPE", []))

        # ---- project id first: project calendars and code types belong to it
        if mode == "update":
            pid = target["proj_id"]
        else:
            pid = _next_key(con, "PROJECT", "proj_id")
        imp.idmap["PROJECT"][xpid] = pid

        cals = x.get("CALENDAR", [])
        imp.match_global("CALENDAR", [c for c in cals if c.get("proj_id") is None], parent_col="base_clndr_id")
        proj_cal_sql = ("SELECT clndr_id FROM CALENDAR WHERE proj_id = ? AND clndr_name = ?"
                        if mode == "update" else None)
        imp.match_or_create("CALENDAR", [c for c in cals if c.get("proj_id") is not None],
                            lambda r: (pid, r["clndr_name"]), proj_cal_sql, parent_col="base_clndr_id")

        # ---- project row
        if mode == "update":
            imp.upd_row("PROJECT", pid, xp, {"proj_short_name": target["proj_short_name"],
                                             "orig_proj_id": target.get("orig_proj_id"),
                                             "sum_base_proj_id": target.get("sum_base_proj_id"),
                                             "project_flag": "Y"})
        else:
            extra = {"proj_short_name": short, "project_flag": "Y", "proj_id": pid,
                     "guid": _guid(), "sum_base_proj_id": None, "orig_proj_id": None}
            if mode == "baseline":
                extra["orig_proj_id"] = target["proj_id"]
                if baseline_type:
                    bt = con.execute("SELECT base_type_id FROM BASETYPE WHERE base_type = ?",
                                     (baseline_type,)).fetchone()
                    if not bt:
                        raise ValueError(f"Baseline type not found: {baseline_type!r}: "
                                         f"{[r[0] for r in con.execute('SELECT base_type FROM BASETYPE')]}")
                    extra["base_type_id"] = bt[0]
            row = imp.prep("PROJECT", xp)
            row.update(extra)
            row.update(_stamp_cols(con, "PROJECT", imp.now, create=True))
            _insert(con, "PROJECT", row)
            imp.count("PROJECT", "inserted")
            if mode == "baseline" and set_as_project_baseline:
                con.execute("UPDATE PROJECT SET sum_base_proj_id = ? WHERE proj_id = ?",
                            (pid, target["proj_id"]))

        # ---- resources, code types, codes
        imp.match_global("RSRC", x.get("RSRC", []), parent_col="parent_rsrc_id")
        types = x.get("ACTVTYPE", [])
        imp.match_global("ACTVTYPE", [t for t in types if t.get("proj_id") is None])
        proj_type_sql = ("SELECT actv_code_type_id FROM ACTVTYPE WHERE proj_id = ? AND actv_code_type = ?"
                         if mode == "update" else None)
        imp.match_or_create("ACTVTYPE", [t for t in types if t.get("proj_id") is not None],
                            lambda r: (pid, r["actv_code_type"]), proj_type_sql)
        imp.match_or_create("ACTVCODE", x.get("ACTVCODE", []),
                            lambda r: (imp.idmap["ACTVTYPE"].get(r["actv_code_type_id"]), r["short_name"],
                                       imp.idmap["ACTVCODE"].get(r.get("parent_actv_code_id"))),
                            "SELECT actv_code_id FROM ACTVCODE WHERE actv_code_type_id = ? AND short_name = ? "
                            "AND parent_actv_code_id IS ?", parent_col="parent_actv_code_id")
        for r in x.get("RSRCRATE", []):
            if r.get("rsrc_id") in imp.created["RSRC"]:
                imp.new_row("RSRCRATE", r)
            else:
                imp.count("RSRCRATE", "kept_existing")

        # ---- WBS: project node maps to the project node, others matched by path in update mode
        wbs = x.get("PROJWBS", [])
        by_id = {w["wbs_id"]: w for w in wbs}
        node = next((w for w in wbs if w.get("proj_node_flag") == "Y"), None)
        if node is None:
            raise ValueError("XER has no project WBS node.")
        if mode == "update":
            db_node = con.execute("SELECT wbs_id FROM PROJWBS WHERE proj_id = ? AND proj_node_flag = 'Y'"
                                  + _alive(con, "PROJWBS"), (pid,)).fetchone()[0]
            imp.idmap["PROJWBS"][node["wbs_id"]] = db_node
            imp.count("PROJWBS", "matched")
        else:
            imp.idmap["PROJWBS"][node["wbs_id"]] = imp.new_row(
                "PROJWBS", node, {"parent_wbs_id": parent_wbs, "wbs_short_name": short})

        def depth(w, n=0):
            p = by_id.get(w.get("parent_wbs_id"))
            return n if p is None or n > 100 else depth(p, n + 1)

        for w in sorted((w for w in wbs if w is not node), key=depth):
            parent = imp.idmap["PROJWBS"].get(w.get("parent_wbs_id"), imp.idmap["PROJWBS"][node["wbs_id"]])
            hit = None
            if mode == "update":
                hit = con.execute("SELECT wbs_id FROM PROJWBS WHERE proj_id = ? AND parent_wbs_id = ? "
                                  "AND wbs_short_name = ?" + _alive(con, "PROJWBS"),
                                  (pid, parent, w["wbs_short_name"])).fetchone()
            if hit:
                imp.idmap["PROJWBS"][w["wbs_id"]] = hit[0]
                imp.upd_row("PROJWBS", hit[0], w, {"parent_wbs_id": parent, "proj_id": pid})
            else:
                imp.idmap["PROJWBS"][w["wbs_id"]] = imp.new_row("PROJWBS", w, {"parent_wbs_id": parent})

        # ---- activities
        for t in x.get("TASK", []):
            hit = None
            if mode == "update":
                hit = con.execute("SELECT task_id FROM TASK WHERE proj_id = ? AND task_code = ?"
                                  + _alive(con, "TASK"), (pid, t["task_code"])).fetchone()
            if hit:
                imp.idmap["TASK"][t["task_id"]] = hit[0]
                imp.upd_row("TASK", hit[0], t)
            else:
                imp.idmap["TASK"][t["task_id"]] = imp.new_row("TASK", t)

        # ---- relationships (one link per pair and type: a repeated XER row is skipped)
        seen_links = set()
        for r in x.get("TASKPRED", []):
            succ, pred = imp.idmap["TASK"].get(r["task_id"]), imp.idmap["TASK"].get(r["pred_task_id"])
            if succ is None or pred is None:
                imp.count("TASKPRED", "skipped_external")
                continue
            if (succ, pred, r.get("pred_type")) in seen_links:
                imp.count("TASKPRED", "skipped_duplicate")
                continue
            seen_links.add((succ, pred, r.get("pred_type")))
            hit = None
            if mode == "update":
                hit = con.execute("SELECT task_pred_id FROM TASKPRED WHERE task_id = ? AND pred_task_id = ? "
                                  "AND pred_type = ?" + _alive(con, "TASKPRED"),
                                  (succ, pred, r["pred_type"])).fetchone()
            if hit:
                imp.upd_row("TASKPRED", hit[0], r)
                imp.touched["TASKPRED"].add(hit[0])
            else:
                imp.touched["TASKPRED"].add(imp.new_row("TASKPRED", r))

        # ---- assignments (a database row is matched once: touched holds matched and new ids)
        used = imp.touched["TASKRSRC"]
        for r in x.get("TASKRSRC", []):
            task, rsrc = imp.idmap["TASK"].get(r["task_id"]), imp.idmap["RSRC"].get(r.get("rsrc_id"))
            if task is None:
                imp.count("TASKRSRC", "skipped_unmapped")
                continue
            hit = None
            if mode == "update":
                hit = next((h[0] for h in con.execute(
                    "SELECT taskrsrc_id FROM TASKRSRC WHERE task_id = ? AND rsrc_id IS ?"
                    + _alive(con, "TASKRSRC"), (task, rsrc)) if h[0] not in used), None)
            if hit:
                imp.idmap["TASKRSRC"][r["taskrsrc_id"]] = hit
                imp.upd_row("TASKRSRC", hit, r)
            else:
                hit = imp.idmap["TASKRSRC"][r["taskrsrc_id"]] = imp.new_row("TASKRSRC", r)
            used.add(hit)

        # ---- activity code assignments and UDF values (upsert on their composite keys)
        for r in x.get("TASKACTV", []):
            row = imp.prep("TASKACTV", r)
            if None in (row.get("task_id"), row.get("actv_code_type_id"), row.get("actv_code_id")):
                imp.count("TASKACTV", "skipped_unmapped")
                continue
            _upsert(imp, "TASKACTV", row, ("task_id", "actv_code_type_id"))
        udf_tables = {u["udf_type_id"]: u["table_name"] for u in x.get("UDFTYPE", [])}
        for r in x.get("UDFVALUE", []):
            tbl = udf_tables.get(r["udf_type_id"])
            row = imp.prep("UDFVALUE", r)
            row["fk_id"] = imp.idmap.get(tbl, {}).get(r["fk_id"]) if tbl in UDF_FK_TABLES else None
            if row.get("udf_type_id") is None or row["fk_id"] is None:
                imp.count("UDFVALUE", "skipped_unmapped")
                continue
            _upsert(imp, "UDFVALUE", row, ("udf_type_id", "fk_id"))

        # ---- update: rows of the project missing from the XER, deleted only when asked (never activities)
        missing, gone_txt = None, []
        if mode == "update":
            missing, drop = _missing_rows(imp, pid)
            for t, label in (("TASKPRED", "relationships"), ("TASKRSRC", "assignments")):
                missing[t]["removed"] = t in remove and bool(drop[t])
                if missing[t]["removed"]:
                    _remove_rows(imp, t, drop[t])
                    gone_txt.append(f"Delete {len(drop[t])} {label} missing from the XER (remove_missing)")
                elif drop[t]:
                    gone_txt.append(f"Keep {len(drop[t])} {label} missing from the XER "
                                    f"(remove_missing=['{t}'] deletes them)")

        do_opts = (mode != "baseline") if import_schedule_options is None else bool(import_schedule_options)
        sched = None
        if x.get("SCHEDOPTIONS") and do_opts and _xer_sched(x, xp):
            sched, w = _sched_write(con, pid, (x, xp, imp.idmap["TASK"].get), {}, None, None, imp.now)
            imp.warnings += w
            imp.count("PROJPROP", sched["action"])
        elif x.get("SCHEDOPTIONS"):
            imp.warnings.append("SCHEDOPTIONS not imported (import_schedule_options False): check Schedule Options "
                                "in P6 before F9, or use p6_set_schedule_options from_xer.")
            if target:
                db_opts, lag_db = _sched_options(con, target["proj_id"])[0], _lag_rule(con, target["proj_id"])[0]
            else:
                db_opts, lag_db = {}, "rcal_Predecessor"
            xcodes = {t["task_id"]: t["task_code"] for t in x.get("TASK", [])}
            diff = [o for o in _sched_diff(db_opts, _xer_sched(x, xp), lambda v: _db_task_code(con, v), xcodes.get)
                    if o["same"] is False or (o["option"] not in db_opts and o["option"] != "levelprioritylist")]
            where = f"project {target['proj_short_name']}" if target else "the new project"
            if diff and db_opts:
                imp.warnings.append(f"XER schedule options that differ from {where}: " + "; ".join(
                    f"{o['option']} XER {o['xer']}, database {o['db'] if o['option'] in db_opts else 'not stored'}"
                    for o in diff))
            elif diff:
                imp.warnings.append(f"No schedule options stored for {where} (P6 defaults apply, lag rule "
                                    f"{lag_db}). XER options: " + "; ".join(f"{o['option']} {o['xer']}" for o in diff))
        skipped = sorted(set(x) - set(XER_TABLES) - {"SCHEDOPTIONS"})
        if skipped:
            imp.warnings.append(f"Tables not handled, ignored: {skipped}")

        target_txt = {"new": f"new project {short}", "baseline": f"baseline {short} of {target and target['proj_short_name']}",
                      "update": f"update of {target and target['proj_short_name']}"}[mode]
        plan = [f"Import {os.path.basename(path)} (P6 {header[1] if len(header) > 1 else '?'}, "
                f"project {xp['proj_short_name']}) as {target_txt}"]
        if eps_txt:
            plan.append(eps_txt)
        if sched:
            ch = [o for o in sched["options"] if o["changed"]]
            plan.append(f"Schedule options: XER SCHEDOPTIONS written to PROJPROP scheduling ({len(ch)} values "
                        f"change, row {sched['action']})")
        if mode == "update":
            plan.append(f"Data date: database {data_date['database']} -> XER {data_date['xer']}" + (
                (" (older, allowed)" if allow_older_data_date else " (older)") if data_date["older"] else ""))
            plan.append(f"Missing from the XER: {missing['TASK']['count']} activities (kept), "
                        f"{missing['TASKPRED']['count']} relationships, {missing['TASKRSRC']['count']} assignments")
            plan += gone_txt
        else:
            plan.append(f"Data date: XER {data_date['xer']}")
        if backup: plan.append(f"Backup: {backup}")
        # a new project or baseline holds the XER's own F9 results; an update mixes old and new data
        sched_rows = _opt_rows(con, sched["options"]) if sched else None
        res = _commit(con, plan, confirm, projects=(pid,) if mode == "update" else (),
                      log=[{"operation": f"import_xer_{mode}", "proj_id": pid, "table": None,
                            "keys": {"proj_id": pid, "file": os.path.abspath(path), "xer_project": xp["proj_short_name"]},
                            "after": {"counts": imp.stats}, "summary": plan[0]}])
        res.update({"counts": imp.stats, "warnings": imp.warnings, "data_date": data_date, "preflight": pf})
        if missing is not None:
            res["missing_from_xer"] = missing
        res["schedule_options"] = {"imported": sched is not None, "action": sched and sched["action"],
                                   "changed": [o for o in sched_rows if o["changed"]] if sched_rows else []}
        if not confirm:
            res.update(would_refuse=bool(reasons), refuse_reasons=reasons)
            if reasons:
                res["next"] = "confirm=True would be refused: " + " ".join(reasons)
        return res


@_tool()
def p6_inspect_xer(path: str, xer_project: Optional[str] = None, db_preview: bool = True) -> dict:
    """Read an XER file without importing it: header, encoding (UTF-8, else cp1252 with the offset of
    the first non UTF-8 byte), BOM, %E end marker, rows per table, projects (Project ID, data date,
    activities) and the pre-flight p6_import_xer runs: errors (they block a confirmed import),
    warnings, info, tables not imported, XER columns this database lacks (dropped_columns) and,
    with db_preview, the global items (calendars, resources, code types...) the import would reuse
    or create. Read only. xer_project picks the project when the file holds several."""
    header, x, meta = _xer_read_meta(path)
    res = {"path": path, "header": header, **meta, "tables": {t: len(r) for t, r in x.items()},
           "projects": [{"short_name": p.get("proj_short_name"),
                         "data_date": _fmt_dt(d) if (d := _db_dt(p.get("last_recalc_date"))) else None,
                         "activities": sum(1 for t in x.get("TASK", []) if t.get("proj_id") == p.get("proj_id"))}
                        for p in x.get("PROJECT", [])]}
    if xer_project is None and len(res["projects"]) != 1:
        res.update(preflight=None, note="Several projects: pass xer_project= for the pre-flight."
                   if res["projects"] else "No PROJECT row: nothing to import.")
        return res
    try:
        con = _connect()
    except RuntimeError as e:
        con, res["db_note"] = None, str(e)
    try:
        res["preflight"] = _xer_preflight(header, x, meta, xer_project, con, db_preview)
    finally:
        if con is not None:
            con.close()
    return res


def _upsert(imp: _XerImport, table: str, row: dict, key: tuple) -> None:
    where = {k: row[k] for k in key}
    if imp.con.execute(f"SELECT 1 FROM {table} WHERE " + " AND ".join(k + "=?" for k in key),
                       list(where.values())).fetchone():
        revive = {c: None for c in ("delete_session_id", "delete_date") if c in _cols(imp.con, table)}  # a soft-deleted row comes back
        _update(imp.con, table, where, {**row, **revive, **_stamp_cols(imp.con, table, imp.now, create=False)})
        imp.count(table, "updated")
    else:
        _insert(imp.con, table, {**row, **_stamp_cols(imp.con, table, imp.now, create=True)})
        imp.count(table, "inserted")


def _ids_sql(ids) -> str:
    return "(" + ",".join(str(int(i)) for i in ids) + ")" if ids else "(NULL)"


@_tool(read_only=False, destructive=True)
def p6_export_xer(project: ProjectId, path: str, tables: Optional[list[XerTable]] = None,
                  xer_version: Optional[str] = None, overwrite: bool = False) -> dict:
    """Export one project to an XER file (read only on the database). An existing file is refused
    unless overwrite=True.
    tables: which XER tables to write, any of
      CURRTYPE, FINTMPL, OBS, UDFTYPE, PROJECT, CALENDAR, SCHEDOPTIONS, PROJWBS, RSRC, ACTVTYPE,
      RSRCRATE, TASK, ACTVCODE, TASKPRED, TASKRSRC, TASKACTV, UDFVALUE (default: all).
    Global data is limited to what the project uses (calendars, resources, codes, UDF types, OBS).
    SCHEDOPTIONS carries the project's schedule options (skipped with a warning when none are stored).
    xer_version: version written in the header (default: this database's P6 version); lower it
    when the file must be imported into an older P6."""
    tables = [t.upper() for t in (tables or XER_EXPORT_TABLES)]
    bad = [t for t in tables if t not in XER_EXPORT_TABLES]
    if bad:
        raise ValueError(f"Unknown tables {bad}; choose from {XER_EXPORT_TABLES}")
    path = _export_target(path)
    if os.path.exists(path) and not overwrite:
        raise ValueError(f"{path} already exists: pass overwrite=True to replace it, or choose another path.")
    with closing(_connect()) as con:
        return _export_xer(con, project, path, tables, xer_version)


def _export_target(path: str, ext: str = ".xer") -> str:
    """Absolute export path: a file ending with ext (.xer, .xlsx) in an existing folder (never the
    database, a backup or a log)."""
    if not path or not path.lower().endswith(ext):
        raise ValueError(f"Export path must end with {ext}: {path!r}")
    path = os.path.abspath(path)
    if not os.path.isdir(os.path.dirname(path)):
        raise ValueError(f"Folder not found: {os.path.dirname(path)!r}")
    if os.path.isdir(path):
        raise ValueError(f"{path!r} is a folder")
    return path


def _export_xer(con: sqlite3.Connection, project: str, path: str, tables: list[str],
                xer_version: Optional[str]) -> dict:
    pid = _project_id(con, project)
    al = lambda t: _alive(con, t)  # noqa: E731
    one = lambda sql: {r[0] for r in con.execute(sql) if r[0] is not None}  # noqa: E731

    tasks = one(f"SELECT task_id FROM TASK WHERE proj_id = {pid}{al('TASK')}")
    trsrc = one(f"SELECT taskrsrc_id FROM TASKRSRC WHERE proj_id = {pid}{al('TASKRSRC')}")
    wbs = one(f"SELECT wbs_id FROM PROJWBS WHERE proj_id = {pid}{al('PROJWBS')}")
    rsrc = one(f"SELECT rsrc_id FROM TASKRSRC WHERE proj_id = {pid}{al('TASKRSRC')}") | \
        one(f"SELECT rsrc_id FROM TASK WHERE proj_id = {pid}{al('TASK')}")
    while True:  # parents of used resources
        more = one(f"SELECT parent_rsrc_id FROM RSRC WHERE rsrc_id IN {_ids_sql(rsrc)}") - rsrc
        if not more: break
        rsrc |= more
    cals = one(f"SELECT clndr_id FROM PROJECT WHERE proj_id = {pid}") | \
        one(f"SELECT clndr_id FROM TASK WHERE proj_id = {pid}{al('TASK')}") | \
        one(f"SELECT clndr_id FROM RSRC WHERE rsrc_id IN {_ids_sql(rsrc)}") | \
        one(f"SELECT clndr_id FROM CALENDAR WHERE proj_id = {pid}{al('CALENDAR')}")
    while True:
        more = one(f"SELECT base_clndr_id FROM CALENDAR WHERE clndr_id IN {_ids_sql(cals)}") - cals
        if not more: break
        cals |= more
    types = one(f"SELECT actv_code_type_id FROM TASKACTV WHERE task_id IN {_ids_sql(tasks)}") | \
        one(f"SELECT actv_code_type_id FROM ACTVTYPE WHERE proj_id = {pid}{al('ACTVTYPE')}")
    obs = one(f"SELECT obs_id FROM PROJWBS WHERE proj_id = {pid}{al('PROJWBS')}")
    while True:
        more = one(f"SELECT parent_obs_id FROM OBS WHERE obs_id IN {_ids_sql(obs)}") - obs
        if not more: break
        obs |= more
    udf_where = (f"(u.table_name = 'TASK' AND v.fk_id IN {_ids_sql(tasks)}) OR "
                 f"(u.table_name = 'PROJWBS' AND v.fk_id IN {_ids_sql(wbs)}) OR "
                 f"(u.table_name = 'TASKRSRC' AND v.fk_id IN {_ids_sql(trsrc)}) OR "
                 f"(u.table_name = 'RSRC' AND v.fk_id IN {_ids_sql(rsrc)}) OR "
                 f"(u.table_name = 'PROJECT' AND v.fk_id = {pid})")
    udf_types = one(f"SELECT v.udf_type_id FROM UDFVALUE v JOIN UDFTYPE u ON u.udf_type_id = v.udf_type_id "
                    f"WHERE {udf_where}")

    where = {
        "CURRTYPE": "1=1",
        "FINTMPL": f"fintmpl_id IN (SELECT fintmpl_id FROM PROJECT WHERE proj_id = {pid})",
        "OBS": f"obs_id IN {_ids_sql(obs)}",
        "UDFTYPE": f"udf_type_id IN {_ids_sql(udf_types)}",
        "PROJECT": f"proj_id = {pid}",
        "CALENDAR": f"clndr_id IN {_ids_sql(cals)}",
        "PROJWBS": f"proj_id = {pid}",
        "RSRC": f"rsrc_id IN {_ids_sql(rsrc)}",
        "ACTVTYPE": f"actv_code_type_id IN {_ids_sql(types)}",
        "RSRCRATE": f"rsrc_id IN {_ids_sql(rsrc)}",
        "TASK": f"proj_id = {pid}",
        "ACTVCODE": f"actv_code_type_id IN {_ids_sql(types)}",
        "TASKPRED": f"task_id IN {_ids_sql(tasks)} AND pred_task_id IN {_ids_sql(tasks)}",
        "TASKRSRC": f"proj_id = {pid}",
        "TASKACTV": f"task_id IN {_ids_sql(tasks)}",
        "UDFVALUE": f"EXISTS (SELECT 1 FROM UDFTYPE u WHERE u.udf_type_id = v.udf_type_id AND ({udf_where}))",
    }
    if xer_version is None:
        try:
            ver = con.execute("SELECT * FROM PREFER").fetchone() or ()
            ver = next((str(v) for v in ver if isinstance(v, str) and re.fullmatch(r"\d\d\.\d+\.\d+", v)), "")
            xer_version = ".".join(ver.split(".")[:2]) or "24.12"
        except sqlite3.Error:
            xer_version = "24.12"
    base_curr = con.execute("SELECT curr_short_name FROM CURRTYPE WHERE base_exch_rate = 1"
                            + al("CURRTYPE")).fetchone()
    lines = ["\t".join(["ERMHDR", xer_version, datetime.now().strftime("%Y-%m-%d"), "Project",
                        P6_USER, P6_USER, "dbxDatabaseNoName", "Project Management",
                        base_curr[0] if base_curr else "USD"])]
    counts, lossy, warn = {}, {}, []
    for t in tables:
        if t == "SCHEDOPTIONS":
            vals, w = _xer_sched_export(con, pid, tasks)
            warn += w
            if vals is not None:
                counts[t] = 1
                lines += [f"%T\t{t}", "%F\t" + "\t".join(SCHED_XER_COLS),
                          "%R\t" + "\t".join(_xer_encode(v) for v in vals)]
            continue
        cols = [r[1].lower() for r in con.execute(f"PRAGMA table_info({t})")
                if r[1].lower() not in ("delete_session_id", "delete_date")]
        if not cols:
            warn.append(f"{t} is not in this database: not exported.")
            continue
        alias = " v" if t == "UDFVALUE" else ""
        rows = con.execute(f"SELECT {', '.join(cols)} FROM {t}{alias} WHERE {where[t]}"
                           + _alive(con, t, alias.strip())).fetchall()
        counts[t] = len(rows)
        lines.append(f"%T\t{t}")
        lines.append("%F\t" + "\t".join(cols))
        key = cols.index("task_code") if "task_code" in cols else 0
        for r in rows:
            vals = [_xer_encode(v) for v in r]
            if not all(_cp1252_ok(v) for v in vals):
                lossy.setdefault(t, []).append(vals[key])
            lines.append("%R\t" + "\t".join(vals))
    lines.append("%E")
    tmp = path + ".tmp"
    try:  # written beside the target, then swapped in: a failed export leaves the old file intact
        with open(tmp, "w", encoding="cp1252", errors="replace", newline="") as fh:
            fh.write("\r\n".join(lines) + "\r\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    for t, keys in lossy.items():
        warn.append(f"{t}: {len(keys)} row(s) with characters outside cp1252, written as '?' "
                    f"(first: {', '.join(keys[:10])}).")
    deps = {"TASK": ["PROJWBS", "CALENDAR"], "TASKPRED": ["TASK"], "TASKRSRC": ["TASK", "RSRC"],
            "TASKACTV": ["TASK", "ACTVTYPE", "ACTVCODE"], "UDFVALUE": ["UDFTYPE"],
            "PROJWBS": ["PROJECT"], "RSRCRATE": ["RSRC"], "ACTVCODE": ["ACTVTYPE"]}
    for t, needs in deps.items():
        miss = [n for n in needs if t in tables and n not in tables]
        if miss:
            warn.append(f"{t} exported without {miss}: P6 may not resolve those references on import.")
    return {"path": path, "xer_version": xer_version, "rows": counts, "warnings": warn}


def _cp1252_ok(v: str) -> bool:
    try:
        v.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


# ----------------------------------------------------------------- dashboard export (Excel)
# p6_export_dashboard gathers the report tools' results (each from its own snapshot: the data date and
# the last F9 are compared at the start and the end) and writes a workbook built by _dash_book with
# openpyxl, imported on use so the server runs without it. The template is this code: sheets are sized
# to the data, values are written as values (the file is regenerated at every update) and charts are
# native Excel charts.

DASH_WBS_ROWS = 5000  # WBS bands of the Schedule Overview sheet
DASH_MILESTONES = 30  # milestones listed without milestones= (every milestone, in date order)
DASH_MILESTONES_MAX = 50  # Activity IDs milestones= accepts
DASH_PANEL_MILESTONES = 11  # milestones on the one-page dashboard
DCMA_LABELS = {"logic": "Logic (no predecessor or successor)", "leads": "Leads (negative lags)", "lags": "Lags",
               "long_lags": "Long lags", "fs_links": "Finish to start links", "hard_constraints": "Hard constraints",
               "high_float": "High float", "negative_float": "Negative float", "high_duration": "High duration",
               "invalid_dates": "Invalid dates", "resources": "Resources", "missed_tasks": "Missed tasks",
               "critical_path_test": "Critical path test", "cpli": "Critical path length index (CPLI)",
               "bei": "Baseline execution index (BEI)", "key_dates": "Key dates (information only)"}
# Colors (RGB): text, rules, headers; float classes (told apart by lightness too, always labeled); tints
# for cells; good status.
DASH_INK, DASH_MUTED, DASH_RULE, DASH_HEAD = "1F2937", "6B7280", "D1D5DB", "1F3A5F"
DASH_FLOAT = {"neg": "B2182B", "near": "E69F00", "ok": "0072B2"}  # negative, 0 to near_critical_days, above
DASH_TINT = {"neg": "F6D5D9", "near": "FCEBC7", "ok": "D6E7F3", "pass": "DCEFE2", "fail": "F6D5D9", "na": "EEF0F3"}
DASH_GOOD = "1B7F3B"
# Sheets in order, with the sidebar glyph of each (Segoe UI Symbol); the Dashboard sidebar links to the others
DASH_SHEETS = ("Dashboard", "Schedule Overview", "Critical Path", "Reports")
DASH_NAV = {"Dashboard": "⌂", "Schedule Overview": "▤", "Critical Path": "◎", "Reports": "▥"}
DASH_ACT_CAP = 20000  # activities of the Schedule Overview sheet
DASH_TABLE_ROWS = 12  # critical and delayed activities on the Dashboard sheet
DASH_PAGE = "F3F5F8"  # page background of the Dashboard sheet (cards and panels are white)
DASH_STATUS = {"Delayed": "B2182B", "At risk": "E69F00", "On track": "009E73", "Completed": "9CA3AF"}  # finish variance
DASH_SO_LEVEL = 2  # the Schedule Overview opens folded to the WBS bands of this level (activities hidden)
DASH_WBS_BARS = 20  # WBS per progress chart of the Dashboard (more: the ones with the most budget units)
DASH_PILL = {"Critical": ("F6D5D9", "B2182B"), "Delayed": ("FCEBC7", "8A5300"), "At risk": ("FDF3D7", "8A5300"),
             "On track": ("DCEFE2", "1B7F3B"), "Completed": ("EEF0F3", "4B5563"), "No baseline": ("EEF0F3", "4B5563")}


def _openpyxl():
    """The openpyxl module (a dependency, imported on use), or a ValueError saying how to get it back."""
    try:
        import openpyxl
    except ImportError:
        if getattr(sys, "frozen", False):
            raise ValueError("p6_export_dashboard needs the openpyxl package, missing from this P6 MCP build: "
                             "reinstall P6 MCP") from None
        raise ValueError(f"p6_export_dashboard needs the openpyxl package: run '\"{sys.executable}\" -m pip install "
                         "openpyxl', then restart the server") from None
    return openpyxl


def _openpyxl_version() -> Optional[str]:
    try:
        return _openpyxl().__version__
    except ValueError:
        return None


def _dash_brand(company: Optional[str] = None, colors: Optional[str] = None) -> dict:
    """Dashboard branding from the export's own arguments (nothing is stored): company name (at most
    120 characters) and colors 'primary[,accent]' in hex, else the defaults. Raises before anything is
    written when a value cannot be used."""
    name = (str(company).strip() or None) if company is not None else None
    if name and len(name) > 120:
        raise ValueError("company: at most 120 characters")
    out: dict[str, Any] = {"company": name, "primary": DASH_HEAD, "accent": "0072B2"}
    cols = [c.strip().lstrip("#") for c in str(colors or "").split(",") if c.strip()]
    if len(cols) > 2:
        raise ValueError("colors: at most two hex colors, 'primary,accent' (e.g. '1F3A5F,0072B2')")
    for key, c in zip(("primary", "accent"), cols):
        if not re.fullmatch(r"[0-9A-Fa-f]{6}", c):
            raise ValueError(f"colors: {c!r} is not a hex color such as 1F3A5F")
        out[key] = c.upper()
    return out


def _ink_on(color: str) -> str:
    """Text color for a fill: white or the ink color, whichever contrasts more (WCAG luminance)."""
    def lum(c: str) -> float:
        ch = [int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        ch = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
    fill = lum(color)
    return "FFFFFF" if 1.05 / (fill + 0.05) >= (fill + 0.05) / (lum(DASH_INK) + 0.05) else DASH_INK


def _all_rows(fn: Callable[..., dict], cap: int, **kw) -> tuple[dict, list[dict], bool]:
    """Rows of every page of a list tool, up to cap: (first page, rows, cut at cap)."""
    first = res = fn(offset=0, limit=MAX_ROWS, **kw)
    rows = list(res["rows"])
    while res["next_offset"] is not None and len(rows) < cap:
        res = fn(offset=res["next_offset"], limit=MAX_ROWS, **kw)
        rows += res["rows"]
    return first, rows[:cap], len(rows) > cap or res["next_offset"] is not None


def _dash_read(con, pid: int, bid: Optional[int], codes: Optional[Sequence[str]], baseline_dates: str) -> dict:
    """What the dashboard reads itself, in the caller's snapshot: the project and baseline dates; every
    activity (baseline dates of the baseline activity with the same Activity ID, slip in working days of
    its own calendar, % complete by its percent complete type, labor units, original, remaining and
    actual durations, P6 float path); every WBS node in P6 order with its units % (as p6_list_wbs), the
    baseline planned units % at the data date, the finish variance (latest baseline finish to latest
    finish of its activities) and the band values of its activities and of the WBS below it (dates,
    labor units, % complete; finish variance and durations in working days of the project calendar); the
    milestones (milestones= codes, else every milestone) with their baseline date and variance; the
    project figures in working days of the project calendar."""
    al = functools.partial(_alive, con)
    pc = _cols(con, "PROJECT")
    pf = ("proj_short_name", "last_recalc_date", "last_schedule_date", "scd_end_date", "plan_end_date", "clndr_id")

    def proj(p: int) -> dict:
        return _dict(con.execute("SELECT " + ", ".join(c if c in pc else f"NULL AS {c}" for c in pf)
                                 + " FROM PROJECT WHERE proj_id = ?", (p,)).fetchone())
    prj, base = proj(pid), proj(bid) if bid is not None else None
    cals, wd_notes = _calendars(con), set()
    pcal = cals.get(prj["clndr_id"]) if prj["clndr_id"] is not None else None

    def wd(cal: Optional[_Cal], a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
        """Working days from a to b (negative when b is earlier), 1 decimal; calendar days when the
        calendar is missing or unreadable (noted)."""
        if a is None or b is None:
            return None
        if cal is None or cal.error:
            wd_notes.add("a calendar is missing or unreadable: calendar days used instead of working days there")
            return round((b - a).total_seconds() / 86400, 1)
        return round(cal.work_days(a, b), 1) + 0.0  # + 0.0: -0.0 becomes 0.0
    sep = _name_sep(con, pid)
    nodes = _wbs_tree(con, pid, sep)
    if not nodes:
        raise ValueError(f"Project {prj['proj_short_name']} has no project WBS node.")
    own = {r["wbs_id"]: {k: r[k] for k in r.keys() if k != "wbs_id"} for r in con.execute(
        f"SELECT t.wbs_id AS wbs_id, {_act_agg_sql(con)} FROM TASK t LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id "
        "WHERE t.proj_id = ?" + al("TASK", "t") + " GROUP BY t.wbs_id", (pid,))}

    def bdate(x: str, kind: str) -> str:  # baseline date: actual else early ('current'), or planned
        return (f"COALESCE({x}.act_{kind}_date, {x}.early_{kind}_date)" if baseline_dates == "current"
                else f"{x}.target_{kind}_date")
    bl_finish, bl_act = None, {}  # baseline project finish (its latest activity date); dates by Activity ID
    if bid is not None:
        bl_finish = _db_dt(con.execute(f"SELECT MAX({bdate('x', 'end')}) FROM TASK x WHERE x.proj_id = ?" + al("TASK", "x"),
                                       (bid,)).fetchone()[0])
        bl_act = {r[0]: (_db_dt(r[1]), _db_dt(r[2])) for r in con.execute(
            f"SELECT x.task_code, {bdate('x', 'start')}, {bdate('x', 'end')} FROM TASK x WHERE x.proj_id = ?"
            + al("TASK", "x") + " ORDER BY x.task_id DESC", (bid,))}  # lowest task_id wins
    dd = _db_dt(prj["last_recalc_date"])
    top = nodes[0]["wbs_code"]
    code_of = {n["wbs_id"]: n["wbs_code"][len(top) + len(sep):] if n["depth"] else top for n in nodes}
    acts = []
    for r in con.execute(_act_from(con) + " ORDER BY COALESCE(t.act_start_date, t.early_start_date), t.task_code, "
                         "t.task_id", (pid,)):
        a = _dict(r)
        num = lambda k: float(a.get(k) or 0)  # noqa: E731
        bs, bf = bl_act.get(a["task_code"], (None, None))
        s_act, f_act = _db_dt(a["act_start_date"]), _db_dt(a["act_end_date"])
        f = f_act or _db_dt(a["early_end_date"])
        done = a["status_code"] == "TK_Complete"
        orig_h, rem_h = num("target_drtn_hr_cnt"), num("remain_drtn_hr_cnt")
        kind = a.get("complete_pct_type")  # the activity's % complete as P6 shows it, by its percent complete type
        if kind == "CP_Drtn":
            pct = 100 * (orig_h - rem_h) / orig_h if orig_h else 100.0 * done
        elif kind == "CP_Units":
            at = sum(num(f"{k}_{u}_qty") for k in ("act", "remain") for u in ("work", "equip"))
            pct = 100 * (num("act_work_qty") + num("act_equip_qty")) / at if at else 100.0 * done
        else:
            pct = num("phys_complete_pct")
        end = f_act or dd
        acts.append({"task_code": a["task_code"], "task_name": a["task_name"], "task_type": a["task_type"],
                     "status_code": a["status_code"], "wbs_id": a["wbs_id"], "wbs_name": a["wbs_name"],
                     "wbs_code": code_of.get(a["wbs_id"]), "start": _db_dt(a["act_start_date"] or a["early_start_date"]),
                     "finish": f, "bl_start": bs, "bl_finish": bf, "slip_d": wd(cals.get(a["clndr_id"]), bf, f),
                     "act_start": s_act, "act_finish": f_act, "plan_start": _db_dt(a["target_start_date"]),
                     "plan_finish": _db_dt(a["target_end_date"]), "pct": round(min(100.0, max(0.0, pct)), 2),
                     "lab_t": num("target_work_qty"), "lab_a": num("act_work_qty"), "lab_r": num("remain_work_qty"),
                     "orig_h": orig_h, "rem_h": rem_h, "orig_d": _days(a["target_drtn_hr_cnt"], a["hpd"]),
                     "rem_d": _days(a["remain_drtn_hr_cnt"], a["hpd"]),
                     "act_d": max(0.0, wd(cals.get(a["clndr_id"]), s_act, end)) if s_act and end else None,
                     "tf_d": _days(a["total_float_hr_cnt"], a["hpd"]) if not done else None,
                     "longest": a.get("driving_path_flag") == "Y", "float_path": a.get("float_path"),
                     "float_path_order": a.get("float_path_order")})
    # baseline planned units at the data date per WBS (baseline assignments by the Activity ID of the current
    # activities, linear between their planned dates as p6_progress_curve), rolled up below
    bpl: dict[int, dict] = {}
    if bid is not None and dd is not None and _cols(con, "TASKRSRC"):
        wbs_of = {a["task_code"]: a["wbs_id"] for a in acts}
        rtype = "COALESCE(tr.rsrc_type, r.rsrc_type)" if "rsrc_type" in _cols(con, "TASKRSRC") else "r.rsrc_type"
        for code, q, s, e in con.execute(
                "SELECT x.task_code, COALESCE(tr.target_qty, 0), tr.target_start_date, tr.target_end_date FROM TASKRSRC tr "
                "JOIN TASK x ON x.task_id = tr.task_id LEFT JOIN RSRC r ON r.rsrc_id = tr.rsrc_id" + al("RSRC", "r")
                + f" WHERE x.proj_id = ? AND {rtype} IN ('RT_Labor', 'RT_Equip')" + al("TASKRSRC", "tr") + al("TASK", "x"),
                (bid,)):
            w = wbs_of.get(code)
            if w is None or not q:
                continue
            s, e = _db_dt(s), _db_dt(e)
            acc = bpl.setdefault(w, {"bl_plan": 0.0, "bl_budget": 0.0})
            acc["bl_budget"] += float(q)
            if s and e:
                acc["bl_plan"] += _value_at([(float(q), s, max(s, e))], dd)
    # band values of every WBS: its activities and the WBS below it
    lows, highs = ("bl_s", "s", "as", "ps", "os"), ("bl_f", "f", "af", "pf", "of")

    def merge(acc: dict, part: Mapping) -> dict:  # minimum or maximum of dates, sum of the rest; None ignored
        for k, v in part.items():
            if v is not None:
                a0 = acc.get(k)
                acc[k] = v if a0 is None else min(a0, v) if k in lows else max(a0, v) if k in highs else a0 + v
        return acc
    band_own: dict[int, dict] = {}
    for a in acts:
        is_open = a["status_code"] != "TK_Complete"
        merge(band_own.setdefault(a["wbs_id"], {}), {
            "n": 1, "done": 0 if is_open else 1, "bl_s": a["bl_start"], "bl_f": a["bl_finish"], "s": a["start"],
            "f": a["finish"], "as": a["act_start"], "af": a["act_finish"], "ps": a["plan_start"], "pf": a["plan_finish"],
            "os": a["start"] if is_open else None, "of": a["finish"] if is_open else None, "lt": a["lab_t"],
            "la": a["lab_a"], "lr": a["lab_r"], "oh": a["orig_h"], "rh": a["rem_h"]})
    ids = {n["wbs_id"] for n in nodes}
    roll: dict[int, dict] = {}
    proll: dict[int, dict] = {}
    band: dict[int, dict] = {}
    for n in reversed(nodes):  # descendants first
        w, par = n["wbs_id"], n["parent_wbs_id"]
        roll[w] = _merge_agg(roll.get(w, {}), own.get(w, {}))
        proll[w] = _merge_agg(proll.get(w, {}), bpl.get(w, {}))
        band[w] = merge(band.get(w, {}), band_own.get(w, {}))
        if par in ids:
            roll[par] = _merge_agg(roll.get(par, {}), roll[w])
            proll[par] = _merge_agg(proll.get(par, {}), proll[w])
            band[par] = merge(band.get(par, {}), band[w])
    rows = []
    for n in nodes:
        r, pl, b = _rollup(roll[n["wbs_id"]]), proll[n["wbs_id"]], band[n["wbs_id"]]
        n_, all_done = b.get("n", 0), b.get("n", 0) and b.get("done", 0) == b.get("n", 0)
        lab, oh = b.get("la", 0) + b.get("lr", 0), b.get("oh", 0)
        os_ = max(b["os"], dd) if b.get("os") and dd else b.get("os")
        rows.append({"wbs_id": n["wbs_id"], "depth": n["depth"], "code": code_of[n["wbs_id"]], "name": n["wbs_name"],
                     "units_pct": r["units_pct"], "budget_units": r["budget_units"],
                     "bl_planned_pct": round(100 * pl["bl_plan"] / pl["bl_budget"], 2) if pl.get("bl_budget") else None,
                     "finish_var_d": wd(pcal, b.get("bl_f"), b.get("f")), "all_done": bool(all_done),
                     "band": {"activities": n_, "bl_start": b.get("bl_s"), "bl_finish": b.get("bl_f"), "start": b.get("s"),
                              "finish": b.get("f"), "act_start": b.get("as"), "act_finish": b.get("af") if all_done else None,
                              "pct": (round(100 * b["la"] / lab, 2) if lab else round(100 * (oh - b.get("rh", 0)) / oh, 2)
                                      if oh else 100.0 if all_done else None) if n_ else None,
                              "lab_t": b.get("lt"), "lab_a": b.get("la"), "lab_r": b.get("lr"),
                              "orig_d": wd(pcal, b.get("ps"), b.get("pf")),
                              "rem_d": max(0.0, wd(pcal, os_, b["of"])) if os_ and b.get("of") else 0.0 if n_ else None,
                              "act_d": max(0.0, wd(pcal, b["as"], b.get("af") if all_done else dd))
                              if b.get("as") and (all_done or dd) else None}})
    cut = len(rows) > DASH_WBS_ROWS
    q, args = _act_from(con), [pid]
    if codes:
        q += f" AND t.task_code IN ({', '.join('?' * len(codes))})"
        args += list(codes)
    else:
        q += " AND t.task_type IN ('TT_Mile', 'TT_FinMile')"
    found: dict[str, dict] = {}
    for r in con.execute(q + " ORDER BY t.task_code, t.task_id", args):
        found.setdefault(r["task_code"], _dict(r))
    if codes and set(codes) - set(found):
        raise ValueError(f"Activity IDs not found in {prj['proj_short_name']}: {sorted(set(codes) - set(found))}")

    def mdate(m: Mapping, planned: bool = False) -> Optional[datetime]:  # the milestone's one date
        kind = "start" if m["task_type"] == "TT_Mile" else "end"
        return _db_dt(m[f"target_{kind}_date"] if planned else m[f"act_{kind}_date"] or m[f"early_{kind}_date"])
    ms = sorted(found.values(), key=lambda m: (mdate(m) or datetime.max, m["task_code"]))
    ms_note = None
    if not codes and len(ms) > DASH_MILESTONES:
        ms_note = f"{len(ms)} milestones: the first {DASH_MILESTONES} by date are shown (pass milestones= to choose)."
        ms = ms[:DASH_MILESTONES]
    bl: dict[str, dict] = {}
    if bid is not None and ms:
        bl = {r["task_code"]: _dict(r) for r in con.execute(
            "SELECT task_code, act_start_date, act_end_date, early_start_date, early_end_date, target_start_date, "
            f"target_end_date FROM TASK WHERE proj_id = ? AND task_code IN ({', '.join('?' * len(ms))})" + al("TASK")
            + " ORDER BY task_id DESC", [bid, *(m["task_code"] for m in ms)])}
    out_ms = []
    for m in ms:
        cur, b = mdate(m), bl.get(m["task_code"])
        bd = mdate({**b, "task_type": m["task_type"]}, baseline_dates == "planned") if b else None
        out_ms.append({"task_code": m["task_code"], "task_name": m["task_name"], "task_type": m["task_type"],
                       "status_code": m["status_code"], "date": cur, "baseline": bd,
                       "variance_d": wd(cals.get(m["clndr_id"]), bd, cur),
                       "total_float_d": _days(m["total_float_hr_cnt"], m["hpd"])})
    scd, must = (_db_dt(prj[k]) for k in ("scd_end_date", "plan_end_date"))
    kpi = {"vs_must_d": wd(pcal, must, scd), "vs_baseline_d": wd(pcal, bl_finish, scd),
           "remaining_d": max(0.0, wd(pcal, dd, scd)) if dd and scd else None}
    return {"project": prj, "baseline": base, "bl_finish": bl_finish, "rows": rows[:DASH_WBS_ROWS], "rows_cut": cut,
            "milestones": out_ms, "ms_note": ms_note, "calendar": pcal.name if pcal else None,
            "hpd": pcal.hpd if pcal else 8.0, "kpi": kpi, "acts": acts, "wd_notes": sorted(wd_notes)}


def _dash_book(d: dict) -> tuple[Any, dict, list]:
    """The dashboard workbook (openpyxl) of what p6_export_dashboard gathered, the data rows written
    per sheet and the 7 card values ({card, value, detail}, dates as YYYY-MM-DD). Sheets in DASH_SHEETS
    order; the Dashboard sheet (fixed objects, protected) has a sidebar of links to the others, which
    link back to it."""
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont
    from openpyxl.chart import BarChart, LineChart, Reference
    from openpyxl.chart.axis import ChartLines
    from openpyxl.chart.label import DataLabelList
    from openpyxl.chart.marker import DataPoint
    from openpyxl.chart.shapes import GraphicalProperties
    from openpyxl.drawing.line import LineProperties
    from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, TwoCellAnchor
    from openpyxl.formatting.rule import DataBarRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Protection, Side
    from openpyxl.utils import get_column_letter as letter
    from openpyxl.utils.units import pixels_to_EMU
    from openpyxl.worksheet.hyperlink import Hyperlink
    from openpyxl.worksheet.page import PageMargins

    DATE, DAYS, SIGNED, PCT = "dd-mmm-yy", "0.0", "+0.0;-0.0;0.0", '0.0"%"'
    near, rd, st, hc, sm = d["near"], d["rd"], d["state"], d["hc"], d["summary"]
    dd = _db_dt(rd["project"]["last_recalc_date"])
    lsd = _db_dt(rd["project"]["last_schedule_date"])
    bname = rd["baseline"]["proj_short_name"] if rd["baseline"] else None
    counts: dict[str, int] = {}
    fills: dict[str, Any] = {}
    wb = Workbook()
    wb.properties.creator = "P6 MCP server"
    wb.properties.title = f"{d['project']} project controls dashboard"
    sheets = {DASH_SHEETS[0]: wb.active}
    sheets[DASH_SHEETS[0]].title = DASH_SHEETS[0]
    for n in DASH_SHEETS[1:]:
        sheets[n] = wb.create_sheet(n)
    brand = d["brand"]
    head, accent = brand["primary"], brand["accent"]  # headers, sidebar and dark bars; S-curve actual line
    head_ink = _ink_on(head)
    sheets[DASH_SHEETS[0]].sheet_properties.tabColor = head
    crit, acts, ms = d["crit"], rd["acts"], rd["milestones"]

    def fclass(tf: Optional[float]) -> Optional[str]:
        return None if tf is None else "neg" if tf < 0 else "near" if tf <= near else "ok"

    def late(v: Optional[float]) -> str:  # a slip in working days against the baseline
        return "No baseline" if v is None else "Delayed" if v > near else "At risk" if v > 0 else "On track"

    def act_status(a: Mapping) -> str:  # Completed, Critical (project definition), else by the slip
        return "Completed" if a["status_code"] == "TK_Complete" else "Critical" if a["task_code"] in crit else late(a["slip_d"])

    def fill(color: str):
        if color not in fills:
            fills[color] = PatternFill("solid", fgColor=color)
        return fills[color]

    def mix(color: str, f: float) -> str:  # color moved toward white by f (0 to 1)
        return "".join(f"{round(int(color[i:i + 2], 16) + (255 - int(color[i:i + 2], 16)) * f):02X}" for i in (0, 2, 4))

    def put(ws, r: int, c: int, v, fmt: Optional[str] = None, bold: bool = False, size: float = 10,
            color: str = DASH_INK, bg: Optional[str] = None, h: Optional[str] = None, wrap: bool = False, indent: int = 0,
            valign: str = "center", font: str = "Calibri"):
        if isinstance(v, str):
            v = ILLEGAL_CHARACTERS_RE.sub("", v)[:32000]
        cell = ws.cell(row=r, column=c, value=v)
        if isinstance(v, str) and v.startswith("="):
            cell.data_type = "s"  # text from the database, never a formula
        cell.font = Font(name=font, size=size, bold=bold, color=color)
        cell.alignment = Alignment(horizontal=h, vertical=valign, wrap_text=wrap, indent=indent)
        if fmt:
            cell.number_format = fmt
        if bg:
            cell.fill = fill(bg)
        return cell

    def merged(ws, r: int, c0: int, c1: int, v, r1: Optional[int] = None, **kw):
        if c1 > c0 or (r1 or r) > r:
            ws.merge_cells(start_row=r, start_column=c0, end_row=r1 or r, end_column=c1)
        return put(ws, r, c0, v, **kw)

    def rich(*parts: tuple) -> Any:  # (text, color, symbol font?, bold?, size) blocks in one cell
        return CellRichText(*(TextBlock(InlineFont(rFont="Segoe UI Symbol" if sym else "Calibri", sz=size, b=bold or None,
                                                   color=color), text) for text, color, sym, bold, size in parts))

    def link(ws, r: int, c: int, text: str, sheet: str, color: str = "0563C1", size: float = 9) -> None:
        cell = put(ws, r, c, text, size=size, color=color)
        cell.font = Font(name="Calibri", size=size, color=color, underline="single")
        cell.hyperlink = Hyperlink(ref=cell.coordinate, location=f"'{sheet}'!A1", display=text)

    def top(ws, title: str, sub: Optional[str] = None) -> None:
        put(ws, 1, 1, title, bold=True, size=14)
        if sub:
            put(ws, 2, 1, sub, color=DASH_MUTED)
        ws.row_dimensions[1].height = 22
        link(ws, 3, 1, "Back to the Dashboard", DASH_SHEETS[0])

    def header(ws, r: int, c0: int, labels: Sequence[str], widths: Optional[Sequence[float]] = None) -> None:
        for i, lab in enumerate(labels):
            put(ws, r, c0 + i, lab, bold=True, size=9, color=head_ink, bg=head, h="center", wrap=True)
            if widths:
                ws.column_dimensions[letter(c0 + i)].width = widths[i]
        ws.row_dimensions[r].height = 30

    def page(ws, one_page: bool = False, title_rows: Optional[str] = None) -> None:
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_A3
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 1 if one_page else 0
        ws.page_margins = PageMargins(left=0.4, right=0.4, top=0.5, bottom=0.5)
        if title_rows:
            ws.print_title_rows = title_rows

    def section(ws, r: int, c0: int, c1: int, text: str) -> None:
        put(ws, r, c0, text, bold=True, size=11)
        for c in range(c0, c1 + 1):
            ws.cell(r, c).border = Border(bottom=Side(style="thin", color=DASH_RULE))

    def axes(ch, legend: str = "b") -> None:
        ch.roundedCorners = False
        ch.x_axis.delete = False
        ch.y_axis.delete = False
        ch.y_axis.majorGridlines = ChartLines(spPr=GraphicalProperties(ln=LineProperties(solidFill="E5E7EB")))
        ch.x_axis.majorGridlines = None
        if legend:
            ch.legend.position = legend
            ch.legend.overlay = False  # Excel overlays the plot when the flag is missing
        else:
            ch.legend = None

    def borderless(ch) -> Any:  # a chart inside a white panel: no frame of its own
        ch.graphical_properties = GraphicalProperties(ln=LineProperties(noFill=True))
        return ch

    def values_only():  # data labels with the value alone (Excel adds series and category names otherwise)
        return DataLabelList(showVal=True, showSerName=False, showCatName=False, showLegendKey=False,
                             showPercent=False, showBubbleSize=False, dLblPos="outEnd")

    def line(s, color: str, width: float = 2.0, dash: Optional[str] = None) -> None:
        s.graphicalProperties.line.solidFill = color
        s.graphicalProperties.line.width = int(width * 12700)  # points to EMU
        if dash:
            s.graphicalProperties.line.dashStyle = dash
        s.smooth = False
        s.marker.symbol = "none"

    def bars(s, color: str) -> None:
        s.graphicalProperties = GraphicalProperties(solidFill=color)
        s.graphicalProperties.line.solidFill = color
        s.invertIfNegative = False  # a negative bar keeps its color

    def col_px(width: float) -> int:  # pixels of a column of that width (Calibri 11: 7 px per character)
        return int((256 * width + int(128 / 7)) / 256 * 7)

    def row_px(height: Optional[float]) -> int:  # pixels of a row of that height in points (Excel rounds down)
        return int((height or 15) * 4 / 3 + 1e-9)

    def act_label(code: str, name: Optional[str]) -> str:
        return f"{code} - {name or ''}".rstrip(" -")

    def pill(ws, r: int, c: int, status: str) -> None:  # a status as colored text on its tint
        bg, fg = DASH_PILL.get(status, DASH_PILL["Completed"])
        cell = ws.cell(r, c)
        cell.fill = fill(bg)
        cell.font = Font(name="Calibri", size=9, bold=True, color=fg)

    # ---- Schedule Overview: every activity grouped by WBS, a band row per WBS (all levels), outline levels
    ws = sheets["Schedule Overview"]
    shown = acts[:DASH_ACT_CAP]
    top(ws, "Schedule Overview")  # what the columns hold: the Reports notes
    labels =["Activity ID", "Activity Name", "BL Start", "BL Finish", "Start", "Finish", "Actual Start", "Actual Finish",
              "% Complete", "Budgeted Labor Units", "Actual Labor Units", "Remaining Labor Units", "Original Duration",
              "Remaining Duration", "Actual Duration"]
    header(ws, 4, 1, labels, [16, 62, 11, 11, 11, 11, 11, 11, 10, 12, 12, 12, 10, 10, 10])
    fmts = (None, None, DATE, DATE, DATE, DATE, DATE, DATE, PCT, "#,##0.0", "#,##0.0", "#,##0.0", DAYS, DAYS, DAYS)
    by_wbs: dict[int, list] = {}
    for a in shown:
        by_wbs.setdefault(a["wbs_id"], []).append(a)
    r = 5
    for w in rd["rows"]:
        b, depth = w["band"], w["depth"]
        bg = mix(head, min(0.94, 0.55 + 0.1 * depth))  # the band color lightens with the depth, as P6 bands
        ink = _ink_on(bg)
        vals = [w["code"], w["name"], b["bl_start"], b["bl_finish"], b["start"], b["finish"], b["act_start"],
                b["act_finish"], b["pct"], b["lab_t"], b["lab_a"], b["lab_r"], b["orig_d"], b["rem_d"], b["act_d"]]
        for j, (v, f) in enumerate(zip(vals, fmts)):
            put(ws, r, 1 + j, v, fmt=f, size=9, bold=True, color=ink, bg=bg, indent=depth if j == 1 else 0,
                h="center" if j == 0 else None)
        ws.row_dimensions[r].outline_level = min(depth, 7)
        ws.row_dimensions[r].hidden = depth > DASH_SO_LEVEL
        r += 1
        for a in by_wbs.get(w["wbs_id"], []):
            vals = [a["task_code"], a["task_name"], a["bl_start"], a["bl_finish"], a["start"], a["finish"], a["act_start"],
                    a["act_finish"], a["pct"], a["lab_t"], a["lab_a"], a["lab_r"], a["orig_d"], a["rem_d"], a["act_d"]]
            for j, (v, f) in enumerate(zip(vals, fmts)):
                put(ws, r, 1 + j, v, fmt=f, size=9, indent=depth + 1 if j == 1 else 0, h="center" if j == 0 else None)
            ws.row_dimensions[r].outline_level = min(depth + 1, 7)
            ws.row_dimensions[r].hidden = True  # folded to the WBS bands of DASH_SO_LEVEL
            r += 1
    for rr in range(5, r - 1):  # a band whose rows below are folded shows +, as Excel marks it (nested ones too)
        nxt = ws.row_dimensions[rr + 1]
        if nxt.hidden and nxt.outline_level > ws.row_dimensions[rr].outline_level:
            ws.row_dimensions[rr].collapsed = True
    ws.sheet_properties.outlinePr.summaryBelow = False  # a band is above its activities
    ws.auto_filter.ref = f"A4:{letter(len(labels))}{r - 1}"
    ws.freeze_panes = "C5"
    page(ws, title_rows="4:4")
    counts["Schedule Overview"] = len(shown)

    # ---- Critical Path: P6 float paths (path 1 = most critical), a bold line between paths
    ws = sheets["Critical Path"]
    fp = sorted((a for a in acts if a["float_path"] is not None and a["task_type"] not in ("TT_LOE", "TT_WBS")),
                key=lambda a: (a["float_path"], a["float_path_order"] or 0, a["task_code"]))
    if fp:
        groups = [(f"Path {k}", list(g)) for k, g in itertools.groupby(fp, key=lambda a: a["float_path"])]
        cp_how = (f"the {len(groups)} float paths P6 calculated at the last F9 (path 1 = most critical), each in P6's "
                  "order (float_path_order)")
    else:
        near_rows = sorted((a for a in acts if a["status_code"] != "TK_Complete" and a["task_type"] not in ("TT_LOE", "TT_WBS")
                            and a["tf_d"] is not None and a["tf_d"] <= near),
                           key=lambda a: (a["tf_d"], a["start"] or datetime.max, a["task_code"]))
        groups = [(f"TF {k:g} d", list(g)) for k, g in itertools.groupby(near_rows, key=lambda a: a["tf_d"])]
        cp_how = (f"no float paths are stored (enable multiple float paths in the P6 schedule options), so it lists the "
                  f"open activities with total float up to {near:g} d, grouped by total float value")
    top(ws, "Critical Path")  # how the paths were chosen: the Reports notes
    labels = ["Path", "Order", "Activity ID", "Activity Name", "WBS", "P6 status", "Start", "Finish", "BL Finish",
              "Slip (wd)", "Remaining (d)", "Total float (d)", "Status"]
    header(ws, 4, 1, labels, [10, 7, 18, 56, 30, 12, 11, 11, 11, 10, 11, 11, 12])
    thick = Side(style="thick", color=DASH_INK)
    r = 5
    for name, rows_ in groups:
        for i, a in enumerate(rows_):
            status = act_status(a)
            vals = [name if i == 0 else None, a["float_path_order"] if fp else i + 1, a["task_code"], a["task_name"],
                    a["wbs_name"], STATUS_NAMES.get(a["status_code"], a["status_code"]), a["start"], a["finish"],
                    a["bl_finish"], a["slip_d"], a["rem_d"], a["tf_d"], status]
            for j, (v, f) in enumerate(zip(vals, (None, "0", None, None, None, None, DATE, DATE, DATE, SIGNED, DAYS, DAYS,
                                                  None))):
                put(ws, r, 1 + j, v, fmt=f, size=9, bold=j == 0, h="center" if j in (1, 12) else None)
            pill(ws, r, 13, status)
            cls = fclass(a["tf_d"])
            if cls:
                ws.cell(r, 12).fill = fill(DASH_TINT[cls])
            if i == len(rows_) - 1:  # the bold line under the last activity of the path
                for c in range(1, len(labels) + 1):
                    ws.cell(r, c).border = Border(bottom=thick)
            r += 1
    if not groups:
        put(ws, 5, 1, "No activity.", color=DASH_MUTED)
    ws.freeze_panes = "E5"
    page(ws, title_rows="4:4")
    counts["Critical Path"] = r - 5

    # ---- Reports: schedule quality (DCMA 14), about
    ws, chk = sheets["Reports"], hc["checks"]
    top(ws, "Reports","Schedule quality (DCMA 14) and the sources, settings and definitions of this dashboard.")
    section(ws, 4, 1, 6, "Schedule quality (DCMA 14)")
    header(ws, 5, 1, ["Check", "Result", "Count", "Of", "Value", "Threshold"], [44, 16, 12, 12, 12, 14])
    qrows = [k for k in DCMA_CHECKS if k in chk] + (["key_dates"] if "key_dates" in chk else [])
    for i, k in enumerate(qrows):
        c, rule = chk[k], DCMA_CHECKS.get(k, (None, None))[1]
        ratio = rule == "ratio"
        thr = c.get("threshold")
        thr_txt = "" if thr is None or rule is None else ("<= " if rule == "max" else ">= ") + (f"{thr:g}" if ratio else f"{thr:g} %")
        res = "Information" if k == "key_dates" else {True: "Pass", False: "Fail", None: "Not evaluated"}[c.get("pass")]
        vals = [DCMA_LABELS.get(k, k), res, c.get("count"), c.get("denominator"), c.get("value") if ratio else c.get("pct"),
                thr_txt]
        for j, (v, f) in enumerate(zip(vals, (None, None, "#,##0", "#,##0", "0.00" if ratio else '0.00"%"', None))):
            put(ws, 6 + i, 1 + j, v, fmt=f, size=9, h="center" if j in (1, 5) else None)
        ws.cell(6 + i, 2).fill = fill(DASH_TINT["pass" if res == "Pass" else "fail" if res == "Fail" else "na"])
    counts["Reports"] = len(qrows)
    r = 6 + len(qrows) + 1
    section(ws, r, 1, 6, "About this dashboard")
    cv = d["curve"]
    cvb = cv and cv["baseline"]
    facts = [("Project", d["project"]), ("Project name", d["name"]), ("Company", brand["company"]),
             ("Prepared by", d["prepared_by"]), ("Revision", d["revision"]), ("Database file", d["db"]),
             ("Project calendar", rd["calendar"]), ("Data date", dd), ("Last schedule run (F9)", lsd),
             ("Edited since the last F9", "Yes: open P6 and press F9, then export again" if st["stale"] else "No"),
             ("Baseline", f"{bname} ({'baseline=' if d['baseline_arg'] else 'project baseline'}), {d['baseline_dates']} dates"
              if bname else "None: set a project baseline in P6 or pass baseline="),
             ("Critical", sm["critical"]["definition"]), ("Delayed and at risk", f"more than {near:g} / up to {near:g} "
                                                                                  "working days after the baseline finish"),
             ("S-curve", f"{cv['measure']['kind']} per {cv['period']}" + (f", baseline {cvb['project']}" if cvb else "")
              if cv else "not drawn"),
             ("DCMA thresholds", hc["threshold_source"]), ("Generated", d["now"]), ("Server version", SERVER_VERSION)]
    r += 1
    for i, (k, v) in enumerate(facts):
        put(ws, r + i, 1, k, bold=True, size=9)
        merged(ws, r + i, 2, 6, v, fmt="dd-mmm-yyyy hh:mm" if isinstance(v, datetime) else None, size=9, h="left")
    r += len(facts) + 1
    put(ws, r, 1, "Definitions and notes", bold=True, size=11)
    notes = [
        "Dates, float and the critical path are those of the last F9 in P6; changes made after it show only once P6 "
        "reschedules.",
        f"Slip and variance: working days between the baseline date and the forecast or actual date, on the project "
        f"calendar ({rd['calendar'] or 'none'}) for the project figures and on each activity's or milestone's own calendar "
        "otherwise, as P6 variance columns.",
        f"Status: Critical = the project's critical definition; Delayed = open and more than {near:g} working days later "
        f"than the baseline finish; At risk = later by up to {near:g}; On track = not later.",
        "Remaining duration (card): working days of the project calendar from the data date to the forecast finish.",
        f"Schedule Overview: every activity of the last F9 ({len(acts):,}" + (f", the first {DASH_ACT_CAP:,} listed"
        if len(acts) > DASH_ACT_CAP else "") + f") under a band per WBS in P6 order; it opens folded to the WBS bands of "
        f"level {DASH_SO_LEVEL} (outline buttons on the left show the activities). Baseline dates of the same Activity "
        "ID; % complete by each activity's percent complete type; labor units as stored; original and remaining "
        "durations in days of the activity calendar, actual duration in working days to the actual finish or data date.",
        "Schedule Overview WBS bands: earliest start and latest finish of their activities (actual finish once all are "
        "completed), summed labor units, % complete = actual / (actual + remaining) labor units, else the duration %; "
        "original duration from the earliest planned start to the latest planned finish, remaining from the data date "
        "(or the earliest open start) to the latest open finish, actual from the earliest actual start to the data date "
        "(or the last actual finish), in working days of the project calendar.",
        f"Critical Path sheet: {cp_how}. A bold line separates the paths. Slip against the baseline finish in working "
        "days of the activity calendar; total float in days.",
        "DCMA checks: open activities = not completed, LOE and WBS summary left out; the relationship checks count the "
        "links whose successor is open.",
        "WBS Progress and Level 2 Progress (dashboard): units % complete = actual / (actual + remaining) labor and "
        "nonlabor units; baseline planned % = baseline assignments of the activities with the same Activity ID, spread "
        "linearly between their planned dates, at the data date.",
        "Finish Variance by WBS (dashboard): working days of the project calendar from the latest baseline finish to "
        "the latest finish of the level 1 WBS's activities; colored by the status rule above (gray: all completed).",
        "S-curve: quantities are spread linearly over calendar time between the assignment dates (P6 spreads over "
        "working time and resource curves) and the actual curve is rebuilt from the actual dates, so only the values at "
        "the data date are exact. Earned value = budget x activity % complete (its percent complete type).",
        *d["warnings"]]
    for i, n in enumerate(notes):
        put(ws, r + 1 + i, 1, "Warning" if i >= len(notes) - len(d["warnings"]) else None, size=9, bold=True,
            color=DASH_FLOAT["neg"], valign="top")
        merged(ws, r + 1 + i, 2, 6, n, size=9, wrap=True, valign="top")
        ws.row_dimensions[r + 1 + i].height = 13 * max(1, math.ceil(len(n) / 95))
    page(ws)

    # ---- Dashboard: sidebar, header, 7 cards, then 6 panels, on one A3 page; objects fixed, sheet protected
    ws = sheets["Dashboard"]
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 85
    G0, NG, LAST = 3, 27, 60  # first grid column (C), grid columns, last row
    col = lambda k: G0 + k  # noqa: E731
    hc0 = col(NG) + 2  # hidden helper data of the charts, right of the print area
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 26, 1.5
    for k in range(NG):  # 7 groups of 3 wide columns (a card) and a narrow gutter, the last without: the A3 width
        ws.column_dimensions[letter(col(k))].width = 6.14 if k % 4 == 3 else 10.29
    side_ink = _ink_on(head)
    side_soft = mix(head, 0.65) if side_ink == "FFFFFF" else DASH_MUTED
    for rr in range(1, LAST + 1):
        ws.cell(rr, 1).fill = fill(head)
        for c in range(2, col(NG)):
            ws.cell(rr, c).fill = fill(DASH_PAGE)
    placed: list[tuple] = []  # (chart, row, column, (width, height) in cm): anchored once every size is final

    def area(r0: int, r1: int, c0_: int, c1: int, color: str = "FFFFFF") -> None:
        for rr in range(r0, r1 + 1):
            for c in range(c0_, c1 + 1):
                ws.cell(rr, c).fill = fill(color)
    frames: list[tuple] = []  # (r0, r1, c0, c1) drawn last, after every merge

    def box(r0: int, r1: int, k0: int, k1: int) -> None:
        area(r0, r1, col(k0), col(k1))
        frames.append((r0, r1, col(k0), col(k1)))

    def panel_title(r: int, k: int, text: str) -> None:
        put(ws, r, col(k), text, bold=True, size=12, indent=1)
        ws.row_dimensions[r].height = 22

    # sidebar: company, links to every sheet, the project at the bottom
    merged(ws, 1, 1, 1, brand["company"] or "Schedule dashboard", r1=2, bold=True, size=13, color=side_ink, bg=head,
           indent=1, wrap=True)
    put(ws, 3, 1, "Primavera P6 schedule", size=9, color=side_soft, bg=head, indent=1)
    for i, name in enumerate(DASH_SHEETS):  # one link every other row from row 12 (rows of the panels, 15 points)
        cell = ws.cell(12 + 2 * i, 1)
        cell.value = rich((DASH_NAV[name] + "  ", side_ink, True, False, 11), (name, side_ink, False, i == 0, 11))
        cell.fill = fill(mix(head, 0.18) if i == 0 else head)
        cell.alignment = Alignment(vertical="center", indent=1)
        if i:
            cell.hyperlink = Hyperlink(ref=cell.coordinate, location=f"'{name}'!A1", display=name)
            cell.protection = Protection(locked=False)  # the only cells that can be selected: the links work
    put(ws, LAST - 7, 1, d["project"], bold=True, size=11, color=side_ink, bg=head, indent=1)
    merged(ws, LAST - 6, 1, 1, d["name"] or "", r1=LAST - 3, size=9, color=side_soft, bg=head, indent=1, wrap=True,
           valign="top")
    put(ws, LAST - 2, 1, f"Data date {dd:%d %b %Y}" if dd else "No data date", size=9, color=side_soft, bg=head, indent=1)

    # header: title, subtitle, data date (its dot red when this server changed the schedule after the last F9)
    put(ws, 1, col(0), "Project Controls Dashboard", bold=True, size=20)
    put(ws, 2, col(0), f"Primavera P6 schedule analysis  |  Excel export  |  {d['project']}" + (f" - {d['name']}" if d["name"]
        else ""), size=10, color=DASH_MUTED)
    put(ws, 3, col(0), "  |  ".join(x for x in (d["prepared_by"] and f"Prepared by {d['prepared_by']}",
                                                 d["revision"] and f"Revision {d['revision']}",
                                                 f"Generated {d['now']:%d %b %Y %H:%M}") if x), size=9, color=DASH_MUTED)
    for rr, hh in ((1, 32), (2, 18), (3, 16)):
        ws.row_dimensions[rr].height = hh
    state = ws.cell(1, col(NG - 1))  # right-aligned on the right edge, running on to the left
    state.value = rich(("● ", DASH_GOOD if not st["stale"] else DASH_FLOAT["neg"], True, False, 12),
                       ("Data date " + (f"{dd:%d %b %Y %H:%M}" if dd else "none"), DASH_INK, False, True, 12))
    state.alignment = Alignment(horizontal="right", vertical="center")
    if st["stale"]:
        put(ws, 4, col(0), "Warning: this server changed the schedule after the last F9. Open P6, press F9, then export "
            "again.", bold=True, size=9, color=DASH_FLOAT["neg"])

    # cards
    pc, bl, cr = sm["percent_complete"], sm["baseline"], sm["critical"]
    kpi, ms_open = rd["kpi"], [m for m in ms if m["status_code"] != "TK_Complete" and m["variance_d"] is not None]
    real = [a for a in acts if a["task_type"] not in ("TT_LOE", "TT_WBS")]
    open_real = [a for a in real if a["status_code"] != "TK_Complete"]
    n_delayed = sum(1 for a in open_real if late(a["slip_d"]) == "Delayed")
    n_risk = sum(1 for a in open_real if late(a["slip_d"]) == "At risk")
    plan = bl.get("planned_units_pct_at_data_date")
    td = cv["to_date"] if cv else None
    sv = 100 * td["variance"] / td["baseline_planned"] if td and td["baseline_planned"] else None
    thr_h = cr.get("threshold_h")
    crit_sub = ("On the longest path (project setting)" if thr_h is None
                else f"Total float <= {thr_h / (rd['hpd'] or 8):g} d (project setting)")
    bfin, vb, rem = rd["bl_finish"], kpi["vs_baseline_d"], kpi["remaining_d"]
    n_late = sum(1 for m in ms_open if m["variance_d"] > 0)
    cards = [
        ("◔", "Overall Progress", pc["primary_pct"], '0.00"%"',
         f"Planned: {plan:.2f}%  |  Variance: {pc['primary_pct'] - plan:+.2f}%" if plan is not None and pc["primary_pct"]
         is not None else "No baseline plan at the data date", "0072B2", pc["primary_pct"]),
        ("◎", "Critical Activities", cr["critical"], "#,##0", crit_sub, DASH_FLOAT["neg"], None),
        ("◷", "Delayed Activities", n_delayed if bname else None, "#,##0",
         f"Behind baseline by over {near:g} wd; {n_risk:,} at risk" if bname else "No baseline", DASH_FLOAT["near"], None),
        ("Δ", "Schedule Variance", sv, '+0.0"%";-0.0"%";0.0"%"',
         f"Earned vs baseline planned (SPI {td['spi']:.2f})" if sv is not None else "Needs a baseline with resources",
         "7B61C9", None),
        ("⚑", "Forecast Completion", _db_dt(rd["project"]["scd_end_date"]), DATE,
         f"Baseline: {bfin:%d %b %Y} ({vb:+.1f} wd)" if bfin and vb is not None else "No baseline", "009E73", None),
        ("▸", "Remaining Duration", rem, '0" d"' if rem is None or float(rem).is_integer() else '0.0" d"',
         "Working days to the forecast finish", head, None),
        ("◆", "Milestones Late", n_late if ms_open else None, "0",
         f"of {len(ms_open)} open milestones with a baseline" if ms_open else "No open milestone with a baseline",
         DASH_FLOAT["neg"] if n_late else DASH_GOOD, None)]
    for k, (glyph, title, value, fmt, sub, color, bar) in enumerate(cards):
        c0_ = col(4 * k)
        merged(ws, 5, c0_, c0_ + 2, None, indent=1)  # the card's symbol in its color, then the title
        ws.cell(5, c0_).value = rich((glyph + "  ", color, glyph != "Δ", True, 12), (title, DASH_INK, False, True, 9))
        merged(ws, 6, c0_, c0_ + 2, "n/a" if value is None else value, r1=7, fmt=fmt, bold=True, size=18, h="center")
        merged(ws, 8, c0_, c0_ + 2, sub, size=8, color=DASH_MUTED, wrap=True, indent=1, valign="top")
        box(5, 9, 4 * k, 4 * k + 2)
        if bar is None:
            ws.cell(9, c0_).fill = fill(color)  # the short accent line of the card
        else:
            merged(ws, 9, c0_, c0_ + 2, bar, fmt=";;;", bg="FFFFFF")
            ws.conditional_formatting.add(f"{letter(c0_)}9:{letter(c0_ + 2)}9", DataBarRule(
                start_type="num", start_value=0, end_type="num", end_value=100, color=color, showValue=False))
    for rr, hh in ((5, 20), (6, 18), (7, 18), (8, 30), (9, 8)):
        ws.row_dimensions[rr].height = hh
    card_values = [{"card": title, "value": value.strftime("%Y-%m-%d") if isinstance(value, datetime)
                    else round(value, 2) if isinstance(value, float) else value, "detail": sub}
                   for _, title, value, _, sub, _, _ in cards]

    def rows_cm(r0: int, r1: int) -> float:  # height of rows r0 to r1, less a margin, in cm
        return sum(row_px(ws.row_dimensions[rr].height) for rr in range(r0, r1 + 1)) / 37.8 - 0.4

    def cols_cm(k0: int, k1: int) -> float:  # width of grid columns k0 to k1, less a margin, in cm
        return sum(col_px(ws.column_dimensions[letter(col(k))].width or 8.43) for k in range(k0, k1 + 1)) / 37.8 - 0.4

    def wlabel(w: Mapping) -> str:  # a WBS on a chart axis: its code (relative to the project) and name
        return (f"{w['code']}. {w['name']}" if w["depth"] == 1 else f"{w['code']} {w['name']}")[:40]

    def pick(level: int) -> tuple[list, int]:  # WBS of a level with units in P6 order; past the cap, the most budget
        all_ = [w for w in rd["rows"] if w["depth"] == level and w["units_pct"] is not None]
        keep = {w["wbs_id"] for w in sorted(all_, key=lambda w: (-(w["budget_units"] or 0), w["code"]))[:DASH_WBS_BARS]}
        return [w for w in all_ if w["wbs_id"] in keep], len(all_)

    def bar_chart(cats: int, c0_: int, series: Sequence[tuple[int, str]]) -> Any:
        """Horizontal bars of the helper rows 2 to cats + 1: categories in column c0_, a series (title in row 1)
        per (column, color); the data labels show the cells' number format."""
        ch = BarChart()
        ch.type, ch.grouping, ch.gapWidth, ch.overlap = "bar", "clustered", 60, 0
        ch.visible_cells_only = False
        for c, color in series:
            ch.add_data(Reference(ws, min_col=c, min_row=1, max_row=1 + cats), titles_from_data=True)
            bars(ch.series[-1], color)
        ch.set_categories(Reference(ws, min_col=c0_, min_row=2, max_row=1 + cats))  # after the series: it sets theirs
        ch.x_axis.axPos, ch.y_axis.axPos = "l", "b"
        return ch

    def progress_chart(rows_: list, c0_: int, both: bool) -> Any:
        """Baseline planned % (with a baseline) and actual units % of WBS rows, the first one on top; data in
        the hidden columns c0_ to c0_ + 2; data labels on both series when there is room, else on the actual."""
        for j, lab in enumerate(("WBS", "Planned (baseline)", "Actual")):
            put(ws, 1, c0_ + j, lab, size=8)
        for i, w in enumerate(reversed(rows_)):  # Excel draws the first category at the bottom
            put(ws, 2 + i, c0_, wlabel(w), size=8)
            put(ws, 2 + i, c0_ + 1, w["bl_planned_pct"], fmt='0"%"', size=8)
            put(ws, 2 + i, c0_ + 2, w["units_pct"], fmt='0"%"', size=8)
        ch = bar_chart(len(rows_), c0_, ([(c0_ + 1, "A9B8CC")] if bname else []) + [(c0_ + 2, head)])
        for s_ in ch.series[-1:] if not both else ch.series:
            s_.dLbls = values_only()
        axes(ch, "t")
        ch.y_axis.number_format = '0"%"'
        ch.y_axis.scaling.min, ch.y_axis.scaling.max = 0, 110
        ch.y_axis.majorUnit = 20
        return borderless(ch)

    def per_bar(r0: int, r1: int, n: int) -> float:  # pixels of plot height per category, about
        return rows_cm(r0, r1) * 37.8 / max(1, n)

    # panel row 1: S-curve (grid 0-10), finish variance by WBS (12-18), WBS progress (20-26), rows 11 to 27
    box(11, 27, 0, 10)
    panel_title(11, 0, "Planned vs Actual S-Curve")
    per = cv["periods"] if cv else []
    if per:  # the S-curve's own data in hidden columns (the dashboard needs no other sheet)
        ref = cv["baseline"] is not None
        sc0 = hc0 + 9
        for j, lab in enumerate(["Period", "Planned (baseline)" if ref else "Planned (current)", "Actual", "Forecast"]):
            put(ws, 1, sc0 + j, lab, size=8)
        for i, p in enumerate(per):
            a = datetime.strptime(p["start"], "%Y-%m-%d")
            for j, v in enumerate([f"{a:%b-%y}" if cv["period"] == "month" else f"{a:%d-%b-%y}",
                                   p.get("baseline_pct" if ref else "planned_pct"), p.get("actual_pct"),
                                   p.get("forecast_pct")]):
                put(ws, 2 + i, sc0 + j, v, size=8)
        ch = LineChart()
        ch.visible_cells_only = False
        for j, (color, width, dash) in enumerate((("7F8A99", 2.0, "dash"), (accent, 2.75, None), ("E69F00", 2.0, "sysDash"))):
            ch.add_data(Reference(ws, min_col=sc0 + 1 + j, min_row=1, max_row=1 + len(per)), titles_from_data=True)
            line(ch.series[-1], color, width, dash)
        ch.set_categories(Reference(ws, min_col=sc0, min_row=2, max_row=1 + len(per)))
        axes(ch, "t")
        ch.y_axis.number_format = '0"%"'
        ch.y_axis.scaling.min = 0
        if max([v for p in per for v in (p.get("baseline_pct"), p.get("planned_pct"), p.get("actual_pct"),
                                         p.get("forecast_pct")) if v is not None] or [0]) <= 100:
            ch.y_axis.scaling.max = 100
        placed.append((borderless(ch), 12, col(0), (cols_cm(0, 10), rows_cm(12, 27))))
    else:
        merged(ws, 13, col(0), col(10), f"Not drawn: {d['curve_error']}", color=DASH_MUTED, wrap=True)
    box(11, 27, 12, 18)
    panel_title(11, 12, "Finish Variance by WBS")
    fv = [w for w in rd["rows"] if w["depth"] == 1 and w["finish_var_d"] is not None][:DASH_WBS_BARS]
    if fv:  # working days from the latest baseline finish to the latest finish of each level 1 WBS
        put(ws, 1, hc0 + 3, "WBS", size=8)
        put(ws, 1, hc0 + 4, "Finish variance (wd)", size=8)
        tone = [DASH_STATUS["Completed" if w["all_done"] else late(w["finish_var_d"])] for w in reversed(fv)]
        for i, w in enumerate(reversed(fv)):
            put(ws, 2 + i, hc0 + 3, wlabel(w), size=8)
            put(ws, 2 + i, hc0 + 4, w["finish_var_d"], fmt=SIGNED, size=8)
        ch = bar_chart(len(fv), hc0 + 3, [(hc0 + 4, DASH_STATUS["On track"])])
        sv_ = ch.series[0]
        for i, color in enumerate(tone):  # each bar in its status color
            sv_.dPt.append(DataPoint(idx=i, invertIfNegative=False,
                                     spPr=GraphicalProperties(solidFill=color, ln=LineProperties(solidFill=color))))
        sv_.dLbls = values_only()
        axes(ch, None)
        ch.x_axis.tickLblPos = "low"  # the WBS names stay on the left beside a negative bar
        ch.y_axis.number_format = "0"
        placed.append((borderless(ch), 12, col(12), (cols_cm(12, 18), rows_cm(12, 26))))
        keyc = ws.cell(27, col(12))  # the color key, as the bars carry no legend
        keyc.value = rich(*(part for lab in ("Delayed", "At risk", "On track", "Completed") for part in (
            ("● ", DASH_STATUS[lab], True, False, 9),
            (lab + (f" (> {near:g} wd)" if lab == "Delayed" else "") + "    ", DASH_MUTED, False, False, 8))))
        keyc.alignment = Alignment(vertical="center", indent=1)
    else:
        merged(ws, 13, col(12), col(18), "Needs a baseline: no level 1 WBS has a baseline finish." if bname
               else "No baseline: the finish variance needs one.", size=9, color=DASH_MUTED, indent=1, wrap=True)
    box(11, 27, 20, 26)
    l1, n_l1 = pick(1)
    panel_title(11, 20, "WBS Progress" + (f" ({len(l1)} of {n_l1}, by budget units)" if n_l1 > len(l1) else ""))
    if l1:
        placed.append((progress_chart(l1, hc0, per_bar(12, 27, len(l1)) >= 40), 12, col(20),
                       (cols_cm(20, 26), rows_cm(12, 27))))
    else:
        merged(ws, 13, col(20), col(26), "No level 1 WBS with resource units.", size=9, color=DASH_MUTED, indent=1)

    # panel row 2: milestones, level 2 progress (rows 29 to 42); milestone rows hold two lines of a long name
    for rr in range(31, 31 + DASH_PANEL_MILESTONES):
        ws.row_dimensions[rr].height = 27.75
    box(29, 42, 0, 10)
    panel_title(29, 0, "Milestones")
    for a, b, lab in ((0, 4, "Milestone"), (5, 6, "Baseline"), (7, 8, "Forecast"), (9, 10, "Status")):
        merged(ws, 30, col(a), col(b), lab, bold=True, size=9, color=DASH_MUTED, bg="F7F8FA", indent=1 if a == 0 else 0,
               h=None if a == 0 else "center")
    shown_ms = ms if len(ms) <= DASH_PANEL_MILESTONES else sorted(
        ([m for m in ms if m["status_code"] != "TK_Complete"] + [m for m in ms if m["status_code"] == "TK_Complete"])
        [:DASH_PANEL_MILESTONES], key=lambda m: (m["date"] or datetime.max, m["task_code"]))
    for i, m in enumerate(shown_ms):
        rr = 31 + i
        status = "Completed" if m["status_code"] == "TK_Complete" else late(m["variance_d"])
        merged(ws, rr, col(0), col(4), act_label(m["task_code"], m["task_name"]), size=9, indent=1, wrap=True, h="left")
        merged(ws, rr, col(5), col(6), m["baseline"], fmt=DATE, size=9, h="center")
        merged(ws, rr, col(7), col(8), m["date"], fmt=DATE, size=9, h="center")
        merged(ws, rr, col(9), col(10), None)
        ws.cell(rr, col(9)).value = rich(("● ", DASH_PILL.get(status, DASH_PILL["Completed"])[1], True, False, 9),
                                         (status, DASH_INK, False, False, 9))
        for c in range(col(0), col(10) + 1):
            ws.cell(rr, c).border = Border(bottom=Side(style="hair", color=DASH_RULE))
    if not ms:
        merged(ws, 31, col(0), col(10), "No milestones in this project.", size=9, color=DASH_MUTED, indent=1)
    elif len(ms) > len(shown_ms):
        merged(ws, 31 + len(shown_ms), col(0), col(10), f"{len(ms) - len(shown_ms)} more milestones not shown.", size=8,
               color=DASH_MUTED, indent=1)
    box(29, 42, 12, 26)
    l2, n_l2 = pick(2)
    panel_title(29, 12, "Level 2 Progress" + (f" ({len(l2)} of {n_l2}, by budget units)" if n_l2 > len(l2) else ""))
    if l2:
        placed.append((progress_chart(l2, hc0 + 5, per_bar(30, 42, len(l2)) >= 40), 30, col(12),
                       (cols_cm(12, 26), rows_cm(30, 42))))
    else:
        merged(ws, 31, col(12), col(26), "No level 2 WBS with resource units.", size=9, color=DASH_MUTED, indent=1)
    ws.column_dimensions.group(letter(hc0), letter(hc0 + 12), hidden=True)

    # panel row 3: critical and delayed activities (rows 44 to 58)
    box(44, 58, 0, 26)
    flagged = [a for a in open_real if a["task_code"] in crit or late(a["slip_d"]) == "Delayed"]
    flagged.sort(key=lambda a: (a["task_code"] not in crit, (a["tf_d"] or 0) if a["task_code"] in crit
                                else -(a["slip_d"] or 0), a["start"] or datetime.max, a["task_code"]))
    panel_title(44, 0, "Critical & Delayed Activities")
    put(ws, 44, col(26), "Full lists on the Schedule Overview and Critical Path sheets", size=8, color=DASH_MUTED, h="right")
    cols = ((0, 1, "ID"), (2, 3, "WBS"), (4, 11, "Activity Name"), (12, 13, "Start (BL)"), (14, 15, "Finish (BL)"),
            (16, 17, "Start"), (18, 19, "Finish"), (20, 21, "Total Float"), (22, 23, "Slip (wd)"), (24, 26, "Status"))
    for j, (a, b, lab) in enumerate(cols):
        merged(ws, 45, col(a), col(b), lab, bold=True, size=9, color=DASH_MUTED, bg="F7F8FA",
               indent=1 if j == 0 else 0, h=None if j < 3 else "center")
    for i, a in enumerate(flagged[:DASH_TABLE_ROWS]):
        rr = 46 + i
        status = act_status(a)
        vals = (a["task_code"], a["wbs_code"], a["task_name"], a["bl_start"], a["bl_finish"], a["start"], a["finish"],
                a["tf_d"], a["slip_d"], status)
        for j, ((c_a, c_b, _), v) in enumerate(zip(cols, vals)):  # j: 7 total float, 8 slip, 9 status
            fmt = (DATE if isinstance(v, datetime) else '0.0" d"' if j == 7 else SIGNED if j == 8 else None)
            merged(ws, rr, col(c_a), col(c_b), v, fmt=fmt, size=9, indent=1 if j < 3 else 0,
                   h=None if j < 3 else "center", bold=j == 9,
                   color=DASH_FLOAT["neg"] if j == 7 and (v or 0) < 0 else DASH_INK)
        pill(ws, rr, col(cols[-1][0]), status)
        for c in range(col(0), col(26) + 1):
            ws.cell(rr, c).border = Border(bottom=Side(style="hair", color=DASH_RULE))
    put(ws, 58, col(0), f"Showing {min(len(flagged), DASH_TABLE_ROWS)} of {len(flagged):,} critical or delayed activities.",
        size=8, color=DASH_MUTED, indent=1)
    put(ws, LAST, col(0), f"Source: {d['db']}, project {d['project']}. Values of the last F9"
        + (f" ({lsd:%d-%b-%Y %H:%M})" if lsd else "") + f". P6 MCP server {SERVER_VERSION}.", size=8, color=DASH_MUTED)
    rule = Side(style="thin", color=DASH_RULE)
    for r0, r1, c0_, c1 in frames:  # panel and card frames, after the merges (a merge rewrites its edge borders)
        for rr in range(r0, r1 + 1):
            for c in range(c0_, c1 + 1):
                if rr in (r0, r1) or c in (c0_, c1):
                    old = ws.cell(rr, c).border
                    ws.cell(rr, c).border = Border(left=rule if c == c0_ else old.left, right=rule if c == c1 else old.right,
                                                   top=rule if rr == r0 else old.top, bottom=rule if rr == r1 else old.bottom)
    # fixed charts: two-cell anchors that never move or size with cells (Excel's "Don't move or size with cells"),
    # from the final column widths and row heights. An absolute anchor would keep its points while printing widens
    # the columns, so the charts would print off their panels.
    def marker(r: int, c: int, dx: int, dy: int) -> AnchorMarker:  # the point dx, dy pixels right of and below (r, c)
        while dx >= (w := col_px(ws.column_dimensions[letter(c)].width or 8.43)):
            dx, c = dx - w, c + 1
        while dy >= (h := row_px(ws.row_dimensions[r].height)):
            dy, r = dy - h, r + 1
        return AnchorMarker(col=c - 1, colOff=pixels_to_EMU(dx), row=r - 1, rowOff=pixels_to_EMU(dy))

    for obj, r, c, size in placed:  # 4 px inside the cell corner, so a chart never covers its panel's frame
        w, h = (round(v * 96 / 2.54) for v in size)
        obj.anchor = TwoCellAnchor(editAs="absolute", _from=marker(r, c, 4, 4), to=marker(r, c, 4 + w, 4 + h))
        ws.add_chart(obj)
    ws.protection.sheet = True  # a page: locked cells cannot be selected or changed, objects cannot be moved
    ws.protection.selectLockedCells = True
    ws.protection.objects = True
    page(ws, one_page=True)
    ws.print_area = f"A1:{letter(col(NG - 1))}{LAST}"
    counts["Dashboard"] = 1
    wb.active = 0
    return wb, counts, card_values


@_tool(read_only=False, destructive=True)
def p6_export_dashboard(project: ProjectId, path: str, baseline: Optional[ProjectId] = None,
                        baseline_dates: BaselineDates = "planned", milestones: Optional[ActivityIds] = None,
                        near_critical_days: NonNegDays = 10, period: Period = "month",
                        prepared_by: Optional[str] = None, revision: Optional[str] = None,
                        company: Optional[str] = None, colors: Optional[str] = None,
                        overwrite: bool = False) -> dict:
    """Write an Excel project controls dashboard to path (.xlsx in an existing folder; an existing file
    is refused unless overwrite=True; never writes the database). Sheets: Dashboard (one A3 page, protected, objects fixed, a
    sidebar of links: cards for overall progress, critical and delayed activities, schedule variance,
    forecast completion, remaining duration and late milestones; S-curve, finish variance by level 1
    WBS, WBS progress (level 1) and level 2 progress against the baseline, milestones, critical and
    delayed activities), Schedule Overview (every activity grouped by WBS with WBS bands, opened folded
    to WBS level 2: baseline, current and actual dates, % complete, labor units, original, remaining
    and actual durations), Critical Path (the P6 float paths of the last
    F9, a bold line between paths; without stored paths, activities with total float up to
    near_critical_days by total float value) and Reports (DCMA 14, sources and definitions). Status:
    Critical (project definition), Delayed (more than near_critical_days working days after the
    baseline finish), At risk (later by up to that), On track. Values of the last F9: reschedule in P6
    first. baseline= Project ID (default the project baseline); baseline_dates 'planned' (default: the
    baseline's planned dates) or 'current' (its actual, else early dates); baseline dates come from
    the baseline activities with the same Activity ID. milestones= Activity IDs (default every
    milestone, at most 30). Slip and variance in working days: project calendar for the project
    figures, each activity's own calendar otherwise. prepared_by and revision are printed under the
    title; company (sidebar) and colors ('primary,accent' in hex: sidebar, headers and dark bars, then
    the S-curve actual line) apply to this export only. The result gives the 7 card values as written
    (cards) and the DCMA checks that fail (dcma_failed)."""
    if baseline_dates not in get_args(BaselineDates):
        raise ValueError(f"baseline_dates must be one of {list(get_args(BaselineDates))}")
    if period not in get_args(Period):
        raise ValueError(f"period must be one of {list(get_args(Period))}")
    near = float(near_critical_days)
    if not math.isfinite(near) or near < 0:
        raise ValueError("near_critical_days must be 0 or more")
    codes = None
    if milestones is not None:
        codes = list(dict.fromkeys(str(c).strip() for c in ([milestones] if isinstance(milestones, str) else milestones)))
        if not codes or not all(codes):
            raise ValueError("milestones must list Activity IDs")
        if len(codes) > DASH_MILESTONES_MAX:
            raise ValueError(f"milestones: at most {DASH_MILESTONES_MAX} Activity IDs")
    texts = {k: (str(v).strip() or None) if v is not None else None
             for k, v in (("prepared_by", prepared_by), ("revision", revision))}
    for k, v in texts.items():
        if v and len(v) > 100:
            raise ValueError(f"{k}: at most 100 characters")
    brand = _dash_brand(company, colors)
    path = _export_target(path, ".xlsx")
    if os.path.exists(path) and not overwrite:
        raise ValueError(f"{path} already exists: pass overwrite=True to replace it, or choose another path.")
    _openpyxl()
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # project, WBS, activities and milestones from one snapshot
    try:
        bid = _baseline_pid(con, pid, baseline)
        rd = _dash_read(con, pid, bid, codes, baseline_dates)
        name = con.execute("SELECT wbs_name FROM PROJWBS WHERE proj_id = ? AND proj_node_flag = 'Y'"
                           + _alive(con, "PROJWBS"), (pid,)).fetchone()
    finally:
        con.rollback()
    warnings: list[str] = [f"Working days: {n}." for n in rd["wd_notes"]]
    if rd["rows_cut"]:
        warnings.append(f"More than {DASH_WBS_ROWS} WBS nodes: the first {DASH_WBS_ROWS} are shown.")
    if rd["ms_note"]:
        warnings.append(rd["ms_note"])
    if len(rd["acts"]) > DASH_ACT_CAP:
        warnings.append(f"{len(rd['acts']):,} activities: the Schedule Overview shows the first {DASH_ACT_CAP:,}.")
    summary = p6_project_summary(project, baseline)
    curve, curve_error = None, None
    for measure in ("units", "cost"):
        try:
            curve = p6_progress_curve(project, period=period, baseline=baseline, measure=measure)
            break
        except ValueError as e:
            curve_error = str(e)
    if curve is None:
        warnings.append(f"S-curve not drawn: {curve_error}")
    elif curve["measure"]["kind"] == "cost":
        warnings.append("No planned units on labor or nonlabor assignments: the S-curve uses cost.")
    crit = {r["task_code"] for r in _all_rows(p6_critical_path, 10 ** 7, project=project, fields=["task_code"])[1]}
    hc = p6_health_check(project, baseline=baseline, baseline_dates=baseline_dates, save_snapshot=False)
    state = hc["schedule_state"]
    seen = {(rd["project"]["last_recalc_date"], rd["project"]["last_schedule_date"]),
            (summary["dates"]["data_date"], summary["dates"]["last_schedule_run"]),
            (state["data_date"], state["last_schedule_date"])}
    if len(seen) > 1:
        raise ValueError(f"The schedule of {project} changed during the export (data date or last F9): run it again.")
    source = (", ".join(os.path.basename(f) for f in SETTINGS.xer_files) if SETTINGS.xer_files  # XER-only mode
              else os.path.basename(DB_PATH))
    data = {"project": project, "name": name and name[0], "db": source, "now": datetime.now(),
            "rd": rd, "summary": summary, "curve": curve, "curve_error": curve_error, "crit": crit, "hc": hc,
            "state": state, "near": near, "baseline_dates": baseline_dates, "baseline_arg": baseline,
            "warnings": warnings, "brand": brand, **texts}
    wb, counts, cards = _dash_book(data)
    tmp = path + ".tmp"
    try:  # written beside the target, then swapped in: a failed export leaves the old file intact
        wb.save(tmp)
        os.replace(tmp, path)
    except PermissionError as e:
        raise ValueError(f"Cannot write {path}: close it in Excel, then export again ({e.strerror})") from None
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"path": path, "project": project, "data_date": rd["project"]["last_recalc_date"],
            "baseline": rd["baseline"] and rd["baseline"]["proj_short_name"], "sheets": counts,
            "counts": {"wbs": len(rd["rows"]), "activities": len(rd["acts"]), "milestones": len(rd["milestones"]),
                       "critical": len(crit)},
            "cards": cards, "dcma_failed": [k for k, c in hc["checks"].items() if c.get("pass") is False],
            "warnings": warnings, "schedule_state": state,
            "note": "Values of the last F9 in P6. The Dashboard sheet is protected (no password) and prints on one A3 "
                    "landscape page."}


_register_prompts()


if __name__ == "__main__":
    mcp.run()
