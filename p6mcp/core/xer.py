"""XER files: reading, pre-flight, import (new, baseline, update) and export."""

from __future__ import annotations

import functools
import math
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime
from typing import Any, Optional, Sequence, get_args

from p6mcp.core import state as _state
from p6mcp.core.constants import REL_TYPES_REV
from p6mcp.registry import Removable, XerTable
from p6mcp.core.db import _alive, _cols, _ids_sql, _insert, _next_key, _now_str, _prefer, _project_id, _stamp_cols
from p6mcp.core.props import _blob_text, _project_row, SCHED_XER_COLS, _xer_project, _xer_sched_export
from p6mcp.core.wbs_codes import _guid
from p6mcp.messages import _t


# ----------------------------------------------------------------- XER import / export

# Tables exported, in dependency order. SCHEDOPTIONS is not a table of the database: export builds it from
# PROJPROP 'scheduling', import writes it back there. The order of the 17 tables of the reference XERs is the
# one P6 writes; P6 sorts by dependency level, then by name within a level, and the notebook, step, expense,
# role and risk tables (3.4.0) are placed by that rule: no P6 export of them was available to confirm it.
XER_EXPORT_TABLES = list(get_args(XerTable))
# Tables imported row by row.
XER_TABLES = [t for t in XER_EXPORT_TABLES if t != "SCHEDOPTIONS"]
# Primary key of each table that receives new ids from NEXTKEY.
XER_PK = {"CURRTYPE": "curr_id", "FINTMPL": "fintmpl_id", "OBS": "obs_id", "UDFTYPE": "udf_type_id",
          "PROJECT": "proj_id", "CALENDAR": "clndr_id", "PROJWBS": "wbs_id", "RSRC": "rsrc_id",
          "ACTVTYPE": "actv_code_type_id", "RSRCRATE": "rsrc_rate_id", "TASK": "task_id",
          "ACTVCODE": "actv_code_id", "TASKPRED": "task_pred_id", "TASKRSRC": "taskrsrc_id",
          "COSTTYPE": "cost_type_id", "MEMOTYPE": "memo_type_id", "RISKTYPE": "risk_type_id", "ROLES": "role_id",
          "PROJRISK": "risk_id", "ROLERATE": "role_rate_id", "PROJCOST": "cost_item_id", "TASKMEMO": "memo_id",
          "TASKPROC": "proc_id"}
# Id columns that point to a table carried by the XER. Any other *_id column refers to data
# the XER does not contain (cost accounts, responsible managers, curves, locations...) and is cleared on import.
XER_FK = {"proj_id": "PROJECT", "pred_proj_id": "PROJECT", "orig_proj_id": "PROJECT",
          "sum_base_proj_id": "PROJECT", "source_proj_id": "PROJECT",
          "wbs_id": "PROJWBS", "parent_wbs_id": "PROJWBS", "obs_id": "OBS", "parent_obs_id": "OBS",
          "clndr_id": "CALENDAR", "base_clndr_id": "CALENDAR", "rsrc_id": "RSRC",
          "parent_rsrc_id": "RSRC", "curr_id": "CURRTYPE", "fintmpl_id": "FINTMPL",
          "actv_code_type_id": "ACTVTYPE", "actv_code_id": "ACTVCODE",
          "parent_actv_code_id": "ACTVCODE", "udf_type_id": "UDFTYPE",
          "task_id": "TASK", "pred_task_id": "TASK", "memo_type_id": "MEMOTYPE", "cost_type_id": "COSTTYPE",
          "role_id": "ROLES", "parent_role_id": "ROLES", "risk_type_id": "RISKTYPE",
          "parent_risk_type_id": "RISKTYPE", "risk_id": "PROJRISK"}
# UDFVALUE.fk_id points to the table named in UDFTYPE.table_name.
UDF_FK_TABLES = {"PROJECT", "PROJWBS", "TASK", "RSRC", "TASKRSRC", "PROJCOST", "TASKPROC", "PROJRISK"}
# Memo columns (HTML text): stored as P6 stores them in a BLOB column (UTF-8 BOM, text, NUL).
XER_MEMO_COLS = {"TASKMEMO": "task_memo", "TASKPROC": "proc_descr", "PROJCOST": "cost_descr", "ROLES": "role_descr",
                 "PROJRISK": "risk_descr"}
# Table names of other P6 versions read as ours (Oracle's XER data map spells the risks table PRORISK).
XER_ALIASES = {"PRORISK": "PROJRISK"}
# Tables added in 3.4.0 (notebooks, steps, expenses, roles, risks): skipped with a warning when this database lacks
# them or their NEXTKEY counter (the 17 older tables are required).
XER_DETAIL_TABLES = ("COSTTYPE", "MEMOTYPE", "RISKTYPE", "ROLES", "PROJRISK", "ROLERATE", "PROJCOST", "TASKMEMO",
                     "TASKPROC", "TASKRISK")
# Project rows with their own id matched on update by these columns (after id mapping), each database row once.
XER_CHILD_KEYS = {"PROJRISK": ("proj_id", "risk_code"), "TASKMEMO": ("task_id", "memo_type_id"),
                  "TASKPROC": ("task_id", "proc_name"), "PROJCOST": ("task_id", "cost_name")}
# Ids a child row needs: without them it is skipped (skipped_unmapped).
XER_CHILD_NEEDS = {"PROJRISK": ("proj_id",), "TASKMEMO": ("task_id", "memo_type_id"), "TASKPROC": ("task_id",),
                   "PROJCOST": ("task_id",), "TASKRISK": ("task_id", "risk_id")}
