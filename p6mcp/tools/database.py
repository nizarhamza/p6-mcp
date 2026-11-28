"""Database inspection tools: calendars and their check, schedule options, SQL, diagnostics."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime
from time import monotonic
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from p6mcp import config as _config
from p6mcp import pmdb as _pmdb
from p6mcp import policy as _policy
from p6mcp import shop as _shop
from p6mcp.eppm import store as _eppm_store

from p6mcp.core import state as _state
from p6mcp.core.state import _cols_cache, _engine_cache_clear, _refresh, _schema_cache, _shop_on
from p6mcp.core.constants import (
    CAL_CHECK_KINDS, CAL_CHECK_YEARS, CAL_EXC_EACH, CAL_EXC_PAGE, CAL_EXC_ROWS, MAX_ROWS, SEVERITY_RANK, SQL_COUNT_CAP,
    SQL_COUNT_SECONDS, SQL_PAGE_BYTES, SQL_QUERY_SECONDS,
)
from p6mcp.registry import (
    CalCheckKinds, DateStr, Limit, LISTED, Offset, _plan_gate, ProjectId, SERVER_VERSION, SourceKind, _tool,
    TOOL_HINTS, Year,
)
from p6mcp.core.db import (
    _alive, _backup_dir, _check_page, _cols, _connect, _db_dt, _dict, _env, _p6_running, _page, _parse_dt, _project_id,
    _schema_info, _slice, _sql_authorizer, _write_refusal,
)
from p6mcp.core.netcache import _engine_cache_info
from p6mcp.core.props import _db_task_code, _lag_rule, _sched_diff, _sched_options, _xer_project, _xer_sched
from p6mcp.core.calendar import _cal_row, CAL_WINDOW_DAYS, _calendar, _calendars, _week_text, WEEKDAYS
from p6mcp.core.editlog import _log_dir
from p6mcp.core.reporting import _cal_check_read, _diag_schema, _holidays, _week_diff
from p6mcp.core.xer import _xer_read
from p6mcp.core.dashboard import _openpyxl_version


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
    whether P6 is running, the backup and edit log folders, the network cache of the CPM engine
    (engine_cache: on or off, projects kept, hits and misses) and data_source (the source in use, the saved
    choice, and since when this server process was switched with p6_use_database). Works without a database."""
    st = _state.SETTINGS
    ov = _state.OVERRIDE
    saved = _config.load() if ov else st  # the tool list follows the saved settings, never a switch of this process
    now_listed = {t for t in TOOL_HINTS if _policy.listed(t, saved.tool_profile, saved.allow_writes, _shop_on(saved))}
    out: dict[str, Any] = {
        "server_version": SERVER_VERSION,
        "settings": {"source": st.source, "config_file": st.config_path, "config_error": st.config_error,
                     "config_migrated": list(st.migrated), "database": st.db_name, "db_path": _state.DB_PATH or None,
                     "tool_profile": st.tool_profile, "allow_writes": st.allow_writes,
                     "backup_before_write": st.backup_before_write, "backup_scope": st.backup_scope,
                     "backup_retention": st.backup_retention, "block_when_p6_running": st.block_when_p6_running},
        "data_source": {"active": _choice(st), "saved": _choice(saved), "switched": ov is not None,
                        "since": ov["since"] if ov else None, "transport": _state.TRANSPORT},
        "tools_listed": len(LISTED),
        "restart_required": now_listed != LISTED,
        "p6_running": _p6_running(),
        "openpyxl": _openpyxl_version(),  # p6_export_dashboard needs it (None: not installed)
        "shop": {"enabled": _shop_on(st), "catalog": _shop.REPO_URL},
        "engine_cache": _engine_cache_info(),
    }
    if ov:
        out["data_source"]["note"] = ("This server process was switched with p6_use_database: config.json and MCP for "
                                      "Primavera P6 Settings keep the saved choice, and a restart of the server, or "
                                      "p6_use_database(source=\"saved\"), returns to it.")
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
    if st.pmdb:
        prof = _config.pmdb_profile(st.raw, st.pmdb) or {}
        m = _state.PMDB_MANIFEST
        out["pmdb_mode"] = {
            "database": st.pmdb, "driver": prof.get("driver"), "server": prof.get("host"), "port": prof.get("port"),
            "service": prof.get("service"), "schema": prof.get("schema") or None, "login": prof.get("user"),
            "tls": bool(prof.get("tls")), "selection": {"projects": prof.get("projects", []),
                                                        "eps": prof.get("eps", []),
                                                        "baselines": prof.get("baselines", "current")},
            "local_copy": _pmdb.store_path(st.pmdb) if m else None, "copy": _pmdb.summary(m, prof),
            "rows": (m or {}).get("rows"), "warnings": (m or {}).get("warnings", [])[:10],
            "note": "Read only: the tools read a local copy made by p6_pmdb_refresh (only SELECT statements run on "
                    "the database server, never a write). Refresh it to see changes made in P6 since copied_at."}
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


