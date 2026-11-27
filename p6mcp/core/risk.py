"""Schedule risk: duration ranges of the Monte Carlo runs and percentiles."""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence

from p6mcp.core.constants import UDF_KINDS, UDF_VALUE
from p6mcp.registry import RiskRange
from p6mcp.core.db import _alive, _num
from p6mcp.core.wbs_codes import _code_filter, _name_sep, _wbs_tree
from p6mcp.core.read import _udf_type
from p6mcp.core.engine import _Cpm


# ----------------------------------------------------------------- schedule risk (Monte Carlo on the CPM engine)

RISK_DEFAULT = (85.0, 100.0, 135.0)  # triangular min, mode, max in % of the remaining duration
RISK_ITERATIONS = (100, 5000)  # iterations asked are clamped to this range
RISK_WORK = 1_500_000  # at most iterations x activities recomputed per iteration (a run stays near 30 s)
RISK_SOURCES = ("udf", "task", "code", "wbs", "default")  # where an activity's range comes from, by precedence
RISK_CAVEAT = ("An estimate built on assumed duration ranges, not a forecast: each open activity's remaining duration "
               "is drawn on its own (no correlation between activities is modelled), discrete risk events, resource "
               "limits and changes of logic are not modelled, and the network, calendars and constraints are those of "
               "the last F9.")


def _risk_pcts(item: Any, where: str) -> tuple[float, float, float]:
    try:
        v = tuple(float(item[k]) for k in ("min_pct", "mode_pct", "max_pct"))
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{where}: min_pct, mode_pct and max_pct are required numbers") from None
    if not all(math.isfinite(x) for x in v) or not 0 <= v[0] <= v[1] <= v[2]:
        raise ValueError(f"{where}: need 0 <= min_pct <= mode_pct <= max_pct (percent of the remaining duration)")
    return v


def _risk_ranges(con, pid: int, cpm: _Cpm, ranges: Optional[Sequence]) -> tuple[dict[int, tuple], list[dict]]:
    """({task_id: (source, pcts, range index)} by precedence task > code > wbs (deepest node, then the earlier
    range), the ranges as understood with the activities each one matches)."""
    if ranges is None:
        return {}, []
    if not isinstance(ranges, (list, tuple)):
        raise ValueError("ranges: a list of ranges")
    keys, nodes, seen = set(RiskRange.__annotations__), None, set()
    hits: dict[int, list] = {}  # task_id -> [(rank, index, source, pcts)]
    echo = []
    for i, r in enumerate(ranges):
        where = f"ranges[{i}]"
        if not isinstance(r, Mapping):
            raise ValueError(f"{where}: an object with min_pct, mode_pct, max_pct and one selector")
        if set(r) - keys:
            raise ValueError(f"{where}: unknown keys {sorted(set(r) - keys)}; keys: {sorted(keys)}")
        sel = [k for k in ("task_code", "wbs", "code_type") if r.get(k)]
        if len(sel) != 1:
            raise ValueError(f"{where}: give exactly one of task_code, wbs or code_type (with code_value)")
        pcts = _risk_pcts(r, where)
        if sel[0] == "task_code":
            code = str(r["task_code"])
            if code not in cpm.code:
                raise ValueError(f"{where}: activity {code} not found in project {cpm.prj['proj_short_name']}")
            if code in seen:
                raise ValueError(f"{where}: activity {code} has two ranges")
            seen.add(code)
            ids, rank, src = {cpm.code[code]}, (0, 0), "task"
        elif sel[0] == "wbs":
            sep = _name_sep(con, pid)
            nodes = nodes if nodes is not None else _wbs_tree(con, pid, sep)
            if not nodes:
                raise ValueError(f"{where}: the project has no project WBS node")
            top, p = nodes[0]["wbs_code"], str(r["wbs"]).strip()
            k = next((j for j, n in enumerate(nodes) if n["wbs_code"] in (p, top + sep + p)), None)
            if k is None:
                raise ValueError(f"{where}: WBS code {r['wbs']!r} not found (p6_list_wbs lists the codes)")
            d0 = nodes[k]["depth"]
            end = next((j for j in range(k + 1, len(nodes)) if nodes[j]["depth"] <= d0), len(nodes))
            sub = {n["wbs_id"] for n in nodes[k:end]}
            ids, rank, src = {t for t, d in cpm.t.items() if d["wbs_id"] in sub}, (2, -d0), "wbs"
        else:
            if not r.get("code_value"):
                raise ValueError(f"{where}: code_type needs code_value")
            cond, cargs, _ = _code_filter(con, pid, r["code_type"], r["code_value"], True)
            ids = {x[0] for x in con.execute("SELECT t.task_id FROM TASK t WHERE t.proj_id = ?" + _alive(con, "TASK", "t")
                                             + cond, [pid, *cargs])}
            rank, src = (1, 0), "code"
        for t in ids:
            hits.setdefault(t, []).append((rank, i, src, pcts))
        echo.append({**{k: r[k] for k in ("task_code", "wbs", "code_type", "code_value") if r.get(k)},
                     "min_pct": pcts[0], "mode_pct": pcts[1], "max_pct": pcts[2], "matched": len(ids), "applied": 0})
    best = {}
    for t, hs in hits.items():
        rank, i, src, pcts = min(hs, key=lambda h: (h[0], h[1]))
        best[t] = (src, pcts, i)
    return best, echo


def _risk_udfs(con, pid: int, labels: Sequence[str]) -> tuple[dict[int, tuple], dict]:
    """({task_id: (optimistic, most likely, pessimistic) days} of the activities with three valid values,
    {labels, activities_with_values, invalid}) from three number UDFs of activities."""
    types = [_udf_type(con, "TASK", u) for u in labels]
    for y in types:
        if UDF_VALUE.get(y["logical_data_type"]) != "udf_number":
            raise ValueError(f"UDF {y['udf_type_label']!r} holds {UDF_KINDS.get(y['logical_data_type'])}, not a number "
                             "of days")
    vals: dict[int, list] = {}
    for k, y in enumerate(types):
        for tid, v in con.execute("SELECT v.fk_id, v.udf_number FROM UDFVALUE v JOIN TASK t ON t.task_id = v.fk_id "
                                  "WHERE v.udf_type_id = ? AND t.proj_id = ?" + _alive(con, "UDFVALUE", "v")
                                  + _alive(con, "TASK", "t"), (y["udf_type_id"], pid)):
            if _num(v) is not None:
                vals.setdefault(tid, [None] * 3)[k] = float(v)
    good = {t: tuple(v) for t, v in vals.items() if None not in v and 0 <= v[0] <= v[1] <= v[2]}
    return good, {"optimistic": types[0]["udf_type_label"], "most_likely": types[1]["udf_type_label"],
                  "pessimistic": types[2]["udf_type_label"], "activities_with_values": len(good),
                  "invalid": sum(1 for t in vals if t not in good)}


def _pctl(vals: list, p: float):
    """Nearest-rank percentile of a sorted list."""
    return vals[max(0, math.ceil(p / 100 * len(vals)) - 1)]
