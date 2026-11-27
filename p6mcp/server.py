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

import bisect
import copy
import difflib
import functools
import json
import math
import os
import random
import re
import sqlite3
import statistics
import uuid
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta
from time import monotonic
from typing import Annotated, Any, Literal, Mapping, Optional, get_args

try:  # always installed with mcp
    from pydantic import Field
except ImportError:  # test stub without pydantic: annotations are never evaluated
    def Field(*_a, **_k):
        return None

from p6mcp import config as _config
from p6mcp import policy as _policy
from p6mcp import skills as _skills
from p6mcp import shop as _shop
from p6mcp.eppm import store as _eppm_store

from p6mcp.core import state as _state
from p6mcp import registry as _registry

# Imported names other modules and the tests read from p6mcp.server.
from p6mcp import __version__ as _pkg_version  # noqa: F401
from p6mcp import confirm as _confirm  # noqa: F401
from p6mcp import instructions as _instructions  # noqa: F401
from p6mcp import schema as _schema  # noqa: F401
from p6mcp import xerstore as _xerstore  # noqa: F401
from p6mcp.registry import FastMCP, ToolAnnotations, ToolError  # noqa: F401
from p6mcp.core.state import (  # noqa: F401  (re-exported)
    _cols_cache, _log_cache, _refresh, _ro_cons, _schema_cache, _shop_on,
)
from p6mcp.core.constants import (  # noqa: F401  (re-exported)
    ACT_EXTRA, ACT_LINKS_CAP, CAL_CHECK_KINDS, CAL_CHECK_YEARS, CAL_EXC_EACH, CAL_EXC_PAGE, CAL_EXC_ROWS, CSTR_FINISH,
    _cstr_label, CSTR_LABELS, CSTR_TYPES, DCMA_LIMIT_DAYS, EPPM_EDIT_TOOLS, EPPM_NEXT, EPPM_TOOLS, FLOAT_EDGES,
    HARD_CSTR, HARD_CSTR_SET, HPD_SQL, LA_AUTO_DAYS, LA_DAY_GRID_MAX, LA_EXPORT_CAP, LAG_RULES, LOOKAHEAD_DAYS, MAT_SQL,
    MAX_ROWS, REL_TYPES, REL_TYPES_REV, SEVERITY_RANK, SQL_COUNT_CAP, SQL_COUNT_SECONDS, SQL_PAGE_BYTES, SQL_PRAGMAS,
    SQL_QUERY_SECONDS, STATUS_NAMES, SUMMARY_TYPES, TYPE_NAMES, UDF_INDICATOR, UDF_KINDS, UDF_PROJECT_TABLES,
    UDF_TEXT_MAX, UDF_VALUE, XER_READ_ONLY,
)
from p6mcp.registry import (  # noqa: F401  (re-exported)
    ACT_FIELDS, ACT_HOURS, ActEdit, _ActEditKeys, ActEdits, ActField, ActFields, ActivityId, ActivityIds, AsgEdit,
    AsgEdits, BaselineDates, _before_call, CalCheckKind, CalCheckKinds, _CALL, CompareSection, CompareTo, _confirm_text,
    ConfirmText, ConfirmToken, CpMode, CstrName, CutPct, DanglingKind, DATE_PATTERN, DateStr, DATETIME_PATTERN,
    DateTimeStr, DcmaCheck, Depth, DESTRUCTIVE_DOC, Detail, DetailArg, Direction, DryRun, DurationBasis, _eppm_refusal,
    _eppm_skill_ok, EpsOrder, Explain, FloatEdges, _gated, GroupBy, HcCheck, HINT_KEYS, ImportMode, INSTRUCTIONS,
    IntegrityFlag, _json_text, LaGrid, LegacyConfirm, Level, LevelPriority, Limit, LinkEdit, LinkEdits, _LinkEnds,
    LinkIds, LinkNew, LinkNews, LinkRef, LinkRefs, LISTED, Measure, MsCodes, MsOrder, NewEps, _NewEpsKeys, NonNegDays,
    NonNegQty, Offset, PathNo, Pct, Period, _plan_gate, _preview, ProjectId, ProjectIds, ProjectList, ProjOrder,
    RedundantCategory, _register_prompts, REGISTERED, RelType, Removable, RiskIterations, RiskPcts, RiskRange,
    RiskRanges, SampleSize, SchedValues, _sdk_tool_kw, SERVER_VERSION, _slim_schema, Status, ThresholdCheck, Thresholds,
    TOKENS, _tool, TOOL_HINTS, TrendMs, TrendProjects, UdfEdit, _UdfEditKeys, UdfEdits, UdfFilter, _UdfFilterKeys,
    UdfFilters, UdfNames, UdfOp, UdfValue, _version_kw, WhatIfEdit, WhatIfEdits, WhatIfOp, WindowDays, WindowWeeks,
    _wire, WRITE_DOC, XerTable, Year,
)
from p6mcp.core.db import (  # noqa: F401  (re-exported)
    _act_from, _act_row, _act_view, _alive, _all_rows, _backup, _backup_dir, _backup_file, BACKUP_NAME,
    _backup_taken_at, _baseline_pid, _bound, _brief, _brief_on, _check_page, _cols, _connect, _days, _db_dt,
    _DB_DT_SHAPE, _db_dt_text, _dict, _env, _fmt_dt, _guard_write, _ids_sql, _insert, _next_key, _now_str, _num,
    _p6_running, _page, _parse_dt, _prefer, _project_id, _projects_of, _prune_backups, _ro_uri, _schema_info, _slice,
    _SQL_ALLOWED, _sql_authorizer, _SQL_SECRET_COL, _stamp_cols, _task, _update, _write_refusal,
)
from p6mcp.core.props import (  # noqa: F401  (re-exported)
    _blob_text, CHECK_SPEC, _db_task_code, _key_task, _lag_hpd, _lag_hpd_sql, _lag_info, _lag_rule, _lpl_arg, _lpl_of,
    LPL_ORDERS, _lpl_text, _norm_opt, _opt_eq, _opt_rows, _p6_dump, _p6_find, _p6_tree, _project_row, PROP_SPEC, _props,
    _sched_diff, _sched_options, SCHED_SPEC, _sched_write, SCHED_XER_COLS, _set_props, _task_id_by_code, _xer_lpl,
    _xer_project, _xer_sched, _xer_sched_export, _xer_sched_row, _xer_sched_values,
)
from p6mcp.core.calendar import (  # noqa: F401  (re-exported)
    _Cal, CAL_EPOCH, _cal_parse, _cal_row, CAL_SCAN_DAYS, CAL_WINDOW_DAYS, _calendar, _calendars, DEFAULT_WEEK, _hhmm,
    _hm, _lag_cal_of, _lag_hpd_of, _resolve_dt, _shift_txt, _shifts_of, _week_text, WEEKDAYS,
)
from p6mcp.core.editlog import (  # noqa: F401  (re-exported)
    _caller_tool, _edit_state, _log_dir, _log_dirs, _log_entries, _log_ts, _log_write, _schedule_state, _undone_fn,
)
from p6mcp.core.wbs_codes import (  # noqa: F401  (re-exported)
    _act_agg_sql, AGG_MAX, AGG_MIN, _code_filter, _code_tree, _code_type, _eps_chain, _eps_nodes, _eps_tree,
    _eps_wbs_id, _guid, _merge_agg, _name_sep, _rollup, _wbs_tree,
)
from p6mcp.core.read import (  # noqa: F401  (re-exported)
    _act_details, _critical_clause, _lag_arg, _memo_row, _memo_text, _plus, _rel_code, _rel_page, _rel_row_fn, _rel_sql,
    UDF_CMP, _udf_col, _udf_filter, _udf_out, _udf_row, _udf_type, _udf_values, _wbs_of,
)
from p6mcp.core.engine import (  # noqa: F401  (re-exported)
    _Cpm, CPM_TF_TOL_H, CPM_TOL, CPM_TRUST, _CS_FINISH, _CS_START, _cstr_sources, _DT_EPOCH, EARLY_CSTR, _ELAPSED,
    _Elapsed, ENGINE_VERSION, _fbo, _FLOAT_TYPES, _float_walk, GAP_TOL_H, _h2m, LATE_CSTR, _late_origin, LINK_FROM,
    LINK_TO, _mins, _mn, _mx, _NEG, _Net, _net_snapshot, _POS, REDUNDANT_CATEGORIES, _root_driver, _sbo, _shift_lag,
    _shift_w, _transitive, TRANSITIVE_DEPTH, _wm,
)
from p6mcp.core.analysis import (  # noqa: F401  (re-exported)
    _chk, _cmp_acts, _cmp_links, _cpli, _crit_named, _cstr_txt, _dangling_suggest, _dcma, DCMA_CHECKS, _dcma_settings,
    _downstream, HC_ACT_FIELDS, _hc_dur, HC_GRADES, HC_NO_ITEMS, _hc_pick, _hc_snap_dir, _hc_snapshot_save,
    _hc_snapshots, _hc_status, HC_TOTALS, HC_UNSCORED, HC_WARN_MAX, HC_WARN_MIN, HC_WARN_SHARE, HC_WEIGHTS, _integrity,
    INTEGRITY_CAVEAT, INTEGRITY_DEFS, INTEGRITY_FLAGS, _key_dates, _limit, LINK_KEY, OOS_SQL, _slip, SLIP_CAUSES,
    _tf_change, _weighted_score,
)
from p6mcp.core.explain import (  # noqa: F401  (re-exported)
    _cal_window, CAUSE_CLASSES, _cpm_classify, _cpm_compare, _cpm_context, CPM_DIFF_LABELS, _cpm_other_fields,
    _cpm_reloaded, _cpt, CTX_LINKS, CTX_WINDOW_WD, _data_issues, _dcma_brief, _dcma_memory, _engine_rules, F9_SAVE_TOL,
    _finish_ties, FLOAT_TEXT, IMPORT_WINDOW, LABEL_OF_TABLE, LAG_CAL_TEXT, OOS_TEXT, _project_context, _stale_f9,
    _stale_min, STALE_SAMPLE, _stale_view, _temp_network, _whatif_apply, _whatif_assumptions, WHATIF_CAVEAT,
    WHATIF_CUT_PCT,
)
from p6mcp.core.risk import (  # noqa: F401  (re-exported)
    _pctl, RISK_CAVEAT, RISK_DEFAULT, RISK_ITERATIONS, _risk_pcts, _risk_ranges, RISK_SOURCES, _risk_udfs, RISK_WORK,
)
from p6mcp.core.reporting import (  # noqa: F401  (re-exported)
    _available, _bl_planned_by_wbs, _cal_check_read, _code_list, _cost_ev, COST_SOURCE_OF, COST_SOURCES, _cum,
    _curve_items, CURVE_POINTS, _curve_shape, _diag_schema, ETC_FORMULAS, _etc_setting, _etc_technique, ETC_TECHNIQUES,
    EV_CURVE_TOKEN, _ev_map, _ev_pct, EV_TECHNIQUES, _expense_rows, _float_bucket, _float_edges, _float_labels,
    _group_row, _holidays, _la_query, LOAD_KIND_ORDER, _measure_rows, MS_VS, _perf_pct, _period_edges, RSRC_KINDS,
    _rsrc_limits, _shape_at, _Spread, _spread_cal, _spread_note, SPREAD_RULES, _summary, _trend, _trend_changes,
    _trend_check, _trend_float, TREND_HEAD, _trend_head, TREND_MAX, _trend_ms, TREND_MS_CAP, _trend_series, _value_at,
    _week_diff,
)
from p6mcp.core.writes import (  # noqa: F401  (re-exported)
    _act_check, ACT_DEL_CLEAR, ACT_DEL_ORDER, ACT_DEL_OWN, ACT_DEL_UDF, _act_del_where, _act_delete_rows, ACT_KEYS,
    ACT_SNAPSHOT, _act_update, ASG_COST, _asg_find, ASG_KEYS, ASG_QTY, ASG_RATE, ASG_TASK, ASG_TASK_KIND, _asg_update,
    _batch, _batch_commit, _commit, _create_eps, _cross_links, _db_tables, DEL_CLEAR, DEL_ORDER, DEL_OWN, _del_sets,
    DEL_SKIP, DEL_UDF, _del_where, _delete_rows, _delete_targets, _empty_eps, _ENDS, _eps_ancestors, _find_rel,
    FIXED_DRTN_TYPES, _item_keys, _items_arg, _link_batch, LINK_KEYS, MAX_BATCH, _open_for_write, ORPHAN_REFS, _orphans,
    _owned_where, _Refused, _rel_add, _rel_delete, _rel_delete_row, _rel_ends, _rel_log, REL_LOG_COLS, _rel_update,
    _release, _side_effects, _single_link, _sum_plan, SUM_TABLES, _summaries_stale, SUMMARIZE_ADVICE, _task_by_id,
    _temp_id_sets, UDF_KEYS, _udf_next, _udf_set, _udf_shown, _udf_store, _write_session,
)
from p6mcp.core.xer import (  # noqa: F401  (re-exported)
    _col_type, _cp1252_ok, _export_target, _export_xer, _has_next_key, IMPORT_ACTIONS, _import_target, _memo_blob,
    _missing_rows, _preview_matches, _removable, _remove_rows, UDF_FK_TABLES, _upsert, XER_ALIASES, XER_CHILD_KEYS,
    XER_CHILD_NEEDS, _XER_DATE, _xer_decode, XER_DETAIL_TABLES, _xer_encode, XER_EXPORT_TABLES, XER_FK, XER_MATCH,
    XER_MATCH_PARENT, XER_MEMO_COLS, _XER_NL, _xer_only, XER_PK, _xer_preflight, _xer_read, _xer_read_meta,
    XER_SKIP_COLS, XER_TABLES, _XerImport,
)
from p6mcp.core.dashboard import (  # noqa: F401  (re-exported)
    DASH_ACT_CAP, _dash_book, _dash_brand, DASH_FLOAT, DASH_GOOD, DASH_HEAD, DASH_INK, DASH_MILESTONES,
    DASH_MILESTONES_MAX, DASH_MUTED, DASH_NAV, DASH_PAGE, DASH_PANEL_MILESTONES, DASH_PILL, _dash_read, DASH_RULE,
    DASH_SHEETS, DASH_SO_LEVEL, DASH_STATUS, DASH_TABLE_ROWS, DASH_TINT, DASH_WBS_BARS, DASH_WBS_ROWS, DCMA_LABELS,
    _ink_on, LA_BAND, LA_BAR, _la_book, LA_COLS, LA_DD, LA_NONWORK, _openpyxl, _openpyxl_version, _xl_shown, _XlCells,
)


# Globals that _refresh() rebinds (and that tests patch) live in their owning module: the facade reads
# them from there on every access (PEP 562), so p6mcp.server.DB_PATH is always the current one.
_FORWARD = {
    'BACKUP_DIR': _state, '_backup_done': _state, 'DB_PATH': _state, 'EPPM_MANIFEST': _state, '_last_backup': _state,
    '_last_pruned': _state, 'LOG_DIR': _state, 'P6_USER': _state, 'SETTINGS': _state, 'SKIP_PROC': _state,
    'STARTUP': _state, 'XER_MANIFEST': _state,
    'mcp': _registry, 'PROMPTS': _registry,
}


def __getattr__(name: str):
    mod = _FORWARD.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(mod, name)


# ----------------------------------------------------------------- read tools

