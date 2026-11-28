"""Schedule analysis: DCMA 14 checks and the health check, dangling ends, out-of-sequence progress, project
comparison and update integrity."""

from __future__ import annotations

import functools
import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Optional, get_args

from p6mcp.core import state as _state
from p6mcp.core.constants import (
    CSTR_FINISH, _cstr_label, DCMA_LIMIT_DAYS, HARD_CSTR, HPD_SQL, MAT_SQL, REL_TYPES_REV, SUMMARY_TYPES,
)
from p6mcp.registry import DcmaCheck, IntegrityFlag, ThresholdCheck
from p6mcp.core.db import _act_row, _alive, _cols, _days, _db_dt, _dict, _fmt_dt, _num
from p6mcp.core.props import (
    _db_task_code, _lag_hpd, _lag_hpd_sql, _lag_rule, _opt_eq, _props, _sched_diff, _sched_options,
)
from p6mcp.core.calendar import _Cal, _calendar, _calendars, WEEKDAYS
from p6mcp.core.editlog import _log_dir
from p6mcp.core.read import _critical_clause
from p6mcp.core.engine import CPM_TF_TOL_H, CPM_TOL, _float_walk, GAP_TOL_H, LINK_FROM, LINK_TO, _Net, _net_load
from p6mcp.messages import _join, _t