# ----------------------------------------------------------------- data source of this server process (P2)
# p6_use_database layers a data source selection over config.json for this server process only (_state.OVERRIDE,
# read by _refresh at every call): the file and the Settings app keep their choice, a restart returns to it.

SOURCE_LABELS = {"database": "P6 Professional SQLite database", "xer": "XER files (read only)",
                 "eppm": "P6 EPPM server (local working copy)",
                 "pmdb": "P6 Professional on SQL Server or Oracle (local read-only copy)"}
READ_ONLY_SOURCES = ("xer", "pmdb")
ENV_WINS = ("P6_DB_PATH is set in the environment of this server (the MCP client's configuration): the environment "
            "wins over the settings, so this server always uses that database. Remove P6_DB_PATH from the client's "
            "configuration to choose the data source in MCP for Primavera P6 Settings.")


def _env_db() -> Optional[str]:
    v = os.environ.get("P6_DB_PATH", "").strip()
    return None if not v or "${" in v else os.path.abspath(v)


def _no_userinfo(url: str) -> str:
    """A server URL without a user name or password (a hand-written profile could hold them)."""
    try:
        parts = urlsplit(url or "")
        if parts.username is None and parts.password is None:
            return url or ""
        host = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    except ValueError:
        return ""
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _choice(st: _config.Settings) -> dict:
    """{source, profile} a Settings selects: a data source of config.json, 'environment' (P6_DB_PATH) or None."""
    if _env_db():
        return {"source": "environment", "profile": "P6_DB_PATH"}
    if st.source != "config":
        return {"source": None, "profile": None}
    source, profile = _config.source_choice(st.raw)
    return {"source": source, "profile": profile}


def _describe(c: dict) -> str:
    if c["source"] == "environment":
        return "the database of P6_DB_PATH (environment)"
    if c["source"] is None:
        return "no data source (no settings file)"
    return SOURCE_LABELS.get(c["source"], c["source"]) + (f" {c['profile']!r}" if c["profile"] is not None else "")


def _source_state() -> dict:
    """What this process uses now, the saved choice and the switch (since when)."""
    st, ov = _state.SETTINGS, _state.OVERRIDE
    saved = _config.load() if ov else st
    out = {"active": dict(_choice(st), db_path=_state.DB_PATH or None),
           "saved": _choice(saved), "switched": ov is not None, "since": ov["since"] if ov else None}
    if not _state.DB_PATH:
        out["active"]["problem"] = st.db_problem or _config.NO_DB_MESSAGE
    return out


def _source_entries(data: dict) -> list[tuple[str, Optional[str], dict]]:
    """(source, profile name, entry) of every data source set up in the settings (one XER file set at most)."""
    out = [("database", d.get("name"), d) for d in data.get("databases", [])]
    if data.get("xer_files"):
        out.append(("xer", None, {}))
    out += [("eppm", p.get("name"), p) for p in data.get("eppm_profiles", [])]
    out += [("pmdb", p.get("name"), p) for p in data.get("pmdb_profiles", [])]
    return out


