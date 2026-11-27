"""The write path (dry run, commit, edit log entry) and the edits: relationships, activities, assignments,
UDF values, activity and project deletes, EPS nodes."""

from __future__ import annotations

import functools
import math
import os
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from p6mcp.core import state as _state
from p6mcp.core.constants import (
    CSTR_FINISH, CSTR_TYPES, EPPM_NEXT, HPD_SQL, REL_TYPES, REL_TYPES_REV, SUMMARY_TYPES, UDF_INDICATOR, UDF_KINDS,
    UDF_TEXT_MAX, UDF_VALUE,
)
from p6mcp.registry import ActEdit, _ActEditKeys, AsgEdit, LinkEdit, LinkNew, LinkRef, _plan_gate, UdfEdit
from p6mcp.core.db import (
    _alive, _backup, _cols, _connect, _days, _db_dt, _dict, _fmt_dt, _guard_write, _ids_sql, _insert, _next_key,
    _now_str, _num, _parse_dt, _project_id, _stamp_cols, _task, _write_refusal,
)
from p6mcp.core.props import _lag_hpd, _lag_info
from p6mcp.core.calendar import _calendars, _resolve_dt
from p6mcp.core.editlog import _caller_tool, _log_ts, _log_write
from p6mcp.core.wbs_codes import _eps_nodes, _guid
from p6mcp.core.read import _lag_arg, _rel_code, _udf_out, _udf_type


# ----------------------------------------------------------------- write tools

def _commit(con: sqlite3.Connection, plan: list[str], confirm: bool, projects: Iterable[int] = (),
            log: Optional[Sequence[dict]] = None) -> dict:
    """Dry run (rollback) or commit. projects = proj_ids whose schedule the change makes stale.
    log = the edit log entries of the change, one per changed item: {operation, proj_id or project,
    table, keys, before, after, summary}; default one entry summarising plan[0]. A confirmed commit
    appends them (with tool, time, backup in force and the stale projects' last_schedule_date)."""
    mismatch = _plan_gate(plan, confirm)
    if not confirm:
        con.rollback()
        con.close()
        return {"dry_run": True, "would_do": plan,
                "next": "Call again with confirm=True to apply"
                        + (" (the local working copy only)." if _state.SETTINGS.eppm else " (P6 must be closed).")}
    if mismatch:
        con.rollback()
        con.close()
        return {"applied": False, "error": mismatch}
    err = _guard_write()
    if err:
        con.rollback()
        con.close()
        return {"applied": False, "error": err}
    lsd = "last_schedule_date" if "last_schedule_date" in _cols(con, "PROJECT") else "NULL"
    snap = {}
    for p in sorted({int(p) for p in projects if p is not None}):
        r = con.execute(f"SELECT proj_short_name, {lsd} FROM PROJECT WHERE proj_id = ?" + _alive(con, "PROJECT"),
                        (p,)).fetchone()
        if r:
            snap[p] = (r[0], r[1])
    tool = _caller_tool()
    items = list(log) if log else [{"operation": tool[3:] if tool else "write",
                                    "proj_id": next(iter(snap), None), "summary": plan[0]}]
    names: dict[int, Optional[str]] = {p: v[0] for p, v in snap.items()}
    for it in items:  # project names now: a delete removes them at commit
        p = it.get("proj_id")
        if p is not None and p not in names:
            r = con.execute("SELECT proj_short_name FROM PROJECT WHERE proj_id = ?", (p,)).fetchone()
            names[p] = r[0] if r else None
    con.commit()
    con.close()
    ts, commit_id = _log_ts(), uuid.uuid4().hex[:12]
    stale = {str(p): v[1] for p, v in snap.items()}
    entries = [{"ts": ts, "commit": commit_id, "db": os.path.basename(_state.DB_PATH), "tool": tool,
                "operation": it.get("operation"), "project": it.get("project") or names.get(it.get("proj_id")),
                "proj_id": it.get("proj_id"), "table": it.get("table"), "keys": it.get("keys"),
                "before": it.get("before"), "after": it.get("after"), "summary": it.get("summary"),
                "change": plan[0][:160], "backup": _state._last_backup, "stale": stale,
                "stale_projects": [v[0] for v in snap.values()]} for it in items]
    res = {"applied": True, "done": plan,
           "next": EPPM_NEXT if _state.SETTINGS.eppm else "Open P6 and press F9 to reschedule."}
    if snap:
        res["stale_projects"] = [v[0] for v in snap.values()]
    try:
        res["edit_log"] = {"file": _log_write(entries), "entries": len(entries)}
    except OSError as e:  # committed already: say so rather than fail
        res["edit_log"] = {"error": f"edit log not written: {e}"}
    return res


def _open_for_write(confirm: bool) -> tuple[sqlite3.Connection, Optional[str]]:
    why = _write_refusal()  # dry runs too: a preview of a write that could never be applied misleads
    if why:
        raise RuntimeError(why)
    backup = None
    if confirm:
        err = _guard_write()
        if err:
            raise RuntimeError(err)
        if _state.SETTINGS.backup_before_write and (_state.SETTINGS.backup_scope == "every_write" or not _state._backup_done):
            backup = _backup()
    con = _connect(readonly=False)
    try:
        con.execute("BEGIN IMMEDIATE")  # fails fast if something else holds a write lock
    except BaseException:
        con.close()
        raise
    return con, backup


def _release(con: sqlite3.Connection) -> None:
    """Roll back anything uncommitted and close. Safe on an already closed connection."""
    try:
        con.rollback()
    except sqlite3.ProgrammingError:
        pass
    con.close()


@contextmanager
def _write_session(confirm: bool):
    """Open a write transaction that is always released, even if the tool raises."""
    con, backup = _open_for_write(confirm)
    try:
        yield con, backup
    finally:
        _release(con)


REL_LOG_COLS = ("pred_type", "lag_hr_cnt")  # TASKPRED values recorded in the edit log


def _rel_log(op: str, pred, succ, task_pred_id: int, before, after, summary: str) -> dict:
    """Edit log entry of one relationship change (before / after: TASKPRED rows or dicts)."""
    def pick(r) -> Optional[dict]:
        d = _dict(r) if isinstance(r, sqlite3.Row) else r
        return None if d is None else {k: d[k] for k in REL_LOG_COLS if k in d}
    return {"operation": op, "proj_id": succ["proj_id"], "table": "TASKPRED",
            "keys": {"task_pred_id": task_pred_id, "task_id": succ["task_id"], "pred_task_id": pred["task_id"],
                     "successor": succ["task_code"], "predecessor": pred["task_code"],
                     "pred_proj_id": pred["proj_id"]},
            "before": pick(before), "after": pick(after), "summary": summary}


def _find_rel(con, succ, pred, current_type: Optional[str]):
    """The one relationship pred -> succ (P6 allows one per type, e.g. an SS + FF pair).
    Returns (row, None) or (None, error)."""
    rows = con.execute("SELECT * FROM TASKPRED r WHERE r.task_id=? AND r.pred_task_id=?"
                       + _alive(con, "TASKPRED", "r"), (succ["task_id"], pred["task_id"])).fetchall()
    if current_type:
        if current_type.upper() not in REL_TYPES:
            raise ValueError("current_type must be FS, SS, FF or SF")
        rows = [r for r in rows if r["pred_type"] == REL_TYPES[current_type.upper()]]
    if not rows:
        return None, "Relationship not found."
    if len(rows) > 1:
        return None, ("Several relationships link these activities "
                      f"({[REL_TYPES_REV.get(r['pred_type']) for r in rows]}): pass current_type.")
    return rows[0], None


class _Refused(ValueError):
    """A link item refused for a reason the single-link tools return as {applied: False, error}."""


MAX_BATCH = 500  # items per batch call


def _task_by_id(con, task_id: int) -> sqlite3.Row:
    r = con.execute(f"SELECT t.*, {HPD_SQL} AS hpd, p.proj_short_name AS proj_short_name FROM TASK t "
                    "LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id JOIN PROJECT p ON p.proj_id = t.proj_id "
                    "WHERE t.task_id = ?" + _alive(con, "TASK", "t"), (task_id,)).fetchone()
    if r is None:
        raise ValueError(f"Activity id {task_id} not found")
    return r


def _rel_ends(con, project: str, it: Mapping) -> tuple[sqlite3.Row, sqlite3.Row]:
    """(pred, succ) of a link item: successor in project, predecessor in predecessor_project or project."""
    for k in ("predecessor_code", "successor_code"):
        if not isinstance(it.get(k), str) or not it[k]:
            raise ValueError(f"{k} is required")
    succ = _task(con, project, it["successor_code"])
    pred = _task(con, it.get("predecessor_project") or project, it["predecessor_code"])
    return pred, succ