# DCMA 14 check -> (DCMA default threshold, rule, PROJPROP 'schedule_check' target, its limit in hours, on/off flag).
# rule: max = pct at most the threshold, min = pct at least, ratio = value at least; threshold None = report only.
DCMA_CHECKS: dict[str, tuple] = {
    "logic": (5, "max", "logic_target", None, "check_logic"),
    "logic_density": (1.5, "ratio", None, None, None),  # not a P6 check: links with an open successor / open activities
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
# Checks of p6_health_check checks left out of summary, score, dcma_failed and the dashboard's DCMA table (those
# stay the DCMA 14): logic_density counts in weighted_score only.
HC_UNSCORED = ("logic_density",)
# weighted_score: weight per check; pass earns the weight, warn HC_WARN_SHARE of it, fail 0, not evaluated is left
# out. The weights add up to 110, so points = earned / weight of the evaluated checks x 100.
HC_WEIGHTS = {"logic_density": 20, "logic": 20, "critical_path_test": 15, "negative_float": 15, "high_float": 10,
              "hard_constraints": 10, "high_duration": 5, "leads": 5, "lags": 5, "fs_links": 5}
HC_WARN_SHARE = 0.5
HC_WARN_MAX = 2.0  # an "at most" check that fails warns while its % is at most this many times the threshold
HC_WARN_MIN = 0.9  # an "at least" check that fails warns while its % or value is at least this share of it
HC_GRADES = (("A", 90.0), ("B", 80.0), ("C", 70.0), ("D", 55.0))  # grade when points are at least; F below
assert set(HC_WEIGHTS) <= set(DCMA_CHECKS) and set(HC_UNSCORED) <= set(DCMA_CHECKS)


def _dcma_settings(con, pid: int, overrides: Mapping, high_float_days: Optional[float]) -> dict:
    """Threshold and limit of each DCMA check: the project's PROJPROP 'schedule_check' when stored (its
    *_constraint limits are hours; a check_* flag 'N' disables the check), else the DCMA defaults (limits
    44 d of each calendar). overrides (by check name; they also evaluate a disabled check) and
    high_float_days (days) win."""
    if "critical_path_test" in overrides:
        raise ValueError(_t("critical_path_test is not run (needs a CPM engine): it takes no threshold"))
    bad = sorted(set(overrides) - set(DCMA_CHECKS))
    if bad:
        raise ValueError(_t("Unknown check(s) in thresholds: {bad}; checks: {checks}", bad=bad,
                            checks=list(get_args(ThresholdCheck))))
    nan = [k for k, v in overrides.items() if _num(v) is None]
    if nan:
        raise ValueError(_t("thresholds must be numbers: {nan}", nan=nan))
    hfd = None if high_float_days is None else _num(high_float_days)
    if high_float_days is not None and (hfd is None or hfd < 0):
        raise ValueError(_t("high_float_days must be 0 or more"))
    tree = _props(con, pid, "schedule_check")
    p = {k.lower(): v for k, v in tree["attrs"].items()} if tree and not tree.get("error") else {}
    out: dict[str, Any] = {"source": "PROJPROP schedule_check" if p else _t("DCMA defaults"), "threshold": {},
                           "limit": {}, "disabled": [],
                           "notes": [_t("{error}: DCMA defaults used", error=tree["error"])] if tree and tree.get("error")
                           else []}
    for name, (default, _, key, lim, flag) in DCMA_CHECKS.items():
        thr = default if _num(p.get(key)) is None else _num(p[key])
        if name == "cpli" and _num(p.get(key)) is not None:  # P6 stores a tolerance in % (5 beside bei_target 0.95)
            thr = round(1 - thr / 100, 4)
            out["notes"].append(_t("cpli_target {target} read as a tolerance in %: CPLI at least {min:g}", target=p[key],
                                   min=thr))
        if p.get(flag) == "N" and name in overrides:
            out["notes"].append(_t("{check}: disabled in the project's Check Schedule settings, evaluated with "
                                   "thresholds=", check=name))
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
        return {**out, "note": _t("not computed: needs a data date, a scheduled finish after it and a must finish by "
                                  "date (PROJECT.plan_end_date)")}
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
            "basis": _t("working days of the project calendar {calendar!r}", calendar=cal.name) if wd else
                     _t("calendar days (no readable project calendar)"),
            "note": _t("approximation from the project dates of the last F9 (data date, scd_end_date, plan_end_date)")}


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
                          exempt=[{"task_code": r[1], "milestone": _t("project start (no predecessor)") if k == "start"
                                   else _t("project finish (no successor)")}
                                  for k, r in ms.items() if r],
                          dangling={"open_start": dangling(f"{hp} AND NOT " + has["pred"].format(
                                        " AND r.pred_type IN ('PR_FS', 'PR_SS')")),
                                    "open_finish": dangling(f"{hs} AND NOT " + has["succ"].format(
                                        " AND r.pred_type IN ('PR_FS', 'PR_FF')")),
                                    "note": _t("information only (not in count or pass): start driven only by FF/SF "
                                               "predecessors, finish driving only SS/SF successors; milestones left "
                                               "out")})
    # logic density: links per open activity (not a P6 check, out of summary and score: see HC_UNSCORED)
    checks["logic_density"] = _chk(n_olinks, n_open, thr["logic_density"], "ratio",
                                   value=n_olinks / n_open if n_open else None)
    checks["logic_density"]["pct"] = None  # a ratio, not a share
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
    checks["long_lags"] = rel("long_lags", cond, **extra, basis=_t("lag days of the {basis}", basis=lag_basis) if "limit_d" in extra
                              else _t("lag hours (PROJPROP long_lags_constraint)"))
    checks["fs_links"] = rel("fs_links", "r.pred_type = 'PR_FS'")
    checks["fs_links"] = {**{k: v for k, v in checks["fs_links"].items() if k != "sample"}, "ladder_pairs": n_lad,
                          "fs_pct_excluding_ladders": pct(checks["fs_links"]["count"], n_olinks - 2 * n_lad),
                          "ladder_note": _t("information only: SS + FF pairs with the same lag left out"),
                          "sample": checks["fs_links"]["sample"]}
    cond = f"t.cstr_type IN {HARD_CSTR}" + (f" OR t.cstr_type2 IN {HARD_CSTR}" if "cstr_type2" in tcols else "")
    checks["hard_constraints"] = act("hard_constraints", op, cond, n_open)
    def worst(cond: str, order: str) -> Optional[dict]:  # the open activity with the lowest / highest float in days
        r = con.execute(f"SELECT t.task_code, t.task_name, t.total_float_hr_cnt, {HPD_SQL} {acts}{op} AND ({cond}) "
                        f"ORDER BY t.total_float_hr_cnt / {HPD_SQL} {order}, t.task_code, t.task_id LIMIT 1",
                        args).fetchone()
        return None if r is None else {"task_code": r[0], "task_name": r[1], "total_float_d": _days(r[2], r[3])}
    cond, extra = _limit(lim["high_float"], "t.total_float_hr_cnt", HPD_SQL, "hf", args)
    checks["high_float"] = act("high_float", op, cond, n_open, **extra, worst=worst(cond, "DESC"))
    checks["negative_float"] = act("negative_float", op, "t.total_float_hr_cnt < 0", n_open,
                                   worst=worst("t.total_float_hr_cnt < 0", "ASC"))
    col = "t.remain_drtn_hr_cnt" if basis == "remaining" else "t.target_drtn_hr_cnt"
    cond, extra = _limit(lim["high_duration"], col, HPD_SQL, "hd", args)
    checks["high_duration"] = act("high_duration", work, cond, n_work, **extra,
                                  basis=_t("remaining duration of open TT_Task and TT_Rsrc activities")
                                  if basis == "remaining" else
                                  _t("original duration of open TT_Task and TT_Rsrc activities")
                                  if basis == "original" else
                                  _t("{basis} duration of open TT_Task and TT_Rsrc activities", basis=basis))
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
        checks["invalid_dates"] = _chk(None, n_all, thr["invalid_dates"], "max", note=_t("no data date"))
    if _cols(con, "TASKRSRC"):
        checks["resources"] = act("resources", work, "COALESCE(t.target_drtn_hr_cnt, 0) > 0 AND NOT EXISTS (SELECT 1 FROM "
                                  "TASKRSRC a WHERE a.task_id = t.task_id" + al("TASKRSRC", "a") + ")", n_work,
                                  basis=_t("open TT_Task/TT_Rsrc with a duration and no TASKRSRC assignment"))
        if thr["resources"] is None:
            checks["resources"]["note"] = _t("not evaluated: DCMA sets no threshold and the project stores none; "
                                             "thresholds={{'resources': 0}} evaluates it")
    else:
        checks["resources"] = _chk(None, n_work, thr["resources"], "max", note=_t("no TASKRSRC table"))
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
        smp = [_t("{code} baseline finish {date}: not in the project", code=r[0], date=r[1]) if r[2] is None else
               _t("{code} baseline finish {date}: finished {finish}", code=r[0], date=r[1], finish=r[3])
               if r[2] == "TK_Complete" else
               _t("{code} baseline finish {date}: not finished", code=r[0], date=r[1]) for r in rows]
        checks["missed_tasks"] = _chk(n_miss, n_due, thr["missed_tasks"], "max", smp, basis=(
            _t("baseline finish (actual, else early) at or before the data date: not finished, or finished on a later "
               "day") if baseline_dates == "current" else
            _t("baseline finish (planned) at or before the data date: not finished, or finished on a later day")))
        done = n(acts, "t.status_code = 'TK_Complete'")
        bei = _chk(done, n_due, thr["bei"], "ratio", value=done / n_due if n_due else None,
                   basis=_t("completed activities / activities whose baseline finish is at or before the data date"))
    else:
        why = _t("no baseline: pass baseline= or set a project baseline in P6") if bid is None else _t("no data date")
        checks["missed_tasks"] = _chk(None, None, thr["missed_tasks"], "max", note=why)
        bei = _chk(None, None, thr["bei"], "ratio", note=why)
    checks["critical_path_test"] = _chk(None, None, None, None, activity=None, activity_name=None, note=(
        _t("not run here: full reports only (CPM engine, see p6_cpm_validate)")))
    checks["cpli"] = _cpli(con, prj, thr["cpli"])
    checks["bei"] = bei
    for name in cfg["disabled"]:
        checks[name]["note"] = _join("; ", filter(None, (checks[name].get("note"), _t(
            "disabled in the project's Check Schedule settings (PROJPROP schedule_check)"))))
    return checks, n_open, n_links, ends, items


