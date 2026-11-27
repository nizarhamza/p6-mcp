"""Reporting helpers: groups, look-ahead, S-curve spreads and earned value, milestones, cost, project
summary, trend, float buckets, resource loading, calendar check, diagnostics."""

from __future__ import annotations

import bisect
import functools
import math
import re
import sqlite3
import statistics
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Mapping, Optional, Sequence

from p6mcp.core import state as _state
from p6mcp.core.constants import FLOAT_EDGES, HPD_SQL, MAX_ROWS, SUMMARY_TYPES
from p6mcp.core.db import (
    _act_from, _alive, _baseline_pid, _cols, _connect, _db_dt, _dict, _fmt_dt, _parse_dt, _project_id,
)
from p6mcp.core.props import _blob_text, _p6_tree
from p6mcp.core.calendar import _Cal, _calendars, _shift_txt, WEEKDAYS
from p6mcp.core.editlog import _schedule_state
from p6mcp.core.wbs_codes import _code_filter, _rollup
from p6mcp.core.read import _critical_clause
from p6mcp.core.engine import CPM_TF_TOL_H
from p6mcp.core.analysis import _cmp_acts, _cmp_links, _dcma, _dcma_settings, HC_UNSCORED, LINK_KEY, _weighted_score
from p6mcp.core.explain import _cpt


# ----------------------------------------------------------------- reporting: groups, look-ahead, S-curve, summary

def _group_row(a: Mapping) -> dict:
    """p6_group_summary figures of merged _act_agg_sql aggregates (plus crit)."""
    r2 = lambda v: None if v is None else round(v, 2)  # noqa: E731
    return {**_rollup(a), "critical": a.get("crit") or 0, "max_tf_d": r2(a.get("tf_max")),
            "avg_tf_d": r2(a["tf_sum"] / a["tf_n"]) if a.get("tf_n") else None,
            "orig_dur_d": r2(a.get("orig_d")), "rem_dur_d": r2(a.get("rem_d"))}