def _rel_add(con, project: str, it: Mapping) -> dict:
    """Insert one link {predecessor_code, successor_code, rel_type='FS', lag_days=0, predecessor_project}."""
    code = _rel_code(it.get("rel_type") or "FS", "rel_type")
    lag_days = _lag_arg(it.get("lag_days") or 0)
    pred, succ = _rel_ends(con, project, it)
    if pred["task_id"] == succ["task_id"]:
        raise ValueError("An activity cannot be linked to itself.")
    if con.execute("SELECT 1 FROM TASKPRED r WHERE r.task_id=? AND r.pred_task_id=? AND r.pred_type=?"
                   + _alive(con, "TASKPRED", "r"), (succ["task_id"], pred["task_id"], code)).fetchone():
        raise _Refused("This relationship already exists, use p6_update_relationship.")
    hpd, basis = _lag_hpd(con, pred, succ)
    new_id = _next_key(con, "TASKPRED", "task_pred_id")
    row = {"task_pred_id": new_id, "task_id": succ["task_id"], "pred_task_id": pred["task_id"],
           "proj_id": succ["proj_id"], "pred_proj_id": pred["proj_id"], "pred_type": code, "lag_hr_cnt": lag_days * hpd}
    row.update(_stamp_cols(con, "TASKPRED", _now_str(con), create=True))
    row = {k: v for k, v in row.items() if k in _cols(con, "TASKPRED")}
    con.execute(f"INSERT INTO TASKPRED ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    plan = (f"Add {REL_TYPES_REV[code]} {pred['task_code']} -> {succ['task_code']}, "
            f"lag {lag_days:g} d = {lag_days * hpd:g} h ({basis}) (task_pred_id {new_id})")
    return {"plan": plan, "projects": [succ["proj_id"], pred["proj_id"]], "task_pred_id": new_id,
            "log": [_rel_log("add_relationship", pred, succ, new_id, None, row, plan)]}


def _rel_update(con, project: str, it: Mapping) -> dict:
    """Change type and/or lag of one link {predecessor_code, successor_code, rel_type, lag_days,
    current_type, predecessor_project}."""
    code = _rel_code(it["rel_type"], "rel_type") if it.get("rel_type") else None
    lag_days = None if it.get("lag_days") is None else _lag_arg(it["lag_days"])
    pred, succ = _rel_ends(con, project, it)
    r, err = _find_rel(con, succ, pred, it.get("current_type"))
    if err:
        raise _Refused(err)
    sets = _stamp_cols(con, "TASKPRED", _now_str(con), create=False)
    plan = []
    if code and code != r["pred_type"]:
        if con.execute("SELECT 1 FROM TASKPRED r WHERE r.task_id=? AND r.pred_task_id=? AND r.pred_type=?"
                       + _alive(con, "TASKPRED", "r"), (succ["task_id"], pred["task_id"], code)).fetchone():
            raise _Refused(f"A {REL_TYPES_REV[code]} relationship already links these activities: change or "
                           "delete that one instead.")
        sets["pred_type"] = code
        plan.append(f"type {REL_TYPES_REV.get(r['pred_type'])} -> {REL_TYPES_REV[code]}")
    if lag_days is not None:
        hpd, basis = _lag_hpd(con, pred, succ)
        sets["lag_hr_cnt"] = lag_days * hpd
        plan.append(f"lag {_days(r['lag_hr_cnt'], hpd)} d -> {lag_days:g} d = {lag_days * hpd:g} h ({basis})")
    if not plan:
        raise _Refused("Nothing to change.")
    con.execute(f"UPDATE TASKPRED SET {', '.join(k + '=?' for k in sets)} WHERE task_pred_id=?",
                [*sets.values(), r["task_pred_id"]])
    plan = f"{pred['task_code']} -> {succ['task_code']}: " + ", ".join(plan)
    return {"plan": plan, "projects": [succ["proj_id"], pred["proj_id"]], "task_pred_id": r["task_pred_id"],
            "log": [_rel_log("update_relationship", pred, succ, r["task_pred_id"], r, {**_dict(r), **sets}, plan)]}


def _rel_delete_row(con, r: sqlite3.Row, pred: sqlite3.Row, succ: sqlite3.Row) -> dict:
    """Hard-delete one TASKPRED row (as P6 and p6_delete_relationship do; P6's trigger logs REFRDEL).
    _pred / _succ: task_ids for side effects (not reported)."""
    con.execute("DELETE FROM TASKPRED WHERE task_pred_id=?", (r["task_pred_id"],))
    hpd = _lag_hpd(con, pred, succ)[0]
    plan = f"Delete {REL_TYPES_REV.get(r['pred_type'])} {pred['task_code']} -> {succ['task_code']}"
    return {"plan": plan, "projects": [succ["proj_id"], pred["proj_id"]], "task_pred_id": r["task_pred_id"],
            "predecessor": pred["task_code"], "successor": succ["task_code"],
            "type": REL_TYPES_REV.get(r["pred_type"], r["pred_type"]), "lag_d": _days(r["lag_hr_cnt"] or 0, hpd),
            "_pred": pred["task_id"], "_succ": succ["task_id"],
            "log": [_rel_log("delete_relationship", pred, succ, r["task_pred_id"], r, None, plan)]}


def _rel_delete(con, project: str, it: Mapping) -> dict:
    """Delete one link {predecessor_code, successor_code, type (or current_type), predecessor_project}."""
    pred, succ = _rel_ends(con, project, it)
    r, err = _find_rel(con, succ, pred, it.get("type") or it.get("current_type"))
    if err:
        raise _Refused(err)
    return _rel_delete_row(con, r, pred, succ)


def _single_link(fn: Callable, project: str, it: Mapping, confirm: bool) -> dict:
    """One link change as its own transaction (the single-link tools)."""
    with _write_session(confirm) as (con, backup):
        try:
            x = fn(con, project, it)
        except _Refused as e:
            return {"applied": False, "error": str(e)}
        plan = [x["plan"]]
        if backup:
            plan.append(f"Backup: {backup}")
        return _commit(con, plan, confirm, projects=x["projects"], log=x["log"])


_ENDS = {"predecessor_code", "successor_code"}
LINK_KEYS = {"delete": set(LinkRef.__annotations__) | _ENDS | {"current_type"},
             "add": set(LinkNew.__annotations__) | _ENDS, "update": set(LinkEdit.__annotations__) | _ENDS}


def _items_arg(items: Any) -> list:
    if not isinstance(items, (list, tuple)):
        raise ValueError("items: a list of objects")
    if len(items) > MAX_BATCH:
        raise ValueError(f"At most {MAX_BATCH} items per call")
    return list(items)


def _item_keys(it: Any, kind: str) -> Mapping:
    if not isinstance(it, Mapping):
        raise ValueError("an item must be an object with predecessor_code and successor_code")
    bad = sorted(set(it) - LINK_KEYS[kind])
    if bad:
        raise ValueError(f"unknown key(s) {bad}; allowed: {sorted(LINK_KEYS[kind])}")
    return it


def _side_effects(con, lost_pred: Iterable[int], lost_succ: Iterable[int], gone: Iterable[int] = ()) -> dict:
    """Open activities (not completed, not LOE or WBS summary) that lost a predecessor or a successor in
    this transaction and now have none (links to LOE and WBS summary activities do not count, as in
    p6_health_check). gone: deleted activities, left out."""
    gone = set(gone)
    out: dict[str, Any] = {}
    for key, ids, me, other in (("no_predecessor", lost_pred, "task_id", "pred_task_id"),
                                ("no_successor", lost_succ, "pred_task_id", "task_id")):
        ids = sorted(set(ids) - gone)
        rows = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows += con.execute(
                "SELECT p.proj_short_name, t.task_code, t.task_name, t.task_type FROM TASK t JOIN PROJECT p ON "
                f"p.proj_id = t.proj_id WHERE t.task_id IN ({','.join('?' * len(chunk))})" + _alive(con, "TASK", "t")
                + f" AND t.status_code <> 'TK_Complete' AND t.task_type NOT IN {SUMMARY_TYPES} AND NOT EXISTS "
                f"(SELECT 1 FROM TASKPRED r JOIN TASK o ON o.task_id = r.{other} WHERE r.{me} = t.task_id"
                + _alive(con, "TASKPRED", "r") + _alive(con, "TASK", "o") + f" AND o.task_type NOT IN {SUMMARY_TYPES})",
                chunk).fetchall()
        out[key] = [{"project": r[0], "task_code": r[1], "task_name": r[2], "task_type": r[3]}
                    for r in sorted(rows, key=lambda r: (r[0], r[1]))]
    out["count"] = len(out["no_predecessor"]) + len(out["no_successor"])
    out["note"] = ("open activities left without any predecessor or successor by this change (LOE and WBS "
                   "summary links do not count)")
    return out


