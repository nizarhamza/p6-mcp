"""WBS tree and activity aggregates, EPS tree, activity code types and filters."""

from __future__ import annotations

import base64
import uuid
from typing import Mapping, Optional

from p6mcp.core.constants import ACT_LINKS_CAP, HPD_SQL, MAT_SQL, SUMMARY_TYPES
from p6mcp.core.db import _alive, _cols, _dict, _ids_sql


# ----------------------------------------------------------------- WBS tree and activity aggregates

def _name_sep(con, pid: int) -> str:
    """Code separator: PROJECT.name_sep_char, else PREFER.name_sep_char, else '.'."""
    for table, sql, args in (("PROJECT", "SELECT name_sep_char FROM PROJECT WHERE proj_id = ?", (pid,)),
                             ("PREFER", "SELECT name_sep_char FROM PREFER", ())):
        if "name_sep_char" in _cols(con, table):
            r = con.execute(sql, args).fetchone()
            if r and r[0]:
                return r[0]
    return "."


def _wbs_tree(con, pid: int, sep: Optional[str] = None) -> list[dict]:
    """WBS nodes reachable from the project node (proj_node_flag 'Y'), in P6 order: pre-order,
    siblings by seq_num then wbs_id. Keys: wbs_id, parent_wbs_id, depth (project node 0), wbs_code
    (short names joined with sep, default _name_sep), wbs_short_name, wbs_name."""
    seg = "printf('%010d.%010d', COALESCE({0}.seq_num, 0) + 1000000000, {0}.wbs_id)"  # fixed width: sorts as text
    sql = (f"WITH RECURSIVE pw AS {MAT_SQL}(SELECT wbs_id, parent_wbs_id, seq_num, proj_node_flag, wbs_short_name, "
           "wbs_name FROM PROJWBS WHERE proj_id = :pid" + _alive(con, "PROJWBS") + "), "
           "tree(wbs_id, depth, sort_key, wbs_code) AS ("
           f"SELECT wbs_id, 0, {seg.format('pw')}, wbs_short_name FROM pw WHERE proj_node_flag = 'Y' "
           f"UNION ALL SELECT c.wbs_id, t.depth + 1, t.sort_key || '/' || {seg.format('c')}, "
           "t.wbs_code || :sep || c.wbs_short_name FROM pw c JOIN tree t ON c.parent_wbs_id = t.wbs_id "
           "WHERE t.depth < 100) "
           "SELECT t.wbs_id AS wbs_id, p.parent_wbs_id AS parent_wbs_id, t.depth AS depth, t.wbs_code AS wbs_code, "
           "p.wbs_short_name AS wbs_short_name, p.wbs_name AS wbs_name FROM tree t JOIN pw p ON p.wbs_id = t.wbs_id "
           "ORDER BY t.sort_key")
    return [_dict(r) for r in con.execute(sql, {"pid": pid, "sep": sep or _name_sep(con, pid)})]


AGG_MIN, AGG_MAX = ("tf_min", "start"), ("tf_max", "finish")  # the other _act_agg_sql keys add up


def _act_agg_sql(con) -> str:
    """SELECT list of additive aggregates over TASK t (CALENDAR c for days): counts by status, open
    (not completed, SUMMARY_TYPES left out, as the analysis tools), total float of the open
    activities in days (min, max, sum, n), negative float, durations in days, start, finish,
    budget, actual and remaining units (labor plus nonlabor). Partial results combine with
    _merge_agg."""
    tf, cols = f"t.total_float_hr_cnt / {HPD_SQL}", _cols(con, "TASK")
    op = f"t.status_code <> 'TK_Complete' AND t.task_type NOT IN {SUMMARY_TYPES}"

    def qty(kind: str) -> str:
        ks = [f"COALESCE(t.{kind}_{u}_qty, 0)" for u in ("work", "equip") if f"{kind}_{u}_qty" in cols]
        return f"SUM({' + '.join(ks)})" if ks else "NULL"
    return (f"COUNT(*) AS n, SUM(t.status_code = 'TK_NotStart') AS not_started, "
            "SUM(t.status_code = 'TK_Active') AS in_progress, SUM(t.status_code = 'TK_Complete') AS completed, "
            f"SUM({op}) AS open, SUM({op} AND t.total_float_hr_cnt < 0) AS negative, "
            f"SUM({op} AND t.total_float_hr_cnt IS NOT NULL) AS tf_n, SUM(CASE WHEN {op} THEN {tf} END) AS tf_sum, "
            f"MIN(CASE WHEN {op} THEN {tf} END) AS tf_min, MAX(CASE WHEN {op} THEN {tf} END) AS tf_max, "
            f"SUM(t.target_drtn_hr_cnt / {HPD_SQL}) AS orig_d, SUM(t.remain_drtn_hr_cnt / {HPD_SQL}) AS rem_d, "
            "MIN(COALESCE(t.act_start_date, t.early_start_date)) AS start, "
            "MAX(COALESCE(t.act_end_date, t.early_end_date)) AS finish, "
            f"{qty('target')} AS budget, {qty('act')} AS actual, {qty('remain')} AS remain")