HC_NO_ITEMS = {"logic_density", "critical_path_test", "cpli", "bei"}  # project-level values, no item list


def _hc_status(name: str, c: Mapping) -> str:
    """pass, warn, fail or not_evaluated of one weighted check: a failed check warns inside its band (an "at
    most" check up to HC_WARN_MAX x its threshold, an "at least" check down to HC_WARN_MIN x it; none for a
    zero threshold or a check without threshold), compared before rounding."""
    ok = c.get("pass")
    if ok is None:
        return "not_evaluated"
    if ok:
        return "pass"
    rule, thr = DCMA_CHECKS[name][1], c.get("threshold")
    count, den = c.get("count"), c.get("denominator")
    if not thr or rule is None:
        return "fail"
    if rule == "ratio":
        v = count / den if count is not None and den else c.get("value")
        return "warn" if v is not None and v >= HC_WARN_MIN * thr else "fail"
    if count is None or not den:
        return "fail"
    pct = 100 * count / den
    return "warn" if (pct <= HC_WARN_MAX * thr if rule == "max" else pct >= HC_WARN_MIN * thr) else "fail"


def _weighted_score(checks: Mapping) -> dict:
    """weighted_score of a health check (HC_WEIGHTS): per check weight, status and points earned; points =
    earned / weight of the evaluated checks x 100 (1 decimal), grade from the points as written."""
    rows, earned, top = {}, 0.0, 0
    for name, w in HC_WEIGHTS.items():
        st = _hc_status(name, checks.get(name) or {})
        e = None if st == "not_evaluated" else float(w if st == "pass" else w * HC_WARN_SHARE if st == "warn" else 0)
        if e is not None:
            earned, top = earned + e, top + w
        rows[name] = {"weight": w, "status": st, "earned": e}
    points = round(100 * earned / top, 1) if top else None
    grade = None if points is None else next((g for g, lo in HC_GRADES if points >= lo), "F")
    return {"points": points, "grade": grade, "earned": earned, "max": top, "checks": rows}


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
                         "detail": _t("the constraint date is later than the project must finish date")})
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
                         "detail": _t("the work and lags after its constraint date reach {task_code} on {proj} "
                                      "({need} d later), after the must finish date",
                                      task_code=fm['task_code'], proj=_fmt_dt(proj), need=round(need, 2))})
    miles = [(d, ty, dt) for d, ty, dt in cons if d["task_type"] in ("TT_Mile", "TT_FinMile")]
    for d, _ty, dt in miles:
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
                             "detail": _t("the work and lags between {task_code} ({dt}) and it take {gap} d: it "
                                          "cannot be met before {earliest}",
                                          task_code=d['task_code'], dt=_fmt_dt(dt), gap=round(gap, 2), earliest=_fmt_dt(earliest))})
    if fm is not None and net.must and net.end and net.end > net.must + tol:
        rows.append({"task_code": fm["task_code"], "task_name": fm["task_name"], "task_type": fm["task_type"],
                     "issue": "project_finish_after_must_finish", "project_finish": net.prj["scd_end_date"],
                     "must_finish": net.prj["plan_end_date"],
                     "detail": _t("the last F9 finishes the project after its must finish date")})
    rows.sort(key=lambda r: (r["task_code"], r["issue"], r.get("other") or ""))
    n_items = len({r["task_code"] for r in rows})
    # count = issue rows (what check='key_dates' pages); pct = constraints (plus the must finish) with an issue
    hit = len({(r["task_code"], r.get("cstr_type")) for r in rows})
    denom = len(cons) + (1 if net.must else 0)
    chk = _chk(len(rows), denom, thr, "max",
               [f"{r['task_code']} {r['issue']}" + (f" ({r['other']})" if r.get("other") else "") for r in rows[:15]],
               activities=n_items, finish_milestone=fm and fm["task_code"], must_finish=net.prj["plan_end_date"],
               note=_t("information only; items: check='key_dates'"))
    chk["pct"] = round(100 * hit / denom, 2) if denom else None
    return chk, rows


def _hc_snap_dir() -> str:
    return os.path.join(_log_dir(), "health")


def _hc_snapshots(project: Optional[str] = None, this_db: bool = True) -> list[dict]:
    """Health snapshots (metadata and content), oldest first."""
    folder, out = _hc_snap_dir(), []
    if not os.path.isdir(folder):
        return out
    db = os.path.basename(_state.DB_PATH).lower()
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