def _link_batch(project: str, items: list, kind: str, confirm: bool, fn: Callable, head: str) -> dict:
    """Batch of link changes (one transaction, all or nothing) with side_effects and lag_rule. An item
    {'task_pred_id': n} deletes that link (it must touch the project)."""
    with _write_session(confirm) as (con, backup):
        pid = _project_id(con, project)
        lost_pred, lost_succ, done, removed = set(), set(), {}, []

        def apply_one(i: int, it) -> dict:
            if kind == "delete" and isinstance(it, Mapping) and set(it) == {"task_pred_id"}:
                tpid = it["task_pred_id"]
                if isinstance(tpid, bool) or not isinstance(tpid, int):
                    raise ValueError(f"task_pred_id must be an integer, not {tpid!r}")
                if tpid in done:
                    return {"plan": f"task_pred_id {tpid}: already removed by item {done[tpid]}", "projects": []}
                r = con.execute("SELECT * FROM TASKPRED WHERE task_pred_id = ?" + _alive(con, "TASKPRED"),
                                (tpid,)).fetchone()
                if r is None:
                    raise ValueError(f"task_pred_id {tpid} not found")
                pred, succ = _task_by_id(con, r["pred_task_id"]), _task_by_id(con, r["task_id"])
                if pid not in (pred["proj_id"], succ["proj_id"]):
                    raise ValueError(f"task_pred_id {tpid} links {pred['proj_short_name']} {pred['task_code']} -> "
                                     f"{succ['proj_short_name']} {succ['task_code']}: not a link of {project}")
                x = _rel_delete_row(con, r, pred, succ)
            elif kind == "delete":
                keys = _item_keys(it, kind)
                try:
                    x = fn(con, project, keys)
                except ValueError:  # the same link named twice (by id, then by pair): already gone, not an error
                    pred, succ = _rel_ends(con, project, keys)
                    ty = keys.get("type") or keys.get("current_type")
                    prior = [j for p, s, t, j in removed if p == pred["task_id"] and s == succ["task_id"]
                             and (ty is None or t == str(ty).upper())]
                    if not prior:
                        raise
                    return {"plan": f"{pred['task_code']} -> {succ['task_code']}: already removed by item {prior[0]}",
                            "projects": []}
            else:
                x = fn(con, project, _item_keys(it, kind))
            if kind == "delete":
                removed.append((x["_pred"], x["_succ"], x["type"], i))
                done[x["task_pred_id"]] = i
                lost_pred.add(x["_succ"])
                lost_succ.add(x["_pred"])
            return x

        b = _batch(con, items, apply_one)
        for rep, it in zip(b["items"], items, strict=False):
            rep["item"] = it
        side = _side_effects(con, lost_pred, lost_succ)
        lag = _lag_info(con, pid)
        res = _batch_commit(con, b, confirm, head, backup)
        res.update(side_effects=side, lag_rule=lag)
        return res


ACT_KEYS = set(ActEdit.__annotations__) | set(_ActEditKeys.__annotations__)


def _act_check(it: Mapping) -> dict:
    """Checks of one activity edit that need no database (keys, durations, %, constraint, date formats).
    Returns {code: constraint code or None, dates: {field: (value, edge)}}; ValueError otherwise."""
    if not isinstance(it, Mapping):
        raise ValueError("an item must be an object with task_code")
    bad = sorted(set(it) - ACT_KEYS)
    if bad:
        raise ValueError(f"unknown key(s) {bad}; allowed: {sorted(ACT_KEYS)}")
    if not isinstance(it.get("task_code"), str) or not it["task_code"]:
        raise ValueError("task_code is required")
    for name in ("original_duration_days", "remaining_duration_days"):
        v = it.get(name)
        if v is not None:
            f = _num(v)
            if f is None or not (math.isfinite(f) and f >= 0):
                raise ValueError(f"{name}: a duration must be 0 or more")
    if it.get("physical_pct") is not None:
        f = _num(it["physical_pct"])
        if f is None or not 0 <= f <= 100:
            raise ValueError("physical_pct must be between 0 and 100")
    constraint = it.get("constraint")
    if constraint is not None and constraint not in CSTR_TYPES:
        raise ValueError(f"constraint must be one of {list(CSTR_TYPES)}")
    code = CSTR_TYPES.get(constraint) if constraint is not None else None
    if code and code != "CS_ALAP" and not it.get("constraint_date"):
        raise ValueError("constraint_date is required for this constraint")
    dates = {"actual_start": (it.get("actual_start"), "start"), "actual_finish": (it.get("actual_finish"), "finish"),
             "constraint_date": (it.get("constraint_date") if code and code != "CS_ALAP" else None,
                                 "finish" if code in CSTR_FINISH else "start")}
    for k, (v, _) in dates.items():
        if v:
            _parse_dt(v, k)
    return {"code": code, "dates": dates}


def _act_update(con, project: str, it: Mapping) -> dict:
    """Change one activity (an ActEdit item) on an open write connection. Returns {plan (one line),
    _lines (plan line then notes), log, projects, task_code}; _Refused when there is nothing to change,
    ValueError for an edit P6 would refuse."""
    chk = _act_check(it)
    code, dates = chk["code"], chk["dates"]
    task_code = it["task_code"]
    task_name = it.get("task_name")
    original_duration_days = None if it.get("original_duration_days") is None else _num(it["original_duration_days"])
    remaining_duration_days = None if it.get("remaining_duration_days") is None else _num(it["remaining_duration_days"])
    constraint = it.get("constraint")
    actual_start, actual_finish = it.get("actual_start"), it.get("actual_finish")
    physical_pct = None if it.get("physical_pct") is None else _num(it["physical_pct"])
    t = _task(con, project, task_code)
    hpd = float(t["hpd"])
    sets: dict[str, Any] = {}
    plan: list[str] = []
    notes: list[str] = []
    cal = _calendars(con, int(t["clndr_id"])).get(int(t["clndr_id"])) if t["clndr_id"] is not None else None
    cal = None if cal is None or cal.error else cal

    def norm(field: str) -> Optional[str]:
        v, edge = dates[field]
        if not v:
            return None
        out, n = _resolve_dt(cal, v, field, edge)
        notes.extend(n)
        return out

    if task_name:
        sets["task_name"] = task_name
        plan.append(f"name -> {task_name}")
    if original_duration_days is not None:
        sets["target_drtn_hr_cnt"] = original_duration_days * hpd
        plan.append(f"orig dur {_days(t['target_drtn_hr_cnt'], hpd)} -> {original_duration_days} d")
        if t["status_code"] == "TK_NotStart" and remaining_duration_days is None:
            sets["remain_drtn_hr_cnt"] = original_duration_days * hpd
            plan.append(f"rem dur -> {original_duration_days} d (not started: follows the original)")
    if remaining_duration_days is not None:
        sets["remain_drtn_hr_cnt"] = remaining_duration_days * hpd
        plan.append(f"rem dur {_days(t['remain_drtn_hr_cnt'], hpd)} -> {remaining_duration_days} d")
    if constraint is not None:
        sets["cstr_type"] = code
        sets["cstr_date"] = norm("constraint_date")
        plan.append(f"constraint {t['cstr_type']} -> {code} {sets['cstr_date'] or ''}".strip())
    mile = {"TT_Mile": "start", "TT_FinMile": "finish"}.get(t["task_type"])
    if mile and (actual_start or actual_finish):  # one date: P6 stores actual start = actual finish
        got = [_resolve_dt(cal, dates[f][0], f, mile) for f in ("actual_start", "actual_finish") if dates[f][0]]
        if len({v for v, _ in got}) > 1:
            raise ValueError(f"{task_code} is a {mile} milestone: its actual start and finish are one date, "
                             f"not {' and '.join(v for v, _ in got)}")
        v = got[0][0]
        notes.extend(got[0][1])
        sets.update(act_start_date=v, act_end_date=v, status_code="TK_Complete", remain_drtn_hr_cnt=0)
        plan.append(f"actual start and finish -> {v} ({mile} milestone, Completed)")
    if actual_start and not mile:
        sets["act_start_date"] = norm("actual_start")
        started = t["status_code"] == "TK_NotStart" and not actual_finish
        if t["status_code"] == "TK_NotStart":
            sets["status_code"] = "TK_Active"
        plan.append(f"actual start -> {sets['act_start_date']}" + (" (In Progress)" if started else ""))
    if actual_finish and not mile:
        if not (actual_start or t["act_start_date"]):
            raise ValueError("Set actual_start before (or together with) actual_finish.")
        sets["act_end_date"] = norm("actual_finish")
        sets["status_code"] = "TK_Complete"
        sets["remain_drtn_hr_cnt"] = 0
        plan.append(f"actual finish -> {sets['act_end_date']} (Completed)")
    if physical_pct is not None:
        sets["phys_complete_pct"] = it["physical_pct"]
        plan.append(f"physical % -> {it['physical_pct']}")
    if not sets:
        raise _Refused("Nothing to change.")
    start = _db_dt(sets.get("act_start_date") or t["act_start_date"])
    finish = _db_dt(sets.get("act_end_date") or t["act_end_date"])
    if start and finish and finish < start:
        raise ValueError(f"actual finish {_fmt_dt(finish)} is before actual start {_fmt_dt(start)}")
    dd = None
    if "last_recalc_date" in _cols(con, "PROJECT"):
        dd = _db_dt(con.execute("SELECT last_recalc_date FROM PROJECT WHERE proj_id = ?", (t["proj_id"],)).fetchone()[0])
    late = [f"{k} {sets[c]}" for k, c in (("actual_start", "act_start_date"), ("actual_finish", "act_end_date"))
            if c in sets and dd and _db_dt(sets[c]) > dd]
    if late and not it.get("allow_after_data_date"):
        raise ValueError(f"{' and '.join(late)} after the data date {_fmt_dt(dd)}: actuals belong before it. "
                         "Pass allow_after_data_date=True if this is intended.")
    notes += [f"{x} is after the data date {_fmt_dt(dd)} (allowed)" for x in late]
    if physical_pct is not None and float(physical_pct) == 100 and not finish:
        notes.append("physical % 100 but no actual finish: the activity is not Completed until actual_finish is set")
    sets.update(_stamp_cols(con, "TASK", _now_str(con), create=False))
    sets = {k: v for k, v in sets.items() if k in _cols(con, "TASK")}
    con.execute(f"UPDATE TASK SET {', '.join(k + '=?' for k in sets)} WHERE task_id=?",
                [*sets.values(), t["task_id"]])
    head = f"{task_code}: " + "; ".join(plan)
    sched = set(sets) & {"target_drtn_hr_cnt", "remain_drtn_hr_cnt", "cstr_type", "cstr_date",
                         "act_start_date", "act_end_date", "status_code"}  # name and physical % do not move dates
    changed = [k for k in sets if k not in ("update_date", "update_user")]
    log = [{"operation": "update_activity", "proj_id": t["proj_id"], "table": "TASK",
            "keys": {"task_id": t["task_id"], "task_code": task_code},
            "before": {k: t[k] for k in changed}, "after": {k: sets[k] for k in changed}, "summary": head}]
    return {"plan": head + "".join(f"; Note: {n}" for n in notes), "_lines": [head] + [f"Note: {n}" for n in notes],
            "log": log, "projects": [t["proj_id"]] if sched else [], "task_code": task_code}