@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_list_projects(include_baselines: bool = False, exclude_templates: bool = False, eps: Optional[str] = None,
                     order: ProjOrder = "project", offset: Offset = 0, limit: Limit = 200) -> dict:
    """Projects, one page: {total, offset, returned, next_offset, rows}. Row: Project ID, name, data
    date, planned start, scheduled finish, status (of the project WBS node), baseline_of, and the EPS
    node holding it: eps_id, eps_name, eps_path (EPS names from the top, joined with ' > '); all three
    None for a project outside every EPS node. EPS nodes are never listed; baselines only with
    include_baselines=True (baseline_of is set); exclude_templates=True leaves out the P6 template
    projects (status WS_Template, or no EPS node). eps= an EPS ID: the projects of that EPS node and of
    every EPS below it. order: 'project' (Project ID) or 'eps' (EPS tree order as p6_list_eps
    order='tree', then Project ID; projects outside every EPS node last)."""
    if order not in get_args(ProjOrder):
        raise ValueError(f"order must be one of {list(get_args(ProjOrder))}")
    eps_id = eps  # the EPS ID asked (eps then holds the EPS tree)
    con = _connect()
    want = ["proj_id", "proj_short_name", "last_recalc_date", "plan_start_date", "scd_end_date"]
    have = [c for c in want if c in _cols(con, "PROJECT")]
    status = "w.status_code" if "status_code" in _cols(con, "PROJWBS") else "NULL"
    con.execute("BEGIN")  # EPS tree and page from one snapshot
    eps = _eps_tree(con)
    ids = ", ".join(str(int(n["wbs_id"])) for n in eps) or "NULL"
    keep = ""
    if eps_id is not None:
        top = next((n for n in eps if n["eps_id"] == eps_id), None)
        if top is None:
            con.rollback()
            raise ValueError(f"EPS {eps_id!r} not found: {[n['eps_id'] for n in eps]}")
        under, k = [top], eps.index(top) + 1
        while k < len(eps) and eps[k]["depth"] > top["depth"]:  # tree order: the subtree follows its node
            under.append(eps[k])
            k += 1
        keep = f" AND w.parent_wbs_id IN ({', '.join(str(int(n['wbs_id'])) for n in under)})"
    rank = "CASE w.parent_wbs_id " + " ".join(f"WHEN {int(n['wbs_id'])} THEN {i}" for i, n in enumerate(eps)) \
        + f" ELSE {len(eps)} END" if eps else "0"
    q = (f"SELECT {', '.join('p.' + c + ' AS ' + c for c in have)}, w.wbs_name AS project_name, "
         f"{status} AS status_code, o.proj_short_name AS baseline_of, "
         f"CASE WHEN w.parent_wbs_id IN ({ids}) THEN w.parent_wbs_id END AS eps_wbs_id "
         "FROM PROJECT p LEFT JOIN PROJWBS w ON w.proj_id = p.proj_id AND w.proj_node_flag = 'Y'"
         + _alive(con, "PROJWBS", "w") + " LEFT JOIN PROJECT o ON o.proj_id = p.orig_proj_id "
         "WHERE COALESCE(p.project_flag, 'Y') = 'Y'" + _alive(con, "PROJECT", "p")
         + ("" if include_baselines else " AND p.orig_proj_id IS NULL")
         + (f" AND w.parent_wbs_id IN ({ids}) AND {status} IS NOT 'WS_Template'" if exclude_templates else "") + keep
         + (f" ORDER BY {rank}, p.proj_short_name, p.proj_id" if order == "eps" else " ORDER BY p.proj_short_name, p.proj_id"))
    at = {n["wbs_id"]: n for n in eps}

    def row(r: sqlite3.Row) -> dict:
        d = _dict(r)
        e = at.get(d.pop("eps_wbs_id"))
        return {**d, "eps_id": e and e["eps_id"], "eps_name": e and e["eps_name"], "eps_path": e and e["path"]}
    try:
        return _page(con, q, (), offset, limit, row)
    finally:
        con.rollback()


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_list_wbs(project: ProjectId, parent: Optional[str] = None, max_depth: Optional[Depth] = None,
                rollups: bool = True, baseline: Optional[ProjectId] = None, offset: Offset = 0,
                limit: Limit = 100) -> dict:
    """WBS tree in P6 order (each node before its children), one page: {total, offset, returned,
    next_offset, rows, root}. Row: wbs_id, parent_wbs_id, depth (project node 0), wbs_code (as P6
    shows it, e.g. 'PRJ.5.1'), wbs_short_name, wbs_name, children (direct), and with rollups=True
    the rollup of its whole subtree: activities, open (not completed, LOE and WBS summary left
    out), not_started, in_progress, completed, min_tf_d and negative_float (open activities, days
    of each calendar), start, finish (actual, else
    early), budget_units, actual_units (labor plus nonlabor), units_pct (actual / at completion, as
    P6 Units % Complete) and bl_planned_pct: baseline planned units at the data date / baseline budget
    units of the subtree, x 100 (the baseline's labor and nonlabor assignments placed by the Activity ID
    of the current activities, linear between their planned dates as p6_progress_curve and the
    dashboard; None without a baseline, a data date or baseline units). baseline= a Project ID (default:
    the project baseline); the result says which one (baseline). parent= starts at that WBS code, full
    or relative to the project node ('5.1'); max_depth counts levels below the start node. root repeats
    the start node (unplaced = activities outside the tree, when it is the project node)."""
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
        bpl: dict[int, dict] = {}
        bid = _baseline_pid(con, pid, baseline)
        if rollups:
            own = {r["wbs_id"]: {k: r[k] for k in r.keys() if k != "wbs_id"} for r in con.execute(
                f"SELECT t.wbs_id AS wbs_id, {_act_agg_sql(con)} FROM TASK t LEFT JOIN CALENDAR c "
                "ON c.clndr_id = t.clndr_id WHERE t.proj_id = ?" + _alive(con, "TASK", "t") + " GROUP BY t.wbs_id",
                (pid,))}
            dd = (_db_dt(con.execute("SELECT last_recalc_date FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone()[0])
                  if "last_recalc_date" in _cols(con, "PROJECT") else None)
            if bid is not None and dd is not None:  # Activity ID -> WBS as the dashboard (the last one in its order)
                bpl = _bl_planned_by_wbs(con, bid, dd, {r[0]: r[1] for r in con.execute(
                    "SELECT t.task_code, t.wbs_id FROM TASK t WHERE t.proj_id = ?" + _alive(con, "TASK", "t")
                    + " ORDER BY COALESCE(t.act_start_date, t.early_start_date), t.task_code, t.task_id", (pid,))})
        bname = bid and con.execute("SELECT proj_short_name FROM PROJECT WHERE proj_id = ?", (bid,)).fetchone()[0]
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
    proll: dict[int, dict] = {}
    if rollups:
        for n in reversed(nodes):  # descendants come first in reverse pre-order
            w, par = n["wbs_id"], n["parent_wbs_id"]
            roll[w] = _merge_agg(roll.get(w, {}), own.get(w, {}))
            proll[w] = _merge_agg(proll.get(w, {}), bpl.get(w, {}))
            if par in ids:
                roll[par] = _merge_agg(roll.get(par, {}), roll[w])
                proll[par] = _merge_agg(proll.get(par, {}), proll[w])

    def row(n: dict) -> dict:
        out = {**n, "children": kids[n["wbs_id"]]}
        if rollups:
            pl = proll[n["wbs_id"]]
            out["rollup"] = {**_rollup(roll[n["wbs_id"]]), "bl_planned_pct": round(100 * pl["bl_plan"] / pl["bl_budget"], 2)
                             if pl.get("bl_budget") else None}
        return out

    res = _slice(sub, offset, limit)
    res["rows"] = [row(n) for n in res["rows"]]
    res["root"] = row(nodes[i])
    res["baseline"] = bname and {"project": bname, "source": "baseline=" if baseline else
                                 "project baseline (PROJECT.sum_base_proj_id)"}
    if rollups and i == 0:
        res["root"]["unplaced"] = sum(v["n"] for k, v in own.items() if k not in ids)
    if stored != len(nodes):
        res["note"] = f"{stored - len(nodes)} WBS rows not reachable from the project node."
    return res


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
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
    detail: DetailArg = "full",
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
    udfs {label: value} (None when empty). detail='brief' leaves the null values out of the rows (fields=
    still applies; udfs kept whole)."""
    if status is not None and status not in get_args(Status):
        raise ValueError(f"status must be one of {list(get_args(Status))}")
    row_fn = _act_row(fields, detail)
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


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_get_activity(project: ProjectId, task_code: ActivityId, detail: DetailArg = "full") -> dict:
    """Full detail of one activity: the list fields (see p6_query_activities), secondary
    constraint, remaining, expected, suspend and resume dates, calendar (hours_per_day converts
    hours to days), WBS (wbs_code as in p6_list_wbs, wbs_path names), predecessors and successors
    (first 50 of each, with predecessor_count / successor_count; page further with
    p6_get_relationships), assignments (resource and role, quantities and costs as stored),
    activity codes, UDFs and notebooks (first 50 of each), lag_rule and schedule_state.
    detail='brief' leaves the null values out of the activity and of its list rows, and drops from the
    links direction (the list says it), lag_h (lag_d stays) and other_project when it is this project."""
    brief = _brief_on(detail)
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
    if brief:
        for key in ("predecessors", "successors"):
            out[key] = [{k: v for k, v in r.items() if k not in ("direction", "lag_h")
                         and not (k == "other_project" and v == project)} for r in out[key]]
        out = {k: [_brief(r) if isinstance(r, dict) else r for r in v] if isinstance(v, list) else v
               for k, v in _brief(out).items()}
    return out


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_critical_path(project: ProjectId, mode: CpMode = "project", float_threshold_days: Optional[float] = None,
                     include_completed: bool = False, fields: Optional[ActFields] = None,
                     min_float_days: Optional[float] = None, offset: Offset = 0, limit: Limit = 50,
                     detail: DetailArg = "full") -> dict:
    """Critical activities of the last P6 schedule run (F9), one page: {total, offset, returned,
    next_offset, rows, definition, mode_used, threshold_h, counts, schedule_state, note}.
    mode: project (default) follows the project setting (total float <= its hours threshold, or
    longest path); total_float uses that hours threshold; longest_path uses P6's driving path flag.
    float_threshold_days forces total float <= that many days (not with longest_path); min_float_days
    keeps only total float > that many days (days of each activity's calendar, not with longest_path):
    min_float_days=0 with float_threshold_days=10 lists the near critical activities. LOE and WBS
    summary activities are left out (even when P6 flags them), completed ones too unless
    include_completed=True. Order: total float then start, or float path order for the longest
    path. counts (open activities except LOE and WBS summary, any page): open, total_float,
    longest_path, negative_float; excluded_summary = LOE and WBS summary activities that meet the
    definition and the status filter but are not listed. fields= and detail= as in p6_query_activities."""
    row_fn = _act_row(fields, detail)
    con = _connect()
    pid = _project_id(con, project)
    cond, args, definition, thr_h, used = _critical_clause(con, pid, mode, float_threshold_days)
    tf_cond, tf_args = _critical_clause(con, pid, "total_float", float_threshold_days)[:2]
    if min_float_days is not None:
        lo = _num(min_float_days)
        if lo is None or not math.isfinite(lo):
            raise ValueError("min_float_days must be a number of days")
        if used == "longest_path":
            raise ValueError("min_float_days applies to total float, not to the longest path")
        cond, args = f"({cond}) AND t.total_float_hr_cnt > ? * {HPD_SQL}", [*args, lo]
        definition += f"; total float > {lo:g} d (days of each activity's calendar)"
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


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_float_paths(project: ProjectId, path: Optional[PathNo] = None, include_completed: bool = False,
                   fields: Optional[ActFields] = None, offset: Offset = 0, limit: Limit = 50,
                   detail: DetailArg = "full") -> dict:
    """Float paths of the last P6 schedule run (Schedule Options, multiple float paths), one page
    in P6 order (path, then order on the path): {total, offset, returned, next_offset, rows, paths,
    source, schedule_state}. Path 1 is the most critical. paths summarises each path whatever the
    page: activities, first_order, last_order, min_tf_d, max_tf_d, finish. path= keeps one path.
    Completed activities only with include_completed=True. When P6 stored no float path, rows are
    the longest path activities (driving_path_flag, LOE and WBS summary left out as in
    p6_critical_path) by start and source is 'driving_path_flag'. fields= and detail= as in
    p6_query_activities."""
    row_fn = _act_row(fields, detail)
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


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
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
    {count, sample}; leads_negative_lag, positive_lags, non_fs_links (links whose successor is open).
    checks.logic_density (after logic): links whose successor is open / open activities, value at least
    1.5 (not in summary or score, which stay the DCMA 14). negative_float.worst and high_float.worst: the
    open activity with the lowest / highest total float {task_code, task_name, total_float_d}.
    weighted_score {points, grade, earned, max, checks {name: {weight, status pass / warn / fail /
    not_evaluated, earned}}}: logic_density 20, logic 20, critical_path_test 15, negative_float 15,
    high_float 10, hard_constraints 10, high_duration 5, leads 5, lags 5, fs_links 5; pass = the weight,
    warn = half (a failed at most check up to 2 x its threshold, a failed at least check down to 0.9 x it;
    no band for a zero threshold), fail = 0, not evaluated left out; points = earned / weight of the
    evaluated checks x 100; grade A >= 90, B >= 80, C >= 70, D >= 55, else F. engine_version: the CPM
    engine of the critical path test."""
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
           "summary": {k: [c for c, v in checks.items() if v["pass"] is ok and c not in HC_UNSCORED] for k, ok in
                       (("failed", False), ("passed", True), ("not_evaluated", None))},
           "threshold_source": cfg["source"]}
    sm = res["summary"]
    ev = len(sm["passed"]) + len(sm["failed"])
    res["score"] = {"passed": len(sm["passed"]), "failed": len(sm["failed"]), "not_evaluated": len(sm["not_evaluated"]),
                    "evaluated": ev, "pct_passed": round(100 * len(sm["passed"]) / ev, 1) if ev else None}
    res["weighted_score"] = _weighted_score(checks)
    res["engine_version"] = ENGINE_VERSION
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
                "project": project, "proj_id": pid, "db": os.path.basename(_state.DB_PATH), "ts": _log_ts(),
                "last_schedule_date": state["last_schedule_date"], "data_date": state["data_date"],
                "stale": state["stale"], "params": {"baseline": res["baseline"] and res["baseline"]["project"],
                                                    "baseline_dates": baseline_dates, "duration_basis": duration_basis,
                                                    "thresholds": dict(thresholds or {}), "high_float_days": high_float_days},
                "totals": {"open_incomplete_activities": n_open, "relationships": n_links,
                           "open_successor_links": fs["denominator"], "non_fs_links": res["non_fs_links"]},
                "checks": {k: {f: v.get(f) for f in ("count", "denominator", "pct", "value", "threshold", "pass")}
                           for k, v in checks.items()},
                "weighted_score": res["weighted_score"],
                "engine_version": ENGINE_VERSION, "items": keys}
        try:
            _hc_snapshot_save(snap)
            res["snapshot"] = {"id": snap["id"]}
        except OSError as e:
            res["snapshot"] = {"error": f"snapshot not saved: {e}"}
    return res


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_health_check_compare(project: ProjectId, before: str = "previous", after: str = "latest") -> dict:
    """Compare two health check snapshots of a project (each full p6_health_check run saves one):
    before / after = 'latest', 'previous' (the one before latest) or a snapshot id (any database), or
    after='current' to run the health check now (before 'latest' or 'previous' is then the latest snapshot
    saved before that run). totals: open activities, relationships, links with an
    open successor, non-FS links {before, after, delta}. checks: per check count, pct and value
    {before, after, delta}, pass {before, after}, and the items that entered and left it (activity IDs or
    links 'pred -> succ type', first 50 of each, with entered_count / left_count). changed: checks whose
    count moved. weighted_score: points {before, after, delta} and grade {before, after} (null for a
    snapshot saved before weighted scores). engine_version {before, after}. warnings: parameters that
    differ (baseline, thresholds...), and the engine version when it differs. snapshots: the project's
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
    ev = {"before": a.get("engine_version"), "after": b.get("engine_version")}
    if ev["before"] != ev["after"]:
        warn.append(f"engine_version differs: {ev['before']} -> {ev['after']}" + (
            " (a snapshot saved before engine versions were recorded)" if None in ev.values() else "")
            + ": the critical path test may differ for that reason alone")
    wa, wb = a.get("weighted_score") or {}, b.get("weighted_score") or {}
    weighted = {"points": {"before": wa.get("points"), "after": wb.get("points"),
                           "delta": delta(wa.get("points"), wb.get("points"))},
                "grade": {"before": wa.get("grade"), "after": wb.get("grade")}}
    return {"project": project, "before": meta(a), "after": meta(b), "totals": totals, "checks": checks,
            "changed": [k for k, v in checks.items() if (v.get("count") or {}).get("delta")],
            "weighted_score": weighted, "engine_version": ev,
            "warnings": warn, "snapshots": [meta(s) for s in snaps[-20:]]}


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
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
    mode = ("progress override forward, retained logic backward (both flags Y)" if rl == po == "Y"
            else "retained logic" if rl == "Y" else "progress override" if po == "Y"
            else "actual dates" if rl == po == "N" else "retained logic (P6 default, no option stored)")
    return {"project": project, "data_date": dd, "out_of_sequence": oos,
            "out_of_sequence_total": {"links": tot[0], "successors": tot[1]}, "status_checks": status,
            "logic_option": {"mode": mode, "sched_retained_logic": rl, "sched_progress_override": po, "source": src,
                             "note": "Retained logic keeps the remaining work of an out-of-sequence successor after its "
                                     "predecessors at F9; progress override ignores those links."},
            "schedule_state": _schedule_state(con, pid),
            "note": "Statuses and dates as stored (last update and F9). Predecessors in other projects are shown as "
                    "PROJECT:ACTIVITY."}


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_driving_path(project: ProjectId, target: ActivityId, max_depth: Annotated[int, Field(ge=1)] = 500,
                    include_completed: bool = False, offset: Offset = 0, limit: Limit = 100) -> dict:
    """Driving predecessors of an activity, traced backward on the early dates of the last F9: a link
    drives when its gap is 0 (within 5 minutes): working time from the predecessor's date plus lag
    (lag calendar rule) to the successor's date, by type (FS finish to start, SS start to start, FF
    finish to finish, SF start to finish), on the successor calendar. Every driving predecessor is
    followed (several can drive), down to max_depth levels. Page of rows, one per activity, by level (0 =
    target): task_code, name, early dates, total_float_d, calendar, constraint, and the link it drives
    through (successor, type, lag_d, gap_h, gap_d; also_drives for other path activities it drives).
    drivers_at_root: what starts each chain (task_code, task_name, date; type data_date, constraint,
    as_late_as_possible, actual_date, open_start,
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
        row = {"level": lv, **net.act_view(d), **dict.fromkeys(("successor", "successor_name", "task_pred_id", "type",
                                                                    "lag_d", "gap_h", "gap_d")), "also_drives": []}
        es = sorted((e for e in edges.get(tid, []) if e[0]["succ"] in level),
                    key=lambda e: (level[e[0]["succ"]], net.t[e[0]["succ"]]["task_code"]))
        if es:  # the level 0 row (the target) keeps null link keys
            row.update(net.link_view(es[0][0], es[0][1], "succ"))
            row["also_drives"] = [net.link_view(lk, g, "succ") for lk, g in es[1:]]
        rows.append(row)
    rows.sort(key=lambda r: (r["level"], r["early_finish"] or "", r["task_code"]))
    res = _slice(rows, offset, limit)
    res.update(target=net.act_view(tgt), drivers_at_root=roots, data_date=net.prj["last_recalc_date"],
               lag_rule=_lag_info(con, pid), tolerance_h=round(GAP_TOL_H, 4), schedule_state=_schedule_state(con, pid))
    if cut:
        res["note"] = f"Stopped at max_depth {max_depth}: earlier drivers not listed."
    return res


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_float_origin(project: ProjectId, task_code: ActivityId, max_depth: Annotated[int, Field(ge=1)] = 500,
                    offset: Offset = 0, limit: Limit = 100) -> dict:
    """Where the late dates (and so the total float) of an activity come from, on the last F9: walks
    forward along the successor links that set the late date (late gap 0 within 5 minutes: from the
    predecessor late date to the successor late date less the lag, by type, on the predecessor calendar,
    as the backward pass lays the bound; out-of-sequence options applied), following
    every controlling successor, until the source. origin: type activity_constraint (a constraint
    bounding late dates: start/finish on, on or before, mandatory), project_must_finish
    (PROJECT.plan_end_date), open_end (no controlling successor: project finish), unexplained, or mixed
    (several sources, all in sources), or none (max_depth reached before any source); task_code,
    task_name and date always present (null unless there is exactly one source; the constraint date for
    a constraint), the constraint keys with a constraint; every source carries task_code, task_name and
    date; explanation says it in
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
    w = _float_walk(net, d0, max_depth)
    level, via, origin, own, expl, cut = w["level"], w["via"], w["origin"], w["own"], w["explanation"], w["cut"]
    rows = []
    for tid, lv in level.items():
        row = {"level": lv, **net.act_view(net.t[tid], late=True)}
        vs = sorted(via.get(tid, []), key=lambda e: (level.get(e[0]["succ"], 0), net.t[e[0]["succ"]]["task_code"]))
        row["controlled_by"] = [net.link_view(lk, g, "succ") for lk, g in vs]  # [] at the source
        rows.append(row)
    rows.sort(key=lambda r: (r["level"], r["task_code"]))
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


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
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
        rows.append({"task_pred_id": i, "predecessor": p["task_code"], "predecessor_name": p["task_name"],
                     "successor": s["task_code"], "successor_name": s["task_name"],
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


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_cpm_validate(project: ProjectId, offset: Offset = 0, limit: Limit = 50) -> dict:
    """Run this server's CPM engine on the unchanged network and compare it with P6's last F9: early
    start and finish, late start and finish and total float of every open activity (completed ones:
    early dates; their late dates, unused by float, are counted apart). match_rate = share of open
    activities identical on all five (dates within a minute, float within 3 minutes); trusted when it
    is at least 0.99 (p6_schedule_whatif and the DCMA critical path test rely on it). Page of
    mismatched activities: differences (stored, engine), differences_text (the same in words),
    probable_cause, cause_class, evidence (every finding on the activity), inherited_from (the mismatched
    neighbour whose difference it inherits, with its class) and context: the inputs both used (activity
    type, status, calendar with work week and the non-work days within 10 working days of the differing
    date, constraints, actual dates, remaining and original duration, suspend, resume, expected finish;
    predecessors with type, lag, lag calendar, drives_stored and drives_engine; successors with
    controls_stored / controls_engine when a late date differs). cause_class: stale_f9 (the activity, its
    links, assignments or calendar, a linked activity of another project, the scheduling options, deleted
    rows or this server's writes changed after the last F9: the stored dates are out of date, not wrong),
    data (invalid input: actual dates after the data date or out of order, status against actuals, out of
    sequence progress, missing or unreadable calendar, unreadable date, constraint without a date, no F9
    recorded), engine (same inputs, another result: the rule is named), unknown (never guessed);
    cause_counts per class. stale_f9 / stale_f9_detail: whether anything changed after the last F9 (update
    dates compared from last_schedule_date + 5 min, or from the end of the project's XER import into this
    database), with counts and a note: press F9 in P6 or use a frozen copy before trusting the comparison.
    project_context: data date, last F9, must finish, project calendar and the scheduling options in force.
    A page ends early (page_note, next_offset) past about 150 KB. other_fields: free float, longest path flag
    and float path (when multiple float paths are on) of open activities, engine against stored, outside
    match_rate. rules: what the engine models; engine_version: version of its date, calendar and float
    rules. Read only."""
    offset, limit = _check_page(offset, limit)
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # network, stored F9, update dates and links from one snapshot
    try:
        cpm = _Cpm(con, pid)
        ev = _stale_f9(con, pid, cpm)
        net = _Net(con, pid)
    finally:
        con.rollback()
    state = _schedule_state(con, pid)
    t0 = monotonic()
    res = cpm.run()
    secs = round(monotonic() - t0, 2)
    cmp = _cpm_compare(cpm, res, state["stale"], not state["last_schedule_date"], ev)
    fm, end = cpm.finish(res)
    out = _slice(cmp.pop("rows"), offset, limit)
    if out["rows"]:
        net_e = copy.copy(net)  # the same network with the engine's dates: which link drives each engine date
        net_e.t = dict(net.t)
        for n in cpm.t:
            if n in net.t:
                net_e.t[n] = {**net.t[n], **{k: res[n][k] for k in ("es", "ef", "ls", "lf")}}
        size = 0
        for i, r in enumerate(out["rows"]):
            r["context"] = _cpm_context(cpm, res, net, net_e, r["_tid"], r["differences"])
            size += len(json.dumps(r, indent=2, default=str)) + 4
            if size > SQL_PAGE_BYTES and i:  # a page stays under the client budget: the next page continues
                out.update(rows=out["rows"][:i], returned=i, next_offset=offset + i,
                           page_note=f"page ended after {i} rows (about {SQL_PAGE_BYTES} bytes with their context): "
                                     "pass next_offset for the next page")
                break
    for r in out["rows"]:
        r.pop("_tid", None)
    out.setdefault("page_note", None)
    out.update(project=project, **cmp, trusted=cmp["match_rate"] is not None and cmp["match_rate"] >= CPM_TRUST,
               stale_f9=ev["stale"], stale_f9_detail=_stale_view(ev, cpm), project_context=_project_context(cpm, ev),
               project_finish={"engine": _fmt_dt(end), "stored": cpm.prj["scd_end_date"],
                               "finish_activity": fm and fm["task_code"]},
               options={k: cpm.opts.get(k) for k in ("sched_retained_logic", "sched_progress_override",
                                                      "sched_lag_early_start_flag", "sched_calendar_on_relationship_lag",
                                                      "sched_float_type", "sched_use_project_end_date_for_float",
                                                      "sched_use_expect_end_flag", "sched_open_critical_flag")},
               seconds=secs, schedule_state=state, engine_version=ENGINE_VERSION,
               rules="P6 25.12 rules measured on 107 parity cases (engine version 23): every status placed, "
                     "out-of-sequence modes, lag from progressed predecessors, calendars per activity and lag rule, "
                     "planned start and resume dates, constraints and pairs, ALAP, LOE, WBS summary, resource dependent "
                     "activities on their resources' calendars, expected finish, open ends, total float per "
                     "sched_float_type, free float, longest path, float paths; resource leveling not modelled")
    return out


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_schedule_whatif(project: ProjectId, edits: WhatIfEdits, compare_to: CompareTo = "current",
                       milestones: Optional[list[ActivityId]] = None, offset: Offset = 0, limit: Limit = 50,
                       max_cut_pct: CutPct = WHATIF_CUT_PCT, near_critical_days: NonNegDays = 10,
                       dcma: bool = True) -> dict:
    """What-if scheduling in memory (the database is never written): applies the edits in order (add,
    delete or update links, durations, constraints; lag days and durations in days of the lag rule and
    activity calendars), runs this server's CPM engine and compares with compare_to: 'current' (P6's
    last F9 as stored) or 'engine' (the engine on the unchanged network: isolates the edits from any
    engine difference). project_finish {before, after, delta_cd (calendar days), delta_wd (working days
    of the finish activity)}; milestones (given, else the finish activity and every constrained
    activity) with finish and total float before / after; float_change: open activities (LOE and WBS
    summary left out) critical and with negative float before and after under the project's critical
    definition (total float threshold, or the longest path the engine computes), became_critical (count
    and up to 50 rows by float after) and no_longer_critical; longest_path_change: open activities that
    joined (up to 50 rows) or left the longest path; page of changed activities (early dates or
    total float moved by more than a minute), largest finish change first. engine: match rate of the
    unchanged network against the F9 (trusted at 0.99); engine_version. assumptions (flags only, the run is
    never refused): duration_cuts (cut by more than max_cut_pct, default 30, of the original or remaining
    duration), open_ends (open activities left without a predecessor or successor, or without a link to
    their start or from their finish, by a deleted or retyped link), hard_constraints (new mandatory, must
    or no later than constraints), negative_lags, ss_zero_lag (with starts_together and overlap_d),
    near_critical (became near critical: total float within near_critical_days, default 10, of the critical
    limit), finish_convergence (links driving the activities on the project finish, before and after),
    flag_count, and dcma: p6_health_check's DCMA checks before and after the edits, in memory with no
    snapshot (failed_before, failed_after, newly_failed, no_longer_failed, changed; dcma=False skips them).
    caveat: the dates are calculated the way P6 would, not checked against crews, materials or access.
    Changes to make for real: the write tools, then F9 in P6."""
    if compare_to not in get_args(CompareTo):
        raise ValueError("compare_to must be current or engine")
    if not isinstance(edits, (list, tuple)) or not edits:
        raise ValueError("edits: a list of at least one edit")
    cut, near = _num(max_cut_pct), _num(near_critical_days)
    if cut is None or not 0 <= cut <= 100:
        raise ValueError("max_cut_pct must be a number from 0 to 100")
    if near is None or near < 0:
        raise ValueError("near_critical_days must be 0 or more")
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")
    try:
        base = _Cpm(con, pid)
        thr_h, used = _critical_clause(con, pid, "project", None)[3:5]
    finally:
        con.rollback()
    state = _schedule_state(con, pid)
    d0: dict = {}
    r0 = base.run(d0)
    chk = _cpm_compare(base, r0, state["stale"])
    trial = base.copy()
    done, trace = [], []
    for i, e in enumerate(edits):
        if not isinstance(e, Mapping):
            raise ValueError(f"edit {i}: an object with op")
        k = len(trace)
        try:
            done.append(_whatif_apply(trial, e, trace))
        except ValueError as err:
            raise ValueError(f"edit {i} ({e.get('op')}): {err}") from None
        for t in trace[k:]:
            t["edit"] = done[-1]
    d1: dict = {}
    r1 = trial.run(d1)
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
    lim = float(thr_h or 0) if used == "total_float" else 0.0
    is_open = lambda d: d["status_code"] != "TK_Complete" and d["task_type"] not in SUMMARY_TYPES  # noqa: E731
    lp_before = (lambda n: T[n].get("driving_path_flag") == "Y") if compare_to == "current" \
        else (lambda n: r0[n]["longest"])  # noqa: E731
    if used == "total_float":
        crit_b = lambda n: before[n]["tf_h"] is not None and before[n]["tf_h"] <= lim + CPM_TF_TOL_H  # noqa: E731
        crit_a = lambda n: r1[n]["tf_h"] is not None and r1[n]["tf_h"] <= lim + CPM_TF_TOL_H  # noqa: E731
    else:
        crit_b, crit_a = lp_before, (lambda n: r1[n]["longest"])
    neg = lambda tf: tf is not None and tf < -CPM_TF_TOL_H  # noqa: E731
    opn = [n for n, d in T.items() if is_open(d)]
    row = lambda n: {"task_code": T[n]["task_code"], "task_name": T[n]["task_name"],  # noqa: E731
                     "total_float_d_before": _days(before[n]["tf_h"], T[n]["hpd"]),
                     "total_float_d_after": _days(r1[n]["tf_h"], T[n]["hpd"])}
    tf_key = lambda r: (r["total_float_d_after"] if r["total_float_d_after"] is not None else 0, r["task_code"])  # noqa: E731
    became = sorted((row(n) for n in opn if crit_a(n) and not crit_b(n)), key=tf_key)
    joined = sorted((row(n) for n in opn if r1[n]["longest"] and not lp_before(n)), key=tf_key)
    float_change = {"definition": (f"total float <= {lim:g} h" if used == "total_float" else
                                   "on the longest path (the project's critical definition)")
                    + "; open activities, LOE and WBS summary left out",
                    "critical_before": sum(bool(crit_b(n)) for n in opn),
                    "critical_after": sum(bool(crit_a(n)) for n in opn),
                    "negative_float_before": sum(neg(before[n]["tf_h"]) for n in opn),
                    "negative_float_after": sum(neg(r1[n]["tf_h"]) for n in opn),
                    "became_critical": len(became), "became_critical_rows": became[:ACT_LINKS_CAP],
                    "no_longer_critical": sum(bool(crit_b(n)) and not crit_a(n) for n in opn)}
    longest_change = {"joined": len(joined), "joined_rows": joined[:ACT_LINKS_CAP],
                      "left": sum(bool(lp_before(n)) and not r1[n]["longest"] for n in opn),
                      "on_path_after": sum(bool(r1[n]["longest"]) for n in opn)}
    assumptions = _whatif_assumptions(base, trial, before, r1, trace, crit_b, crit_a, opn, cut, near, lim,
                                      _finish_ties(base, r0, d0), _finish_ties(trial, r1, d1))
    if dcma:
        cb, src = _dcma_memory(pid, base, r0) if compare_to == "engine" else \
            _dcma_memory(pid, stored=(base, r0, chk["match_rate"]))
        ca = _dcma_memory(pid, trial, r1)[0]
        fb, fa = ([k for k, v in c.items() if v["pass"] is False and k not in HC_UNSCORED] for c in (cb, ca))
        assumptions["dcma"] = {
            "before": _dcma_brief(cb), "after": _dcma_brief(ca), "failed_before": fb, "failed_after": fa,
            "newly_failed": [k for k in fa if k not in fb], "no_longer_failed": [k for k in fb if k not in fa],
            "changed": [k for k in ca if k in cb and any(ca[k].get(f) != cb[k].get(f) for f in ("count", "pct", "value", "pass"))],
            "threshold_source": src,
            "note": "p6_health_check's DCMA checks (project settings, project baseline, remaining durations) run in "
                    "memory, no snapshot saved: before = " + ("P6's last F9 as stored" if compare_to == "current" else
                                                              "the engine on the unchanged network")
                    + ", after = the edited network with the engine's dates (critical path test rerun on it)"}
    else:
        assumptions["dcma"] = None
    out = _slice(changed, offset, limit)
    out.update(project=project, edits=done, compare_to=compare_to,
               project_finish={"before": fin_before and _fmt_dt(fin_before), "after": _fmt_dt(fin_after),
                               "delta_cd": delta(fin_before, fin_after), "delta_wd": wd(fin_before, fin_after),
                               "finish_activity": fm and fm["task_code"]},
               milestones=ms, float_change=float_change, longest_path_change=longest_change,
               assumptions=assumptions, caveat=WHATIF_CAVEAT,
               engine={"match_rate": chk["match_rate"], "trusted": chk["match_rate"] is not None
                                      and chk["match_rate"] >= CPM_TRUST, "open_mismatched": chk["counts"]["open_mismatched"]},
               engine_version=ENGINE_VERSION, schedule_state=state, note="In memory only: nothing was written. "
               + ("" if compare_to == "engine" else "Before = P6's last F9; with an engine mismatch, compare_to='engine' "
                                                    "isolates the edits."))
    return out


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_schedule_risk(project: ProjectId, iterations: RiskIterations = 1000, seed: int = 1,
                     milestones: Optional[list[ActivityId]] = None, default_range: Optional[RiskPcts] = None,
                     ranges: Optional[RiskRanges] = None, udf_optimistic: Optional[str] = None,
                     udf_most_likely: Optional[str] = None, udf_pessimistic: Optional[str] = None,
                     offset: Offset = 0, limit: Limit = 50) -> dict:
    """Monte Carlo schedule risk in memory (the database is never written): each iteration draws the remaining
    duration of every open activity (not completed, not a milestone, LOE or WBS summary) from a triangular
    range, then runs the forward pass of this server's CPM engine (the one of p6_schedule_whatif: activity
    calendars, link types, lag calendar rule, constraints, data date, progress). Ranges in % of the remaining
    duration: default_range (default min 85, mode 100, max 135), ranges= by task_code, activity code value
    (code_type + code_value) or WBS (deepest node wins), and udf_optimistic / udf_most_likely /
    udf_pessimistic (number UDFs in days of the activity calendar, used for the activities that have all
    three); precedence UDF, task, code, WBS, default. Activities whose duration cannot move (expected finish in
    use, mandatory finish) are not drawn. iterations (default 1000) are clamped to 100..5000, then to at most
    1,500,000 activity passes (iterations_requested and iterations_note say so); seed (default 1) makes the
    run repeatable: the same project, arguments and seed give the same output. deterministic: the engine on
    unchanged durations (finish, finish_activity and its name, stored_finish = P6's last F9). finish: p10, p50,
    p80, p90,
    min, max of the project finish (latest early finish, LOE and WBS summary left out; nearest-rank
    percentiles). on_time_probability: deterministic_pct (finish at or before the deterministic one) and
    must_finish_pct (at or before the must finish by date; None without one). p80_buffer_wd: working days of
    the project calendar from the deterministic finish to p80. milestones (given, else the finish activity
    and every open constrained activity): deterministic, p10 to p90 of its finish, on_time_pct, constraint,
    constraint_date, by_constraint_pct (its constrained date, start or finish, at or before the constraint
    date), p80_buffer_wd (its calendar). histogram: weeks (P6 week start day) with count, pct, cum_pct.
    Page of drivers by criticality: rows of open activities that sat on the critical path of at least one
    iteration (the walk back from the project finish along the links that set each start or finish),
    criticality_index (share of iterations, 0 to 1) and criticality_pct, on_deterministic_path, total_float_d
    and rem_dur_d (stored), min_d / mode_d / max_d drawn (days of its calendar) and range_source. engine:
    match rate of the unchanged network against the last F9 and a warning below 0.99 (see p6_cpm_validate);
    engine_version; distribution: what was drawn; caveat: what this estimate leaves out. A logic loop is
    refused, as P6 does."""
    offset, limit = _check_page(offset, limit)
    try:
        n_req, seed = int(iterations), int(seed)
    except (TypeError, ValueError):
        raise ValueError("iterations and seed must be whole numbers") from None
    labels = [udf_optimistic, udf_most_likely, udf_pessimistic]
    if any(labels) and not all(labels):
        raise ValueError("udf_optimistic, udf_most_likely and udf_pessimistic go together: give all three or none")
    dflt = RISK_DEFAULT if default_range is None else _risk_pcts(default_range, "default_range")
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # network, ranges and UDF values from one snapshot
    try:
        cpm = _Cpm(con, pid)
        assign, echo = _risk_ranges(con, pid, cpm, ranges)
        uvals, uinfo = _risk_udfs(con, pid, labels) if all(labels) else ({}, None)
        week_day = _prefer(con, "week_start_day_num") or 1
    finally:
        con.rollback()
    state = _schedule_state(con, pid)
    full = cpm.run()  # refuses a logic loop and a missing data date
    chk = _cpm_compare(cpm, full, state["stale"])
    plan, T = cpm.plan(), cpm.t
    dist: dict[int, tuple] = {}  # task_id -> (low, high, mode hours, source)
    fixed = 0
    for n in sorted(T):
        d = T[n]
        if d["status_code"] == "TK_Complete" or d["task_type"] in ("TT_LOE", "TT_WBS", "TT_Mile", "TT_FinMile"):
            continue
        if cpm.duration_fixed(d):
            fixed += 1
            continue
        hpd = d["hpd"] or 8.0
        if n in uvals:
            lo, mo, hi = (x * hpd for x in uvals[n])
            src = "udf"
        else:
            src, pcts, i = assign.get(n, ("default", dflt, None))
            if i is not None:
                echo[i]["applied"] += 1
            lo, mo, hi = (d["rem"] * x / 100 for x in pcts)
        dist[n] = (lo, hi, mo, src)
    sampled = list(dist)
    affected, stack = set(sampled), list(sampled)  # what a draw can move: the drawn activities and their successors
    while stack:
        for lk, *_ in plan["by_p"].get(stack.pop(), ()):
            if lk["succ"] not in affected:
                affected.add(lk["succ"])
                stack.append(lk["succ"])
    sub = [n for n in plan["order"] if n in affected]
    cap = max(RISK_ITERATIONS[0], RISK_WORK // max(1, len(sub)))
    n_iter = min(max(n_req, RISK_ITERATIONS[0]), RISK_ITERATIONS[1], cap)
    base_drv: dict = {}
    base = cpm.forward(plan, drivers=base_drv)
    own = [n for n in T if T[n]["task_type"] not in ("TT_LOE", "TT_WBS")]
    own_sub = [n for n in own if n in affected]
    own_fixed = [n for n in own if n not in affected]
    fixed_end = max((base[n]["ef"] for n in own_fixed), default=cpm.dd)
    fixed_top = [n for n in own_fixed if base[n]["ef"] >= fixed_end - CPM_TOL]
    counted = {n for n in own if T[n]["status_code"] != "TK_Complete"}  # open activities get a criticality index
    fixed_ties: dict[int, tuple] = {}  # ties of the activities no draw moves, worked out once

    def path(res: dict, drv: dict, end: datetime) -> set:
        """Open activities on the critical path of one pass: from those finishing at the project finish, back
        along the links that set each one's dates (completed activities end the walk)."""
        stack = [n for n in own_sub if res[n]["ef"] >= end - CPM_TOL] + (fixed_top if fixed_end >= end - CPM_TOL else [])
        seen = set(stack)
        while stack:
            n = stack.pop()
            if n in affected:
                qs = cpm.ties(drv.get(n))
            else:
                qs = fixed_ties.get(n)
                if qs is None:
                    qs = fixed_ties[n] = cpm.ties(base_drv.get(n))
            for q in qs:
                if q not in seen and T[q]["status_code"] != "TK_Complete":
                    seen.add(q)
                    stack.append(q)
        return seen & counted
    det_end = max(fixed_end, max((base[n]["ef"] for n in own_sub), default=fixed_end))
    det_path = path(base, base_drv, det_end)
    fm0 = cpm.finish(full)[0]
    keys = list(dict.fromkeys(milestones or ([fm0["task_code"]] if fm0 else []) + sorted(
        d["task_code"] for d in T.values() if d["cstr_type"] and d["status_code"] != "TK_Complete")))
    for code in keys:
        if code not in cpm.code:
            raise ValueError(f"milestones: activity {code} not found in project {project}")
    mids = [cpm.code[c] for c in keys]
    rng = random.Random(seed)
    ends, crit, ms_vals = [], Counter(), {m: [] for m in mids}
    for _ in range(n_iter):
        rem = {n: rng.triangular(*dist[n][:3]) for n in sampled}
        drv: dict = {}  # entries of the recomputed activities (the others keep base_drv)
        res = cpm.forward(plan, rem, drv, (base, sub))
        end = max(fixed_end, max((res[n]["ef"] for n in own_sub), default=fixed_end))
        ends.append(end)
        crit.update(path(res, drv, end))
        for m in mids:
            ms_vals[m].append((res[m]["es"], res[m]["ef"]))
    ends.sort()
    pct = lambda k: round(100 * k / n_iter, 2)  # noqa: E731
    pcal = cpm.pcal if cpm.pcal is not None and not cpm.pcal.error else None
    p80 = _pctl(ends, 80)
    ms_rows = []
    for m in mids:
        d, v = T[m], ms_vals[m]
        fins = sorted(x[1] for x in v)
        det = base[m]["ef"]
        cal = d["cal"] if d["cal"] is not None and not d["cal"].error else None
        cdt = d["cstr_date"] if d["cstr_type"] and d["status_code"] != "TK_Complete" else None
        side = 1 if d["cstr_type"] in CSTR_FINISH else 0
        ms_rows.append({"task_code": d["task_code"], "task_name": d["task_name"], "task_type": d["task_type"],
                        "deterministic": _fmt_dt(det), **{f"p{p}": _fmt_dt(_pctl(fins, p)) for p in (10, 50, 80, 90)},
                        "on_time_pct": pct(sum(x <= det + CPM_TOL for x in fins)),
                        "constraint": _cstr_label(d["cstr_type"]) if cdt else None,
                        "constraint_date": cdt and _fmt_dt(cdt),
                        "by_constraint_pct": None if cdt is None else pct(sum(x[side] <= cdt + CPM_TOL for x in v)),
                        "p80_buffer_wd": None if cal is None else round(cal.work_days(det, _pctl(fins, 80)), 2) + 0.0})
    edges = _period_edges(ends[0], ends[-1], "week", week_day)
    hist, cum = [], 0
    for a, b in zip(edges, edges[1:]):
        k = bisect.bisect_left(ends, b) - bisect.bisect_left(ends, a)
        cum += k
        hist.append({"week_start": a.date().isoformat(), "week_end": (b - timedelta(days=1)).date().isoformat(),
                     "count": k, "pct": pct(k), "cum_pct": pct(cum)})
    rows = []
    for n, k in sorted(crit.items(), key=lambda e: (-e[1], T[e[0]]["task_code"], e[0])):
        d, hpd = T[n], T[n]["hpd"] or 8.0
        lo, hi, mo, src = dist.get(n, (None, None, None, "milestone" if d["task_type"] in ("TT_Mile", "TT_FinMile")
                                       else "duration_fixed"))
        rows.append({"task_code": d["task_code"], "task_name": d["task_name"], "task_type": d["task_type"],
                     "status_code": d["status_code"], "criticality_index": round(k / n_iter, 4),
                     "criticality_pct": pct(k), "on_deterministic_path": n in det_path,
                     "total_float_d": _days(d["total_float_hr_cnt"], hpd), "rem_dur_d": _days(d["rem"], hpd),
                     "min_d": _days(lo, hpd), "mode_d": _days(mo, hpd), "max_d": _days(hi, hpd), "range_source": src})
    must = cpm.must
    out = _slice(rows, offset, limit)
    out.update(project=project, iterations=n_iter, iterations_requested=n_req, seed=seed, engine_version=ENGINE_VERSION,
               engine={"match_rate": chk["match_rate"], "trusted": chk["match_rate"] is not None
                       and chk["match_rate"] >= CPM_TRUST, "open_mismatched": chk["counts"]["open_mismatched"],
                       "warning": None if chk["match_rate"] is not None and chk["match_rate"] >= CPM_TRUST else (
                           f"The engine matches the last F9 on {chk['match_rate']} of the open activities (needs "
                           f"{CPM_TRUST}): the simulated dates may not follow P6's; see p6_cpm_validate.")},
               distribution={"default": dict(zip(("min_pct", "mode_pct", "max_pct"), dflt)), "ranges": echo,
                             "udfs": uinfo, "counts": {"drawn": len(dist), **{s: sum(1 for x in dist.values()
                                                                                      if x[3] == s) for s in RISK_SOURCES},
                                                       "duration_fixed": fixed, "recomputed_per_iteration": len(sub)}},
               deterministic={"finish": _fmt_dt(det_end), "finish_activity": fm0 and fm0["task_code"],
                              "finish_activity_name": fm0 and fm0["task_name"], "stored_finish": cpm.prj["scd_end_date"]},
               finish={**{f"p{p}": _fmt_dt(_pctl(ends, p)) for p in (10, 50, 80, 90)}, "min": _fmt_dt(ends[0]),
                       "max": _fmt_dt(ends[-1])},
               must_finish=cpm.prj["plan_end_date"],
               on_time_probability={"deterministic_pct": pct(sum(e <= det_end + CPM_TOL for e in ends)),
                                    "must_finish_pct": None if must is None else pct(sum(e <= must + CPM_TOL for e in ends))},
               p80_buffer_wd=None if pcal is None else round(pcal.work_days(det_end, p80), 2) + 0.0,
               milestones=ms_rows, histogram=hist, caveat=RISK_CAVEAT, iterations_note=None, schedule_state=state)
    if n_iter < min(max(n_req, RISK_ITERATIONS[0]), RISK_ITERATIONS[1]):
        out["iterations_note"] = (f"{n_iter} iterations instead of {n_req}: {len(sub)} activities are recomputed per "
                                  f"iteration and a run is capped at {RISK_WORK:,} activity passes")
    elif n_iter != n_req:
        out["iterations_note"] = f"iterations clamped to {RISK_ITERATIONS[0]}..{RISK_ITERATIONS[1]}"
    return out


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_compare_projects(project: ProjectId, other: Optional[ProjectId] = None, section: CompareSection = "activities",
                        dates: BaselineDates = "current", min_variance_days: NonNegDays = 0,
                        offset: Offset = 0, limit: Limit = 50, explain: Explain = False) -> dict:
    """Compare a project with another project or baseline (other=; default: the project baseline),
    activities matched by Activity ID, links by (predecessor, successor, type, predecessor project).
    summary (whole projects): activities matched, added (only in project), removed (only in other),
    finishing later or earlier (finish = actual, else early; dates='planned' takes the other's
    planned finish), average and maximum variance in calendar days, status, original duration and
    name changes; relationships added, removed and with a changed lag; project data date and finish
    dates; slip_causes counts the matched activities per slip_cause (slip_cause_unknown: no readable
    calendar or a date missing). Then one page of section: 'activities' (added and removed first, then
    by absolute variance; matched ones only from min_variance_days calendar days; name = the project's
    activity name, the other's for a removed activity; start and finish =
    actual, else early; variance_cd in calendar days, variance_wd in working days of the activity's
    calendar; start_variance_wd + duration_delta_wd = variance_wd (start-to-finish span in working days
    of the same calendar); slip_cause late_start, longer_duration, late_start_and_longer_duration,
    on_time or earlier; total_float_before_d (other) and total_float_after_d (project), tf_delta_d =
    after - before, critical_before and critical_after: open and critical under each project's own
    definition, None where the activity is missing) or 'relationships' (added, removed, lag_changed;
    pred_name and succ_name of the project, of the other one for a removed link; lag days under each
    project's lag rule). summary.activities also counts, over the matched activities open in the project
    (LOE and WBS summary left out), float_eroded (total float lower than in other by more than 3 minutes),
    became_critical and no_longer_critical (critical = open and critical under each project's own
    definition, as p6_critical_path); explain=True adds float_definition, the same in words with both
    definitions, and the schedule_state note."""
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
    (ca, cdef_a), (cb, cdef_b) = _crit_named(con, pid, "ca", args), _crit_named(con, oid, "cb", args)
    args["tftol"] = CPM_TF_TOL_H
    acts, lnk = _cmp_acts(con, dates, (ca, cb)), _cmp_links(con)
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
        cals = _calendars(con)
        causes = Counter(_slip(cals.get(r[0]), *r[1:])[2] for r in con.execute(
            acts + "SELECT a.clndr_id, a.st, a.fin, b.st, b.fin FROM a JOIN b ON b.task_code = a.task_code", args))
        fl = con.execute(acts + "SELECT SUM(a.op AND a.tf_h < b.tf_h - :tftol), SUM(a.op AND a.crit AND NOT b.crit), "
                         "SUM(a.op AND b.crit AND NOT a.crit) FROM a JOIN b ON b.task_code = a.task_code", args).fetchone()
        lsum = con.execute(lnk + "SELECT (SELECT COUNT(*) FROM la JOIN lb ON " + LINK_KEY + "), "
                           "(SELECT COUNT(*) FROM la WHERE NOT EXISTS (SELECT 1 FROM lb WHERE " + LINK_KEY + ")), "
                           "(SELECT COUNT(*) FROM lb WHERE NOT EXISTS (SELECT 1 FROM la WHERE " + LINK_KEY + ")), "
                           "(SELECT COUNT(*) FROM la JOIN lb ON " + LINK_KEY + " WHERE COALESCE(la.lag_h, 0) <> "
                           "COALESCE(lb.lag_h, 0))", args).fetchone()
        if section == "activities":
            q = acts + ("SELECT * FROM (SELECT CASE WHEN b.task_id IS NULL THEN 'added' ELSE 'matched' END AS change, "
                        "a.task_id AS tid, a.task_code, a.task_name, b.task_name AS other_name, a.status_code, "
                        "b.status_code AS other_status, a.clndr_id, a.hpd, b.hpd AS other_hpd, a.orig_h, b.orig_h AS other_orig_h, "
                        f"a.tf_h, b.tf_h AS other_tf_h, a.st, b.st AS other_st, a.fin, b.fin AS other_fin, {var} AS v, "
                        "julianday(a.st) - julianday(b.st) AS sv, a.crit, b.crit AS other_crit FROM a LEFT JOIN b "
                        "ON b.task_code = a.task_code UNION ALL SELECT 'removed', b.task_id, b.task_code, NULL, b.task_name, "
                        "NULL, b.status_code, NULL, NULL, b.hpd, NULL, b.orig_h, NULL, b.tf_h, NULL, b.st, NULL, b.fin, NULL, "
                        "NULL, NULL, b.crit FROM b WHERE NOT EXISTS (SELECT 1 FROM a WHERE a.task_code = b.task_code)) "
                        "WHERE change <> 'matched' OR :minv <= 0 OR ABS(v) >= :minv "
                        "ORDER BY change = 'matched', ROUND(ABS(COALESCE(v, 0)), 6) DESC, task_code, tid")

            def row_fn(r: sqlite3.Row) -> dict:
                cal = cals.get(r["clndr_id"])
                a, b = _db_dt(r["fin"]), _db_dt(r["other_fin"])
                wd = round(cal.work_days(b, a), 2) if cal is not None and not cal.error and a and b else None
                sv, dv, cause = _slip(cal, r["st"], r["fin"], r["other_st"], r["other_fin"])
                return {"change": r["change"], "task_code": r["task_code"], "task_name": r["task_name"],
                        "other_task_name": r["other_name"],
                        "name": r["task_name"] if r["task_name"] is not None else r["other_name"],
                        "status": r["status_code"], "other_status": r["other_status"],
                        "start": r["st"], "other_start": r["other_st"],
                        "start_variance_cd": None if r["sv"] is None else round(r["sv"], 2) + 0.0,
                        "start_variance_wd": sv, "finish": r["fin"], "other_finish": r["other_fin"],
                        "variance_cd": None if r["v"] is None else round(r["v"], 2), "variance_wd": wd,
                        "duration_delta_wd": dv, "slip_cause": cause,
                        "orig_dur_d": _days(r["orig_h"], r["hpd"]), "other_orig_dur_d": _days(r["other_orig_h"], r["other_hpd"]),
                        "total_float_d": _days(r["tf_h"], r["hpd"]), "other_total_float_d": _days(r["other_tf_h"], r["other_hpd"]),
                        **_tf_change(_days(r["other_tf_h"], r["other_hpd"]), _days(r["tf_h"], r["hpd"]),
                                     r["other_crit"], r["crit"])}
        else:
            q = lnk + ("SELECT * FROM (SELECT CASE WHEN lb.rid IS NULL THEN 'added' ELSE 'lag_changed' END AS change, "
                       "la.rid, la.pred, la.succ, la.pred_name, la.succ_name, la.type, la.pred_project, la.lag_h, "
                       "lb.lag_h AS other_lag_h, la.pred_hpd, "
                       "la.succ_hpd, lb.pred_hpd AS other_pred_hpd, lb.succ_hpd AS other_succ_hpd FROM la LEFT JOIN lb ON "
                       + LINK_KEY + " WHERE lb.rid IS NULL OR COALESCE(la.lag_h, 0) <> COALESCE(lb.lag_h, 0) UNION ALL "
                       "SELECT 'removed', lb.rid, lb.pred, lb.succ, lb.pred_name, lb.succ_name, lb.type, lb.pred_project, "
                       "NULL, lb.lag_h, NULL, NULL, "
                       "lb.pred_hpd, lb.succ_hpd FROM lb WHERE NOT EXISTS (SELECT 1 FROM la WHERE " + LINK_KEY + ")) "
                       "ORDER BY change, succ, pred, type, rid")

            def lag_d(h, ph, sh, p: int):
                return None if ph is None else _days(h or 0, _lag_hpd(con, {"hpd": ph}, {"hpd": sh}, p, rule[p])[0])

            def row_fn(r: sqlite3.Row) -> dict:
                return {"change": r["change"], "pred_code": r["pred"], "pred_name": r["pred_name"],
                        "succ_code": r["succ"], "succ_name": r["succ_name"],
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
                                      "orig_dur_changed": s[9] or 0, "name_changed": s[10] or 0,
                                      "slip_causes": {k: causes.get(k, 0) for k in SLIP_CAUSES},
                                      "slip_cause_unknown": causes.get(None, 0), "float_eroded": fl[0] or 0,
                                      "became_critical": fl[1] or 0, "no_longer_critical": fl[2] or 0,
                                      **({"float_definition": (
                                          "over the matched activities open in the project (LOE and WBS summary left "
                                          "out): float_eroded = total float lower than in the other one by more than "
                                          f"{CPM_TF_TOL_H * 60:g} minutes; critical = open and critical under each "
                                          f"project's own definition (project: {cdef_a}; other: {cdef_b})")}
                                         if explain else {})},
                       "relationships": {"matched": lsum[0], "added": lsum[1], "removed": lsum[2], "lag_changed": lsum[3]}},
           "projects": {k: {"project": pa[k], "other": pb[k]} for k in pf},
           "basis": ("finish = actual, else early finish; other: " + ("same" if dates == "current" else "planned finish")
                     + "; variance = finish - other finish (positive = later)"),
           "schedule_state": _schedule_state(con, pid, explain)}
    return res


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_update_integrity(project: ProjectId, other: ProjectId, flag: Optional[IntegrityFlag] = None,
                        near_critical_days: NonNegDays = 10, sample_size: SampleSize = 10,
                        offset: Offset = 0, limit: Limit = 100, explain: Explain = False) -> dict:
    """Integrity review of a schedule update against the previous update (other = its Project ID): the changes
    reviewers question. The flags are questions to raise with whoever submitted the update, not proof of
    manipulation (caveat says so). Activities are matched by Activity ID and links by (predecessor,
    successor, type, predecessor project), as p6_compare_projects; before = other, after = project; open = not
    completed, LOE and WBS summary left out; critical = each project's own definition. flags (each {count,
    sample: first sample_size rows with before / after values}; explain=True adds definition, the flag in
    words, here and in result, and the schedule_state note): open_end_to_finish,
    negative_float_cleared_by_constraint, must_finish_moved, actuals_changed, duration_cut (with critical:
    those critical before), links_removed_on_critical (successor critical or within near_critical_days of total
    float before), rel_type_or_lag_changed, schedule_options_changed, calendars_changed,
    deleted_in_progress_or_critical, data_date (raised when it did not advance). summary: count per flag;
    raised: the flags with a count; data_date {before, after, delta_cd, advanced}; must_finish {before,
    after, delta_cd, delta_wd (working days of the project calendar)}. flag= returns that flag's rows as a page {total, offset, returned, next_offset, rows} with result
    {count}. DCMA counts before and after: p6_health_check on both projects, then
    p6_health_check_compare(project, before=<snapshot id of other>, after=<snapshot id of project>). Values of
    each project's last F9; read only."""
    if flag is not None and flag not in INTEGRITY_FLAGS:
        raise ValueError(f"flag must be one of {list(INTEGRITY_FLAGS)}")
    near = _num(near_critical_days)
    if near is None or near < 0:
        raise ValueError("near_critical_days must be 0 or more")
    size = int(sample_size)
    if not 0 <= size <= 50:
        raise ValueError("sample_size must be 0 to 50")
    offset, limit = _check_page(offset, limit)
    con = _connect()
    pid, oid = _project_id(con, project), _project_id(con, other)
    if pid == oid:
        raise ValueError("other must be another project: the previous update of this one")
    con.execute("BEGIN")  # both projects from one snapshot
    try:
        rows, facts = _integrity(con, pid, oid, near)
    finally:
        con.rollback()
    state = _schedule_state(con, pid, explain)
    head = {"project": project, "other": other}
    words = (lambda k: {"definition": INTEGRITY_DEFS[k]}) if explain else (lambda k: {})  # noqa: E731
    if flag:
        return {**head, "flag": flag, "result": {"count": len(rows[flag]), **words(flag)},
                **_slice(rows[flag], offset, limit), "caveat": INTEGRITY_CAVEAT, "schedule_state": state}
    flags = {k: {"count": len(v), **words(k), "sample": v[:size]} for k, v in rows.items()}
    flags["duration_cut"]["critical"] = sum(r["critical_before"] for r in rows["duration_cut"])
    summary = {k: f["count"] for k, f in flags.items()}
    raised = [k for k, n in summary.items() if n]
    return {**head, "data_date": facts["data_date"], "summary": summary, "raised": raised, "raised_count": len(raised),
            "flags": flags, "must_finish": facts["must_finish"], "critical_definition": facts["critical_definition"],
            "near_critical_days": near, "caveat": INTEGRITY_CAVEAT, "schedule_state": state}


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_lookahead(project: ProjectId, days: Optional[WindowDays] = None, weeks: Optional[WindowWeeks] = None,
                 from_date: Optional[DateTimeStr] = None, include_in_progress: bool = True,
                 include_completed: bool = False, wbs_name_contains: Optional[str] = None,
                 code_type: Optional[str] = None, code_value: Optional[str] = None, include_child_codes: bool = True,
                 critical_only: bool = False, fields: Optional[ActFields] = None, offset: Offset = 0,
                 limit: Limit = 50, detail: DetailArg = "full") -> dict:
    """Look-ahead of a project, one page: {total, offset, returned, next_offset, rows, window, weeks,
    data_date, counts, critical_definition, schedule_state, note}. The window starts at the data date (or from_date; a
    date alone starts at 00:00), lasts days= (default 14) or weeks= calendar days and is half-open
    [from, to). Listed: not started activities starting in it, open ones finishing in it, not
    started ones spanning it (start before, finish after), every in-progress activity
    (include_in_progress=False keeps those finishing in the window) and, with include_completed=True,
    completed ones whose actual dates overlap it. Open activities use their remaining dates (early
    when empty) of the last F9. LOE and WBS summary activities are left out. Each row has window: the
    categories it meets (in_progress, starting, finishing, spanning, completed), critical (open and
    critical under the project definition, as p6_critical_path), fcst_start (actual start, else remaining
    start), fcst_finish (actual finish when completed, else remaining finish) and start_week /
    finish_week: the week of the window holding that date (1 = the first 7 days from the window start,
    None outside the window; weeks lists each one's from and to). critical_only=True lists the critical
    ones only. counts gives the categories and critical over all pages. wbs_name_contains, code_type / code_value / include_child_codes, fields=
    and detail= as in p6_query_activities. Order: start, finish, Activity ID."""
    if days is not None and weeks is not None:
        raise ValueError("give days or weeks, not both")
    n = int(days) if days is not None else 7 * int(weeks) if weeks is not None else LOOKAHEAD_DAYS
    if not 1 <= n <= 366:
        raise ValueError("the window must be 1 to 366 days (days=, or weeks= up to 52)")
    if from_date:
        _parse_dt(from_date, "from_date")
    row_fn, brief = _act_row(fields), _brief_on(detail)
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # data date, code filter, page and counts from one snapshot
    try:
        la = _la_query(con, pid, project, n, from_date, include_in_progress, include_completed, wbs_name_contains,
                       code_type, code_value, include_child_codes, critical_only)

        def row(r: sqlite3.Row) -> dict:
            out = {**row_fn(r), **la["extra"](r)}
            return _brief(out) if brief else out
        res = _page(con, la["sql"], la["args"], offset, limit, row)
        counts = la["counts"]()
    finally:
        con.rollback()
    res.update(window=la["window"], weeks=la["weeks"], data_date=la["data_date"], counts=counts,
               critical_definition=la["critical_definition"])
    if la["code_filter"]:
        res["code_filter"] = la["code_filter"]
    res.update(schedule_state=_schedule_state(con, pid),
               note="Dates of the last F9. The data date has a time (e.g. 18:00): pass from_date for whole days.")
    return res


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_progress_curve(project: ProjectId, period: Period = "month", baseline: Optional[ProjectId] = None,
                      measure: Measure = "units", resource: Optional[str] = None, from_date: Optional[DateStr] = None,
                      to_date: Optional[DateStr] = None, updates: Optional[ProjectIds] = None) -> dict:
    """Progress S-curve per month or week in the schedule's own weights: measure 'units' (labor and
    nonlabor assignments, the default) or 'cost' (every resource); resource= one resource ID only.
    periods: per period and cumulative values with % of the budget for baseline (baseline= Project
    ID, default the project baseline; its planned dates), planned (the project's current planned
    dates), actual (actual quantities, up to the data date) and forecast (actuals, then remaining
    from the data date); period_count = the number of periods. to_date: exact values at the data date:
    baseline planned (planned value),
    planned, actual and actual %, earned and earned % (earned value: budget x performance % complete
    by the earned value technique of the activity's WBS node: activity % complete by its percent
    complete type, 0/100, 50/50, custom %, WBS milestones or resource curves), variance
    (earned - baseline planned), spi (earned / baseline planned) and spi_actual (actual / baseline
    planned, the units-based index); with measure='cost', to_date.cost = the cost earned value block of
    p6_project_summary (expenses included; resource= that resource only), else null. updates= earlier updates of
    the same schedule (Project IDs): their earned and actual totals at their own data date
    (earned_points). from_date / to_date (YYYY-MM-DD) trim the periods; cumulatives stay exact.
    checks: totals reached by the last period, actuals clipped at the data date, remaining starts
    moved to it, quantities without dates, WBS nodes per earned value technique, spread (spans
    over calendar time, resource curves applied or unknown, period actuals used). Periods spread
    each assignment over the working time of its calendar and on its resource curve; see note."""
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
        evm = _ev_map(con, pid)
        sp = _Spread(con, [pid] + ([bid] if bid is not None else []), measure)
        cur = _curve_items(_measure_rows(con, pid, measure, resource), dd, evm, sp)
        spread = dict(sp.counts)  # the project's own assignments
        base = bdd = bname = None
        if bid is not None:
            bdd, bname = dd_of(bid), con.execute("SELECT proj_short_name FROM PROJECT WHERE proj_id = ?", (bid,)).fetchone()[0]
            base = _curve_items(_measure_rows(con, bid, measure, resource), bdd or dd, _ev_map(con, bid), sp)
        pts = [(u, dd_of(p), _curve_items(_measure_rows(con, p, measure, resource), dd_of(p) or dd, _ev_map(con, p)))
               for u, p in upd]
        week_day = _prefer(con, "week_start_day_num") or 1
        fin = {t: con.execute(f"SELECT COUNT(*) FROM {t} WHERE proj_id = ?" + _alive(con, t), (pid,)).fetchone()[0]
               if "proj_id" in _cols(con, t) else 0 for t in ("TRSRCFIN", "TASKFIN")}
        spread["trsrcfin"] = fin["TRSRCFIN"]
        costb = _cost_ev(con, pid, bid, dd, resource) if measure == "cost" else None
        ev_other = 0  # WBS nodes whose stored technique is not EC_Cmp_pct (activity percent complete)
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
    dates = [d for v in curves.values() for it in v for d in it[1:3]]
    if not dates:
        raise ValueError(f"no assignment of {project} has dates")
    edges = _period_edges(lo or min(dates), hi or max(dates), period, week_day)
    cums = {k: _cum(v, edges) for k, v in curves.items()}
    budget = {k: base["budget"] if k == "baseline" else cur["budget"] for k in curves}
    r2 = lambda v: None if v is None else round(v, 2)  # noqa: E731
    periods = []
    for i, a in enumerate(edges[:-1]):
        row = {"period": f"{a:%Y-%m}" if period == "month" else f"{a:%Y-%m-%d}", "start": f"{a:%Y-%m-%d}",
               **({} if base else {"baseline": None, "baseline_cum": None, "baseline_pct": None})}
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
    note = (_spread_note(spread) + " Exact: the totals and every value at the data date; to_date keeps the planned value "
            "of the planned dates linear over calendar time (baseline_planned, planned, variance, spi: the definition used "
            "before 3.6.0), so it can differ a little from the period curve at the data date. Earned value (budget x "
            "activity % complete) exists at a data date only: to_date, and earned_points of earlier updates (updates=). "
            "Planned uses the current "
            "project planned dates, which move with the schedule for unstarted work; use a baseline for a true plan."
            + ("" if base else " No baseline: pass baseline= or set a project baseline in P6.")
            + (f" {ev_other} WBS nodes use another earned value technique than activity percent complete: earned "
               "applies each node's technique (rules of the P6 documentation, not checked against P6's own numbers)."
               if ev_other else "")
            + (f" Unknown earned value technique(s) {evm['unknown']}: counted as activity percent complete."
               if evm["unknown"] else ""))
    return {"project": project, "data_date": _fmt_dt(dd), "period": period,
            "measure": {"kind": measure, "resources": what, "budget": r2(cur["budget"]), "actual": r2(act),
                        "remaining": r2(cur["remain_qty"]), "at_completion": r2(act + cur["remain_qty"])},
            "baseline": base and {"project": bname, "source": "baseline=" if baseline else "project baseline "
                                  "(PROJECT.sum_base_proj_id)", "data_date": bdd and _fmt_dt(bdd), "budget": r2(base["budget"])},
            "to_date": {"baseline_planned": r2(bp), "planned": r2(_value_at(cur["planned"], dd)), "actual": r2(act),
                        "actual_pct": r2(100 * act / cur["budget"]), "earned": r2(ev), "earned_pct": r2(100 * ev / cur["budget"]),
                        "variance": r2(None if bp is None else ev - bp), "spi": round(ev / bp, 4) if bp else None,
                        "spi_actual": round(act / bp, 4) if bp else None, "cost": costb},
            "periods": periods, "period_count": len(periods),
            "earned_points": sorted([point(project, dd, cur)] + [point(u, d, c) for u, d, c in pts],
                                    key=lambda p: p["data_date"] or ""),
            "checks": {"totals_reached": reached, "clipped_actuals": cur["clipped"], "clamped_remaining": cur["clamped"],
                       "unspread_planned": r2(cur["unspread"]), "trsrcfin_rows": fin["TRSRCFIN"], "taskfin_rows": fin["TASKFIN"],
                       "spread": spread,
                       "wbs_other_ev_technique": ev_other, "ev_techniques": evm["nodes"],
                       "ev_unknown_techniques": evm["unknown"]},
            "schedule_state": _schedule_state(con, pid), "note": note}


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_milestones(project: ProjectId, baseline: Optional[ProjectId] = None, baseline_dates: BaselineDates = "current",
                  open_only: bool = False, order: MsOrder = "date", codes: Optional[MsCodes] = None,
                  offset: Offset = 0, limit: Limit = 50, detail: DetailArg = "full") -> dict:
    """Milestones of a project (TT_Mile start milestones, TT_FinMile finish milestones), one page by date
    then Activity ID (order='variance': the largest variance in working days first, calendar days when the
    calendar is unreadable, milestones without a baseline date last, then date): {total, offset, returned, next_offset, rows, counts, baseline, must_finish,
    critical_definition, schedule_state, note}. A milestone has one date: the start of a start milestone,
    the finish of a finish milestone, actual else early (date_kind actual or forecast). Row: task_code,
    task_name, task_type, status_code, wbs_name, date, date_kind, baseline_date (the baseline activity with
    the same Activity ID, same date rule; baseline_dates='planned' takes its planned date; None when it is
    not in the baseline), variance_cd (date - baseline date, calendar days, positive = later), variance_wd
    (working days of the milestone's calendar; None when it is unreadable), vs_baseline (late, early or
    on_time within 5 minutes of working time; None without a baseline date), total_float_d (days of its
    calendar; None when completed), critical (open and critical under the project definition, as
    p6_critical_path), constraint cstr_type / cstr_label / cstr_date. counts over every milestone listed:
    milestones, completed, open, critical, late, early, on_time, no_baseline, open_late. baseline= a
    Project ID (default: the project baseline). open_only=True leaves completed milestones out.
    codes= lists these Activity IDs instead of every milestone (any activity type: a task takes its
    finish as its date), and not_found gives the IDs the project does not have. Every row also has
    hard_constraint (its primary or secondary constraint is hard: start or finish on, on or before,
    mandatory), predecessor_count (links into it), vs_constraint_wd (its date minus its constraint date in
    working days of its calendar, positive = later; None without a constraint date), float_origin_type and
    float_origin_explanation (where its total float comes from, as p6_float_origin; None when completed).
    detail='brief' leaves the null values out of the rows."""
    brief = _brief_on(detail)
    if baseline_dates not in get_args(BaselineDates):
        raise ValueError(f"baseline_dates must be one of {list(get_args(BaselineDates))}")
    if order not in get_args(MsOrder):
        raise ValueError(f"order must be one of {list(get_args(MsOrder))}")
    wanted = None if codes is None else _code_list(codes, "codes")
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # milestones, baseline, settings and the network from one snapshot
    try:
        bid = _baseline_pid(con, pid, baseline)
        crit, cargs, cdef = _critical_clause(con, pid, "project", None)[:3]
        sel = (f" AND t.task_code IN ({', '.join('?' * len(wanted))})" if wanted
               else " AND t.task_type IN ('TT_Mile', 'TT_FinMile')")
        q = (_act_from(con).replace("SELECT t.*,", f"SELECT t.*, (t.status_code <> 'TK_Complete' AND "
                                    f"COALESCE({crit}, 0)) AS is_crit,", 1)
             + sel + (" AND t.status_code <> 'TK_Complete'" if open_only else ""))
        ms = [_dict(r) for r in con.execute(q + " ORDER BY t.task_id", [*cargs, pid, *(wanted or [])])]
        net = _Net(con, pid)
        bl: dict[str, dict] = {}
        if bid is not None:
            for r in con.execute("SELECT task_code, act_start_date, act_end_date, early_start_date, early_end_date, "
                                 "target_start_date, target_end_date FROM TASK WHERE proj_id = ?" + _alive(con, "TASK")
                                 + " ORDER BY task_id DESC", (bid,)):
                bl[r["task_code"]] = _dict(r)  # the lowest task_id wins
        bname = bid and con.execute("SELECT proj_short_name FROM PROJECT WHERE proj_id = ?", (bid,)).fetchone()[0]
        must = (con.execute("SELECT plan_end_date FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone()[0]
                if "plan_end_date" in _cols(con, "PROJECT") else None)
        cals = _calendars(con)
    finally:
        con.rollback()

    def one(m: Mapping, task_type: str, planned: bool = False) -> tuple[Optional[str], Optional[str]]:
        k = "start" if task_type == "TT_Mile" else "end"
        if planned:
            return m.get(f"target_{k}_date"), "planned"
        return (m[f"act_{k}_date"], "actual") if m.get(f"act_{k}_date") else (m.get(f"early_{k}_date"), "forecast")
    rows = []
    for m in ms:
        date, kind = one(m, m["task_type"])
        b = bl.get(m["task_code"])
        bdate = one(b, m["task_type"], baseline_dates == "planned")[0] if b else None
        cal = cals.get(m["clndr_id"])
        ok = cal is not None and not cal.error
        d, bd = _db_dt(date), _db_dt(bdate)
        vs = vcd = vwd = None
        if d and bd:
            vcd = round((d - bd).total_seconds() / 86400, 2) + 0.0
            wh = cal.work_hours(bd, d) if ok else (d - bd).total_seconds() / 3600
            vwd = round(wh / cal.hpd, 2) + 0.0 if ok else None
            vs = "late" if wh > GAP_TOL_H else "early" if wh < -GAP_TOL_H else "on_time"
        done = m["status_code"] == "TK_Complete"
        cd = _db_dt(m.get("cstr_date"))
        vcw = None if not (d and cd and ok) else round(cal.work_hours(cd, d) / cal.hpd, 2) + 0.0
        nd = net.t[m["task_id"]]
        fo = None if done or nd["lf"] is None else _float_walk(net, nd)
        rows.append({"task_code": m["task_code"], "task_name": m["task_name"], "task_type": m["task_type"],
                     "status_code": m["status_code"], "wbs_name": m.get("wbs_name"), "date": date, "date_kind": kind,
                     "baseline_date": bdate, "variance_cd": vcd, "variance_wd": vwd, "vs_baseline": vs,
                     "total_float_d": None if done else _days(m.get("total_float_hr_cnt"), m.get("hpd")),
                     "critical": bool(m["is_crit"]), "cstr_type": m.get("cstr_type"),
                     "cstr_label": _cstr_label(m.get("cstr_type")), "cstr_date": m.get("cstr_date"),
                     "hard_constraint": bool({m.get("cstr_type"), m.get("cstr_type2")} & HARD_CSTR_SET),
                     "predecessor_count": len(net.by_succ.get(m["task_id"], ())), "vs_constraint_wd": vcw,
                     "float_origin_type": fo and fo["origin"]["type"], "float_origin_explanation": fo and fo["explanation"],
                     "_tid": m["task_id"]})
    rows.sort(key=lambda r: (_db_dt(r["date"]) or datetime.max, r["task_code"], r["_tid"]))
    if order == "variance":  # stable: equal variances keep the date order
        rows.sort(key=lambda r: (r["variance_cd"] is None, -(r["variance_wd"] if r["variance_wd"] is not None
                                                              else r["variance_cd"] or 0)))
    for r in rows:
        del r["_tid"]
    n_open = sum(r["status_code"] != "TK_Complete" for r in rows)
    counts = {"milestones": len(rows), "completed": len(rows) - n_open, "open": n_open,
              "critical": sum(r["critical"] for r in rows), **{k: sum(r["vs_baseline"] == k for r in rows) for k in MS_VS},
              "no_baseline": sum(r["vs_baseline"] is None for r in rows),
              "open_late": sum(r["vs_baseline"] == "late" and r["status_code"] != "TK_Complete" for r in rows)}
    res = _slice(rows, offset, limit)
    if brief:
        res["rows"] = [_brief(r) for r in res["rows"]]
    found = {m["task_code"] for m in ms}
    if wanted is not None:  # an open_only call leaves a completed ID out of not_found too
        found |= {c for c in wanted if c in net.code}
    res.update(counts=counts, baseline=bname and {"project": bname, "dates": baseline_dates,
                                                    "source": "baseline=" if baseline else
                                                    "project baseline (PROJECT.sum_base_proj_id)"},
               not_found=[] if wanted is None else [c for c in wanted if c not in found],
               must_finish=must, critical_definition=cdef + "; open activities", schedule_state=_schedule_state(con, pid),
               note="Dates of the last F9 (actual, else early). Variance = date - baseline date: positive = later."
                    + ("" if bid is not None else " No baseline: pass baseline= or set a project baseline in P6."))
    return res


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_project_summary(project: ProjectId, baseline: Optional[ProjectId] = None) -> dict:
    """One-call overview of a project (values of the last F9): project (name, calendar), dates (data
    date, planned start, must finish by, scheduled finish, last schedule run, first start and last
    finish of the activities, scheduled finish minus must finish by in calendar days and in working days
    of the project calendar (None when it is unreadable)), activities (by
    status, by type, open, open milestones), relationships (successor in the project; from and to
    other projects), critical (counts of open activities, LOE and WBS summary left out, as
    p6_critical_path), percent_complete (primary = units: actual / at completion units, labor plus
    nonlabor, P6 Units % Complete as p6_list_wbs; p6_progress_curve gives % of the budget instead;
    physical: physical % weighted by budget units; duration; activities completed;
    units_vs_physical_mismatch: activities with units whose physical % and units % differ by more
    than 0.01 point), baseline (the project baseline, baselines attached, the one used: baseline=
    else the project baseline, its scheduled finish and data date; every key present, null without a
    baseline; finish variance = scheduled finish minus the baseline's scheduled finish in
    calendar days and in working days of the project calendar, baseline planned units % at the
    data date and spi_units = earned units / baseline planned units, earned value and linear spread
    as p6_progress_curve), cost (earned value in cost at the data date, P6's earned value fields:
    BAC = budgeted cost of every assignment plus expenses, PV = baseline planned cost at the data date,
    EV = budgeted cost x earned % of each activity's WBS technique, AC = actual cost plus actual
    expenses, CV, SV, CPI, SPI, ETC, EAC by the project's estimate to complete technique (eac_method)
    with the standard variants in eac_variants, VAC, TCPI; by_source: labor, nonlabor, material, other,
    expenses; every key present, null without costs, PV, SV and SPI null without a baseline), counts
    (WBS, assignments, resources, calendars, code assignments) and schedule_state."""
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # settings and every figure from one snapshot
    try:
        return _summary(con, pid, project, baseline)
    finally:
        con.rollback()


def _trend_names(projects, eps: Optional[str]) -> list[str]:
    """Project IDs of a trend: projects= as given, or the projects of the EPS node eps and of the EPS below it
    (baselines and templates left out, as p6_list_projects)."""
    if (projects is None) == (eps is None):
        raise ValueError("Give projects= (the Project IDs of the updates) or eps= (an EPS ID), one of them")
    if eps is not None:
        names = [r["proj_short_name"] for r in _all_rows(p6_list_projects, TREND_MAX + 1, eps=eps,
                                                         exclude_templates=True)[1]]
        if len(names) < 2:
            raise ValueError(f"EPS {eps} holds {len(names)} project(s) (baselines and templates left out): a trend "
                             "needs at least two updates")
        return names
    if isinstance(projects, str) or not isinstance(projects, (list, tuple)):
        raise ValueError("projects must be a list of Project IDs")
    return [str(p).strip() for p in projects]


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_trend(projects: Optional[TrendProjects] = None, eps: Optional[str] = None, baseline: Optional[ProjectId] = None,
             codes: Optional[MsCodes] = None, milestones: TrendMs = "matched", near_critical_days: NonNegDays = 10,
             dcma: bool = True, offset: Offset = 0, limit: Limit = 20) -> dict:
    """Trend across the schedule updates of one job, one row per update ordered by data date (then last F9,
    then Project ID): projects= the Project IDs of the updates (baselines allowed), or eps= an EPS ID (every
    project of that EPS node and of the EPS below it, baselines and templates left out); at least two
    updates. One page (default 20 updates) {total, offset, returned, next_offset, rows, series, milestones,
    near_critical_days, engine_version, notes}. Values of the
    last F9 of each update, every figure of an update from one read snapshot. Row: update (1 = earliest),
    project, name, data_date, last_schedule_date, stale (this server changed it after its F9), finish
    (scheduled finish), must_finish, finish_vs_must_finish_wd, finish_variance_first_wd / _cd (against the
    first update), finish_variance_previous_wd / _cd (against the previous one; None on the first): working
    days of the update's project calendar (None when unreadable) and calendar days, positive = later;
    baseline (the update's project baseline, or baseline= for every update), baseline_finish,
    baseline_finish_variance_wd, units_pct (actual / at completion units), planned_units_pct (baseline
    planned units % at the data date) and spi_units, as p6_project_summary; critical (project definition),
    near_critical (open, not critical, total float at most near_critical_days days of its calendar),
    negative_float, total_float of open activities (LOE and WBS summary left out) {open, with_float, min_d,
    max_d, mean_d, median_d}; dcma {points, grade, failed, bei, bei_pass} as p6_health_check with its
    defaults (no snapshot saved; dcma=False skips it, the slowest part); milestones: one item per milestone
    followed {task_code, date (actual else early; start of a start milestone, else finish), date_kind,
    status_code, slip_wd (against the previous update), variance_first_wd (against the first)}, working days
    of the milestone's calendar in this update, None where the milestone is missing; changes since the
    previous update {since, activities_added, activities_removed, links_added, links_removed, durations_cut}
    (matched as p6_compare_projects; durations cut as p6_update_integrity), None on the first. Milestones
    followed (sibling milestones: task_code, task_name, task_type, updates = how many hold it): codes= these
    Activity IDs (any activity type: a task takes its finish), else milestones='matched' (default: TT_Mile
    and TT_FinMile in every update), 'any' (in at least one) or 'none'; at most 20 without codes=, by date in
    the latest update."""
    if milestones not in get_args(TrendMs):
        raise ValueError(f"milestones must be one of {list(get_args(TrendMs))}")
    near = _num(near_critical_days)
    if near is None or not math.isfinite(near) or near < 0:
        raise ValueError("near_critical_days must be 0 or more")
    offset, limit = _check_page(offset, limit)
    wanted = None if codes is None else _code_list(codes, "codes")
    series = _trend_series(_trend_names(projects, eps))
    t = _trend(series, baseline, wanted, milestones, near, bool(dcma), range(offset, offset + limit))
    return {**_env(len(series), offset, t["rows"]),
            "series": {"source": "eps=" if eps is not None else "projects=", "eps": eps, "updates": len(series),
                       "projects": [u["project"] for u in series], "first_data_date": series[0]["data_date"],
                       "last_data_date": series[-1]["data_date"]},
            "milestones": t["milestones"], "near_critical_days": near,
            "engine_version": ENGINE_VERSION if dcma else None, "notes": t["notes"]}


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
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
                    "not time-phased (p6_resource_loading spreads them per period against max units). Quantity totals "
                    "cover labor and nonlabor only: material quantities stay in their own rows and unit. Values of the "
                    "last F9 and progress update.")
    return res


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_resource_loading(project: ProjectId, resource: Optional[str] = None, role: Optional[str] = None,
                        period: Period = "month", from_date: Optional[DateStr] = None, to_date: Optional[DateStr] = None,
                        offset: Offset = 0, limit: Limit = 100) -> dict:
    """Time-phased resource loading of a project, one page: {total, offset, returned, next_offset, rows,
    periods, resources, counts, data_date, schedule_state, note}. One row per resource and month or week
    (period=), ordered by resource (labor and nonlabor first, then material, each by Resource ID) then
    period start, from the resource's first to its last loaded period: budget (budgeted units on the
    planned dates), actual (up to the data date; null for periods after it), remaining (from the remaining
    dates), load (actual + remaining), available (working hours of the resource's calendar in the period x
    its max units per hour by effective date, RSRCRATE), load_pct, over (load above available) and over_by.
    Quantities are spread over the working time of each assignment's calendar and on its resource curve,
    as p6_progress_curve. resources: per resource kind, unit, calendar, max units per hour at the data date,
    totals (budget, actual, remaining, at completion: the quantities of p6_resource_summary), peak period
    and over-allocated periods. Material quantities are in their own unit and not compared with a limit.
    resource= one Resource ID, role= the assignments of one Role ID; from_date / to_date (YYYY-MM-DD) trim
    the periods. counts: over-allocated periods and resources over (labor and nonlabor), resources without
    a max units limit."""
    if period not in get_args(Period):
        raise ValueError(f"period must be one of {list(get_args(Period))}")
    lo = _parse_dt(from_date, "from_date")[0] if from_date else None
    hi = _parse_dt(to_date, "to_date")[0] if to_date else None
    if lo and hi and hi < lo:
        raise ValueError("to_date is before from_date")
    offset, limit = _check_page(offset, limit)
    con = _connect()
    pid = _project_id(con, project)
    if resource and not con.execute("SELECT 1 FROM RSRC WHERE rsrc_short_name = ?" + _alive(con, "RSRC"),
                                    (resource,)).fetchone():
        raise ValueError(f"Resource not found: {resource}")
    if role is not None and not (_cols(con, "ROLES") and con.execute(
            "SELECT 1 FROM ROLES WHERE role_short_name = ?" + _alive(con, "ROLES"), (role,)).fetchone()):
        raise ValueError(f"Role not found: {role}")
    con.execute("BEGIN")  # assignments, calendars, curves, period actuals and limits from one snapshot
    try:
        dd = (_db_dt(con.execute("SELECT last_recalc_date FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone()[0])
              if "last_recalc_date" in _cols(con, "PROJECT") else None)
        if dd is None:
            raise ValueError(f"{project} has no data date (PROJECT.last_recalc_date)")
        rows = _measure_rows(con, pid, "units", resource, every_type=True, role=role)
        sp = _Spread(con, [pid], "units")
        rcols = _cols(con, "RSRC")
        unit_sql = ("(SELECT u.unit_abbrev FROM UMEASURE u WHERE u.unit_id = r.unit_id" + _alive(con, "UMEASURE", "u")
                    + ")") if {"unit_id", "unit_abbrev"} <= _cols(con, "UMEASURE") and "unit_id" in rcols else "NULL"
        ids = sorted({r["rsrc_id"] for r in rows if r["rsrc_id"] is not None})
        info = {x["rsrc_id"]: x for x in con.execute(
            f"SELECT r.rsrc_id, r.rsrc_short_name, r.rsrc_name, r.rsrc_type, {unit_sql} AS unit, "
            + ("r.clndr_id" if "clndr_id" in rcols else "NULL") + " AS clndr_id FROM RSRC r WHERE r.rsrc_id IN ("
            + ",".join("?" * len(ids)) + ")", ids)} if ids else {}
        limits = _rsrc_limits(con, ids)
        week_day = _prefer(con, "week_start_day_num") or 1
        fin_rows = (con.execute("SELECT COUNT(*) FROM TRSRCFIN WHERE proj_id = ?" + _alive(con, "TRSRCFIN"), (pid,))
                    .fetchone()[0] if "proj_id" in _cols(con, "TRSRCFIN") else 0)
    finally:
        con.rollback()
    groups: dict[Optional[int], list] = {}
    for r in rows:
        groups.setdefault(r["rsrc_id"], []).append(r)
    items = {rid: _curve_items(g, dd, None, sp) for rid, g in groups.items()}
    dates = [d for c in items.values() for k in ("planned", "actual", "remaining") for it in c[k] for d in it[1:3]]
    edges = _period_edges(lo or min(dates), hi or max(dates), period, week_day) if dates else []  # none: no assignment
    r2 = lambda v: None if v is None else round(v, 2)  # noqa: E731

    def meta(rid: Optional[int]) -> dict:
        x = info.get(rid)
        typ = (x and x["rsrc_type"]) or groups[rid][0]["rsrc_type"]
        kind = RSRC_KINDS.get(typ, "other")
        return {"rsrc_id": rid, "resource": x["rsrc_short_name"] if x else "(no resource)",
                "name": x["rsrc_name"] if x else None, "type": typ, "kind": kind,
                "unit": (x["unit"] if x else None) or ("h" if kind in ("labor", "nonlabor") else None),
                "cal": sp.cals.get(x["clndr_id"]) if x and x["clndr_id"] is not None else None}
    res = sorted((meta(rid) for rid in groups), key=lambda m: (LOAD_KIND_ORDER.get(m["kind"], 2), m["rsrc_id"] is None,
                                                               m["resource"], m["rsrc_id"] or 0))
    out_rows, resources, no_limit = [], [], []
    over_periods = resources_over = 0
    for m in res:
        rid, c, cal = m["rsrc_id"], items[m["rsrc_id"]], m["cal"]
        cums = {k: _cum(c[k], edges) for k in ("planned", "actual", "remaining")}
        segs, src = limits.get(rid, (None, None)) if m["kind"] in ("labor", "nonlabor") else (None, None)
        cap = cal is not None and not cal.error and bool(segs)
        if m["kind"] in ("labor", "nonlabor") and not cap:
            no_limit.append(m["resource"])
        per = [{k: cums[k][i + 1] - cums[k][i] for k in cums} for i in range(max(0, len(edges) - 1))]
        used = [i for i, p in enumerate(per) if any(abs(v) > 1e-9 for v in p.values())]
        mine, peak, n_over = [], None, 0
        for i in range(used[0], used[-1] + 1) if used else ():
            a, b, p = edges[i], edges[i + 1], per[i]
            act = p["actual"] if a <= dd else None
            load = (act or 0.0) + p["remaining"]
            avail = _available(cal, segs, a, b) if cap else None
            over = None if avail is None else r2(load) > r2(avail)
            n_over += bool(over)
            row = {"resource": m["resource"], "name": m["name"], "kind": m["kind"], "unit": m["unit"],
                   "period": f"{a:%Y-%m}" if period == "month" else f"{a:%Y-%m-%d}", "start": f"{a:%Y-%m-%d}",
                   "end": f"{b:%Y-%m-%d}", "budget": r2(p["planned"]), "actual": r2(act), "remaining": r2(p["remaining"]),
                   "load": r2(load), "available": r2(avail),
                   "load_pct": r2(100 * load / avail) if avail else None, "over": over,
                   "over_by": r2(load - avail) if over else (None if over is None else 0.0)}
            mine.append(row)
            if peak is None or load > peak["load"] + 1e-9:
                peak = {"period": row["period"], "load": load, "available": row["available"], "load_pct": row["load_pct"]}
        out_rows += mine
        over_periods += n_over
        resources_over += n_over > 0
        now = next((mq for d, mq in reversed(segs) if d is None or d <= dd), segs[0][1]) if segs else None
        resources.append({
            "resource": m["resource"], "name": m["name"], "type": m["type"], "kind": m["kind"], "unit": m["unit"],
            "calendar": cal.name if cal else None, "max_units_per_hour": now,
            "limit_source": src if cap else None, "assignments": len(groups[rid]), "budget": r2(c["budget"]), "actual": r2(c["actual_qty"]),
            "remaining": r2(c["remain_qty"]), "at_completion": r2(c["actual_qty"] + c["remain_qty"]),
            "unspread_budget": r2(c["unspread"]), "periods": len(mine), "over_periods": n_over if cap else None,
            "peak": peak and {**peak, "load": r2(peak["load"])}})
    page = _slice(out_rows, offset, limit)
    page.update(
        project=project, data_date=_fmt_dt(dd), period=period,
        periods=[{"period": f"{a:%Y-%m}" if period == "month" else f"{a:%Y-%m-%d}", "start": f"{a:%Y-%m-%d}",
                  "end": f"{b:%Y-%m-%d}"} for a, b in zip(edges, edges[1:])],
        resources=resources,
        counts={"resources": len(resources), "over_allocated_periods": over_periods, "resources_over": resources_over,
                "material_resources": sum(m["kind"] == "material" for m in res), "no_limit": no_limit,
                "spread": {**sp.counts, "trsrcfin": fin_rows}},
        schedule_state=_schedule_state(con, pid),
        note=_spread_note({**sp.counts, "trsrcfin": fin_rows}) + " Available = working hours of the resource's calendar in "
        "the period x its max units per hour (RSRCRATE by effective date, the first rate also before its date); shift "
        "calendars and leveling are not applied. Over-allocated = actual + remaining above available, both rounded to "
        "2 decimals. Labor and nonlabor quantities are hours; material quantities are in their own unit and have no "
        "limit here. Totals per resource are the stored assignment quantities (p6_resource_summary); with "
        "from_date / to_date the rows cover only those periods. Values of the last F9 and progress update.")
    return page


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_calendar_check(project: ProjectId, kinds: Optional[CalCheckKinds] = None, from_year: Optional[Year] = None,
                      to_year: Optional[Year] = None, offset: Offset = 0, limit: Limit = 100) -> dict:
    """Checks the calendars a project uses (its project calendar, its activities' calendars, the calendars of its
    assigned resources, and their base calendars), one page of findings: {total, offset, returned, next_offset,
    rows, project, span, calendars, counts, by_severity, schedule_state, note}. Kinds, by severity:
    error: calendar_missing (a calendar id that no calendar has, or activities without one), unreadable
    (calendar data that cannot be read: no working time), base_missing, no_working_time (no work in the span),
    rsrc_dep_no_calendar (open resource dependent activity with an assigned resource without a usable calendar:
    one row per activity); warning: project_holiday_worked (a holiday of the project calendar, on a weekday it
    works, that another used calendar works), holiday_not_in_project (a holiday of another used calendar that the
    project calendar works), no_exceptions_in_year (a year of the span without any exception date),
    day_hours_stale (hours per day differ from the work week's day hours); info: week_hours_stale (stored week
    hours differ from the work week), work_week_differs (work week other than the project calendar's). Span:
    whole years from the data date (else the project start) to the project finish (scheduled finish, else the
    latest activity finish), at most 10 years; from_year / to_year override it. Rows: kind, severity, clndr_id,
    calendar, calendar_type, roles (project, activity, resource, base), date, year, task_code, task_name, detail,
    activities (activities of the project on that calendar), resource_assignments (assignments of the project
    whose resource uses it). calendars lists the calendars checked with their counts and findings. counts and
    by_severity cover every kind, whatever kinds= keeps. Order: severity, kind, calendar, date or year, Activity ID."""
    want = list(CAL_CHECK_KINDS) if kinds is None else list(dict.fromkeys([kinds] if isinstance(kinds, str) else kinds))
    bad = [k for k in want if k not in CAL_CHECK_KINDS]
    if bad or not want:
        raise ValueError(f"kinds: choose from {list(CAL_CHECK_KINDS)}" + (f" (unknown: {bad})" if bad else ""))
    for k, v in (("from_year", from_year), ("to_year", to_year)):
        if v is not None and not 1900 <= int(v) <= 2200:
            raise ValueError(f"{k} must be a year from 1900 to 2200")
    _check_page(offset, limit)
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # project, calendars, counts and assignments from one snapshot
    try:
        d = _cal_check_read(con, pid)
    finally:
        con.rollback()
    prj, raw, cals, acts, asg = d["prj"], d["raw"], d["cals"], d["acts"], d["asg"]
    notes: list[str] = []
    # span of whole years
    a = _db_dt(prj["last_recalc_date"]) or _db_dt(prj["plan_start_date"])
    b = _db_dt(prj["scd_end_date"]) or _db_dt(prj["plan_end_date"]) or _db_dt(d["last"])
    basis = (f"{'data date' if prj['last_recalc_date'] else 'project start'} {a.date()} to "
             f"{'scheduled finish' if prj['scd_end_date'] else 'planned finish' if prj['plan_end_date'] else 'latest activity finish'}"
             f" {b.date()}") if a and b else None
    y0 = int(from_year) if from_year is not None else a.year if a else None
    y1 = int(to_year) if to_year is not None else b.year if b else None
    if y0 is None or y1 is None:
        raise ValueError(f"{project} has no data date, start or finish: pass from_year and to_year")
    if y1 < y0:
        if to_year is not None or from_year is not None:
            raise ValueError("to_year is before from_year")
        y1 = y0  # finish before the data date: the data date's year
    if y1 - y0 + 1 > CAL_CHECK_YEARS:
        if from_year is not None and to_year is not None:
            raise ValueError(f"at most {CAL_CHECK_YEARS} years (from_year to to_year)")
        notes.append(f"Span cut to {CAL_CHECK_YEARS} years ({y0} to {y0 + CAL_CHECK_YEARS - 1}).")
        y1 = y0 + CAL_CHECK_YEARS - 1
    years = range(y0, y1 + 1)

    # calendars used, with their roles; then their base chains
    roles: dict = {}
    if prj["clndr_id"] is not None:
        roles.setdefault(prj["clndr_id"], []).append("project")
    for cid in acts:
        if cid is not None:
            roles.setdefault(cid, []).append("activity")
    for cid in asg:
        if cid is not None:
            roles.setdefault(cid, []).append("resource")
    chain = list(roles)
    for cid in chain:  # appends the bases while it walks (each id once)
        base = raw[cid]["base_clndr_id"] if cid in raw else None
        if base is not None and base != cid and base in raw and base not in roles:
            roles[base] = ["base"]
            chain.append(base)
    proj = cals.get(prj["clndr_id"])
    proj = proj if proj is not None and not proj.error else None
    if prj["clndr_id"] is None:
        notes.append("The project has no calendar: holidays and work weeks are not compared.")
    elif proj is None:
        notes.append("The project calendar is missing or unreadable: holidays and work weeks are not compared.")

    found: list[dict] = []

    def add(kind: str, cid, detail: str, when: Optional[date] = None, year: Optional[int] = None,
            task: Optional[sqlite3.Row] = None) -> None:
        r = raw.get(cid)
        found.append({"kind": kind, "severity": CAL_CHECK_KINDS[kind], "clndr_id": cid,
                      "calendar": r["clndr_name"] if r else None, "calendar_type": r["clndr_type"] if r else None,
                      "roles": roles.get(cid, []), "date": when.isoformat() if when else None, "year": year,
                      "task_code": task["task_code"] if task else None, "task_name": task["task_name"] if task else None,
                      "detail": detail, "activities": acts.get(cid, (0, 0))[0] if cid is not None else 0,
                      "resource_assignments": asg.get(cid, 0) if cid is not None else 0})

    if None in acts:
        add("calendar_missing", None, f"{acts[None][0]} activities of the project have no calendar.")
    t0, t1 = datetime(y0, 1, 1), datetime(y1 + 1, 1, 1)
    for cid in chain:
        r, cal = raw.get(cid), cals.get(cid)
        if r is None or cal is None:
            add("calendar_missing", cid, f"Calendar {cid} is not in the database; used as {', '.join(roles[cid])} "
                "calendar.")
            continue
        if r["base_clndr_id"] is not None and r["base_clndr_id"] != cid and r["base_clndr_id"] not in raw:
            add("base_missing", cid, f"Base calendar {r['base_clndr_id']} is not in the database: its exceptions "
                "(and, for a resource calendar, its work week) are not applied.")
        if cal.error:
            add("unreadable", cid, f"Calendar data cannot be read ({cal.error}): it has no working time here.")
            continue
        if roles[cid] == ["base"]:
            continue  # a base calendar is checked through the calendars built on it
        work_h = cal.work_hours(t0, t1)
        if work_h <= 0:
            add("no_working_time", cid, f"No working time from {y0} to {y1}"
                + (f": open activities on it that cannot be scheduled: {acts[cid][1]}." if acts.get(cid, (0, 0))[1]
                   else "."))
            continue
        hours = [sum(f - s for s, f in sh) / 60 for sh in cal.week]
        day_h, n_days = sorted({h for h in hours if h}), sum(1 for h in hours if h)
        hpd = r["day_hr_cnt"]
        if not hpd:
            add("day_hours_stale", cid, "Hours per day is empty: 8 h per day is used to show durations and float.")
        elif day_h and all(abs(float(hpd) - h) > 1e-6 for h in day_h) \
                and abs(float(hpd) - sum(hours) / n_days) > 1e-6:
            add("day_hours_stale", cid, f"Hours per day is {float(hpd):g} but the work days have "
                f"{', '.join(f'{h:g}' for h in day_h)} h (average {sum(hours) / n_days:g} h): durations and float "
                "in days use the stored value.")
        if r["week_hr_cnt"] is not None and abs(float(r["week_hr_cnt"]) - sum(hours)) > 1e-6:
            add("week_hours_stale", cid, f"Stored hours per week {float(r['week_hr_cnt']):g}, the work week has "
                f"{sum(hours):g} h.")
        for y in years:
            if not any(x.year == y for x in cal.exc):
                add("no_exceptions_in_year", cid, f"No exception date (holiday or changed hours) in {y}.", year=y)
        if proj is None or cid == prj["clndr_id"]:
            continue
        diff = _week_diff(cal, proj)
        if diff:
            add("work_week_differs", cid, f"Work week differs from the project calendar {proj.name!r}: "
                + "; ".join(diff) + ".")
        for x in _holidays(proj, y0, y1):
            if cal.is_workday(x):
                add("project_holiday_worked", cid, f"{WEEKDAYS[x.weekday()]} {x} is a holiday of the project calendar "
                    f"{proj.name!r}; this calendar works {cal.day_hours(x):g} h that day.", when=x)
        for x in _holidays(cal, y0, y1):
            if proj.is_workday(x):
                add("holiday_not_in_project", cid, f"{WEEKDAYS[x.weekday()]} {x} is a holiday of this calendar; the "
                    f"project calendar {proj.name!r} works {proj.day_hours(x):g} h that day.", when=x)
    # open resource dependent activities whose assigned resources have no usable calendar
    by_task: dict = {}
    for t in d["rd"]:
        rc = cals.get(t["rsrc_clndr_id"])
        why = ("resource not found" if t["found"] is None else "no calendar" if t["rsrc_clndr_id"] is None
               else f"calendar {t['rsrc_clndr_id']} not found" if rc is None else "calendar unreadable" if rc.error
               else None)
        if why:
            by_task.setdefault(t["task_id"], [t, []])[1].append(f"{t['rsrc_short_name'] or t['rsrc_id']} ({why})")
    for t, res in by_task.values():
        add("rsrc_dep_no_calendar", t["clndr_id"], "Resource dependent activity; assigned resources without a usable "
            f"calendar: {', '.join(res)}. This server's CPM engine schedules those assignments on the activity "
            "calendar.", task=t)

    kind_rank = {k: i for i, k in enumerate(CAL_CHECK_KINDS)}
    found.sort(key=lambda f: (SEVERITY_RANK[f["severity"]], kind_rank[f["kind"]], (f["calendar"] or "").lower(),
                              f["clndr_id"] if f["clndr_id"] is not None else -1, f["date"] or "", f["year"] or 0,
                              f["task_code"] or ""))
    counts = {k: 0 for k in CAL_CHECK_KINDS}
    per_cal: dict = {}
    for f in found:
        counts[f["kind"]] += 1
        per_cal[f["clndr_id"]] = per_cal.get(f["clndr_id"], 0) + 1
    by_sev = {s: sum(n for k, n in counts.items() if CAL_CHECK_KINDS[k] == s) for s in SEVERITY_RANK}
    role_rank = {"project": 0, "activity": 1, "resource": 2, "base": 3}
    listed = []
    for cid in sorted(chain, key=lambda c: (role_rank[roles[c][0]], (raw[c]["clndr_name"] if c in raw else "").lower(), c)):
        r, cal = raw.get(cid), cals.get(cid)
        ok = cal is not None and not cal.error
        listed.append({"clndr_id": cid, "name": r["clndr_name"] if r else None, "type": r["clndr_type"] if r else None,
                       "roles": roles[cid], "base_clndr_id": r["base_clndr_id"] if r else None,
                       "activities": acts.get(cid, (0, 0))[0], "open_activities": acts.get(cid, (0, 0))[1],
                       "resource_assignments": asg.get(cid, 0),
                       "hours_per_day": float(r["day_hr_cnt"]) if r and r["day_hr_cnt"] else None,
                       "work_week": _week_text(cal) if ok else None,
                       "week_hours": sum(sum(f - s for s, f in sh) for sh in cal.week) / 60 if ok else None,
                       "holidays_in_span": len(_holidays(cal, y0, y1)) if ok else None,
                       "exceptions_in_span": sum(1 for x in cal.exc if y0 <= x.year <= y1) if ok else None,
                       "findings": per_cal.get(cid, 0)})
    res = _slice([f for f in found if f["kind"] in want], offset, limit)
    res.update(project=project, span={"from_year": y0, "to_year": y1, "basis": "from_year / to_year" if (
        from_year is not None or to_year is not None) else basis},
               calendars=listed[:MAX_ROWS], counts=counts, by_severity=by_sev, schedule_state=d["state"],
               note=" ".join(notes + [
                   "Holidays = exception dates without working time on a weekday the calendar's work week works; "
                   "a calendar's exceptions include its base calendar's. Compared with the project calendar only. "
                   "Hours per day convert hours to days; stored hours per week are never used in arithmetic here."]
                   + ([f"calendars lists the first {MAX_ROWS} of {len(listed)}."] if len(listed) > MAX_ROWS else [])))
    return res


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_diagnose() -> dict:
    """Start here when a tool reports a configuration problem. Shows the server settings in force
    (database, tool profile, writes allowed, restart needed), the P6 schema and version detected,
    whether P6 is running, the backup and edit log folders. Works without a database."""
    st = _state.SETTINGS
    now_listed = {t for t in TOOL_HINTS if _policy.listed(t, st.tool_profile, st.allow_writes, _shop_on(st))}
    out: dict[str, Any] = {
        "server_version": SERVER_VERSION,
        "settings": {"source": st.source, "config_file": st.config_path, "config_error": st.config_error,
                     "config_migrated": list(st.migrated), "database": st.db_name, "db_path": _state.DB_PATH or None,
                     "tool_profile": st.tool_profile, "allow_writes": st.allow_writes,
                     "backup_before_write": st.backup_before_write, "backup_scope": st.backup_scope,
                     "backup_retention": st.backup_retention, "block_when_p6_running": st.block_when_p6_running},
        "tools_listed": len(LISTED),
        "restart_required": now_listed != LISTED,
        "p6_running": _p6_running(),
        "openpyxl": _openpyxl_version(),  # p6_export_dashboard needs it (None: not installed)
        "shop": {"enabled": _shop_on(st), "catalog": _shop.REPO_URL},
    }
    if out["restart_required"]:
        out["restart_note"] = ("The tool profile, the write permission or the skill shop setting changed since the "
                               "server started: restart your MCP client to refresh its tool list.")
    if _state.XER_MANIFEST is not None:
        out["xer_mode"] = {"files": [{"file": os.path.basename(f["path"]), "path": f["path"],
                                      "xer_version": f.get("xer_version"), "projects": f["projects"],
                                      "warnings": f["warnings"][:10]} for f in _state.XER_MANIFEST["files"]],
                           "note": "Read only. A Project ID already used by an earlier file gets a ~2 suffix."}
    if st.eppm:
        m = _state.EPPM_MANIFEST or {}
        out["eppm_mode"] = {"server": st.eppm, "working_copy": _eppm_store.store_path(st.eppm) if m else None,
                            "pulled_at": m.get("pulled_at"), "route": m.get("route"),
                            "projects": [p["project_id"] for p in m.get("projects", [])],
                            "warnings": m.get("warnings", [])[:10],
                            "note": "Tools read the working copy (p6_eppm_pull). Edits change only that copy: "
                                    "p6_eppm_push sends them, p6_eppm_schedule schedules on the server; "
                                    "p6_eppm_status lists the pending changes."}
        prof = _config.eppm_profile(st.raw, st.eppm)
        if prof:  # how the REST calls go out (no password: the proxy user only); the SQL route is never proxied
            from p6mcp.eppm import proxy as _eppm_proxy
            out["eppm_mode"]["proxy"] = _eppm_proxy.resolve(prof.get("base_url", ""), prof)
    if not _state.DB_PATH:
        out["database_problem"] = st.db_problem or _config.NO_DB_MESSAGE
        return out
    con = _connect()
    try:
        nk = [_dict(r) for r in con.execute(
            "SELECT key_name, key_seq_num FROM NEXTKEY WHERE lower(key_name) LIKE '%task_pred_id%' "
            "OR lower(key_name) LIKE '%task_task_id%'")]
        info = _schema_info()
        out.update({
            "db_path": _state.DB_PATH,
            "schema": _diag_schema(info),
            "writes": _write_refusal() or "allowed",
            "tables": {t: sorted(_cols(con, t))[:60] for t in ("TASK", "TASKPRED", "PROJECT", "CALENDAR")},
            "nextkey_rows": nk,
            "backup_taken_this_session": _state._backup_done,
            "backup_dir": _backup_dir(),
            "backups_pruned_last": _state._last_pruned,
            "edit_log_dir": _log_dir(),
        })
    finally:
        con.close()
    return out


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_add_relationships(project: ProjectId, items: LinkNews, confirm: bool = False) -> dict:
    """Add many relationships in one transaction (all or nothing; dry run unless confirm=True). Item:
    {predecessor_code, successor_code, rel_type (FS default), lag_days (0 default, days of the lag
    calendar rule as in p6_add_relationship), predecessor_project}. items: one report per item
    (task_pred_id from NEXTKEY, or the error); side_effects as in p6_delete_relationships (empty for
    additions). One edit log entry per link."""
    items = _items_arg(items)
    return _link_batch(project, items, "add", confirm, _rel_add, f"Add {len(items)} relationship(s)")


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_update_relationships(project: ProjectId, items: LinkEdits, confirm: bool = False) -> dict:
    """Change type and/or lag of many relationships in one transaction (all or nothing; dry run unless
    confirm=True). Item: {predecessor_code, successor_code, rel_type, lag_days, current_type (picks
    the link when several types link the pair), predecessor_project}, as p6_update_relationship.
    items: one report per item; side_effects as in p6_delete_relationships (empty: no link is
    removed). One edit log entry per link."""
    items = _items_arg(items)
    return _link_batch(project, items, "update", confirm, _rel_update, f"Change {len(items)} relationship(s)")


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
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
    it = {k: v for k, v in (
        ("task_code", task_code), ("task_name", task_name), ("original_duration_days", original_duration_days),
        ("remaining_duration_days", remaining_duration_days), ("constraint", constraint),
        ("constraint_date", constraint_date), ("actual_start", actual_start), ("actual_finish", actual_finish),
        ("physical_pct", physical_pct)) if v is not None}
    if allow_after_data_date:
        it["allow_after_data_date"] = True
    _act_check(it)  # a malformed value fails before any lock or backup
    with _write_session(confirm) as (con, backup):
        try:
            r = _act_update(con, project, it)
        except _Refused as e:
            return {"applied": False, "error": str(e)}
        plan = list(r["_lines"])
        if backup: plan.append(f"Backup: {backup}")
        return _commit(con, plan, confirm, projects=r["projects"], log=r["log"])


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_update_activities(project: ProjectId, items: ActEdits, confirm: bool = False) -> dict:
    """Edit many activities in one transaction (all or nothing; dry run unless confirm=True), for a
    progress update: actual dates, remaining durations, physical %, names, constraints. Item: {task_code,
    task_name, original_duration_days, remaining_duration_days, constraint, constraint_date, actual_start,
    actual_finish, physical_pct, allow_after_data_date}, each with the rules of p6_update_activity. items:
    one report per item (its plan or the error); one edit log entry per activity. At most 500 items."""
    items = _items_arg(items)
    with _write_session(confirm) as (con, backup):
        _project_id(con, project)
        b = _batch(con, items, lambda i, it: _act_update(con, project, it))
        for rep, it in zip(b["items"], items):
            rep["item"] = it
        word = "activity" if len(items) == 1 else "activities"
        return _batch_commit(con, b, confirm, f"Update {len(items)} {word}", backup)


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
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
    activity's labor / nonlabor unit totals follow. Dates are not affected. P6's summary tables
    (SUMTASK, SUMTASKSPREAD, SUMTRSRC) are never written: summaries_stale {stale, tables {table:
    rows the change leaves out of date}, refreshed (always empty), advice (run Summarize in P6)}.
    Dry run unless confirm=True."""
    it = {k: v for k, v in (
        ("task_code", task_code), ("resource", resource), ("role", role), ("taskrsrc_id", taskrsrc_id),
        ("budgeted_units", budgeted_units), ("actual_units", actual_units), ("remaining_units", remaining_units),
        ("at_completion_units", at_completion_units)) if v is not None}
    with _write_session(confirm) as (con, backup):
        try:
            x = _asg_update(con, project, it)
        except _Refused as e:
            return {"applied": False, "error": str(e)}
        sums = _summaries_stale(con, [x["_sum"]])
        plan = [x["plan"], *_sum_plan(sums)]
        if backup:
            plan.append(f"Backup: {backup}")
        res = _commit(con, plan, confirm, projects=x["projects"], log=x["log"])
        res["summaries_stale"] = sums
        return res


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_update_assignments(project: ProjectId, items: AsgEdits, confirm: bool = False) -> dict:
    """Set the units of many assignments in one transaction (all or nothing; dry run unless
    confirm=True). Item: {task_code, resource, role, taskrsrc_id, budgeted_units, actual_units,
    remaining_units, at_completion_units}, as p6_update_assignment. items: one report per item
    (assignment before / after, or the error). One edit log entry per assignment. summaries_stale
    as p6_update_assignment, for every applied item."""
    items = _items_arg(items)
    with _write_session(confirm) as (con, backup):
        _project_id(con, project)
        keys: list[tuple] = []

        def apply_one(i: int, it) -> dict:
            if not isinstance(it, Mapping):
                raise ValueError("an item must be an object")
            x = _asg_update(con, project, it)
            keys.append(x["_sum"])
            return x

        b = _batch(con, items, apply_one)
        for rep, it in zip(b["items"], items):
            rep["item"] = it
        sums = _summaries_stale(con, keys)
        res = _batch_commit(con, b, confirm, f"Update units of {len(items)} assignment(s)", backup, tail=_sum_plan(sums))
        res["summaries_stale"] = sums
        return res


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
def p6_backup_database() -> dict:
    """Take a manual backup copy of the P6 database now (kept apart from the automatic retention)."""
    why = _write_refusal()
    if why:
        raise RuntimeError(why)
    return {"backup": _backup("manual")}


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_list_backups(offset: Offset = 0, limit: Limit = 200) -> dict:
    """Backup files (.db) in the backup folder, newest first: {total, offset, returned, next_offset,
    rows, folder}. Row: name (pass it to p6_restore_backup), taken_at (from the file name, else the
    file time), size_bytes, size_mb, kind ('backup', 'pre_restore' = safety copy taken by a
    restore), this_database (named after the current database file)."""
    folder = _backup_dir()
    stem = os.path.splitext(os.path.basename(_state.DB_PATH))[0].lower()
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


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False})
def p6_restore_backup(name: str, confirm: bool = False) -> dict:
    """Replace the database with a backup from the backup folder (name as p6_list_backups shows it).
    Dry run: the backup's projects compared with the current ones (only_in_backup, only_in_database,
    changed data date or last schedule date). confirm=True: refused while P6 is running; checks the
    backup (PRAGMA quick_check), takes a safety backup of the current database (kind pre_restore),
    copies the backup in with the SQLite backup API and logs the restore in the edit log (logged
    edits made after the backup was taken become undone). Afterwards open P6 and check the projects."""
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
    cur, old = _projects_of(_state.DB_PATH), _projects_of(path)
    changed = sorted(p for p in set(cur) & set(old) if cur[p] != old[p])
    diff = {"only_in_backup": sorted(set(old) - set(cur))[:50], "only_in_database": sorted(set(cur) - set(old))[:50],
            "changed": [{"project": p, "database": {"data_date": cur[p][0], "last_schedule_date": cur[p][1]},
                         "backup": {"data_date": old[p][0], "last_schedule_date": old[p][1]}} for p in changed[:50]],
            "counts": {"backup_projects": len(old), "database_projects": len(cur),
                       "only_in_backup": len(set(old) - set(cur)), "only_in_database": len(set(cur) - set(old)),
                       "changed": len(changed)}}
    if os.path.splitext(name)[0].lower().startswith(os.path.splitext(os.path.basename(_state.DB_PATH))[0].lower() + "_"):
        warn = []
    else:
        warn = [f"{name} is not named after {os.path.basename(_state.DB_PATH)}: it may come from another database"]
    plan = [f"Restore {name} (taken {taken}, {os.path.getsize(path) / 1048576:.1f} MB) over {_state.DB_PATH}",
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
                "next": "Call again with confirm=True to restore"
                        + (" (the local working copy only)." if _state.SETTINGS.eppm else " (P6 must be closed).")}
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
        dst = sqlite3.connect(_state.DB_PATH, timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    _state._backup_done = False  # the next write backs up the restored state
    _cols_cache.clear()
    _schema_cache.clear()
    entry = {"ts": _log_ts(), "commit": uuid.uuid4().hex[:12], "db": os.path.basename(_state.DB_PATH),
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


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_list_eps(order: EpsOrder = "seq", offset: Offset = 0, limit: Limit = 200) -> dict:
    """EPS nodes, one page: {total, offset, returned, next_offset, rows}. eps_id is the short name
    used by p6_import_xer and p6_create_eps; parent_wbs_id links a node to its parent EPS; depth (0 = top
    level) and path (EPS names from the top, joined with ' > '), projects (projects directly under the
    node) and projects_subtree (under the node and every EPS below it); baselines and template projects
    (status WS_Template) are not counted. order: 'seq' (P6 sequence, then EPS ID) or 'tree' (each node
    before its children, siblings in 'seq' order)."""
    if order not in get_args(EpsOrder):
        raise ValueError(f"order must be one of {list(get_args(EpsOrder))}")
    con = _connect()
    con.execute("BEGIN")  # nodes, tree and counts from one snapshot
    try:
        nodes, tree = _eps_nodes(con), _eps_tree(con)
        status = "w.status_code" if "status_code" in _cols(con, "PROJWBS") else "NULL"
        direct = Counter(r[0] for r in con.execute(
            "SELECT w.parent_wbs_id FROM PROJECT p JOIN PROJWBS w ON w.proj_id = p.proj_id AND w.proj_node_flag = 'Y'"
            + _alive(con, "PROJWBS", "w") + " WHERE COALESCE(p.project_flag, 'Y') = 'Y' AND p.orig_proj_id IS NULL"
            + _alive(con, "PROJECT", "p") + f" AND {status} IS NOT 'WS_Template'"))
    finally:
        con.rollback()
    sub = {n["wbs_id"]: direct.get(n["wbs_id"], 0) for n in tree}
    for n in reversed(tree):  # children come after their parent: add them up from the bottom
        if n["parent_wbs_id"] in sub:
            sub[n["parent_wbs_id"]] += sub[n["wbs_id"]]
    tree = [{**n, "projects": direct.get(n["wbs_id"], 0), "projects_subtree": sub[n["wbs_id"]]} for n in tree]
    if order == "seq":
        at = {n["wbs_id"]: n for n in tree}
        tree = [at[n["wbs_id"]] for n in nodes if n["wbs_id"] in at]
    return _slice(tree, offset, limit)


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_set_schedule_options(project: ProjectId, options: Optional[SchedValues] = None,
                            from_xer: Optional[str] = None, xer_project: Optional[str] = None,
                            key_activity: Optional[str] = None, level_priority_list: Optional[LevelPriority] = None,
                            check_options: Optional[SchedValues] = None, confirm: bool = False) -> dict:
    """Write a project's schedule options (P6 Tools > Schedule > Options, stored in PROJPROP
    'scheduling'). Dry run unless confirm=True; the result lists old and new values, changes first.
    from_xer: take every option from that XER's SCHEDOPTIONS (xer_project picks the project); the
    XER key activity is mapped by Activity ID, -99 (none) with a warning when it has no match.
    options: {name: value} with the names p6_schedule_options shows (Y/N flags, numbers, enumerations
    such as sched_float_type FT_FF/FT_SS/FT_Min, sched_calendar_on_relationship_lag rcal_Predecessor/
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


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False})
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
    Global data (currencies, OBS, UDF types, calendars, resources, code types and codes, notebook
    topics, expense categories, roles and risk types) is matched by name and reused; missing items
    are created. Resource and role rates are only imported for new resources and roles. Notebooks,
    steps, expenses and risks are imported with the activities (update mode: matched by activity and
    topic, step name, expense name or Risk ID; the ones missing from the XER are kept).
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
        for t in ("MEMOTYPE", "COSTTYPE", "ROLES", "RISKTYPE"):  # notebook topics, expense categories, roles
            if x.get(t) and imp.can(t):
                imp.match_global(t, x[t])

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
            imp.flush()
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
        for t, owner, col in (("RSRCRATE", "RSRC", "rsrc_id"), ("ROLERATE", "ROLES", "role_id")):
            for r in (x.get(t, []) if t == "RSRCRATE" or imp.can(t) else []):  # rates of new resources and roles
                if r.get(col) in imp.created[owner]:
                    imp.new_row(t, r)
                else:
                    imp.count(t, "kept_existing")

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

        imp.flush()
        # update mode: the match queries' keys read once (_XerImport.index / find), inserted rows added
        wbs_sql = "SELECT wbs_id FROM PROJWBS WHERE proj_id = ? AND parent_wbs_id = ? AND wbs_short_name = ?" \
            + _alive(con, "PROJWBS")
        wbs_ix = imp.index("SELECT wbs_id, parent_wbs_id, wbs_short_name FROM PROJWBS WHERE proj_id = ?"
                           + _alive(con, "PROJWBS"), (pid,)) if mode == "update" else {}
        for w in sorted((w for w in wbs if w is not node), key=depth):
            parent = imp.idmap["PROJWBS"].get(w.get("parent_wbs_id"), imp.idmap["PROJWBS"][node["wbs_id"]])
            hit = None
            if mode == "update":
                hit = imp.find(wbs_ix, (parent, w["wbs_short_name"]), wbs_sql, (pid, parent, w["wbs_short_name"]))
            if hit:
                imp.idmap["PROJWBS"][w["wbs_id"]] = hit
                imp.upd_row("PROJWBS", hit, w, {"parent_wbs_id": parent, "proj_id": pid})
            else:
                new = imp.idmap["PROJWBS"][w["wbs_id"]] = imp.new_row("PROJWBS", w, {"parent_wbs_id": parent})
                if imp.idmap["PROJECT"].get(w.get("proj_id")) == pid:
                    wbs_ix.setdefault((parent, w["wbs_short_name"]), []).append(new)
        imp.flush()

        # ---- activities
        task_sql = "SELECT task_id FROM TASK WHERE proj_id = ? AND task_code = ?" + _alive(con, "TASK")
        task_ix = imp.index("SELECT task_id, task_code FROM TASK WHERE proj_id = ?" + _alive(con, "TASK"),
                            (pid,)) if mode == "update" else {}
        for t in x.get("TASK", []):
            hit = None
            if mode == "update":
                hit = imp.find(task_ix, (t["task_code"],), task_sql, (pid, t["task_code"]))
            if hit:
                imp.idmap["TASK"][t["task_id"]] = hit
                imp.upd_row("TASK", hit, t)
            else:
                new = imp.idmap["TASK"][t["task_id"]] = imp.new_row("TASK", t)
                if imp.idmap["PROJECT"].get(t.get("proj_id")) == pid:
                    task_ix.setdefault((t["task_code"],), []).append(new)
        imp.flush()
        # links, assignments and codes are read once for the project's activities (tids, soft-deleted ones too);
        # a row of another activity (a new one outside the project) is matched with its query as before
        own = "IN (SELECT task_id FROM TASK WHERE proj_id = ?)"
        tids = {r[0] for r in con.execute("SELECT task_id FROM TASK WHERE proj_id = ?", (pid,))}

        # ---- relationships (one link per pair and type: a repeated XER row is skipped)
        seen_links = set()
        pred_sql = "SELECT task_pred_id FROM TASKPRED WHERE task_id = ? AND pred_task_id = ? AND pred_type = ?" \
            + _alive(con, "TASKPRED")
        pred_ix = imp.index("SELECT task_pred_id, task_id, pred_task_id, pred_type FROM TASKPRED WHERE task_id "
                            + own + _alive(con, "TASKPRED"), (pid,)) if mode == "update" else {}
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
                hit = imp.find(pred_ix, (succ, pred, r["pred_type"]), pred_sql, (succ, pred, r["pred_type"]),
                               succ in tids)
            if hit:
                imp.upd_row("TASKPRED", hit, r)
                imp.touched["TASKPRED"].add(hit)
            else:
                new = imp.new_row("TASKPRED", r)
                imp.touched["TASKPRED"].add(new)
                pred_ix.setdefault((succ, pred, r["pred_type"]), []).append(new)
        imp.flush()

        # ---- assignments (a database row is matched once: touched holds matched and new ids)
        used = imp.touched["TASKRSRC"]
        asg_sql = "SELECT taskrsrc_id FROM TASKRSRC WHERE task_id = ? AND rsrc_id IS ?" + _alive(con, "TASKRSRC")
        asg_ix = imp.index("SELECT taskrsrc_id, task_id, rsrc_id FROM TASKRSRC WHERE task_id " + own
                           + _alive(con, "TASKRSRC"), (pid,)) if mode == "update" else {}
        for r in x.get("TASKRSRC", []):
            task, rsrc = imp.idmap["TASK"].get(r["task_id"]), imp.idmap["RSRC"].get(r.get("rsrc_id"))
            if task is None:
                imp.count("TASKRSRC", "skipped_unmapped")
                continue
            hit = None
            if mode == "update":
                free = [i for i in asg_ix.get((task, rsrc), ()) if i not in used]
                if len(free) > 1 or task not in tids:  # the database's row order decides, as before
                    imp.flush()
                    free = [h[0] for h in con.execute(asg_sql, (task, rsrc)) if h[0] not in used]
                hit = free[0] if free else None
            if hit:
                imp.idmap["TASKRSRC"][r["taskrsrc_id"]] = hit
                imp.upd_row("TASKRSRC", hit, r)
            else:
                hit = imp.idmap["TASKRSRC"][r["taskrsrc_id"]] = imp.new_row("TASKRSRC", r)
                asg_ix.setdefault((task, rsrc), []).append(hit)
            used.add(hit)
        imp.flush()

        # ---- risks, notebooks, steps and expenses (before the UDF values that may point to them)
        for t in ("PROJRISK", "TASKMEMO", "TASKPROC", "PROJCOST"):
            imp.children(t, x.get(t, []))
        if x.get("TASKRISK") and imp.can("TASKRISK"):
            for r in x["TASKRISK"]:
                row = imp.prep("TASKRISK", r)
                if any(row.get(c) is None for c in XER_CHILD_NEEDS["TASKRISK"]):
                    imp.count("TASKRISK", "skipped_unmapped")
                    continue
                _upsert(imp, "TASKRISK", row, ("task_id", "risk_id"))

        # ---- activity code assignments and UDF values (upsert on their composite keys)
        # keys present (soft-deleted rows too: an upsert revives them); rows of other activities checked one by one
        actv = (lambda k: k[0] in tids, {tuple(r) for r in con.execute(
            "SELECT task_id, actv_code_type_id FROM TASKACTV WHERE task_id " + own, (pid,))})
        for r in x.get("TASKACTV", []):
            row = imp.prep("TASKACTV", r)
            if None in (row.get("task_id"), row.get("actv_code_type_id"), row.get("actv_code_id")):
                imp.count("TASKACTV", "skipped_unmapped")
                continue
            _upsert(imp, "TASKACTV", row, ("task_id", "actv_code_type_id"), actv)
        imp.flush()
        udf_tables = {u["udf_type_id"]: u["table_name"] for u in x.get("UDFTYPE", [])}
        types = {v for v in imp.idmap["UDFTYPE"].values() if v is not None}
        udfv = (lambda k: k[0] in types, {tuple(r) for r in con.execute(
            f"SELECT udf_type_id, fk_id FROM UDFVALUE WHERE udf_type_id IN {_ids_sql(sorted(types))}")})
        for r in x.get("UDFVALUE", []):
            tbl = udf_tables.get(r["udf_type_id"])
            row = imp.prep("UDFVALUE", r)
            row["fk_id"] = imp.idmap.get(tbl, {}).get(r["fk_id"]) if tbl in UDF_FK_TABLES else None
            if row.get("udf_type_id") is None or row["fk_id"] is None:
                imp.count("UDFVALUE", "skipped_unmapped")
                continue
            _upsert(imp, "UDFVALUE", row, ("udf_type_id", "fk_id"), udfv)
        imp.flush()

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
        res.update({"counts": imp.counts(), "warnings": imp.warnings, "data_date": data_date, "preflight": pf})
        if missing is not None:
            res["missing_from_xer"] = missing
        res["schedule_options"] = {"imported": sched is not None, "action": sched and sched["action"],
                                   "changed": [o for o in sched_rows if o["changed"]] if sched_rows else []}
        if not confirm:
            res.update(would_refuse=bool(reasons), refuse_reasons=reasons)
            if reasons:
                res["next"] = "confirm=True would be refused: " + " ".join(reasons)
        return res


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
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


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_export_xer(project: ProjectId, path: str, tables: Optional[list[XerTable]] = None,
                  xer_version: Optional[str] = None, overwrite: bool = False) -> dict:
    """Export one project to an XER file (read only on the database). An existing file is refused
    unless overwrite=True.
    tables: which XER tables to write, any of
      COSTTYPE, CURRTYPE, FINTMPL, MEMOTYPE, OBS, RISKTYPE, ROLES, UDFTYPE, PROJECT, CALENDAR,
      SCHEDOPTIONS, PROJWBS, RSRC, ACTVTYPE, PROJRISK, ROLERATE, RSRCRATE, TASK, ACTVCODE, PROJCOST,
      TASKMEMO, TASKPRED, TASKPROC, TASKRSRC, TASKACTV, TASKRISK, UDFVALUE (default: all; a table
      this database lacks is skipped with a warning).
    Activity notebooks (TASKMEMO, topics in MEMOTYPE), steps (TASKPROC), expenses (PROJCOST,
    categories in COSTTYPE), roles (ROLES, ROLERATE) and risks (RISKTYPE, PROJRISK, TASKRISK) are
    written with the activities.
    Global data is limited to what the project uses (calendars, resources, roles, codes, UDF types,
    OBS, notebook topics, expense categories, risk types).
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


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_export_dashboard(project: ProjectId, path: str, baseline: Optional[ProjectId] = None,
                        baseline_dates: BaselineDates = "planned", milestones: Optional[ActivityIds] = None,
                        near_critical_days: NonNegDays = 10, period: Period = "month",
                        prepared_by: Optional[str] = None, revision: Optional[str] = None,
                        company: Optional[str] = None, colors: Optional[str] = None,
                        trend: Optional[TrendProjects] = None, overwrite: bool = False) -> dict:
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
    the S-curve actual line) apply to this export only. trend= the Project IDs of the earlier updates of
    the same job (the project is added when missing and must be the latest by data date) adds a Trend
    sheet: one row per update as p6_trend (finish, variances, progress, SPI, critical counts, float,
    DCMA score, changes since the previous update), charts of the forecast finish and of progress and
    SPI per data date, and the milestones of every update (or milestones=) by update. The result gives
    the 7 card values as written (cards), the DCMA checks that fail (dcma_failed) and, with trend=,
    trend {updates, projects}."""
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
    series = None
    if trend is not None:
        names = _trend_names(trend, None)
        series = _trend_series(names + ([] if project in names else [project]))
        if series[-1]["project"] != project:
            raise ValueError(f"trend=: {project} must be the latest update by data date; the latest is "
                             f"{series[-1]['project']} ({series[-1]['data_date']})")
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
    tr = None
    if series:  # every update of the series, the exported project last
        tr = _trend(series, baseline, codes, "matched", near, True, range(len(series)))
        seen.add((tr["rows"][-1]["data_date"], tr["rows"][-1]["last_schedule_date"]))
        warnings += [f"Trend: {n}" for n in tr["notes"]]
        warnings += [f"Trend: {u['project']} was changed by this server after its last F9: its values are those of "
                     "that F9." for u in tr["rows"] if u["stale"]]
    if len(seen) > 1:
        raise ValueError(f"The schedule of {project} changed during the export (data date or last F9): run it again.")
    source = (", ".join(os.path.basename(f) for f in _state.SETTINGS.xer_files) if _state.SETTINGS.xer_files  # XER-only mode
              else os.path.basename(_state.DB_PATH))
    data = {"project": project, "name": name and name[0], "db": source, "now": datetime.now(),
            "rd": rd, "summary": summary, "curve": curve, "curve_error": curve_error, "crit": crit, "hc": hc,
            "state": state, "near": near, "baseline_dates": baseline_dates, "baseline_arg": baseline,
            "warnings": warnings, "brand": brand, **texts, **({"trend": tr} if tr else {})}
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
            "cards": cards, "dcma_failed": [k for k, c in hc["checks"].items()
                                            if c.get("pass") is False and k not in HC_UNSCORED],
            **({"trend": {"updates": len(tr["rows"]), "projects": [u["project"] for u in tr["rows"]]}} if tr else {}),
            "warnings": warnings, "schedule_state": state,
            "note": "Values of the last F9 in P6. The Dashboard sheet is protected (no password) and prints on one A3 "
                    "landscape page."}


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_export_lookahead(project: ProjectId, path: str, weeks: WindowWeeks = 3, from_date: Optional[DateTimeStr] = None,
                        grid: LaGrid = "auto", include_in_progress: bool = True, include_completed: bool = False,
                        critical_only: bool = False, wbs_name_contains: Optional[str] = None,
                        code_type: Optional[str] = None, code_value: Optional[str] = None,
                        include_child_codes: bool = True, overwrite: bool = False) -> dict:
    """Write the p6_lookahead window of a project as a printable Excel sheet to path (.xlsx in an existing folder;
    an existing file is refused unless overwrite=True; never writes the database). Same window, activities,
    categories and critical flag as p6_lookahead with weeks= (default 3) and the same filters. The sheet: title
    with project, data date and window; activities grouped by WBS (P6 order, one band per WBS holding listed
    activities) with Activity ID, name, start, finish (actual dates marked A), remaining duration and total float
    in days of each calendar, critical, status; then a grid of the window, days (grid='day', at most 92 days) or
    weeks (grid='week'; 'auto' = days up to 6 weeks), with a bar per activity (critical red, completed gray,
    others blue, milestones as a diamond), non-work days of the project calendar shaded and the data date marked.
    Landscape A3, fit to width, header rows repeated on every page, panes frozen. Result: path, rows written
    (activities, wbs_bands, grid_columns, bars), counts per category (as p6_lookahead), window, warnings."""
    weeks = int(weeks)
    if not 1 <= weeks <= 52:
        raise ValueError("weeks must be 1 to 52")
    if grid not in get_args(LaGrid):
        raise ValueError(f"grid must be one of {list(get_args(LaGrid))}")
    days = 7 * weeks
    use = grid if grid != "auto" else "day" if days <= LA_AUTO_DAYS else "week"
    if use == "day" and days > LA_DAY_GRID_MAX:
        raise ValueError(f"grid='day' covers at most {LA_DAY_GRID_MAX} days ({LA_DAY_GRID_MAX // 7} weeks): "
                         "use grid='week' or fewer weeks")
    if from_date:
        _parse_dt(from_date, "from_date")
    path = _export_target(path, ".xlsx")
    if os.path.exists(path) and not overwrite:
        raise ValueError(f"{path} already exists: pass overwrite=True to replace it, or choose another path.")
    _openpyxl()
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # window, activities, counts, WBS tree and calendar from one snapshot
    try:
        la = _la_query(con, pid, project, days, from_date, include_in_progress, include_completed, wbs_name_contains,
                       code_type, code_value, include_child_codes, critical_only)
        rows = con.execute(f"{la['sql']}\nLIMIT {LA_EXPORT_CAP + 1}", la["args"]).fetchall()
        counts = la["counts"]()
        tree = _wbs_tree(con, pid)
        pcols = _cols(con, "PROJECT")
        prj = con.execute("SELECT " + ", ".join(c if c in pcols else f"NULL AS {c}" for c in ("clndr_id", "last_schedule_date"))
                          + " FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone()
        cal = _calendars(con).get(prj["clndr_id"]) if prj["clndr_id"] is not None else None
        state = _schedule_state(con, pid)
    finally:
        con.rollback()
    warnings: list[str] = []
    cut = len(rows) > LA_EXPORT_CAP
    if cut:
        rows = rows[:LA_EXPORT_CAP]
        warnings.append(f"More than {LA_EXPORT_CAP:,} activities: the first {LA_EXPORT_CAP:,} in look-ahead order are "
                        "written; narrow with wbs_name_contains, code_type / code_value or critical_only.")
    if cal is None or cal.error:
        warnings.append("The project calendar is missing or unreadable: non-work days are not shaded.")
        cal = None
    if state["stale"]:
        warnings.append("This server changed the schedule after the last F9: open P6 and press F9, then export again.")
    order = {n["wbs_id"]: i for i, n in enumerate(tree)}
    node = {n["wbs_id"]: n for n in tree}
    acts = [{**_act_view(r), **la["extra"](r), "wbs_id": r["wbs_id"]} for r in rows]
    nodates = sum(1 for a in acts if not a["fcst_start"] or not a["fcst_finish"])
    if nodates:
        warnings.append(f"{nodates} activities have no start or finish date: no bar is drawn for them.")
    groups: dict = {}
    for a in acts:  # look-ahead order kept inside each WBS
        groups.setdefault(a["wbs_id"] if a["wbs_id"] in order else None, []).append(a)
    bands = [(f"{node[w]['wbs_code']}  {node[w]['wbs_name'] or ''}".rstrip() if w is not None
              else "(not in the WBS tree)", items)
             for w, items in sorted(groups.items(), key=lambda kv: order.get(kv[0], len(order)))]
    name = next((n["wbs_name"] for n in tree if n["depth"] == 0), None)
    source = (", ".join(os.path.basename(f) for f in _state.SETTINGS.xer_files) if _state.SETTINGS.xer_files  # XER-only mode
              else os.path.basename(_state.DB_PATH))
    data = {"project": project, "name": name, "db": source, "frm": la["frm"], "to": la["to"], "days": days,
            "weeks": la["weeks"], "grid": use, "cal": cal, "data_date": la["data_date"],
            "last_schedule_date": prj["last_schedule_date"], "groups": bands}
    wb, written = _la_book(data)
    if written["data_date_column"] is None:
        warnings.append("The data date is outside the window: no data date line.")
    tmp = path + ".tmp"
    try:  # written beside the target, then swapped in: a failed export leaves the old file intact
        wb.save(tmp)
        os.replace(tmp, path)
    except PermissionError as e:
        raise ValueError(f"Cannot write {path}: close it in Excel, then export again ({e.strerror})") from None
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    res = {"path": path, "project": project, "data_date": la["data_date"], "window": la["window"],
           "weeks": la["weeks"], "grid": use, "rows": written, "counts": counts, "truncated": cut,
           "critical_definition": la["critical_definition"], "warnings": warnings, "schedule_state": state,
           "note": "Values of the last F9 in P6. One sheet, landscape A3 fit to width, header rows repeated."}
    if la["code_filter"]:
        res["code_filter"] = la["code_filter"]
    return res


# ----------------------------------------------------------------- P6 EPPM server (p6mcp.eppm)

def _eppm_profile() -> dict:
    """The confirmed active P6 EPPM server profile; ValueError outside P6 EPPM mode."""
    st = _state.SETTINGS
    p = _config.eppm_profile(st.raw, st.eppm) if st.eppm else None
    if p is None:
        raise ValueError("Not in P6 EPPM mode: in MCP for Primavera P6 Settings (Database), choose 'A P6 EPPM "
                         "server', add the server, test the connection, click Use this server, then Save.")
    return p


def _eppm_secret(key: str, what: str) -> str:
    from p6mcp import credentials
    try:
        pw = credentials.get(key)
    except credentials.SecretStoreError as e:
        raise ValueError(str(e)) from None
    if not pw:
        raise ValueError(f"No {what} is stored for this server: enter it in MCP for Primavera P6 Settings "
                         "(Database) and click Test connection.")
    return pw


def _eppm_pending() -> list[dict]:
    """Net pending changes of the working copy (edits since the pull, not undone, not pushed)."""
    from p6mcp.eppm import pending
    m = _state.EPPM_MANIFEST
    if not m or not _state.DB_PATH:
        return []
    es = _log_entries()
    return pending.net(pending.entries_since(es, m.get("pulled_ts") or m["pulled_at"], _undone_fn(es),
                                             m.get("pushed_entries", [])))


def _eppm_session(p: dict):
    from p6mcp import credentials, eppm
    from p6mcp.eppm import rest
    try:
        return rest.from_profile(p, _eppm_secret(credentials.eppm_key(p["name"]), "P6 password"),
                                 lock_check=eppm.lock_check(p))
    except rest.EppmError as e:
        raise ValueError(str(e)) from None


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
def p6_eppm_projects(search: str = "", offset: int = 0, limit: int = 100) -> dict:
    """P6 EPPM mode: the projects this P6 user can read on the server (Project ID, name, data date, last
    schedule, whether a baseline is set, whether it is in the working copy). Reads the server.
    search: part of the Project ID or name (case ignored)."""
    offset, limit = _check_page(offset, limit)
    p = _eppm_profile()
    pulled = {x["project_id"].lower() for x in (_state.EPPM_MANIFEST or {}).get("projects", [])}
    from p6mcp.eppm import rest
    try:
        with _eppm_session(p) as s:
            rows = s.read("project", ["ObjectId", "Id", "Name", "DataDate", "LastScheduleDate", "Status",
                                      "CurrentBaselineProjectObjectId"])
    except rest.EppmError as e:
        raise ValueError(str(e)) from None
    q = search.strip().lower()
    out = sorted(({"project_id": r.get("Id"), "name": r.get("Name"), "status": r.get("Status"),
                   "data_date": (r.get("DataDate") or "").replace("T", " ")[:19] or None,
                   "last_schedule": (r.get("LastScheduleDate") or "").replace("T", " ")[:19] or None,
                   "has_baseline": r.get("CurrentBaselineProjectObjectId") not in (None, ""),
                   "pulled": str(r.get("Id", "")).lower() in pulled}
                  for r in rows if not q or q in f"{r.get('Id', '')} {r.get('Name', '')}".lower()),
                 key=lambda r: (str(r["project_id"]).lower(), str(r["name"])))
    return dict(_slice(out, offset, limit), server=p["name"])


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True})
def p6_eppm_pull(projects: list[str], include_baseline: bool = True,
                 route: Literal["auto", "sql", "rest"] = "auto", discard_pending: bool = False) -> dict:
    """P6 EPPM mode: copy projects from the server into the local working copy that every other tool
    reads (the projects pulled before are refreshed too). Reads the server only: nothing is changed there.
    projects: Project IDs (see p6_eppm_projects). include_baseline: also copy each project's current
    baseline (needed by baseline variance, BEI and milestones vs baseline).
    route: sql (the read-only SQL account set in MCP for Primavera P6 Settings: every table as stored),
    rest (the server's XER export, for servers without a SQL account), auto = sql when it is set up and
    its driver installed, else rest.
    A pull rebuilds the working copy: it is refused while local edits are pending (see p6_eppm_status)
    unless discard_pending=True, which drops them (ask the user first)."""
    p = _eppm_profile()
    if not projects:
        raise ValueError("Name at least one project (Project ID, see p6_eppm_projects).")
    waiting = _eppm_pending()
    if waiting and not discard_pending:
        raise ValueError(f"{len(waiting)} local change(s) are pending in the working copy and a pull would drop them. "
                         "Push them first (p6_eppm_push), or pull with discard_pending=True if the user agrees to "
                         "lose them. See p6_eppm_status.")
    out = _eppm_pull_now(p, projects, include_baseline, route)
    if waiting:
        out["discarded_changes"] = len(waiting)
    return out


