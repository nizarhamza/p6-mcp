"""The network (_Net: driving and controlling links on the stored F9 dates, float origin, redundant links)
and the CPM engine (_Cpm: P6's F9 in memory, read only)."""

from __future__ import annotations

import functools
import heapq
from datetime import datetime, timedelta
from typing import Mapping, Optional, get_args

from p6mcp.core.constants import CSTR_FINISH, _cstr_label, HPD_SQL, REL_TYPES_REV
from p6mcp.registry import RedundantCategory
from p6mcp.core.db import _alive, _cols, _days, _db_dt, _dict, _fmt_dt
from p6mcp.core.props import _lag_rule, _sched_options
from p6mcp.core.calendar import _Cal, _calendars, _lag_cal_of, _lag_hpd_of


# ----------------------------------------------------------------- network analysis (driving, float origin, redundant links)
# A link's gap is the working time from the predecessor's date plus lag to the successor's date, by
# type: FS finish -> start, SS start -> start, FF finish -> finish, SF start -> finish. The lag is laid
# on the calendar of the project's lag rule. Early dates (driving): the gap is counted on the successor
# calendar; late dates (float origin): from the predecessor's late date to the bound the successor's late
# date less the lag lays, on the predecessor calendar (resource calendars too for a resource dependent
# predecessor; out-of-sequence options applied), as the CPM engine's backward pass. The dates are the stored
# F9 dates of every status: P6 also places completed activities (retained logic). The lag is the engine's:
# the part of a positive lag elapsed between the predecessor's actual start (SS, SF) or actual finish
# (FS, FF) and the data date is dropped (whatever sched_lag_early_start_flag says), a negative one from a
# progressed predecessor counts as 0; links to other projects are left out under SD_None. Oracle: every
# open activity of the reference project and of the parity cases is explained on both passes.

GAP_TOL_H = 5 / 60  # a gap within 5 minutes counts as 0 (driving, controlling)
LINK_FROM = {"PR_FS": "f", "PR_SS": "s", "PR_FF": "f", "PR_SF": "s"}  # predecessor date of each type
LINK_TO = {"PR_FS": "s", "PR_SS": "s", "PR_FF": "f", "PR_SF": "f"}  # successor date
LATE_CSTR = {"CS_MSO", "CS_MSOB", "CS_MANDSTART", "CS_MEO", "CS_MEOB", "CS_MANDFIN"}  # bound the late dates
EARLY_CSTR = {"CS_MSO", "CS_MSOA", "CS_MANDSTART", "CS_MEO", "CS_MEOA", "CS_MANDFIN"}  # bound the early dates


class _Net:
    """The links of one project (successor or predecessor in it) with their two activities, from one
    read snapshot: stored F9 dates, calendars, lag rule and data date."""

    def __init__(self, con, pid: int):
        pcols, tcols = _cols(con, "PROJECT"), _cols(con, "TASK")
        self.pid = pid
        self.prj = con.execute("SELECT proj_short_name, " + ", ".join(c if c in pcols else f"NULL AS {c}" for c in (
            "last_recalc_date", "plan_end_date", "scd_end_date", "clndr_id")) + " FROM PROJECT WHERE proj_id = ?",
            (pid,)).fetchone()
        self.dd = _db_dt(self.prj["last_recalc_date"])
        self.must = _db_dt(self.prj["plan_end_date"])
        self.end = _db_dt(self.prj["scd_end_date"])
        self.rule = _lag_rule(con, pid)[0]
        opts = _sched_options(con, pid)[0]
        rl, po = opts.get("sched_retained_logic", "Y") != "N", opts.get("sched_progress_override", "N") == "Y"
        self.oos = "both" if po and rl else "override" if po else "retained" if rl else "actual"
        self.cals = _calendars(con)
        self.pcal = self.cals.get(self.prj["clndr_id"])
        al = functools.partial(_alive, con)
        links = con.execute("SELECT r.task_pred_id, r.task_id, r.pred_task_id, r.pred_type, COALESCE(r.lag_hr_cnt, 0) AS lag, "
                            "s.proj_id, p.proj_id "
                            "FROM TASKPRED r JOIN TASK s ON s.task_id = r.task_id JOIN TASK p ON p.task_id = r.pred_task_id "
                            "WHERE (s.proj_id = ? OR p.proj_id = ?)" + al("TASKPRED", "r") + al("TASK", "s") + al("TASK", "p")
                            + " ORDER BY r.task_pred_id", (pid, pid)).fetchall()
        ignore_ext = opts.get("sched_outer_depend_type") == "SD_None"  # P6 ignores links to other projects
        self.links = [dict(id=r[0], succ=r[1], pred=r[2], type=r[3], lag=float(r[4])) for r in links
                      if not (ignore_ext and r[5] != r[6])]
        # calendars of the resources of the driving assignments of each open resource dependent activity
        self.rcals: dict[int, list] = {}
        acols, rcols = _cols(con, "TASKRSRC"), _cols(con, "RSRC")
        if "rollup_dates_flag" in acols and "clndr_id" in rcols:
            for tid, cid in con.execute(
                    "SELECT t.task_id, rs.clndr_id FROM TASKRSRC a JOIN TASK t ON t.task_id = a.task_id "
                    "JOIN RSRC rs ON rs.rsrc_id = a.rsrc_id WHERE t.proj_id = ? AND t.task_type = 'TT_Rsrc' "
                    "AND t.status_code <> 'TK_Complete' AND a.rollup_dates_flag = 'Y'" + al("TASKRSRC", "a")
                    + al("TASK", "t") + " ORDER BY a.taskrsrc_id", (pid,)):
                c = self.cals.get(cid)
                if c is not None and not c.error:
                    self.rcals.setdefault(tid, []).append(c)
        ids = {i for lk in self.links for i in (lk["succ"], lk["pred"])}
        sel = ", ".join(f"t.{c}" if c in tcols else f"NULL AS {c}" for c in (
            "task_id", "task_code", "task_name", "proj_id", "wbs_id", "task_type", "status_code", "clndr_id", "early_start_date",
            "early_end_date", "late_start_date", "late_end_date", "act_start_date", "act_end_date", "total_float_hr_cnt",
            "free_float_hr_cnt", "remain_drtn_hr_cnt", "cstr_type", "cstr_date", "cstr_type2", "cstr_date2"))
        q = (f"SELECT {sel}, {HPD_SQL} AS hpd, p.proj_short_name AS project, c.clndr_name AS calendar FROM TASK t "
             "JOIN PROJECT p ON p.proj_id = t.proj_id LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id WHERE ")
        rows = con.execute(q + "t.proj_id = ?" + al("TASK", "t"), (pid,)).fetchall()
        other = sorted(ids - {r["task_id"] for r in rows})
        for i in range(0, len(other), 500):
            chunk = other[i:i + 500]
            rows += con.execute(q + f"t.task_id IN ({','.join('?' * len(chunk))})" + al("TASK", "t"), chunk).fetchall()
        self.t: dict[int, dict] = {}
        for r in rows:
            d = _dict(r)
            for k, c in (("es", "early_start_date"), ("ef", "early_end_date"), ("ls", "late_start_date"),
                         ("lf", "late_end_date"), ("as_", "act_start_date"), ("af", "act_end_date"),
                         ("cd", "cstr_date"), ("cd2", "cstr_date2")):
                d[k] = _db_dt(d[c])
            d["cal"] = self.cals.get(d["clndr_id"])
            self.t[d["task_id"]] = d
        self.by_succ: dict[int, list] = {}
        self.by_pred: dict[int, list] = {}
        for lk in self.links:
            self.by_succ.setdefault(lk["succ"], []).append(lk)
            self.by_pred.setdefault(lk["pred"], []).append(lk)
        self.code = {d["task_code"]: i for i, d in self.t.items() if d["proj_id"] == pid}

    def task(self, code: str) -> dict:
        if code not in self.code:
            raise ValueError(f"Activity {code} not found in project {self.prj['proj_short_name']}")
        return self.t[self.code[code]]

    @staticmethod
    def summary(d: dict) -> bool:
        return d["task_type"] in ("TT_LOE", "TT_WBS")

    def lag_cal(self, p: dict, s: dict) -> Optional[_Cal]:
        """Calendar of the lag (None: 24-hour days)."""
        return _lag_cal_of(self.rule, p["cal"], s["cal"], self.pcal)

    def lag_hpd(self, p: dict, s: dict) -> float:
        return _lag_hpd_of(self.rule, p["hpd"], s["hpd"], self.pcal)

    def _lag(self, lk: dict, p: dict, lc) -> float:
        """The lag the schedule lays (the CPM engine's rule): a positive lag from a progressed predecessor loses
        the working time between its actual date and the data date; a negative one from it counts as 0."""
        lag, ref = lk["lag"], p["as_"] if LINK_FROM[lk["type"]] == "s" else p["af"]
        if lag == 0 or self.dd is None or ref is None or ref >= self.dd:
            return lag
        return 0.0 if lag < 0 else max(0.0, lag - _wm(lc, ref, self.dd) / 60)

    def gap(self, lk: dict, late: bool = False) -> Optional[float]:
        """Gap of a link in hours; None when a date or calendar is missing, or when the link bounds nothing.
        Early dates (driving): the predecessor's date plus lag to the successor's date, on the successor
        calendar. Late dates (controlling): bound_gap, as the backward pass lays the bound."""
        if late:
            return self.bound_gap(lk, "l")
        p, s = self.t.get(lk["pred"]), self.t.get(lk["succ"])
        if p is None or s is None:
            return None
        a, b = p["e" + LINK_FROM[lk["type"]]], s["e" + LINK_TO[lk["type"]]]
        cal, lc = s["cal"], self.lag_cal(p, s)
        if a is None or b is None or cal is None or cal.error or (lc is not None and lc.error):
            return None
        lc = lc or _ELAPSED
        return cal.work_hours(_shift_lag(lc, a, self._lag(lk, p, lc)), b)

    def bound_gap(self, lk: dict, side: str = "l") -> Optional[float]:
        """Hours from the predecessor's date (side 'l' late, 'e' early) to the bound its link lays from the
        successor's date: that date less the lag on the lag calendar, counted on the predecessor calendar (the
        MCP counted it forward, which leaves false gaps when the lag calendar is neither activity's). Under
        progress override a link into an in-progress activity bounds nothing; under actual dates an FS or SS
        link into one bounds from its actual start. A resource dependent predecessor's late finish is laid on
        its resources' calendars too: the gap nearest to zero counts."""
        p, s = self.t.get(lk["pred"]), self.t.get(lk["succ"])
        if p is None or s is None:
            return None
        prog = s["proj_id"] == self.pid and s["status_code"] == "TK_Active"
        if prog and self.oos == "override":
            return None
        fe, te = LINK_FROM[lk["type"]], LINK_TO[lk["type"]]
        to = s["as_"] if te == "s" and prog and self.oos == "actual" else s[side + te]
        frm, cal, lc = p[side + fe], p["cal"], self.lag_cal(p, s)
        if frm is None or to is None or cal is None or cal.error or (lc is not None and lc.error):
            return None
        lc = lc or _ELAPSED
        bound = _shift_w(lc, to, -_h2m(self._lag(lk, p, lc)))
        gaps = [cal.work_hours(frm, bound)]
        if side == "l" and fe == "f" and p["task_id"] in self.rcals:
            laid = cal.prev_work_end(bound)
            gaps += [rc.work_hours(frm, laid) for rc in self.rcals[p["task_id"]]]
        return min(gaps, key=abs)

    def zero(self, cal: Optional[_Cal], a: Optional[datetime], b: Optional[datetime]) -> bool:
        """a and b have no working time between them (5 minutes tolerance)."""
        if a is None or b is None:
            return False
        if cal is None or cal.error:
            return abs((b - a).total_seconds()) <= GAP_TOL_H * 3600
        return abs(cal.work_hours(a, b)) <= GAP_TOL_H

    def link_view(self, lk: dict, gap: Optional[float], other: str) -> dict:
        p, s = self.t[lk["pred"]], self.t[lk["succ"]]
        o = self.t[lk[other]]
        hpd = (s if other == "pred" else p)["hpd"] or 8.0
        key = "predecessor" if other == "pred" else "successor"
        return {key: o["task_code"], f"{key}_name": o["task_name"],
                **({"other_project": o["project"]} if o["proj_id"] != self.pid else {}),
                "task_pred_id": lk["id"], "type": REL_TYPES_REV.get(lk["type"], lk["type"]),
                "lag_d": _days(lk["lag"], self.lag_hpd(p, s)), "gap_h": None if gap is None else round(gap, 2),
                "gap_d": None if gap is None else _days(gap, hpd)}

    def act_view(self, d: dict, late: bool = False) -> dict:
        out = {"task_code": d["task_code"], "task_name": d["task_name"], "task_type": d["task_type"],
               "status_code": d["status_code"]}
        if d["proj_id"] != self.pid:
            out["project"] = d["project"]
        out.update({"early_start": d["early_start_date"], "early_finish": d["early_end_date"]} if not late else
                   {"late_start": d["late_start_date"], "late_finish": d["late_end_date"]})
        out.update(total_float_d=_days(d["total_float_hr_cnt"], d["hpd"]), calendar=d["calendar"])
        if d["cstr_type"]:
            out.update(cstr_type=d["cstr_type"], cstr_label=_cstr_label(d["cstr_type"]), cstr_date=d["cstr_date"])
        return out

    def cstr_hits(self, d: dict, kinds: set, late: bool) -> list[dict]:
        """Constraints of d (primary, secondary) of kinds whose date matches the bounded date of d."""
        out = []
        for ty, dt in ((d["cstr_type"], d["cd"]), (d["cstr_type2"], d["cd2"])):
            if ty in kinds and dt is not None:
                own = d[("l" if late else "e") + ("f" if ty in CSTR_FINISH else "s")]
                if self.zero(d["cal"], dt, own):
                    out.append({"cstr_type": ty, "cstr_label": _cstr_label(ty), "cstr_date": _fmt_dt(dt)})
        return out