# Global rows (no proj_id) matched by name on import and reused: (key columns, query on those keys).
XER_MATCH = {
    "CURRTYPE": (("curr_short_name",), "SELECT curr_id FROM CURRTYPE WHERE curr_short_name = ?"),
    "FINTMPL": (("fintmpl_name",), "SELECT fintmpl_id FROM FINTMPL WHERE fintmpl_name = ?"),
    "OBS": (("obs_name",), "SELECT obs_id FROM OBS WHERE obs_name = ?"),
    "UDFTYPE": (("table_name", "udf_type_label"),
                "SELECT udf_type_id FROM UDFTYPE WHERE table_name = ? AND udf_type_label = ?"),
    "CALENDAR": (("clndr_type", "clndr_name"),
                 "SELECT clndr_id FROM CALENDAR WHERE proj_id IS NULL AND clndr_type = ? AND clndr_name = ?"),
    "RSRC": (("rsrc_short_name",), "SELECT rsrc_id FROM RSRC WHERE rsrc_short_name = ?"),
    "ACTVTYPE": (("actv_code_type_scope", "actv_code_type"), "SELECT actv_code_type_id FROM ACTVTYPE WHERE "
                 "proj_id IS NULL AND actv_code_type_scope = ? AND actv_code_type = ?"),
    "MEMOTYPE": (("memo_type",), "SELECT memo_type_id FROM MEMOTYPE WHERE memo_type = ?"),
    "COSTTYPE": (("cost_type",), "SELECT cost_type_id FROM COSTTYPE WHERE cost_type = ?"),
    "ROLES": (("role_short_name", "parent_role_id"),
              "SELECT role_id FROM ROLES WHERE role_short_name = ? AND parent_role_id IS ?"),
    "RISKTYPE": (("risk_type", "parent_risk_type_id"),
                 "SELECT risk_type_id FROM RISKTYPE WHERE risk_type = ? AND parent_risk_type_id IS ?"),
}
# Hierarchical global tables matched under their parent: the parent column of the key is mapped first (a Role ID
# is only unique under its parent role).
XER_MATCH_PARENT = {"ROLES": "parent_role_id", "RISKTYPE": "parent_risk_type_id"}
# Never copied from an XER row: set by triggers / this server, or meaningless across databases.
XER_SKIP_COLS = {"create_date", "create_user", "update_date", "update_user",
                 "delete_session_id", "delete_date", "guid"}
_XER_DATE = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d")
# p6_import_xer counts: the actions every table row carries (0 when none)
IMPORT_ACTIONS = ("matched", "inserted", "updated", "unchanged", "restored", "deleted", "kept_existing", "skipped_external",
                  "skipped_duplicate", "skipped_unmapped")  # unchanged / restored: PROJPROP's schedule options row
_XER_NL = "\x7f\x7f"  # P6 writes line breaks inside memo fields as two DEL characters


def _xer_decode(v: str) -> Optional[str]:
    if v == "":
        return None
    v = v.replace(_XER_NL, "\n").replace('""', '"')
    return v + ":00" if _XER_DATE.fullmatch(v) else v