def _eppm_pull_now(p: dict, projects: list[str], include_baseline: bool = True, route: str = "auto") -> dict:
    """Pull into the working copy (the checks on pending changes are the caller's)."""
    from p6mcp import credentials
    from p6mcp.eppm import rest, sql
    if route not in _eppm_store.ROUTES:
        raise ValueError(f"route must be one of {list(_eppm_store.ROUTES)}")
    spw = None
    if route == "sql" or (route == "auto" and p.get("sql")):
        if not p.get("sql"):
            raise ValueError("This server has no read-only SQL account: use route='rest', or add the account in MCP "
                             "for Primavera P6 Settings (Database).")
        driver_ok = sql.driver_module(p["sql"]["driver"]) is not None
        if route == "sql" and not driver_ok:
            raise ValueError(f"The {sql.DRIVERS[p['sql']['driver']]} driver is not installed: install it from MCP "
                             "for Primavera P6 Settings (Database, Test connection), or use route='rest'.")
        if driver_ok:
            spw = _eppm_secret(credentials.eppm_sql_key(p["name"]), "SQL password")
        route = "sql" if driver_ok else "rest"
    elif route == "auto":
        route = "rest"
    pw = _eppm_secret(credentials.eppm_key(p["name"]), "P6 password")
    try:
        m = _eppm_store.pull(p, projects, pw, spw, include_baseline, route)
    except (rest.EppmError, sql.SqlError, _eppm_store.PullError) as e:
        raise ValueError(str(e)) from None
    except OSError as e:
        raise ValueError(f"The working copy could not be replaced (is another tool reading it?): {e}") from None
    _cols_cache.clear()
    _schema_cache.clear()
    _refresh()
    return {"server": p["name"], "pulled_at": m["pulled_at"], "seconds": m["seconds"], "route": m["route"],
            "projects": m["projects"], "rows": m["rows"], "warnings": m["warnings"],
            "next": "Every tool now reads the working copy. Edits change the copy only (p6_eppm_status lists them) "
                    "until p6_eppm_push."}