def _net_snapshot(con, pid: int) -> _Net:
    own = not con.in_transaction
    if own:
        con.execute("BEGIN")
    try:
        return _Net(con, pid)
    finally:
        if own:
            con.rollback()


def _root_driver(net: _Net, d: dict, hidden: list) -> dict:
    """What starts the driving chain at d (no listed driving predecessor)."""
    base = {"task_code": d["task_code"], "task_name": d["task_name"], "date": d["early_start_date"]}
    cs = net.cstr_hits(d, EARLY_CSTR, late=False)
    if cs:
        return {**base, "type": "constraint", **cs[0]}
    if d["cstr_type"] == "CS_ALAP" and d["status_code"] == "TK_NotStart" and (
            net.zero(d["cal"], d["ef"], d["lf"]) or net.zero(d["cal"], d["ef"], net.must or net.end)
            or any(g is not None and abs(g) <= GAP_TOL_H
                   for g in (net.bound_gap(lk, "e") for lk in net.by_pred.get(d["task_id"], [])))):
        return {**base, "type": "as_late_as_possible", "cstr_type": "CS_ALAP", "cstr_label": _cstr_label("CS_ALAP"),
                "detail": "As Late As Possible: moved later by its free float, onto its successors or its late dates"}
    if net.dd and d["es"] and d["cal"] and not d["cal"].error and d["cal"].work_hours(net.dd, d["es"]) <= GAP_TOL_H:
        return {**base, "type": "data_date", "data_date": _fmt_dt(net.dd)}
    if hidden:
        return {**base, "type": "completed_predecessor", "predecessors": hidden[:10],
                "detail": "driven by completed activities (pass include_completed=True to follow them)"}
    if d["as_"] is not None and d["status_code"] != "TK_NotStart":
        return {**base, "type": "actual_date", "actual_start": d["act_start_date"],
                "detail": "in progress: remaining work placed by P6 from its progress"}
    if not net.by_succ.get(d["task_id"]):
        return {**base, "type": "open_start", "detail": "no predecessor"}
    gaps = [g for g in (net.gap(lk) for lk in net.by_succ[d["task_id"]]) if g is not None]
    return {**base, "type": "unexplained", "min_gap_h": round(min(gaps), 2) if gaps else None,
            "detail": "no predecessor link with a zero gap, no constraint or data date at its start (leveling, "
                      "expected finish or an option this server does not model)"}


def _late_origin(net: _Net, d: dict) -> list[dict]:
    """Origins of the late dates of d when no successor link controls them."""
    out = _cstr_sources(net, d)
    if out:
        return out
    who = {"task_code": d["task_code"], "task_name": d["task_name"]}
    if net.must and net.zero(d["cal"], d["lf"], net.must):
        return [{"type": "project_must_finish", **who, "date": net.prj["plan_end_date"],
                 "detail": "PROJECT.plan_end_date (Must Finish By)"}]
    if net.end and net.zero(d["cal"], d["lf"], net.end) or net.zero(d["cal"], d["lf"], d["ef"]):
        return [{"type": "open_end", **who, "date": d["late_end_date"],
                 "detail": "no controlling successor: late finish at the project finish (or its own early finish)"}]
    return [{"type": "unexplained", **who, "date": d["late_end_date"],
             "detail": "no successor link with a zero late gap, no constraint, must finish or project finish date"}]


def _cstr_sources(net: _Net, d: dict) -> list[dict]:
    """Late-date origins of d from its own constraints (date = the constraint date)."""
    return [{"type": "activity_constraint", "task_code": d["task_code"], "task_name": d["task_name"], **c,
             "date": c["cstr_date"]} for c in net.cstr_hits(d, LATE_CSTR, late=True)]