# ----------------------------------------------------------------- resource assignment units

FIXED_DRTN_TYPES = ("DT_FixedDrtn", "DT_FixedDUR2")  # P6 keeps the duration when units change: units/time follow
ASG_QTY = (("budgeted_units", "target_qty"), ("actual_units", "act_reg_qty"), ("remaining_units", "remain_qty"))
ASG_COST = {"target_qty": "target_cost", "act_reg_qty": "act_reg_cost", "remain_qty": "remain_cost"}
ASG_RATE = {"target_qty": ("target_qty_per_hr", "target_drtn_hr_cnt"),
            "remain_qty": ("remain_qty_per_hr", "remain_drtn_hr_cnt")}
ASG_TASK = {"target_qty": "target", "act_reg_qty": "act", "remain_qty": "remain"}  # TASK.<x>_work_qty / <x>_equip_qty
ASG_TASK_KIND = {"RT_Labor": "work", "RT_Equip": "equip"}  # material units are not rolled up to the activity
ASG_KEYS = set(AsgEdit.__annotations__)


def _asg_find(con, project: str, it: Mapping) -> tuple[sqlite3.Row, sqlite3.Row, str]:
    """(TASKRSRC row with rsrc_short_name and role_short_name, its activity, label) of an assignment item."""
    al = functools.partial(_alive, con)
    has_role = bool(_cols(con, "ROLES")) and "role_id" in _cols(con, "TASKRSRC")
    sql = ("SELECT a.*, r.rsrc_short_name AS rsrc_short_name, "
           + ("ro.role_short_name" if has_role else "NULL") + " AS role_short_name FROM TASKRSRC a "
           "LEFT JOIN RSRC r ON r.rsrc_id = a.rsrc_id" + al("RSRC", "r")
           + (" LEFT JOIN ROLES ro ON ro.role_id = a.role_id" + al("ROLES", "ro") if has_role else ""))
    label = lambda a: (a["rsrc_short_name"] or f"role {a['role_short_name'] or '?'}") + f" (taskrsrc_id {a['taskrsrc_id']})"  # noqa: E731
    tid = it.get("taskrsrc_id")
    if tid is not None:
        if isinstance(tid, bool) or not isinstance(tid, int):
            raise ValueError(f"taskrsrc_id must be an integer, not {tid!r}")
        a = con.execute(sql + " WHERE a.taskrsrc_id = ?" + al("TASKRSRC", "a"), (tid,)).fetchone()
        if a is None:
            raise ValueError(f"Assignment taskrsrc_id {tid} not found")
        t = _task_by_id(con, a["task_id"])
        if t["proj_id"] != _project_id(con, project) or it.get("task_code") not in (None, t["task_code"]):
            raise ValueError(f"taskrsrc_id {tid} is an assignment of {t['proj_short_name']} {t['task_code']}, "
                             f"not of {project} {it.get('task_code') or ''}".rstrip())
        return a, t, label(a)
    if not isinstance(it.get("task_code"), str) or not it["task_code"]:
        raise ValueError("task_code (with resource or role) or taskrsrc_id is required")
    t = _task(con, project, it["task_code"])
    rows = con.execute(sql + " WHERE a.task_id = ?" + al("TASKRSRC", "a") + " ORDER BY a.taskrsrc_id",
                       (t["task_id"],)).fetchall()
    if not rows:
        raise ValueError(f"{t['task_code']} has no resource assignment")
    if not it.get("resource") and not it.get("role"):
        raise ValueError(f"resource or role is required; assignments of {t['task_code']}: {[label(a) for a in rows]}")
    hits = rows
    for key, col in (("resource", "rsrc_short_name"), ("role", "role_short_name")):
        if it.get(key):
            want = str(it[key])
            exact = [a for a in hits if a[col] == want]
            hits = exact or [a for a in hits if (a[col] or "").casefold() == want.casefold()]
    if len(hits) != 1:
        raise ValueError(f"{'No' if not hits else 'Several'} assignment(s) of {t['task_code']} match "
                         f"{ {k: it[k] for k in ('resource', 'role') if it.get(k)} }: "
                         f"{[label(a) for a in (hits or rows)]}" + (" (pass taskrsrc_id)" if hits else ""))
    return hits[0], t, label(hits[0])


SUM_TABLES = ("SUMTASK", "SUMTASKSPREAD", "SUMTRSRC")  # P6 summary tables (Tools > Summarize)
SUMMARIZE_ADVICE = ("In P6 choose Tools > Summarize > Open Projects (or All Projects: only that one refreshes "
                    "the global resource summaries) to refresh them.")


def _summaries_stale(con, keys: Iterable[tuple]) -> dict:
    """Summary rows that assignment unit changes leave out of date, keys = (proj_id, rsrc_id, role_id) of
    the changed assignments: SUMTASK and SUMTASKSPREAD rows of the projects, SUMTRSRC rows of the
    projects or of the resources and roles (enterprise rows have no project). They are never written:
    no recompute matched P6's stored rows (time-phased spreads; the test database's SUMTRSRC rows match
    no current assignment). {stale, tables {table: rows}, refreshed [], advice}."""
    keys = list(keys)
    ids = {k: sorted({x[i] for x in keys if x[i] is not None}) for i, k in enumerate(("proj_id", "rsrc_id", "role_id"))}
    tables: dict[str, int] = {}
    for t in SUM_TABLES:
        cols = _cols(con, t)
        conds, args = [], []
        for c in ("proj_id",) + (("rsrc_id", "role_id") if t == "SUMTRSRC" else ()):
            if c in cols and ids[c]:
                conds.append(f"{c} IN ({', '.join('?' * len(ids[c]))})")
                args += ids[c]
        if conds:
            n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE ({' OR '.join(conds)})" + _alive(con, t), args).fetchone()[0]
            if n:
                tables[t] = n
    return {"stale": bool(tables), "tables": tables, "refreshed": [], "advice": SUMMARIZE_ADVICE if tables else None}


def _sum_plan(s: dict) -> list[str]:
    """Plan line of _summaries_stale (none when no summary row is affected)."""
    if not s["stale"]:
        return []
    rows = ", ".join(f"{t} {n} row(s)" for t, n in s["tables"].items())
    return [f"Summaries not refreshed ({rows}): {s['advice']}"]


