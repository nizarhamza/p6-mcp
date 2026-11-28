"""CPM engine tools: validation against the last F9, what-if, schedule risk."""

from __future__ import annotations

import bisect
import copy
import json
from datetime import timedelta
from time import monotonic
from typing import Mapping, Optional, get_args

from p6mcp.core.constants import ACT_LINKS_CAP, CSTR_FINISH, _cstr_label, SQL_PAGE_BYTES, SUMMARY_TYPES
from p6mcp.registry import (
    ActivityId, CompareTo, CutPct, Limit, NonNegDays, Offset, ProjectId, RiskIterations, RiskPcts, RiskRanges, _tool,
    WhatIfEdits,
)
from p6mcp.registry import RiskCorrelations, RiskEvents, RiskWorkers
from p6mcp.core.db import _check_page, _connect, _days, _db_dt, _fmt_dt, _num, _prefer, _project_id, _slice
from p6mcp.core.editlog import _schedule_state
from p6mcp.core.read import _critical_clause
from p6mcp.core.engine import _cpm_load, _cpm_run, CPM_TF_TOL_H, CPM_TOL, CPM_TRUST, ENGINE_VERSION, _net_load
from p6mcp.core.analysis import HC_UNSCORED
from p6mcp.core.explain import (
    _cpm_compare, _cpm_context, _dcma_brief, _dcma_memory, _finish_ties, _fragnet_ctx, _fragnet_impact,
    _project_context, _stale_f9, _stale_view, _whatif_apply, _whatif_assumptions, WHATIF_CAVEAT, WHATIF_CUT_PCT,
)
from p6mcp.core.risk import (
    _pctl, RISK_CAVEAT, RISK_DEFAULT, RISK_ITERATIONS, _risk_pcts, _risk_ranges, RISK_SOURCES, _risk_udfs, RISK_WORK,
)
from p6mcp.core.reporting import _period_edges
from p6mcp.core.risk import (
    RISK_CAVEAT_MODEL, _risk_correlations, _risk_events, _risk_iterate, _risk_workers, _RiskSim,
)


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
        cpm, tk = _cpm_load(con, pid)  # copies of the cached network while the database is unchanged
        ev = _stale_f9(con, pid, cpm)
        net = _net_load(con, pid, tk)
    finally:
        con.rollback()
    state = _schedule_state(con, pid)
    t0 = monotonic()
    res = _cpm_run(cpm, tk)
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
    Fragnet (time impact analysis): add_activity edits add new activities in memory only (a new Activity ID,
    name, original_days in days of calendar (default the project calendar), task_type TT_Task / TT_Mile /
    TT_FinMile, wbs code (default the project node), optional constraint); add_link then links them to each
    other or to existing activities (a link that closes a loop is refused with the loop). With them the result
    adds fragnet: activities (engine dates, total and free float, critical, links, driven_by, drives_finish),
    impact (project finish and milestones before / after, delay_cd and delay_wd in working days of the project
    calendar; milestones given, else every open milestone) and driving_path (the chain whose relationships set
    the dates, from the fragnet to the project finish). Fragnet activities count as new activities in the
    assumptions and DCMA blocks; the changed rows and float_change cover the existing activities.
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
    frag = any(isinstance(e, Mapping) and e.get("op") == "add_activity" for e in edits)
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")
    try:
        base, tk = _cpm_load(con, pid)
        thr_h, used = _critical_clause(con, pid, "project", None)[3:5]
        ctx = _fragnet_ctx(con, pid) if frag else None
    finally:
        con.rollback()
    state = _schedule_state(con, pid)
    d0: dict = {}
    r0 = _cpm_run(base, tk, d0)
    chk = _cpm_compare(base, r0, state["stale"])
    trial = base.copy()
    if ctx is not None:
        trial.code, trial.frag_ctx = dict(base.code), ctx  # fragnet activities: in the trial network only
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
    keys = list(dict.fromkeys(milestones or [c for c in [x["task_code"] for x in (fm0, fm) if x] + sorted(
        d["task_code"] for d in trial.t.values() if d["cstr_type"] and d["status_code"] != "TK_Complete")
        if c in base.code]))
    ms = []
    for code in keys:
        if code not in base.code:
            raise ValueError(f"milestones: activity {code} " + (
                "is a fragnet activity: its dates are in fragnet.activities" if code in trial.code
                else f"not found in project {project}"))
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
    if ctx is not None:
        out["fragnet"] = _fragnet_impact(base, trial, before, fin_before, r1, d1, crit_a, milestones,
                                         "P6's last F9 as stored" if compare_to == "current"
                                         else "the engine on the unchanged network")
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
                     correlations: Optional[RiskCorrelations] = None, risks: Optional[RiskEvents] = None,
                     use_p6_risks: bool = False, workers: Optional[RiskWorkers] = None,
                     offset: Offset = 0, limit: Limit = 50) -> dict:
    """Monte Carlo schedule risk in memory (the database is never written): each iteration draws the remaining
    duration of every open activity (not completed, not a milestone, LOE or WBS summary) from a triangular
    range, then runs the forward pass of this server's CPM engine (the one of p6_schedule_whatif: activity
    calendars, link types, lag calendar rule, constraints, data date, progress). Ranges in % of the remaining
    duration: default_range (default min 85, mode 100, max 135), ranges= by task_code, activity code value
    (code_type + code_value) or WBS (deepest node wins), and udf_optimistic / udf_most_likely /
    udf_pessimistic (number UDFs in days of the activity calendar, used for the activities that have all
    three); precedence UDF, task, code, WBS, default. Activities whose duration cannot move (expected finish in
    use, mandatory finish) are not drawn. correlations: groups (task_codes, wbs, or code_type + code_value)
    with a coefficient 0 to 1: the draws of a group share one normal factor per iteration (Gaussian copula,
    the rank correlation of two members is close to the coefficient; 1 = the same percentile of each range;
    an activity in two groups keeps the first). risks: discrete events {id, name, probability 0 to 1, impact
    {add_days (a number or min / likely / max, days of each activity's calendar, negative for an opportunity),
    task_codes, wbs or code_type + code_value}}: when one fires (independently, each iteration) its days are
    added to the remaining duration of its activities. use_p6_risks: list the project's P6 risks (P6 stores
    their probability and impact as scale levels): one is simulated when risks= quantifies its Risk ID, on
    its assigned activities when the impact has no selector. iterations (default 1000) are clamped to
    100..5000, then to at most 1,500,000 activity passes (iterations_requested and iterations_note say so);
    seed (default 1) makes the run repeatable: the same project, arguments and seed give the same output,
    whatever workers is. workers: processes sharing the iterations (default min(4, CPUs - 1), 1 = none),
    used for large runs only; parallel says how many ran. deterministic: the engine on unchanged durations
    (finish, finish_activity and its name, stored_finish = P6's last F9). finish: p10, p50, p80, p90, min, max
    of the project finish (latest early finish, LOE and WBS summary left out; nearest-rank percentiles).
    on_time_probability: deterministic_pct (finish at or before the deterministic one) and must_finish_pct (at
    or before the must finish by date; None without one). p80_buffer_wd: working days of the project
    calendar from the deterministic finish to p80. milestones (given, else the finish activity and every open
    constrained activity): deterministic, p10 to p90 of its finish, on_time_pct, constraint, constraint_date,
    by_constraint_pct (its constrained date, start or finish, at or before the constraint date),
    p80_buffer_wd (its calendar). histogram: weeks (P6 week start day) with count, pct, cum_pct. risk_events,
    ranked: fired / fired_pct, mean finish delay in working days (from the deterministic finish) when it fired
    and when not, finish_impact_wd (their difference), expected_delay_wd (impact x fired share) and share_pct
    (of the positive expected delays). Page of drivers by criticality: rows of open activities that sat on
    the critical path of at least one iteration (the walk back from the project finish along the links that
    set each start or finish), criticality_index (share of iterations, 0 to 1) and criticality_pct,
    on_deterministic_path, total_float_d and rem_dur_d (stored), min_d / mode_d / max_d drawn (days of its
    calendar) and range_source. engine: match rate of the unchanged network against the last F9 and a warning
    below 0.99 (see p6_cpm_validate); engine_version; distribution: what was drawn and the correlation groups;
    caveat: what this estimate leaves out. A logic loop is refused, as P6 does."""
    offset, limit = _check_page(offset, limit)
    try:
        n_req, seed = int(iterations), int(seed)
    except (TypeError, ValueError):
        raise ValueError("iterations and seed must be whole numbers") from None
    labels = [udf_optimistic, udf_most_likely, udf_pessimistic]
    if any(labels) and not all(labels):
        raise ValueError("udf_optimistic, udf_most_likely and udf_pessimistic go together: give all three or none")
    dflt = RISK_DEFAULT if default_range is None else _risk_pcts(default_range, "default_range")
    n_workers = _risk_workers(workers)
    con = _connect()
    pid = _project_id(con, project)
    con.execute("BEGIN")  # network, ranges, groups, risks and UDF values from one snapshot
    try:
        cpm, tk = _cpm_load(con, pid)
        assign, echo = _risk_ranges(con, pid, cpm, ranges)
        uvals, uinfo = _risk_udfs(con, pid, labels) if all(labels) else ({}, None)
        cache: dict = {}
        member, coef, cecho = _risk_correlations(con, pid, cpm, correlations, cache)
        events, p6_block = _risk_events(con, pid, cpm, risks, bool(use_p6_risks), cache)
        week_day = _prefer(con, "week_start_day_num") or 1
    finally:
        con.rollback()
    state = _schedule_state(con, pid)
    full = _cpm_run(cpm, tk)  # refuses a logic loop and a missing data date
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
        if n in member:
            cecho[member[n]]["applied"] += 1
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
    det_end = max(fixed_end, max((base[n]["ef"] for n in own_sub), default=fixed_end))
    fm0 = cpm.finish(full)[0]
    keys = list(dict.fromkeys(milestones or ([fm0["task_code"]] if fm0 else []) + sorted(
        d["task_code"] for d in T.values() if d["cstr_type"] and d["status_code"] != "TK_Complete")))
    for code in keys:
        if code not in cpm.code:
            raise ValueError(f"milestones: activity {code} not found in project {project}")
    mids = [cpm.code[c] for c in keys]
    sim = _RiskSim(cpm, plan, base, base_drv, dist, affected, sub, own_sub, fixed_end, fixed_top, counted, mids, seed,
                   member, coef, events)
    det_path = sim.path(base, base_drv, det_end)
    runs, par = _risk_iterate(sim, n_iter, n_workers)
    ends, crit, ms_vals, fired = runs["ends"], runs["crit"], runs["ms"], runs["fired"]
    pct = lambda k: round(100 * k / n_iter, 2)  # noqa: E731
    pcal = cpm.pcal if cpm.pcal is not None and not cpm.pcal.error else None
    ev_rows = []
    if events:
        slip = [None if pcal is None else pcal.work_days(det_end, e) for e in ends]
        mean = lambda xs: None if not xs or pcal is None else sum(xs) / len(xs)  # noqa: E731
        r2 = lambda x: None if x is None else round(x, 2) + 0.0  # noqa: E731
        for k, e in enumerate(events):
            hit = [(f >> k) & 1 for f in fired]
            nf = sum(hit)
            m_f = mean([s for s, h in zip(slip, hit, strict=True) if h])
            m_n = mean([s for s, h in zip(slip, hit, strict=True) if not h])
            impact = None if m_f is None or m_n is None else m_f - m_n
            applied = len(e["ids"] & dist.keys())
            lo, mo, hi = e["days"]
            ev_rows.append({"id": e["id"], "name": e["name"], "source": e["source"], "probability": e["probability"],
                            "add_days": {"min": lo, "likely": mo, "max": hi}, "selector": e["selector"],
                            "activities": applied, "activities_not_drawn": len(e["ids"]) - applied,
                            "fired": nf, "fired_pct": pct(nf), "mean_delay_fired_wd": r2(m_f),
                            "mean_delay_not_fired_wd": r2(m_n), "finish_impact_wd": r2(impact),
                            "expected_delay_wd": r2(None if impact is None else impact * nf / n_iter),
                            "_x": None if impact is None else impact * nf / n_iter})
        pos = sum(x["_x"] for x in ev_rows if x["_x"] is not None and x["_x"] > 0)
        ev_rows.sort(key=lambda x: (x["_x"] is None, -(x["_x"] or 0.0), x["id"]))
        for rank, x in enumerate(ev_rows, 1):
            v = x.pop("_x")
            x["share_pct"] = None if v is None or pos <= 0 else round(100 * max(v, 0.0) / pos, 2) + 0.0
            x["rank"] = rank
    ends.sort()
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
    for a, b in zip(edges, edges[1:], strict=False):
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
               distribution={"default": dict(zip(("min_pct", "mode_pct", "max_pct"), dflt, strict=False)), "ranges": echo,
                             "udfs": uinfo, "counts": {"drawn": len(dist), **{s: sum(1 for x in dist.values()
                                                                                      if x[3] == s) for s in RISK_SOURCES},
                                                       "duration_fixed": fixed, "recomputed_per_iteration": len(sub),
                                                       "correlated": sum(1 for c in sim.corr if c is not None)},
                             "correlations": cecho},
               deterministic={"finish": _fmt_dt(det_end), "finish_activity": fm0 and fm0["task_code"],
                              "finish_activity_name": fm0 and fm0["task_name"], "stored_finish": cpm.prj["scd_end_date"]},
               finish={**{f"p{p}": _fmt_dt(_pctl(ends, p)) for p in (10, 50, 80, 90)}, "min": _fmt_dt(ends[0]),
                       "max": _fmt_dt(ends[-1])},
               must_finish=cpm.prj["plan_end_date"],
               on_time_probability={"deterministic_pct": pct(sum(e <= det_end + CPM_TOL for e in ends)),
                                    "must_finish_pct": None if must is None else pct(sum(e <= must + CPM_TOL for e in ends))},
               p80_buffer_wd=None if pcal is None else round(pcal.work_days(det_end, p80), 2) + 0.0,
               milestones=ms_rows, histogram=hist, risk_events=ev_rows, p6_risks=p6_block,
               caveat=RISK_CAVEAT_MODEL if correlations or risks else RISK_CAVEAT, iterations_note=None,
               parallel={"workers_requested": n_workers, **par}, schedule_state=state)
    if n_iter < min(max(n_req, RISK_ITERATIONS[0]), RISK_ITERATIONS[1]):
        out["iterations_note"] = (f"{n_iter} iterations instead of {n_req}: {len(sub)} activities are recomputed per "
                                  f"iteration and a run is capped at {RISK_WORK:,} activity passes")
    elif n_iter != n_req:
        out["iterations_note"] = f"iterations clamped to {RISK_ITERATIONS[0]}..{RISK_ITERATIONS[1]}"
    return out
