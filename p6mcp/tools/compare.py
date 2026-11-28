"""Comparison tools: two projects, update integrity."""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Optional, get_args

from p6mcp.core.constants import REL_TYPES_REV
from p6mcp.registry import (
    BaselineDates, CompareSection, Explain, IntegrityFlag, Limit, NonNegDays, Offset, ProjectId, SampleSize, _tool,
)
from p6mcp.core.db import (
    _alive, _baseline_pid, _check_page, _cols, _connect, _days, _db_dt, _num, _page, _project_id, _slice,
)
from p6mcp.core.props import _lag_hpd, _lag_rule
from p6mcp.core.calendar import _calendars
from p6mcp.core.editlog import _schedule_state
from p6mcp.core.engine import CPM_TF_TOL_H
from p6mcp.core.analysis import (
    _cmp_acts, _cmp_links, _crit_named, _integrity, INTEGRITY_CAVEAT, INTEGRITY_DEFS, INTEGRITY_FLAGS, LINK_KEY, _slip,
    SLIP_CAUSES, _tf_change,
)
from p6mcp.messages import _t


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
        raise ValueError(_t("section must be one of {values}", values=list(get_args(CompareSection))))
    if dates not in get_args(BaselineDates):
        raise ValueError(_t("dates must be one of {values}", values=list(get_args(BaselineDates))))
    minv = _num(min_variance_days)
    if minv is None or minv < 0:
        raise ValueError(_t("min_variance_days must be 0 or more"))
    con = _connect()
    pid = _project_id(con, project)
    oid = _baseline_pid(con, pid, other)
    if oid is None:
        bls = []
        if "orig_proj_id" in _cols(con, "PROJECT"):
            bls = [r[0] for r in con.execute("SELECT proj_short_name FROM PROJECT WHERE orig_proj_id = ?"
                                             + _alive(con, "PROJECT") + " ORDER BY proj_short_name, proj_id", (pid,))]
        raise ValueError(_t("{project} has no project baseline: pass other= (a Project ID or baseline). Baselines of "
                            "{project}: {baselines}", project=project, baselines=bls or _t("none")))
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
    res = {"project": project, "other": pb[0], "other_source": "other=" if other else _t("project baseline"),
           "dates": dates, "section": section, **res,
           "summary": {"activities": {"matched": s[0], "added": s[1] or 0, "removed": removed, "later": s[2] or 0,
                                      "earlier": s[3] or 0, "same_finish": s[4] or 0, "later_avg_cd": r2(s[5]),
                                      "later_max_cd": r2(s[6]), "earlier_max_cd": r2(s[7]), "status_changed": s[8] or 0,
                                      "orig_dur_changed": s[9] or 0, "name_changed": s[10] or 0,
                                      "slip_causes": {k: causes.get(k, 0) for k in SLIP_CAUSES},
                                      "slip_cause_unknown": causes.get(None, 0), "float_eroded": fl[0] or 0,
                                      "became_critical": fl[1] or 0, "no_longer_critical": fl[2] or 0,
                                      **({"float_definition": _t(
                                          "over the matched activities open in the project (LOE and WBS summary left "
                                          "out): float_eroded = total float lower than in the other one by more than "
                                          "{minutes:g} minutes; critical = open and critical under each project's own "
                                          "definition (project: {project}; other: {other})",
                                          minutes=CPM_TF_TOL_H * 60, project=cdef_a, other=cdef_b)}
                                         if explain else {})},
                       "relationships": {"matched": lsum[0], "added": lsum[1], "removed": lsum[2], "lag_changed": lsum[3]}},
           "projects": {k: {"project": pa[k], "other": pb[k]} for k in pf},
           "basis": (_t("finish = actual, else early finish; other: same; variance = finish - other finish (positive "
                        "= later)") if dates == "current" else
                     _t("finish = actual, else early finish; other: planned finish; variance = finish - other finish "
                        "(positive = later)")),
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
        raise ValueError(_t("flag must be one of {INTEGRITY_FLAGS}", INTEGRITY_FLAGS=list(INTEGRITY_FLAGS)))
    near = _num(near_critical_days)
    if near is None or near < 0:
        raise ValueError(_t("near_critical_days must be 0 or more"))
    size = int(sample_size)
    if not 0 <= size <= 50:
        raise ValueError(_t("sample_size must be 0 to 50"))
    offset, limit = _check_page(offset, limit)
    con = _connect()
    pid, oid = _project_id(con, project), _project_id(con, other)
    if pid == oid:
        raise ValueError(_t("other must be another project: the previous update of this one"))
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