def _asg_update(con, project: str, it: Mapping) -> dict:
    """Set the budgeted, actual and/or remaining (or at completion) units of one assignment, with P6's
    recalculation: linked costs, units/time (fixed duration), remaining = budget for a not started
    activity when the project links them, the activity's labor / nonlabor totals."""
    bad = sorted(set(it) - ASG_KEYS)
    if bad:
        raise ValueError(f"unknown key(s) {bad}; allowed: {sorted(ASG_KEYS)}")
    vals: dict[str, float] = {}
    for k in ("budgeted_units", "actual_units", "remaining_units", "at_completion_units"):
        if it.get(k) is not None:
            f = None if isinstance(it[k], bool) else _num(it[k])
            if f is None or f < 0:
                raise ValueError(f"{k} must be a number, 0 or more (not {it[k]!r})")
            vals[k] = f
    if "remaining_units" in vals and "at_completion_units" in vals:
        raise ValueError("Give remaining_units or at_completion_units (= actual + remaining), not both.")
    if not vals:
        raise _Refused("Nothing to change: give budgeted_units, actual_units, remaining_units or at_completion_units.")
    a, t, label = _asg_find(con, project, it)
    code, st = t["task_code"], t["status_code"]
    dt = t["duration_type"] if "duration_type" in _cols(con, "TASK") else None
    if dt is not None and dt not in FIXED_DRTN_TYPES:
        raise ValueError(f"{code} has duration type {dt}: P6 would recompute its duration from the units, and this "
                         "tool never changes durations. Only Fixed Duration types are handled; edit it in P6.")
    acols = _cols(con, "TASKRSRC")
    old = {c: float(a[c] or 0) for _, c in ASG_QTY}
    ot = float(a["act_ot_qty"] or 0) if "act_ot_qty" in acols else 0.0
    new = dict(old)
    notes: list[str] = []
    if "actual_units" in vals:
        if st == "TK_NotStart" and vals["actual_units"] > 0:
            raise ValueError(f"{code} is not started: set its actual start first (p6_update_activity), then actual units.")
        if vals["actual_units"] < ot:
            raise ValueError(f"actual_units {vals['actual_units']:g} is less than the overtime actual {ot:g}")
        new["act_reg_qty"] = vals["actual_units"] - ot
    actual = new["act_reg_qty"] + ot
    if "at_completion_units" in vals:
        if vals["at_completion_units"] < actual:
            raise ValueError(f"at_completion_units {vals['at_completion_units']:g} is less than the actual units {actual:g}")
        new["remain_qty"] = vals["at_completion_units"] - actual
    if "remaining_units" in vals:
        new["remain_qty"] = vals["remaining_units"]
    if st == "TK_Complete" and new["remain_qty"] > 0:
        raise ValueError(f"{code} is completed: its remaining units stay 0 (at completion = actual {actual:g}).")
    if "budgeted_units" in vals:
        new["target_qty"] = vals["budgeted_units"]
    link = None
    if st == "TK_NotStart" and "rem_target_link_flag" in _cols(con, "PROJECT"):
        link = con.execute("SELECT rem_target_link_flag FROM PROJECT WHERE proj_id = ?", (t["proj_id"],)).fetchone()[0]
    if link == "Y":  # P6 option "Link Budget and At Completion for not started activities"
        given_rem = "remaining_units" in vals or "at_completion_units" in vals
        if "budgeted_units" in vals and given_rem and abs(new["remain_qty"] - new["target_qty"]) > 1e-9:
            raise ValueError(f"{code} is not started and its project links budget and at completion units: give one "
                             f"value (budget {new['target_qty']:g} and at completion {new['remain_qty']:g} differ).")
        if "budgeted_units" in vals and not given_rem:
            new["remain_qty"] = new["target_qty"]
            notes.append("remaining follows the budget (not started, budget and at completion linked)")
        elif given_rem and "budgeted_units" not in vals:
            new["target_qty"] = new["remain_qty"]
            notes.append("budget follows at completion (not started, budget and at completion linked)")
    changed = [c for _, c in ASG_QTY if abs(new[c] - old[c]) > 1e-9]
    if not changed:
        raise _Refused(f"Nothing to change: {code} {label} already has these units.")
    sets: dict[str, Any] = {c: new[c] for c in changed}
    price = a["cost_per_qty"] if "cost_per_qty" in acols else None
    linked = "cost_qty_link_flag" in acols and a["cost_qty_link_flag"] == "Y" and price is not None
    if linked:
        sets.update({ASG_COST[c]: new[c] * float(price) for c in changed if ASG_COST[c] in acols})
    elif any(ASG_COST[c] in acols for c in changed):
        notes.append("costs unchanged (cost not linked to units on this assignment)")
    for c in changed:
        rate, drtn = ASG_RATE.get(c, (None, None))
        if rate in acols:  # fixed duration: the units/time absorb the change (resource calendar hours kept)
            hrs = float(t[drtn] or 0)
            sets[rate] = (float(a[rate]) * new[c] / old[c] if old[c] > 0 and a[rate] is not None
                          else new[c] / hrs if hrs > 0 else 0.0)
    kind = ASG_TASK_KIND.get(a["rsrc_type"]) if "rsrc_type" in acols else None
    tsets = {}
    for c in changed:
        col = f"{ASG_TASK[c]}_{kind}_qty"
        if kind and col in _cols(con, "TASK"):
            tsets[col] = float(t[col] or 0) + new[c] - old[c]
    if not kind:
        notes.append(f"resource type {a['rsrc_type']}: not rolled up to the activity's labor or nonlabor units")
    now = _now_str(con)
    full = {**sets, **_stamp_cols(con, "TASKRSRC", now, create=False)}
    con.execute(f"UPDATE TASKRSRC SET {', '.join(k + '=?' for k in full)} WHERE taskrsrc_id=?",
                [*full.values(), a["taskrsrc_id"]])
    if tsets:
        full = {**tsets, **_stamp_cols(con, "TASK", now, create=False)}
        con.execute(f"UPDATE TASK SET {', '.join(k + '=?' for k in full)} WHERE task_id=?", [*full.values(), t["task_id"]])
    names = {"target_qty": "budget", "act_reg_qty": "actual", "remain_qty": "remaining"}
    fmt = lambda v: f"{v:.6g}"  # noqa: E731
    parts = [f"{names[c]} {fmt(old[c] + (ot if c == 'act_reg_qty' else 0))} -> {fmt(new[c] + (ot if c == 'act_reg_qty' else 0))}"
             for c in changed]
    parts.append(f"at completion {fmt(old['act_reg_qty'] + ot + old['remain_qty'])} -> {fmt(actual + new['remain_qty'])}")
    costs = [f"{ASG_COST[c].replace('_', ' ')} {fmt(float(a[ASG_COST[c]] or 0))} -> {fmt(sets[ASG_COST[c]])}"
             for c in changed if ASG_COST[c] in sets]
    if costs:
        parts.append(f"{', '.join(costs)} ({fmt(float(price))}/unit)")
    plan = f"{code} {label}: " + ", ".join(parts) + "".join(f"; {n}" for n in notes)
    view = lambda q: {"budgeted_units": q["target_qty"], "actual_units": q["act_reg_qty"] + ot,  # noqa: E731
                      "remaining_units": q["remain_qty"], "at_completion_units": q["act_reg_qty"] + ot + q["remain_qty"]}
    log = [{"operation": "update_assignment", "proj_id": t["proj_id"], "table": "TASKRSRC",
            "keys": {"taskrsrc_id": a["taskrsrc_id"], "task_id": t["task_id"], "task_code": code,
                     "resource": a["rsrc_short_name"], "role": a["role_short_name"]},
            "before": {**{k: a[k] for k in sets}, **{f"TASK.{k}": t[k] for k in tsets}},
            "after": {**sets, **{f"TASK.{k}": v for k, v in tsets.items()}}, "summary": plan}]
    return {"plan": plan, "projects": [], "log": log,
            "_sum": (t["proj_id"], a["rsrc_id"] if "rsrc_id" in acols else None, a["role_id"] if "role_id" in acols else None),
            "assignment": {"taskrsrc_id": a["taskrsrc_id"], "task_code": code, "resource": a["rsrc_short_name"],
                           "role": a["role_short_name"], "before": view(old), "after": view(new)}}


UDF_KEYS = set(UdfEdit.__required_keys__) | set(UdfEdit.__optional_keys__)


def _udf_store(con, y: sqlite3.Row, value: Any) -> tuple[str, Any, Any, list[str]]:
    """(UDFVALUE column, value to store, value as shown, notes) of a user value for UDF type y."""
    lt, label = y["logical_data_type"], y["udf_type_label"]
    kind = UDF_KINDS.get(lt)
    if kind is None:
        raise ValueError(f"{label!r}: UDF data type {lt} is not supported")
    if kind == "text":
        if not isinstance(value, str):
            raise ValueError(f"{label!r} is a text UDF: give a string, not {value!r}")
        if len(value) > UDF_TEXT_MAX:
            raise ValueError(f"{label!r}: text longer than {UDF_TEXT_MAX} characters ({len(value)})")
        return "udf_text", value, value, []
    if kind == "indicator":
        v = value.strip().upper() if isinstance(value, str) else None
        if not v or not UDF_INDICATOR.fullmatch(v):
            raise ValueError(f"{label!r} is an indicator UDF: give UDF_G0 to UDF_G4 as P6 stores the icons, not {value!r}")
        return "udf_text", v, v, []
    if kind in ("start_date", "finish_date"):
        if not isinstance(value, str):
            raise ValueError(f"{label!r} is a date UDF: give 'YYYY-MM-DD[ HH:MM]', not {value!r}")
        s = _fmt_dt(_parse_dt(value, label)[0])
        return "udf_date", s, s, []
    if kind == "code":
        if "udf_code_id" not in _cols(con, "UDFVALUE") or "udf_type_id" not in _cols(con, "UDFCODE"):
            raise ValueError(f"{label!r} is a code UDF and this database has no UDFCODE table")
        rows = con.execute("SELECT udf_code_id, short_name, udf_code_name FROM UDFCODE WHERE udf_type_id = ?"
                           + _alive(con, "UDFCODE") + " ORDER BY udf_code_id", (y["udf_type_id"],)).fetchall()
        v = str(value).strip()
        hit = ([r for r in rows if r["short_name"] == v] or [r for r in rows if (r["short_name"] or "").casefold() == v.casefold()]
               or [r for r in rows if (r["udf_code_name"] or "").casefold() == v.casefold()])
        if len(hit) != 1:
            raise ValueError(f"{label!r}: {'several codes match' if hit else 'no code'} {value!r}; codes: "
                             f"{[r['short_name'] for r in (hit or rows)][:30]}")
        return "udf_code_id", hit[0]["udf_code_id"], hit[0]["short_name"], []
    f = None if isinstance(value, bool) else _num(value)
    if f is None:
        raise ValueError(f"{label!r} is a{'n' if kind == 'integer' else ''} {kind} UDF: give a number, not {value!r}")
    if kind == "integer":
        if not f.is_integer():
            raise ValueError(f"{label!r} is an integer UDF: {value!r} is not a whole number")
        return "udf_number", f, int(f), []
    if kind == "number" and round(f, 2) != f:
        return "udf_number", round(f, 2), round(f, 2), [f"rounded to 2 decimals as P6 stores it ({f!r})"]
    return "udf_number", f, f, []