def _float_walk(net: _Net, d0: dict, max_depth: int = 500) -> dict:
    """Where the late dates of the open activity d0 come from (p6_float_origin, p6_milestones,
    p6_update_integrity): {level, via (task_id -> [(controlling link, gap)]), sources, cut, origin, own
    (its own constraints and whether they control), explanation}."""
    level, via, order, i, sources, cut = {d0["task_id"]: 0}, {}, [d0["task_id"]], 0, [], False
    while i < len(order):
        n = order[i]
        i += 1
        d = net.t[n]
        ctl = [(lk, g) for lk in net.by_pred.get(n, []) for g in (net.gap(lk, late=True),)
               if g is not None and abs(g) <= GAP_TOL_H]  # completed successors too: P6 places them (retained logic)
        own = _cstr_sources(net, d)
        sources += own
        if not ctl and not own:
            sources += _late_origin(net, d)
        for lk, g in ctl:
            via.setdefault(n, []).append((lk, g))
            if lk["succ"] not in level:
                if level[n] + 1 > max_depth:
                    cut = True
                    continue
                level[lk["succ"]] = level[n] + 1
                order.append(lk["succ"])
    kinds = sorted({s["type"] for s in sources})
    origin = {"type": kinds[0] if len(kinds) == 1 else "mixed" if kinds else "none", "task_code": None,
              "task_name": None, "date": None, "sources": sources}
    if len(sources) == 1:
        origin.update({k: v for k, v in sources[0].items() if k != "type"})
    code = d0["task_code"]
    own = [{"cstr_type": ty, "cstr_label": _cstr_label(ty), "cstr_date": dt,
            "controls": any(s["type"] == "activity_constraint" and s["task_code"] == code and s["cstr_type"] == ty
                            for s in sources)}
           for ty, dt in ((d0["cstr_type"], d0["cstr_date"]), (d0["cstr_type2"], d0["cstr_date2"])) if ty]
    path = [code]
    n = d0["task_id"]
    while via.get(n):
        lk, _ = sorted(via[n], key=lambda e: net.t[e[0]["succ"]]["task_code"])[0]
        nxt = net.t[lk["succ"]]
        path.append(f"{REL_TYPES_REV.get(lk['type'])} {_days(lk['lag'], net.lag_hpd(net.t[n], nxt)):g} d -> {nxt['task_code']}")
        n = lk["succ"]
        if len(path) > 12:
            path.append("...")
            break
    words = {"activity_constraint": "a constraint", "project_must_finish": "the project must finish date "
             + str(net.prj["plan_end_date"]), "open_end": "the project finish (open end)", "unexplained": "an unexplained source"}
    expl = (f"Total float {_days(d0['total_float_hr_cnt'], d0['hpd'])} d of {code} comes from "
            + (" and ".join(sorted({words[s['type']] + (f" ({s['cstr_label']} {s['cstr_date']} on {s['task_code']})"
                                                         if s['type'] == 'activity_constraint' else '') for s in sources}))
               or "nothing found") + ", through " + " ".join(path) + ".")
    for c in own:
        expl += (f" Its own constraint {c['cstr_label']} {c['cstr_date']} "
                 + ("controls its late dates." if c["controls"] else "does NOT control its late dates."))
    return {"level": level, "via": via, "sources": sources, "cut": cut, "origin": origin, "own": own,
            "explanation": expl}


REDUNDANT_CATEGORIES = list(get_args(RedundantCategory))
TRANSITIVE_DEPTH = 10  # links per FS chain explored for transitive links


def _transitive(net: _Net) -> dict[int, str]:
    """FS links A -> C of the project (open successor, no summary end) implied by an FS chain A -> ... -> C
    of 2 to TRANSITIVE_DEPTH links through non-summary activities whose lags add up to at least the
    direct lag (hours). Search pruned at activities starting after the latest direct successor."""
    out: dict[int, str] = {}
    fs = {}
    for lk in net.links:
        p, s = net.t.get(lk["pred"]), net.t.get(lk["succ"])
        if lk["type"] == "PR_FS" and p and s and not net.summary(p) and not net.summary(s):
            fs.setdefault(lk["pred"], []).append(lk)
    for a, direct in fs.items():
        tg = {}
        for lk in direct:
            s = net.t[lk["succ"]]
            if s["proj_id"] == net.pid and s["status_code"] != "TK_Complete":
                tg.setdefault(lk["succ"], []).append(lk)
        if not tg:
            continue
        horizon = max((net.t[c]["es"] for c in tg if net.t[c]["es"]), default=None)
        best = {a: (0.0, None)}  # node -> (max lag sum of a path from a, (prev, link))
        best2: dict[int, tuple] = {}  # node -> same over paths of 2 links or more
        frontier = {a}
        for _ in range(TRANSITIVE_DEPTH):
            nxt = set()
            for n in frontier:
                for lk in fs.get(n, []):
                    c = lk["succ"]
                    cs = net.t[c]
                    if c == a or (horizon and cs["es"] and cs["es"] > horizon):
                        continue
                    v = best[n][0] + lk["lag"]
                    if n != a and (c not in best2 or v > best2[c][0] + 1e-9):
                        best2[c] = (v, (n, lk))
                    if c not in best or v > best[c][0] + 1e-9:
                        best[c] = (v, (n, lk))
                        nxt.add(c)
            frontier = nxt
            if not frontier:
                break
        for c, lks in tg.items():
            if c not in best2:
                continue
            v, (n, last) = best2[c]
            chain, guard = [last], 0
            while n != a and guard < TRANSITIVE_DEPTH + 2:
                n, lk = best[n][1]
                chain.insert(0, lk)
                guard += 1
            codes = [net.t[a]["task_code"]] + [net.t[x["succ"]]["task_code"] for x in chain]
            for lk in lks:
                if v + 1e-9 >= lk["lag"]:  # the chain's lags reach the direct lag (no 5 minute slack)
                    out[lk["id"]] = f"implied by the FS chain {' -> '.join(codes)} (lags {v:g} h >= {lk['lag']:g} h)"
    return out


# ----------------------------------------------------------------- CPM engine (read-only what-if)
# Forward and backward pass of one project in memory, from the data as stored (nothing is written). Its rules are
# measured against P6 25.12 F9 results (ENGINE_VERSION 23)
# on 107 parity cases plus anonymized real projects (0 differences on every compared field; oracle test:
# test_p6_engine_parity.py). Durations and lags are whole minutes (hours x 60, rounded half to even).
#   * every status is placed (retained logic): a completed activity sits at the latest of the data date and
#     its predecessors' dates; a link between two completed activities carries no lag forward.
#   * a positive lag from a progressed predecessor loses the working time elapsed between its actual date and
#     the data date, whatever sched_lag_early_start_flag says; a negative one from it counts as 0.
#   * out-of-sequence modes from sched_retained_logic + sched_progress_override: progress override (and both
#     flags Y) ignore the predecessors of an in-progress activity forward; progress override alone also drops
#     those links backward; actual dates bound FS/SS links into an in-progress activity from its actual start.
#   * a not started activity starts no earlier than the project planned start, an in-progress one no earlier
#     than its resume date; link finish bounds are laid on the activity calendar (kept when work runs just
#     before them, else the next work start); late start bounds kept inside a period, else the previous end.
#   * open ends (and every late finish) are bounded by the must finish by date when set, else the project
#     early finish; sched_open_critical_flag makes an activity without successor finish late at its early
#     finish. Constraints as P6 25.12 lays them (forward: not started activities only; mandatory pairs).
#   * as late as possible moves later by its free float; LOE spans its ties (late dates mirrored from its
#     predecessors); WBS summary spans its node and every node below; resource dependent activities with
#     driving assignments are laid on their resources' calendars; assignment lag on task dependent SS/SF.
#   * total float per sched_float_type (FT_FF finish, FT_SS start, FT_Min the smaller); free float, longest
#     path, multiple float paths, remaining and planned dates as F9 writes them.
# Not modelled: resource leveling. Links to other projects use the other activity's stored dates.

CPM_TOL = timedelta(minutes=1)  # dates equal within a minute
CPM_TF_TOL_H = 0.05  # total float equal within 3 minutes
CPM_TRUST = 0.99  # match rate on open activities needed to trust what-if results and the critical path test
# Version of the engine's date, calendar and float semantics: bump it whenever a rule above changes what the
# engine computes (returned by p6_cpm_validate, p6_schedule_whatif, p6_schedule_risk, stored in health snapshots).
# 23: the rules measured on the P6 25.12 parity cases (tests/parity).
ENGINE_VERSION = 23
_NEG, _POS = datetime.min, datetime.max  # no bound (forward, backward)
_DT_EPOCH = datetime(1899, 12, 30)
_CS_START = {"CS_MSO", "CS_MSOA", "CS_MSOB", "CS_MANDSTART"}  # constraints of the start (a finish milestone ignores them)
_CS_FINISH = {"CS_MEO", "CS_MEOA", "CS_MEOB", "CS_MANDFIN"}  # of the finish (a start milestone ignores them)
_FLOAT_TYPES = {"FT_SS": "start", "FT_Min": "min", "FT_SF": "start", "FT_SM": "min"}  # else finish float


class _Elapsed:
    """Elapsed time with the _Cal arithmetic the engine uses: the 24-hour lag calendar, or an activity
    without a readable calendar."""
    error, name, hpd = None, "elapsed time", 24.0

    @staticmethod
    def add_work_hours(dt: datetime, h: float) -> datetime:
        return dt + timedelta(minutes=round(h * 60))

    @staticmethod
    def subtract_work_hours(dt: datetime, h: float) -> datetime:
        return dt - timedelta(minutes=round(h * 60))

    @staticmethod
    def work_hours(a: datetime, b: datetime) -> float:
        return (b - a).total_seconds() / 3600

    @staticmethod
    def next_work_start(dt: datetime) -> datetime:
        return dt

    @staticmethod
    def prev_work_end(dt: datetime) -> datetime:
        return dt

    @staticmethod
    def is_work_time(dt: datetime) -> bool:
        return True


_ELAPSED = _Elapsed()


def _h2m(h) -> int:
    """Hours -> whole minutes (half to even), as add_work_hours rounds them."""
    return int(round(float(h or 0) * 60))