def _source_target(source: str, name: Optional[str], entry: dict, data: dict) -> dict:
    """What a profile points to (paths in full: a local tool; never a password) and its local copy."""
    if source == "database":
        return {"points_to": os.path.abspath(entry.get("path") or ""), "copy": None}
    if source == "xer":
        return {"points_to": [os.path.abspath(f) for f in data.get("xer_files", [])], "copy": None}
    if source == "eppm":
        m = _eppm_store.read_manifest(name) if name else None
        db = f" (database {entry['database']})" if entry.get("database") else ""
        return {"points_to": _no_userinfo(entry.get("base_url", "")) + db,
                "copy": {"path": _eppm_store.store_path(name), "pulled_at": m.get("pulled_at"),
                         "projects": [p.get("project_id") for p in m.get("projects", [])]} if m else None}
    m = _pmdb.read_manifest(name) if name else None
    where = f"{entry.get('host', '')}:{entry.get('port', '')} {entry.get('service', '')}".strip()
    schema = f" schema {entry['schema']}" if entry.get("schema") else ""
    return {"points_to": f"{entry.get('driver')} {where}{schema}",
            "copy": {"path": _pmdb.store_path(name), "copied_at": m.get("copied_at"),
                     "projects": [p.get("project_id") for p in m.get("projects", [])]} if m else None}


def _source_problem(source: str, name: Optional[str], entry: dict, data: dict) -> Optional[str]:
    """Why this profile cannot be switched to (None = it can)."""
    settings = "MCP for Primavera P6 Settings (Database)"
    if source == "xer":
        files = data.get("xer_files") or []
        if not data.get("xer_confirmed"):
            return f"The XER files were not loaded and checked: open {settings} and click Load."
        missing = [f for f in files if not os.path.isfile(f)]
        return f"XER file not found: {missing[0]}." if missing else None
    what = {"database": "Database", "eppm": "P6 EPPM server", "pmdb": "Database server"}[source]
    if not entry.get("confirmed"):
        return (f"{what} {name!r} was set up but its connection was not tested and confirmed: open {settings}, click "
                "Test connection and confirm it.")
    if source == "database":
        path = os.path.abspath(entry.get("path") or "")
        return None if os.path.isfile(path) else f"Database {name!r}: the file was not found ({path})."
    if source == "eppm" and _eppm_store.read_manifest(name) is None:
        return (f"P6 EPPM server {name!r} has no working copy yet: pull projects first (make it the data source in "
                f"{settings}, then p6_eppm_pull).")
    if source == "pmdb" and _pmdb.read_manifest(name) is None:
        return (f"Database server {name!r} has no local copy yet: copy the projects first (Copy now in {settings}, "
                "or p6_pmdb_refresh while it is the saved data source).")
    return None


def _source_rows(data: dict, state: dict, env: bool) -> list[dict]:
    rows = []
    for source, name, entry in _source_entries(data):
        why = ENV_WINS if env else _source_problem(source, name, entry, data)
        key = (source, name)
        rows.append({"source": source, "kind": SOURCE_LABELS[source], "profile": name,
                     **_source_target(source, name, entry, data),
                     "confirmed": bool(data.get("xer_confirmed") if source == "xer" else entry.get("confirmed")),
                     "read_only": source in READ_ONLY_SOURCES,
                     "active": key == (state["active"]["source"], state["active"]["profile"]),
                     "saved": key == (state["saved"]["source"], state["saved"]["profile"]),
                     "switchable": why is None, "why_not": why})
    return rows


def _source_find(data: dict, source: str, profile: Optional[str]) -> tuple[str, Optional[str], dict]:
    found = [e for e in _source_entries(data) if e[0] == source]
    label = SOURCE_LABELS[source]
    if not found:
        raise ValueError(f"No {label} is set up in MCP for Primavera P6 Settings (Database): set it up and confirm it "
                         "there first.")
    if source == "xer":
        return found[0]
    names = [n for _s, n, _e in found]
    if profile is None or not str(profile).strip():
        if len(found) == 1:
            return found[0]
        raise ValueError(f"profile: name the {label} to use, one of {names}.")
    hit = [e for e in found if e[1] == profile] or [e for e in found if str(e[1]).lower() == profile.strip().lower()]
    if not hit:
        raise ValueError(f"No {label} named {profile!r}: the ones set up are {names} (call p6_use_database without "
                         "arguments to list them).")
    return hit[0]