def _udf_shown(con, y: sqlite3.Row, r: Optional[sqlite3.Row]) -> Any:
    """Value of a UDFVALUE row as shown (code short name), None for no row."""
    if r is None:
        return None
    lt = y["logical_data_type"]
    if lt == "FT_CODE":
        if r["udf_code_id"] is None or "udf_type_id" not in _cols(con, "UDFCODE"):
            return None
        c = con.execute("SELECT short_name FROM UDFCODE WHERE udf_code_id = ?", (r["udf_code_id"],)).fetchone()
        return c[0] if c else f"udf_code_id {r['udf_code_id']}"
    return _udf_out(lt, r[UDF_VALUE.get(lt, "udf_text")])


def _udf_set(con, project: str, it: Mapping) -> dict:
    """Set or clear one activity UDF value (UDFVALUE row of udf_type_id + task_id): insert, update (a
    soft-deleted row is revived) or delete (hard, as P6; its trigger logs REFRDEL)."""
    bad = sorted(set(it) - UDF_KEYS)
    if bad:
        raise ValueError(f"unknown key(s) {bad}; allowed: {sorted(UDF_KEYS)}")
    if not isinstance(it.get("task_code"), str) or not it["task_code"]:
        raise ValueError("task_code is required")
    clear = it.get("clear", False)
    if not isinstance(clear, bool):
        raise ValueError("clear must be true or false")
    value = it.get("value")
    if clear and value is not None:
        raise ValueError("Give value or clear=True, not both.")
    if not clear and (value is None or value == ""):
        raise ValueError("value is required (clear=True removes the value)")
    t = _task(con, project, it["task_code"])
    y = _udf_type(con, "TASK", it.get("udf"))
    label, code, tid, uid = y["udf_type_label"], t["task_code"], t["task_id"], y["udf_type_id"]
    if "formula" in _cols(con, "UDFTYPE") and y["formula"]:
        raise ValueError(f"{label!r} is a formula UDF ({y['formula']}): P6 computes its value")
    vcols = _cols(con, "UDFVALUE")
    old = con.execute("SELECT * FROM UDFVALUE WHERE udf_type_id = ? AND fk_id = ?", (uid, tid)).fetchone()
    dead = old is not None and "delete_session_id" in vcols and old["delete_session_id"] is not None
    live = None if dead else old
    before = _udf_shown(con, y, live)
    keys = {"udf_type_id": uid, "fk_id": tid, "task_code": code, "udf": label}
    now = _now_str(con)
    if clear:
        if live is None:
            raise _Refused(f"Nothing to clear: {code} has no {label!r} value.")
        col = "udf_code_id" if y["logical_data_type"] == "FT_CODE" else UDF_VALUE.get(y["logical_data_type"], "udf_text")
        con.execute("DELETE FROM UDFVALUE WHERE udf_type_id = ? AND fk_id = ?", (uid, tid))
        plan = f"{code} {label!r}: {before!r} -> cleared"
        log = [{"operation": "clear_udf", "proj_id": t["proj_id"], "table": "UDFVALUE", "keys": keys,
                "before": {col: live[col]}, "after": None, "summary": plan}]
        after = None
    else:
        col, stored, after, notes = _udf_store(con, y, value)
        if live is not None:
            cur = live[col]
            same = (cur is not None and abs(float(cur) - float(stored)) <= 1e-9 if col == "udf_number"
                    else _db_dt(cur) == _db_dt(stored) if col == "udf_date" else cur == stored)
            if same:
                raise _Refused(f"Nothing to change: {code} {label!r} is already {after!r}.")
        if old is not None:
            sets = {col: stored, "proj_id": t["proj_id"], **_stamp_cols(con, "UDFVALUE", now, create=False)}
            if dead:
                sets.update({c: None for c in ("delete_session_id", "delete_date") if c in vcols})
            con.execute(f"UPDATE UDFVALUE SET {', '.join(k + ' = ?' for k in sets)} WHERE udf_type_id = ? AND fk_id = ?",
                        [*sets.values(), uid, tid])
        else:
            row = {"udf_type_id": uid, "fk_id": tid, "proj_id": t["proj_id"], col: stored,
                   **_stamp_cols(con, "UDFVALUE", now, create=True)}
            con.execute(f"INSERT INTO UDFVALUE ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        plan = f"{code} {label!r}: {before!r} -> {after!r}" + "".join(f"; {n}" for n in notes)
        log = [{"operation": "update_udf", "proj_id": t["proj_id"], "table": "UDFVALUE", "keys": keys,
                "before": None if live is None else {col: live[col]}, "after": {col: stored}, "summary": plan}]
    return {"plan": plan, "projects": [], "log": log,
            "udf": {"task_code": code, "udf": label, "data_type": UDF_KINDS.get(y["logical_data_type"]),
                    "before": before, "after": after}}


def _udf_next(res: dict) -> dict:
    if res.get("applied"):
        res["next"] = "Open P6 to see the values (UDFs do not move dates: no F9 needed)."
    return res


# ----------------------------------------------------------------- delete one activity

# Rows that only point at an activity (an issue raised on it): kept, the link cleared. Every other
# table with a task_id, pred_task_id or taskrsrc_id column holds rows of the activity and loses them.
ACT_DEL_CLEAR = (("PROJISSU", "task_id"),)
ACT_DEL_ORDER = ("TASKACTV", "UDFVALUE", "TASKPROC", "PROJCOST", "TASKRSRC", "TASKPRED",
                 "TASK")  # children first, as DEL_ORDER
ACT_DEL_OWN = {"task_id": "_dt", "pred_task_id": "_dt", "taskrsrc_id": "_dr"}  # as DEL_OWN, activities only
ACT_DEL_UDF = {"TASK": "_dt", "TASKRSRC": "_dr", "TASKPROC": ("proc_id", "task_id", "_dt"),
               "PROJCOST": ("cost_item_id", "task_id", "_dt")}
ACT_SNAPSHOT = ("task_code", "task_name", "task_type", "status_code", "wbs_id", "clndr_id", "target_drtn_hr_cnt",
                "remain_drtn_hr_cnt", "cstr_type", "cstr_date", "act_start_date", "act_end_date", "phys_complete_pct")


def _act_del_where(con, t: str) -> list[str]:
    return _owned_where(con, t, ACT_DEL_OWN, ACT_DEL_UDF)


def _act_delete_rows(con, task_ids: Iterable[int]) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Hard-delete activities and every row of the schema that belongs to them: rows carrying their
    task_id or pred_task_id (links both ways, codes, notebooks, steps, expenses, documents, risks...),
    their assignments and the assignments' rows (taskrsrc_id: period actuals, timesheet hours, codes),
    UDF values of both, and the read marks of their discussions. ACT_DEL_CLEAR rows stay with the link
    cleared. Returns (deleted per table, cleared per column, rows still naming them: must be empty)."""
    _temp_id_sets(con, ("_dt", "_dr"))
    con.executemany("INSERT OR IGNORE INTO temp._dt VALUES (?)", [(int(i),) for i in task_ids])
    con.execute("INSERT OR IGNORE INTO temp._dr SELECT taskrsrc_id FROM TASKRSRC WHERE task_id IN (SELECT id FROM temp._dt)")
    cleared, counts = {}, {}
    for t, c in ACT_DEL_CLEAR:
        if c in _cols(con, t):
            n = con.execute(f"UPDATE {t} SET {c} = NULL WHERE {c} IN (SELECT id FROM temp._dt)").rowcount
            if n:
                cleared[f"{t}.{c}"] = n
    if "discussion_id" in _cols(con, "DISCUSSION_READ") and {"discussion_id", "task_id"} <= _cols(con, "DISCUSSION"):
        n = con.execute("DELETE FROM DISCUSSION_READ WHERE discussion_id IN (SELECT discussion_id FROM DISCUSSION "
                        "WHERE task_id IN (SELECT id FROM temp._dt))").rowcount
        if n:
            counts["DISCUSSION_READ"] = n
    tables = _db_tables(con)
    for t in [t for t in tables if t not in ACT_DEL_ORDER] + [t for t in ACT_DEL_ORDER if t in tables]:
        conds = _act_del_where(con, t)
        if conds:
            n = con.execute(f"DELETE FROM {t} WHERE {' OR '.join(conds)}").rowcount
            if n:
                counts[t] = n
    left = {}
    for t in tables:
        conds = _act_del_where(con, t)
        if conds:
            n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE {' OR '.join(conds)}").fetchone()[0]
            if n:
                left[t] = n
    return counts, cleared, left


# ----------------------------------------------------------------- batch writes

def _batch(con, items: Sequence, apply_one: Callable[[int, Any], dict]) -> dict:
    """Apply apply_one(index, item) to every item inside the open write transaction, each in its own
    savepoint. apply_one changes the database and returns {plan: str, log: [entries], projects:
    [proj_ids], **extra}, or raises ValueError: that item alone is rolled back and reported. Ids of
    inserts come from _next_key in the same transaction. Returns {items (one report per item: index,
    ok, plan or error, extra keys), plan, log, projects, failed}."""
    out: dict[str, Any] = {"items": [], "plan": [], "log": [], "projects": set(), "failed": 0}
    for i, item in enumerate(items):
        con.execute("SAVEPOINT p6_batch_item")
        try:
            r = apply_one(i, item)
        except ValueError as e:
            con.execute("ROLLBACK TO p6_batch_item")
            con.execute("RELEASE p6_batch_item")
            out["items"].append({"index": i, "ok": False, "error": str(e)})
            out["failed"] += 1
            continue
        except BaseException:
            con.execute("ROLLBACK TO p6_batch_item")
            con.execute("RELEASE p6_batch_item")
            raise
        con.execute("RELEASE p6_batch_item")
        extra = {k: v for k, v in r.items() if k not in ("plan", "log", "projects") and not k.startswith("_")}
        out["items"].append({"index": i, "ok": True, "plan": r["plan"], **extra})
        out["plan"].append(r["plan"])
        out["log"] += r.get("log") or []
        out["projects"] |= {p for p in r.get("projects") or () if p is not None}
    return out


def _batch_commit(con, b: dict, confirm: bool, head: str, backup: Optional[str], tail: Sequence[str] = ()) -> dict:
    """All or nothing: commit every item of _batch result b (one edit log entry per item), or, when an
    item failed, nothing (a dry run reports would_refuse; confirm returns applied False). The report
    per item is under items."""
    n, bad = len(b["items"]), b["failed"]
    plan = [head] + [f"  {p}" for p in b["plan"]] + list(tail)
    reasons = [f"{bad} of {n} item(s) cannot be applied (see items): nothing is written"] if bad else []
    if not n:
        reasons = ["no items"]
    if reasons and confirm:
        return {"applied": False, "error": " ".join(reasons) + " (all or nothing).", "items": b["items"]}
    if backup:
        plan.append(f"Backup: {backup}")
    res = _commit(con, plan, confirm, projects=b["projects"], log=b["log"])
    res["items"] = b["items"]
    if not confirm:
        res.update(would_refuse=bool(reasons), refuse_reasons=reasons)
        if reasons:
            res["next"] = "confirm=True would be refused: " + " ".join(reasons)
    return res


def _create_eps(con, eps_id: str, eps_name: str, parent_wbs: Optional[int], now: str) -> int:
    """Insert an EPS node (PROJECT row with project_flag N plus its PROJWBS node); returns the node wbs_id.
    Refuses a name already used by a project, baseline or EPS node (tools look PROJECT up by name)."""
    if con.execute("SELECT 1 FROM PROJECT WHERE proj_short_name = ?" + _alive(con, "PROJECT"), (eps_id,)).fetchone():
        raise ValueError(f"{eps_id!r} is already used by a project or EPS node: choose another EPS ID.")
    root_obs = con.execute("SELECT obs_id FROM OBS WHERE parent_obs_id IS NULL"
                           + _alive(con, "OBS") + " ORDER BY seq_num, obs_id").fetchone()
    pid = _next_key(con, "PROJECT", "proj_id")
    _insert(con, "PROJECT", {"proj_id": pid, "proj_short_name": eps_id, "project_flag": "N",
                             "guid": _guid(), **_stamp_cols(con, "PROJECT", now, create=True)})
    wid = _next_key(con, "PROJWBS", "wbs_id")
    _insert(con, "PROJWBS", {"wbs_id": wid, "proj_id": pid, "parent_wbs_id": parent_wbs,
                             "obs_id": root_obs[0] if root_obs else None, "seq_num": 0,
                             "proj_node_flag": "Y", "sum_data_flag": "N", "status_code": "WS_Open",
                             "wbs_short_name": eps_id, "wbs_name": eps_name, "guid": _guid(),
                             **_stamp_cols(con, "PROJWBS", now, create=True)})
    return wid


# ----------------------------------------------------------------- delete projects

# A P6 GUI delete (compared on this database, 116 projects and their EPS on 2026-10-19) hard-deletes
# the rows, children first, in this order per project; the AFTER DELETE triggers log each row in
# REFRDEL. Nothing is soft-deleted (delete_session_id stays unused) and NEXTKEY is not touched.
DEL_ORDER = ("TASKACTV", "UDFVALUE", "TASKPROC", "PROJCOST", "TASKRISK", "PROJRISK", "TASKRSRC", "TASKPRED", "TASK",
             "ACTVCODE", "ACTVTYPE", "PROJWBS", "CALENDAR", "PROJECT")
# (steps, expenses and risks after UDFVALUE, so that their UDF values are still found: placed, not measured)
# Never deleted from: delete logs, key counters, cross references and P6 sessions.
DEL_SKIP = {"REFRDEL", "NEXTKEY", "PKXREF", "USESSION"}
# Column -> set of deleted ids it belongs to (temp tables built per run). Every table of the schema
# that has one of these columns loses the matching rows: TASKPRED by either end (links to or from
# other projects), ACTVCODE and TASKACTV through the project's activity code types.
DEL_OWN = {"proj_id": "_dp", "task_id": "_dt", "pred_task_id": "_dt", "wbs_id": "_dw", "taskrsrc_id": "_dr",
           "actv_code_type_id": "_dct"}
# UDFVALUE.fk_id per UDFTYPE.table_name: a temp set, or (id column, owner column, temp set) for the rows of
# a table owned by those ids (steps and expenses of the activities, risks of the projects).
DEL_UDF = {"PROJECT": "_dp", "PROJWBS": "_dw", "TASK": "_dt", "TASKRSRC": "_dr",
           "TASKPROC": ("proc_id", "task_id", "_dt"), "PROJCOST": ("cost_item_id", "proj_id", "_dp"),
           "PROJRISK": ("risk_id", "proj_id", "_dp")}
# Columns of rows that stay but may name a deleted project: cleared (project baseline, copy source...).
DEL_CLEAR = (("PROJECT", "sum_base_proj_id"), ("PROJECT", "source_proj_id"), ("PROJISSU", "base_proj_id"))
# Orphan check: column -> (parent table, key). UDFVALUE.fk_id is checked per UDFTYPE.table_name.
ORPHAN_REFS = {"proj_id": ("PROJECT", "proj_id"), "orig_proj_id": ("PROJECT", "proj_id"),
               "sum_base_proj_id": ("PROJECT", "proj_id"), "pred_proj_id": ("PROJECT", "proj_id"),
               "base_proj_id": ("PROJECT", "proj_id"), "task_id": ("TASK", "task_id"),
               "pred_task_id": ("TASK", "task_id"), "wbs_id": ("PROJWBS", "wbs_id"),
               "parent_wbs_id": ("PROJWBS", "wbs_id"), "taskrsrc_id": ("TASKRSRC", "taskrsrc_id"),
               "actv_code_type_id": ("ACTVTYPE", "actv_code_type_id"), "actv_code_id": ("ACTVCODE", "actv_code_id"),
               "parent_actv_code_id": ("ACTVCODE", "actv_code_id"), "clndr_id": ("CALENDAR", "clndr_id"),
               "base_clndr_id": ("CALENDAR", "clndr_id")}


def _db_tables(con) -> list[str]:
    """Tables of the database that may hold project data (not the delete logs and key counters)."""
    return [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE "
                                      "'sqlite_%' ORDER BY name") if r[0] not in DEL_SKIP and not r[0].startswith("DLT")]


def _orphans(con) -> dict[str, int]:
    """Rows whose id column names a row that does not exist: {'TABLE.col -> PARENT': count}, non-zero
    only. Covers every table with a column of ORPHAN_REFS and UDFVALUE.fk_id per UDFTYPE.table_name."""
    out = {}
    for t in _db_tables(con):
        for c in sorted(_cols(con, t)):
            if c in ORPHAN_REFS and ORPHAN_REFS[c] != (t, c):
                pt, pk = ORPHAN_REFS[c]
                n = con.execute(f"SELECT COUNT(*) FROM {t} x WHERE x.{c} IS NOT NULL AND NOT EXISTS "
                                f"(SELECT 1 FROM {pt} p WHERE p.{pk} = x.{c})").fetchone()[0]
                if n:
                    out[f"{t}.{c} -> {pt}"] = n
    if {"fk_id", "udf_type_id"} <= _cols(con, "UDFVALUE") and _cols(con, "UDFTYPE"):
        for tn, pk in (("PROJECT", "proj_id"), ("PROJWBS", "wbs_id"), ("TASK", "task_id"), ("TASKRSRC", "taskrsrc_id"),
                       ("RSRC", "rsrc_id"), ("TASKPROC", "proc_id"), ("PROJCOST", "cost_item_id"), ("PROJRISK", "risk_id")):
            if pk not in _cols(con, tn):
                continue
            n = con.execute(f"SELECT COUNT(*) FROM UDFVALUE v JOIN UDFTYPE u ON u.udf_type_id = v.udf_type_id "
                            f"WHERE u.table_name = ? AND NOT EXISTS (SELECT 1 FROM {tn} p WHERE p.{pk} = v.fk_id)",
                            (tn,)).fetchone()[0]
            if n:
                out[f"UDFVALUE.fk_id -> {tn}"] = n
    return out


def _del_sets(con, pids: Iterable[int]) -> None:
    """Temp tables of the ids a delete removes: projects, their activities, WBS, assignments, code types."""
    _temp_id_sets(con, ("_dp", "_dt", "_dw", "_dr", "_dct"))
    con.executemany("INSERT OR IGNORE INTO temp._dp VALUES (?)", [(int(p),) for p in pids])
    con.execute("INSERT OR IGNORE INTO temp._dt SELECT task_id FROM TASK WHERE proj_id IN (SELECT id FROM temp._dp)")
    con.execute("INSERT OR IGNORE INTO temp._dw SELECT wbs_id FROM PROJWBS WHERE proj_id IN (SELECT id FROM temp._dp)")
    con.execute("INSERT OR IGNORE INTO temp._dr SELECT taskrsrc_id FROM TASKRSRC WHERE proj_id IN "
                "(SELECT id FROM temp._dp) OR task_id IN (SELECT id FROM temp._dt)")
    if "proj_id" in _cols(con, "ACTVTYPE"):
        con.execute("INSERT OR IGNORE INTO temp._dct SELECT actv_code_type_id FROM ACTVTYPE WHERE proj_id IN "
                    "(SELECT id FROM temp._dp)")


def _temp_id_sets(con, names: Iterable[str]) -> None:
    """Empty temp tables of ids (id INTEGER PRIMARY KEY) for a delete."""
    for t in names:
        con.execute(f"DROP TABLE IF EXISTS temp.{t}")
        con.execute(f"CREATE TEMP TABLE {t} (id INTEGER PRIMARY KEY)")


def _owned_where(con, t: str, own: Mapping[str, str], udf: Mapping[str, str]) -> list[str]:
    """Conditions (OR) that select the rows of table t belonging to the ids in the temp sets: a column
    of own (column -> temp set), or a UDFVALUE whose UDF type is on a table of udf (table -> temp set)."""
    conds = [f"{c} IN (SELECT id FROM temp.{s})" for c, s in own.items()
             if c in _cols(con, t) and not (c == "actv_code_type_id" and t == "ACTVTYPE")]
    if t == "UDFVALUE" and _cols(con, "UDFTYPE"):
        for tn, s in udf.items():
            if isinstance(s, tuple):  # rows of tn owned by the ids: the UDF values go before them (DEL_ORDER)
                pk, owner, s = s
                if not {pk, owner} <= _cols(con, tn):
                    continue
                ids = f"SELECT {pk} FROM {tn} WHERE {owner} IN (SELECT id FROM temp.{s})"
            else:
                ids = f"SELECT id FROM temp.{s}"
            conds.append(f"(fk_id IN ({ids}) AND udf_type_id IN (SELECT udf_type_id FROM UDFTYPE "
                         f"WHERE table_name = '{tn}'))")
    return conds


def _del_where(con, t: str) -> list[str]:
    """Conditions (OR) that select the rows of table t belonging to the deleted projects' ids."""
    return _owned_where(con, t, DEL_OWN, DEL_UDF)


def _delete_rows(con, pids: Iterable[int]) -> tuple[dict[str, int], dict[str, int]]:
    """Hard-delete the projects pids and every row of the schema that belongs to them, children first
    (P6's order, then the other tables). Returns (rows deleted per table, rows deleted per table that
    carry the proj_id of a project not deleted)."""
    _del_sets(con, pids)
    tables = _db_tables(con)
    order = [t for t in tables if t not in DEL_ORDER] + [t for t in DEL_ORDER if t in tables]
    counts, foreign = {}, {}
    for t in order:
        conds = _del_where(con, t)
        if not conds:
            continue
        where = " OR ".join(conds)
        if "proj_id" in _cols(con, t):
            n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE ({where}) AND proj_id IS NOT NULL AND proj_id NOT IN "
                            "(SELECT id FROM temp._dp)").fetchone()[0]
            if n:
                foreign[t] = n
        n = con.execute(f"DELETE FROM {t} WHERE {where}").rowcount
        if n:
            counts[t] = n
    return counts, foreign


