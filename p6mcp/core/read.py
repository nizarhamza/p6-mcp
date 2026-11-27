"""Read helpers of the activity and relationship tools: relationship rows, UDF values and filters, activity
details, the critical path condition."""

from __future__ import annotations

import difflib
import functools
import html
import re
import sqlite3
from typing import Any, Callable, Mapping, Optional, Sequence, get_args

from p6mcp.core.constants import ACT_LINKS_CAP, HPD_SQL, REL_TYPES, REL_TYPES_REV, UDF_KINDS, UDF_VALUE
from p6mcp.registry import CpMode, UdfOp
from p6mcp.core.db import _alive, _cols, _days, _fmt_dt, _ids_sql, _num, _page, _parse_dt
from p6mcp.core.props import _blob_text, _lag_hpd, _lag_rule
from p6mcp.core.wbs_codes import _name_sep


def _rel_sql(con, direction: str) -> str:
    """Links of activity :tid with the hours per day of both calendars, in a stable order."""
    parts = []
    for d, other, me in (("predecessor", "pred_task_id", "task_id"), ("successor", "task_id", "pred_task_id")):
        if direction in ("both", d[:4]):
            parts.append(
                f"SELECT '{d}' AS direction, r.task_pred_id, r.pred_type, r.lag_hr_cnt, r.proj_id AS rel_proj_id, "
                "o.task_code AS other_code, o.task_name AS other_name, po.proj_short_name AS other_project, "
                "COALESCE(NULLIF(cp.day_hr_cnt, 0), 8) AS pred_hpd, COALESCE(NULLIF(cs.day_hr_cnt, 0), 8) AS succ_hpd "
                f"FROM TASKPRED r JOIN TASK o ON o.task_id = r.{other} JOIN PROJECT po ON po.proj_id = o.proj_id "
                "JOIN TASK p ON p.task_id = r.pred_task_id JOIN TASK s ON s.task_id = r.task_id "
                "LEFT JOIN CALENDAR cp ON cp.clndr_id = p.clndr_id LEFT JOIN CALENDAR cs ON cs.clndr_id = s.clndr_id "
                f"WHERE r.{me} = :tid" + _alive(con, "TASKPRED", "r") + _alive(con, "TASK", "o")
                + _alive(con, "PROJECT", "po"))
    return ("SELECT * FROM (\n" + "\nUNION ALL\n".join(parts)
            + "\n) ORDER BY direction, other_project, other_code, pred_type, task_pred_id")


def _rel_row_fn(con, own_pid: int) -> Callable[[sqlite3.Row], dict]:
    """Row builder: lag in hours and in days under the lag rule of the link's (successor's) project."""
    rules: dict[int, str] = {}

    def row_fn(r: sqlite3.Row) -> dict:
        pid = own_pid if r["rel_proj_id"] is None else r["rel_proj_id"]
        for p in {pid, own_pid} - set(rules):
            rules[p] = _lag_rule(con, p)[0]
        hpd, basis = _lag_hpd(con, {"hpd": r["pred_hpd"]}, {"hpd": r["succ_hpd"]}, pid, rules[pid])
        out = {"direction": r["direction"], "task_pred_id": r["task_pred_id"], "other_code": r["other_code"],
               "other_name": r["other_name"], "other_project": r["other_project"],
               "type": REL_TYPES_REV.get(r["pred_type"], r["pred_type"]),
               "lag_h": r["lag_hr_cnt"], "lag_d": _days(r["lag_hr_cnt"], hpd)}
        if rules[pid] != rules[own_pid]:
            out["lag_basis"] = basis
        return out
    return row_fn


def _rel_page(con, t: sqlite3.Row, direction: str, offset: int = 0, limit: int = 100) -> dict:
    return _page(con, _rel_sql(con, direction), {"tid": t["task_id"]}, offset, limit,
                 _rel_row_fn(con, t["proj_id"]))


