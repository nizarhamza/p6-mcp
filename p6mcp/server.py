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

from p6mcp.core import state as _state
from p6mcp import registry as _registry

# Imported names other modules and the tests read from p6mcp.server.
from p6mcp import __version__ as _pkg_version  # noqa: F401
from p6mcp import config as _config  # noqa: F401
from p6mcp import confirm as _confirm  # noqa: F401
from p6mcp import instructions as _instructions  # noqa: F401
from p6mcp import policy as _policy  # noqa: F401
from p6mcp import skills as _skills  # noqa: F401
from p6mcp import schema as _schema  # noqa: F401
from p6mcp import shop as _shop  # noqa: F401
from p6mcp import xerstore as _xerstore  # noqa: F401
from p6mcp.eppm import store as _eppm_store  # noqa: F401
from p6mcp.registry import FastMCP, Field, ToolAnnotations, ToolError  # noqa: F401

# The modules of the server, lowest layer first; every name they define is re-exported here.
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
# The tool modules, in the order of the tool list: importing one registers its tools.
from p6mcp.tools.read import (  # noqa: F401  (re-exported)
    p6_critical_path, p6_float_paths, p6_get_activity, p6_get_relationships, p6_list_projects, p6_list_udfs,
    p6_list_wbs, p6_query_activities,
)
from p6mcp.tools.analysis import (  # noqa: F401  (re-exported)
    p6_dangling, p6_driving_path, p6_float_origin, p6_health_check, p6_health_check_compare, p6_progress_check,
    p6_redundant_links,
)
from p6mcp.tools.engine import p6_cpm_validate, p6_schedule_risk, p6_schedule_whatif  # noqa: F401  (re-exported)
from p6mcp.tools.compare import p6_compare_projects, p6_update_integrity  # noqa: F401  (re-exported)
from p6mcp.tools.reporting import (  # noqa: F401  (re-exported)
    p6_float_analysis, p6_group_summary, p6_lookahead, p6_milestones, p6_progress_curve, p6_project_summary,
    p6_resource_loading, p6_resource_summary, p6_trend, _trend_names,
)
from p6mcp.tools.database import (  # noqa: F401  (re-exported)
    p6_calendar_check, p6_diagnose, p6_list_calendars, p6_schedule_options, p6_sql_select,
)
from p6mcp.tools.writes import (  # noqa: F401  (re-exported)
    p6_add_relationship, p6_add_relationships, p6_delete_activity, p6_delete_relationship, p6_delete_relationships,
    p6_update_activities, p6_update_activity, p6_update_assignment, p6_update_assignments, p6_update_relationship,
    p6_update_relationships, p6_update_udf, p6_update_udfs,
)
from p6mcp.tools.admin import (  # noqa: F401  (re-exported)
    p6_backup_database, p6_create_eps, p6_delete_project, p6_edit_log, p6_list_backups, p6_list_eps, p6_restore_backup,
    p6_set_schedule_options,
)
from p6mcp.tools.xer import p6_export_xer, p6_import_xer, p6_inspect_xer  # noqa: F401  (re-exported)
from p6mcp.tools.export import p6_export_dashboard, p6_export_lookahead  # noqa: F401  (re-exported)
from p6mcp.tools.eppm import (  # noqa: F401  (re-exported)
    _eppm_pending, _eppm_profile, _eppm_pull_now, _eppm_pushed_projects, _eppm_pushed_stamps, _eppm_revert,
    _eppm_schedule_lines, _eppm_scheduled, _eppm_secret, _eppm_session, _eppm_targets, p6_eppm_discard,
    p6_eppm_projects, p6_eppm_pull, p6_eppm_push, p6_eppm_schedule, p6_eppm_status, _srv_time,
)
from p6mcp.tools.shop import (  # noqa: F401  (re-exported)
    p6_shop_get, p6_shop_install, p6_shop_search, _shop_entry, SHOP_ROW, _shop_settings, _skill_clients,
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


_register_prompts()


if __name__ == "__main__":
    _registry.mcp.run()
