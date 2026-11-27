"""Read tools: projects, WBS, activities, relationships, UDFs, critical path, float paths."""

from __future__ import annotations

import difflib
import math
import sqlite3
from collections import Counter
from typing import Any, Optional, get_args

from p6mcp.core.constants import (
    ACT_EXTRA, ACT_LINKS_CAP, _cstr_label, HPD_SQL, SUMMARY_TYPES, UDF_KINDS, UDF_PROJECT_TABLES,
)
from p6mcp.registry import (
    ActFields, ActivityId, CpMode, DateStr, Depth, DetailArg, Direction, Limit, Offset, PathNo, ProjectId, ProjOrder,
    Status, _tool, UdfFilters, UdfNames,
)
from p6mcp.core.db import (
    _act_from, _act_row, _act_view, _alive, _baseline_pid, _bound, _brief, _brief_on, _cols, _connect, _db_dt, _dict,
    _num, _page, _project_id, _slice, _task,
)
from p6mcp.core.props import _lag_info
from p6mcp.core.editlog import _schedule_state
from p6mcp.core.wbs_codes import _act_agg_sql, _code_filter, _eps_tree, _merge_agg, _name_sep, _rollup, _wbs_tree
from p6mcp.core.read import (
    _act_details, _critical_clause, _rel_page, _udf_filter, _udf_out, _udf_type, _udf_values, _wbs_of,
)
from p6mcp.core.reporting import _bl_planned_by_wbs


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
                    or [r for r, n in zip(rel, nodes[1:], strict=False) if n["depth"] == 1][:10])
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
        q += " AND t.task_name LIKE ?"
        args.append(f"%{name_contains}%")
    if code_prefix:
        q += " AND t.task_code LIKE ?"
        args.append(f"{code_prefix}%")
    if wbs_name_contains:
        q += " AND w.wbs_name LIKE ?"
        args.append(f"%{wbs_name_contains}%")
    if status:
        q += " AND t.status_code = ?"
        args.append(status)
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
            cond += " AND t.float_path = ?"
            args.append(int(path))
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