def _hc_pick(project: str, ref: str, snaps: list[dict], name: str) -> dict:
    if ref in ("latest", "previous"):
        k = 1 if ref == "latest" else 2
        if len(snaps) < k:
            raise ValueError(_t("{name}={ref!r}: {project} has {n_snaps} health snapshot(s) of this database; run "
                                "p6_health_check first (each full run saves one)",
                                name=name, ref=ref, project=project, n_snaps=len(snaps)))
        return snaps[-k]
    for s in _hc_snapshots(None, this_db=False):
        if s.get("id") == ref:
            return s
    raise ValueError(_t("{name}: no health snapshot {ref!r} (ids: p6_health_check_compare lists them in snapshots)", name=name, ref=ref))


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
                        "reason": _t("ladder: {task_code} drives its finish (FF): add the matching SS (same lag), or "
                                     "an FS if the work cannot overlap", task_code=p['task_code'])}
        cands = [x for x in net.t.values() if x["proj_id"] == net.pid and x["wbs_id"] == d["wbs_id"]
                 and x["task_id"] != d["task_id"] and not net.summary(x) and x["ef"] and d["es"] and x["ef"] <= d["es"]
                 and not any(lk["pred"] == x["task_id"] for lk in net.by_succ.get(d["task_id"], []))]
        if cands:
            x = max(cands, key=lambda x: (x["ef"], x["task_code"]))
            return {"items": [{"predecessor_code": x["task_code"], "successor_code": me, "rel_type": "FS", "lag_days": 0}],
                    "reason": _t("closest activity of the same WBS finishing before its early start ({early_end_date})",
                                 early_end_date=x['early_end_date'])}
        return None
    for lk in sorted(net.by_pred.get(d["task_id"], []), key=lambda x: x["id"]):
        s = net.t[lk["succ"]]
        if lk["type"] == "PR_SS" and not net.summary(s) and s["proj_id"] == net.pid:
            return {"items": [{"predecessor_code": me, "successor_code": s["task_code"], "rel_type": "FF",
                               "lag_days": _days(lk["lag"], net.lag_hpd(d, s))}],
                    "alternative": [{"predecessor_code": me, "successor_code": s["task_code"], "rel_type": "FS",
                                     "lag_days": 0}],
                    "reason": _t("ladder: it drives the start of {task_code} (SS): add the matching FF (same lag), or "
                                 "an FS if the work cannot overlap", task_code=s['task_code'])}
    cands = [x for x in net.t.values() if x["proj_id"] == net.pid and x["wbs_id"] == d["wbs_id"]
             and x["task_id"] != d["task_id"] and not net.summary(x) and x["status_code"] != "TK_Complete"
             and x["es"] and d["ef"] and x["es"] >= d["ef"]
             and not any(lk["succ"] == x["task_id"] for lk in net.by_pred.get(d["task_id"], []))]
    if cands:
        x = min(cands, key=lambda x: (x["es"], x["task_code"]))
        return {"items": [{"predecessor_code": me, "successor_code": x["task_code"], "rel_type": "FS", "lag_days": 0}],
                "reason": _t("closest activity of the same WBS starting after its early finish ({early_start_date})",
                             early_start_date=x['early_start_date'])}
    return None


# Out-of-sequence progress on a link r (successor t, predecessor p)
OOS_SQL = {"FS": "r.pred_type = 'PR_FS' AND COALESCE(r.lag_hr_cnt, 0) >= 0 AND t.act_start_date IS NOT NULL "
                 "AND p.status_code <> 'TK_Complete'",
           "FS_lead": "r.pred_type = 'PR_FS' AND r.lag_hr_cnt < 0 AND t.act_start_date IS NOT NULL "
                      "AND p.status_code <> 'TK_Complete'",
           "SS": "r.pred_type = 'PR_SS' AND t.act_start_date IS NOT NULL AND p.act_start_date IS NULL",
           "FF": "r.pred_type = 'PR_FF' AND t.status_code = 'TK_Complete' AND p.status_code <> 'TK_Complete'",
           "SF": "r.pred_type = 'PR_SF' AND t.status_code = 'TK_Complete' AND p.act_start_date IS NULL"}


def _cmp_acts(con, dates: str, crit: tuple[str, str] = ("0", "0")) -> str:
    """CTEs a (project :pid) and b (other :oid) of the activity comparison: start and finish = actual,
    else early ('planned' takes b's planned dates), hpd, original duration and total float hours, op (open:
    not completed, not LOE or WBS summary) and crit (open and critical under crit, the condition on TASK t
    of each side's critical definition, see _crit_named)."""
    tcols = _cols(con, "TASK")
    extra = ", ".join(f"t.{k}" if k in tcols else f"NULL AS {k}" for k in ("cstr_type2", "cstr_date2"))

    def side(p: str, st: str, fin: str, cond: str) -> str:
        op = f"(t.status_code <> 'TK_Complete' AND t.task_type NOT IN {SUMMARY_TYPES})"
        return (f"SELECT t.task_id, t.task_code, t.task_name, t.status_code, t.clndr_id, {HPD_SQL} AS hpd, "
                f"t.target_drtn_hr_cnt AS orig_h, t.total_float_hr_cnt AS tf_h, {st} AS st, {fin} AS fin, {op} AS op, "
                f"({op} AND COALESCE({cond}, 0)) AS crit, t.task_type, t.remain_drtn_hr_cnt AS rem_h, "
                f"t.act_start_date AS as_d, t.act_end_date AS af_d, t.cstr_type, t.cstr_date, {extra} FROM TASK t "
                f"LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id WHERE t.proj_id = :{p}" + _alive(con, "TASK", "t"))
    cur = ("COALESCE(t.act_start_date, t.early_start_date)", "COALESCE(t.act_end_date, t.early_end_date)")
    oth = cur if dates == "current" else ("t.target_start_date", "t.target_end_date")
    return f"WITH a AS {MAT_SQL}({side('pid', *cur, crit[0])}), b AS {MAT_SQL}({side('oid', *oth, crit[1])}) "