def _srv_time(v: Any) -> str:
    """A server LastUpdateDate as 'YYYY-MM-DD HH:MM:SS' on the server's clock (offset dropped, as the filters take it)."""
    return str(v or "").replace("T", " ")[:19]


def _eppm_pushed_stamps(sess, sent: list[dict]) -> dict[str, str]:
    """{activity ObjectId: server LastUpdateDate} read back just after a push, for the activities its calls touched
    (updated ones, both ends of a relationship, the activity of an assignment). p6_eppm_status leaves an activity
    out of the server changes while its LastUpdateDate is still this one. A read failure only loses that filter."""
    from p6mcp.eppm import rest
    ids: set[int] = set()
    for x in sent:
        if x["table"] == "TASK":
            ids.add(int(x["object_id"]))
        for k in ("task_id", "pred_task_id"):
            v = (x["change"].get("refs") or {}).get(k)
            if v is not None:
                ids.add(int(v))
    out: dict[str, str] = {}
    todo = sorted(ids)
    try:
        for i in range(0, len(todo), 100):
            for r in sess.read("activity", ["ObjectId", "LastUpdateDate"],
                               " :or: ".join(f"ObjectId :eq: {x}" for x in todo[i:i + 100])):
                out[str(int(r["ObjectId"]))] = _srv_time(r.get("LastUpdateDate"))
    except rest.EppmError:
        return {}
    return out


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
def p6_eppm_status(check_server: bool = True) -> dict:
    """P6 EPPM mode: the working copy (server, pull time, route, projects, rows, warnings) and, with
    check_server, how many activities of each pulled project someone changed on the server since the pull
    (one read per project: pull again to bring them in); the user's own pushes are counted apart."""
    p = _eppm_profile()
    m = _state.EPPM_MANIFEST
    out: dict[str, Any] = {"server": p["name"], "address": p["base_url"], "pulled": m is not None}
    if m is None:
        out["next"] = "Nothing pulled yet: list the projects with p6_eppm_projects, then p6_eppm_pull."
        return out
    out.update(working_copy=_eppm_store.store_path(p["name"]), pulled_at=m["pulled_at"], route=m["route"],
               projects=m["projects"], rows=m["rows"], warnings=m["warnings"])
    from p6mcp.eppm import pending
    changes = _eppm_pending()
    out["pending"] = {"count": len(changes), "by_table": pending.summary(changes),
                      "changes": [{k: v for k, v in c.items() if k not in ("entries", "rollups")}
                                  for c in changes[:50]],
                      "truncated": len(changes) > 50}
    if m.get("pushes"):
        out["last_push"] = m["pushes"][-1]
    if check_server and m.get("server_stamp"):
        from p6mcp.eppm import rest
        stamp = m["server_stamp"].replace("T", " ")
        mine = m.get("pushed_activities") or {}
        changed, yours = {}, {}
        try:
            with _eppm_session(p) as s:
                for x in m["projects"]:
                    rows = s.read("activity", ["ObjectId", "LastUpdateDate"],
                                  f"ProjectObjectId :eq: {int(x['object_id'])} :and: LastUpdateDate:gt:'{stamp}'")
                    # the server keeps fractions of a second: the activity whose change is the stamp matches gt
                    rows = [r for r in rows if _srv_time(r.get("LastUpdateDate")) > stamp]
                    own = [r for r in rows if str(int(r["ObjectId"])) in mine
                           and _srv_time(r.get("LastUpdateDate")) <= mine[str(int(r["ObjectId"]))]]
                    changed[x["project_id"]] = len(rows) - len(own)
                    yours[x["project_id"]] = len(own)
            out["changed_on_server"] = {
                "since": m["pulled_at"], "since_server_clock": stamp, "activities": changed, "pushed_by_you": yours,
                "note": "activities = changed on the server by someone else since the pull (since: the pull, on this "
                        "PC's clock; since_server_clock: where the server comparison starts, on the server's clock). "
                        "pushed_by_you = activities your own pushes changed: the working copy already has them."}
        except rest.EppmError as e:
            out["changed_on_server"] = {"error": str(e)}
    return out


