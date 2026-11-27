"""P6 codes, names and limits shared by the tools."""

from __future__ import annotations

import re
import sqlite3
from typing import Optional


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
HARD_CSTR_SET = frozenset(re.findall(r"CS_\w+", HARD_CSTR))
DCMA_LIMIT_DAYS = 44.0  # DCMA default limit of high float, high duration and long lags (days of the calendar)
MAT_SQL = "MATERIALIZED " if sqlite3.sqlite_version_info >= (3, 35, 0) else ""  # CTE hint (SQLite 3.35+)
SQL_COUNT_CAP, SQL_COUNT_SECONDS = 1_000_000, 3  # p6_sql_select counts at most this many rows, for this long
SQL_PAGE_BYTES = 150_000  # p6_sql_select ends a page early past this much row JSON (one row at least)
SQL_QUERY_SECONDS = 30  # p6_sql_select stops a query (execute, skip and fetch) after this long
SQL_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list"}  # read-only ones
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
# p6_list_calendars(include_exceptions=True): calendars and exception dates per page, dates per calendar (< 200 KB)
CAL_EXC_ROWS, CAL_EXC_PAGE, CAL_EXC_EACH = 50, 800, 100
LOOKAHEAD_DAYS = 14  # p6_lookahead window without days= or weeks=
CAL_CHECK_YEARS = 10  # p6_calendar_check: years compared at most (from the start year of the span)
# p6_calendar_check finding kinds in report order, with their severity
CAL_CHECK_KINDS = {"calendar_missing": "error", "unreadable": "error", "base_missing": "error",
                   "no_working_time": "error", "rsrc_dep_no_calendar": "error",
                   "project_holiday_worked": "warning", "holiday_not_in_project": "warning",
                   "no_exceptions_in_year": "warning", "day_hours_stale": "warning",
                   "week_hours_stale": "info", "work_week_differs": "info"}
SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
LA_EXPORT_CAP = 5000  # p6_export_lookahead: activity rows written at most
LA_DAY_GRID_MAX = 92  # p6_export_lookahead: a day grid covers at most this many days (else weeks)
LA_AUTO_DAYS = 42  # grid='auto': days up to this window length, weeks beyond
FLOAT_EDGES = (-20.0, -10.0, -5.0, 0.0, 5.0, 10.0, 20.0, 44.0)  # p6_float_analysis default bucket edges (days)
STATUS_NAMES = {"TK_NotStart": "Not Started", "TK_Active": "In Progress", "TK_Complete": "Completed"}
TYPE_NAMES = {"TT_Task": "Task Dependent", "TT_Rsrc": "Resource Dependent", "TT_Mile": "Start Milestone",
              "TT_FinMile": "Finish Milestone", "TT_LOE": "Level of Effort", "TT_WBS": "WBS Summary"}
XER_READ_ONLY = ("XER-only mode is read only: the XER files are analysed, never changed. Select a P6 database in "
                 "MCP for Primavera P6 Settings to write.")
EPPM_TOOLS = ("p6_eppm_projects", "p6_eppm_pull", "p6_eppm_status")
# Edits allowed on a P6 EPPM working copy: the ones the push can send (docs/EPPM_PLAN.md 4.1), plus the local backups
EPPM_EDIT_TOOLS = frozenset({
    "p6_update_activity", "p6_update_activities", "p6_update_assignment", "p6_update_assignments",
    "p6_add_relationship", "p6_add_relationships", "p6_update_relationship", "p6_update_relationships",
    "p6_delete_relationship", "p6_delete_relationships", "p6_backup_database", "p6_restore_backup",
    "p6_eppm_push", "p6_eppm_schedule", "p6_eppm_discard"})
EPPM_NEXT = ("Changed in the local working copy only: nothing is sent to the P6 EPPM server until p6_eppm_push "
             "(dry run first, then the user's yes). p6_eppm_status lists the pending changes.")