def _xer_encode(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        v = _blob_text(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            return ""
        s = f"{v:.6f}".rstrip("0").rstrip(".")  # 6 decimals as before, never "7." or "-0"
        return "0" if s in ("-0", "") else s
    v = str(v)
    if re.fullmatch(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d(:\d\d(\.\d+)?)?", v):
        return v[:10] + " " + v[11:16]
    return v.replace("\r\n", "\n").replace("\n", _XER_NL).replace("\t", " ").replace('"', '""')


def _xer_read_meta(path: str) -> tuple[list[str], dict[str, list[dict]], dict]:
    """XER file -> (header, tables, meta). A UTF-8 BOM is skipped; text is UTF-8, else cp1252
    (utf8_error_at = file offset of the first non UTF-8 byte). A repeated %T block adds to its
    table, %R lines before their %F are not read, and rows with more or fewer values than fields
    are counted (missing values read as empty, extra ones dropped). XER_ALIASES names are read as ours."""
    with open(path, "rb") as fh:
        raw = fh.read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if bom else raw
    meta: dict[str, Any] = {"bytes": len(raw), "bom": bom, "encoding": "utf-8", "utf8_error_at": None}
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        meta.update(encoding="cp1252", utf8_error_at=e.start + 3 * bom)
        text = body.decode("cp1252", errors="replace")
    header: list[str] = []
    tables: dict[str, list[dict]] = {}
    cur, fields, end = None, None, False
    blocks, early, mismatch = Counter(), Counter(), Counter()
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("ERMHDR"):
            header = line.split("\t")
        elif line.startswith("%T\t"):
            cur, fields = line[3:].strip(), None
            cur = XER_ALIASES.get(cur, cur)
            blocks[cur] += 1
            tables.setdefault(cur, [])
        elif line.startswith("%F\t"):
            fields = [f.lower() for f in line[3:].split("\t")]
        elif line.startswith("%R\t") and cur:
            if fields is None:
                early[cur] += 1
                continue
            vals = line[3:].split("\t")
            if len(vals) != len(fields):
                mismatch[cur] += 1
                vals = (vals + [""] * len(fields))[:len(fields)]
            tables[cur].append({f: _xer_decode(v) for f, v in zip(fields, vals, strict=False)})
        elif line.startswith("%E"):
            end = True
    if not header:
        raise ValueError(_t("{path} is not an XER file (no ERMHDR line).", path=path))
    meta.update(replacement_chars=text.count("\ufffd"),
                non_ascii_bytes=len(body.translate(None, bytes(range(128)))),
                has_end_marker=end, field_count_mismatch=dict(mismatch), rows_before_fields=dict(early),
                duplicate_table_blocks=sorted(t for t, n in blocks.items() if n > 1))
    return header, tables, meta


def _xer_read(path: str) -> tuple[list[str], dict[str, list[dict]]]:
    return _xer_read_meta(path)[:2]


def _xer_only(x: dict, xpid) -> dict:
    """Rows of XER project xpid plus global rows (no proj_id). ACTVCODE has no proj_id: only the
    codes of a kept code type stay (another project's types are dropped, so are their codes)."""
    out = {t: [r for r in rows if r.get("proj_id") in (None, xpid)] for t, rows in x.items()}
    if "ACTVCODE" in out:
        kept = {r.get("actv_code_type_id") for r in out.get("ACTVTYPE", [])}
        out["ACTVCODE"] = [r for r in out["ACTVCODE"] if r.get("actv_code_type_id") in kept]
    return out


def _xer_preflight(header: list[str], x: dict, meta: dict, xer_project: Optional[str] = None,
                   con: Optional[sqlite3.Connection] = None, preview: bool = True) -> dict:
    """Checks of one XER project before import, each {check, count, sample (15), note}. errors: data
    the import would write wrong (no project WBS node, activities without WBS or calendar, duplicate
    Activity IDs, assignments of activities not in the file, rows with a wrong number of values); a
    confirmed p6_import_xer refuses them. warnings: values cleared or reattached, tables not imported, codes
    longer than PREFER allows. info: rows skipped on purpose, encoding. With con: dropped_columns
    (XER fields the database lacks) and, with preview, db_preview (global items matched by name
    or created, as the import would)."""
    xp = _xer_project(x, xer_project)
    all_codes = x.get("ACTVCODE", [])
    x = _xer_only(x, xp["proj_id"])
    kept_codes = {id(r) for r in x.get("ACTVCODE", [])}
    out: dict[str, Any] = {"project": xp["proj_short_name"], "errors": [], "warnings": [], "info": [],
                           "unhandled_tables": {t: len(r) for t, r in x.items()
                                                if t not in XER_TABLES and t != "SCHEDOPTIONS"},
                           "dropped_columns": {}, "db_preview": None}

    def add(level: str, check: str, items: Sequence, note: str = "") -> None:  # note: a tool message (_t)
        if items:
            out[level].append({"check": check, "count": len(items), "sample": [str(i) for i in items[:15]],
                               **({"note": note} if note else {})})

    ids = lambda t: {r.get(XER_PK.get(t, "")) for r in x.get(t, [])} - {None}  # noqa: E731
    fields = {t: set().union(*rows) for t, rows in x.items()}
    tasks, wbs, cals, rsrc, asg = (x.get(t, []) for t in ("TASK", "PROJWBS", "CALENDAR", "RSRC", "TASKRSRC"))
    wbs_ids, cal_ids, task_ids, rsrc_ids = ids("PROJWBS"), ids("CALENDAR"), ids("TASK"), ids("RSRC")
    code = {t.get("task_id"): t.get("task_code") for t in tasks}
    node_ids = {w.get("wbs_id") for w in wbs if w.get("proj_node_flag") == "Y"}
    fc = meta.get("field_count_mismatch", {})
    add("errors", "project_wbs_node_missing", [] if node_ids else [xp["proj_short_name"]], _t("no project WBS node"))
    add("errors", "task_wbs_missing", [t.get("task_code") for t in tasks if t.get("wbs_id") not in wbs_ids],
        _t("would be imported without a WBS"))
    add("errors", "task_calendar_missing", [t.get("task_code") for t in tasks if t.get("clndr_id") not in cal_ids],
        _t("would be imported without a calendar"))
    add("errors", "duplicate_task_code", sorted(str(c) for c, n in Counter(code.values()).items() if n > 1),
        _t("Activity IDs must be unique in a project"))
    add("errors", "taskrsrc_task_missing", [f"taskrsrc {r.get('taskrsrc_id')}: task {r.get('task_id')}" for r in asg
                                            if r.get("task_id") not in task_ids], _t("assignment of an activity not in the file"))
    add("errors", "field_count_mismatch", [f"{t}: {n} rows" for t, n in fc.items() if t in XER_TABLES],
        _t("rows with more or fewer values than fields"))
    add("warnings", "field_count_mismatch_not_imported", [f"{t}: {n} rows" for t, n in fc.items() if t not in XER_TABLES])
    add("warnings", "rows_before_fields", [f"{t}: {n} rows" for t, n in meta.get("rows_before_fields", {}).items()],
        _t("%R lines before the %F line of their table are not read"))
    add("warnings", "duplicate_table_blocks", meta.get("duplicate_table_blocks", []), _t("the rows of every block are read"))
    add("warnings", "end_marker_missing", [] if meta.get("has_end_marker", True) else ["%E"], _t("the file may be truncated"))
    add("warnings", "undecodable_bytes", [meta["replacement_chars"]] if meta.get("replacement_chars") else [],
        _t("characters not readable as {encoding}: check names and notes", encoding=meta.get("encoding")))
    add("warnings", "resource_missing",
        [f"activity {t.get('task_code')}" for t in tasks if t.get("rsrc_id") is not None and t["rsrc_id"] not in rsrc_ids]
        + [f"taskrsrc {r.get('taskrsrc_id')}" for r in asg if r.get("rsrc_id") is not None and r["rsrc_id"] not in rsrc_ids],
        _t("the resource is cleared"))
    add("warnings", "wbs_parent_missing", [w.get("wbs_short_name") for w in wbs if w.get("wbs_id") not in node_ids
                                           and w.get("parent_wbs_id") not in wbs_ids], _t("attached to the project node"))
    cal_ok = cal_ids | {None}
    add("warnings", "calendar_missing",
        [f"base of {c.get('clndr_name')}" for c in cals if c.get("base_clndr_id") not in cal_ok]
        + [f"resource {r.get('rsrc_short_name')}" for r in rsrc if r.get("clndr_id") not in cal_ok]
        + (["project"] if xp.get("clndr_id") not in cal_ok else []), _t("cleared on import"))
    links = Counter((r.get("pred_task_id"), r.get("task_id"), r.get("pred_type")) for r in x.get("TASKPRED", []))
    add("warnings", "duplicate_link", [f"{code.get(p, p)} -> {code.get(s, s)} {REL_TYPES_REV.get(k, k)}"
                                       for (p, s, k), n in links.items() if n > 1], _t("P6 keeps one link per type"))
    cleared = Counter()
    for t in XER_TABLES:
        for k in sorted(fields.get(t, ())):
            if k.endswith("_id") and k not in XER_FK and k not in XER_SKIP_COLS and k not in ("fk_id", XER_PK.get(t)):
                cleared[f"{t}.{k}"] = sum(1 for r in x[t] if r.get(k) is not None)
    add("warnings", "foreign_ids_cleared", [f"{k}: {n}" for k, n in cleared.items() if n],
        _t("ids of data an XER does not carry (roles, accounts, curves...) are cleared"))
    add("warnings", "tables_not_imported", [f"{t}: {n} rows" for t, n in out["unhandled_tables"].items()])
    add("info", "activity_codes_other_types", [f"{c.get('short_name')} (type {c.get('actv_code_type_id')})"
                                               for c in all_codes if id(c) not in kept_codes],
        _t("codes of another project's code types or of a type not in the file: not imported"))
    add("info", "schedule_options",["SCHEDOPTIONS"] if x.get("SCHEDOPTIONS") else [],
        _t("written to the project's schedule options (PROJPROP scheduling) unless import_schedule_options=False "
           "(default off in baseline mode); compare with p6_schedule_options"))
    add("info", "external_links", [f"{code.get(r.get('pred_task_id'), r.get('pred_task_id'))} -> "
                                   f"{code.get(r.get('task_id'), r.get('task_id'))}" for r in x.get("TASKPRED", [])
                                   if r.get("task_id") not in task_ids or r.get("pred_task_id") not in task_ids],
        _t("relationships with an activity outside the file are skipped"))
    types_, codes_ = ids("ACTVTYPE"), ids("ACTVCODE")
    add("info", "activity_codes_unmapped", [f"task {r.get('task_id')}" for r in x.get("TASKACTV", [])
                                            if r.get("task_id") not in task_ids or r.get("actv_code_type_id") not in types_
                                            or r.get("actv_code_id") not in codes_], _t("skipped"))
    utab = {u.get("udf_type_id"): u.get("table_name") for u in x.get("UDFTYPE", [])}
    pks = {t: ids(t) for t in UDF_FK_TABLES}
    add("info", "udf_values_unmapped", [f"{utab.get(v.get('udf_type_id'))} {v.get('fk_id')}" for v in x.get("UDFVALUE", [])
                                        if v.get("fk_id") not in pks.get(utab.get(v.get("udf_type_id")), ())], _t("skipped"))
    memo_types, risks = ids("MEMOTYPE"), ids("PROJRISK")
    add("info", "activity_details_unmapped",
        [f"{t} {r.get(XER_PK.get(t, 'risk_id'))}: task {r.get('task_id')}" for t in ("TASKMEMO", "TASKPROC", "PROJCOST",
                                                                                   "TASKRISK")
         for r in x.get(t, []) if r.get("task_id") not in task_ids]
        + [f"TASKMEMO {r.get('memo_id')}: topic {r.get('memo_type_id')}" for r in x.get("TASKMEMO", [])
           if r.get("task_id") in task_ids and r.get("memo_type_id") not in memo_types]
        + [f"TASKRISK task {r.get('task_id')}: risk {r.get('risk_id')}" for r in x.get("TASKRISK", [])
           if r.get("task_id") in task_ids and r.get("risk_id") not in risks],
        _t("notebooks, steps, expenses or risk links of an activity, topic or risk not in the file: skipped"))
    add("info", "project_baseline_not_imported", [] if xp.get("sum_base_proj_id") is None else [xp["sum_base_proj_id"]],
        _t("the project baseline link (sum_base_proj_id) is not carried over"))
    add("info", "encoding", [] if meta.get("encoding") == "utf-8" else [
        f"{meta.get('encoding')}, first non UTF-8 byte at offset {meta.get('utf8_error_at')}"], _t("the file is not UTF-8"))
    if con is None:
        out["note"] = _t("No database: dropped_columns, code lengths and db_preview not checked.")
        return out
    absent = [t for t in XER_TABLES if x.get(t) and (not _cols(con, t) or (
        t in XER_DETAIL_TABLES and t in XER_PK and not _has_next_key(con, t, XER_PK[t])))]
    add("warnings", "tables_not_in_database", [f"{t}: {len(x[t])} rows" for t in absent],
        _t("this database has no such table (or no NEXTKEY counter for it): the rows are not imported"))
    out["dropped_columns"] = {t: sorted(fields[t] - _cols(con, t)) for t in XER_TABLES
                              if fields.get(t) and _cols(con, t) and fields[t] - _cols(con, t)}
    long = []
    for key, rows, col in (("task_code_len", tasks, "task_code"), ("proj_short_len", [xp], "proj_short_name"),
                           ("wbs_short_len", [w for w in wbs if w.get("wbs_id") not in node_ids], "wbs_short_name"),
                           ("rsrc_short_len", rsrc, "rsrc_short_name")):
        n = _prefer(con, key)
        long += [f"{col} {r.get(col)!r} ({len(r.get(col))} > {n})" for r in rows if n and len(r.get(col) or "") > n]
    add("warnings", "codes_too_long", long, _t("longer than this database allows (PREFER); for the Project ID "
                                               "pass new_short_name"))
    out["project_id_in_db"] = con.execute("SELECT 1 FROM PROJECT WHERE proj_short_name = ?" + _alive(con, "PROJECT"),
                                          (xp["proj_short_name"],)).fetchone() is not None
    if preview:
        out["db_preview"] = {}
        for t in XER_MATCH:
            rows = [r for r in x.get(t, []) if r.get("proj_id") is None]
            if rows and t not in absent:
                hit = len(_preview_matches(con, t, rows))
                out["db_preview"][t] = {"matched": hit, "would_create": len(rows) - hit}
    return out


def _preview_matches(con, table: str, rows: list[dict]) -> dict:
    """{xer id: database id} of the global rows an import would match (XER_MATCH), without writing. Under
    XER_MATCH_PARENT a row matches only when its parent does (a new parent has no children yet)."""
    keys, sql = XER_MATCH[table]
    par, pk = XER_MATCH_PARENT.get(table), XER_PK[table]
    xids = {r.get(pk) for r in rows}
    found: dict = {}
    done: set = set()
    pending = list(rows)
    while pending:
        later = [r for r in pending if par and r.get(par) in xids and r.get(par) not in done and r.get(par) != r.get(pk)]
        for r in pending:
            if any(r is q for q in later):
                continue
            done.add(r.get(pk))
            if par and r.get(par) in xids and r.get(par) not in found:
                continue  # the parent would be created
            vals = [found.get(r.get(k)) if k == par else r.get(k) for k in keys]
            hit = con.execute(sql + _alive(con, table), vals).fetchone()
            if hit:
                found[r.get(pk)] = hit[0]
        if len(later) == len(pending):  # parent cycle: the rest would be created
            break
        pending = later
    return found


class _XerImport:
    """One import run. idmap[table][xer id] = database id."""

    def __init__(self, con, tables: dict[str, list[dict]], mode: str):
        self.con, self.x, self.mode = con, tables, mode
        self.idmap: dict[str, dict[str, int]] = {t: {} for t in XER_TABLES}
        self.created: dict[str, set[str]] = {t: set() for t in XER_TABLES}  # xer ids inserted, not matched
        self.touched: dict[str, set[int]] = {"TASKPRED": set(), "TASKRSRC": set()}  # db ids matched or inserted
        self.stats: dict[str, dict[str, int]] = {}
        self.warnings: list[str] = []
        self.now = _now_str(con)
        self._upd: list[list] = []  # buffered UPDATEs: [sql, [params, ...]] runs in statement order (flush)
        self._upd_sql: dict[tuple, Optional[tuple]] = {}  # (table, row keys, where keys) -> (sql, row columns)

    def count(self, table: str, what: str, n: int = 1) -> None:
        self.stats.setdefault(table, {}).setdefault(what, 0)
        self.stats[table][what] += n

    def counts(self) -> dict[str, dict[str, int]]:
        """stats with every IMPORT_ACTIONS key per table (0 when none), then any other key (PROJPROP's schedule
        options action)."""
        return {t: {**{a: v.get(a, 0) for a in IMPORT_ACTIONS}, **{k: n for k, n in v.items() if k not in IMPORT_ACTIONS}}
                for t, v in self.stats.items()}

    def prep(self, table: str, xrow: dict) -> dict:
        """XER row -> database row: ids remapped, foreign ids not in the XER cleared."""
        out = {}
        for k, v in xrow.items():
            if k in XER_SKIP_COLS or k == XER_PK.get(table):
                continue
            if k in XER_FK:
                v = self.idmap[XER_FK[k]].get(v) if v is not None else None
            elif k.endswith("_id") and k != "fk_id":
                v = None
            out[k] = v
        memo = XER_MEMO_COLS.get(table)
        if isinstance(out.get(memo), str) and _col_type(self.con, table, memo) == "BLOB":
            out[memo] = _memo_blob(out[memo])
        return out

    def can(self, table: str) -> bool:
        """True when this database can take the rows of table: the table exists and, for a table with its own
        id, NEXTKEY has its counter. Otherwise its rows are skipped with one warning."""
        n = len(self.x.get(table) or ())
        if not _cols(self.con, table):
            why = _t("{table} is not in this database: {n} XER row(s) not imported.", table=table, n=n)
        elif table in XER_PK and not _has_next_key(self.con, table, XER_PK[table]):
            why = _t("{table} has no NEXTKEY counter in this database: {n} XER row(s) not imported.", table=table, n=n)
        else:
            return True
        if self.x.get(table):
            self.warnings.append(why)
            self.count(table, "skipped_unmapped", 0)
        return False

    def children(self, table: str, rows: list[dict]) -> None:
        """Project rows with their own id (risks, notebooks, steps, expenses): ids mapped, then in update mode
        matched on XER_CHILD_KEYS (each database row once, lowest id first) and updated, else inserted. A row
        whose XER_CHILD_NEEDS ids do not map is skipped_unmapped."""
        if not rows or not self.can(table):
            return
        pk, keys, used = XER_PK[table], XER_CHILD_KEYS[table], set()
        for r in rows:
            row = self.prep(table, r)
            if any(row.get(c) is None for c in XER_CHILD_NEEDS[table]):
                self.count(table, "skipped_unmapped")
                continue
            hit = None
            if self.mode == "update":
                where = " AND ".join(f"{c} IS ?" for c in keys)
                hit = next((h[0] for h in self.con.execute(
                    f"SELECT {pk} FROM {table} WHERE {where}" + _alive(self.con, table) + f" ORDER BY {pk}",
                    [row.get(c) for c in keys]) if h[0] not in used), None)
            if hit:
                self.upd_row(table, hit, r)
            else:
                hit = self.new_row(table, r)
            self.idmap[table][r.get(pk)] = hit
            used.add(hit)

    def new_row(self, table: str, xrow: dict, extra: Optional[dict] = None) -> int:
        row = self.prep(table, xrow)
        row.update(extra or {})
        new_id = _next_key(self.con, table, XER_PK[table])
        row[XER_PK[table]] = new_id
        if "guid" in _cols(self.con, table):
            row["guid"] = _guid()
        row.update(_stamp_cols(self.con, table, self.now, create=True))
        _insert(self.con, table, row)
        self.count(table, "inserted")
        return new_id

    def upd_row(self, table: str, db_id: int, xrow: dict, extra: Optional[dict] = None) -> None:
        row = self.prep(table, xrow)
        row.update(extra or {})
        row.update(_stamp_cols(self.con, table, self.now, create=False))
        self.update(table, {XER_PK[table]: db_id}, row)
        self.count(table, "updated")

    def update(self, table: str, where: dict, row: dict) -> None:
        """_update, buffered: the same statement, sent by flush() with executemany (consecutive UPDATEs with
        the same SQL in one call, statement order kept). Call flush() before reading what they change."""
        k = (table, tuple(row), tuple(where))
        plan = self._upd_sql.get(k, False)
        if plan is False:
            cols = [c for c in row if c in _cols(self.con, table) and c not in where]
            plan = self._upd_sql[k] = (f"UPDATE {table} SET {', '.join(c + '=?' for c in cols)} WHERE "
                                       + " AND ".join(c + "=?" for c in where), cols) if cols else None
        if plan is None:
            return
        params = [*(row[c] for c in plan[1]), *where.values()]
        if self._upd and self._upd[-1][0] == plan[0]:
            self._upd[-1][1].append(params)
        else:
            self._upd.append([plan[0], [params]])

    def flush(self) -> None:
        """Send the buffered UPDATEs (update())."""
        for sql, rows in self._upd:
            self.con.executemany(sql, rows)
        self._upd = []

    def index(self, sql: str, args=()) -> dict:
        """{key: [ids in query order]} of the rows (id, *key) of sql, read once for the match lookups (find)."""
        out: dict = {}
        for r in self.con.execute(sql, args):
            out.setdefault(tuple(r[1:]), []).append(r[0])
        return out

    def find(self, index: dict, key: tuple, sql: str, args, covered: bool = True) -> Optional[int]:
        """The id sql (the match query, its first row) gives for key, from index: one candidate is the answer,
        several run sql itself (after flush) so the database picks as before, and so does a key index does not
        cover (covered=False). index must hold every live row sql can match for a covered key, rows inserted
        since it was read included; a NULL compared with = matches nothing."""
        if None in key:
            return None
        ids = index.get(key, ())
        if covered and len(ids) < 2:
            return ids[0] if ids else None
        self.flush()
        hit = self.con.execute(sql, args).fetchone()
        return hit[0] if hit else None

    def match_or_create(self, table: str, rows: list[dict], key, find_sql: Optional[str],
                        parent_col: Optional[str] = None) -> None:
        """Reuse the database row found by find_sql(key(row)), else insert (always when find_sql is None).
        Rows referring to a parent in the same table are processed after their parent."""
        pk = XER_PK[table]
        pending = list(rows)
        while pending:
            progressed, later = False, []
            for r in pending:
                par = r.get(parent_col) if parent_col else None
                if par is not None and par not in self.idmap[table] and \
                        any(p[pk] == par for p in pending if p is not r):
                    later.append(r)
                    continue
                hit = find_sql and self.con.execute(find_sql + _alive(self.con, table), key(r)).fetchone()
                if hit:
                    self.idmap[table][r[pk]] = hit[0]
                    self.count(table, "matched")
                else:
                    self.idmap[table][r[pk]] = self.new_row(table, r)
                    self.created[table].add(r[pk])
                progressed = True
            if not progressed:  # parent cycle: insert the rest without parents
                for r in later:
                    self.idmap[table][r[pk]] = self.new_row(table, r)
                    self.created[table].add(r[pk])
                break
            pending = later

    def match_global(self, table: str, rows: list[dict], parent_col: Optional[str] = None) -> None:
        keys, sql = XER_MATCH[table]
        par = XER_MATCH_PARENT.get(table)  # matched under the parent the import mapped (None at the top)

        def key(r):
            return [self.idmap[table].get(r.get(k)) if k == par else r.get(k) for k in keys]
        self.match_or_create(table, rows, key, sql, parent_col or par)


def _col_type(con, table: str, col: str) -> Optional[str]:
    """Declared type of a column, upper case (None when the column does not exist)."""
    return next((str(r[2]).upper() for r in con.execute(f"PRAGMA table_info({table})") if r[1].lower() == col), None)


def _memo_blob(text: str) -> bytes:
    """Memo text as P6 stores it in a BLOB column: UTF-8 with a BOM, then a NUL (_blob_text reads it back)."""
    return b"\xef\xbb\xbf" + text.encode("utf-8") + b"\x00"


def _has_next_key(con, table: str, id_col: str) -> bool:
    return con.execute("SELECT 1 FROM NEXTKEY WHERE lower(key_name) = lower(?)", (f"{table}_{id_col}",)).fetchone() \
        is not None


def _import_target(con, mode: str, project: Optional[str], xp: dict) -> Optional[dict]:
    """PROJECT row (lowercase keys) that an update or baseline import applies to; None in mode new."""
    if mode == "new":
        return None
    name = project or (xp["proj_short_name"] if mode == "update" else None)
    if not name:
        raise ValueError(_t("baseline mode needs project= (the project the baseline belongs to)"))
    return _project_row(con, name)


def _removable(remove_missing, mode: str) -> list[str]:
    rm = [str(t).upper() for t in ([remove_missing] if isinstance(remove_missing, str) else remove_missing or [])]
    if "TASK" in rm:
        raise ValueError(_t("Activities are never deleted by an import: delete them in P6 (remove_missing takes "
                            "{allowed}).", allowed=list(get_args(Removable))))
    bad = [t for t in rm if t not in get_args(Removable)]
    if bad:
        raise ValueError(_t("remove_missing takes {allowed}, not {bad}", allowed=list(get_args(Removable)), bad=bad))
    if rm and mode != "update":
        raise ValueError(_t("remove_missing only applies to mode='update'"))
    return list(dict.fromkeys(rm))


def _missing_rows(imp: _XerImport, pid: int) -> tuple[dict, dict[str, list[int]]]:
    """Live rows of project pid that the update did not match or create: activities (never
    removed), relationships with both activities in pid (links to other projects are never
    touched) and assignments. Rows of an activity missing from the XER are counted apart
    (on_missing_activities) and never removed. Returns (report, removable ids per table)."""
    con, al = imp.con, functools.partial(_alive, imp.con)
    seen = set(imp.idmap["TASK"].values())
    acts = [r for r in con.execute("SELECT task_id, task_code, task_name, status_code FROM TASK WHERE proj_id = ?"
                                   + al("TASK") + " ORDER BY task_code, task_id", (pid,)) if r["task_id"] not in seen]
    gone = {r["task_id"] for r in acts}
    links = [r for r in con.execute(
        "SELECT r.task_pred_id, r.task_id, r.pred_task_id, r.pred_type, r.lag_hr_cnt, p.task_code AS pred_code, "
        "s.task_code AS succ_code FROM TASKPRED r JOIN TASK s ON s.task_id = r.task_id JOIN TASK p ON p.task_id = "
        "r.pred_task_id WHERE s.proj_id = ? AND p.proj_id = ?" + al("TASKPRED", "r") + al("TASK", "s") + al("TASK", "p")
        + " ORDER BY r.task_pred_id", (pid, pid)) if r["task_pred_id"] not in imp.touched["TASKPRED"]]
    rels = [r for r in links if not {r["task_id"], r["pred_task_id"]} & gone]
    asg = [r for r in con.execute(
        "SELECT a.taskrsrc_id, a.task_id, t.task_code, rs.rsrc_short_name FROM TASKRSRC a JOIN TASK t ON t.task_id = "
        "a.task_id" + al("TASK", "t") + " LEFT JOIN RSRC rs ON rs.rsrc_id = a.rsrc_id WHERE a.proj_id = ?"
        + al("TASKRSRC", "a") + " ORDER BY a.taskrsrc_id", (pid,)) if r["taskrsrc_id"] not in imp.touched["TASKRSRC"]]
    kept = [r for r in asg if r["task_id"] not in gone]
    report = {
        "TASK": {"count": len(acts), "sample": [f"{r['task_code']} {r['task_name']} ({r['status_code']})" for r in acts[:15]],
                 "note": _t("activities are never deleted by an import: delete them in P6 if they are obsolete")},
        "TASKPRED": {"count": len(rels), "by_type": dict(Counter(r["pred_type"] for r in rels)),
                     "sample": [f"{r['pred_code']} -> {r['succ_code']} {REL_TYPES_REV.get(r['pred_type'], r['pred_type'])}"
                                f" lag {float(r['lag_hr_cnt'] or 0):g} h" for r in rels[:15]],
                     "on_missing_activities": len(links) - len(rels)},
        "TASKRSRC": {"count": len(kept), "sample": [f"{r['task_code']} {r['rsrc_short_name'] or '(no resource)'}"
                                                    for r in kept[:15]], "on_missing_activities": len(asg) - len(kept)}}
    return report, {"TASKPRED": [r["task_pred_id"] for r in rels], "TASKRSRC": [r["taskrsrc_id"] for r in kept]}


def _remove_rows(imp: _XerImport, table: str, ids: list[int]) -> None:
    """Delete TASKPRED or TASKRSRC rows by id (P6 delete triggers log them); an assignment's UDF
    values and cost, hour and code rows go first."""
    con = imp.con
    if table == "TASKRSRC":
        deps = [("UDFVALUE", f"fk_id IN {_ids_sql(ids)} AND udf_type_id IN (SELECT udf_type_id FROM UDFTYPE "
                             "WHERE table_name = 'TASKRSRC')")] if _cols(con, "UDFTYPE") else []
        deps += [(t, f"taskrsrc_id IN {_ids_sql(ids)}") for t in ("TRSRCFIN", "RSRCHOUR", "ASGNMNTACAT")
                 if "taskrsrc_id" in _cols(con, t)]
        for t, where in deps:
            n = con.execute(f"DELETE FROM {t} WHERE {where}").rowcount
            if n:
                imp.count(t, "deleted", n)
    imp.count(table, "deleted", con.execute(f"DELETE FROM {table} WHERE {XER_PK[table]} IN {_ids_sql(ids)}").rowcount)


def _upsert(imp: _XerImport, table: str, row: dict, key: tuple, known: Optional[tuple] = None) -> None:
    """Update the row with these key values (a soft-deleted one comes back), else insert it. known = (covers,
    keys): the key tuples present in the table for every key covers(k) accepts, read once (inserts added);
    another key is looked up in the table. The UPDATE is buffered (imp.update: flush afterwards)."""
    where = {k: row[k] for k in key}
    k = tuple(where.values())
    if known is not None and known[0](k):
        hit = k in known[1]
    else:
        hit = imp.con.execute(f"SELECT 1 FROM {table} WHERE " + " AND ".join(c + "=?" for c in key), k).fetchone()
    if hit:
        revive = {c: None for c in ("delete_session_id", "delete_date") if c in _cols(imp.con, table)}  # a soft-deleted row comes back
        imp.update(table, where, {**row, **revive, **_stamp_cols(imp.con, table, imp.now, create=False)})
        imp.count(table, "updated")
    else:
        imp.flush()
        _insert(imp.con, table, {**row, **_stamp_cols(imp.con, table, imp.now, create=True)})
        imp.count(table, "inserted")
        if known is not None and known[0](k):
            known[1].add(k)


def _export_target(path: str, ext: str = ".xer") -> str:
    """Absolute export path: a file ending with ext (.xer, .xlsx) in an existing folder (never the
    database, a backup or a log)."""
    if not path or not path.lower().endswith(ext):
        raise ValueError(_t("Export path must end with {ext}: {path!r}", ext=ext, path=path))
    path = os.path.abspath(path)
    if not os.path.isdir(os.path.dirname(path)):
        raise ValueError(_t("Folder not found: {folder!r}", folder=os.path.dirname(path)))
    if os.path.isdir(path):
        raise ValueError(_t("{path!r} is a folder", path=path))
    return path


def _export_xer(con: sqlite3.Connection, project: str, path: str, tables: list[str],
                xer_version: Optional[str]) -> dict:
    pid = _project_id(con, project)
    al = lambda t: _alive(con, t)  # noqa: E731
    one = lambda sql: {r[0] for r in con.execute(sql) if r[0] is not None}  # noqa: E731
    has = lambda t, *c: set(c) <= _cols(con, t)  # noqa: E731  (tables and columns vary between P6 versions)

    def tree(table: str, pk: str, parent: str, start: set) -> set:
        """start plus the parents of its rows (roles, risk types)."""
        got = set(start)
        while has(table, pk, parent):
            more = one(f"SELECT {parent} FROM {table} WHERE {pk} IN {_ids_sql(got)}") - got
            if not more:
                break
            got |= more
        return got

    tasks = one(f"SELECT task_id FROM TASK WHERE proj_id = {pid}{al('TASK')}")
    trsrc = one(f"SELECT taskrsrc_id FROM TASKRSRC WHERE proj_id = {pid}{al('TASKRSRC')}")
    wbs = one(f"SELECT wbs_id FROM PROJWBS WHERE proj_id = {pid}{al('PROJWBS')}")
    risks = one(f"SELECT risk_id FROM PROJRISK WHERE proj_id = {pid}{al('PROJRISK')}") \
        if has("PROJRISK", "risk_id", "proj_id") else set()
    steps = one(f"SELECT proc_id FROM TASKPROC WHERE task_id IN {_ids_sql(tasks)}{al('TASKPROC')}") \
        if has("TASKPROC", "proc_id", "task_id") else set()
    costs = one(f"SELECT cost_item_id FROM PROJCOST WHERE proj_id = {pid}{al('PROJCOST')}") \
        if has("PROJCOST", "cost_item_id", "proj_id") else set()
    rsrc = one(f"SELECT rsrc_id FROM TASKRSRC WHERE proj_id = {pid}{al('TASKRSRC')}") | \
        one(f"SELECT rsrc_id FROM TASK WHERE proj_id = {pid}{al('TASK')}") | \
        (one(f"SELECT rsrc_id FROM PROJRISK WHERE risk_id IN {_ids_sql(risks)}") if has("PROJRISK", "rsrc_id") else set())
    while True:  # parents of used resources
        more = one(f"SELECT parent_rsrc_id FROM RSRC WHERE rsrc_id IN {_ids_sql(rsrc)}") - rsrc
        if not more:
            break
        rsrc |= more
    cals = one(f"SELECT clndr_id FROM PROJECT WHERE proj_id = {pid}") | \
        one(f"SELECT clndr_id FROM TASK WHERE proj_id = {pid}{al('TASK')}") | \
        one(f"SELECT clndr_id FROM RSRC WHERE rsrc_id IN {_ids_sql(rsrc)}") | \
        one(f"SELECT clndr_id FROM CALENDAR WHERE proj_id = {pid}{al('CALENDAR')}")
    while True:
        more = one(f"SELECT base_clndr_id FROM CALENDAR WHERE clndr_id IN {_ids_sql(cals)}") - cals
        if not more:
            break
        cals |= more
    types = one(f"SELECT actv_code_type_id FROM TASKACTV WHERE task_id IN {_ids_sql(tasks)}") | \
        one(f"SELECT actv_code_type_id FROM ACTVTYPE WHERE proj_id = {pid}{al('ACTVTYPE')}")
    obs = one(f"SELECT obs_id FROM PROJWBS WHERE proj_id = {pid}{al('PROJWBS')}")
    while True:
        more = one(f"SELECT parent_obs_id FROM OBS WHERE obs_id IN {_ids_sql(obs)}") - obs
        if not more:
            break
        obs |= more
    roles = tree("ROLES", "role_id", "parent_role_id",
                 (one(f"SELECT role_id FROM TASKRSRC WHERE proj_id = {pid}{al('TASKRSRC')}")
                  if has("TASKRSRC", "role_id") else set())
                 | (one(f"SELECT role_id FROM RSRC WHERE rsrc_id IN {_ids_sql(rsrc)}") if has("RSRC", "role_id") else set()))
    risk_types = tree("RISKTYPE", "risk_type_id", "parent_risk_type_id",
                      one(f"SELECT risk_type_id FROM PROJRISK WHERE risk_id IN {_ids_sql(risks)}")
                      if has("PROJRISK", "risk_type_id") else set())
    udf_where = (f"(u.table_name = 'TASK' AND v.fk_id IN {_ids_sql(tasks)}) OR "
                 f"(u.table_name = 'PROJWBS' AND v.fk_id IN {_ids_sql(wbs)}) OR "
                 f"(u.table_name = 'TASKRSRC' AND v.fk_id IN {_ids_sql(trsrc)}) OR "
                 f"(u.table_name = 'RSRC' AND v.fk_id IN {_ids_sql(rsrc)}) OR "
                 f"(u.table_name = 'TASKPROC' AND v.fk_id IN {_ids_sql(steps)}) OR "
                 f"(u.table_name = 'PROJCOST' AND v.fk_id IN {_ids_sql(costs)}) OR "
                 f"(u.table_name = 'PROJRISK' AND v.fk_id IN {_ids_sql(risks)}) OR "
                 f"(u.table_name = 'PROJECT' AND v.fk_id = {pid})")
    udf_types = one(f"SELECT v.udf_type_id FROM UDFVALUE v JOIN UDFTYPE u ON u.udf_type_id = v.udf_type_id "
                    f"WHERE {udf_where}")

    where = {
        "CURRTYPE": "1=1",
        "FINTMPL": f"fintmpl_id IN (SELECT fintmpl_id FROM PROJECT WHERE proj_id = {pid})",
        "OBS": f"obs_id IN {_ids_sql(obs)}",
        "UDFTYPE": f"udf_type_id IN {_ids_sql(udf_types)}",
        "PROJECT": f"proj_id = {pid}",
        "CALENDAR": f"clndr_id IN {_ids_sql(cals)}",
        "PROJWBS": f"proj_id = {pid}",
        "RSRC": f"rsrc_id IN {_ids_sql(rsrc)}",
        "ACTVTYPE": f"actv_code_type_id IN {_ids_sql(types)}",
        "RSRCRATE": f"rsrc_id IN {_ids_sql(rsrc)}",
        "TASK": f"proj_id = {pid}",
        "ACTVCODE": f"actv_code_type_id IN {_ids_sql(types)}",
        "TASKPRED": f"task_id IN {_ids_sql(tasks)} AND pred_task_id IN {_ids_sql(tasks)}",
        "TASKRSRC": f"proj_id = {pid}",
        "TASKACTV": f"task_id IN {_ids_sql(tasks)}",
        "UDFVALUE": f"EXISTS (SELECT 1 FROM UDFTYPE u WHERE u.udf_type_id = v.udf_type_id AND ({udf_where}))",
        "MEMOTYPE": f"memo_type_id IN (SELECT memo_type_id FROM TASKMEMO WHERE task_id IN {_ids_sql(tasks)})"
                    if has("TASKMEMO", "memo_type_id", "task_id") else "0=1",
        "TASKMEMO": f"task_id IN {_ids_sql(tasks)}",
        "TASKPROC": f"proc_id IN {_ids_sql(steps)}",
        "COSTTYPE": f"cost_type_id IN (SELECT cost_type_id FROM PROJCOST WHERE cost_item_id IN {_ids_sql(costs)})"
                    if has("PROJCOST", "cost_type_id") else "0=1",
        "PROJCOST": f"cost_item_id IN {_ids_sql(costs)}",
        "ROLES": f"role_id IN {_ids_sql(roles)}",
        "ROLERATE": f"role_id IN {_ids_sql(roles)}",
        "RISKTYPE": f"risk_type_id IN {_ids_sql(risk_types)}",
        "PROJRISK": f"risk_id IN {_ids_sql(risks)}",
        "TASKRISK": f"task_id IN {_ids_sql(tasks)} AND risk_id IN {_ids_sql(risks)}",
    }
    if xer_version is None:
        try:
            ver = con.execute("SELECT * FROM PREFER").fetchone() or ()
            ver = next((str(v) for v in ver if isinstance(v, str) and re.fullmatch(r"\d\d\.\d+\.\d+", v)), "")
            xer_version = ".".join(ver.split(".")[:2]) or "24.12"
        except sqlite3.Error:
            xer_version = "24.12"
    base_curr = con.execute("SELECT curr_short_name FROM CURRTYPE WHERE base_exch_rate = 1"
                            + al("CURRTYPE")).fetchone()
    lines = ["\t".join(["ERMHDR", xer_version, datetime.now().strftime("%Y-%m-%d"), "Project",
                        _state.P6_USER, _state.P6_USER, "dbxDatabaseNoName", "Project Management",
                        base_curr[0] if base_curr else "USD"])]
    counts, lossy, warn = {}, {}, []
    for t in tables:
        if t == "SCHEDOPTIONS":
            vals, w = _xer_sched_export(con, pid, tasks)
            warn += w
            if vals is not None:
                counts[t] = 1
                lines += [f"%T\t{t}", "%F\t" + "\t".join(SCHED_XER_COLS),
                          "%R\t" + "\t".join(_xer_encode(v) for v in vals)]
            continue
        cols = [r[1].lower() for r in con.execute(f"PRAGMA table_info({t})")
                if r[1].lower() not in ("delete_session_id", "delete_date")]
        if not cols:
            warn.append(_t("{table} is not in this database: not exported.", table=t))
            continue
        alias = " v" if t == "UDFVALUE" else ""
        rows = con.execute(f"SELECT {', '.join(cols)} FROM {t}{alias} WHERE {where[t]}"
                           + _alive(con, t, alias.strip())).fetchall()
        counts[t] = len(rows)
        lines.append(f"%T\t{t}")
        lines.append("%F\t" + "\t".join(cols))
        key = cols.index("task_code") if "task_code" in cols else 0
        for r in rows:
            vals = [_xer_encode(v) for v in r]
            if not all(_cp1252_ok(v) for v in vals):
                lossy.setdefault(t, []).append(vals[key])
            lines.append("%R\t" + "\t".join(vals))
    lines.append("%E")
    tmp = path + ".tmp"
    try:  # written beside the target, then swapped in: a failed export leaves the old file intact
        with open(tmp, "w", encoding="cp1252", errors="replace", newline="") as fh:
            fh.write("\r\n".join(lines) + "\r\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    for t, keys in lossy.items():
        warn.append(_t("{table}: {n} row(s) with characters outside cp1252, written as '?' (first: {first}).",
                       table=t, n=len(keys), first=", ".join(keys[:10])))
    deps = {"TASK": ["PROJWBS", "CALENDAR"], "TASKPRED": ["TASK"], "TASKRSRC": ["TASK", "RSRC"],
            "TASKACTV": ["TASK", "ACTVTYPE", "ACTVCODE"], "UDFVALUE": ["UDFTYPE"],
            "PROJWBS": ["PROJECT"], "RSRCRATE": ["RSRC"], "ACTVCODE": ["ACTVTYPE"],
            "TASKMEMO": ["TASK", "MEMOTYPE"], "TASKPROC": ["TASK"], "PROJCOST": ["TASK", "COSTTYPE"],
            "ROLERATE": ["ROLES"], "PROJRISK": ["RISKTYPE"], "TASKRISK": ["TASK", "PROJRISK"]}
    for t, needs in deps.items():
        miss = [n for n in needs if t in tables and n not in tables]
        if miss:
            warn.append(_t("{table} exported without {missing}: P6 may not resolve those references on import.",
                           table=t, missing=miss))
    return {"path": path, "xer_version": xer_version, "rows": counts, "warnings": warn}


def _cp1252_ok(v: str) -> bool:
    try:
        v.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False