def _eppm_pushed_projects(changes: list[dict], m: dict) -> list[str]:
    """Project IDs of the pulled projects the pending changes touch (a relationship: both ends' projects), in the
    working copy's order."""
    by_oid = {int(x["object_id"]): x["project_id"] for x in m.get("projects", [])}
    by_name = {str(x["project_id"]).lower(): x["project_id"] for x in m.get("projects", [])}
    hit: set[str] = set()
    for c in changes:
        for oid in (c.get("proj_id"), (c.get("refs") or {}).get("pred_proj_id")):
            if oid is not None and str(oid).lstrip("-").isdigit() and int(oid) in by_oid:
                hit.add(by_oid[int(oid)])
        if c.get("project") and str(c["project"]).lower() in by_name:
            hit.add(by_name[str(c["project"]).lower()])
    return [x["project_id"] for x in m.get("projects", []) if x["project_id"] in hit]


def _eppm_targets(sess, names: list[str]) -> list[dict]:
    """The server's project rows (ObjectId, Id, DataDate) of these Project IDs; ValueError for one this user cannot
    read."""
    listed = {str(r.get("Id", "")).lower(): r for r in sess.read("project", ["ObjectId", "Id", "DataDate"])}
    missing = [n for n in names if n.lower() not in listed]
    if missing:
        raise ValueError(f"Not found among the projects this P6 user can read: {', '.join(missing)}")
    return [listed[n.lower()] for n in names]