def _merge_agg(acc: Mapping, part: Mapping) -> dict:
    """Two _act_agg_sql results combined: min/max keys keep the extreme, the others add; None ignored."""
    out = dict(acc)
    for k, v in part.items():
        a = out.get(k)
        if v is None or a is None:
            out[k] = a if v is None else v
        else:
            out[k] = min(a, v) if k in AGG_MIN else max(a, v) if k in AGG_MAX else a + v
    return out


def _rollup(a: Mapping) -> dict:
    """WBS rollup fields from merged aggregates (an empty subtree gives zeros and None). units_pct
    is P6's Units % Complete: actual / at completion (actual + remaining)."""
    n = lambda k: a.get(k) or 0  # noqa: E731
    actual = float(n("actual"))
    at_completion = actual + float(n("remain"))
    return {"activities": n("n"), "open": n("open"), "not_started": n("not_started"),
            "in_progress": n("in_progress"), "completed": n("completed"),
            "min_tf_d": None if a.get("tf_min") is None else round(a["tf_min"], 2),
            "negative_float": n("negative"), "start": a.get("start"), "finish": a.get("finish"),
            "budget_units": round(float(n("budget")), 2), "actual_units": round(actual, 2),
            "units_pct": round(100 * actual / at_completion, 2) if at_completion else None}


# ----------------------------------------------------------------- activity codes

def _eps_chain(con, pid: int) -> list[int]:
    """proj_ids of the EPS nodes above a project, nearest first (depth cap 50)."""
    r = con.execute("SELECT parent_wbs_id FROM PROJWBS WHERE proj_id = ? AND proj_node_flag = 'Y'"
                    + _alive(con, "PROJWBS"), (pid,)).fetchone()
    out, wid = [], r and r[0]
    while wid is not None and len(out) < 50:
        r = con.execute("SELECT proj_id, parent_wbs_id FROM PROJWBS WHERE wbs_id = ? AND proj_node_flag = 'Y'"
                        + _alive(con, "PROJWBS"), (wid,)).fetchone()
        if r is None or r[0] in out:
            break
        out.append(r[0])
        wid = r[1]
    return out


def _code_type(con, pid: int, name: str) -> dict:
    """Activity code type called name (case ignored) that project pid sees: its own (AS_Project), else
    one of its EPS nodes (nearest first), else a global type. Raises when there is none, or when two
    share the best rank. shadowed: scopes of the other types with that name."""
    eps = _eps_chain(con, pid)

    def rank(p) -> Optional[int]:
        return 0 if p == pid else 1 + eps.index(p) if p in eps else 1000 if p is None else None
    seen = [r for r in con.execute("SELECT actv_code_type_id, actv_code_type, actv_code_type_scope, proj_id FROM ACTVTYPE "
                                   "WHERE 1=1" + _alive(con, "ACTVTYPE")) if rank(r["proj_id"]) is not None]
    hits = sorted((r for r in seen if (r["actv_code_type"] or "").lower() == str(name).strip().lower()),
                  key=lambda r: (rank(r["proj_id"]), r["actv_code_type_id"]))
    if not hits:
        raise ValueError(f"Activity code type {name!r} not found for this project; types: "
                         f"{sorted({r['actv_code_type'] for r in seen})}")
    best = [r for r in hits if rank(r["proj_id"]) == rank(hits[0]["proj_id"])]
    if len(best) > 1:
        raise ValueError(f"Several activity code types called {name!r} at the same level: ids "
                         f"{[r['actv_code_type_id'] for r in best]}")
    r = hits[0]
    return {"type": r["actv_code_type"], "type_id": r["actv_code_type_id"], "scope": r["actv_code_type_scope"],
            "shadowed": [h["actv_code_type_scope"] for h in hits[1:]]}


def _code_tree(con, type_id: int) -> dict[int, dict]:
    """Codes of a type by actv_code_id: id, short_name, name, level (1 = top), chain (ids from the
    top), path (short names joined with PREFER.name_sep_char, else '.') and sort (P6 order: seq_num,
    then id, at each level). A parent outside the type or a cycle ends the chain."""
    sep = "."
    if "name_sep_char" in _cols(con, "PREFER"):
        r = con.execute("SELECT name_sep_char FROM PREFER").fetchone()
        sep = (r and r[0]) or "."
    rows = {r["actv_code_id"]: r for r in con.execute(
        "SELECT actv_code_id, parent_actv_code_id, short_name, actv_code_name, seq_num FROM ACTVCODE "
        "WHERE actv_code_type_id = ?" + _alive(con, "ACTVCODE"), (type_id,))}
    out: dict[int, dict] = {}
    for cid, r in rows.items():
        chain, c = [], cid
        while c in rows and c not in chain and len(chain) < 50:
            chain.insert(0, c)
            c = rows[c]["parent_actv_code_id"]
        out[cid] = {"id": cid, "short_name": r["short_name"], "name": r["actv_code_name"], "level": len(chain),
                    "chain": tuple(chain), "path": sep.join(rows[c]["short_name"] or "" for c in chain),
                    "sort": tuple((rows[c]["seq_num"] or 0, c) for c in chain)}
    return out