def _crit_named(con, pid: int, key: str, args: dict) -> tuple[str, str]:
    """(condition on TASK t, definition) of the project's critical definition (_critical_clause 'project')
    with its value as the named parameter :key, added to args."""
    cond, cargs, cdef = _critical_clause(con, pid, "project", None)[:3]
    for i, v in enumerate(cargs):
        args[f"{key}{i}"] = v
        cond = cond.replace("?", f":{key}{i}", 1)
    return cond, cdef


SLIP_CAUSES = ("late_start", "longer_duration", "late_start_and_longer_duration", "on_time", "earlier")


def _slip(cal: Optional["_Cal"], s, f, bs, bf) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """(start_variance_wd, duration_delta_wd, slip_cause) of an activity against the other project, in
    working days of its calendar: start variance = other start to start, duration delta = (start to
    finish) minus (other start to other finish); they add up to the finish variance exactly (working
    time is additive). slip_cause (SLIP_CAUSES): a finish later by more than GAP_TOL_H is late_start,
    longer_duration or both (each part above GAP_TOL_H; else the larger part), on_time within GAP_TOL_H,
    else earlier. All None without a readable calendar or a date."""
    s, f, bs, bf = (_db_dt(x) for x in (s, f, bs, bf))
    if cal is None or cal.error or not (s and f and bs and bf):
        return None, None, None
    sv, fv = cal.work_hours(bs, s), cal.work_hours(bf, f)
    dv = fv - sv
    if fv > GAP_TOL_H:
        late, longer = sv > GAP_TOL_H, dv > GAP_TOL_H
        cause = ("late_start_and_longer_duration" if late and longer else "late_start" if late else "longer_duration"
                 if longer else "late_start" if sv >= dv else "longer_duration")
    else:
        cause = "on_time" if fv >= -GAP_TOL_H else "earlier"
    return round(sv / cal.hpd, 2) + 0.0, round(dv / cal.hpd, 2) + 0.0, cause


def _cmp_links(con) -> str:
    """CTEs la (project :pid) and lb (other :oid): links whose successor is in the project, keyed on
    (pred, succ, type, pred_project); pred_project is NULL when the predecessor is in that project."""
    def side(p: str) -> str:
        return ("SELECT r.task_pred_id AS rid, pt.task_code AS pred, st.task_code AS succ, pt.task_name AS pred_name, "
                "st.task_name AS succ_name, r.pred_type AS type, "
                f"CASE WHEN pt.proj_id = :{p} THEN NULL ELSE pp.proj_short_name END AS pred_project, r.lag_hr_cnt AS lag_h, "
                "COALESCE(NULLIF(cp.day_hr_cnt, 0), 8) AS pred_hpd, COALESCE(NULLIF(cs.day_hr_cnt, 0), 8) AS succ_hpd "
                "FROM TASKPRED r JOIN TASK st ON st.task_id = r.task_id JOIN TASK pt ON pt.task_id = r.pred_task_id "
                "LEFT JOIN PROJECT pp ON pp.proj_id = pt.proj_id LEFT JOIN CALENDAR cp ON cp.clndr_id = pt.clndr_id "
                f"LEFT JOIN CALENDAR cs ON cs.clndr_id = st.clndr_id WHERE st.proj_id = :{p}"
                + _alive(con, "TASKPRED", "r") + _alive(con, "TASK", "st") + _alive(con, "TASK", "pt"))

    def one(p: str) -> str:  # a key linked twice counts once: the lowest id (bare columns follow MIN)
        return (f"SELECT MIN(rid) AS rid, pred, succ, pred_name, succ_name, type, pred_project, lag_h, pred_hpd, "
                f"succ_hpd FROM ({side(p)}) GROUP BY pred, succ, type, pred_project")
    return f"WITH la AS {MAT_SQL}({one('pid')}), lb AS {MAT_SQL}({one('oid')}) "


LINK_KEY = "lb.pred = la.pred AND lb.succ = la.succ AND lb.type = la.type AND lb.pred_project IS la.pred_project"


def _tf_change(before_d: Optional[float], after_d: Optional[float], crit_before, crit_after) -> dict:
    """Total float and critical flags of an activity in the other project (before) and the project (after):
    tf_delta_d = after - before (days of each side's calendar); None where a side lacks the activity."""
    return {"total_float_before_d": before_d, "total_float_after_d": after_d,
            "tf_delta_d": None if before_d is None or after_d is None else round(after_d - before_d, 2) + 0.0,
            "critical_before": None if crit_before is None else bool(crit_before),
            "critical_after": None if crit_after is None else bool(crit_after)}


INTEGRITY_FLAGS: tuple[str, ...] = get_args(IntegrityFlag)
INTEGRITY_CAVEAT = (_t("Each flag is a question to raise with whoever submitted the update, not proof of "
                       "manipulation: a change can be a correct revision (new information, a re-plan, a corrected "
                       "error). Ask for the reason and the evidence behind it."))