def _eppm_schedule_lines(p: dict, targets: list[dict], dd: Optional[str], lead: str = "Schedule") -> list[str]:
    return [f"{lead} {t['Id']} on {p['name']} (data date "
            + (f"{(t.get('DataDate') or '')[:16].replace('T', ' ')} -> {dd[:16].replace('T', ' ')}" if dd
               else f"{(t.get('DataDate') or '?')[:16].replace('T', ' ')}, unchanged") + ")" for t in targets]


def _eppm_scheduled(p: dict, targets: list[dict], job: dict, dd: Optional[str], plan: list[str], pull_after: bool,
                    tool: str) -> dict:
    """The result of a finished schedule job: its log in the edit log, then the pull when asked (its failure is
    reported, not raised: the server was scheduled). Shared by p6_eppm_schedule and p6_eppm_push(schedule=True)."""
    from p6mcp.eppm import jobs
    out = {"applied": job["finished"], "server": p["name"], "projects": [t["Id"] for t in targets],
           "job": {k: job[k] for k in ("job_id", "status", "seconds")}, "log_facts": jobs.log_facts(job["log"]),
           "log": job["log"], "log_truncated": job["log_truncated"]}
    try:
        _log_write([{"ts": _log_ts(), "commit": uuid.uuid4().hex[:12], "db": os.path.basename(_state.DB_PATH or "eppm"),
                     "tool": tool, "operation": "schedule_eppm", "project": ", ".join(out["projects"]),
                     "proj_id": None, "table": None, "keys": {"server": p["name"]}, "before": None,
                     "after": {"job": out["job"], "new_data_date": dd}, "summary": "; ".join(plan)}])
    except OSError:
        pass
    if not job["finished"]:
        out["next"] = (f"The schedule job ended as {job['status']!r}: read the log, fix the cause in P6 EPPM, then "
                       "schedule again (p6_eppm_schedule).")
        return out
    if not pull_after:
        out["next"] = "Scheduled on the server. Pull again (p6_eppm_pull) to read the new dates."
        return out
    waiting = _eppm_pending()
    if waiting:  # a pull would drop them
        out["pull"] = {"skipped": f"{len(waiting)} local change(s) are still pending: the working copy was not "
                                  "pulled again."}
        out["next"] = "Scheduled on the server. Push the pending changes, then pull again to read the new dates."
        return out
    m = _state.EPPM_MANIFEST or {}
    try:
        pulled = _eppm_pull_now(p, sorted({x["project_id"] for x in m.get("projects", [])} | set(out["projects"])),
                                route=m.get("route") if m.get("route") in ("sql", "rest") else "auto")
    except ValueError as e:
        out["pull"] = {"error": str(e)}
        out["next"] = "Scheduled on the server, but the pull failed: pull again (p6_eppm_pull) to read the new dates."
        return out
    out["pull"] = {k: pulled[k] for k in ("pulled_at", "seconds", "route", "rows", "warnings")}
    out["next"] = "Scheduled on the server and pulled again: the tools read P6's new dates."
    return out


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
def p6_eppm_push(schedule: bool = False, new_data_date: Optional[DateTimeStr] = None, pull_after: bool = True,
                 confirm: bool = False) -> dict:
    """P6 EPPM mode: send the pending changes of the working copy to the server (see p6_eppm_status).
    The dry run reads every changed object on the server and lists the calls; a change made on the
    server since the pull is a conflict and refuses the push (pull again, or drop the local change).
    Calls go one object at a time (relationship deletes, activity updates, new relationships,
    relationship updates, assignments) and the first failure stops the push: the result and the journal
    say what reached the server, and a new push sends the rest.
    schedule: once every call has succeeded, also schedule the pushed projects on the server with P6's
    own scheduler (as p6_eppm_schedule: new_data_date moves the data date, YYYY-MM-DD [HH:MM]; pull_after
    pulls them again so the tools read the new dates); the same confirmation covers both steps, and a push
    that fails or has a conflict schedules nothing. Without schedule the server is not rescheduled: dates
    move when the project is scheduled in P6 EPPM (or with p6_eppm_schedule)."""
    from p6mcp.eppm import jobs
    from p6mcp.eppm import push as _push
    from p6mcp.eppm import rest
    p = _eppm_profile()
    m = _state.EPPM_MANIFEST
    if not m:
        raise ValueError("Nothing pulled yet: there is nothing to push.")
    if new_data_date and not schedule:
        raise ValueError("new_data_date moves the data date of a schedule: pass schedule=True with it.")
    dd = _parse_dt(new_data_date, "new_data_date")[0].strftime("%Y-%m-%dT%H:%M:%S") if new_data_date else None
    changes = _eppm_pending()
    if not changes:
        return {"dry_run": not confirm, "would_refuse": True, "refuse_reasons": ["No pending change to push."],
                "would_do": ["Nothing to send."]} if not confirm else {"applied": False,
                                                                    "error": "No pending change to push."}
    names = _eppm_pushed_projects(changes, m) if schedule else []
    if schedule and not names:
        raise ValueError("The pending changes name no pulled project to schedule: push without schedule, then use "
                         "p6_eppm_schedule.")
    try:
        with _eppm_session(p) as sess:
            pl = _push.plan(sess, changes, _eppm_store.LOCAL_ID_BASE)
            targets = _eppm_targets(sess, names) if schedule else []
            sched_plan = _eppm_schedule_lines(p, targets, dd, "After the push, schedule") + (
                ["Then pull them again into the working copy"] if pull_after else []) if schedule else []
            view = {"calls": len(pl["calls"]),
                    "already_done": [{"what": x["label"], "why": x["reason"]} for x in pl["already_done"]],
                    "conflicts": [{"what": x["label"], "why": x["reason"], "id": x["change"].get("id")}
                                  for x in pl["conflicts"]],
                    "unsupported": [f"{c['table']} {c['action']}" for c in pl["unsupported"]],
                    "not_sent_fields": {x["label"]: x["not_sent"] for x in pl["calls"] if x.get("not_sent")}}
            reasons = ([f"conflict: {x['what']}: {x['why']}" for x in view["conflicts"]]
                       + [f"not supported by the push: {u}" for u in view["unsupported"]])
            mismatch = _plan_gate(pl["plan"] + sched_plan, confirm)
            if not confirm:
                out = {"dry_run": True, "server": p["name"], "would_do": pl["plan"] + sched_plan, **view}
                if schedule:
                    out["schedule"] = {"projects": [t["Id"] for t in targets], "new_data_date": dd,
                                       "pull_after": pull_after,
                                       "note": "Runs only when every call of the push succeeds. P6 rewrites the "
                                               "early and late dates and the float of every activity of these "
                                               "projects on the server."}
                if reasons:
                    out.update(would_refuse=True, refuse_reasons=reasons)
                return out
            if mismatch:
                return {"applied": False, "error": mismatch}
            if reasons:
                return {"applied": False, "error": "The push is refused: " + "; ".join(reasons)}
            journal = os.path.join(_eppm_store.store_dir(p["name"]), f"push_{datetime.now():%Y%m%d_%H%M%S}.jsonl")
            res = _push.apply(sess, pl["calls"], journal)
            mine = _eppm_pushed_stamps(sess, res["sent"])
    except rest.EppmError as e:
        raise ValueError(str(e)) from None
    # what reached the server (or was there already) no longer counts as pending; new relationships take their ids
    created = dict(res["created"])
    created.update({x["object_id"]: x["server_id"] for x in pl["already_done"] if x.get("server_id")})
    if created:
        with closing(_connect(readonly=False)) as con:
            for local, server_id in created.items():
                con.execute("UPDATE TASKPRED SET task_pred_id = ? WHERE task_pred_id = ?", (server_id, local))
            con.commit()
    done = [e for x in res["sent"] + pl["already_done"] for e in x["change"]["entries"]]
    record = {"at": datetime.now().isoformat(timespec="seconds"), "sent": len(res["sent"]),
              "already_done": len(pl["already_done"]), "failed": (res["failed"] or {}).get("label"),
              "journal": journal}
    _eppm_store.update_manifest(p["name"], pushed_entries=done, push=record, pushed_activities=mine)
    try:
        _log_write([{"ts": _log_ts(), "commit": uuid.uuid4().hex[:12], "db": os.path.basename(_state.DB_PATH),
                     "tool": "p6_eppm_push", "operation": "push_eppm", "project": None, "proj_id": None,
                     "table": None, "keys": {"server": p["name"]}, "before": None, "after": record,
                     "summary": f"pushed {len(res['sent'])} change(s) to {p['name']}"}])
    except OSError:
        pass
    _refresh()
    out = {"applied": res["failed"] is None, "server": p["name"], "sent": [x["label"] for x in res["sent"]],
           "already_done": [x["label"] for x in pl["already_done"]], "journal": journal,
           "new_ids": {str(k): v for k, v in created.items()},
           "next": "The server is not rescheduled: schedule the project in P6 EPPM (or with p6_eppm_schedule), then "
                   "pull it again to read the new dates."}
    if res["failed"]:
        out["failed"] = {"what": res["failed"]["label"], "error": res["failed"].get("error")}
        out["next"] = ("The push stopped at the first failure; the changes listed in sent reached the server. Fix "
                       "the cause, then push again: only the rest is sent.")
        if schedule:
            out["schedule"] = {"applied": False, "projects": [t["Id"] for t in targets],
                               "skipped": "The push did not complete: nothing was scheduled."}
        return out
    if schedule:
        try:
            with _eppm_session(p) as sess:
                job = jobs.schedule(sess, [int(t["ObjectId"]) for t in targets], dd)
        except (ValueError, jobs.JobError) as e:  # the push is done: report, never raise
            out["schedule"] = {"applied": False, "projects": [t["Id"] for t in targets], "error": str(e)}
            out["next"] = ("Pushed, but the schedule could not run on the server: read schedule.error, then schedule "
                           "with p6_eppm_schedule.")
            return out
        out["schedule"] = _eppm_scheduled(p, targets, job, dd, sched_plan, pull_after, "p6_eppm_push")
        out["next"] = "Pushed. " + out["schedule"].pop("next")
    return out


