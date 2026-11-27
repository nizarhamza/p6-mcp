"""Excel export tools: dashboard, look-ahead."""

from __future__ import annotations

import math
import os
from datetime import datetime
from typing import Optional, get_args

from p6mcp.core import state as _state
from p6mcp.core.constants import LA_AUTO_DAYS, LA_DAY_GRID_MAX, LA_EXPORT_CAP
from p6mcp.registry import (
    ActivityIds, BaselineDates, DateTimeStr, LaGrid, NonNegDays, Period, ProjectId, _tool, TrendProjects, WindowWeeks,
)
from p6mcp.core.db import _act_view, _alive, _all_rows, _baseline_pid, _cols, _connect, _parse_dt, _project_id
from p6mcp.core.calendar import _calendars
from p6mcp.core.editlog import _schedule_state
from p6mcp.core.wbs_codes import _wbs_tree
from p6mcp.core.analysis import HC_UNSCORED
from p6mcp.core.reporting import _la_query, _trend, _trend_series
from p6mcp.core.xer import _export_target
from p6mcp.core.dashboard import (
    DASH_ACT_CAP, _dash_book, _dash_brand, DASH_MILESTONES_MAX, _dash_read, DASH_WBS_ROWS, _la_book, _openpyxl,
)
from p6mcp.tools.read import p6_critical_path
from p6mcp.tools.analysis import p6_health_check
from p6mcp.tools.reporting import p6_progress_curve, p6_project_summary, _trend_names


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
