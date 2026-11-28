"""Analysis tools: health check and its comparison, dangling ends, progress check, driving path,
float origin, redundant links."""

from __future__ import annotations

import functools
import os
import re
from datetime import datetime
from typing import Annotated, Any, Optional, get_args
from p6mcp.messages import _t

try:  # always installed with mcp
    from pydantic import Field
except ImportError:  # test stub without pydantic: annotations are never evaluated
    def Field(*_a, **_k):
        return None

from p6mcp.core import state as _state
from p6mcp.core.constants import REL_TYPES_REV, SUMMARY_TYPES
from p6mcp.registry import (
    ActivityId, BaselineDates, DanglingKind, DurationBasis, HcCheck, Limit, NonNegDays, Offset, ProjectId,
    RedundantCategory, SampleSize, Thresholds, _tool,
)
from p6mcp.core.db import _alive, _baseline_pid, _check_page, _cols, _connect, _days, _page, _project_id, _slice
from p6mcp.core.props import _lag_info, _sched_options
from p6mcp.core.editlog import _log_ts, _schedule_state
from p6mcp.core.engine import (
    ENGINE_VERSION, _float_walk, GAP_TOL_H, _net_load, _net_snapshot, REDUNDANT_CATEGORIES, _root_driver, _transitive,
)
from p6mcp.core.analysis import (
    _dangling_suggest, _dcma, _dcma_settings, HC_NO_ITEMS, _hc_pick, _hc_snapshot_save, _hc_snapshots, HC_TOTALS,
    HC_UNSCORED, _key_dates, OOS_SQL, _weighted_score,
)
from p6mcp.core.explain import _cpt


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
        raise ValueError(_t("baseline_dates must be one of {values}", values=list(get_args(BaselineDates))))
    if duration_basis not in get_args(DurationBasis):
        raise ValueError(_t("duration_basis must be one of {values}", values=list(get_args(DurationBasis))))
    if check is not None and check not in get_args(HcCheck):
        raise ValueError(_t("check must be one of {values}", values=list(get_args(HcCheck))))
    if check in HC_NO_ITEMS:
        raise ValueError(_t("{check} is a project-level value without an item list", check=check))
    offset, limit = _check_page(offset, limit)
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # settings and every check from one snapshot
    cfg = _dcma_settings(con, pid, dict(thresholds or {}), high_float_days)
    bid = _baseline_pid(con, pid, baseline)
    try:
        checks, n_open, n_links, ends, items = _dcma(con, pid, bid, cfg, baseline_dates, duration_basis)
        if check in (None, "key_dates"):  # a drill-down of another check does not need the network
            kd, kd_rows = _key_dates(_net_load(con, pid), None)
            checks["key_dates"] = kd
        if check is None and "critical_path_test" not in cfg["disabled"]:
            checks["critical_path_test"] = _cpt(con, pid)
        bl = con.execute("SELECT proj_short_name, " + ("last_recalc_date" if "last_recalc_date" in _cols(con, "PROJECT")
                         else "NULL") + " FROM PROJECT WHERE proj_id = ?", (bid,)).fetchone()
        if check == "key_dates":
            page = _slice(kd_rows, offset, limit)
        elif check:
            if check not in items:
                raise ValueError(_t("{check}: no item list here ({why})", check=check,
                                    why=checks[check].get("note") or _t("not evaluated")))
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
                "note": _t("Items of the last F9 in P6. Open = not completed, LOE and WBS summary left out.")}
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
                                "source": "baseline=" if baseline else _t("project baseline (PROJECT.sum_base_proj_id)")},
               schedule_state=state,
               note=_t("Values of the last F9 in P6: reschedule first. Open = not completed, LOE and WBS summary left "
                       "out."))
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
            res["snapshot"] = {"error": _t("snapshot not saved: {e}", e=e)}
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
            raise ValueError(_t("after='current': {error}",
                                error=snap.get("error", _t("the health check snapshot was not saved"))))
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
    warn = [_t("{param} differs: {before!r} -> {after!r}", param=k, before=a["params"].get(k), after=b["params"].get(k))
            for k in
            dict.fromkeys(list(a.get("params", {})) + list(b.get("params", {})))
            if a.get("params", {}).get(k) != b.get("params", {}).get(k)]
    if a.get("project") != b.get("project"):
        warn.append(_t("different projects: {before} -> {after}", before=a.get("project"), after=b.get("project")))
    if a.get("db") != b.get("db"):
        warn.append(_t("different databases: {before} -> {after}", before=a.get("db"), after=b.get("db")))
    if b.get("stale") or a.get("stale"):
        warn.append(_t("a snapshot was taken while the project was edited since its last F9"))
    ev = {"before": a.get("engine_version"), "after": b.get("engine_version")}
    if ev["before"] != ev["after"]:
        warn.append(_t("engine_version differs: {before} -> {after} (a snapshot saved before engine versions were "
                       "recorded): the critical path test may differ for that reason alone", **ev)
                    if None in ev.values() else
                    _t("engine_version differs: {before} -> {after}: the critical path test may differ for that reason "
                       "alone", **ev))
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
        raise ValueError(_t("kind must be start, finish or both"))
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
               note=_t("Suggestions are proposals: check them, then pass the items to p6_add_relationships (dry run "
                       "first)."))
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
        raise ValueError(_t("sample_size must be 0 to 50"))
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
    mode = (_t("progress override forward, retained logic backward (both flags Y)") if rl == po == "Y"
            else _t("retained logic") if rl == "Y" else _t("progress override") if po == "Y"
            else _t("actual dates") if rl == po == "N" else _t("retained logic (P6 default, no option stored)"))
    return {"project": project, "data_date": dd, "out_of_sequence": oos,
            "out_of_sequence_total": {"links": tot[0], "successors": tot[1]}, "status_checks": status,
            "logic_option": {"mode": mode, "sched_retained_logic": rl, "sched_progress_override": po, "source": src,
                             "note": _t("Retained logic keeps the remaining work of an out-of-sequence successor "
                                        "after its predecessors at F9; progress override ignores those links.")},
            "schedule_state": _schedule_state(con, pid),
            "note": _t("Statuses and dates as stored (last update and F9). Predecessors in other projects are shown "
                       "as PROJECT:ACTIVITY.")}


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
        raise ValueError(_t("max_depth must be 1 or more"))
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
        res["note"] = _t("Stopped at max_depth {max_depth}: earlier drivers not listed.", max_depth=max_depth)
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
        raise ValueError(_t("max_depth must be 1 or more"))
    con = _connect()
    pid = _project_id(con, project)
    net = _net_snapshot(con, pid)
    d0 = net.task(task_code)
    if d0["status_code"] == "TK_Complete" or d0["lf"] is None:
        raise ValueError(_t("{task_code} is completed or has no late dates: no float to explain", task_code=task_code))
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
        res["note"] = _t("Stopped at max_depth {max_depth}: the source may lie further.", max_depth=max_depth)
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
        raise ValueError(_t("categories: some of {REDUNDANT_CATEGORIES}", REDUNDANT_CATEGORIES=REDUNDANT_CATEGORIES))
    if "marked_deleted" in cats and not (name_pattern or "").strip():
        raise ValueError(_t("name_pattern must not be empty"))
    con = _connect()
    pid = _project_id(con, project)
    net = _net_snapshot(con, pid)
    own = [lk for lk in net.links if net.t[lk["succ"]]["proj_id"] == pid]
    hit: dict[int, dict[str, str]] = {}
    if "completed_predecessor" in cats:
        for lk in own:
            p, s = net.t[lk["pred"]], net.t[lk["succ"]]
            if p["status_code"] == "TK_Complete" and s["status_code"] != "TK_Complete":
                hit.setdefault(lk["id"], {})["completed_predecessor"] = _t("{code} finished {date}", code=p["task_code"],
                                                                           date=p["act_end_date"])
    if "marked_deleted" in cats:
        pat = name_pattern.lower()
        for lk in own:
            m = [net.t[lk[k]]["task_code"] for k in ("pred", "succ") if pat in (net.t[lk[k]]["task_name"] or "").lower()]
            if m:
                hit.setdefault(lk["id"], {})["marked_deleted"] = (
                    _t("name of {code} contains {pattern!r}", code=m[0], pattern=name_pattern) if len(m) == 1 else
                    _t("name of {pred} and {succ} contains {pattern!r}", pred=m[0], succ=m[1], pattern=name_pattern))
    if "transitive" in cats:
        for i, txt in _transitive(net).items():
            hit.setdefault(i, {})["transitive"] = txt
    if "duplicate" in cats:
        first: dict[tuple, int] = {}
        for lk in own:
            k = (lk["succ"], lk["pred"], lk["type"])
            if k in first:
                hit.setdefault(lk["id"], {})["duplicate"] = _t("same pair and type as task_pred_id {id}", id=first[k])
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
    side["note"] = _t("if every listed link (all pages) were removed; LOE and WBS summary links do not count")
    res = _slice(rows, offset, limit)
    res.update(counts={c: sum(c in r["categories"] for r in rows) for c in cats}, categories=cats,
               side_effects=side, schedule_state=_schedule_state(con, pid),
               note=_t("Candidates only: check drives and side_effects, then pass the task_pred_id values to "
                       "p6_delete_relationships(ids=...)."))
    return res