def _wm(cal, a: datetime, b: datetime) -> int:
    """Working minutes from a to b on cal (negative when b is before a)."""
    return int(round(cal.work_hours(a, b) * 60))


def _shift_w(cal, dt: datetime, m: int) -> datetime:
    """dt moved by m working minutes (backward when negative)."""
    if not m:
        return dt
    return cal.add_work_hours(dt, m / 60) if m > 0 else cal.subtract_work_hours(dt, -m / 60)


def _shift_lag(cal, dt: datetime, lag_h: float) -> datetime:
    return _shift_w(cal, dt, _h2m(lag_h))


def _fbo(cal, b: datetime) -> datetime:
    """A finish bound laid on cal: kept when work runs just before it, else the next work start."""
    return b if cal.prev_work_end(b) == b else cal.next_work_start(b)


def _sbo(cal, b: datetime) -> datetime:
    """A late start bound laid on cal: kept inside a period, else the previous work end."""
    return b if cal.is_work_time(b) else cal.prev_work_end(b)


def _mx(*xs):
    v = [x for x in xs if x is not None]
    return max(v) if v else None


def _mn(*xs):
    v = [x for x in xs if x is not None]
    return min(v) if v else None


def _mins(dt: datetime) -> float:
    return (dt - _DT_EPOCH).total_seconds() / 60


