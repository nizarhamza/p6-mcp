"""Why an engine date differs from the stored one (p6_cpm_validate), what-if edits on a network copy and
their assumptions, the DCMA critical path test on the engine."""

from __future__ import annotations

import copy
import functools
from collections import Counter
from datetime import datetime, timedelta
from typing import Callable, Mapping, Optional, get_args

from p6mcp.core.constants import (
    ACT_LINKS_CAP, CSTR_FINISH, _cstr_label, CSTR_TYPES, HARD_CSTR_SET, HPD_SQL, REL_TYPES_REV,
)
from p6mcp.registry import FragnetType, WhatIfOp
from p6mcp.core.db import _alive, _baseline_pid, _cols, _connect, _days, _db_dt, _fmt_dt, _num
from p6mcp.core.calendar import _resolve_dt, _week_text
from p6mcp.core.wbs_codes import _name_sep, _wbs_tree
from p6mcp.core.editlog import _edit_state, _log_entries, _undone_fn
from p6mcp.core.read import _lag_arg, _rel_code
from p6mcp.core.netcache import _cached, _peek, _Ticket, _ticket
from p6mcp.core.engine import (
    _Cpm, _cpm_load, _cpm_run, CPM_TF_TOL_H, CPM_TOL, CPM_TRUST, _ELAPSED, GAP_TOL_H, LINK_FROM, LINK_TO, _mx, _Net,
)
from p6mcp.core.analysis import _dcma, _dcma_settings
from p6mcp.messages import _join, _t


CPM_DIFF_LABELS = {"es": _t("early start"), "ef": _t("early finish"), "ls": _t("late start"), "lf": _t("late finish"),
                   "tf_d": _t("total float (d)")}  # differences_text of p6_cpm_validate rows


# ----------------------------------------------------------------- p6_cpm_validate: why a date differs

F9_SAVE_TOL = timedelta(minutes=5)  # rows stamped this long after last_schedule_date count as the F9 save
IMPORT_WINDOW = timedelta(minutes=10)  # no import entry in the edit log: rows this long after the project's creation
STALE_SAMPLE = 10  # Activity IDs listed per kind of change in stale_f9_detail
CAUSE_CLASSES = ("stale_f9", "data", "engine", "unknown")  # precedence of p6_cpm_validate cause_class
CTX_LINKS = 10  # relationships listed per side in a context block
CTX_WINDOW_WD = 10  # working days each side of the differing date for the non-work days of a context block
OOS_TEXT = {"retained": _t("retained logic"), "override": _t("progress override"), "actual": _t("actual dates"),
            "both": _t("retained logic and progress override both on (P6 25.12 runs retained logic)")}
FLOAT_TEXT = {"finish": _t("finish float (late finish - early finish)"), "start": _t("start float (late start - early start)"),
              "min": _t("smallest of start and finish float")}
LAG_CAL_TEXT = {"rcal_Predecessor": _t("predecessor calendar"), "rcal_Successor": _t("successor calendar"),
                "rcal_24Hour": _t("24-hour calendar"), "rcal_ProjDefault": _t("project default calendar")}