def _cross_links(con) -> dict:
    """Relationships between a deleted activity and one that stays (temp sets of _del_sets)."""
    q = ("FROM TASKPRED r JOIN TASK s ON s.task_id = r.task_id JOIN TASK p ON p.task_id = r.pred_task_id "
         "JOIN PROJECT sp ON sp.proj_id = s.proj_id JOIN PROJECT pp ON pp.proj_id = p.proj_id WHERE "
         "(r.task_id IN (SELECT id FROM temp._dt)) <> (r.pred_task_id IN (SELECT id FROM temp._dt))")
    n = con.execute("SELECT COUNT(*) " + q).fetchone()[0]
    kept = sorted({r[0] for r in con.execute(
        "SELECT CASE WHEN r.task_id IN (SELECT id FROM temp._dt) THEN p.proj_id ELSE s.proj_id END " + q)})
    rows = con.execute("SELECT pp.proj_short_name AS pp, p.task_code AS pc, sp.proj_short_name AS sp, "
                       "s.task_code AS sc, r.pred_type AS ty " + q + " ORDER BY r.task_pred_id LIMIT 15").fetchall()
    return {"count": n, "sample": [f"{r['pp']} {r['pc']} -> {r['sp']} {r['sc']} {REL_TYPES_REV.get(r['ty'], r['ty'])}"
                                   for r in rows],
            "other_proj_ids": kept,
            "note": "deleted with the project (P6 does the same); the other project needs F9"}