def _source_writes(source: str, data: dict) -> str:
    if source in READ_ONLY_SOURCES:
        return "read only: every write tool is refused"
    if not data.get("allow_writes"):
        return "write tools refused (writes are turned off in the settings)"
    if source == "eppm":
        return "edits change the local working copy only (p6_eppm_push sends them to the server)"
    return "as the settings allow (two-step flow; a P6 schema that is not known or trusted is read only)"


def _source_notes() -> list[str]:
    st, kind = _state.SETTINGS, _choice(_state.SETTINGS)["source"]
    notes = [("This server process serves every client of the HTTP server: every later request of every connected "
              "client uses this source." if _state.TRANSPORT == "http" else
              "Other MCP clients and the remote access server run their own server process: they keep the saved "
              "choice."),
             "The tool list is the one the server started with: a tool the active source does not allow is refused "
             "when called (writes on a read-only source, P6 EPPM tools outside a P6 EPPM working copy).",
             "Dates and float still come from the last F9 stored in this source; staleness, backups and the edit log "
             "follow its database file."]
    if _state.DB_PATH and not _write_refusal() and not any(_policy.is_write(t) for t in LISTED):
        notes.append("Writes are allowed on this source, but the write tools were not offered when the server "
                     "started: save this source in MCP for Primavera P6 Settings and restart the MCP client to use them.")
    startup = _choice(_state.STARTUP)["source"]
    if kind != startup:
        notes.append("The server instructions your MCP client received at startup describe the source it started "
                     f"with ({SOURCE_LABELS.get(startup, startup)}).")
    if st.eppm:
        notes.append("P6 EPPM: p6_eppm_status lists the pending changes of this working copy.")
    return notes