def _stale_f9(con, pid: int, cpm: _Cpm) -> dict:
    """Inputs of the project's schedule changed after its last F9, from update dates (run in the snapshot that
    loaded cpm). Reference time: last_schedule_date + F9_SAVE_TOL, or the end of the project's last XER import
    into this database when later (edit log; without an entry, PROJECT.create_date + IMPORT_WINDOW when the
    project was created after its last F9): an import writes every row after the F9 the XER carries. Changed:
    activities (TASK), links touching the project (TASKPRED), assignments (TASKRSRC), calendars used (activity,
    project and resource calendars, with their bases), the scheduling options (PROJPROP 'scheduling'), activities
    of other projects linked to it, deletions (REFRDEL), and this server's writes (edit log, _edit_state)."""
    pcols = _cols(con, "PROJECT")
    prj = con.execute("SELECT " + ", ".join(c if c in pcols else f"NULL AS {c}" for c in ("last_schedule_date", "create_date"))
                      + " FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone()
    lsd_raw = prj["last_schedule_date"]
    lsd = _db_dt(lsd_raw)
    out = {"stale": False, "last_schedule_date": lsd_raw, "reference": None, "basis": None, "acts": {}, "links": {},
           "asg": {}, "cals": {}, "options": None, "deleted": Counter(), "deleted_latest": None, "ext": {},
           "server_edits": _edit_state(pid, lsd_raw), "project_level": []}
    if out["server_edits"]:
        out["project_level"].append(_t("this server changed the schedule after the F9 (edit log, last at {at})",
                                       at=out["server_edits"]["at"]))
    if lsd is None:
        out["stale"] = bool(out["project_level"])
        return out
    ref, basis = lsd + F9_SAVE_TOL, _t("last F9 + {min} min (rows P6 writes when it saves the F9)",
                                       min=int(F9_SAVE_TOL.total_seconds() // 60))
    entries = _log_entries()
    undone = _undone_fn(entries)
    imp = [_db_dt(e["ts"]) for e in entries if str(e.get("operation", "")).startswith("import_xer")
           and e.get("proj_id") == pid and not undone(e)]
    imp = [t for t in imp if t is not None]
    created = _db_dt(prj["create_date"])
    if imp and max(imp) > ref:  # update dates have whole seconds: a row stamped in a later second is a change
        ref, basis = max(imp), _t("end of the XER import into this database (edit log)")
    elif not imp and created is not None and created > lsd and created + IMPORT_WINDOW > ref:
        ref, basis = created + IMPORT_WINDOW, _t("project created after its last F9 (an XER import): rows within {min} "
                                                 "min of its creation count as the import",
                                                 min=int(IMPORT_WINDOW.total_seconds() // 60))
    out["reference"], out["basis"] = _fmt_dt(ref), basis
    refs = _fmt_dt(ref)
    al = functools.partial(_alive, con)
    later = lambda v: (lambda x: x is not None and x > ref)(_db_dt(v))  # noqa: E731
    for n, d in cpm.t.items():
        if d.get("update_date") is not None and d["update_date"] > ref:
            out["acts"][n] = d["update_date"]
    if "update_date" in _cols(con, "TASKPRED"):
        for r in con.execute("SELECT r.task_pred_id, r.task_id, r.pred_task_id, r.update_date FROM TASKPRED r "
                             "JOIN TASK s ON s.task_id = r.task_id JOIN TASK p ON p.task_id = r.pred_task_id "
                             "WHERE (s.proj_id = ? OR p.proj_id = ?) AND r.update_date > ?" + al("TASKPRED", "r")
                             + al("TASK", "s") + al("TASK", "p"), (pid, pid, refs)):
            if later(r[3]):
                out["links"][r[0]] = (r[1], r[2], _db_dt(r[3]))
    if "update_date" in _cols(con, "TASKRSRC"):
        for r in con.execute("SELECT a.task_id, MAX(a.update_date) FROM TASKRSRC a JOIN TASK t ON t.task_id = a.task_id "
                             "WHERE t.proj_id = ? AND a.update_date > ?" + al("TASKRSRC", "a") + al("TASK", "t")
                             + " GROUP BY a.task_id", (pid, refs)):
            if later(r[1]):
                out["asg"][r[0]] = _db_dt(r[1])
    ccols = _cols(con, "CALENDAR")
    if {"update_date", "base_clndr_id"} <= ccols:
        rows = {r[0]: (r[1], r[2], _db_dt(r[3])) for r in con.execute(
            "SELECT clndr_id, clndr_name, base_clndr_id, update_date FROM CALENDAR WHERE 1 = 1" + al("CALENDAR"))}
        used = {d["clndr_id"] for d in cpm.t.values()} | {cpm.prj["clndr_id"]} | {a["rsrc_clndr_id"] for a in cpm.asg}
        for lk in cpm.ext_pred + cpm.ext_succ:
            if lk.get("cal") is not None:
                used.add(lk["cal"].clndr_id)
        for cid in used:
            k, seen = cid, set()
            while k in rows and k not in seen and len(seen) <= 10:
                seen.add(k)
                if rows[k][2] is not None and rows[k][2] > ref:
                    out["cals"][k] = (rows[k][0], rows[k][2])
                k = rows[k][1]
        out["cal_chain"] = {}
        for cid in used:
            k, chain = cid, []
            while k in rows and k not in chain and len(chain) <= 10:
                chain.append(k)
                k = rows[k][1]
            out["cal_chain"][cid] = [c for c in chain if c in out["cals"]]
    pp = _cols(con, "PROJPROP")
    if "update_date" in pp:
        r = con.execute("SELECT update_date FROM PROJPROP WHERE proj_id = ? AND prop_name = 'scheduling'" + al("PROJPROP"),
                        (pid,)).fetchone()
        if r and later(r[0]):
            out["options"] = _db_dt(r[0])
            out["project_level"].append(_t("the scheduling options were changed at {at}", at=_fmt_dt(out["options"])))
    if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'REFRDEL'").fetchone():
        for tbl, n, last in con.execute("SELECT table_name, COUNT(*), MAX(delete_date) FROM REFRDEL WHERE proj_id = ? "
                                        "AND table_name IN ('TASK', 'TASKPRED', 'TASKRSRC') AND delete_date > ? "
                                        "GROUP BY table_name", (pid, refs)):
            out["deleted"][tbl] = n
            out["deleted_latest"] = _mx(out["deleted_latest"], _db_dt(last))
        if out["deleted"]:
            out["project_level"].append(_t("rows deleted after the F9: {rows}", rows=_join(", ", (
                _deleted_text(t, n) for t, n in sorted(out["deleted"].items())))))
    other = {}
    for lk in cpm.ext_pred:
        other.setdefault(lk["pred"], set()).add(lk["succ"])
    for lk in cpm.ext_succ:
        other.setdefault(lk["succ"], set()).add(lk["pred"])
    if other:
        ids = sorted(other)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for tid, code, upd in con.execute(f"SELECT task_id, task_code, update_date FROM TASK WHERE task_id IN "
                                              f"({','.join('?' * len(chunk))})", chunk):
                if later(upd):
                    for own in other[tid]:
                        out["ext"].setdefault(own, []).append((code, _db_dt(upd)))
    if cpm.rule == "rcal_ProjDefault" and out.get("cal_chain", {}).get(cpm.prj["clndr_id"]):
        out["project_level"].append(_t("the project calendar (lag calendar of every link) was changed after the F9"))
    out["stale"] = bool(out["project_level"] or out["acts"] or out["links"] or out["asg"] or out["cals"] or out["ext"])
    return out


LABEL_OF_TABLE = {"TASK": "activities", "TASKPRED": "relationships", "TASKRSRC": "assignments"}  # rows_deleted keys


def _deleted_text(table: str, n: int):
    """'{n} activities' (relationships, assignments) of the rows deleted after the F9."""
    if table == "TASK":
        return _t("{n} activities", n=n)
    if table == "TASKPRED":
        return _t("{n} relationships", n=n)
    if table == "TASKRSRC":
        return _t("{n} assignments", n=n)
    return f"{n} {table}"


def _stale_view(ev: dict, cpm: _Cpm) -> dict:
    """stale_f9_detail of p6_cpm_validate (counts, latest change, samples of Activity IDs)."""
    T = cpm.t
    code = lambda n: T[n]["task_code"] if n in T else str(n)  # noqa: E731
    link_acts = sorted({code(x) for s, p, _ in ev["links"].values() for x in (s, p) if x in T})
    dts = ([*ev["acts"].values(), *(x[2] for x in ev["links"].values()), *ev["asg"].values(),
            *(x[1] for x in ev["cals"].values()), *(dt for v in ev["ext"].values() for _, dt in v)]
           + [x for x in (ev["options"], ev["deleted_latest"]) if x is not None])
    se = ev["server_edits"]
    return {"stale": ev["stale"], "last_schedule_date": ev["last_schedule_date"], "changed_after": ev["reference"],
            "basis": ev["basis"],
            "activities_changed": {"count": len(ev["acts"]), "sample": sorted(code(n) for n in ev["acts"])[:STALE_SAMPLE]},
            "relationships_changed": {"count": len(ev["links"]), "activities": link_acts[:STALE_SAMPLE]},
            "assignments_changed": {"count": len(ev["asg"]), "sample": sorted(code(n) for n in ev["asg"])[:STALE_SAMPLE]},
            "calendars_changed": sorted(name for name, _ in ev["cals"].values()),
            "schedule_options_changed": ev["options"] and _fmt_dt(ev["options"]),
            "rows_deleted": {LABEL_OF_TABLE[t]: n for t, n in sorted(ev["deleted"].items())},
            "other_projects_changed": sorted({c for v in ev["ext"].values() for c, _ in v})[:STALE_SAMPLE],
            "server_edits": se, "latest_change": _fmt_dt(max(dts)) if dts else (se and se["at"]),
            "note": (_t("Inputs changed after the last F9: the stored dates are out of date, not wrong. Press F9 in "
                        "P6, or compare on a frozen copy of the database, before trusting this comparison.",
                        ) if ev["stale"] else
                     _t("Nothing changed after the last F9 (update dates of activities, relationships, assignments, "
                        "calendars, schedule options and linked activities of other projects; deletions; this "
                        "server's edit log).") if ev["reference"] else
                     _t("No last F9 recorded (last_schedule_date empty): changes after it cannot be dated."))}


def _stale_min(stale: bool) -> dict:
    """Evidence when only the edit log flag is known (what-if, risk, critical path test: rows unused)."""
    return {"stale": stale, "acts": {}, "links": {}, "asg": {}, "cals": {}, "cal_chain": {}, "ext": {},
            "project_level": [_t("this server changed the schedule after the F9 (edit log)")] if stale else []}


def _data_issues(cpm: _Cpm, n: int, by_s: dict) -> list[str]:
    """Invalid or contradictory inputs of one activity (cause_class data)."""
    T, dd, d = cpm.t, cpm.dd, cpm.t[n]
    st, as_, af = d["status_code"], d["act_start_date"], d["act_end_date"]
    out = []
    if d.get("bad_dates"):
        out.append(_t("unreadable stored date(s): {dates}", dates=", ".join(d["bad_dates"])))
    if dd is not None and as_ is not None and as_ > dd + CPM_TOL:
        out.append(_t("actual start {date} after the data date {data_date}", date=_fmt_dt(as_), data_date=_fmt_dt(dd)))
    if dd is not None and af is not None and af > dd + CPM_TOL:
        out.append(_t("actual finish {date} after the data date {data_date}", date=_fmt_dt(af), data_date=_fmt_dt(dd)))
    if as_ is not None and af is not None and af < as_:
        out.append(_t("actual finish before the actual start"))
    if st == "TK_Complete" and af is None:
        out.append(_t("completed without an actual finish"))
    if st in ("TK_Active", "TK_Complete") and as_ is None:
        out.append(_t("started without an actual start"))
    if st == "TK_NotStart" and (as_ is not None or af is not None):
        out.append(_t("not started but carries actual dates"))
    if st == "TK_Active" and af is not None:
        out.append(_t("in progress with an actual finish"))
    if d["cal"] is None:
        out.append(_t("calendar {id} missing", id=d["clndr_id"]))
    elif d["cal"].error:
        out.append(_t("calendar {name!r} unreadable ({error})", name=d["cal"].name, error=d["cal"].error))
    for c, dt, which in ((d["cstr_type"], d["cstr_date"], "primary"), (d["cstr_type2"], d["cstr_date2"], "secondary")):
        if c and c != "CS_ALAP" and dt is None:
            out.append(_t("primary constraint {label} without a date", label=_cstr_label(c) or c) if which == "primary"
                       else _t("secondary constraint {label} without a date", label=_cstr_label(c) or c))
    if d["suspend_date"] is not None and d["resume_date"] is not None and d["resume_date"] < d["suspend_date"]:
        out.append(_t("resume date before the suspend date"))
    if d["rem"] < 0:
        out.append(_t("negative remaining duration"))
    if st != "TK_Complete" and (d["early_start_date"] is None or d["early_end_date"] is None) and not d.get("bad_dates"):
        out.append(_t("no stored early dates"))
    if as_ is not None:  # out of sequence progress: started or finished before its predecessors allow
        for lk in by_s.get(n, ()):
            p = T.get(lk["pred"])
            if p is None:
                continue
            ty, pas, paf = lk["type"], p["act_start_date"], p["act_end_date"]
            if ty == "PR_FS" and (paf is None or paf > as_ + CPM_TOL):
                out.append(_t("out of sequence: started {start} before its FS predecessor {code} finished",
                              start=_fmt_dt(as_), code=p["task_code"]) if paf is None else
                           _t("out of sequence: started {start} before its FS predecessor {code} finished ({finish})",
                              start=_fmt_dt(as_), code=p["task_code"], finish=_fmt_dt(paf)))
            elif ty == "PR_SS" and (pas is None or pas > as_ + CPM_TOL):
                out.append(_t("out of sequence: started before its SS predecessor {code} started", code=p["task_code"]))
            elif ty == "PR_FF" and af is not None and (paf is None or paf > af + CPM_TOL):
                out.append(_t("out of sequence: finished before its FF predecessor {code} finished", code=p["task_code"]))
    return out


def _engine_rules(cpm: _Cpm, n: int, bad: Mapping, plan: dict, by_p: dict) -> list[str]:
    """Engine rules that act on this activity's own dates (named when same inputs give another result)."""
    d, out = cpm.t[n], []
    st, ty = d["status_code"], d["task_type"]
    if ty == "TT_Rsrc" and plan["drv"].get(n):
        out.append(_t("resource dependent activity laid on its resources' calendars (assignment units, rates, lags)"))
    if ty == "TT_LOE":
        out.append(_t("level of effort spans the dates of its links"))
    if ty == "TT_WBS":
        out.append(_t("WBS summary spans the activities of its WBS node"))
    if cpm._from_expect(d):
        out.append(_t("expected finish sets the remaining duration (sched_use_expect_end_flag)"))
    for c, dt in ((d["cstr_type"], d["cstr_date"]), (d["cstr_type2"], d["cstr_date2"])):
        if c == "CS_ALAP":
            out.append(_t("as late as possible (moved by its free float)"))
        elif c:
            out.append(_t("{label} constraint on {date}", label=_cstr_label(c) or c, date=_fmt_dt(dt)) if dt else
                       _t("{label} constraint", label=_cstr_label(c) or c))
    if st == "TK_Active" and d["resume_date"] is not None:
        out.append(_t("resume date bounds the remaining work"))
    if plan["alag"].get(n):
        out.append(_t("assignment lag on the activity's links"))
    if st == "TK_NotStart" and cpm.pstart is not None and cpm.dd is not None and cpm.pstart > cpm.dd:
        out.append(_t("project planned start after the data date bounds the start"))
    for e in plan["by_s"].get(n, ()):
        if e[0]["lag"] != 0 and e[2] != e[0]["lag"]:
            out.append(_t("lag from the progressed predecessor {code} reduced by the time elapsed since its actual date",
                          code=cpm.t[e[0]["pred"]]["task_code"]))
            break
    if set(bad) <= {"ls", "lf", "tf_d"}:
        if not by_p.get(n) and not any(lk["pred"] == n for lk in cpm.ext_succ):
            out.append(_t("open end: its late finish is the must finish by date or the project finish"))
        if cpm.ft != "finish" and "tf_d" in bad:
            out.append(_t("total float as {kind} (sched_float_type)", kind=FLOAT_TEXT[cpm.ft]))
    return out


def _cpm_classify(cpm: _Cpm, mism: dict, ev: dict, never: bool) -> dict[int, dict]:
    """{task_id: {cause_class, probable_cause, evidence, inherited_from}} of each mismatched activity.
    Precedence: stale_f9 (own inputs, or the project's, changed after the F9), data (invalid input), the class
    of the mismatched neighbour it inherits the difference from, engine (a rule of its own named), unknown."""
    T = cpm.t
    plan = cpm._plan_cache()
    by_s, by_p = {}, {}
    for lk in cpm.links:
        by_s.setdefault(lk["succ"], []).append(lk)
        by_p.setdefault(lk["pred"], []).append(lk["succ"])
    link_upd: dict[int, list] = {}
    for _lid, (s, p, dt) in ev["links"].items():
        for own, other in ((s, p), (p, s)):
            if own in T:
                link_upd.setdefault(own, []).append((T[other]["task_code"] if other in T else str(other), dt))
    ext = {lk["succ"] for lk in cpm.ext_pred} | {lk["pred"] for lk in cpm.ext_succ}
    own: dict[int, dict] = {}
    for n in mism:
        d = T[n]
        stale = list(ev["project_level"])
        if n in ev["acts"]:
            stale.append(_t("the activity was changed at {at}", at=_fmt_dt(ev["acts"][n])))
        for other, dt in sorted(link_upd.get(n, []), key=lambda x: x[1])[-2:]:
            stale.append(_t("its relationship with {code} was changed at {at}", code=other, at=_fmt_dt(dt)))
        if n in ev["asg"]:
            stale.append(_t("its assignments were changed at {at}", at=_fmt_dt(ev["asg"][n])))
        for cid in ev.get("cal_chain", {}).get(d["clndr_id"], []):
            stale.append(_t("its calendar {name!r} was changed at {at}", name=ev["cals"][cid][0],
                            at=_fmt_dt(ev["cals"][cid][1])))
        for code, dt in ev["ext"].get(n, []):
            stale.append(_t("the linked activity {code} of another project was changed at {at}", code=code,
                            at=_fmt_dt(dt)))
        data = [_t("no F9 recorded (last_schedule_date empty): the stored dates are no oracle")] if never else []
        data += _data_issues(cpm, n, by_s)
        own[n] = {"stale": stale, "data": data, "rules": _engine_rules(cpm, n, mism[n], plan, by_p), "ext": n in ext}
    rank = {c: i for i, c in enumerate(CAUSE_CLASSES)}
    cands: dict[int, list] = {}  # mismatched neighbours whose difference n can inherit: (task_id, side)
    for n, bad in mism.items():
        c = []
        if set(bad) & {"es", "ef"}:
            c += [(lk["pred"], "early") for lk in by_s.get(n, ()) if lk["pred"] in mism and lk["pred"] != n
                  and set(mism[lk["pred"]]) & {"es", "ef"}]
        if set(bad) & {"ls", "lf", "tf_d"}:
            c += [(q, "late") for q in by_p.get(n, ()) if q in mism and q != n and set(mism[q]) & {"ls", "lf"}]
        cands[n] = c
    out: dict[int, dict] = {}
    for n, o in own.items():
        evidence = o["stale"] + o["data"] + o["rules"]
        if o["ext"]:
            evidence.append(_t("linked to another project (its stored dates are used)"))
        out[n] = {"cause_class": None, "probable_cause": None, "evidence": evidence, "inherited_from": None}
        if o["stale"]:
            out[n].update(cause_class="stale_f9", probable_cause=_t(
                "changed after the last F9: {changes}. The stored dates are out of date, not wrong: press F9 in P6 first",
                changes=_join("; ", o["stale"][:3])))
        elif o["data"]:
            out[n].update(cause_class="data", probable_cause=_t("invalid or contradictory input: {issues}",
                                                                 issues=_join("; ", o["data"][:3])))

    def spread() -> None:
        """Unresolved rows inherit the best class among their resolved neighbours, until nothing changes."""
        changed = True
        while changed:
            changed = False
            for n in mism:
                if out[n]["cause_class"] is not None and out[n]["inherited_from"] is None:
                    continue  # its own finding
                got = [(rank[out[q]["cause_class"]], T[q]["task_code"], q, side) for q, side in cands[n]
                       if out[q]["cause_class"] is not None]
                if not got:
                    continue
                r, _, q, side = min(got)
                cur = out[n]["cause_class"]
                if cur is not None and rank[cur] <= r:
                    continue
                out[n].update(cause_class=out[q]["cause_class"],
                              inherited_from={"task_code": T[q]["task_code"], "task_name": T[q]["task_name"]},
                              probable_cause=_t("inherits the early date difference of {code} - {name} ({cause_class})",
                                                code=T[q]["task_code"], name=T[q]["task_name"],
                                                cause_class=out[q]["cause_class"]) if side == "early" else
                              _t("inherits the late date difference of {code} - {name} ({cause_class})",
                                 code=T[q]["task_code"], name=T[q]["task_name"], cause_class=out[q]["cause_class"]))
                changed = True
    spread()
    for n, o in own.items():
        if out[n]["cause_class"] is None and o["rules"]:
            out[n].update(cause_class="engine", probable_cause=_t(
                "same inputs, another result: engine rule {rule} (also: {others})", rule=o["rules"][0],
                others=_join("; ", o["rules"][1:3])) if len(o["rules"]) > 1 else
                _t("same inputs, another result: engine rule {rule}", rule=o["rules"][0]))
    spread()
    for pass_ in (0, 1):  # rows without a resolved neighbour first, then whatever a loop of differences left
        for n, o in own.items():
            if out[n]["cause_class"] is None and (pass_ or not cands[n]):
                out[n].update(cause_class="unknown", probable_cause=(
                    _t("unknown: linked to another project whose stored dates did not change after the F9 (schedule "
                       "that project in P6, then this one)") if o["ext"] else
                    _t("unknown: same inputs and no rule of this activity explains it (resource leveling is not "
                       "modelled and the database does not record it)")))
        spread()
    return out


def _cal_window(cal, at: datetime) -> dict:
    """Non-work days within CTX_WINDOW_WD working days each side of at (weekly or exception), and the exception
    days in that window that keep other working hours."""
    if cal is None or getattr(cal, "error", None) or not hasattr(cal, "week"):
        return {"from": None, "to": None, "non_work_days": [], "exception_hours": []}
    lo = hi = at.date()
    k = 0
    while k < CTX_WINDOW_WD and (at.date() - lo).days < 120:
        lo -= timedelta(days=1)
        k += cal.is_workday(lo)
    k = 0
    while k < CTX_WINDOW_WD and (hi - at.date()).days < 120:
        hi += timedelta(days=1)
        k += cal.is_workday(hi)
    off, exc, hours = [], [], []
    for i in range((hi - lo).days + 1):
        x = lo + timedelta(days=i)
        if not cal.is_workday(x):
            off.append(x.isoformat())
            if x in cal.exc:
                exc.append(x.isoformat())
        elif x in cal.exc and cal.exc[x] != cal.week[x.weekday()]:
            hours.append({"date": x.isoformat(), "hours": round(cal.day_hours(x), 2)})
    return {"from": lo.isoformat(), "to": hi.isoformat(), "non_work_days": off, "holidays": exc,
            "exception_hours": hours}


def _cpm_context(cpm: _Cpm, res: dict, net: "_Net", net_e: "_Net", n: int, bad: Mapping) -> dict:
    """Inputs the engine and P6 both used for one mismatched activity: the activity, its calendar around the
    differing date, its relationships with the link that drives each date (stored F9 and engine), lag and lag
    calendar."""
    d = cpm.t[n]
    hpd = d["hpd"] or 8.0
    when = [x for k in ("es", "ef", "ls", "lf") if k in bad for x in (_db_dt(bad[k]["stored"]), _db_dt(bad[k]["engine"]))
            if x is not None] or [x for x in (res[n]["lf"], res[n]["ef"]) if x is not None]
    cal = d["cal"]
    cstr = lambda c, dt: {"type": _cstr_label(c) or c, "date": dt and _fmt_dt(dt)} if c else None  # noqa: E731
    act = {"task_type": d["task_type"], "status_code": d["status_code"],
           "calendar": {"name": cal.name if cal is not None else None, "hours_per_day": d["hpd"],
                        "work_week": _week_text(cal), "error": cal.error if cal is not None else "missing",
                        "around": when and _fmt_dt(min(when)), **(_cal_window(cal, min(when)) if when else {})},
           "constraint": cstr(d["cstr_type"], d["cstr_date"]), "constraint2": cstr(d["cstr_type2"], d["cstr_date2"]),
           "actual_start": d["act_start_date"] and _fmt_dt(d["act_start_date"]),
           "actual_finish": d["act_end_date"] and _fmt_dt(d["act_end_date"]),
           "remaining_d": _days(d["rem"], hpd), "original_d": _days(d.get("target_drtn_hr_cnt"), hpd),
           "suspend": d["suspend_date"] and _fmt_dt(d["suspend_date"]),
           "resume": d["resume_date"] and _fmt_dt(d["resume_date"]),
           "expected_finish": d["expect_end_date"] and _fmt_dt(d["expect_end_date"])}

    def links(side: str) -> tuple[list, int]:
        lks = net.by_succ.get(n, []) if side == "pred" else net.by_pred.get(n, [])
        rows = []
        for lk in lks:
            p, s = net.t.get(lk["pred"]), net.t.get(lk["succ"])
            if p is None or s is None:
                continue
            o = p if side == "pred" else s
            lc = net.lag_cal(p, s)
            late = side == "succ"
            g_s, g_e = net.gap(lk, late), net_e.gap(lk, late)
            key = "drives" if side == "pred" else "controls"
            applied = net._lag(lk, p, lc if lc is not None and not lc.error else _ELAPSED)
            lhpd = net.lag_hpd(p, s)
            row = {("predecessor" if side == "pred" else "successor"): o["task_code"],
                   ("predecessor" if side == "pred" else "successor") + "_name": o["task_name"],
                   **({"other_project": o["project"]} if o["proj_id"] != net.pid else {}),
                   "type": REL_TYPES_REV.get(lk["type"], lk["type"]), "lag_d": _days(lk["lag"], lhpd),
                   "lag_calendar": lc.name if lc is not None else _t("24-hour (elapsed time)"),
                   f"{key}_stored": g_s is not None and abs(g_s) <= GAP_TOL_H,
                   f"{key}_engine": g_e is not None and abs(g_e) <= GAP_TOL_H}
            if abs(applied - lk["lag"]) > 1e-9:
                row["lag_applied_d"] = _days(applied, lhpd)
            rows.append(row)
        key = "drives" if side == "pred" else "controls"
        rows.sort(key=lambda r: (not (r[f"{key}_stored"] or r[f"{key}_engine"]), r.get("predecessor", r.get("successor"))))
        return rows[:CTX_LINKS], len(rows)
    out = {"activity": act}
    out["predecessors"], out["predecessor_count"] = links("pred")
    if set(bad) & {"ls", "lf", "tf_d"}:
        out["successors"], out["successor_count"] = links("succ")
    return out


def _project_context(cpm: _Cpm, ev: dict) -> dict:
    """Project inputs of every comparison row: dates and the scheduling options in force."""
    o, pc = cpm.opts, cpm.pcal
    ctype = cpm.prj["critical_path_type"]
    return {"data_date": cpm.prj["last_recalc_date"], "last_schedule_date": ev.get("last_schedule_date"),
            "must_finish_by": cpm.prj["plan_end_date"], "planned_start": cpm.prj["plan_start_date"],
            "calendar": {"name": pc.name if pc is not None else None, "hours_per_day": pc.hpd if pc is not None else None,
                         "work_week": _week_text(pc)},
            "scheduling": {"out_of_sequence": OOS_TEXT[cpm.oos],
                           "total_float": FLOAT_TEXT[cpm.ft],
                           "sched_use_project_end_date_for_float": o.get("sched_use_project_end_date_for_float"),
                           "critical": (_t("longest path") if ctype == "CT_DrivPath" else
                                        _t("total float <= {hours:g} h", hours=float(cpm.prj["critical_drtn_hr_cnt"] or 0))),
                           "open_ends_critical": cpm.open_crit,
                           "lag_calendar": LAG_CAL_TEXT.get(cpm.rule, cpm.rule),
                           "lag_from_progressed_predecessor": _t("reduced by the time elapsed since its actual date"),
                           "external_dates": _t("ignored (sched_outer_depend_type SD_None)") if cpm.ignore_ext else
                                             _t("used (stored dates of the linked activities of other projects)"),
                           "expected_finish": cpm.use_expect,
                           "multiple_float_paths": None if cpm.fp is None else {
                               "paths": cpm.fp["max"], "by": _t("total float") if cpm.fp["total"] else _t("free float"),
                               "end_activity": cpm.t[cpm.fp["end"]]["task_code"] if cpm.fp["end"] in cpm.t else None}}}


def _cpm_compare(cpm: _Cpm, res: dict, stale: bool, never: bool = False, ev: Optional[dict] = None) -> dict:
    """Engine results against the stored F9: rows of mismatches with a probable cause and its cause_class
    (_cpm_classify on the evidence ev of _stale_f9; without it only the edit log flag stale is known), match
    rates. Rows carry _tid (the task_id) for the caller."""
    fields = (("es", "early_start_date"), ("ef", "early_end_date"), ("ls", "late_start_date"), ("lf", "late_end_date"))
    T, mism, stats = cpm.t, {}, Counter()
    for n, d in T.items():
        done = d["status_code"] == "TK_Complete"
        bad = {}
        for k, col in fields:
            if done and k in ("ls", "lf"):
                continue  # late dates of completed activities: not used by float, reported apart
            a, b = res[n][k], d[col]
            if b is None or a is None or abs(a - b) > CPM_TOL:
                bad[k] = {"stored": b and _fmt_dt(b), "engine": a and _fmt_dt(a)}
        if not done:
            b, a = d["total_float_hr_cnt"], res[n]["tf_h"]
            if b is None or a is None or abs(float(b) - a) > CPM_TF_TOL_H:
                bad["tf_d"] = {"stored": _days(b, d["hpd"]), "engine": _days(a, d["hpd"])}
        grp = "completed" if done else "open"
        stats[grp] += 1
        if bad:
            stats[grp + "_bad"] += 1
            mism[n] = bad
        if done:
            for k, col in (("ls", "late_start_date"), ("lf", "late_end_date")):
                if res[n][k] is not None and d[col] is not None and abs(res[n][k] - d[col]) > CPM_TOL:
                    stats["completed_late_bad"] += 1
                    break
    why = _cpm_classify(cpm, mism, ev if ev is not None else _stale_min(stale), never) if mism else {}
    rows = []
    for n, bad in mism.items():
        d = T[n]
        rows.append({"task_code": d["task_code"], "task_name": d["task_name"], "task_type": d["task_type"],
                     "status_code": d["status_code"], "differences": bad,
                     "differences_text": _join("; ", (_t("{field}: stored {stored}, engine {engine}",
                                                         field=CPM_DIFF_LABELS[k], stored=v["stored"], engine=v["engine"])
                                                      for k, v in bad.items())),
                     **why[n], "_tid": n})
    rows.sort(key=lambda r: (r["status_code"] == "TK_Complete", r["task_code"]))
    rate = lambda g: None if not stats[g] else round(1 - stats[g + "_bad"] / stats[g], 4)  # noqa: E731
    cc = Counter(r["cause_class"] for r in rows)
    return {"rows": rows, "match_rate": rate("open"), "match_rate_completed": rate("completed"),
            "counts": {"open": stats["open"], "open_mismatched": stats["open_bad"], "completed": stats["completed"],
                       "completed_mismatched": stats["completed_bad"],
                       "completed_late_dates_differing": stats["completed_late_bad"]},
            "cause_counts": {c: cc[c] for c in CAUSE_CLASSES},
            "other_fields": _cpm_other_fields(cpm, res)}


def _cpm_other_fields(cpm: _Cpm, res: dict) -> dict:
    """Free float, longest path flag and float path of open activities, engine against the stored F9 (not part
    of match_rate): {field: {compared, mismatched, sample}}; float path only when multiple float paths are on."""
    checks = {"free_float": ("free_float_hr_cnt", "ff_h"), "longest_path": ("driving_path_flag", "longest")}
    if cpm.fp is not None:
        checks["float_path"] = ("float_path", "float_path")
    out = {}
    for name, (col, key) in checks.items():
        n_cmp, bad = 0, []
        for n, d in cpm.t.items():
            if d["status_code"] == "TK_Complete" or col not in d:
                continue
            a, b = res[n][key], d[col]
            n_cmp += 1
            if name == "free_float":
                same = a is not None and b is not None and abs(float(b) - a) <= CPM_TF_TOL_H
            elif name == "longest_path":
                same = (b == "Y") == bool(a)
            else:
                same = (None if b in (None, "") else int(float(b))) == a
            if not same:
                bad.append(d["task_code"])
        out[name] = {"compared": n_cmp, "mismatched": len(bad), "sample": sorted(bad)[:10]}
    return out


def _whatif_apply(cpm: _Cpm, e: Mapping, trace: Optional[list] = None) -> str:
    """Apply one edit to the in-memory network; returns its description. trace: gets one record of what changed
    (the assumption flags of p6_schedule_whatif read it)."""
    rec = (lambda **kw: trace.append(kw)) if trace is not None else (lambda **kw: None)  # noqa: E731
    op = e.get("op")
    if op not in get_args(WhatIfOp):
        raise ValueError(_t("op must be one of {ops}", ops=list(get_args(WhatIfOp))))
    act = lambda code: cpm.t[cpm.code[code]] if code in cpm.code else (_ for _ in ()).throw(  # noqa: E731
        ValueError(_t("Activity {code} not found in project {project} (what-if edits stay inside it; a fragnet "
                      "activity is added by an add_activity edit before its links)", code=code,
                      project=cpm.prj["proj_short_name"]) if getattr(cpm, "frag_ctx", None) else
                   _t("Activity {code} not found in project {project} (what-if edits stay inside it)", code=code,
                      project=cpm.prj["proj_short_name"])))
    if op == "add_activity":
        return _fragnet_add(cpm, e, rec)
    if op in ("add_link", "delete_link", "update_link"):
        if op != "add_link" and e.get("task_pred_id") is not None:
            hits = [lk for lk in cpm.links if lk["id"] == e["task_pred_id"]]
            if not hits:
                raise ValueError(_t("task_pred_id {task_pred_id} is not a link inside the project", task_pred_id=e['task_pred_id']))
        else:
            p, s = act(e.get("predecessor_code")), act(e.get("successor_code"))
            ty = e.get("current_type") if op != "add_link" else e.get("rel_type") or "FS"
            code = _rel_code(ty, "type") if ty else None
            hits = [lk for lk in cpm.links if lk["pred"] == p["task_id"] and lk["succ"] == s["task_id"]
                    and (code is None or lk["type"] == code)]
            if op == "add_link":
                if p["task_id"] == s["task_id"]:
                    raise ValueError(_t("An activity cannot be linked to itself."))
                if hits:
                    raise ValueError(_t("{pred} -> {succ} {type} already exists", pred=p["task_code"], succ=s["task_code"],
                                        type=ty))
                cyc = _link_cycle(cpm, p["task_id"], s["task_id"])
                if cyc:
                    raise ValueError(_t("{pred} -> {succ} would close a loop (P6 refuses to schedule it): {loop}",
                                        pred=p["task_code"], succ=s["task_code"],
                                        loop=" -> ".join(cpm.t[n]["task_code"] for n in cyc)))
                lag = _lag_arg(e.get("lag_days") or 0) * cpm.lag_hpd(p, s)
                cpm.links.append({"id": None, "succ": s["task_id"], "pred": p["task_id"], "type": code, "lag": lag})
                rec(op="add_link", link=cpm.links[-1], before=None)
                return _t("add {type} {pred} -> {succ} lag {lag:g} d", type=REL_TYPES_REV[code], pred=p["task_code"],
                          succ=s["task_code"], lag=e.get("lag_days") or 0)
            if not hits:
                raise ValueError(_t("No link {pred} -> {succ} {type}", pred=p["task_code"], succ=s["task_code"], type=ty)
                                 if ty else _t("No link {pred} -> {succ}", pred=p["task_code"], succ=s["task_code"]))
            if len(hits) > 1:
                raise ValueError(_t("Several links {pred} -> {succ}: give current_type", pred=p["task_code"],
                                    succ=s["task_code"]))
        lk = hits[0]
        p, s = cpm.t[lk["pred"]], cpm.t[lk["succ"]]
        txt = f"{REL_TYPES_REV.get(lk['type'])} {p['task_code']} -> {s['task_code']}"
        if op == "delete_link":
            cpm.links.remove(lk)
            rec(op="delete_link", link=dict(lk), before=dict(lk))
            return _t("delete {link}", link=txt)
        ch, old = [], dict(lk)
        if e.get("rel_type"):
            new = _rel_code(e["rel_type"], "rel_type")
            if new != lk["type"] and any(x is not lk and x["pred"] == lk["pred"] and x["succ"] == lk["succ"] and x["type"] == new
                                         for x in cpm.links):
                raise ValueError(_t("A {type} link already joins {pred} -> {succ}",
                                    type=e["rel_type"], pred=p["task_code"], succ=s["task_code"]))
            lk["type"] = new
            ch.append(_t("type {type}", type=e["rel_type"]))
        if e.get("lag_days") is not None:
            lk["lag"] = _lag_arg(e["lag_days"]) * cpm.lag_hpd(p, s)
            ch.append(_t("lag {lag:g} d", lag=e["lag_days"]))
        if not ch:
            raise ValueError(_t("update_link: give rel_type and/or lag_days"))
        rec(op="update_link", link=lk, before=old)
        return _t("update {link}: {changes}", link=txt, changes=_join(", ", ch))
    d = act(e.get("task_code"))
    if op == "duration":
        ch, rem0 = [], d["rem"]
        given = [k for k in ("remaining_days", "original_days", "add_remaining_days") if e.get(k) is not None]
        if "add_remaining_days" in given and len(given) > 1:
            raise ValueError(_t("add_remaining_days cannot be combined with remaining_days or original_days"))
        for k in given:
            v = _num(e[k])
            if v is None or (v < 0 and k != "add_remaining_days"):
                raise ValueError(_t("{key} must be 0 or more", key=k) if k != "add_remaining_days" else
                                 _t("{key} must be a number", key=k))
        if e.get("add_remaining_days") is not None:
            hpd = d["hpd"] or 8.0
            old = d["rem"] or 0.0
            new = old + float(e["add_remaining_days"]) * hpd
            if new < 0:
                raise ValueError(_t("add_remaining_days {add:g}: the remaining duration of {code} is {old:g} d",
                                    add=e["add_remaining_days"], code=d["task_code"], old=old / hpd))
            d["rem"] = new
            ch.append(_t("remaining {old:g} d {add:+g} d = {new:g} d", old=old / hpd,
                         add=float(e["add_remaining_days"]), new=new / hpd))
        elif e.get("remaining_days") is not None:
            d["rem"] = float(e["remaining_days"]) * (d["hpd"] or 8.0)
            ch.append(_t("remaining {days:g} d", days=e["remaining_days"]))
        elif e.get("original_days") is not None and d["status_code"] == "TK_NotStart":
            d["rem"] = float(e["original_days"]) * (d["hpd"] or 8.0)  # not started: remaining follows the original
            ch.append(_t("original {days:g} d (remaining follows)", days=e["original_days"]))
        if not ch:
            raise ValueError(_t("duration: give remaining_days, add_remaining_days (or original_days for a not "
                                "started activity)"))
        fixed = cpm.duration_fixed(d)
        if fixed:
            ch.append(_t("no effect on its dates ({why})", why=fixed))
        rec(op="duration", task_id=d["task_id"], rem_before=rem0, rem_after=d["rem"], fixed=fixed,
            original=e.get("original_days") is not None and "remaining_days" not in given
            and "add_remaining_days" not in given)
        return _t("{code}: {changes}", code=d["task_code"], changes=_join(", ", ch))
    code, dt = _cstr_arg(d["cal"], e)
    rec(op="constraint", task_id=d["task_id"], before=(d["cstr_type"], d["cstr_date"]), after=(code, dt))
    d["cstr_type"], d["cstr_date"] = code, dt
    if not code:
        return _t("{code}: constraint none", code=d["task_code"])
    if dt:
        return _t("{code}: constraint {label} {date}", code=d["task_code"], label=_cstr_label(code), date=_fmt_dt(dt))
    return _t("{code}: constraint {label}", code=d["task_code"], label=_cstr_label(code))


def _cstr_arg(cal, e: Mapping) -> tuple[Optional[str], Optional[datetime]]:
    """(constraint code, date) of an edit's constraint and constraint_date, the date resolved on cal."""
    name = e.get("constraint")
    if name not in CSTR_TYPES:
        raise ValueError(_t("constraint must be one of {CSTR_TYPES}", CSTR_TYPES=list(CSTR_TYPES)))
    code = CSTR_TYPES[name]
    dt = None
    if code and code != "CS_ALAP":
        if not e.get("constraint_date"):
            raise ValueError(_t("constraint_date is required for this constraint"))
        dt = _db_dt(_resolve_dt(cal if cal and not cal.error else None, e["constraint_date"], "constraint_date",
                                "finish" if code in CSTR_FINISH else "start")[0])
    return code, dt


def _link_cycle(cpm: _Cpm, pred: int, succ: int) -> list[int]:
    """The loop a new link pred -> succ would close: [pred, succ, ..., pred] (task_ids, the shortest path back
    from succ to pred along the links), else []."""
    by_p: dict = {}
    for lk in cpm.links:
        by_p.setdefault(lk["pred"], []).append(lk["succ"])
    back, todo, k = {succ: None}, [succ], 0
    while k < len(todo):
        n = todo[k]
        k += 1
        if n == pred:
            path = []
            while n is not None:
                path.append(n)
                n = back[n]
            return [pred] + path[::-1]
        for m in sorted(by_p.get(n, ())):
            if m not in back:
                back[m] = n
                todo.append(m)
    return []


FRAGNET_TYPES = get_args(FragnetType)
FRAGNET_PATH_CAP = 100  # rows of the fragnet's driving path into the project finish
_FRAG_NONE = ("early_start_date", "early_end_date", "late_start_date", "late_end_date", "act_start_date",
              "act_end_date", "total_float_hr_cnt", "cstr_type2", "cstr_date2", "expect_end_date", "suspend_date",
              "resume_date", "free_float_hr_cnt", "driving_path_flag", "float_path", "float_path_order", "update_date")


def _fragnet_ctx(con, pid: int) -> dict:
    """What add_activity edits read from the database (the what-if's snapshot): the first task_id above every
    TASK row (soft-deleted ones included: the temp tables of the DCMA block hold fragnet rows beside the real
    ones), the project's WBS nodes (wbs_code, project node first), the calendars a project activity may use
    (global and this project's, resource calendars left out) with their hours per day, the project calendar."""
    top = con.execute("SELECT MAX(task_id) FROM TASK").fetchone()[0]
    ccols = _cols(con, "CALENDAR")
    typ = "c.clndr_type" if "clndr_type" in ccols else "NULL"
    own = "c.proj_id" if "proj_id" in ccols else "NULL"
    cals = [{"clndr_id": r[0], "name": r[1], "type": r[2], "proj_id": r[3], "hpd": float(r[4])} for r in con.execute(
        f"SELECT c.clndr_id, c.clndr_name, {typ}, {own}, {HPD_SQL} FROM CALENDAR c WHERE 1 = 1"
        + _alive(con, "CALENDAR", "c") + " ORDER BY c.clndr_id")]
    cals = [c for c in cals if c["type"] != "CA_Rsrc" and (c["type"] != "CA_Project" or c["proj_id"] in (None, pid))]
    pcal = con.execute("SELECT clndr_id FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone()[0]
    hpd = next((c["hpd"] for c in cals if c["clndr_id"] == pcal), 8.0)
    return {"next_id": int(top or 0) + 1, "wbs": _wbs_tree(con, pid), "sep": _name_sep(con, pid), "cals": cals,
            "pcal": pcal, "pcal_hpd": hpd, "added": []}


def _fragnet_add(cpm: _Cpm, e: Mapping, rec: Callable) -> str:
    """add_activity: a fragnet activity in cpm (never written): not started, its original and remaining duration in
    days of its calendar, under the project node or the WBS given."""
    ctx = getattr(cpm, "frag_ctx", None)
    if ctx is None:
        raise ValueError(_t("add_activity needs the what-if context (p6_schedule_whatif)"))
    code = e.get("task_code")
    if not isinstance(code, str) or not code.strip() or code != code.strip():
        raise ValueError(_t("task_code: a new Activity ID (text, no leading or trailing spaces)"))
    if code in cpm.code:
        raise ValueError(_t("Activity ID {code} is already a fragnet activity of these edits", code=code)
                         if cpm.t[cpm.code[code]].get("fragnet") else
                         _t("Activity ID {code} already exists in project {project}", code=code,
                            project=cpm.prj["proj_short_name"]))
    name = e.get("task_name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(_t("task_name is required"))
    ty = e.get("task_type") or "TT_Task"
    if ty not in FRAGNET_TYPES:
        raise ValueError(_t("task_type must be one of {FRAGNET_TYPES}", FRAGNET_TYPES=list(FRAGNET_TYPES)))
    for k in ("remaining_days", "add_remaining_days"):
        if e.get(k) is not None:
            raise ValueError(_t("add_activity takes original_days, not {key} (a fragnet activity is not started)", key=k))
    if ty == "TT_Task" and e.get("original_days") is None:
        raise ValueError(_t("original_days is required for a TT_Task activity"))
    days = _num(e.get("original_days") if e.get("original_days") is not None else 0)
    if days is None or days < 0:
        raise ValueError(_t("original_days must be 0 or more"))
    if ty != "TT_Task" and days:
        raise ValueError(_t("a milestone has no duration: original_days 0 or left out"))
    cal_id, hpd = ctx["pcal"], ctx["pcal_hpd"]
    if e.get("calendar") is not None:
        want = str(e["calendar"]).strip()
        hits = [c for c in ctx["cals"] if c["name"] == want] or \
            [c for c in ctx["cals"] if (c["name"] or "").casefold() == want.casefold()]
        if not hits:
            raise ValueError(_t("calendar {name!r} not found (global or project calendars). Calendars: {calendars}",
                                name=want, calendars=sorted({c["name"] for c in ctx["cals"] if c["name"]})[:15]))
        if len(hits) > 1:
            raise ValueError(_t("calendar {name!r} names several calendars (clndr_id {ids})", name=want,
                                ids=[c["clndr_id"] for c in hits]))
        cal_id, hpd = hits[0]["clndr_id"], hits[0]["hpd"]
    nodes = ctx["wbs"]
    if not nodes:
        raise ValueError(_t("the project has no WBS node"))
    node = nodes[0]
    if e.get("wbs"):
        w, top, sep = str(e["wbs"]).strip(), nodes[0]["wbs_code"], ctx["sep"]
        node = next((n for n in nodes if n["wbs_code"] in (w, top + sep + w)), None)
        if node is None:
            raise ValueError(_t("WBS code {code!r} not found in {project}. Codes: {codes}",
                                code=w, project=cpm.prj["proj_short_name"], codes=[n["wbs_code"] for n in nodes[1:16]]))
    cal = cpm.cals.get(cal_id)
    cstr, cdt = _cstr_arg(cal, e) if e.get("constraint") not in (None, "none") else (None, None)
    tid = ctx["next_id"]
    ctx["next_id"] += 1
    h = float(days) * hpd
    d = dict.fromkeys(_FRAG_NONE)
    d.update(task_id=tid, task_code=code, task_name=name.strip(), proj_id=cpm.pid, wbs_id=node["wbs_id"],
             task_type=ty, status_code="TK_NotStart", clndr_id=cal_id, remain_drtn_hr_cnt=h, target_drtn_hr_cnt=h,
             cstr_type=cstr, cstr_date=cdt, hpd=hpd, bad_dates=[], cal=cal, rem=h,
             wbs=node["wbs_id"] if node["wbs_id"] in cpm.wbs_parent else None, fragnet=True,
             wbs_code=node["wbs_code"], calendar=cal.name if cal is not None else None)
    cpm.t[tid] = d
    cpm.code[code] = tid
    ctx["added"].append(tid)
    rec(op="add_activity", task_id=tid)
    if cstr:
        rec(op="constraint", task_id=tid, before=(None, None), after=(cstr, cdt))
    if cstr and cdt:
        return _t("add activity {code} ({type}, {days:g} d, WBS {wbs}), constraint {label} {date}", code=code, type=ty,
                  days=days, wbs=node["wbs_code"], label=_cstr_label(cstr), date=_fmt_dt(cdt))
    if cstr:
        return _t("add activity {code} ({type}, {days:g} d, WBS {wbs}), constraint {label}", code=code, type=ty,
                  days=days, wbs=node["wbs_code"], label=_cstr_label(cstr))
    return _t("add activity {code} ({type}, {days:g} d, WBS {wbs})", code=code, type=ty, days=days, wbs=node["wbs_code"])

WHATIF_CAVEAT = (_t("The engine calculates these dates the way P6 would. It does not check that crews, materials or "
                    "access allow them."))
WHATIF_CUT_PCT = 30.0  # default max_cut_pct of p6_schedule_whatif


def _temp_network(con, pid: int, cpm: _Cpm, res: Optional[dict], bid: Optional[int]) -> None:
    """Shadow TASK, TASKPRED and PROJECT on this read-only connection with temp copies (an unqualified name
    resolves to the temp schema first) holding cpm's network and the engine results res: what _dcma and _cpt
    read then is the in-memory network, and the database file is never written. TASK keeps the project, its
    baseline and the activities linked to it, plus the fragnet activities of cpm (add_activity, task_ids above
    every TASK row); TASKPRED the links touching the project."""
    al = functools.partial(_alive, con)
    con.execute("CREATE TEMP TABLE TASK AS SELECT * FROM main.TASK WHERE proj_id IN (?, ?) OR task_id IN ("
                "SELECT r.pred_task_id FROM main.TASKPRED r JOIN main.TASK s ON s.task_id = r.task_id WHERE s.proj_id = ?) "
                "OR task_id IN (SELECT r.task_id FROM main.TASKPRED r JOIN main.TASK p ON p.task_id = r.pred_task_id "
                "WHERE p.proj_id = ?)", (pid, bid if bid is not None else pid, pid, pid))
    con.execute("CREATE TEMP TABLE TASKPRED AS SELECT * FROM main.TASKPRED WHERE task_id IN (SELECT task_id FROM temp.TASK "
                "WHERE proj_id = ?) OR pred_task_id IN (SELECT task_id FROM temp.TASK WHERE proj_id = ?)", (pid, pid))
    con.execute("CREATE TEMP TABLE PROJECT AS SELECT * FROM main.PROJECT")
    for i, (t, c) in enumerate((("TASK", "task_id"), ("TASK", "proj_id"), ("TASK", "task_code"), ("TASKPRED", "task_id"),
                                ("TASKPRED", "pred_task_id"), ("TASKPRED", "task_pred_id"), ("PROJECT", "proj_id"))):
        con.execute(f"CREATE INDEX temp.wi_{i} ON {t} ({c})")
    tcols = _cols(con, "TASK")
    for d in cpm.t.values():
        if d.get("fragnet"):  # a fragnet activity of the what-if: a new row (task_id above every TASK row)
            row = {k: v for k, v in (("task_id", d["task_id"]), ("proj_id", pid), ("wbs_id", d["wbs_id"]),
                                     ("clndr_id", d["clndr_id"]), ("task_code", d["task_code"]),
                                     ("task_name", d["task_name"]), ("task_type", d["task_type"]),
                                     ("status_code", d["status_code"]), ("phys_complete_pct", 0.0))
                   if k in tcols}
            con.execute(f"INSERT INTO TASK ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    base_ids = {r[0] for r in con.execute("SELECT r.task_pred_id FROM TASKPRED r JOIN TASK s ON s.task_id = r.task_id "
                                          "JOIN TASK p ON p.task_id = r.pred_task_id WHERE s.proj_id = ? AND p.proj_id = ?"
                                          + al("TASKPRED", "r"), (pid, pid))}
    keep = {lk["id"]: lk for lk in cpm.links if lk["id"] is not None}
    con.executemany("DELETE FROM TASKPRED WHERE task_pred_id = ?", [(i,) for i in base_ids - set(keep)])
    con.executemany("UPDATE TASKPRED SET pred_type = ?, lag_hr_cnt = ? WHERE task_pred_id = ?",
                    [(lk["type"], lk["lag"], i) for i, lk in keep.items()])
    new = [lk for lk in cpm.links if lk["id"] is None]
    for k, lk in enumerate(new, 1):
        con.execute("INSERT INTO TASKPRED (task_pred_id, task_id, pred_task_id, proj_id, pred_proj_id, pred_type, lag_hr_cnt) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (-k, lk["succ"], lk["pred"], pid, pid, lk["type"], lk["lag"]))
    sets = ["remain_drtn_hr_cnt", "cstr_type", "cstr_date"] + (["target_drtn_hr_cnt"] if "target_drtn_hr_cnt" in tcols else [])
    dated = [("early_start_date", "es"), ("early_end_date", "ef"), ("late_start_date", "ls"), ("late_end_date", "lf"),
             ("total_float_hr_cnt", "tf_h"), ("free_float_hr_cnt", "ff_h"), ("driving_path_flag", "longest"),
             ("float_path", "float_path"), ("float_path_order", "float_path_order")]
    dated = [x for x in dated if x[0] in tcols] if res is not None else []
    plain, full = [], sets + [c for c, _ in dated]
    rich = []
    for n, d in cpm.t.items():
        vals = [d["rem"], d["cstr_type"], d["cstr_date"] and _fmt_dt(d["cstr_date"])]
        if "target_drtn_hr_cnt" in sets:
            vals.append(d.get("target_drtn_hr_cnt"))
        if dated and d["status_code"] != "TK_Complete":
            r = res[n]
            if "rem_h" in r:
                vals[0] = r["rem_h"]
            for _col, k in dated:
                v = r.get(k)
                vals.append(_fmt_dt(v) if isinstance(v, datetime) else ("Y" if v else "N") if k == "longest" else v)
            rich.append((*vals, n))
        else:
            plain.append((*vals, n))
    con.executemany(f"UPDATE TASK SET {', '.join(f'{c} = ?' for c in full)} WHERE task_id = ?", rich)
    con.executemany(f"UPDATE TASK SET {', '.join(f'{c} = ?' for c in sets)} WHERE task_id = ?", plain)
    if res is not None:
        con.execute("UPDATE PROJECT SET scd_end_date = ? WHERE proj_id = ?", (_fmt_dt(cpm.finish(res)[1]), pid))


def _cpm_reloaded(con, cpm: _Cpm, res: dict) -> _Cpm:
    """The network _Cpm(con, pid) reads back from _temp_network(con, pid, cpm, res), built in memory: open
    activities with the remaining duration F9 rewrites (rem_h) and the engine's dates, float and paths as their
    stored values, constraint dates at whole seconds, links in task_pred_id order (new links -1, -2...)."""
    tcols = _cols(con, "TASK")
    net = cpm.copy()
    dated = [(c, k) for c, k in (("early_start_date", "es"), ("early_end_date", "ef"), ("late_start_date", "ls"),
                                 ("late_end_date", "lf"), ("total_float_hr_cnt", "tf_h"), ("free_float_hr_cnt", "ff_h"),
                                 ("driving_path_flag", "longest"), ("float_path", "float_path"),
                                 ("float_path_order", "float_path_order")) if c in tcols]
    sec = lambda v: _db_dt(_fmt_dt(v)) if isinstance(v, datetime) else v  # noqa: E731
    for n, d in net.t.items():
        d["cstr_date"] = sec(d["cstr_date"])
        if d["status_code"] == "TK_Complete":
            continue
        r = res[n]
        if "rem_h" in r:
            d["rem"] = d["remain_drtn_hr_cnt"] = r["rem_h"]
        for c, k in dated:
            v = r.get(k)
            d[c] = sec(v) if isinstance(v, datetime) else ("Y" if v else "N") if k == "longest" else v
    for k, lk in enumerate((lk for lk in net.links if lk["id"] is None), 1):
        lk["id"] = -k
    net.links.sort(key=lambda lk: lk["id"])
    return net


def _dcma_memory(pid: int, cpm: Optional[_Cpm] = None, res: Optional[dict] = None,
                 stored: Optional[tuple] = None) -> tuple[dict, str]:
    """(checks, threshold source) of the DCMA assessment as p6_health_check runs it (project settings, the project
    baseline, current dates, remaining durations; key_dates left out; no snapshot): on the stored database, or
    with cpm and res on that network with the engine's dates in temp tables (read-only connection). The critical
    path test reuses what the caller computed instead of loading and running the engine again: stored =
    (cpm, res, match_rate) of the stored database; with cpm and res, the network as the temp tables hold it
    (_cpm_reloaded) and res, its own F9."""
    con = _connect()
    con.execute("BEGIN")
    try:
        cfg = _dcma_settings(con, pid, {}, None)
        bid = _baseline_pid(con, pid, None)
        net = stored
        if cpm is not None:
            qo = con.execute("PRAGMA query_only").fetchone()[0]
            con.execute("PRAGMA query_only = OFF")  # temp tables only: mode=ro keeps the database file read only
            try:
                _temp_network(con, pid, cpm, res, bid)
            finally:
                con.execute(f"PRAGMA query_only = {'ON' if qo else 'OFF'}")
            if res is not None:
                back = _cpm_reloaded(con, cpm, res)
                net = (back, res, _cpm_compare(back, res, False)["match_rate"])
        checks = _dcma(con, pid, bid, cfg, "current", "remaining")[0]
        if "critical_path_test" not in cfg["disabled"]:
            checks["critical_path_test"] = _cpt(con, pid, net)
    finally:
        con.rollback()  # drops the temp tables too
    return checks, cfg["source"]


def _dcma_brief(checks: Mapping) -> dict:
    return {k: {f: v.get(f) for f in ("count", "pct", "value", "threshold", "pass")} for k, v in checks.items()}


def _whatif_assumptions(base: _Cpm, trial: _Cpm, before: dict, r1: dict, trace: list, crit_b: Callable,
                        crit_a: Callable, opn: list, max_cut_pct: float, near_days: float, near_h0: float,
                        ties0: tuple, ties1: tuple) -> dict:
    """What a reviewer would question in the edits (flags only): duration cuts above max_cut_pct of the original
    or remaining duration, open or dangling ends left by deleted or retyped links, new hard constraints, negative
    lags, SS links with zero lag, activities that became near critical, links driving the finish."""
    T, A = base.t, trial.t
    code = lambda n: {"task_code": A[n]["task_code"], "task_name": A[n]["task_name"]}  # noqa: E731
    cuts, ends, hard, neg, ss0, added = [], [], [], [], [], []
    for t in trace:
        if t["op"] == "add_activity":
            added.append(t)
        elif t["op"] == "duration":
            d = A[t["task_id"]]
            hpd = d["hpd"] or 8.0
            orig = float(d.get("target_drtn_hr_cnt") or 0)
            old = orig if t["original"] else t["rem_before"]
            cut = old - t["rem_after"]
            p_rem = None if t["original"] or t["rem_before"] <= 0 else round(100 * cut / t["rem_before"], 1)
            p_org = None if orig <= 0 else round(100 * cut / orig, 1)
            if cut > 0 and max(p_rem or 0, p_org or 0) > max_cut_pct:
                cuts.append({**code(t["task_id"]), "edit": t["edit"], "remaining_before_d": _days(t["rem_before"], hpd),
                             "remaining_after_d": _days(t["rem_after"], hpd), "original_d": _days(orig, hpd),
                             "cut_d": _days(cut, hpd), "cut_pct_of_remaining": p_rem, "cut_pct_of_original": p_org,
                             "no_effect": t["fixed"]})
        elif t["op"] == "constraint":
            (b, bd), (a, ad) = t["before"], t["after"]
            if a in HARD_CSTR_SET and (a, ad) != (b, bd):
                hard.append({**code(t["task_id"]), "edit": t["edit"], "constraint": _cstr_label(a),
                             "constraint_date": ad and _fmt_dt(ad), "before": _cstr_label(b) if b else None})
        else:
            lk, old = t["link"], t["before"]
            p, s = A[lk["pred"]], A[lk["succ"]]
            view = {"predecessor": p["task_code"], "predecessor_name": p["task_name"], "successor": s["task_code"],
                    "successor_name": s["task_name"], "edit": t["edit"]}
            if t["op"] != "delete_link":
                same = old is not None and (old["type"], old["lag"]) == (lk["type"], lk["lag"])
                if lk["lag"] < 0 and not same:
                    neg.append({**view, "type": REL_TYPES_REV[lk["type"]], "lag_d": _days(lk["lag"], trial.lag_hpd(p, s))})
                if lk["type"] == "PR_SS" and lk["lag"] == 0 and not same:
                    sa, pa = r1[lk["succ"]], r1[lk["pred"]]
                    cal = base._time(p)
                    lo_, hi_ = max(sa["es"], pa["es"]), min(sa["ef"], pa["ef"])  # both at work
                    ov = cal.work_hours(lo_, hi_) if hi_ > lo_ else 0.0
                    ss0.append({**view, "starts_together": abs(sa["es"] - pa["es"]) <= CPM_TOL,
                                "overlap_d": _days(ov, p["hpd"] or 8.0)})
            if t["op"] == "delete_link" or (old is not None and old["type"] != lk["type"]):
                ends.append((old, view))
    has = lambda n, side, kinds=None: any(  # noqa: E731
        lk[side] == n and (kinds is None or kinds(lk["type"])) for lk in trial.links) or any(
        lk[side] == n for lk in (trial.ext_succ if side == "pred" else trial.ext_pred) if kinds is None)
    had = lambda n, side, kinds=None: any(  # noqa: E731
        lk[side] == n and (kinds is None or kinds(lk["type"])) for lk in base.links)
    is_open = lambda n: A[n]["status_code"] != "TK_Complete" and A[n]["task_type"] not in ("TT_LOE", "TT_WBS")  # noqa: E731
    mile = ("TT_Mile", "TT_FinMile")
    open_ends, seen = [], set()
    for old, view in ends:
        for n, side, words, key in ((old["pred"], "pred", ("no successor", "no relationship from its finish"), "f"),
                                    (old["succ"], "succ", ("no predecessor", "no relationship to its start"), "s")):
            if not is_open(n):
                continue
            kind = (lambda ty: LINK_FROM[ty] == "f") if key == "f" else (lambda ty: LINK_TO[ty] == "s")
            why = None
            if had(n, side) and not has(n, side):
                why = words[0]
            elif A[n]["task_type"] not in mile and had(n, side, kind) and not has(n, side, kind):
                why = words[1]
            if why and (n, why) not in seen:
                seen.add((n, why))
                open_ends.append({**code(n), "left_with": why, "edit": view["edit"]})
    for t in added:  # a fragnet activity is a new activity: its open ends are flagged as the ones a deleted link leaves
        n = t["task_id"]
        for side, why in (("succ", "no predecessor"), ("pred", "no successor")):
            if not has(n, side):
                open_ends.append({**code(n), "left_with": why, "edit": t["edit"]})
    hpd = lambda n: T[n]["hpd"] or 8.0  # noqa: E731
    near = lambda tf, n: tf is not None and tf <= near_h0 + near_days * hpd(n) + CPM_TF_TOL_H  # noqa: E731
    rows = sorted(({**code(n), "total_float_d_before": _days(before[n]["tf_h"], hpd(n)),
                    "total_float_d_after": _days(r1[n]["tf_h"], hpd(n))} for n in opn
                   if near(r1[n]["tf_h"], n) and not crit_a(n) and not near(before[n]["tf_h"], n) and not crit_b(n)),
                  key=lambda r: (r["total_float_d_after"] if r["total_float_d_after"] is not None else 0, r["task_code"]))
    conv = lambda ties: {"driving_links": len(ties), "predecessors": sorted({A[p]["task_code"] for p, _ in ties})[:CTX_LINKS],  # noqa: E731
                         "finish_activities": sorted({A[f]["task_code"] for _, f in ties})[:CTX_LINKS]}  # those entered
    flags = {"duration_cuts": cuts, "open_ends": open_ends, "hard_constraints": hard, "negative_lags": neg,
             "ss_zero_lag": ss0}
    return {**flags, "near_critical": {"definition": _t("total float within {near_days:g} d of the critical limit "
                                                        "(days of each activity's calendar), not critical",
                                                        near_days=near_days),
                                       "became_near_critical": len(rows), "rows": rows[:ACT_LINKS_CAP]},
            "finish_convergence": {"before": conv(ties0), "after": conv(ties1),
                                   "definition": _t("links from other activities that drive the early finish of the "
                                                    "activities finishing on the project finish (engine): the paths "
                                                    "that converge on it")},
            "flag_count": sum(len(v) for v in flags.values()) + len(rows), "max_cut_pct": max_cut_pct,
            "note": _t("Flags only: none of them stops the run. They mark what a reviewer would question.")}


def _fragnet_impact(base: _Cpm, trial: _Cpm, before: dict, fin_before: Optional[datetime], r1: dict, d1: dict,
                    crit_a: Callable, milestones: Optional[list], basis: str) -> dict:
    """The fragnet block of p6_schedule_whatif (time impact analysis): the fragnet activities with the engine's dates
    and float, the delay of the project finish and of each milestone in working days of the project calendar, and
    the driving path from the fragnet into the project finish (ties() of the forward pass)."""
    T, A = base.t, trial.t
    frag = list(trial.frag_ctx["added"])
    pcal = trial.pcal if trial.pcal is not None and not trial.pcal.error else None
    cd = lambda a, b: None if a is None or b is None else round((b - a).total_seconds() / 86400, 2) + 0.0  # noqa: E731
    wd = lambda a, b: None if a is None or b is None or pcal is None else round(pcal.work_days(a, b), 2) + 0.0  # noqa: E731
    fmt = lambda v: v and _fmt_dt(v)  # noqa: E731
    fm, fin_after = trial.finish(r1)
    # driving path: every activity whose link sets the dates of an activity on the project finish, walked back, then
    # the part of it a fragnet activity starts
    ends = [n for n, d in A.items() if d["task_type"] not in ("TT_LOE", "TT_WBS") and r1[n]["ef"] >= fin_after - CPM_TOL]
    ties: dict = {}
    stack = list(ends)
    while stack:
        n = stack.pop()
        if n not in ties:
            ties[n] = tuple(dict.fromkeys(trial.ties(d1.get(n))))
            stack.extend(ties[n])
    kids: dict = {}
    for n, ps in ties.items():
        for p in ps:
            kids.setdefault(p, []).append(n)
    reach, stack = set(), [n for n in frag if n in ties]
    while stack:
        n = stack.pop()
        if n not in reach:
            reach.add(n)
            stack.extend(kids.get(n, ()))
    rank = {n: i for i, n in enumerate(trial._plan_cache()["order"])}
    codes = lambda ns: [A[n]["task_code"] for n in ns]  # noqa: E731
    path = [{"task_code": A[n]["task_code"], "task_name": A[n]["task_name"], "fragnet": bool(A[n].get("fragnet")),
             "early_start": fmt(r1[n]["es"]), "early_finish": fmt(r1[n]["ef"]),
             "total_float_d": _days(r1[n]["tf_h"], A[n]["hpd"]), "driven_by": codes(ties[n]),
             "on_project_finish": n in ends} for n in sorted(reach, key=lambda n: rank.get(n, 0))]
    by = lambda side, n: sorted(A[lk[side]]["task_code"] for lk in trial.links  # noqa: E731
                                if lk["succ" if side == "pred" else "pred"] == n)
    acts = []
    for n in frag:
        d, r = A[n], r1[n]
        acts.append({"task_code": d["task_code"], "task_name": d["task_name"], "task_type": d["task_type"],
                     "wbs_code": d["wbs_code"], "calendar": d["calendar"], "original_d": _days(d["rem"], d["hpd"]),
                     "constraint": _cstr_label(d["cstr_type"]), "constraint_date": fmt(d["cstr_date"]),
                     "early_start": fmt(r["es"]), "early_finish": fmt(r["ef"]), "late_start": fmt(r["ls"]),
                     "late_finish": fmt(r["lf"]), "total_float_d": _days(r["tf_h"], d["hpd"]),
                     "free_float_d": _days(r.get("ff_h"), d["hpd"]), "critical": bool(crit_a(n)),
                     "longest_path": bool(r.get("longest")), "predecessors": by("pred", n),
                     "successors": by("succ", n), "driven_by": codes(trial.ties(d1.get(n))),
                     "drives_finish": n in reach})
    if milestones:
        keys = [base.code[c] for c in dict.fromkeys(milestones)]
    else:
        keys = [n for n, d in T.items() if d["task_type"] in ("TT_Mile", "TT_FinMile") and d["status_code"] != "TK_Complete"]
    ms = []
    for n in keys:
        k = "es" if T[n]["task_type"] == "TT_Mile" else "ef"
        b, a = before[n][k], r1[n][k]
        ms.append({"task_code": T[n]["task_code"], "task_name": T[n]["task_name"], "task_type": T[n]["task_type"],
                   "date_before": fmt(b), "date_after": fmt(a), "delay_cd": cd(b, a), "delay_wd": wd(b, a),
                   "delayed": b is not None and a - b > CPM_TOL})
    ms.sort(key=lambda m: (-(m["delay_wd"] if m["delay_wd"] is not None else m["delay_cd"] or 0), m["date_after"] or "",
                           m["task_code"]))
    return {
        "activities": acts,
        "impact": {"project_finish": {"before": fmt(fin_before), "after": fmt(fin_after), "delay_cd": cd(fin_before, fin_after),
                                      "delay_wd": wd(fin_before, fin_after), "finish_activity": fm and fm["task_code"]},
                   "milestones": ms[:ACT_LINKS_CAP], "milestones_total": len(ms),
                   "milestones_delayed": sum(m["delayed"] for m in ms),
                   "calendar": pcal.name if pcal is not None else None,
                   "definition": _t("delay = after - before; delay_wd in working days of the project calendar (null "
                                    "when it is unreadable); milestones: the ones given, else every open milestone "
                                    "(start of a start milestone, else finish), most delayed first; before = {basis}",
                                    basis=basis)},
        "driving_path": {"drives_finish": bool(reach), "count": len(path), "rows": path[:FRAGNET_PATH_CAP],
                         "definition": _t("activities whose relationship sets the early dates of the next one, from a "
                                          "fragnet activity to the activities on the project finish (engine forward "
                                          "pass), in network order; driven_by lists every predecessor that sets the "
                                          "activity's dates")},
        "note": _t("Fragnet activities exist only in this calculation: nothing is written. They are new activities "
                   "for the assumptions and DCMA blocks; float_change, longest_path_change and the changed rows cover "
                   "the existing activities only."),
    }


def _finish_ties(cpm: _Cpm, res: dict, drv: dict) -> tuple:
    """(predecessor, finish activity) pairs whose link drives an activity finishing on the project finish, from an
    activity that does not (a chain of activities all on the finish is one path)."""
    end = cpm.finish(res)[1]
    fins = [n for n, d in cpm.t.items() if d["task_type"] not in ("TT_LOE", "TT_WBS") and res[n]["ef"] >= end - CPM_TOL]
    return tuple((p, f) for f in sorted(fins) for p in dict.fromkeys(cpm.ties(drv.get(f))) if p not in fins)


def _cpt(con, pid: int, net: Optional[tuple] = None) -> dict:
    """DCMA critical path test with the engine: delay the earliest most critical open activity by 100
    working days and check that the project finish moves as much (90 % or more of its own shift).
    net: (cpm, res, match_rate) of the project's network as con holds it, already loaded and run by the
    caller (res = cpm.run(), match_rate of res against cpm's stored dates); else loaded and run here.
    Network cache (core/netcache.py): the test of the network as con holds it is kept while the database is
    unchanged; without net it is computed once (from the cached network and base run), with net a kept one is
    returned, else it is computed from net and not kept (net may come from another snapshot). A connection with
    temp tables (the what-if's edited network) never uses the cache."""
    tk = _ticket(con, pid)
    if tk is None:
        return _cpt_calc(con, pid, net, None)
    if net is None:
        return copy.deepcopy(_cached(tk, "cpt", lambda: _cpt_calc(con, pid, None, tk)))
    kept = _peek(tk, "cpt")
    return copy.deepcopy(kept) if kept is not None else _cpt_calc(con, pid, net, None)


def _cpt_calc(con, pid: int, net: Optional[tuple], tk: Optional[_Ticket]) -> dict:
    """_cpt itself: net given, or the network loaded and run here (through the cache with a ticket)."""
    out = {"count": None, "denominator": None, "pct": None, "threshold": None, "pass": None, "activity": None,
           "activity_name": None, "sample": []}  # activity keys always present (null when not run)
    if net is None:
        try:
            if tk is None:
                base = _Cpm(con, pid)
                r0 = base.run()
            else:
                base = _cpm_load(con, pid, tk)[0]
                r0 = _cpm_run(base, tk)
        except ValueError as e:
            return {**out, "note": _t("not run: {e}", e=str(e))}
        rate = _cpm_compare(base, r0, False)["match_rate"]
    else:
        base, r0, rate = net
    if rate is None or rate < CPM_TRUST:
        return {**out, "engine_match_rate": rate,
                "note": _t("not run: the CPM engine matches the last F9 on {rate} of open activities (needs "
                           "{CPM_TRUST}); see p6_cpm_validate", rate=rate, CPM_TRUST=CPM_TRUST)}
    cand = [d for d in base.t.values() if d["task_type"] in ("TT_Task", "TT_Rsrc") and base.duration_fixed(d) is None
            and r0[d["task_id"]]["tf_h"] is not None]
    if not cand:
        return {**out, "note": _t("not run: no open task")}
    low = min(r0[d["task_id"]]["tf_h"] for d in cand)
    crit = sorted((d for d in cand if r0[d["task_id"]]["tf_h"] <= low + 1), key=lambda d: (r0[d["task_id"]]["es"], d["task_code"]))
    d = crit[0]
    delay = 100 * (d["hpd"] or 8.0)
    trial = base.copy()
    trial.t[d["task_id"]]["rem"] = trial.t[d["task_id"]]["rem"] + delay
    r1 = trial.run(fields=False)
    f0, f1 = base.finish(r0)[1], trial.finish(r1)[1]
    act_shift = (r1[d["task_id"]]["ef"] - r0[d["task_id"]]["ef"]).total_seconds() / 86400
    fin_shift = (f1 - f0).total_seconds() / 86400
    if act_shift <= 0:
        return {**out, "activity": d["task_code"], "activity_name": d["task_name"], "engine_match_rate": rate,
                "note": _t("not evaluated: delaying {task_code} did not move its own finish", task_code=d['task_code'])}
    ok = fin_shift >= 0.9 * act_shift
    return {**out, "pass": ok, "activity": d["task_code"], "activity_name": d["task_name"], "delay_d": 100,
            "activity_finish_shift_cd": round(act_shift, 2), "project_finish_shift_cd": round(fin_shift, 2),
            "engine_match_rate": rate}