def _eps_ancestors(con, pids: Iterable[int]) -> list[dict]:
    """EPS nodes above the projects pids, deepest first: {wbs_id, proj_id, eps_id, depth}."""
    nodes = {n["wbs_id"]: n for n in _eps_nodes(con)}
    out: dict[int, dict] = {}
    for (parent,) in con.execute("SELECT parent_wbs_id FROM PROJWBS WHERE proj_node_flag = 'Y' AND proj_id IN "
                                 f"{_ids_sql(pids)}").fetchall():
        chain, w = [], parent
        while w in nodes and w not in chain and len(chain) < 100:
            chain.append(w)
            w = nodes[w]["parent_wbs_id"]
        for d, w in enumerate(reversed(chain)):
            out[w] = {**nodes[w], "depth": d}
    return sorted(out.values(), key=lambda n: -n["depth"])


def _empty_eps(con, candidates: list[dict]) -> list[dict]:
    """Candidate EPS nodes (deepest first) left without any project or child EPS node, counting the
    nodes this pass removes as gone."""
    gone: set[int] = set()
    for n in candidates:
        kids = {r[0] for r in con.execute("SELECT wbs_id FROM PROJWBS WHERE parent_wbs_id = ?"
                                          + _alive(con, "PROJWBS"), (n["wbs_id"],))}
        if not kids - gone:
            gone.add(n["wbs_id"])
    return [n for n in candidates if n["wbs_id"] in gone]


def _delete_targets(con, names: list[str], include_baselines: bool, allow_templates: bool) -> tuple[list[dict], list[dict]]:
    """(projects named, their baselines) as {proj_id, project, kind, activities, status}; ValueError for
    unknown names, EPS nodes and templates (unless allowed)."""
    found, missing, errs = [], [], []
    for name in names:
        r = con.execute("SELECT p.proj_id, p.project_flag, p.orig_proj_id, w.status_code FROM PROJECT p LEFT JOIN "
                        "PROJWBS w ON w.proj_id = p.proj_id AND w.proj_node_flag = 'Y'" + _alive(con, "PROJWBS", "w")
                        + " WHERE p.proj_short_name = ?" + _alive(con, "PROJECT", "p"), (name,)).fetchone()
        if r is None:
            missing.append(name)
        elif (r["project_flag"] or "Y") != "Y":
            errs.append(f"{name} is an EPS node (delete_empty_eps removes EPS nodes left empty)")
        elif r["status_code"] == "WS_Template" and not allow_templates:
            errs.append(f"{name} is a template (status WS_Template): pass allow_templates=True to delete it")
        else:
            found.append({"proj_id": r["proj_id"], "project": name,
                          "kind": "baseline" if r["orig_proj_id"] else "project", "status": r["status_code"]})
    if missing:
        errs.insert(0, f"Project not found: {missing}")
    if errs:
        raise ValueError("; ".join(errs))
    ids = {p["proj_id"] for p in found}
    bls = [{"proj_id": r[0], "project": r[1], "kind": "baseline", "of": r[2], "status": None} for r in con.execute(
        "SELECT b.proj_id, b.proj_short_name, o.proj_short_name FROM PROJECT b JOIN PROJECT o ON o.proj_id = "
        f"b.orig_proj_id WHERE b.orig_proj_id IN {_ids_sql(ids)}" + _alive(con, "PROJECT", "b")
        + " ORDER BY b.proj_short_name") if r[0] not in ids]
    for p in found + bls:
        p["activities"] = con.execute("SELECT COUNT(*) FROM TASK WHERE proj_id = ?" + _alive(con, "TASK"),
                                      (p["proj_id"],)).fetchone()[0]
    return found, bls