def _code_filter(con, pid: int, code_type: Optional[str], code_value: Optional[str] = None,
                 include_children: bool = True) -> tuple[str, list, Optional[dict]]:
    """(condition on TASK t, args, code_filter info): activities with a code of code_type, any value
    when code_value is None, else the codes whose path, else short name, else name equals code_value
    (case ignored, every match kept: 'MEC' finds CON.MEC and COM.MEC) and, with include_children,
    their descendants. ('', [], None) without code_type."""
    if not code_type:
        if code_value is not None:
            raise ValueError("code_value needs code_type (the activity code type, e.g. 'Discipline')")
        return "", [], None
    info = _code_type(con, pid, code_type)
    tree = _code_tree(con, info["type_id"])
    order = lambda ids: sorted(ids, key=lambda i: tree[i]["sort"])  # noqa: E731
    ids: set = set()
    if code_value is not None:
        v, hit = str(code_value).strip().lower(), []
        for key in ("path", "short_name", "name"):
            hit = order(c["id"] for c in tree.values() if (c[key] or "").lower() == v)
            if hit:
                break
        if not hit:
            raise ValueError(f"No {info['type']} code {code_value!r}; codes: {[tree[i]['path'] for i in order(tree)][:20]}")
        ids = set(hit)
        kids = {c["id"] for c in tree.values() if ids & set(c["chain"][:-1])} if include_children else set()
        info.update(value=code_value, include_child_codes=bool(include_children), matched=[tree[i]["path"] for i in hit],
                    children=[tree[i]["path"] for i in order(kids - ids)][:ACT_LINKS_CAP])
        ids |= kids
    info["codes"] = len(ids) if code_value is not None else len(tree)
    sql = (" AND EXISTS (SELECT 1 FROM TASKACTV ta WHERE ta.task_id = t.task_id AND ta.actv_code_type_id = ?"
           + (f" AND ta.actv_code_id IN {_ids_sql(ids)}" if code_value is not None else "")
           + _alive(con, "TASKACTV", "ta") + ")")
    return sql, [info["type_id"]], info


# ----------------------------------------------------------------- EPS

def _eps_nodes(con) -> list[dict]:
    return [_dict(r) for r in con.execute(
        "SELECT w.wbs_id AS wbs_id, w.parent_wbs_id AS parent_wbs_id, w.wbs_short_name AS eps_id, "
        "w.wbs_name AS eps_name, w.proj_id AS proj_id "
        "FROM PROJWBS w JOIN PROJECT p ON p.proj_id = w.proj_id "
        "WHERE p.project_flag = 'N' AND w.proj_node_flag = 'Y'"
        + _alive(con, "PROJWBS", "w") + _alive(con, "PROJECT", "p")
        + " ORDER BY w.seq_num, w.wbs_short_name, w.wbs_id")]


def _eps_tree(con) -> list[dict]:
    """EPS nodes in tree order (each node before its children, siblings in _eps_nodes order) with depth (0 =
    top level) and path (names from the top, joined with ' > '). A node whose parent is not an EPS node is a
    top-level node; a cycle is cut (depth cap 50)."""
    nodes = _eps_nodes(con)
    ids = {n["wbs_id"] for n in nodes}
    kids: dict = {}
    for n in nodes:
        kids.setdefault(n["parent_wbs_id"] if n["parent_wbs_id"] in ids else None, []).append(n)
    out: list[dict] = []
    seen: set = set()

    def walk(parent, depth: int, path: list[str]) -> None:
        for n in kids.get(parent, []):
            if n["wbs_id"] in seen or depth > 50:
                continue
            seen.add(n["wbs_id"])
            p = path + [n["eps_name"] or n["eps_id"]]
            out.append({**n, "depth": depth, "path": " > ".join(p)})
            walk(n["wbs_id"], depth + 1, p)
    walk(None, 0, [])
    return out


def _eps_wbs_id(con, eps: Optional[str]) -> int:
    nodes = _eps_nodes(con)
    if not nodes:
        raise ValueError("No EPS node in this database. Create one in P6 or with p6_create_eps "
                         "(p6_import_xer: create_eps=).")
    if eps is None:
        if len(nodes) == 1:
            return nodes[0]["wbs_id"]
        raise ValueError(f"Several EPS nodes exist, choose one with eps=: {[n['eps_id'] for n in nodes]}")
    for n in nodes:
        if n["eps_id"] == eps:
            return n["wbs_id"]
    raise ValueError(f"EPS {eps!r} not found: {[n['eps_id'] for n in nodes]}")


def _guid() -> str:
    """P6 style GUID: base64 of 16 random bytes, without padding."""
    return base64.b64encode(uuid.uuid4().bytes).decode()[:22]