def _wbs_of(con, t: sqlite3.Row) -> dict:
    """wbs_name, wbs_code (as in p6_list_wbs) and wbs_path (names below the project node) of an
    activity, walking parent_wbs_id up to the project node (depth cap 50)."""
    chain, wid = [], t["wbs_id"]
    while wid is not None and len(chain) < 50:
        r = con.execute("SELECT parent_wbs_id, proj_node_flag, wbs_short_name, wbs_name FROM PROJWBS "
                        "WHERE wbs_id = ? AND proj_id = ?" + _alive(con, "PROJWBS"), (wid, t["proj_id"])).fetchone()
        if r is None:
            break
        chain.insert(0, r)
        wid = None if r["proj_node_flag"] == "Y" else r["parent_wbs_id"]
    return {"wbs_name": chain[-1]["wbs_name"] if chain else None,
            "wbs_code": _name_sep(con, t["proj_id"]).join(r["wbs_short_name"] or "" for r in chain) or None,
            "wbs_path": [r["wbs_name"] for r in chain if r["proj_node_flag"] != "Y"]}


def _memo_text(v) -> str:
    """Notebook TEXT or BLOB -> plain text: HTML tags removed, entities decoded, spaces collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", _blob_text(v))).split())


def _plus(a, b) -> Optional[float]:
    return None if a is None and b is None else float(a or 0) + float(b or 0)


def _udf_row(r: sqlite3.Row) -> dict:
    t = r["logical_data_type"]
    value = r[UDF_VALUE[t]] if t in UDF_VALUE else next(
        (r[k] for k in ("udf_text", "udf_number", "udf_date", "udf_code") if r[k] is not None), None)
    return {"label": r["udf_type_label"], "type": t, "value": value}


# ----------------------------------------------------------------- user defined fields (UDFs)

def _udf_type(con, table: str, udf: Any) -> sqlite3.Row:
    """UDFTYPE row of a subject table (TASK, PROJECT...) by label, the column title in P6: exact, else
    case and outer spaces ignored. Several matches or none raise (close labels listed)."""
    if not isinstance(udf, str) or not udf.strip():
        raise ValueError("udf: the UDF label (its column title in P6) is required")
    rows = con.execute("SELECT * FROM UDFTYPE WHERE table_name = ?" + _alive(con, "UDFTYPE")
                       + " ORDER BY udf_type_id", (table,)).fetchall()
    hit = [r for r in rows if r["udf_type_label"] == udf] or [
        r for r in rows if (r["udf_type_label"] or "").strip().casefold() == udf.strip().casefold()]
    if len(hit) == 1:
        return hit[0]
    if hit:
        raise ValueError(f"Several {table} UDFs are labelled {udf!r} (udf_type_id {[r['udf_type_id'] for r in hit]})")
    labels = [r["udf_type_label"] or "" for r in rows]
    near = difflib.get_close_matches(udf.strip(), labels, n=8, cutoff=0.4) or labels[:20]
    raise ValueError(f"No {table} UDF labelled {udf!r}. Close labels: {near} (p6_list_udfs lists them)")


def _udf_col(con, ltype: str) -> str:
    """SQL value of UDFVALUE alias v for a logical type: code short name, dates with a space."""
    if ltype == "FT_CODE":
        if not _cols(con, "UDFCODE") or "udf_code_id" not in _cols(con, "UDFVALUE"):
            return "NULL"
        return ("(SELECT uc.short_name FROM UDFCODE uc WHERE uc.udf_code_id = v.udf_code_id"
                + _alive(con, "UDFCODE", "uc") + ")")
    if ltype in ("FT_START_DATE", "FT_END_DATE"):
        return "REPLACE(v.udf_date, 'T', ' ')"
    return "v." + UDF_VALUE.get(ltype, "udf_text")


def _udf_out(ltype: str, v: Any) -> Any:
    """Stored value as shown: integers as int, dates with a space, the rest as stored."""
    if v is None:
        return None
    if ltype == "FT_INT" and isinstance(v, float) and v.is_integer():
        return int(v)
    if ltype in ("FT_START_DATE", "FT_END_DATE") and isinstance(v, str):
        return v.replace("T", " ")
    return v


UDF_CMP = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}


def _udf_filter(con, filters: Any) -> tuple[str, list, list[dict]]:
    """(conditions on TASK t, args, udf_filter info) of activity UDF conditions, all to hold. Numbers
    compare as numbers (eq / ne within 1e-9), dates as datetimes (a date alone is the whole day: eq
    any time that day, lt before it, le up to its end), text, indicators and codes ignoring case;
    contains = substring (text, indicator, code); empty = no value; ne needs a value (empty ones are
    not 'different')."""
    if not filters:
        return "", [], []
    if isinstance(filters, Mapping):
        filters = [filters]
    if not isinstance(filters, (list, tuple)):
        raise ValueError("udf_filter: a list of {udf, op, value}")
    sql, args, info = "", [], []
    for f in filters:
        if not isinstance(f, Mapping):
            raise ValueError("udf_filter: a list of {udf, op, value}")
        bad = sorted(set(f) - {"udf", "op", "value"})
        if bad:
            raise ValueError(f"udf_filter: unknown key(s) {bad}; allowed: ['op', 'udf', 'value']")
        op = f.get("op") or "eq"
        if op not in get_args(UdfOp):
            raise ValueError(f"udf_filter op must be one of {list(get_args(UdfOp))}")
        y = _udf_type(con, "TASK", f.get("udf"))
        lt, label = y["logical_data_type"], y["udf_type_label"]
        kind, col, v = UDF_KINDS.get(lt, "text"), _udf_col(con, lt), f.get("value")
        sub = ("SELECT 1 FROM UDFVALUE v WHERE v.udf_type_id = ? AND v.fk_id = t.task_id"
               + _alive(con, "UDFVALUE", "v"))
        a: list[Any] = [y["udf_type_id"]]
        if op in ("empty", "not_empty"):
            if v is not None:
                raise ValueError(f"udf_filter {label!r} {op} takes no value")
            sql += f" AND {'NOT ' if op == 'empty' else ''}EXISTS ({sub} AND NULLIF({col}, '') IS NOT NULL)"
        else:
            if v is None or v == "":
                raise ValueError(f"udf_filter {label!r} {op}: value is required (op 'empty' finds no value)")
            if op == "contains":
                if kind not in ("text", "indicator", "code"):
                    raise ValueError(f"udf_filter {label!r}: contains is for text, indicator and code UDFs ({kind})")
                cond = f"{col} LIKE ?"
                a.append(f"%{v}%")
            elif kind in ("number", "integer", "cost"):
                x = None if isinstance(v, bool) else _num(v)
                if x is None:
                    raise ValueError(f"udf_filter {label!r} is a {kind} UDF: value must be a number, not {v!r}")
                cond = {"eq": f"ABS({col} - ?) <= 1e-9", "ne": f"ABS({col} - ?) > 1e-9"}.get(op, f"{col} {UDF_CMP[op]} ?")
                a.append(x)
            elif kind in ("start_date", "finish_date"):
                dt, has_time = _parse_dt(v, f"udf_filter {label!r}")
                if has_time:
                    cond = f"{col} {UDF_CMP[op]} ?"
                    a.append(_fmt_dt(dt))
                else:
                    lo, hi = dt.strftime("%Y-%m-%d 00:00:00"), dt.strftime("%Y-%m-%d 23:59:59")
                    cond, b = {"eq": (f"{col} BETWEEN ? AND ?", [lo, hi]), "ne": (f"{col} NOT BETWEEN ? AND ?", [lo, hi]),
                               "lt": (f"{col} < ?", [lo]), "le": (f"{col} <= ?", [hi]),
                               "gt": (f"{col} > ?", [hi]), "ge": (f"{col} >= ?", [lo])}[op]
                    a += b
            else:
                cond = f"{col} {UDF_CMP[op]} ? COLLATE NOCASE"
                a.append(str(v).strip())
            sql += f" AND EXISTS ({sub} AND {cond})"
        args += a
        info.append({"udf": label, "data_type": kind, "op": op, "value": v})
    return sql, args, info


def _udf_values(con, table: str, ids: Sequence[int], types: Sequence[sqlite3.Row]) -> dict[int, dict[str, Any]]:
    """{fk_id: {label: value}} of the given UDF types for rows ids of table (None when empty)."""
    out = {i: {y["udf_type_label"]: None for y in types} for i in ids}
    if not ids or not types:
        return out
    by_id = {y["udf_type_id"]: y for y in types}
    code = ("(SELECT uc.short_name FROM UDFCODE uc WHERE uc.udf_code_id = v.udf_code_id" + _alive(con, "UDFCODE", "uc") + ")"
            if _cols(con, "UDFCODE") and "udf_code_id" in _cols(con, "UDFVALUE") else "NULL")
    for r in con.execute(f"SELECT v.fk_id, v.udf_type_id, v.udf_text, v.udf_number, v.udf_date, {code} AS udf_code "
                         f"FROM UDFVALUE v WHERE v.udf_type_id IN {_ids_sql(by_id)} AND v.fk_id IN {_ids_sql(ids)}"
                         + _alive(con, "UDFVALUE", "v")):
        y = by_id[r["udf_type_id"]]
        lt = y["logical_data_type"]
        if r["fk_id"] in out:
            out[r["fk_id"]][y["udf_type_label"]] = _udf_out(lt, r[UDF_VALUE.get(lt, "udf_text")])
    return out


def _memo_row(r: sqlite3.Row) -> dict:
    text = _memo_text(r["task_memo"])
    return {"topic": r["memo_type"], "text": text[:500], "truncated": len(text) > 500}


def _act_details(con, task_id: int) -> tuple[dict, list[str]]:
    """({assignments, codes, udfs, notebooks}, names of the lists cut at ACT_LINKS_CAP) of one
    activity. A list whose tables this P6 version lacks is empty."""
    al = functools.partial(_alive, con)
    unit, code, role = ("NULL", ""), ("NULL", ""), ("NULL", "")
    if _cols(con, "UMEASURE") and "unit_id" in _cols(con, "RSRC"):
        unit = ("u.unit_abbrev", " LEFT JOIN UMEASURE u ON u.unit_id = r.unit_id" + al("UMEASURE", "u"))
    if _cols(con, "UDFCODE") and "udf_code_id" in _cols(con, "UDFVALUE"):
        code = ("uc.short_name", " LEFT JOIN UDFCODE uc ON uc.udf_code_id = v.udf_code_id" + al("UDFCODE", "uc"))
    if _cols(con, "ROLES") and "role_id" in _cols(con, "TASKRSRC"):
        role = ("ro.role_short_name", " LEFT JOIN ROLES ro ON ro.role_id = a.role_id" + al("ROLES", "ro"))
    specs = (
        ("assignments", ("TASKRSRC", "RSRC"),
         f"SELECT a.taskrsrc_id, r.rsrc_short_name, r.rsrc_name, r.rsrc_type, {role[0]} AS role, {unit[0]} AS unit, a.target_qty, "
         "a.act_reg_qty, a.act_ot_qty, a.remain_qty, a.target_cost, a.act_reg_cost, a.act_ot_cost, a.remain_cost "
         "FROM TASKRSRC a LEFT JOIN RSRC r ON r.rsrc_id = a.rsrc_id" + al("RSRC", "r") + unit[1] + role[1]
         + " WHERE a.task_id = ?" + al("TASKRSRC", "a") + " ORDER BY r.rsrc_short_name, a.taskrsrc_id",
         lambda r: {"taskrsrc_id": r["taskrsrc_id"], "resource": r["rsrc_short_name"], "name": r["rsrc_name"], "type": r["rsrc_type"], "role": r["role"],
                    "unit": r["unit"], "target_qty": r["target_qty"], "actual_qty": _plus(r["act_reg_qty"], r["act_ot_qty"]),
                    "remain_qty": r["remain_qty"], "target_cost": r["target_cost"],
                    "actual_cost": _plus(r["act_reg_cost"], r["act_ot_cost"]), "remain_cost": r["remain_cost"]}),
        ("codes", ("TASKACTV", "ACTVTYPE", "ACTVCODE"),
         "SELECT y.actv_code_type, v.short_name, v.actv_code_name FROM TASKACTV a "
         "JOIN ACTVTYPE y ON y.actv_code_type_id = a.actv_code_type_id" + al("ACTVTYPE", "y")
         + " JOIN ACTVCODE v ON v.actv_code_id = a.actv_code_id" + al("ACTVCODE", "v")
         + " WHERE a.task_id = ?" + al("TASKACTV", "a") + " ORDER BY y.actv_code_type, a.actv_code_type_id",
         lambda r: {"type": r["actv_code_type"], "code": r["short_name"], "name": r["actv_code_name"]}),
        ("udfs", ("UDFVALUE", "UDFTYPE"),
         f"SELECT y.udf_type_label, y.logical_data_type, v.udf_text, v.udf_number, v.udf_date, {code[0]} AS udf_code "
         "FROM UDFVALUE v JOIN UDFTYPE y ON y.udf_type_id = v.udf_type_id" + al("UDFTYPE", "y") + code[1]
         + " WHERE y.table_name = 'TASK' AND v.fk_id = ?" + al("UDFVALUE", "v")
         + " ORDER BY y.udf_type_label, v.udf_type_id", _udf_row),
        ("notebooks", ("TASKMEMO", "MEMOTYPE"),
         "SELECT mt.memo_type, m.task_memo FROM TASKMEMO m LEFT JOIN MEMOTYPE mt ON mt.memo_type_id = m.memo_type_id"
         + al("MEMOTYPE", "mt") + " WHERE m.task_id = ?" + al("TASKMEMO", "m") + " ORDER BY mt.seq_num, m.memo_id",
         _memo_row),
    )
    out, cut = {}, []
    for key, tables, sql, fn in specs:
        rows = (con.execute(f"{sql} LIMIT {ACT_LINKS_CAP + 1}", (task_id,)).fetchall()
                if all(_cols(con, tb) for tb in tables) else [])
        out[key] = [fn(r) for r in rows[:ACT_LINKS_CAP]]
        if len(rows) > ACT_LINKS_CAP:
            cut.append(key)
    return out, cut


def _critical_clause(con, pid: int, mode: str, threshold_days: Optional[float]
                     ) -> tuple[str, list, str, Optional[float], str]:
    """(condition on TASK t / CALENDAR c, args, definition, threshold_h, mode_used) of 'critical'.
    project follows PROJECT.critical_path_type: CT_TotFloat = total float <= critical_drtn_hr_cnt
    hours, CT_DrivPath = longest path, anything else total float (said in the definition).
    threshold_days forces total float <= that many days of each activity's calendar."""
    if mode not in get_args(CpMode):
        raise ValueError(f"mode must be one of {list(get_args(CpMode))}")
    if threshold_days is not None:
        if mode == "longest_path":
            raise ValueError("float_threshold_days applies to total float, not to mode='longest_path'")
        return (f"t.total_float_hr_cnt <= ? * {HPD_SQL}", [float(threshold_days)],
                f"total float <= {float(threshold_days):g} d (days of each activity's calendar)", None, "total_float")
    cols = _cols(con, "PROJECT")
    p = con.execute("SELECT " + ", ".join(c if c in cols else f"NULL AS {c}" for c in
                                          ("critical_path_type", "critical_drtn_hr_cnt"))
                    + " FROM PROJECT WHERE proj_id = ?", (pid,)).fetchone()
    hours, src = float(p["critical_drtn_hr_cnt"] or 0), ""
    if mode == "project":
        ctype = p["critical_path_type"]
        mode = "longest_path" if ctype == "CT_DrivPath" else "total_float"
        src = (f"project setting {ctype}: " if ctype in ("CT_DrivPath", "CT_TotFloat")
               else f"project setting {ctype!r} unknown, total float used: ")
    if mode == "longest_path":
        if "driving_path_flag" in _cols(con, "TASK"):
            return "t.driving_path_flag = 'Y'", [], src + "longest path (driving_path_flag)", None, "longest_path"
        src += "no driving_path_flag column, total float used: "
        hours = 0.0  # the hours threshold belongs to the total float definition: 0 h, P6's default
    return ("t.total_float_hr_cnt <= ?", [hours], src + f"total float <= {hours:g} h (critical_drtn_hr_cnt)",
            hours, "total_float")


def _rel_code(v: Any, name: str) -> str:
    if not isinstance(v, str) or v.upper() not in REL_TYPES:
        raise ValueError(f"{name} must be FS, SS, FF or SF")
    return REL_TYPES[v.upper()]


def _lag_arg(v: Any) -> float:
    f = _num(v)
    if f is None:
        raise ValueError("lag_days must be a number")
    return f