INTEGRITY_DEFS = {
    "open_end_to_finish": _t("activities that had no successor (or no predecessor) in the previous update and now have "
                          "exactly one, straight to the project finish (the activity with the latest finish, or a "
                          "finish milestone without successor) or from the project start (the earliest activity, or a "
                          "start milestone without predecessor)"),
    "negative_float_cleared_by_constraint": _t("open activities with negative total float in the previous update and "
                                            "zero or more now, where a constraint was removed or changed on the "
                                            "activity or on the activity its float came from in the previous update "
                                            "(as p6_float_origin), or the must finish by date it came from moved later"),
    "must_finish_moved": _t("the project must finish by date (PROJECT.plan_end_date) differs"),
    "actuals_changed": _t("actual start or finish dates the previous update already had that differ now by more than a "
                       "minute, or are gone (reported history rewritten)"),
    "duration_cut": _t("activities not started in either update whose original or remaining duration is shorter now "
                    "(critical: the ones critical in the previous update)"),
    "links_removed_on_critical": _t("links of the previous update missing now, both activities still in the project, "
                                 "whose successor was open and critical, or within near_critical_days of total float, "
                                 "in the previous update"),
    "rel_type_or_lag_changed": _t("links between the same two activities whose types changed (SS, FF or SF turned into "
                               "FS, and back), and links of the same type whose lag changed or was removed"),
    "schedule_options_changed": _t("scheduling options (PROJPROP scheduling) and the critical definition (PROJECT "
                                "critical_path_type, critical_drtn_hr_cnt) that differ"),
    "calendars_changed": _t("open activities whose calendar changed (by name), and calendars used by open activities "
                         "whose work week or exceptions (holidays) differ from the same calendar of the previous update"),
    "deleted_in_progress_or_critical": _t("activities of the previous update missing now that were in progress or "
                                       "critical in it"),
    "data_date": _t("the data date did not move forward since the previous update"),
}
assert set(INTEGRITY_DEFS) == set(INTEGRITY_FLAGS)


def _cstr_txt(ty, dt) -> str:
    return "none" if not ty else f"{_cstr_label(ty)} {dt}" if dt else _cstr_label(ty)