def _la_query(con, pid: int, project: str, n: int, from_date: Optional[str], include_in_progress: bool,
              include_completed: bool, wbs_name_contains: Optional[str], code_type: Optional[str],
              code_value: Optional[str], include_child_codes: bool, critical_only: bool) -> dict:
    """The look-ahead of p6_lookahead and p6_export_lookahead, inside the caller's snapshot: sql (TASK rows t.* with
    hpd, wbs_name and the w_* category flags, ordered) and args, extra(row) (window, critical, fcst_start,
    fcst_finish, start_week, finish_week), counts() over every row, window, weeks, data_date (stored text), frm / to
    (datetimes), critical_definition, code_filter."""
    if not 1 <= n <= 366:
        raise ValueError("the window must be 1 to 366 days (days=, or weeks= up to 52)")
    frm = _parse_dt(from_date, "from_date")[0] if from_date else None
    dd = (con.execute("SELECT last_recalc_date FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone()[0]
          if "last_recalc_date" in _cols(con, "PROJECT") else None)
    frm = frm or _db_dt(dd)
    if frm is None:
        raise ValueError(f"{project} has no data date: pass from_date")
    to = frm + timedelta(days=n)
    cflt, cargs, cinfo = _code_filter(con, pid, code_type, code_value, include_child_codes)
    crit, crit_args, crit_def = _critical_clause(con, pid, "project", None)[:3]
    f, e = f"'{_fmt_dt(frm)}'", f"'{_fmt_dt(to)}'"  # strftime output: safe as SQL literals
    rs, rf = "COALESCE(t.restart_date, t.early_start_date)", "COALESCE(t.reend_date, t.early_end_date)"
    cat = {"in_progress": "t.status_code = 'TK_Active'",
           "starting": f"t.status_code = 'TK_NotStart' AND {rs} >= {f} AND {rs} < {e}",
           "finishing": f"t.status_code <> 'TK_Complete' AND {rf} >= {f} AND {rf} < {e}",
           "spanning": f"t.status_code = 'TK_NotStart' AND {rs} < {f} AND {rf} >= {e}",
           "completed": f"t.status_code = 'TK_Complete' AND t.act_start_date < {e} AND t.act_end_date >= {f}"}
    on = [k for k in cat if {"in_progress": include_in_progress, "completed": include_completed}.get(k, True)]
    inner = (_act_from(con) + f" AND t.task_type NOT IN {SUMMARY_TYPES}" + cflt
             + (" AND w.wbs_name LIKE ?" if wbs_name_contains else "") + " AND (" + " OR ".join(f"({cat[k]})" for k in on) + ")"
             + (f" AND t.status_code <> 'TK_Complete' AND COALESCE({crit}, 0)" if critical_only else ""))
    args = ([pid, *cargs] + ([f"%{wbs_name_contains}%"] if wbs_name_contains else [])
            + (list(crit_args) if critical_only else []))
    is_crit = f"(t.status_code <> 'TK_Complete' AND COALESCE({crit}, 0))"
    q = (f"SELECT t.*, {', '.join(f'({c}) AS w_{k}' for k, c in cat.items())}, {is_crit} AS w_crit, "
         f"COALESCE(t.act_start_date, {rs}) AS f_st, CASE WHEN t.status_code = 'TK_Complete' THEN t.act_end_date "
         f"ELSE {rf} END AS f_fin FROM ({inner}) t ORDER BY COALESCE(t.act_start_date, {rs}), {rf}, t.task_code, t.task_id")

    def week(v) -> Optional[int]:
        d = _db_dt(v)
        return int((d - frm).total_seconds() // (7 * 86400)) + 1 if d is not None and frm <= d < to else None

    def extra(r: sqlite3.Row) -> dict:
        return {"window": [k for k in cat if r[f"w_{k}"]], "critical": bool(r["w_crit"]), "fcst_start": r["f_st"],
                "fcst_finish": r["f_fin"], "start_week": week(r["f_st"]), "finish_week": week(r["f_fin"])}

    def counts() -> dict:
        c = con.execute("SELECT " + ", ".join(f"SUM({x})" for x in [*cat.values(), is_crit]) + f" FROM ({inner}) t",
                        [*crit_args, *args]).fetchone()
        return {**{k: c[i] or 0 for i, k in enumerate(cat)}, "critical": c[len(cat)] or 0}
    weeks = [{"week": k + 1, "from": _fmt_dt(frm + timedelta(days=7 * k)),
              "to": _fmt_dt(min(to, frm + timedelta(days=7 * k + 7)))}
             for k in range((n + 6) // 7)]
    return {"sql": q, "args": [*crit_args, *args], "extra": extra, "counts": counts, "frm": frm, "to": to,
            "window": {"from": _fmt_dt(frm), "to": _fmt_dt(to), "days": n,
                       "basis": "calendar days, half-open [from, to), from " + ("from_date" if from_date else "the data date")},
            "weeks": weeks, "data_date": dd, "critical_definition": crit_def + "; open activities", "code_filter": cinfo}


CURVE_POINTS = 21  # P6 resource curve: PctUsage points 0 to 20, one per 5 % of the duration


def _curve_shape(data) -> tuple[float, ...]:
    """RSRCCURV.curv_data -> cumulative share of the units (0 to 1) at 0, 5, ..., 100 % of the duration.
    The text is a P6 tree '(0||CurveData()((0||0(PctUsage|0)()) ... (0||20(PctUsage|5)())))' (the root
    name may be empty, BLOB or TEXT): point 0 is the share placed at the start, point k (1 to 20) the share
    used from (k - 1) x 5 % to k x 5 % of the duration (P6's built-in curves are symmetric that way: Bell
    Shaped mirrors point k on point 21 - k). Values are percentages; they are scaled by their sum, so a curve
    that does not add up to exactly 100 keeps its shape. Raises ValueError when a point is missing, negative
    or the sum is 0."""
    try:
        kids = _p6_tree(_blob_text(data))["kids"]
        pts = {int(k["name"]): float(k["attrs"]["PctUsage"]) for k in kids}
    except (KeyError, TypeError, ValueError, IndexError) as e:
        raise ValueError(f"unreadable curve data: {e}") from None
    if sorted(pts) != list(range(CURVE_POINTS)) or min(pts.values()) < 0 or not math.isfinite(sum(pts.values())):
        raise ValueError(f"a curve needs the {CURVE_POINTS} points 0 to 20 with values of 0 or more")
    total, acc, out = sum(pts.values()), 0.0, []
    if total <= 0:
        raise ValueError("the curve points add up to 0")
    for k in range(CURVE_POINTS):
        acc += pts[k]
        out.append(acc / total)
    out[-1] = 1.0
    return tuple(out)


def _shape_at(shape: Sequence[float], w: float) -> float:
    """Cumulative share of a _curve_shape at the duration fraction w (0 < w < 1), linear inside each 5 %."""
    x = w * (CURVE_POINTS - 1)
    k = min(int(x), CURVE_POINTS - 2)
    return shape[k] + (shape[k + 1] - shape[k]) * (x - k)


def _cum(items: Sequence[tuple], edges: Sequence[datetime]) -> list[float]:
    """Cumulative quantity at each edge (increasing) of items (qty, start, end[, calendar[, shape]]); a
    zero-length item counts after its start. Without a calendar the quantity runs linearly over calendar time
    from start to end; with one (_Cal) linearly over its working time (the share at t = working hours from
    start to t / working hours from start to end; a calendar with an error or no working time in the span
    falls back to calendar time); a shape (_curve_shape) then maps that duration share to the units share."""
    out, step = [0.0] * len(edges), [0.0] * (len(edges) + 1)
    for it in items:
        q, s, e = it[0], it[1], it[2]
        cal, shape = (it[3] if len(it) > 3 else None), (it[4] if len(it) > 4 else None)
        i = bisect.bisect_right(edges, s)  # first edge after the start
        j = bisect.bisect_left(edges, e, i) if e > s else i  # first edge at or after the end
        if j > i:
            wh = cal.work_hours(s, e) if cal is not None and not cal.error else 0.0
            span = (e - s).total_seconds()
            for k in range(i, j):
                w = cal.work_hours(s, edges[k]) / wh if wh > 0 else (edges[k] - s).total_seconds() / span
                out[k] += q * (_shape_at(shape, w) if shape else w)
        step[j] += q
    acc = 0.0
    for k in range(len(edges)):
        acc += step[k]
        out[k] += acc
    return out


def _value_at(items: Sequence[tuple], t: datetime) -> float:
    """Cumulative quantity of items at t, linear over calendar time between their dates whatever calendar or
    curve they carry: the planned value definition of to_date (p6_progress_curve), p6_project_summary and the
    dashboard, kept as it was before the working time spread of the periods (3.5.0)."""
    return _cum([it[:3] for it in items], [t])[0]


def _spread_cal(r: Mapping, cals: Mapping[int, _Cal]) -> Optional[_Cal]:
    """Calendar an assignment's units are spread on (P6): the resource's calendar for a resource dependent
    activity (TT_Rsrc) that has one, else the activity's calendar; None when neither is loaded."""
    if r["task_type"] == "TT_Rsrc" and r["rsrc_clndr_id"] in cals:
        return cals[r["rsrc_clndr_id"]]
    return cals.get(r["task_clndr_id"])


class _Spread:
    """What the working time spread of _curve_items needs, read once per call inside the caller's snapshot:
    every calendar (_calendars), the resource curves (RSRCCURV curv_id -> _curve_shape; unreadable ones in
    bad_curves) and the stored period actuals of the assignments (TRSRCFIN rows of the projects pids with the
    FINDATES period: act_qty for units, act_cost for cost; a period ends at the end of its end_date day when
    that is a date at 00:00). counts: calendar_time (items spread over calendar time: no readable calendar
    or no working time in the span), curves (assignments spread on a resource curve), unknown_curve (curv_id not found or unreadable: spread linearly), period_actuals (assignments
    whose actual curve comes from TRSRCFIN)."""

    def __init__(self, con, pids: Sequence[int], measure: str = "units"):
        self.cals = _calendars(con)
        self.curves: dict[int, tuple] = {}
        self.bad_curves: dict[int, str] = {}
        if {"curv_id", "curv_data"} <= set(_cols(con, "RSRCCURV")):
            for cid, data in con.execute("SELECT curv_id, curv_data FROM RSRCCURV WHERE 1=1" + _alive(con, "RSRCCURV")):
                try:
                    self.curves[cid] = _curve_shape(data)
                except ValueError as e:
                    self.bad_curves[cid] = str(e)
        self.fin: dict[int, list[tuple[float, datetime, datetime]]] = {}
        fc, dc = _cols(con, "TRSRCFIN"), _cols(con, "FINDATES")
        col = "act_qty" if measure == "units" else "act_cost"
        if {"fin_dates_id", "taskrsrc_id", "proj_id", col} <= set(fc) and {"fin_dates_id", "start_date", "end_date"} <= set(dc):
            for p in pids:
                for tr, q, s, e in con.execute(
                        f"SELECT f.taskrsrc_id, f.{col}, d.start_date, d.end_date FROM TRSRCFIN f JOIN FINDATES d "
                        "ON d.fin_dates_id = f.fin_dates_id WHERE f.proj_id = ?" + _alive(con, "TRSRCFIN", "f")
                        + _alive(con, "FINDATES", "d") + " ORDER BY f.taskrsrc_id, d.start_date, f.fin_dates_id", (p,)):
                    s, e = _db_dt(s), _db_dt(e)
                    if s is None or e is None or not q:
                        continue
                    if e.time() == time() and e >= s:
                        e += timedelta(days=1)  # FINDATES end_date names the period's last day
                    self.fin.setdefault(tr, []).append((float(q), s, max(s, e)))
        self.counts = {"calendar_time": 0, "curves": 0, "unknown_curve": 0, "period_actuals": 0}

    def item(self, q: float, s: datetime, e: datetime, cal: Optional[_Cal], shape=None) -> tuple:
        """One spread item; a span without working time on cal is spread over calendar time (counted)."""
        if e > s and (cal is None or cal.error or cal.work_hours(s, e) <= 0):
            self.counts["calendar_time"] += 1
            cal = None
        return (q, s, e, cal, shape)

    def shape(self, r: Mapping) -> Optional[tuple]:
        cid = r["curv_id"]
        if cid is None:
            return None
        if cid in self.curves:
            self.counts["curves"] += 1
            return self.curves[cid]
        self.counts["unknown_curve"] += 1
        return None

    def actual(self, r: Mapping, a: float, s: datetime, e: datetime, cal: Optional[_Cal]) -> list[tuple]:
        """Actual items of one assignment from s to e (actual start to actual finish, clipped at the data date):
        its TRSRCFIN period actuals, each over its financial period inside [s, e] (the whole period clipped at e
        when they do not overlap), and the rest (actual - stored) from the end of the last stored period, not
        before s, to e; without stored periods one item from s to e."""
        rows = self.fin.get(r["taskrsrc_id"]) if r["taskrsrc_id"] is not None else None
        if not rows:
            return [self.item(a, s, e, cal)]
        self.counts["period_actuals"] += 1
        out, done, last = [], 0.0, s
        for q, fs, fe in rows:
            lo, hi = max(fs, s), min(fe, e)
            if hi <= lo:
                lo, hi = min(fs, e), min(fe, e)
            out.append(self.item(q, lo, max(lo, hi), cal))
            done, last = done + q, max(last, min(fe, e))
        if abs(a - done) > 1e-9:
            out.append(self.item(a - done, min(last, e), e, cal))
        return out


def _measure_rows(con, pid: int, measure: str, resource: Optional[str] = None, every_type: bool = False,
                  role: Optional[str] = None) -> list[sqlite3.Row]:
    """Assignments of a project: target, actual and remain quantity (measure 'units': labor and
    nonlabor resources, every type with every_type; 'cost': every resource), planned, actual and
    remaining dates, what _perf_pct and _ev_pct need of the activity (WBS node; has_curve = a resource
    curve on one of its assignments) and what the working time spread needs (taskrsrc_id, rsrc_id,
    rsrc_type, activity type and calendar, resource calendar, curv_id). resource= one resource (any
    type); role= the assignments of that Role ID."""
    cols, tc, k = _cols(con, "TASKRSRC"), _cols(con, "TASK"), "qty" if measure == "units" else "cost"
    act = " + ".join(f"COALESCE(tr.{c}, 0)" for c in (f"act_reg_{k}", f"act_ot_{k}") if c in cols) or "0"
    rtype = "COALESCE(tr.rsrc_type, r.rsrc_type)" if "rsrc_type" in cols else "r.rsrc_type"
    tu = lambda kind: " + ".join(f"COALESCE(t.{kind}_{x}_qty, 0)" for x in ("work", "equip")  # noqa: E731
                                 if f"{kind}_{x}_qty" in tc) or "0"
    sql = (f"SELECT COALESCE(tr.target_{k}, 0) AS target, {act} AS actual, COALESCE(tr.remain_{k}, 0) AS remain, "
           "tr.target_start_date, tr.target_end_date, tr.act_start_date, tr.act_end_date, tr.restart_date, "
           "tr.reend_date, t.phys_complete_pct, t.status_code, t.target_drtn_hr_cnt, t.remain_drtn_hr_cnt, "
           + ("t.complete_pct_type" if "complete_pct_type" in tc else "NULL") + f" AS pct_type, {tu('act')} AS task_act, "
           f"{tu('remain')} AS task_remain, t.wbs_id, "
           + ("EXISTS (SELECT 1 FROM TASKRSRC c WHERE c.task_id = t.task_id AND c.curv_id IS NOT NULL"
              + _alive(con, "TASKRSRC", "c") + ")" if "curv_id" in cols else "0") + " AS has_curve, "
           f"tr.taskrsrc_id, tr.rsrc_id, {rtype} AS rsrc_type, {rtype} AS rsrc_kind, t.task_type, "
           "t.clndr_id AS task_clndr_id, "
           + ("r.clndr_id" if "clndr_id" in _cols(con, "RSRC") else "NULL") + " AS rsrc_clndr_id, "
           + ("tr.curv_id" if "curv_id" in cols else "NULL") + " AS curv_id "
           "FROM TASKRSRC tr JOIN TASK t ON t.task_id = tr.task_id "
           "LEFT JOIN RSRC r ON r.rsrc_id = tr.rsrc_id" + _alive(con, "RSRC", "r") + " WHERE t.proj_id = ?"
           + _alive(con, "TASKRSRC", "tr") + _alive(con, "TASK", "t"))
    args: list = [pid]
    if role is not None:
        sql += (" AND tr.role_id IN (SELECT role_id FROM ROLES WHERE role_short_name = ?" + _alive(con, "ROLES") + ")"
                if "role_id" in cols else " AND 0")
        args.append(role)
    if resource:
        return con.execute(sql + " AND r.rsrc_short_name = ? ORDER BY tr.taskrsrc_id", (*args, resource)).fetchall()
    if measure == "units" and not every_type:
        sql += f" AND {rtype} IN ('RT_Labor', 'RT_Equip')"
    return con.execute(sql + " ORDER BY tr.taskrsrc_id", args).fetchall()


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


# P6 earned value techniques (PROJWBS / PREFER ev_compute_type) -> technique name. EC_Cmp_pct is in P6's
# own exports; the other tokens come from a third-party reader of P6 XER files. The token of 'activity
# percent complete using resource curves' is not known: a token naming a curve is read as that technique.
EV_TECHNIQUES = {"EC_Cmp_pct": "activity_pct", "EC_0_pct": "0_100", "EC_50_pct": "50_50",
                 "EC_User_pct": "custom_pct", "EC_MS": "wbs_milestones"}
EV_CURVE_TOKEN = re.compile(r"curv", re.I)


def _ev_map(con, pid: int) -> dict:
    """Earned value technique of every WBS node of a project: {by_wbs: {wbs_id: (technique, custom %,
    WBS milestones %)}, nodes: {technique: node count}, unknown: [tokens]}. A node's own
    ev_compute_type / ev_user_pct, else the project node's, else PREFER's (the admin default), else
    activity percent complete. WBS milestones % = weight of the node's completed WBSSTEP rows / weight
    of all of them x 100 (None without weights). An unknown token counts as activity percent complete
    and is listed."""
    wc = _cols(con, "PROJWBS")
    col = lambda c: c if c in wc else "NULL"  # noqa: E731
    rows = con.execute(f"SELECT wbs_id, proj_node_flag, {col('ev_compute_type')}, {col('ev_user_pct')} FROM PROJWBS "
                       "WHERE proj_id = ?" + _alive(con, "PROJWBS"), (pid,)).fetchall()
    pc = _cols(con, "PREFER")
    pref = (con.execute("SELECT " + ", ".join(c if c in pc else "NULL" for c in ("ev_compute_type", "ev_user_pct"))
                        + " FROM PREFER").fetchone() if pc else None) or (None, None)
    node = next((r for r in rows if r[1] == "Y"), None)
    dflt = (node[2] if node and node[2] else pref[0] or "EC_Cmp_pct",
            node[3] if node and node[3] is not None else pref[1])
    ms: dict[int, list[float]] = {}
    if {"wbs_id", "proj_id", "step_wt", "complete_flag"} <= set(_cols(con, "WBSSTEP")):
        for w, wt, done in con.execute("SELECT wbs_id, step_wt, complete_flag FROM WBSSTEP WHERE proj_id = ?"
                                       + _alive(con, "WBSSTEP"), (pid,)):
            m = ms.setdefault(w, [0.0, 0.0])
            m[0] += float(wt or 0) * (done == "Y")
            m[1] += float(wt or 0)
    out: dict[str, Any] = {"by_wbs": {}, "nodes": {}, "unknown": []}
    for w, _, tok, up in rows:
        tok = tok or dflt[0]
        tech = EV_TECHNIQUES.get(tok) or ("resource_curves" if EV_CURVE_TOKEN.search(tok) else None)
        if tech is None:
            tech = "activity_pct"
            if tok not in out["unknown"]:
                out["unknown"].append(tok)
        up = dflt[1] if up is None else up
        m = ms.get(w)
        out["by_wbs"][w] = (tech, None if up is None else min(100.0, max(0.0, float(up))),
                            100 * m[0] / m[1] if m and m[1] > 0 else None)
        out["nodes"][tech] = out["nodes"].get(tech, 0) + 1
    return out


def _ev_pct(r: Mapping, ev: Optional[Mapping]) -> float:
    """Performance % complete (0 to 100) of a _measure_rows row under the earned value technique of its
    WBS node (ev = _ev_map; None or a node it lacks = activity percent complete), as the P6
    documentation defines them: activity_pct = _perf_pct; resource_curves = units % complete of the
    activity (actual / at completion, labor plus nonlabor) when one of its assignments has a resource
    curve, else _perf_pct; 0_100 = 100 once completed, else 0; 50_50 = 50 once started, 100 once
    completed; custom_pct = the node's custom % once started, 100 once completed; wbs_milestones = the
    node's completed milestone weight share (0 without weights)."""
    tech, user, ms = (ev or {}).get("by_wbs", {}).get(r["wbs_id"], ("activity_pct", None, None))
    st = r["status_code"]
    if tech == "0_100":
        return 100.0 * (st == "TK_Complete")
    if tech in ("50_50", "custom_pct"):
        if st in ("TK_Complete", "TK_NotStart"):
            return 100.0 * (st == "TK_Complete")
        return 50.0 if tech == "50_50" else user or 0.0
    if tech == "wbs_milestones":
        return ms or 0.0
    if tech == "resource_curves" and r["has_curve"]:
        a = float(r["task_act"] or 0)
        at = a + float(r["task_remain"] or 0)
        return min(100.0, max(0.0, 100 * a / at)) if at else 100.0 * (st == "TK_Complete")
    return _perf_pct(r)


SPREAD_RULES = ("Periods: each assignment's quantities are spread over the working time of its calendar (the "
                "resource's calendar for a resource dependent activity, else the activity's), on its resource curve "
                "when it has one (21 points, one per 5 % of the duration), planned over the planned dates, remaining "
                "over the remaining dates from the data date and actual from the actual start to the actual finish "
                "or the data date.")


def _spread_note(counts: Mapping) -> str:
    """Note text of a working time spread (p6_progress_curve, p6_resource_loading) from _Spread.counts plus
    trsrcfin (TRSRCFIN rows of the project)."""
    fin = ("Stored period actuals (TRSRCFIN) place the actual of their financial periods; the rest of an "
           "assignment's actual runs from the end of its last stored period to the data date."
           if counts.get("period_actuals") else
           "The database holds no period actuals for these assignments (TRSRCFIN), so the actual curve is rebuilt "
           "from the actual dates." if not counts.get("trsrcfin") else
           "Period actuals (TRSRCFIN) exist but none for these assignments: the actual curve is rebuilt from the actual dates.")
    return (SPREAD_RULES + " " + fin + " Still approximate per period: an in-progress assignment takes its whole curve "
            "over its remaining dates, units per time that vary inside an assignment and the activity level TASKFIN "
            "rows are not read"
            + (f"; {counts['calendar_time']} spans without working time on their calendar run over calendar time"
               if counts.get("calendar_time") else "")
            + (f"; {counts['unknown_curve']} assignments name a curve that is missing or unreadable (spread linearly)"
               if counts.get("unknown_curve") else "") + ".")


def _curve_items(rows: Sequence, dd: datetime, ev: Optional[Mapping] = None, sp: Optional[_Spread] = None) -> dict:
    """Curves of _measure_rows at data date dd: planned (target over the planned dates), actual (actual
    from the actual start to the actual finish, clipped at dd; dates missing = at dd), remaining (remain
    from the remaining start, not before dd, to the remaining finish). Items are (qty, start, end) spread
    linearly over calendar time without sp; with sp (_Spread) (qty, start, end, calendar, shape): working
    time of the assignment's calendar (_spread_cal), the resource curve of the assignment on planned and
    remaining (the whole curve over the remaining dates), the TRSRCFIN period actuals on actual
    (_Spread.actual). Plus budget, actual_qty, remain_qty and earned (target x _ev_pct under the WBS
    techniques of ev = _ev_map) totals, clipped (actual dates after dd), clamped (remaining start before
    dd) and unspread (target without planned dates)."""
    out: dict[str, Any] = {"planned": [], "actual": [], "remaining": [], "budget": 0.0, "actual_qty": 0.0,
                           "remain_qty": 0.0, "earned": 0.0, "clipped": 0, "clamped": 0, "unspread": 0.0}
    for r in rows:
        q, a, rem = float(r["target"]), float(r["actual"]), float(r["remain"])
        out["budget"] += q
        out["actual_qty"] += a
        out["remain_qty"] += rem
        out["earned"] += _ev_pct(r, ev) / 100 * q
        cal = _spread_cal(r, sp.cals) if sp else None
        shape = sp.shape(r) if sp and (q or rem) else None
        s, e = _db_dt(r["target_start_date"]), _db_dt(r["target_end_date"])
        if q and s and e:
            out["planned"].append(sp.item(q, s, max(s, e), cal, shape) if sp else (q, s, max(s, e)))
        elif q:
            out["unspread"] += q
        if a:
            s, e = _db_dt(r["act_start_date"]), _db_dt(r["act_end_date"])
            out["clipped"] += bool((s and s > dd) or (e and e > dd))
            e = min(e or dd, dd)
            if sp:
                out["actual"] += sp.actual(r, a, min(s or e, e), e, cal)
            else:
                out["actual"].append((a, min(s or e, e), e))
        if rem:
            s = _db_dt(r["restart_date"]) or dd
            out["clamped"] += s < dd
            s = max(s, dd)
            e = max(_db_dt(r["reend_date"]) or s, s)
            out["remaining"].append(sp.item(rem, s, e, cal, shape) if sp else (rem, s, e))
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


MS_VS = ("late", "early", "on_time")  # p6_milestones vs_baseline


def _code_list(codes, name: str) -> list[str]:
    """Activity IDs of a list or of a text separated by commas (blanks dropped, first occurrence kept)."""
    items = codes.split(",") if isinstance(codes, str) else codes
    if not isinstance(items, (list, tuple)) or not all(isinstance(c, str) for c in items):
        raise ValueError(f"{name}: a list of Activity IDs, or one text with the IDs separated by commas")
    out = list(dict.fromkeys(c.strip() for c in items if c.strip()))
    if not out:
        raise ValueError(f"{name}: give at least one Activity ID")
    return out


# Cost earned value at the data date (p6_project_summary cost, p6_progress_curve to_date.cost).
COST_SOURCES = ("labor", "nonlabor", "material", "other", "expenses")
COST_SOURCE_OF = {"RT_Labor": "labor", "RT_Equip": "nonlabor", "RT_Mat": "material"}
# P6 technique for computing estimate to complete (PROJWBS / PREFER ev_etc_compute_type) -> name. EE_Rem_hr and
# EE_PF_cpi are stored by P6 25.12; the tokens of PF = 1, PF = 1 / (CPI x SPI) and PF = custom are not known: a
# token naming spi, user (or custom) or ending in 1 is read as that technique (_etc_technique).
ETC_TECHNIQUES = {"EE_Rem_hr": "remaining_cost", "EE_PF_cpi": "pf_cpi"}
ETC_FORMULAS = {"remaining_cost": "AC + remaining cost (ETC = remaining cost for activity)",
                "pf_1": "AC + (BAC - EV) (PF = 1)", "pf_cpi": "AC + (BAC - EV) / CPI = BAC / CPI (PF = 1 / CPI)",
                "pf_cpi_spi": "AC + (BAC - EV) / (CPI x SPI) (PF = 1 / (CPI x SPI))",
                "pf_custom": "AC + PF x (BAC - EV) (PF = the custom value)"}


def _etc_technique(tok: Optional[str]) -> Optional[str]:
    """Estimate to complete technique of an ev_etc_compute_type token, None when unknown."""
    if not tok:
        return None
    if tok in ETC_TECHNIQUES:
        return ETC_TECHNIQUES[tok]
    t = tok.lower()
    if "spi" in t:
        return "pf_cpi_spi"
    if "cpi" in t:
        return "pf_cpi"
    if re.search(r"usr|user|cust", t):
        return "pf_custom"
    if re.search(r"pf_?1$", t):
        return "pf_1"
    return None


def _etc_setting(con, pid: int) -> dict:
    """Estimate to complete technique of a project: the project WBS node's ev_etc_compute_type /
    ev_etc_user_value, else PREFER's (the admin default), else ETC = remaining cost. other_nodes =
    WBS nodes below whose own stored technique differs (P6 applies each node's; the project figure uses
    the project's)."""
    wc, pc = _cols(con, "PROJWBS"), _cols(con, "PREFER")
    col = lambda cs, c: c if c in cs else "NULL"  # noqa: E731
    rows = con.execute(f"SELECT proj_node_flag, {col(wc, 'ev_etc_compute_type')}, {col(wc, 'ev_etc_user_value')} "
                       "FROM PROJWBS WHERE proj_id = ?" + _alive(con, "PROJWBS"), (pid,)).fetchall()
    pref = (con.execute(f"SELECT {col(pc, 'ev_etc_compute_type')}, {col(pc, 'ev_etc_user_value')} FROM PREFER")
            .fetchone() if pc else None) or (None, None)
    node = next((r for r in rows if r[0] == "Y"), None)
    if node and node[1]:
        tok, pf, src = node[1], node[2], "project WBS node (PROJWBS.ev_etc_compute_type)"
    elif pref[0]:
        tok, pf, src = pref[0], pref[1], "admin preferences (PREFER.ev_etc_compute_type)"
    else:
        tok, pf, src = None, None, "default (no technique stored)"
    tech = _etc_technique(tok)
    return {"token": tok, "technique": tech or "remaining_cost", "pf": None if pf is None else float(pf), "source": src,
            "unknown": tok if tok and tech is None else None,
            "other_nodes": sum(1 for r in rows if r[0] != "Y" and r[1] and r[1] != tok)}


def _expense_rows(con, pid: int) -> list[dict]:
    """Expenses (PROJCOST) of a project's activities as _measure_rows rows: target / actual / remain =
    budgeted / actual / remaining cost, planned dates of the activity by accrual type (CL_Start at its
    planned start, CL_End at its planned finish, else spread over them) and what _ev_pct needs of the
    activity. Empty when this P6 version has no PROJCOST costs."""
    ec, tc, rc = _cols(con, "PROJCOST"), _cols(con, "TASK"), _cols(con, "TASKRSRC")
    if not {"task_id", "target_cost"} <= set(ec):
        return []
    c = lambda x: f"COALESCE(e.{x}, 0)" if x in ec else "0"  # noqa: E731
    tu = lambda kind: " + ".join(f"COALESCE(t.{kind}_{x}_qty, 0)" for x in ("work", "equip")  # noqa: E731
                                 if f"{kind}_{x}_qty" in tc) or "0"
    curve = ("EXISTS (SELECT 1 FROM TASKRSRC a WHERE a.task_id = t.task_id AND a.curv_id IS NOT NULL"
             + _alive(con, "TASKRSRC", "a") + ")" if "curv_id" in rc else "0")
    q = (f"SELECT {c('target_cost')} AS target, {c('act_cost')} AS actual, {c('remain_cost')} AS remain, "
         + ("e.cost_load_type" if "cost_load_type" in ec else "NULL") + " AS load_type, t.target_start_date AS ts, "
         "t.target_end_date AS te, t.phys_complete_pct, t.status_code, t.target_drtn_hr_cnt, t.remain_drtn_hr_cnt, "
         + ("t.complete_pct_type" if "complete_pct_type" in tc else "NULL") + f" AS pct_type, {tu('act')} AS task_act, "
         f"{tu('remain')} AS task_remain, t.wbs_id, t.clndr_id, {curve} AS has_curve FROM PROJCOST e "
         "JOIN TASK t ON t.task_id = e.task_id WHERE t.proj_id = ?" + _alive(con, "PROJCOST", "e") + _alive(con, "TASK", "t")
         + " ORDER BY " + ("e.cost_item_id" if "cost_item_id" in ec else "e.rowid"))
    out = []
    for r in con.execute(q, (pid,)):
        d = _dict(r)
        s, e = {"CL_Start": (d["ts"], d["ts"]), "CL_End": (d["te"], d["te"])}.get(d.pop("load_type"), (d["ts"], d["te"]))
        del d["ts"], d["te"]
        d.update(target_start_date=s, target_end_date=e, act_start_date=None, act_end_date=None, restart_date=None,
                 reend_date=None, rsrc_kind="expense")
        out.append(d)
    return out


def _cost_ev(con, pid: int, bid: Optional[int], dd: Optional[datetime], resource: Optional[str] = None) -> dict:
    """Cost earned value of a project at data date dd (run inside the caller's snapshot), P6's earned value
    fields: BAC = budgeted cost of every assignment (labor, nonlabor, material) plus expenses; PV = the
    baseline's planned cost at dd (its budgeted costs over its planned dates, spread as p6_progress_curve);
    EV = budgeted cost x the earned % of each activity (_ev_pct under its WBS node's technique); AC = actual
    costs (regular + overtime) plus actual expense costs; CV, SV, CPI, SPI; ETC / EAC by the project's
    estimate to complete technique (_etc_setting) and the standard variants; VAC; TCPI. resource= one
    resource's assignments, expenses left out. Every key is present; figures are None without costs, PV, SV,
    SPI (and what needs SPI) None without a baseline or data date."""
    t = dd or datetime.max

    def items(p: int) -> dict:
        ev = _ev_map(con, p)
        g: dict[str, list] = {k: [] for k in COST_SOURCES}
        for r in _measure_rows(con, p, "cost", resource):
            g[COST_SOURCE_OF.get(r["rsrc_kind"], "other")].append(r)
        if resource is None:
            g["expenses"] = _expense_rows(con, p)
        return {k: _curve_items(v, t, ev) for k, v in g.items()}
    cur = items(pid)
    base = items(bid) if bid is not None else None
    etc = _etc_setting(con, pid)
    cr = con.execute("SELECT curr_short_name FROM CURRTYPE WHERE base_exch_rate = 1" + _alive(con, "CURRTYPE")
                     ).fetchone() if {"curr_short_name", "base_exch_rate"} <= set(_cols(con, "CURRTYPE")) else None
    r2, r4 = (lambda v: None if v is None else round(v, 2) + 0.0), (lambda v: None if v is None else round(v, 4) + 0.0)  # noqa: E731
    pv_of = lambda k: _value_at(base[k]["planned"], dd) if base and dd else None  # noqa: E731
    by = {k: {"bac": r2(c["budget"]), "pv": r2(pv_of(k)), "ev": r2(c["earned"]), "ac": r2(c["actual_qty"]),
              "remaining": r2(c["remain_qty"])} for k, c in cur.items()}
    bac, ev, ac, rem = (sum(c[x] for c in cur.values()) for x in ("budget", "earned", "actual_qty", "remain_qty"))
    pv = sum(pv_of(k) for k in COST_SOURCES) if base and dd else None
    has = any(abs(v) > 1e-9 for v in (bac, ac, rem))
    cpi = ev / ac if ac else None
    spi = ev / pv if pv else None
    pf = etc["pf"]
    variants = {"ac_plus_remaining": ac + rem, "ac_plus_bac_minus_ev": ac + bac - ev,
                "bac_over_cpi": bac / cpi if cpi else None,
                "ac_plus_bac_minus_ev_over_cpi_spi": ac + (bac - ev) / (cpi * spi) if cpi and spi else None,
                "ac_plus_pf_x_bac_minus_ev": None if pf is None else ac + pf * (bac - ev)}
    eac = {"remaining_cost": variants["ac_plus_remaining"], "pf_1": variants["ac_plus_bac_minus_ev"],
           "pf_cpi": variants["bac_over_cpi"], "pf_cpi_spi": variants["ac_plus_bac_minus_ev_over_cpi_spi"],
           "pf_custom": variants["ac_plus_pf_x_bac_minus_ev"]}[etc["technique"]]
    fig = {"bac": bac, "pv": pv, "ev": ev, "ac": ac, "remaining": rem, "cv": ev - ac, "sv": None if pv is None else ev - pv,
           "etc": None if eac is None else eac - ac, "eac": eac, "vac": None if eac is None else bac - eac}
    out: dict[str, Any] = {"available": has, "currency": cr and cr[0],
                           **{k: r2(v) if has else None for k, v in fig.items()},
                           "cpi": r4(cpi) if has else None, "spi": r4(spi) if has else None,
                           "tcpi": r4((bac - ev) / (eac - ac)) if has and eac is not None and abs(eac - ac) > 1e-9 else None,
                           "tcpi_bac": r4((bac - ev) / (bac - ac)) if has and abs(bac - ac) > 1e-9 else None,
                           "earned_pct": r2(100 * ev / bac) if has and bac else None,
                           "planned_pct": r2(100 * pv / bac) if has and bac and pv is not None else None,
                           "baseline_bac": r2(sum(c["budget"] for c in base.values())) if base else None,
                           "eac_method": {"technique": etc["technique"], "token": etc["token"], "pf": etc["pf"],
                                          "source": etc["source"], "formula": ETC_FORMULAS[etc["technique"]],
                                          "wbs_nodes_other_technique": etc["other_nodes"]},
                           "eac_variants": {k: r2(v) if has else None for k, v in variants.items()},
                           "by_source": by, "scope": f"resource {resource}, expenses left out" if resource else
                           "every assignment (labor, nonlabor, material) plus expenses (PROJCOST)",
                           "formulas": {"bac": "budgeted cost of the assignments + budgeted expense cost",
                                        "pv": "baseline budgeted costs spread linearly over the baseline planned dates, "
                                              "at the data date", "ev": "sum(budgeted cost x earned % of the activity's "
                                                                        "WBS technique)",
                                        "ac": "actual regular + overtime cost of the assignments + actual expense cost",
                                        "cv": "EV - AC", "sv": "EV - PV", "cpi": "EV / AC", "spi": "EV / PV",
                                        "eac": ETC_FORMULAS[etc["technique"]], "etc": "EAC - AC", "vac": "BAC - EAC",
                                        "tcpi": "(BAC - EV) / (EAC - AC)", "tcpi_bac": "(BAC - EV) / (BAC - AC)"}}
    notes = []
    if not has:
        notes.append("No costs: the assignments and expenses of this project carry no budgeted, actual or "
                     "remaining cost, so every cost figure is null.")
    if bid is None:
        notes.append("No baseline: PV, SV and SPI are null.")
    elif dd is None:
        notes.append("No data date: PV, SV and SPI are null.")
    if etc["unknown"]:
        notes.append(f"Unknown estimate to complete technique {etc['unknown']}: EAC uses AC + remaining cost.")
    if eac is None and has:
        notes.append("EAC is null: its technique needs " + ("a CPI (no actual or earned cost)"
                                                             if etc["technique"] != "pf_custom" else "a PF value")
                     + ("" if etc["technique"] != "pf_cpi_spi" else " and an SPI (baseline planned cost)") + ".")
    if etc["other_nodes"]:
        notes.append(f"{etc['other_nodes']} WBS nodes store another estimate to complete technique than the project: "
                     "EAC applies the project's at project level.")
    out["note"] = " ".join(notes) or None
    return out


def _summary(con, pid: int, project: str, baseline: Optional[str]) -> dict:
    """p6_project_summary of the project pid, read inside the caller's snapshot (p6_trend reads it with the
    other figures of an update)."""
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
    p = con.execute("SELECT " + ", ".join(c if c in pc else f"NULL AS {c}" for c in pf) + " FROM PROJECT WHERE proj_id = ?",
                    (pid,)).fetchone()
    node = con.execute("SELECT wbs_name FROM PROJWBS WHERE proj_id = ? AND proj_node_flag = 'Y'" + al("PROJWBS"),
                       (pid,)).fetchone()
    cal = con.execute(f"SELECT c.clndr_name, {HPD_SQL} FROM CALENDAR c WHERE c.clndr_id = ?" + al("CALENDAR", "c"),
                      (p["clndr_id"],)).fetchone()
    pcal = _calendars(con, int(p["clndr_id"])).get(int(p["clndr_id"])) if p["clndr_id"] is not None else None
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
        braw = {c: (one(f"SELECT {c} FROM PROJECT WHERE proj_id = ?", (bid,)) if c in pc else None)
                for c in ("scd_end_date", "last_recalc_date")}
        bfin = _db_dt(braw["scd_end_date"])
        bl = {"finish": bfin, "raw": braw,
              "items": _curve_items(_measure_rows(con, bid, "units"), dd, _ev_map(con, bid)) if dd else None,
              "cur": _curve_items(_measure_rows(con, pid, "units"), dd, _ev_map(con, pid)) if dd else None}
    names = (name_of(pbid), name_of(bid))
    cost = _cost_ev(con, pid, bid, dd)
    r2, r4 = (lambda v: None if v is None else round(v, 2)), (lambda v: None if v is None else round(v, 4))  # noqa: E731
    budget, actual, remain = (float(v or 0) for v in a[7:10])
    pct = {"units": 100 * actual / (actual + remain) if actual + remain else None,
           "physical": 100 * float(a[10] or 0) / budget if budget else None,
           "duration": 100 * float(a[13] or 0) / float(a[12]) if a[12] else None,
           "activities_completed": 100 * (a[14] or 0) / a[0] if a[0] else None}
    primary = "units" if pct["units"] is not None else "duration"
    sf, mf = _db_dt(p["scd_end_date"]), _db_dt(p["plan_end_date"])
    cd = lambda x, y: r2((x - y).total_seconds() / 86400) if x and y else None  # noqa: E731
    ok = pcal is not None and not pcal.error
    wd = lambda x, y: round(pcal.work_days(y, x), 2) + 0.0 if ok and x and y else None  # noqa: E731
    base = {"project_baseline": names[0], "attached": attached, "used": names[1],
            "source": None if bid is None else "baseline=" if baseline else "project baseline (PROJECT.sum_base_proj_id)",
            "finish": None, "data_date": None, "finish_variance_cd": None, "finish_variance_wd": None,
            "planned_units_pct_at_data_date": None, "spi_units": None}
    if bl:
        base.update(finish=bl["raw"]["scd_end_date"], data_date=bl["raw"]["last_recalc_date"])
        bp = _value_at(bl["items"]["planned"], dd) if bl["items"] else None
        bb = bl["items"]["budget"] if bl["items"] else 0
        base.update(finish_variance_cd=cd(sf, bl["finish"]), finish_variance_wd=wd(sf, bl["finish"]),
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
                      "finish_vs_must_finish_by_cd": cd(sf, mf), "finish_vs_must_finish_by_wd": wd(sf, mf)},
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
            "baseline": base, "cost": cost, "counts": counts, "schedule_state": _schedule_state(con, pid), "notes": notes}


TREND_MAX = 100  # updates in one series
TREND_MS_CAP = 20  # milestones followed when codes= is not given
TREND_HEAD = ("project_flag", "last_recalc_date", "last_schedule_date", "scd_end_date", "plan_end_date", "clndr_id")


def _trend_head(con, pid: int) -> dict:
    cols = _cols(con, "PROJECT")
    return _dict(con.execute("SELECT proj_short_name, " + ", ".join(c if c in cols else f"NULL AS {c}" for c in TREND_HEAD)
                             + " FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone())


def _trend_series(names: Sequence[str]) -> list[dict]:
    """The updates of a trend from one read snapshot, ordered by data date, then last F9, then Project ID:
    [{pid, project, data_date, last_schedule_date}]. Refuses an unknown project, an EPS node, a Project ID
    given twice, an update without a data date, fewer than two updates or more than TREND_MAX."""
    twice = sorted({n for i, n in enumerate(names) if n in names[:i]})
    if twice:
        raise ValueError(f"Project IDs given twice: {twice}")
    if len(names) < 2:
        raise ValueError(f"A trend needs at least two updates; got {len(names)}: {list(names)}")
    if len(names) > TREND_MAX:
        raise ValueError(f"A trend holds at most {TREND_MAX} updates; got {len(names)}")
    con = _connect()
    con.execute("BEGIN")
    try:
        out = []
        for n in names:
            pid = _project_id(con, n)
            h = _trend_head(con, pid)
            if h["project_flag"] == "N":
                raise ValueError(f"{n} is an EPS node: pass eps={n!r} for the projects below it")
            if _db_dt(h["last_recalc_date"]) is None:
                raise ValueError(f"{n} has no data date (PROJECT.last_recalc_date): schedule it in P6 first")
            out.append({"pid": pid, "project": n, "data_date": h["last_recalc_date"],
                        "last_schedule_date": h["last_schedule_date"]})
    finally:
        con.rollback()
    out.sort(key=lambda u: (_db_dt(u["data_date"]), _db_dt(u["last_schedule_date"]) or datetime.min, u["project"]))
    return out


def _trend_check(con, u: Mapping) -> dict:
    """The PROJECT row of update u in the current snapshot; ValueError when its data date or last F9 moved since
    the series was read."""
    h = _trend_head(con, u["pid"])
    if (h["last_recalc_date"], h["last_schedule_date"]) != (u["data_date"], u["last_schedule_date"]):
        raise ValueError(f"The schedule of {u['project']} changed during the call (data date or last F9): run it again.")
    return h


def _trend_ms(con, pid: int, codes: Optional[Sequence[str]]) -> dict[str, dict]:
    """Milestones of an update by Activity ID (the lowest task_id of a repeated ID): codes= these activities (a
    task takes its finish), else every TT_Mile / TT_FinMile; one date as p6_milestones (start of a start
    milestone, else finish; actual else early)."""
    q, args = _act_from(con), [pid]
    if codes:
        q += f" AND t.task_code IN ({', '.join('?' * len(codes))})"
        args += list(codes)
    else:
        q += " AND t.task_type IN ('TT_Mile', 'TT_FinMile')"
    out: dict[str, dict] = {}
    for r in con.execute(q + " ORDER BY t.task_id", args):
        m = _dict(r)
        k = "start" if m["task_type"] == "TT_Mile" else "end"
        raw, kind = (m[f"act_{k}_date"], "actual") if m.get(f"act_{k}_date") else (m.get(f"early_{k}_date"), "forecast")
        out.setdefault(m["task_code"], {"task_name": m["task_name"], "task_type": m["task_type"],
                                        "status_code": m["status_code"], "date": raw, "date_kind": kind,
                                        "clndr_id": m["clndr_id"]})
    return out


def _trend_float(con, pid: int, near_days: float) -> dict:
    """Open activities (not completed, LOE and WBS summary left out): near critical (not critical under the
    project definition, total float at most near_days days of its calendar, compared in hours) and total float
    statistics in days of each activity's calendar."""
    crit, cargs = _critical_clause(con, pid, "project", None)[:2]
    rows = con.execute(f"SELECT t.total_float_hr_cnt, {HPD_SQL}, COALESCE({crit}, 0) FROM TASK t LEFT JOIN CALENDAR c "
                       f"ON c.clndr_id = t.clndr_id WHERE t.proj_id = ? AND t.status_code <> 'TK_Complete' AND "
                       f"t.task_type NOT IN {SUMMARY_TYPES}" + _alive(con, "TASK", "t"), [*cargs, pid]).fetchall()
    near = sum(1 for h, hpd, c in rows if h is not None and not c and float(h) <= near_days * float(hpd))
    days = sorted(float(h) / float(hpd) for h, hpd, _ in rows if h is not None)
    r2 = lambda v: None if v is None else round(v, 2) + 0.0  # noqa: E731
    return {"near_critical": near, "total_float": {
        "open": len(rows), "with_float": len(days), "min_d": r2(days[0] if days else None),
        "max_d": r2(days[-1] if days else None), "mean_d": r2(sum(days) / len(days) if days else None),
        "median_d": r2(statistics.median(days) if days else None)}}


def _trend_changes(con, pid: int, oid: int) -> dict:
    """What changed from update oid (before) to pid (after), matched as p6_compare_projects: activities by
    Activity ID, links by (predecessor, successor, type, predecessor project); durations cut as
    p6_update_integrity duration_cut (activities not started in either update, milestones, LOE and WBS summary
    left out, original or remaining duration shorter by more than 3 minutes)."""
    args = {"pid": pid, "oid": oid}
    acts, lnk = _cmp_acts(con, "current"), _cmp_links(con)
    A: dict[str, dict] = {}
    B: dict[str, dict] = {}
    for side, out in (("a", A), ("b", B)):
        for r in con.execute(acts + f"SELECT task_code, task_type, status_code, orig_h, rem_h FROM {side} "
                             "ORDER BY task_id", args):
            out.setdefault(r["task_code"], _dict(r))  # an Activity ID twice: the lowest task_id
    cut = 0
    for c in set(A) & set(B):
        a, b = A[c], B[c]
        if a["task_type"] in ("TT_LOE", "TT_WBS", "TT_Mile", "TT_FinMile") or a["status_code"] != "TK_NotStart" \
                or b["status_code"] != "TK_NotStart":
            continue
        if (b["orig_h"] or 0) - (a["orig_h"] or 0) > CPM_TF_TOL_H or (b["rem_h"] or 0) - (a["rem_h"] or 0) > CPM_TF_TOL_H:
            cut += 1
    ln = con.execute(lnk + "SELECT (SELECT COUNT(*) FROM la WHERE NOT EXISTS (SELECT 1 FROM lb WHERE " + LINK_KEY + ")), "
                     "(SELECT COUNT(*) FROM lb WHERE NOT EXISTS (SELECT 1 FROM la WHERE " + LINK_KEY + "))", args).fetchone()
    return {"activities_added": len(set(A) - set(B)), "activities_removed": len(set(B) - set(A)),
            "links_added": ln[0], "links_removed": ln[1], "durations_cut": cut}


def _trend(series: list[dict], baseline: Optional[str], codes: Optional[list[str]], ms_mode: str, near: float,
           dcma: bool, page: range) -> dict:
    """{rows, milestones, notes} of p6_trend: the rows of the updates whose index is in page (every figure of an
    update from one read snapshot of it; changes since the previous update from one snapshot of the pair), the
    milestones followed (every update is read for them) and notes."""
    con = _connect()
    info: list[dict] = []
    for i, u in enumerate(series):
        con.execute("BEGIN")  # one snapshot per update
        try:
            h = _trend_check(con, u)
            cals = _calendars(con)
            ms = _trend_ms(con, u["pid"], codes)
            it = {"head": h, "ms": ms, "cal": {k: cals.get(m["clndr_id"]) for k, m in ms.items()},
                  "pcal": cals.get(h["clndr_id"]) if h["clndr_id"] is not None else None}
            if i in page:
                it["summary"] = _summary(con, u["pid"], u["project"], baseline)
                it["float"] = _trend_float(con, u["pid"], near)
                if dcma:  # as p6_health_check with its defaults (key_dates is information only: left out)
                    bid = _baseline_pid(con, u["pid"], baseline)
                    cfg = _dcma_settings(con, u["pid"], {}, None)
                    checks = _dcma(con, u["pid"], bid, cfg, "current", "remaining")[0]
                    if "critical_path_test" not in cfg["disabled"]:
                        checks["critical_path_test"] = _cpt(con, u["pid"])
                    it["checks"] = checks
            info.append(it)
        finally:
            con.rollback()
    # milestones followed: codes= in that order, else the milestones of every update ('matched') or of any one
    present = [set(it["ms"]) for it in info]
    notes: list[str] = []
    if codes:
        keys = [c for c in codes if any(c in p for p in present)]
        missing = [c for c in codes if c not in keys]
        if missing:
            notes.append(f"Activity IDs in none of the updates: {missing}.")
    elif ms_mode == "none":
        keys = []
    else:
        pool = set.intersection(*present) if ms_mode == "matched" else set.union(*present)

        def last_date(c: str) -> tuple:
            m = next(it["ms"][c] for it in reversed(info) if c in it["ms"])
            return (_db_dt(m["date"]) or datetime.max, c)
        keys = sorted(pool, key=last_date)
        if len(keys) > TREND_MS_CAP:
            notes.append(f"{len(keys)} milestones {'in every update' if ms_mode == 'matched' else 'in the updates'}: "
                         f"the first {TREND_MS_CAP} by date in the latest update holding them are followed (pass "
                         "codes= to choose).")
            keys = keys[:TREND_MS_CAP]
    followed = []
    for c in keys:
        m = next(it["ms"][c] for it in reversed(info) if c in it["ms"])
        followed.append({"task_code": c, "task_name": m["task_name"], "task_type": m["task_type"],
                         "updates": sum(c in p for p in present)})

    def wd(cal, a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:  # working days from a to b
        return round(cal.work_days(a, b), 2) + 0.0 if cal is not None and not cal.error and a and b else None

    def cd(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
        return round((b - a).total_seconds() / 86400, 2) + 0.0 if a and b else None
    rows: list[dict] = []
    first_f = _db_dt(info[0]["head"]["scd_end_date"])
    for i in page:
        if i >= len(series):
            break
        u, it = series[i], info[i]
        sm, h, pcal = it["summary"], it["head"], it["pcal"]
        fin, prev_f = _db_dt(h["scd_end_date"]), _db_dt(info[i - 1]["head"]["scd_end_date"]) if i else None
        bl, pc, cr = sm["baseline"], sm["percent_complete"], sm["critical"]
        items = []
        for c in keys:
            m, cal = it["ms"].get(c), it["cal"].get(c)
            d = _db_dt(m["date"]) if m else None
            p0, pp = info[0]["ms"].get(c), info[i - 1]["ms"].get(c) if i else None
            items.append({"task_code": c, "date": m and m["date"], "date_kind": m and m["date_kind"],
                          "status_code": m and m["status_code"],
                          "slip_wd": wd(cal, _db_dt(pp["date"]), d) if pp else None,
                          "variance_first_wd": wd(cal, _db_dt(p0["date"]), d) if p0 else None})
        row = {"update": i + 1, "project": u["project"], "name": sm["project"]["name"], "data_date": u["data_date"],
               "last_schedule_date": u["last_schedule_date"], "stale": sm["schedule_state"]["stale"],
               "finish": h["scd_end_date"], "must_finish": h["plan_end_date"],
               "finish_vs_must_finish_wd": sm["dates"]["finish_vs_must_finish_by_wd"],
               "finish_variance_first_wd": wd(pcal, first_f, fin), "finish_variance_first_cd": cd(first_f, fin),
               "finish_variance_previous_wd": wd(pcal, prev_f, fin) if i else None,
               "finish_variance_previous_cd": cd(prev_f, fin) if i else None,
               "baseline": bl["used"], "baseline_finish": bl["finish"],
               "baseline_finish_variance_wd": bl["finish_variance_wd"],
               "units_pct": pc["units"], "planned_units_pct": bl["planned_units_pct_at_data_date"],
               "spi_units": bl["spi_units"], "critical": cr["critical"], "near_critical": it["float"]["near_critical"],
               "negative_float": cr["negative_float"], "total_float": it["float"]["total_float"], "dcma": None,
               "milestones": items, "changes": None}
        if dcma:
            ch = it["checks"]
            score = _weighted_score(ch)
            row["dcma"] = {"points": score["points"], "grade": score["grade"],
                           "failed": [k for k, v in ch.items() if v.get("pass") is False and k not in HC_UNSCORED],
                           "bei": ch["bei"].get("value"), "bei_pass": ch["bei"].get("pass")}
        rows.append(row)
    for row in rows:  # changes since the previous update: both projects in one snapshot
        i = row["update"] - 1
        if not i:
            continue
        con.execute("BEGIN")
        try:
            _trend_check(con, series[i])
            _trend_check(con, series[i - 1])
            row["changes"] = {"since": series[i - 1]["project"],
                              **_trend_changes(con, series[i]["pid"], series[i - 1]["pid"])}
        finally:
            con.rollback()
    return {"rows": rows, "milestones": followed, "notes": notes}


def _float_edges(edges_days: Optional[Sequence[float]]) -> tuple[list[float], list[str]]:
    """Bucket edges in days (default FLOAT_EDGES), finite and increasing, with 0 added when missing."""
    e = [float(x) + 0.0 for x in (FLOAT_EDGES if edges_days is None else edges_days)]  # + 0.0: -0.0 becomes 0.0
    if not e or not all(math.isfinite(x) for x in e) or any(b <= a for a, b in zip(e, e[1:], strict=False)):
        raise ValueError("edges_days must be finite numbers in increasing order, e.g. [-10, -5, 0, 5, 10, 20, 44]")
    if 0.0 in e:
        return e, []
    return sorted(e + [0.0]), ["0 added to edges_days: zero float has its own bucket"]


def _float_labels(edges: Sequence[float]) -> list[tuple[str, Optional[float], Optional[float]]]:
    """(label, from_d, to_d) of the float buckets of increasing edges holding 0, in order: below the
    first edge, [a, b) below zero, exactly 0, (a, b] above zero, above the last edge."""
    pairs = list(zip(edges, edges[1:], strict=False))
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
    for a, b in zip(edges, edges[1:], strict=False):
        if b <= 0 and a * hpd <= h < b * hpd:
            return f"[{a:g}, {b:g})"
        if a >= 0 and a * hpd < h <= b * hpd:
            return f"({a:g}, {b:g}]"
    raise ValueError(f"edges {list(edges)} do not hold 0")


RSRC_KINDS = {"RT_Labor": "labor", "RT_Equip": "nonlabor", "RT_Mat": "material"}  # P6 resource types
LOAD_KIND_ORDER = {"labor": 0, "nonlabor": 0, "material": 1}  # labor and nonlabor first, material apart, then others


def _rsrc_limits(con, ids: Iterable[int]) -> dict[int, tuple[list[tuple[Optional[datetime], float]], str]]:
    """Max units per hour of resources by effective date: {rsrc_id: (segments, source)}. RSRCRATE rows
    (start_date, max_qty_per_hr) sorted by date (a row without a date first; the same date twice: the higher
    rsrc_rate_id), source 'RSRCRATE'; else the RSRC column max_qty_per_hr when the schema has one (no date),
    source 'RSRC'. A resource without either is left out (no limit)."""
    ids = [i for i in ids if i is not None]
    found: dict[int, dict] = {}
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rc = _cols(con, "RSRCRATE")
    if {"rsrc_id", "max_qty_per_hr"} <= set(rc):
        order = ", rsrc_rate_id" if "rsrc_rate_id" in rc else ""
        date = "start_date" if "start_date" in rc else "NULL"
        for rid, d, m in con.execute(f"SELECT rsrc_id, {date}, max_qty_per_hr FROM RSRCRATE WHERE rsrc_id IN ({marks})"
                                     + _alive(con, "RSRCRATE") + f" AND max_qty_per_hr IS NOT NULL ORDER BY rsrc_id{order}",
                                     ids):
            found.setdefault(rid, {})[_db_dt(d)] = float(m)
    out = {rid: (sorted(segs.items(), key=lambda x: (x[0] is not None, x[0] or datetime.min)), "RSRCRATE")
           for rid, segs in found.items()}
    if "max_qty_per_hr" in _cols(con, "RSRC"):
        for rid, m in con.execute(f"SELECT rsrc_id, max_qty_per_hr FROM RSRC WHERE rsrc_id IN ({marks})"
                                  " AND max_qty_per_hr IS NOT NULL" + _alive(con, "RSRC"), ids):
            out.setdefault(rid, ([(None, float(m))], "RSRC"))
    return out


def _available(cal: _Cal, segs: Sequence[tuple[Optional[datetime], float]], a: datetime, b: datetime) -> float:
    """Units a resource can give from a to b: working hours of its calendar x max units per hour, cut at each
    effective date of segs (_rsrc_limits; the first rate also applies before its date)."""
    tot = 0.0
    for k, (_, m) in enumerate(segs):
        lo = a if k == 0 else max(a, segs[k][0])
        hi = b if k + 1 == len(segs) else min(b, segs[k + 1][0])
        if hi > lo:
            tot += cal.work_hours(lo, hi) * m
    return tot


def _week_diff(a: _Cal, b: _Cal) -> list[str]:
    """Days whose shifts differ between the work weeks of a and b: 'Sat 08:00-12:00 (project: off)'."""
    def txt(sh: tuple) -> str:
        return ", ".join(_shift_txt(sh)) or "off"
    return [f"{WEEKDAYS[i]} {txt(a.week[i])} (project: {txt(b.week[i])})" for i in range(7) if a.week[i] != b.week[i]]


def _holidays(cal: _Cal, y0: int, y1: int) -> list[date]:
    """Holidays of cal in the years y0 to y1: exception dates without working time on a weekday its work week works."""
    return sorted(d for d, sh in cal.exc.items() if not sh and cal.week[d.weekday()] and y0 <= d.year <= y1)


def _cal_check_read(con, pid: int) -> dict:
    """What p6_calendar_check needs, in the caller's snapshot: project row, calendars (parsed and stored rows),
    activity and assignment counts per calendar, open resource dependent assignments and the project finish."""
    al = functools.partial(_alive, con)
    pcols, ccols, tcols = _cols(con, "PROJECT"), _cols(con, "CALENDAR"), _cols(con, "TASK")
    prj = con.execute("SELECT " + ", ".join(c if c in pcols else f"NULL AS {c}" for c in (
        "clndr_id", "plan_start_date", "plan_end_date", "scd_end_date", "last_recalc_date"))
        + " FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone()
    raw = {r["clndr_id"]: r for r in con.execute(
        "SELECT clndr_id, clndr_name, base_clndr_id, day_hr_cnt, "
        + ", ".join(c if c in ccols else f"NULL AS {c}" for c in ("week_hr_cnt", "clndr_type"))
        + " FROM CALENDAR WHERE 1=1" + al("CALENDAR"))}
    acts = {r[0]: (r[1], r[2] or 0) for r in con.execute(
        "SELECT clndr_id, COUNT(*), SUM(status_code <> 'TK_Complete') FROM TASK WHERE proj_id = ?" + al("TASK")
        + " GROUP BY clndr_id", (pid,))}
    asg: dict = {}
    rd: list = []
    if _cols(con, "TASKRSRC") and "clndr_id" in _cols(con, "RSRC"):
        base = ("FROM TASKRSRC a JOIN TASK t ON t.task_id = a.task_id LEFT JOIN RSRC rs ON rs.rsrc_id = a.rsrc_id"
                + al("RSRC", "rs") + " WHERE t.proj_id = ? AND a.rsrc_id IS NOT NULL" + al("TASKRSRC", "a") + al("TASK", "t"))
        asg = {r[0]: r[1] for r in con.execute(f"SELECT rs.clndr_id, COUNT(*) {base} AND rs.rsrc_id IS NOT NULL "
                                                "GROUP BY rs.clndr_id", (pid,))}
        rd = con.execute("SELECT t.task_id, t.task_code, t.task_name, t.clndr_id, a.rsrc_id, rs.rsrc_id AS found, "
                         f"rs.rsrc_short_name, rs.clndr_id AS rsrc_clndr_id {base} AND t.task_type = 'TT_Rsrc' "
                         "AND t.status_code <> 'TK_Complete' ORDER BY t.task_code, t.task_id, a.taskrsrc_id",
                         (pid,)).fetchall()
    ends = [c for c in ("reend_date", "early_end_date", "act_end_date") if c in tcols]
    last = (con.execute(f"SELECT MAX(COALESCE({', '.join(ends)})) FROM TASK WHERE proj_id = ?" + al("TASK"),
                        (pid,)).fetchone()[0] if ends else None)
    return {"prj": prj, "raw": raw, "cals": _calendars(con), "acts": acts, "asg": asg, "rd": rd, "last": last,
            "state": _schedule_state(con, pid)}


def _diag_schema(info: dict) -> dict:
    """The schema block of p6_diagnose. A P6 EPPM working copy is built by p6_eppm_pull, not by P6 Professional: its
    fingerprint is never a known one and the schema write check does not apply to it (_write_refusal)."""
    out = {k: info[k] for k in ("label", "p6_version", "fingerprint", "known", "trusted", "readable", "writable",
                                "missing", "tables")}
    if _state.SETTINGS.eppm:
        out.update(label="P6 EPPM working copy", writable=not info["missing"],
                   note="Built by p6_eppm_pull from the server, not by P6 Professional: no known P6 fingerprint, "
                        "and the P6 Professional schema check does not apply (writes: see writes).")
    return out


def _bl_planned_by_wbs(con, bid: int, dd: datetime, wbs_of: Mapping[str, int]) -> dict[int, dict]:
    """Baseline planned units at the data date dd per WBS node of the current project: the baseline's
    labor and nonlabor assignments, placed by the Activity ID of the current activities (wbs_of: Activity
    ID -> wbs_id; an Activity ID not in it is left out), spread linearly between their planned dates as
    p6_progress_curve. {wbs_id: {bl_plan, bl_budget}} of the activities' own WBS (roll up with _merge_agg)."""
    out: dict[int, dict] = {}
    if not _cols(con, "TASKRSRC"):
        return out
    al = functools.partial(_alive, con)
    rtype = "COALESCE(tr.rsrc_type, r.rsrc_type)" if "rsrc_type" in _cols(con, "TASKRSRC") else "r.rsrc_type"
    for code, q, s_, e in con.execute(
            "SELECT x.task_code, COALESCE(tr.target_qty, 0), tr.target_start_date, tr.target_end_date FROM TASKRSRC tr "
            "JOIN TASK x ON x.task_id = tr.task_id LEFT JOIN RSRC r ON r.rsrc_id = tr.rsrc_id" + al("RSRC", "r")
            + f" WHERE x.proj_id = ? AND {rtype} IN ('RT_Labor', 'RT_Equip')" + al("TASKRSRC", "tr") + al("TASK", "x"),
            (bid,)):
        w = wbs_of.get(code)
        if w is None or not q:
            continue
        s_, e = _db_dt(s_), _db_dt(e)
        acc = out.setdefault(w, {"bl_plan": 0.0, "bl_budget": 0.0})
        acc["bl_budget"] += float(q)
        if s_ and e:
            acc["bl_plan"] += _value_at([(float(q), s_, max(s_, e))], dd)
    return out