def _eppm_revert(con, c: dict) -> dict:
    """Give one pending change's object back the values it had at the pull, on an open write connection, as an
    ordinary logged edit (same operations as the edit tools): the netting of pending.net then cancels the change.
    Returns {plan, log, projects}."""
    t, act, keys, refs, lb = c["table"], c["action"], c["keys"], c.get("refs") or {}, c.get("labels") or {}
    fields = c.get("fields") or {}
    stamp = lambda table, create=False: _stamp_cols(con, table, _now_str(con), create=create)  # noqa: E731
    if t == "TASK" and act == "update":
        tid = int(keys["task_id"])
        row = con.execute("SELECT * FROM TASK WHERE task_id = ?", (tid,)).fetchone()
        if row is None:
            raise ValueError(f"activity {lb.get('task_code', tid)} is no longer in the working copy")
        cols = [k for k in fields if k in _cols(con, "TASK")]
        sets = {k: fields[k]["pulled"] for k in cols}
        before = {k: row[k] for k in cols}
        full = {**sets, **{k: v for k, v in stamp("TASK").items() if k in _cols(con, "TASK")}}
        con.execute(f"UPDATE TASK SET {', '.join(k + '=?' for k in full)} WHERE task_id=?", [*full.values(), tid])
        plan = f"activity {lb.get('task_code', tid)}: " + ", ".join(f"{k} {before[k]!r} -> {sets[k]!r}" for k in cols)
        return {"plan": plan, "projects": [c.get("proj_id")],
                "log": [{"operation": "update_activity", "proj_id": c.get("proj_id"), "table": "TASK",
                         "keys": {"task_id": tid, "task_code": lb.get("task_code")}, "before": before,
                         "after": sets, "summary": "discard: " + plan}]}
    if t == "TASKRSRC" and act == "update":
        rid, tid = int(keys["taskrsrc_id"]), refs.get("task_id")
        row = con.execute("SELECT * FROM TASKRSRC WHERE taskrsrc_id = ?", (rid,)).fetchone()
        if row is None:
            raise ValueError(f"assignment {rid} is no longer in the working copy")
        tid = int(tid if tid is not None else row["task_id"])
        cols = [k for k in fields if k in _cols(con, "TASKRSRC")]
        sets = {k: fields[k]["pulled"] for k in cols}
        before = {k: row[k] for k in cols}
        full = {**sets, **{k: v for k, v in stamp("TASKRSRC").items() if k in _cols(con, "TASKRSRC")}}
        con.execute(f"UPDATE TASKRSRC SET {', '.join(k + '=?' for k in full)} WHERE taskrsrc_id=?", [*full.values(), rid])
        trow = con.execute("SELECT * FROM TASK WHERE task_id = ?", (tid,)).fetchone()
        tbefore, tafter = {}, {}
        for col, v in (c.get("rollups") or {}).items():  # the activity totals lose this assignment's delta only
            if trow is None or col not in _cols(con, "TASK"):
                continue
            delta = float(v["local"] or 0) - float(v["pulled"] or 0)
            tbefore[col] = trow[col]
            tafter[col] = float(trow[col] or 0) - delta
        if tafter:
            con.execute(f"UPDATE TASK SET {', '.join(k + '=?' for k in tafter)} WHERE task_id=?", [*tafter.values(), tid])
        who = lb.get("resource") or lb.get("role") or rid
        plan = f"assignment {lb.get('task_code', tid)} {who}: " + ", ".join(
            f"{k} {before[k]!r} -> {sets[k]!r}" for k in cols)
        return {"plan": plan, "projects": [],
                "log": [{"operation": "update_assignment", "proj_id": c.get("proj_id"), "table": "TASKRSRC",
                         "keys": {"taskrsrc_id": rid, "task_id": tid, "task_code": lb.get("task_code"),
                                  "resource": lb.get("resource"), "role": lb.get("role")},
                         "before": {**before, **{f"TASK.{k}": v for k, v in tbefore.items()}},
                         "after": {**sets, **{f"TASK.{k}": v for k, v in tafter.items()}},
                         "summary": "discard: " + plan}]}
    if t == "TASKPRED":
        pid = int(keys["task_pred_id"])
        link = f"relationship {lb.get('predecessor', refs.get('pred_task_id'))} -> {lb.get('successor', refs.get('task_id'))}"
        lkeys = {"task_pred_id": pid, "task_id": refs.get("task_id"), "pred_task_id": refs.get("pred_task_id"),
                 "successor": lb.get("successor"), "predecessor": lb.get("predecessor"),
                 "pred_proj_id": refs.get("pred_proj_id")}
        projects = [c.get("proj_id"), refs.get("pred_proj_id")]
        row = con.execute("SELECT * FROM TASKPRED WHERE task_pred_id = ?", (pid,)).fetchone()
        if act == "create":  # added locally: removed
            if row is not None:
                con.execute("DELETE FROM TASKPRED WHERE task_pred_id = ?", (pid,))
            before = None if row is None else {k: row[k] for k in REL_LOG_COLS}
            plan = f"{link}: added locally, removed"
            return {"plan": plan, "projects": projects,
                    "log": [{"operation": "delete_relationship", "proj_id": c.get("proj_id"), "table": "TASKPRED",
                             "keys": lkeys, "before": before, "after": None, "summary": "discard: " + plan}]}
        if act == "update":
            if row is None:
                raise ValueError(f"{link} is no longer in the working copy")
            cols = [k for k in fields if k in _cols(con, "TASKPRED")]
            sets = {k: fields[k]["pulled"] for k in cols}
            before = {k: row[k] for k in cols}
            full = {**sets, **{k: v for k, v in stamp("TASKPRED").items() if k in _cols(con, "TASKPRED")}}
            con.execute(f"UPDATE TASKPRED SET {', '.join(k + '=?' for k in full)} WHERE task_pred_id=?",
                        [*full.values(), pid])
            plan = f"{link}: " + ", ".join(f"{k} {before[k]!r} -> {sets[k]!r}" for k in cols)
            return {"plan": plan, "projects": projects,
                    "log": [{"operation": "update_relationship", "proj_id": c.get("proj_id"), "table": "TASKPRED",
                             "keys": lkeys, "before": before, "after": sets, "summary": "discard: " + plan}]}
        if act == "delete":  # deleted locally: put back with its server id
            vals = {k: (fields.get(k) or {}).get("pulled") for k in REL_LOG_COLS}
            if row is not None:
                raise ValueError(f"{link}: id {pid} is in use again in the working copy")
            if None in (refs.get("task_id"), refs.get("pred_task_id"), vals["pred_type"]):
                raise ValueError(f"{link}: the edit log lacks what is needed to put it back (pull again)")
            same = con.execute("SELECT task_pred_id FROM TASKPRED WHERE task_id=? AND pred_task_id=? AND pred_type=?"
                               + _alive(con, "TASKPRED", "TASKPRED"),
                               (refs["task_id"], refs["pred_task_id"], vals["pred_type"])).fetchone()
            if same is not None:
                raise ValueError(f"{link}: a relationship of this type links these activities again in the working "
                                 f"copy (TASKPRED:{same[0]}): drop that change first")
            row = {"task_pred_id": pid, "task_id": refs["task_id"], "pred_task_id": refs["pred_task_id"],
                   "proj_id": c.get("proj_id"), "pred_proj_id": refs.get("pred_proj_id") or c.get("proj_id"),
                   "pred_type": vals["pred_type"], "lag_hr_cnt": vals["lag_hr_cnt"] or 0.0, **stamp("TASKPRED", True)}
            row = {k: v for k, v in row.items() if k in _cols(con, "TASKPRED")}
            con.execute(f"INSERT INTO TASKPRED ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                        list(row.values()))
            plan = f"{link}: deleted locally, put back ({REL_TYPES_REV.get(vals['pred_type'], vals['pred_type'])})"
            return {"plan": plan, "projects": projects,
                    "log": [{"operation": "add_relationship", "proj_id": c.get("proj_id"), "table": "TASKPRED",
                             "keys": lkeys, "before": None, "after": {k: row.get(k) for k in REL_LOG_COLS},
                             "summary": "discard: " + plan}]}
    raise ValueError(f"{c.get('id')}: a {t} {act} cannot be dropped (pull again)")


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_eppm_discard(changes: list[str], confirm: bool = False) -> dict:
    """P6 EPPM mode: drop pending changes of the local working copy, by their id in p6_eppm_status
    (pending.changes[].id, also given with each conflict of a p6_eppm_push dry run), or ["all"]. Each object
    gets back the values it had at the pull and stops counting as pending; the other changes stay pending.
    The server is not touched. Use it after a push conflict on one change, instead of a new pull that drops
    every change. Dry run unless confirm=True."""
    _eppm_profile()
    if not _state.EPPM_MANIFEST:
        raise ValueError("Nothing pulled yet: there is nothing to discard.")
    want = list(dict.fromkeys(str(x).strip() for x in (changes or []) if str(x).strip()))
    if not want:
        raise ValueError("Name the changes to drop: their id in p6_eppm_status (pending.changes), or ['all'].")
    pend = _eppm_pending()
    by_id = {c["id"]: c for c in pend}
    if [w.lower() for w in want] == ["all"]:
        sel = pend
    else:
        unknown = [w for w in want if w not in by_id]
        if unknown:
            ids = ", ".join(list(by_id)[:20]) or "none"
            raise ValueError(f"Not a pending change: {', '.join(unknown)}. Pending: {ids}.")
        sel = [by_id[w] for w in want]
    if not sel:
        return {"applied": False, "error": "No pending change to drop."}
    with _write_session(confirm) as (con, backup):
        plan, log, projects = [], [], set()
        for c in sel:
            r = _eppm_revert(con, c)
            plan.append(f"  {c['id']}: {r['plan']}")
            log += r["log"]
            projects |= {x for x in r["projects"] if x is not None}
        left = len(pend) - len(sel)
        plan = ([f"Drop {len(sel)} pending change(s) from the working copy (the server is not touched); "
                 f"{left} stay pending"] + plan)
        if backup:
            plan.append(f"Backup: {backup}")
        res = _commit(con, plan, confirm, projects=tuple(projects), log=log)
    if confirm and res.get("applied"):
        res["next"] = ("Dropped from the working copy: these objects hold their pulled values again. "
                       "p6_eppm_status lists what is still pending.")
    return res


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
def p6_eppm_schedule(projects: Optional[list[str]] = None, new_data_date: Optional[DateTimeStr] = None,
                     pull_after: bool = True, confirm: bool = False) -> dict:
    """P6 EPPM mode: schedule projects on the server with P6's own scheduler (F9 in P6), wait for the job,
    return P6's scheduling log, then pull them again so the tools read the new dates.
    projects: Project IDs (default: the pulled ones). new_data_date: move the data date (YYYY-MM-DD
    [HH:MM]); omitted, the data date stays. It rewrites the dates of every activity on the server: ask the
    user first. With pull_after, it is refused while local changes are pending (push them first)."""
    from p6mcp.eppm import jobs, rest
    p = _eppm_profile()
    m = _state.EPPM_MANIFEST or {}
    names = [str(x).strip() for x in (projects or [x["project_id"] for x in m.get("projects", [])]) if str(x).strip()]
    if not names:
        raise ValueError("Name the projects to schedule (Project IDs, see p6_eppm_projects).")
    dd = None
    if new_data_date:
        dd = _parse_dt(new_data_date, "new_data_date")[0].strftime("%Y-%m-%dT%H:%M:%S")
    waiting = _eppm_pending() if pull_after else []
    try:
        with _eppm_session(p) as sess:
            targets = _eppm_targets(sess, names)
            plan = _eppm_schedule_lines(p, targets, dd)
            if pull_after:
                plan.append("Then pull them again into the working copy")
            mismatch = _plan_gate(plan, confirm)
            if not confirm:
                out = {"dry_run": True, "would_do": plan,
                       "note": "P6 rewrites the early and late dates and the float of every activity of these "
                               "projects on the server."}
                if waiting:
                    out.update(would_refuse=True, refuse_reasons=[
                        f"{len(waiting)} local change(s) are pending: push them (p6_eppm_push) before scheduling, or "
                        "schedule with pull_after=False"])
                return out
            if mismatch:
                return {"applied": False, "error": mismatch}
            if waiting:
                return {"applied": False, "error": f"{len(waiting)} local change(s) are pending: push them first."}
            job = jobs.schedule(sess, [int(t["ObjectId"]) for t in targets], dd)
    except (rest.EppmError, jobs.JobError) as e:
        raise ValueError(str(e)) from None
    return _eppm_scheduled(p, targets, job, dd, plan, pull_after, "p6_eppm_schedule")


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


_register_prompts()


if __name__ == "__main__":
    _registry.mcp.run()