@_tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_use_database(source: Optional[SourceKind] = None, profile: Optional[str] = None, confirm: bool = False) -> dict:
    """Switches the data source of THIS server process to another one already set up and confirmed in MCP for
    Primavera P6 Settings (Database): source database (a P6 SQLite file), xer (the XER files), eppm (a P6 EPPM
    working copy) or pmdb (a SQL Server / Oracle copy), profile = its name (optional when only one is set up).
    Without source: lists them (kind, what it points to, confirmed, local copy, switchable or why not, active now,
    saved choice) and changes nothing. source="saved" returns to the choice saved in config.json. Never saved:
    config.json and the Settings app keep their choice, and a restart of the server returns to it. Refused: a
    profile not confirmed, a missing file, a P6 EPPM or SQL Server / Oracle profile without a local copy, and any
    switch while P6_DB_PATH is set. The tool list does not change: a tool the new source does not allow (writes on
    a read-only source) is refused when called."""
    st, state, env = _state.SETTINGS, _source_state(), _env_db() is not None
    data, err = None, st.config_error
    if not err and st.source == "config":
        try:
            data = _config.read_file(st.config_path or _config.config_path())[0]
        except _config.ConfigError as e:
            err = str(e)
    if source is None:
        out = {**state, "profiles": _source_rows(data or {}, state, env), "transport": _state.TRANSPORT,
               "note": "Nothing changed. Switch with source= and profile= (a dry run first); the switch is for this "
                       "server process only, never saved."}
        if env:
            out["environment"] = ENV_WINS
        if err:
            out["config_error"] = err
        return out
    if env:
        raise ValueError(ENV_WINS)
    if data is None:
        raise ValueError(f"The settings cannot be read ({err}): fix them in MCP for Primavera P6 Settings first." if err
                         else "There is no settings file: set up a data source in MCP for Primavera P6 Settings first.")
    if source not in (*SOURCE_LABELS, "saved"):
        raise ValueError(f"source must be one of {[*SOURCE_LABELS, 'saved']}")
    active, saved = state["active"], state["saved"]
    if source == "saved":
        if _state.OVERRIDE is None:
            return {**state, "already_active": True,
                    "note": "This server process already uses the saved data source: nothing to switch back."}
        plan = [f"Return this server process to the saved data source: {_describe(saved)} (config.json)",
                f"Stop using {_describe(active)}, used since {state['since']} (p6_use_database)"]
        target, override = saved, None
    else:
        src, name, entry = _source_find(data, source, profile)
        why = _source_problem(src, name, entry, data)
        if why:
            raise ValueError(why + " Nothing was switched.")
        target = {"source": src, "profile": name}
        if (src, name) == (active["source"], active["profile"]):
            return {**state, "already_active": True,
                    "note": f"{_describe(target)} is already the data source of this server process."}
        tgt = _source_target(src, name, entry, data)
        where = tgt["copy"]["path"] if tgt["copy"] else tgt["points_to"]
        plan = [f"Use {_describe(target)} for this server process only: {where}",
                f"Writes: {_source_writes(src, data)}",
                f"config.json and MCP for Primavera P6 Settings keep the saved choice ({_describe(saved)}); a restart "
                "of the server, or p6_use_database(source=\"saved\"), returns to it"]
        override = (None if (src, name) == (saved["source"], saved["profile"]) else
                    {"source": src, "profile": name, "since": datetime.now().isoformat(timespec="seconds"),
                     "selection": _config.source_selection(src, name)})
    gate = _plan_gate(plan, confirm)
    if gate:
        return {"applied": False, "error": gate.replace("The database changed", "The settings changed")}
    if not confirm:
        return {"dry_run": True, "would_do": plan, **state}
    previous = _state.OVERRIDE
    _state.OVERRIDE = override
    now = _refresh()
    if not _state.DB_PATH and source != "saved":  # the source would leave this process without a database
        _state.OVERRIDE = previous
        _refresh()
        raise ValueError(f"{_describe(target)} cannot be used now: {now.db_problem or _config.NO_DB_MESSAGE} "
                         "Nothing was switched.")
    return {"applied": True, **_source_state(),
            "writes": (_write_refusal() or "allowed") if _state.DB_PATH else None,
            "notes": _source_notes()}


# ----------------------------------------------------------------- P6 Professional on SQL Server or Oracle

@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True})
def p6_pmdb_refresh() -> dict:
    """P6 Professional on SQL Server or Oracle: copy again, from the database server, the projects chosen in MCP
    for Primavera P6 Settings (Project IDs or an EPS, with their baselines) into the local read-only copy that
    every other tool reads. Only SELECT statements run on the server: nothing is ever written there. The previous
    copy is replaced once the new one is complete (kept when the copy fails). copied_at says when it was read."""
    from p6mcp import credentials
    st = _state.SETTINGS
    p = _config.pmdb_profile(st.raw, st.pmdb) if st.pmdb else None
    if p is None:
        raise ValueError("Not in P6 Professional SQL Server / Oracle mode: in MCP for Primavera P6 Settings (Database), "
                         "choose 'P6 Professional on SQL Server or Oracle', add the database, test the connection, "
                         "click Use this database, then Save.")
    try:
        pw = credentials.get(credentials.pmdb_key(p["name"]))
    except credentials.SecretStoreError as e:
        raise ValueError(str(e)) from None
    if not pw:
        raise ValueError("No password is stored for this database login: enter it in MCP for Primavera P6 Settings "
                         "(Database) and click Test connection.")
    try:
        m = _pmdb.copy(p, pw)
    except _pmdb.CopyError as e:
        raise ValueError(str(e)) from None
    _cols_cache.clear()
    _schema_cache.clear()
    _engine_cache_clear()
    _refresh()
    return {"database": p["name"], "copied_at": m["copied_at"], "seconds": m["seconds"],
            "p6_version": m["p6_version"].get("version"), "projects": m["projects"], "baselines": m["baselines"],
            "eps": m["eps"], "rows": m["rows"], "warnings": m["warnings"],
            "next": "Every tool now reads this copy. It is read only: make changes in P6, then refresh again."}
