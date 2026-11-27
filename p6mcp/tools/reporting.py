"""Reporting tools: groups, look-ahead, S-curve, milestones, project summary, trend, float, resources."""

from __future__ import annotations

import functools
import math
import sqlite3
import statistics
from collections import Counter
from datetime import datetime
from typing import Mapping, Optional, get_args

from p6mcp.core.constants import _cstr_label, HARD_CSTR_SET, LOOKAHEAD_DAYS, STATUS_NAMES, SUMMARY_TYPES, TYPE_NAMES
from p6mcp.registry import (
    ActFields, BaselineDates, DateStr, DateTimeStr, DetailArg, FloatEdges, GroupBy, Level, Limit, Measure, MsCodes,
    MsOrder, NonNegDays, Offset, Period, ProjectId, ProjectIds, SampleSize, _tool, TrendMs, TrendProjects, WindowDays,
    WindowWeeks,
)
from p6mcp.core.db import (
    _act_from, _act_row, _alive, _all_rows, _baseline_pid, _brief, _brief_on, _check_page, _cols, _connect, _days,
    _db_dt, _dict, _env, _fmt_dt, _num, _page, _parse_dt, _prefer, _project_id, _slice,
)
from p6mcp.core.calendar import _calendars
from p6mcp.core.editlog import _schedule_state
from p6mcp.core.wbs_codes import _act_agg_sql, _code_filter, _code_tree, _code_type, _merge_agg, _wbs_tree
from p6mcp.core.read import _critical_clause
from p6mcp.core.engine import ENGINE_VERSION, _float_walk, GAP_TOL_H, _Net
from p6mcp.core.reporting import (
    _available, _code_list, _cost_ev, _cum, _curve_items, _ev_map, _float_bucket, _float_edges, _float_labels,
    _group_row, _la_query, LOAD_KIND_ORDER, _measure_rows, MS_VS, _period_edges, RSRC_KINDS, _rsrc_limits, _Spread,
    _spread_note, _summary, _trend, TREND_MAX, _trend_series, _value_at,
)
from p6mcp.tools.read import p6_list_projects


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
            "before 3.5.0), so it can differ a little from the period curve at the data date. Earned value (budget x "
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
                  "end": f"{b:%Y-%m-%d}"} for a, b in zip(edges, edges[1:], strict=False)],
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