def _integrity(con, pid: int, oid: int, near_days: float) -> tuple[dict[str, list], dict]:
    """({flag: rows}, facts) of p6_update_integrity: project pid (after) against oid (before, the previous update),
    activities matched by Activity ID and links by (predecessor, successor, type, predecessor project) as
    p6_compare_projects; run inside one read snapshot."""
    args: dict[str, Any] = {"pid": pid, "oid": oid}
    (ca, cdef_a), (cb, cdef_b) = _crit_named(con, pid, "ca", args), _crit_named(con, oid, "cb", args)
    acts = _cmp_acts(con, "current", (ca, cb))
    A: dict[str, dict] = {}
    B: dict[str, dict] = {}
    for side, out in (("a", A), ("b", B)):
        for r in con.execute(acts + f"SELECT * FROM {side} ORDER BY task_id", args):
            out.setdefault(r["task_code"], _dict(r))  # an Activity ID twice: the lowest task_id
    LA: dict[tuple, dict] = {}
    LB: dict[tuple, dict] = {}
    for side, out in (("la", LA), ("lb", LB)):
        for r in con.execute(_cmp_links(con) + f"SELECT * FROM {side} ORDER BY rid", args):
            out[(r["pred"], r["succ"], r["type"], r["pred_project"])] = _dict(r)
    pcols = _cols(con, "PROJECT")
    prj = {p: _dict(con.execute("SELECT " + ", ".join(c if c in pcols else f"NULL AS {c}" for c in (
        "last_recalc_date", "plan_end_date", "clndr_id", "critical_path_type", "critical_drtn_hr_cnt"))
        + " FROM PROJECT WHERE proj_id = ?", (p,)).fetchone()) for p in (pid, oid)}
    cals = _calendars(con)
    rule = {p: _lag_rule(con, p)[0] for p in (pid, oid)}
    net_b = _net_load(con, oid)
    tol, out = CPM_TF_TOL_H, {f: [] for f in INTEGRITY_FLAGS}
    summ = lambda r: r["task_type"] in ("TT_LOE", "TT_WBS")  # noqa: E731
    cst = lambda r: (r["cstr_type"], r["cstr_date"], r["cstr_type2"], r["cstr_date2"])  # noqa: E731
    both = sorted(set(A) & set(B))

    def lag_d(lk: dict, p: int) -> float:
        return _days(lk["lag_h"] or 0, _lag_hpd(con, {"hpd": lk["pred_hpd"]}, {"hpd": lk["succ_hpd"]}, p, rule[p])[0])

    def ends(L: dict, X: dict) -> tuple[dict, dict]:  # links inside the project between activities that are no summary
        succ, pred = {}, {}
        for k in L:
            p, s, _ty, pp = k
            if pp is None and p in X and s in X and not summ(X[p]) and not summ(X[s]):
                succ.setdefault(p, []).append(k)
                pred.setdefault(s, []).append(k)
        return succ, pred
    sa, pa = ends(LA, A)
    sb, pb = ends(LB, B)
    own = [c for c, r in A.items() if not summ(r)]
    fin_a = max(own, key=lambda c: (A[c]["fin"] or "", A[c]["task_type"] == "TT_FinMile", c), default=None)
    st_a = min(own, key=lambda c: (A[c]["st"] or "9999", A[c]["task_type"] != "TT_Mile", c), default=None)
    targets = {"successor": {c for c in own if A[c]["task_type"] == "TT_FinMile" and c not in sa} | {fin_a},
               "predecessor": {c for c in own if A[c]["task_type"] == "TT_Mile" and c not in pa} | {st_a}}
    for c in both:
        if summ(A[c]):
            continue
        for kind, before, now, end in (("successor", sb, sa, 1), ("predecessor", pb, pa, 0)):
            if c in before or len(now.get(c, ())) != 1 or now[c][0] in LB or now[c][0][end] not in targets[kind]:
                continue
            k = now[c][0]
            o = k[end]
            lk = LA[k]
            out["open_end_to_finish"].append({
                "task_code": c, "task_name": A[c]["task_name"], "side": kind, "linked_to": o,
                "linked_to_name": A[o]["task_name"], "type": REL_TYPES_REV.get(k[2], k[2]), "lag_d": lag_d(lk, pid),
                "before": _t("no successor") if kind == "successor" else _t("no predecessor"),
                "after": _t("{type} {lag:g} d to {code}", type=REL_TYPES_REV.get(k[2], k[2]), lag=lag_d(lk, pid), code=o)
                if kind == "successor" else
                _t("{type} {lag:g} d from {code}", type=REL_TYPES_REV.get(k[2], k[2]), lag=lag_d(lk, pid), code=o)})
    must_b, must_a = (_db_dt(prj[p]["plan_end_date"]) for p in (oid, pid))
    for c in both:
        a, b = A[c], B[c]
        if not (a["op"] and b["op"]) or a["tf_h"] is None or b["tf_h"] is None or not (b["tf_h"] < -tol <= a["tf_h"]):
            continue
        causes, source = [], None
        if cst(a) != cst(b):
            causes.append(_t("its own constraint was removed or changed"))
        nb = net_b.t.get(b["task_id"])
        if nb is not None and nb["lf"] is not None:
            for s in _float_walk(net_b, nb)["sources"]:
                if s["type"] == "activity_constraint" and s["task_code"] != c:
                    na, ob = A.get(s["task_code"]), B.get(s["task_code"])
                    if na is None or ob is None or cst(na) != cst(ob):
                        causes.append(_t("the constraint of {code}, the source of its float, was removed or changed",
                                         code=s["task_code"]))
                        source = source or s["task_code"]
                elif s["type"] == "project_must_finish" and must_b and (must_a is None or must_a > must_b):
                    causes.append(_t("the must finish by date, the source of its float, moved later"))
                    source = source or "must finish by"
        if causes:
            out["negative_float_cleared_by_constraint"].append({
                "task_code": c, "task_name": a["task_name"], "total_float_before_d": _days(b["tf_h"], b["hpd"]),
                "total_float_after_d": _days(a["tf_h"], a["hpd"]), "cause": _join("; ", dict.fromkeys(causes)),
                "source": source, "constraint_before": _cstr_txt(b["cstr_type"], b["cstr_date"]),
                "constraint_after": _cstr_txt(a["cstr_type"], a["cstr_date"])})
    pcal = cals.get(prj[pid]["clndr_id"])
    pcal = pcal if pcal is not None and not pcal.error else None
    cd = lambda x, y: None if x is None or y is None else round((y - x).total_seconds() / 86400, 2) + 0.0  # noqa: E731
    must = {"before": prj[oid]["plan_end_date"], "after": prj[pid]["plan_end_date"], "delta_cd": cd(must_b, must_a),
            "delta_wd": None if pcal is None or not (must_a and must_b) else round(pcal.work_days(must_b, must_a), 2) + 0.0}
    if must_a != must_b:
        out["must_finish_moved"].append(dict(must))
    for c in both:
        for fld, name in (("as_d", "actual_start"), ("af_d", "actual_finish")):
            vb, va = _db_dt(B[c][fld]), _db_dt(A[c][fld])
            if vb is not None and (va is None or abs(va - vb) > CPM_TOL):
                out["actuals_changed"].append({"task_code": c, "task_name": A[c]["task_name"], "field": name,
                                               "before": B[c][fld], "after": A[c][fld], "delta_cd": cd(vb, va)})
    for c in both:
        a, b = A[c], B[c]
        if summ(a) or a["task_type"] in ("TT_Mile", "TT_FinMile") or "TK_NotStart" != a["status_code"] or \
                b["status_code"] != "TK_NotStart":
            continue
        if (b["orig_h"] or 0) - (a["orig_h"] or 0) > tol or (b["rem_h"] or 0) - (a["rem_h"] or 0) > tol:
            out["duration_cut"].append({
                "task_code": c, "task_name": a["task_name"], "original_before_d": _days(b["orig_h"], b["hpd"]),
                "original_after_d": _days(a["orig_h"], a["hpd"]), "remaining_before_d": _days(b["rem_h"], b["hpd"]),
                "remaining_after_d": _days(a["rem_h"], a["hpd"]), "critical_before": bool(b["crit"])})
    pair_a: dict[tuple, dict] = {}
    pair_b: dict[tuple, dict] = {}
    for L, P in ((LA, pair_a), (LB, pair_b)):
        for k, lk in L.items():
            P.setdefault((k[0], k[1], k[3]), {})[k[2]] = lk
    for k in sorted(set(LB) - set(LA), key=lambda k: (k[1], k[0], k[2], k[3] or "")):
        p, s, ty, pp = k
        if s not in A or (pp is None and p not in A) or s not in B or not B[s]["op"] or (p, s, pp) in pair_a:
            continue  # a removed activity takes its links (deleted_in_progress_or_critical), a pair still
            # linked by another type is a type change (rel_type_or_lag_changed)
        sbf = B[s]
        crit, near = bool(sbf["crit"]), sbf["tf_h"] is not None and sbf["tf_h"] <= near_days * (sbf["hpd"] or 8.0)
        if crit or near:
            lk = LB[k]
            out["links_removed_on_critical"].append({
                "pred_code": p, "pred_name": lk["pred_name"], "pred_project": pp, "succ_code": s,
                "succ_name": lk["succ_name"], "type": REL_TYPES_REV.get(ty, ty), "lag_d": lag_d(lk, oid),
                "successor_total_float_before_d": _days(sbf["tf_h"], sbf["hpd"]), "successor_critical_before": crit})
    txt = lambda ts, p: ", ".join(f"{REL_TYPES_REV.get(t, t)} {lag_d(ts[t], p):g} d" for t in sorted(ts))  # noqa: E731
    for pk in sorted(set(pair_a) & set(pair_b), key=lambda x: (x[1], x[0], x[2] or "")):
        ta, tb = pair_a[pk], pair_b[pk]
        row = {"pred_code": pk[0], "pred_name": next(iter(ta.values()))["pred_name"], "pred_project": pk[2],
               "succ_code": pk[1], "succ_name": next(iter(ta.values()))["succ_name"]}
        if set(ta) != set(tb):
            out["rel_type_or_lag_changed"].append({**row, "change": "type", "before": txt(tb, oid), "after": txt(ta, pid),
                                                   "lag_before_d": None, "lag_after_d": None})
        for t in sorted(set(ta) & set(tb)):
            if abs((ta[t]["lag_h"] or 0) - (tb[t]["lag_h"] or 0)) > 0.01:
                out["rel_type_or_lag_changed"].append({**row, "change": "lag", "before": txt({t: tb[t]}, oid),
                                                       "after": txt({t: ta[t]}, pid), "lag_before_d": lag_d(tb[t], oid),
                                                       "lag_after_d": lag_d(ta[t], pid)})
    code = lambda v: _db_task_code(con, v)  # noqa: E731
    for o in _sched_diff(_sched_options(con, pid)[0], _sched_options(con, oid)[0], code, code):
        if o["same"] is not True:
            out["schedule_options_changed"].append({"option": o["option"], "before": o["xer"], "after": o["db"]})
    for col in ("critical_path_type", "critical_drtn_hr_cnt"):
        if not _opt_eq(prj[pid][col], prj[oid][col]):
            out["schedule_options_changed"].append({"option": f"PROJECT.{col}", "before": prj[oid][col],
                                                    "after": prj[pid][col]})
    name = lambda cid: cals[cid].name if cid in cals else None  # noqa: E731
    for c in both:
        a, b = A[c], B[c]
        if a["op"] and name(a["clndr_id"]) != name(b["clndr_id"]):
            out["calendars_changed"].append({"kind": "activity", "item": c, "item_name": a["task_name"],
                                             "before": name(b["clndr_id"]), "after": name(a["clndr_id"]),
                                             "detail": _t("the activity is on another calendar")})
    used_a: dict[str, int] = {}
    used_b: dict[str, int] = {}
    for X, U, op_only in ((A, used_a, True), (B, used_b, False)):
        for r in X.values():
            if r["clndr_id"] in cals and (r["op"] or not op_only):
                U.setdefault(cals[r["clndr_id"]].name, r["clndr_id"])
    for nm in sorted(set(used_a) & set(used_b)):
        xa, xb = cals[used_a[nm]], cals[used_b[nm]]
        if xa.clndr_id == xb.clndr_id:
            continue  # the same calendar row: nothing can differ
        days = [WEEKDAYS[i] for i in range(7) if xa.week[i] != xb.week[i]]
        if days:
            out["calendars_changed"].append({"kind": "calendar", "item": nm, "item_name": None,
                                             "before": _t("{hours:g} h a week",
                                                          hours=sum(sum(f - s for s, f in d) for d in xb.week) / 60),
                                             "after": _t("{hours:g} h a week",
                                                         hours=sum(sum(f - s for s, f in d) for d in xa.week) / 60),
                                             "detail": _t("work week changed on {days}", days=", ".join(days))})
        added = sorted(d for d in xa.exc if xb.exc.get(d) != xa.exc[d])
        removed = sorted(d for d in xb.exc if d not in xa.exc)
        if added or removed:
            show = lambda ds: ", ".join(d.isoformat() for d in ds[:5]) + (" ..." if len(ds) > 5 else "")  # noqa: E731
            out["calendars_changed"].append({
                "kind": "calendar", "item": nm, "item_name": None,
                "before": _t("{n} exceptions", n=len(xb.exc)), "after": _t("{n} exceptions", n=len(xa.exc)),
                "detail": _join("; ", filter(None, (
                    added and _t("{n} added or changed ({dates})", n=len(added), dates=show(added)),
                    removed and _t("{n} removed ({dates})", n=len(removed), dates=show(removed)))))})
    for c in sorted(set(B) - set(A)):
        b = B[c]
        if not summ(b) and (b["status_code"] == "TK_Active" or b["crit"]):
            out["deleted_in_progress_or_critical"].append({
                "task_code": c, "task_name": b["task_name"], "status_before": b["status_code"],
                "actual_start_before": b["as_d"], "total_float_before_d": _days(b["tf_h"], b["hpd"]),
                "critical_before": bool(b["crit"])})
    dd_b, dd_a = (prj[p]["last_recalc_date"] for p in (oid, pid))
    dd = {"before": dd_b, "after": dd_a, "delta_cd": cd(_db_dt(dd_b), _db_dt(dd_a)),
          "advanced": bool(dd_a and dd_b and _db_dt(dd_a) > _db_dt(dd_b))}
    if not dd["advanced"]:
        out["data_date"].append(dict(dd))
    facts = {"data_date": dd, "must_finish": must,
             "critical_definition": {"project": cdef_a, "other": cdef_b}}
    return out, facts