class _Cpm:
    """Network of one project for the CPM engine: every activity, links inside the project (links to
    other projects kept with the other activity's stored dates), assignments, calendars and options."""

    def __init__(self, con, pid: int):
        tcols, pcols = _cols(con, "TASK"), _cols(con, "PROJECT")
        self.pid = pid
        self.prj = con.execute("SELECT proj_short_name, " + ", ".join(c if c in pcols else f"NULL AS {c}" for c in (
            "last_recalc_date", "plan_end_date", "plan_start_date", "scd_end_date", "clndr_id", "critical_drtn_hr_cnt",
            "critical_path_type")) + " FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone()
        self.dd = _db_dt(self.prj["last_recalc_date"])
        self.must = _db_dt(self.prj["plan_end_date"])
        self.pstart = _db_dt(self.prj["plan_start_date"])
        self.opts = _sched_options(con, pid)[0]
        self.rule = _lag_rule(con, pid)[0]
        self._derive()
        self.cals = _calendars(con)
        self.pcal = self.cals.get(self.prj["clndr_id"])
        cols = ("task_id", "task_code", "task_name", "proj_id", "wbs_id", "task_type", "status_code", "clndr_id",
                "early_start_date", "early_end_date", "late_start_date", "late_end_date", "act_start_date", "act_end_date",
                "total_float_hr_cnt", "remain_drtn_hr_cnt", "cstr_type", "cstr_date", "cstr_type2", "cstr_date2",
                "expect_end_date", "suspend_date", "resume_date", "free_float_hr_cnt", "driving_path_flag",
                "float_path", "float_path_order", "target_drtn_hr_cnt", "update_date")
        dts = ("early_start_date", "early_end_date", "late_start_date", "late_end_date", "act_start_date",
               "act_end_date", "cstr_date", "cstr_date2", "expect_end_date", "suspend_date", "resume_date", "update_date")
        sel = ", ".join(f"t.{c}" if c in tcols else f"NULL AS {c}" for c in cols)
        al = functools.partial(_alive, con)
        self.t: dict[int, dict] = {}
        for r in con.execute(f"SELECT {sel}, {HPD_SQL} AS hpd FROM TASK t LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id "
                             "WHERE t.proj_id = ?" + al("TASK", "t") + " ORDER BY t.task_id", (pid,)):
            d = _dict(r)
            # stored dates that do not read as a date (p6_cpm_validate reports them as data issues)
            d["bad_dates"] = [k for k in dts if d[k] not in (None, "") and _db_dt(d[k]) is None]
            for k in dts:
                d[k] = _db_dt(d[k])
            d["cal"] = self.cals.get(d["clndr_id"])
            d["rem"] = float(d["remain_drtn_hr_cnt"] or 0)
            self.t[d["task_id"]] = d
        self.code = {d["task_code"]: i for i, d in self.t.items()}
        # WBS: parent of each node of the project; the project node (and anything outside) is None
        self.wbs_parent: dict = {}
        wcols = _cols(con, "PROJWBS")
        if {"wbs_id", "parent_wbs_id"} <= wcols:
            nodes = {r[0]: (r[1], r[2]) for r in con.execute(
                "SELECT wbs_id, parent_wbs_id, " + ("proj_node_flag" if "proj_node_flag" in wcols else "'N'") +
                " FROM PROJWBS w WHERE proj_id = ?" + al("PROJWBS", "w"), (pid,))}
            inner = {w for w, (_, flag) in nodes.items() if flag != "Y"}
            self.wbs_parent = {w: (nodes[w][0] if nodes[w][0] in inner else None) for w in inner}
        for d in self.t.values():
            d["wbs"] = d["wbs_id"] if d["wbs_id"] in self.wbs_parent else None
        self.links: list[dict] = []
        self.ext_pred: list[dict] = []  # links from other projects: the other activity's stored early dates
        self.ext_succ: list[dict] = []  # links to other projects: the other activity's stored late and early dates
        for r in con.execute(
                "SELECT r.task_pred_id, r.task_id, r.pred_task_id, r.pred_type, COALESCE(r.lag_hr_cnt, 0), s.proj_id, p.proj_id, "
                "p.early_start_date, p.early_end_date, s.late_start_date, s.late_end_date, "
                "COALESCE(NULLIF(cp.day_hr_cnt, 0), 8), p.clndr_id, s.clndr_id, s.early_start_date, s.early_end_date "
                "FROM TASKPRED r JOIN TASK s ON s.task_id = r.task_id "
                "JOIN TASK p ON p.task_id = r.pred_task_id LEFT JOIN CALENDAR cp ON cp.clndr_id = p.clndr_id "
                "WHERE (s.proj_id = ? OR p.proj_id = ?)" + al("TASKPRED", "r") + al("TASK", "s") + al("TASK", "p") +
                " ORDER BY r.task_pred_id", (pid, pid)):
            lk = {"id": r[0], "succ": r[1], "pred": r[2], "type": r[3], "lag": float(r[4])}
            if r[5] == pid and r[6] == pid:
                self.links.append(lk)
            elif self.ignore_ext:
                continue  # sched_outer_depend_type SD_None: relationships to other projects are ignored
            elif r[5] == pid:
                self.ext_pred.append({**lk, "es": _db_dt(r[7]), "ef": _db_dt(r[8]), "cal": self.cals.get(r[12])})
            else:
                self.ext_succ.append({**lk, "ls": _db_dt(r[9]), "lf": _db_dt(r[10]), "es": _db_dt(r[14]),
                                      "ef": _db_dt(r[15]), "cal": self.cals.get(r[13])})
        self.asg: list[dict] = []  # assignments: resource dependent dates, assignment lag
        acols, rcols = _cols(con, "TASKRSRC"), _cols(con, "RSRC")
        if acols:
            want = ("taskrsrc_id", "task_id", "rsrc_id", "rollup_dates_flag", "remain_qty", "remain_qty_per_hr",
                    "relag_drtn_hr_cnt", "target_lag_drtn_hr_cnt", "restart_date", "reend_date",
                    "target_start_date", "target_end_date")
            sel = ", ".join(f"a.{c}" if c in acols else f"NULL AS {c}" for c in want)
            rj = ("LEFT JOIN RSRC rs ON rs.rsrc_id = a.rsrc_id", "rs.clndr_id") if "clndr_id" in rcols \
                else ("", "NULL")
            for r in con.execute(f"SELECT {sel}, {rj[1]} AS rsrc_clndr_id FROM TASKRSRC a JOIN TASK t ON t.task_id = a.task_id "
                                 f"{rj[0]} WHERE t.proj_id = ?" + al("TASKRSRC", "a") + al("TASK", "t") +
                                 " ORDER BY a.taskrsrc_id", (pid,)):
                a = _dict(r)
                for k in ("restart_date", "reend_date", "target_start_date", "target_end_date"):
                    a[k] = _db_dt(a[k])
                self.asg.append(a)

    def _derive(self) -> None:
        """Options the passes read, from self.opts (PROJPROP 'scheduling', P6 defaults when absent)."""
        o = self.opts
        rl, po = o.get("sched_retained_logic", "Y") != "N", o.get("sched_progress_override", "N") == "Y"
        self.oos = "both" if po and rl else "override" if po else "retained" if rl else "actual"
        self.ft = _FLOAT_TYPES.get(o.get("sched_float_type"), "finish")
        self.use_expect = o.get("sched_use_expect_end_flag", "Y") != "N"
        self.open_crit = o.get("sched_open_critical_flag") == "Y"
        self.ignore_ext = o.get("sched_outer_depend_type") == "SD_None"
        self.fp = None
        if o.get("enable_multiple_longest_path_calc") == "Y":
            try:
                mx, end = int(float(o.get("max_multiple_longest_path") or 10)), int(float(
                    o.get("key_activity_for_multiple_longest_paths") or -99))
            except ValueError:
                mx, end = 10, -99
            self.fp = {"max": mx, "end": end, "total": o.get("use_total_float_multiple_longest_paths", "Y") != "N",
                       "limit": o.get("limit_multiple_longest_path_calc", "Y") != "N"}

    def copy(self) -> "_Cpm":
        other = object.__new__(_Cpm)
        other.__dict__.update(self.__dict__)
        other.t = {i: dict(d) for i, d in self.t.items()}
        other.links = [dict(lk) for lk in self.links]
        return other

    # ------------------------------------------------------------------ helpers
    def lag_cal(self, p_cal, s_cal) -> Optional[_Cal]:
        return _lag_cal_of(self.rule, p_cal, s_cal, self.pcal)

    def lag_hpd(self, p: dict, s: dict) -> float:
        return _lag_hpd_of(self.rule, p["hpd"], s["hpd"], self.pcal)

    def _lc(self, p_cal, s_cal):
        """Lag calendar as an arithmetic object (elapsed time for 24 hours or a missing calendar)."""
        c = _lag_cal_of(self.rule, p_cal, s_cal, self.pcal)
        return c if c is not None and not c.error else _ELAPSED

    @staticmethod
    def _time(d: dict):
        c = d["cal"]
        return c if c is not None and not c.error else _ELAPSED

    @staticmethod
    def _has_cal(d: dict) -> bool:
        return d["cal"] is not None and not d["cal"].error

    @staticmethod
    def shift(cal: Optional[_Cal], dt: datetime, h: float) -> datetime:
        return _shift_lag(cal if cal is not None and not cal.error else _ELAPSED, dt, h)

    def eff_lag(self, lk: dict, p: dict, lc) -> float:
        """The lag the passes lay: a positive lag from a progressed predecessor loses the working time on the
        lag calendar between its actual date and the data date (never below 0), whatever
        sched_lag_early_start_flag says; a negative lag from it counts as 0 (P6 25.12)."""
        lag = lk["lag"]
        ref = p.get("act_start_date") if LINK_FROM[lk["type"]] == "s" else p.get("act_end_date")
        if lag == 0 or self.dd is None or ref is None or ref >= self.dd:
            return lag
        if lag < 0:
            return 0.0
        lc = lc if lc is not None and not getattr(lc, "error", None) else _ELAPSED
        return max(0.0, lag - _wm(lc, ref, self.dd) / 60)

    def duration_fixed(self, d: dict) -> Optional[str]:
        """Why a change of d's remaining duration cannot move its finish (None when it can)."""
        if d["status_code"] == "TK_Complete":
            return "completed"
        if d["task_type"] in ("TT_Mile", "TT_FinMile"):
            return "milestone"
        if self.use_expect and d.get("expect_end_date"):
            return "expected finish date used by the schedule options"
        if "CS_MANDFIN" in (d["cstr_type"], d["cstr_type2"]):
            return "mandatory finish constraint"
        return None

    def _from_expect(self, d: dict) -> bool:
        """The remaining duration comes from the expected finish (F9 rewrites it)."""
        return (self.use_expect and d["status_code"] != "TK_Complete" and d["task_type"] not in ("TT_Mile", "TT_FinMile")
                and d.get("expect_end_date") is not None and self._has_cal(d))

    def _rem_m(self, d: dict, es: Optional[datetime], h: Optional[float] = None) -> int:
        """Remaining working minutes the passes use (h: hours replacing the stored ones, Monte Carlo)."""
        if d["status_code"] == "TK_Complete" or d["task_type"] in ("TT_Mile", "TT_FinMile"):
            return 0
        if self._from_expect(d) and es is not None:
            return max(0, _wm(self._time(d), es, d["expect_end_date"]))
        return _h2m(d["rem"] if h is None else h)

    def rem(self, d: dict, es: Optional[datetime] = None, h: Optional[float] = None) -> float:
        """Remaining working hours the passes use."""
        return self._rem_m(d, es, h) / 60

    def _ignores(self, d: dict) -> bool:
        """An in-progress activity ignores its predecessors forward (progress override, both flags Y)."""
        return d["status_code"] == "TK_Active" and self.oos in ("override", "both")

    def _dated(self, d: dict) -> list:
        ty = d["task_type"]
        skip = _CS_START if ty == "TT_FinMile" else _CS_FINISH if ty == "TT_Mile" else ()
        return [(c, dt) for c, dt in ((d["cstr_type"], d["cstr_date"]), (d["cstr_type2"], d["cstr_date2"]))
                if c and dt is not None and c not in skip]

    def order(self) -> tuple[list[int], dict, dict]:
        """Activities in link order (Kahn's algorithm, the ready activity first in task_id order)."""
        by_s, by_p = {}, {}
        for lk in self.links:
            if lk["succ"] in self.t and lk["pred"] in self.t:
                by_s.setdefault(lk["succ"], []).append(lk)
                by_p.setdefault(lk["pred"], []).append(lk)
        indeg = {i: len(by_s.get(i, [])) for i in self.t}
        ready = [i for i in self.t if not indeg[i]]
        heapq.heapify(ready)
        out = []
        while ready:
            n = heapq.heappop(ready)
            out.append(n)
            for lk in by_p.get(n, []):
                indeg[lk["succ"]] -= 1
                if not indeg[lk["succ"]]:
                    heapq.heappush(ready, lk["succ"])
        if len(out) < len(self.t):
            loop = sorted(self.t[i]["task_code"] for i in self.t if i not in set(out))[:10]
            raise ValueError(f"The network has a loop (P6 refuses to schedule it): {loop}")
        return out, by_s, by_p

    # ------------------------------------------------------------------ passes
    def run(self, drivers: Optional[dict] = None, fields: bool = True) -> dict[int, dict]:
        """{task_id: {es, ef, ls, lf, tf_h, ff_h, rs, rf, rls, rlf, ps, pf, longest, critical, float_path,
        float_path_order[, rem_h]}} of every activity of the project (rem_h: the remaining duration F9
        rewrites, only where it does). drivers: filled by the forward pass (ties() reads it). fields=False:
        the same forward and backward passes, but only the dates and total float (es, ef, ls, lf, tf_h) are
        returned (no free float, paths or flags: the DCMA critical path test reads no more). The plan is kept
        for _plan_cache (ties(), _cpm_classify) while self.links is the same list."""
        plan = self.plan()
        self._plan = (self.links, plan)
        res = self.forward(plan, drivers=drivers)
        self.backward(plan, res)
        if fields:
            self._fields(plan, res)
        else:
            for r in res.values():
                if r.get("ss") is not None:  # as _fields: the first assignment's dates are the stored ones
                    r["es"], r["ef"] = r["ss"], r["sf"]
        return res

    def plan(self) -> dict:
        """What every pass over this network shares: activity order, links of the project by successor and by
        predecessor (lk, lag calendar, effective lag, link index), links to other projects by own activity,
        driving assignments of resource dependent activities and the assignment lag of task dependent ones."""
        if self.dd is None:
            raise ValueError("The project has no data date (PROJECT.last_recalc_date): schedule it once in P6")
        order, by_s, _ = self.order()
        T = self.t
        by_s2, by_p2, links = {}, {}, []
        idx = {id(lk): i for i, lk in enumerate(self.links)}
        for n in order:
            for lk in by_s.get(n, ()):
                p = T[lk["pred"]]
                lc = self._lc(p["cal"], T[n]["cal"])
                eff = self.eff_lag(lk, p, lc)
                # (link, lag calendar, effective lag h, link index, effective lag minutes, both ends completed,
                #  from the predecessor's finish, to the successor's start)
                e = (lk, lc, eff, idx[id(lk)], _h2m(eff),
                     p["status_code"] == "TK_Complete" and T[n]["status_code"] == "TK_Complete",
                     LINK_FROM[lk["type"]] == "f", LINK_TO[lk["type"]] == "s")
                by_s2.setdefault(n, []).append(e)
                links.append(e)
        for e in sorted(links, key=lambda e: e[3]):
            by_p2.setdefault(e[0]["pred"], []).append(e)
        for n in by_s2:
            by_s2[n].sort(key=lambda e: e[3])
        ext_p, ext_s = {}, {}  # links to other projects by own activity
        for lk in self.ext_pred:
            if lk["succ"] in T:
                ext_p.setdefault(lk["succ"], []).append(
                    {**lk, "lc": self._lc(lk["cal"], T[lk["succ"]]["cal"]), "applied": lk["lag"]})
        for lk in self.ext_succ:
            if lk["pred"] in T:
                own = T[lk["pred"]]
                lc = self._lc(own["cal"], lk["cal"])
                ext_s.setdefault(lk["pred"], []).append({**lk, "lc": lc, "applied": self.eff_lag(lk, own, lc)})
        drv, alag = {}, {}
        for a in self.asg:
            d = T.get(a["task_id"])
            if d is None:
                continue
            rd = d["task_type"] == "TT_Rsrc"
            rc = self.cals.get(a["rsrc_clndr_id"])
            cal = rc if rd and rc is not None and not rc.error else self._time(d)
            rate = float(a["remain_qty_per_hr"] or 0)
            work = None
            if rate > 0:
                work = _h2m(float(a["remain_qty"] or 0) / rate)
            elif a["restart_date"] is not None and a["reend_date"] is not None:
                work = _wm(cal, a["restart_date"], a["reend_date"])
            elif d["status_code"] == "TK_NotStart" and a["target_start_date"] is not None \
                    and a["target_end_date"] is not None:
                work = _wm(cal, a["target_start_date"], a["target_end_date"])
            elif d["status_code"] == "TK_Active":
                work = _h2m(d["rem"])
            work = work or 0
            if rd and a["rollup_dates_flag"] == "Y" and d["status_code"] != "TK_Complete":
                drv.setdefault(d["task_id"], []).append((cal, work, _h2m(a["relag_drtn_hr_cnt"]) if rd else 0))
            lag = _h2m(a["relag_drtn_hr_cnt"]) if float(a["target_lag_drtn_hr_cnt"] or 0) == 0 else 0
            alag[d["task_id"]] = min(alag[d["task_id"]], lag) if d["task_id"] in alag else lag
        return {"order": order, "by_s": by_s2, "by_p": by_p2, "links": sorted(links, key=lambda e: e[3]),
                "ext_p": ext_p, "ext_s": ext_s, "drv": drv, "alag": alag}

    # ------------------------------------------------------------------ driving assignments (resource dependent)
    @staticmethod
    def _a_start(cal, start: datetime, lag: int) -> datetime:
        f = cal.next_work_start(start)
        return cal.next_work_start(_shift_w(cal, f, lag)) if lag > 0 else f

    def _a_finish(self, drv: list, start: datetime) -> datetime:
        return max(_shift_w(c, self._a_start(c, start, lag), w) for c, w, lag in drv)

    def _a_first(self, drv: list, start: datetime) -> datetime:
        return min(self._a_start(c, start, lag) for c, w, lag in drv)

    @staticmethod
    def _a_back(drv: list, finish: datetime) -> datetime:
        return min(_shift_w(c, _shift_w(c, finish, -w), -lag) for c, w, lag in drv)

    @staticmethod
    def _a_work(drv: list) -> int:
        return max([0] + [w for _, w, _ in drv])

    @staticmethod
    def _a_late(drv: list, lf: datetime) -> tuple[datetime, datetime]:
        start, finish = _POS, _NEG
        for c, w, lag in drv:
            f = c.prev_work_end(lf)
            s = _shift_w(c, f, -w)
            if lag > 0:
                s = c.subtract_work_hours(s, lag / 60)
            start, finish = min(start, s), max(finish, f)
        return start, finish

    def _lay_driven(self, drv: list, time, frm: datetime, bound: Optional[datetime] = None) -> tuple:
        """(start, finish, stored start, stored finish) of a resource dependent activity laid from frm."""
        def to_bound(dt):
            return bound if bound is not None and dt < bound and _wm(time, dt, bound) == 0 else dt
        first = self._a_first(drv, frm)
        rstart = self._a_first(drv, time.next_work_start(first))
        if rstart == first or not time.is_work_time(rstart):
            return (first if rstart == first else frm), to_bound(self._a_finish(drv, frm)), None, None
        return rstart, to_bound(self._a_finish(drv, rstart)), first, to_bound(self._a_finish(drv, frm))

    # ------------------------------------------------------------------ forward pass
    def _fwd_cstr(self, d: dict, time, from_exp: bool) -> tuple:
        """(mandatory, start bound, finish bound) of the constraints on the forward pass."""
        if d["status_code"] in ("TK_Complete", "TK_Active"):
            return False, None, None
        s = f = None
        mand = False
        for c, dt in self._dated(d):
            if c in ("CS_MSO", "CS_MSOA", "CS_MANDSTART"):
                s = _mx(s, dt)
            elif c == "CS_MANDFIN" or (c in ("CS_MEO", "CS_MEOA") and not from_exp):
                f = _mx(f, dt if c == "CS_MANDFIN" and d["task_type"] == "TT_FinMile" else time.prev_work_end(dt))
            mand = mand or c in ("CS_MANDSTART", "CS_MANDFIN")
        return mand, s, f

    def _lbound(self, e: tuple, res: dict, ks: str, kf: str) -> Optional[datetime]:
        """The bound a link lays on its successor from the predecessor's dates res[p][ks / kf]; no lag between
        two completed activities."""
        r = res[e[0]["pred"]]
        a = r[kf] if e[6] else r[ks]
        if a is None or e[5]:
            return a
        return _shift_w(e[1], a, e[4])

    def forward(self, plan: dict, rem: Optional[Mapping[int, float]] = None, drivers: Optional[dict] = None,
                fixed: Optional[tuple] = None) -> dict[int, dict]:
        """Early dates {task_id: {es, ef, xs, xf, ss, sf}} (xs / xf: the dates successors link from, ss / sf:
        early dates P6 stores apart from the remaining ones, else None). rem: remaining hours by task_id
        replacing the stored ones (Monte Carlo). drivers: filled with each activity's entry (ties() gives the
        predecessors whose link sets its dates). fixed: (results kept as they are, the activities to compute,
        in network order)."""
        T, dd, ps = self.t, self.dd, self.pstart
        res: dict[int, dict] = dict(fixed[0]) if fixed else {}
        for n in (fixed[1] if fixed else plan["order"]):
            d = T[n]
            st, ty = d["status_code"], d["task_type"]
            sb = lfb = fb = None
            if not self._ignores(d):
                for e in plan["by_s"].get(n, ()):
                    r = res[e[0]["pred"]]
                    b = r["xf"] if e[6] else r["xs"]
                    if not e[5]:
                        b = _shift_w(e[1], b, e[4])
                    if e[7]:
                        if sb is None or b > sb:
                            sb = b
                    elif lfb is None or b > lfb:
                        lfb = b
                for e in plan["ext_p"].get(n, ()):
                    a = e["ef"] if LINK_FROM[e["type"]] == "f" else e["es"]
                    if a is None:
                        continue
                    b = _shift_lag(e["lc"], a, e["applied"])
                    if LINK_TO[e["type"]] == "s":
                        sb = _mx(sb, b)
                    else:
                        lfb = _mx(lfb, b)
            time = self._time(d)
            cmand, cs, cf = self._fwd_cstr(d, time, self._from_expect(d))
            if not cmand:
                sb, fb = _mx(sb, cs), _mx(fb, cf)
            earliest = ps if st == "TK_NotStart" and ps is not None and ps > dd else \
                max(dd, d["resume_date"]) if st == "TK_Active" and d["resume_date"] is not None else dd
            if st == "TK_Complete" or ty == "TT_FinMile":
                if st == "TK_Complete":
                    x, how = _mx(earliest, sb, lfb, fb), "placed"
                else:
                    x = _fbo(time, cf if cmand and cf is not None else _mx(earliest, sb, lfb, fb))
                    how = "mandatory" if cmand else "placed"
                res[n] = {"es": x, "ef": x, "xs": x, "xf": x, "ss": None, "sf": None}
                if drivers is not None:
                    drivers[n] = (n, res, how)
                continue
            start = time.next_work_start(_mx(earliest, sb))
            if ty == "TT_LOE":
                tie = _mx(_fbo(time, lfb) if lfb is not None else None, fb)
                fin = _mx(start, tie)
                res[n] = {"es": start, "ef": fin, "xs": start, "xf": fin, "ss": None, "sf": None}
                if drivers is not None:
                    drivers[n] = (n, res, "finish" if tie is not None and tie > start else "start")
                continue
            driven = plan["drv"].get(n)
            laid = None
            if driven:
                laid = self._lay_driven(driven, time, start)
                start, finish = laid[0], laid[1]
                remaining = _wm(time, start, finish)
            else:
                remaining = self._rem_m(d, start, None if rem is None else rem.get(n))
                finish = _shift_w(time, start, remaining)
            c_fin = fb
            lbo = _fbo(time, lfb) if lfb is not None else None
            fb = _mx(fb, lbo)
            by_fin = fb is not None and fb > finish
            if by_fin and driven and not cmand:
                if lbo is not None and (c_fin is None or lbo >= c_fin):
                    laid = self._lay_driven(driven, time, time.next_work_start(self._a_back(driven, lbo)), lbo)
                else:
                    laid = self._lay_driven(driven, time, _shift_w(time, fb, -self._a_work(driven)))
                start, finish = laid[0], laid[1]
            elif by_fin:
                finish = fb
                start = _shift_w(time, finish, -remaining)
            if cmand:
                if cs is not None:
                    start = time.next_work_start(cs)
                    finish = _shift_w(time, start, remaining)
                    if cf is not None and cf > finish:
                        finish = cf
                        start = _shift_w(time, finish, -(self._a_work(driven) if driven else remaining))
                else:
                    finish = cf
                    start = _shift_w(time, finish, -(self._a_work(driven) if driven else remaining))
                if driven:
                    laid = self._lay_driven(driven, time, start)
                    start, finish = laid[0], laid[1]
            if ty == "TT_Mile":
                start = time.next_work_start(start)
                finish = start
            ss, sf = (laid[2], laid[3]) if laid is not None else (None, None)
            res[n] = {"es": start, "ef": finish, "xs": ss if ss is not None else start,
                      "xf": sf if sf is not None else finish, "ss": ss, "sf": sf}
            if drivers is not None:
                drivers[n] = (n, res, "mandatory" if cmand else "finish" if by_fin else "start")
        return res

    def ties(self, entry: Optional[tuple]) -> tuple:
        """Predecessors (task_ids of the project) whose link sets an activity's early dates, from its forward()
        drivers entry: a link bound within a minute of the date (a finish bound laid on the activity
        calendar), or for dates the start bounds set, one whose next work start is the early start. Empty when a
        mandatory constraint sets the dates, when the activity ignores its predecessors, or when only the data
        date, a constraint or a link to another project sets them."""
        if not entry:
            return ()
        n, res, how = entry
        d = self.t[n]
        if how == "mandatory" or self._ignores(d):
            return ()
        plan = self._plan_cache()
        r = res[n]
        date = r["ef"] if how == "finish" else r["es"]
        time = self._time(d)
        to_ws = how == "start" or d["task_type"] == "TT_Mile"
        out = []
        for want in ((True, False) if how == "placed" else ((how == "start"),)):
            for e in plan["by_s"].get(n, ()):
                if (LINK_TO[e[0]["type"]] == "s") != want:
                    continue
                b = self._lbound(e, res, "es", "ef")
                if b is None:
                    continue
                laid = _fbo(time, b) if how == "finish" else b
                if abs(laid - date) <= CPM_TOL or (to_ws and time.next_work_start(laid) == date):
                    out.append(e[0]["pred"])
        return tuple(out)

    def _plan_cache(self) -> dict:
        p = self.__dict__.get("_plan")
        if p is None or p[0] is not self.links:
            p = self._plan = (self.links, self.plan())
        return p[1]

    # ------------------------------------------------------------------ backward pass
    def _bwd_cstr(self, d: dict, time, driven) -> tuple:
        """(mandatory, late start bound, late finish bound) of the constraints on the backward pass."""
        if d["status_code"] == "TK_Complete":
            return False, None, None
        started = d["status_code"] == "TK_Active"
        s = f = None
        mand = False
        for c, dt in self._dated(d):
            if c in ("CS_MSO", "CS_MSOB"):
                s = _mn(s, dt)
            elif c in ("CS_MEO", "CS_MEOB"):
                f = _mn(f, dt)
            elif c == "CS_MANDSTART" and not started:
                kept = d["task_type"] == "TT_Mile" or (d["task_type"] == "TT_Rsrc" and bool(driven))
                s = _mn(s, time.next_work_start(dt) if not kept else dt if time.is_work_time(dt) else time.prev_work_end(dt))
                mand = True
            elif c == "CS_MANDFIN" and not started:
                f = _mn(f, time.prev_work_end(dt))
                mand = True
        return mand, s, f

    @staticmethod
    def _alagged(time, bound: datetime, status: str, lag: int) -> datetime:
        """A late start bound moved by the assignment remaining lag of a task dependent activity."""
        if lag <= 0:
            return bound
        if status == "TK_Active":
            return time.next_work_start(_shift_w(time, time.next_work_start(bound), lag))
        return bound + timedelta(minutes=lag) if status == "TK_Complete" else bound

    def _sbf(self, driven, time, bound: datetime, remaining: int) -> datetime:
        """The late finish bound a late start bound gives."""
        if driven:
            return self._a_finish(driven, _sbo(time, bound))
        return _shift_w(time, time.next_work_start(bound), remaining) if remaining else bound

    def _link_bounds(self, plan: dict, n: int, res: dict, ks: str, kf: str, time, remaining: int, alag: int,
                     driven) -> tuple[datetime, datetime]:
        """(earliest finish bound, earliest start bound laid on the calendar) of n's links of the project from
        its successors' dates res[s][ks / kf] (late dates; early dates for the as late as possible move)."""
        T = self.t
        fin = sbound = _POS
        st = T[n]["status_code"]
        for e in plan["by_p"].get(n, ()):
            lk, lc = e[0], e[1]
            s = T[lk["succ"]]
            if s["task_type"] == "TT_LOE":
                continue  # a level of effort takes its late dates from its predecessors: it bounds none
            prog = s["status_code"] == "TK_Active"
            if prog and self.oos == "override":
                continue
            if LINK_TO[lk["type"]] == "f":
                frm = res[lk["succ"]][kf]
            elif prog and self.oos == "actual":
                frm = s["act_start_date"]
            else:
                frm = res[lk["succ"]][ks]
            if frm is None:
                continue
            b = _shift_w(lc, frm, -e[4])
            if e[6]:
                fin = min(fin, b)
            else:
                moved = self._alagged(time, b, st, alag)
                fin = min(fin, self._sbf(driven, time, moved, remaining))
                sbound = min(sbound, _sbo(time, moved))
        return fin, sbound

    def _laid_finish(self, time, bound: datetime, d: dict, remaining: int) -> datetime:
        if not self._has_cal(d):
            return bound
        done = d["status_code"] == "TK_Complete"
        if d["task_type"] == "TT_Mile" and not done:
            return _sbo(time, bound)
        if remaining or (not done and d["task_type"] in ("TT_FinMile", "TT_Task", "TT_Rsrc")):
            return time.prev_work_end(bound)
        return bound

    @staticmethod
    def _laid_start(time, finish: datetime, sbound: datetime, remaining: int, has_cal: bool) -> datetime:
        if remaining and has_cal:
            return min(_shift_w(time, finish, -remaining), sbound)
        return _shift_w(time, finish, -remaining) if remaining else finish

    def backward(self, plan: dict, res: dict[int, dict]) -> None:
        """Late dates and total float of every activity, added to the forward pass results res (early dates
        moved by as late as possible and the WBS summary spans)."""
        T = self.t
        own = [res[i]["ef"] for i in res if T[i]["task_type"] not in ("TT_LOE", "TT_WBS")]
        pef = max(own) if own else self.dd
        self._pef = pef
        open_end = self.must if self.must is not None else pef
        for n in reversed(plan["order"]):
            d, r = T[n], res[n]
            st = d["status_code"]
            time, has_cal = self._time(d), self._has_cal(d)
            driven = plan["drv"].get(n)
            remaining = _wm(time, r["es"], r["ef"]) if driven or d["task_type"] == "TT_LOE" else self._rem_m(d, r["es"])
            alag = plan["alag"].get(n, 0) if d["task_type"] == "TT_Task" else 0
            fin_b, sbound = self._link_bounds(plan, n, res, "ls", "lf", time, remaining, alag, driven)
            open_ended = self.open_crit and st != "TK_Complete" and not plan["by_p"].get(n) and not plan["ext_s"].get(n)
            finish = min(min(r["ef"], open_end) if open_ended else open_end, fin_b)
            for e in plan["ext_s"].get(n, ()):
                frm = e["ls"] if LINK_TO[e["type"]] == "s" else e["lf"]
                if frm is None:
                    continue
                b = _shift_lag(e["lc"], frm, -e["applied"])
                if LINK_FROM[e["type"]] == "f":
                    finish = min(finish, b)
                else:
                    moved = self._alagged(time, b, st, alag)
                    finish = min(finish, self._sbf(driven, time, moved, remaining))
                    sbound = min(sbound, _sbo(time, moved))
            if d["cstr_type"] == "CS_ALAP" and st == "TK_NotStart":
                # moves later by its free float: to the latest dates its successors' early dates allow
                fb2, sb2 = self._link_bounds(plan, n, res, "es", "ef", time, remaining, alag, driven)
                by_succ = fb2 != _POS
                free_fin = self._laid_finish(time, fb2 if by_succ else finish, d, remaining)
                if driven:
                    free_st = time.next_work_start(self._a_back(driven, free_fin))
                else:
                    free_st = time.next_work_start(self._laid_start(
                        time, free_fin, sb2 if by_succ else min(sbound, sb2), remaining, has_cal))
                if free_st > r["es"]:
                    r["es"], r["ef"] = free_st, free_fin
            cmand, cs, cf = self._bwd_cstr(d, time, driven)
            if not cmand:
                if cs is not None:
                    finish = min(finish, _shift_w(time, time.next_work_start(cs), remaining) if remaining else cs)
                if cf is not None:
                    finish = min(finish, cf)
            finish = self._laid_finish(time, finish, d, remaining)
            start = self._laid_start(time, finish, sbound, remaining, has_cal)
            if driven and not cmand:
                ls_, finish = self._a_late(driven, finish)
                start = sbound if sbound < ls_ and _wm(time, sbound, ls_) == 0 else ls_
            if cmand:
                if cs is not None:
                    start = cs
                    finish = self._a_finish(driven, start) if driven else _shift_w(time, start, remaining)
                    if cf is not None and cf < finish:
                        finish = cf
                        start = _shift_w(time, finish, -remaining)
                elif driven:
                    start, finish = self._a_late(driven, cf)
                else:
                    finish = cf
                    start = _shift_w(time, finish, -remaining)
            r["ls"], r["lf"] = start, finish
        self._loe_late(plan, res)
        self._wbs_spans(res)
        for n, r in res.items():
            d = T[n]
            if d["status_code"] == "TK_Complete":
                r["tf_h"] = None
                continue
            time = self._time(d)
            fin, stf = _wm(time, r["ef"], r["lf"]), _wm(time, r["es"], r["ls"])
            r["tf_h"] = (stf if self.ft == "start" else min(stf, fin) if self.ft == "min" else fin) / 60

    def _loe_late(self, plan: dict, res: dict) -> None:
        """Late dates of level of effort activities, mirrored from their predecessors' late dates."""
        for n in plan["order"]:
            d = self.t[n]
            if d["task_type"] != "TT_LOE":
                continue
            r = res[n]
            if d["status_code"] == "TK_Complete":
                r["ls"] = r["lf"] = None
                continue
            time = self._time(d)
            st_t = ft_t = None
            for e in plan["by_s"].get(n, ()):
                b = self._lbound(e, res, "ls", "lf")
                if LINK_TO[e[0]["type"]] == "s":
                    st_t = _mx(st_t, b)
                else:
                    ft_t = _mx(ft_t, b)
            fin = r["lf"] if ft_t is None else _fbo(time, ft_t)
            r["lf"] = fin
            r["ls"] = fin if st_t is None else min(time.next_work_start(st_t), fin)

    def _wbs_spans(self, res: dict) -> None:
        """WBS summary activities span the activities of their WBS node and every node below (LOE and WBS
        summary left out, completed ones included); a summary on the project node spans every activity."""
        span: dict = {}
        par = self.wbs_parent
        for i, x in self.t.items():
            if x["task_type"] in ("TT_LOE", "TT_WBS"):
                continue
            r = res[i]
            if None in (r["es"], r["ef"], r["ls"], r["lf"]):
                continue
            w, steps, keys = x["wbs"], 0, [None]
            while w is not None and steps <= len(par):
                keys.append(w)
                w, steps = par.get(w), steps + 1
            for k in keys:
                g = span.get(k)
                span[k] = [r["es"], r["ef"], r["ls"], r["lf"]] if g is None else [
                    min(g[0], r["es"]), max(g[1], r["ef"]), min(g[2], r["ls"]), max(g[3], r["lf"])]
        for n, d in self.t.items():
            if d["task_type"] == "TT_WBS" and d["wbs"] in span:
                g = span[d["wbs"]]
                res[n].update(es=g[0], ef=g[1], ls=g[2], lf=g[3])

    # ------------------------------------------------------------------ fields F9 writes besides the dates
    def _fields(self, plan: dict, res: dict) -> None:
        T = self.t
        ff = self._free_float(plan, res)
        lp = self._longest(plan, res)
        fpath, forder = self._float_paths(plan, res)
        ctype = self.prj["critical_path_type"]
        thr = _h2m(self.prj["critical_drtn_hr_cnt"])
        for n, r in res.items():
            d = T[n]
            done = d["status_code"] == "TK_Complete"
            r["ff_h"] = None if done else ff[n] / 60
            r["longest"] = n in lp
            r["critical"] = False if done else (n in lp if ctype == "CT_DrivPath" else _h2m(r["tf_h"]) <= thr)
            r["float_path"], r["float_path_order"] = fpath.get(n), forder.get(n)
            r["rs"], r["rf"] = (None, None) if done else (r["es"], r["ef"])
            r["rls"], r["rlf"] = (None, None) if done else (r["ls"], r["lf"])
            r["ps"], r["pf"] = (r["es"], r["ef"]) if d["status_code"] == "TK_NotStart" else (None, None)
            if self._from_expect(d):
                r["rem_h"] = self._rem_m(d, r["es"]) / 60
            elif not done and (plan["drv"].get(n) or d["task_type"] in ("TT_LOE", "TT_WBS")):
                r["rem_h"] = _wm(self._time(d), r["es"], r["ef"]) / 60
            if r.get("ss") is not None:  # P6 stores the first assignment's start apart from the remaining dates
                r["es"], r["ef"] = r["ss"], r["sf"]
                if d["status_code"] == "TK_NotStart":
                    r["ps"], r["pf"] = r["rs"], r["rf"]

    def _free_float(self, plan: dict, res: dict) -> dict:
        """Free float in working minutes of each open activity's calendar."""
        T, out = self.t, {}
        for n, d in T.items():
            if d["status_code"] == "TK_Complete":
                continue
            if d["task_type"] == "TT_LOE" or self._from_expect(d) or (
                    d["status_code"] == "TK_NotStart" and d["cstr_type"] in ("CS_MANDSTART", "CS_MANDFIN")):
                out[n] = 0
                continue
            time, r = self._time(d), res[n]
            free = None
            for e in plan["by_p"].get(n, ()):
                lk = e[0]
                s = T[lk["succ"]]
                if self._ignores(s):
                    continue
                to = res[lk["succ"]]["es"] if LINK_TO[lk["type"]] == "s" else res[lk["succ"]]["ef"]
                own = r["ef"] if LINK_FROM[lk["type"]] == "f" else r["es"]
                g = _wm(time, own, _shift_lag(e[1], to, -e[2]))
                free = g if free is None else min(free, g)
            for e in plan["ext_s"].get(n, ()):
                to = e["es"] if LINK_TO[e["type"]] == "s" else e["ef"]
                if to is None:
                    continue
                own = r["ef"] if LINK_FROM[e["type"]] == "f" else r["es"]
                g = _wm(time, own, _shift_lag(e["lc"], to, -e["applied"]))
                free = g if free is None else min(free, g)
            if free is None:
                free = _wm(time, r["ef"], self._pef)
            out[n] = max(0, free)
        return out

    def _longest(self, plan: dict, res: dict) -> set:
        """Longest path: open activities finishing at the latest early finish (LOE and WBS summary left out of
        that finish), then the predecessors driving a flagged activity, traced back."""
        T = self.t
        ends = [res[n]["ef"] for n, d in T.items()
                if d["task_type"] not in ("TT_LOE", "TT_WBS") and d["status_code"] != "TK_Complete"]
        if not ends:
            return set()
        end = max(ends)
        flags = {n for n, d in T.items() if d["status_code"] != "TK_Complete" and res[n]["ef"] == end}
        stack = list(flags)
        while stack:
            n = stack.pop()
            d = T[n]
            if self._ignores(d):
                continue
            time = self._time(d)
            for e in plan["by_s"].get(n, ()):
                p = e[0]["pred"]
                if p in flags:
                    continue
                date = res[n]["es"] if LINK_TO[e[0]["type"]] == "s" else res[n]["ef"]
                b = self._lbound(e, res, "es", "ef")
                if b is not None and abs(_wm(time, b, date)) <= 1:
                    flags.add(p)
                    stack.append(p)
        return flags

    def _float_paths(self, plan: dict, res: dict) -> tuple[dict, dict]:
        """Multiple float paths (enable_multiple_longest_path_calc): ({task_id: path}, {task_id: order})."""
        if self.fp is None:
            return {}, {}
        T = self.t
        done = lambda n: T[n]["status_code"] == "TK_Complete"  # noqa: E731
        end = self.fp["end"] if self.fp["end"] in T else None
        if end is None:
            pool = set(T)
        else:
            pool, stack = {end}, [end]
            while stack:
                for e in plan["by_s"].get(stack.pop(), ()):
                    if e[0]["pred"] not in pool:
                        pool.add(e[0]["pred"])
                        stack.append(e[0]["pred"])
        pool = {n for n in pool if not done(n)}
        total = self.fp["total"]
        fl: dict = {}
        for e in plan["links"]:
            lk, p, s = e[0], e[0]["pred"], e[0]["succ"]
            rp, rs = res[p], res[s]
            frm = rp["ef"] if LINK_FROM[lk["type"]] == "f" else rp["es"]
            to = (rs["ls"] if total else rs["es"]) if LINK_TO[lk["type"]] == "s" else (rs["lf"] if total else rs["ef"])
            fl[e[3]] = None if frm is None or to is None else _wm(self._time(T[p]), frm, _shift_lag(e[1], to, -e[2]))
        code = lambda n: T[n]["task_code"]  # noqa: E731
        starts = sorted(pool, key=lambda a: (_h2m(res[a]["tf_h"]), -_mins(res[a]["ef"]), -_mins(res[a]["es"]), code(a)))
        path, order, traced = {}, {}, {}

        def driving(a):
            best = None
            for e in plan["by_s"].get(a, ()):
                p, f = e[0]["pred"], fl[e[3]]
                if (p not in pool and not done(p)) or f is None:
                    continue
                k = (f, -_mins(res[p]["ef"]), _mins(res[p]["es"]), code(p), e[3])
                if best is None or k < best[0]:
                    best = (k, e)
            return best[1] if best else None
        frontier: list = []
        mx = self.fp["max"] if self.fp["limit"] else float("inf")
        nxt, number = 0, 0
        while number < mx:
            number += 1
            first = None
            while frontier:
                p = heapq.heappop(frontier)[-1]
                if p not in traced:
                    first = p
                    break
            while first is None and nxt < len(starts):
                a = starts[nxt]
                nxt += 1
                if a not in traced:
                    first = a
            if first is None:
                break
            members, a = [], first
            while True:
                traced[a] = number
                if a in pool:
                    members.append(a)
                e = driving(a)
                if e is None or e[0]["pred"] in traced:
                    break
                a = e[0]["pred"]
            members.reverse()
            for i, a in enumerate(members, 1):
                path[a], order[a] = number, i
            for a in members:
                for e in plan["by_s"].get(a, ()):
                    p = e[0]["pred"]
                    if p in pool and p not in traced and fl[e[3]] is not None:
                        heapq.heappush(frontier, (fl[e[3]], path[a], -order[a], code(p), e[3], p))
        return path, order

    def finish(self, res: dict) -> tuple[Optional[dict], datetime]:
        """(finish milestone or last activity, project early finish)."""
        own = [d for d in self.t.values() if d["task_type"] not in ("TT_LOE", "TT_WBS")]
        succ = {lk["pred"] for lk in self.links}
        ends = [d for d in own if d["task_id"] not in succ and d["status_code"] != "TK_Complete"] or own
        fm = max(ends, key=lambda d: (res[d["task_id"]]["ef"], d["task_type"] == "TT_FinMile", d["task_code"]), default=None)
        return fm, max((res[d["task_id"]]["ef"] for d in own), default=self.dd)
