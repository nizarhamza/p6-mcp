"""Schedule risk: duration ranges, correlations and risk events of the Monte Carlo runs, the iterations (in this
process or in worker processes) and percentiles."""

from __future__ import annotations

import math
import multiprocessing
import os
import pickle
import random
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from statistics import NormalDist
from typing import Any, Mapping, Optional, Sequence

from p6mcp.core.constants import UDF_KINDS, UDF_VALUE
from p6mcp.registry import RiskCorrelation, RiskEvent, RiskImpact, RiskRange
from p6mcp.core.db import _alive, _cols, _num
from p6mcp.core.wbs_codes import _code_filter, _name_sep, _wbs_tree
from p6mcp.core.read import _udf_type
from p6mcp.core.engine import _Cpm, CPM_TOL
from p6mcp.messages import _t


# ----------------------------------------------------------------- schedule risk (Monte Carlo on the CPM engine)

RISK_DEFAULT = (85.0, 100.0, 135.0)  # triangular min, mode, max in % of the remaining duration
RISK_ITERATIONS = (100, 5000)  # iterations asked are clamped to this range
RISK_WORK = 1_500_000  # at most iterations x activities recomputed per iteration (a run stays near 30 s)
RISK_SOURCES = ("udf", "task", "code", "wbs", "default")  # where an activity's range comes from, by precedence
RISK_CAVEAT = (_t("An estimate built on assumed duration ranges, not a forecast: each open activity's remaining "
                  "duration is drawn on its own (no correlation between activities is modelled), discrete risk "
                  "events, resource limits and changes of logic are not modelled, and the network, calendars and "
                  "constraints are those of the last F9."))
RISK_CAVEAT_MODEL = (_t("An estimate built on assumed duration ranges, correlations and risk events, not a forecast: "
                        "durations are correlated only within the groups given (one shared factor per group), risk "
                        "events fire independently of each other and of the durations and only add or remove "
                        "remaining duration, resource limits and changes of logic are not modelled, and the network, "
                        "calendars and constraints are those of the last F9."))
RISK_POOL_MIN_WORK = 200_000  # iterations x recomputed activities from which worker processes pay off (measured)
RISK_POOL_CHUNKS = 2  # slices of the iterations per worker
RISK_WORKERS_DEFAULT = max(1, min(4, (os.cpu_count() or 1) - 1))
RISK_P6_COLS = ("status_code", "risk_to_type", "pre_rsp_prblty", "pre_rsp_schd_prblty", "post_rsp_prblty",
                "post_rsp_schd_prblty")  # PROJRISK values shown as stored (P6 scale levels, not numbers)
_N01 = NormalDist()
_U_MIN = 2.0 ** -53  # random() can return 0.0; the normal quantile needs 0 < u < 1


def _risk_pcts(item: Any, where: str) -> tuple[float, float, float]:
    try:
        v = tuple(float(item[k]) for k in ("min_pct", "mode_pct", "max_pct"))
    except (KeyError, TypeError, ValueError):
        raise ValueError(_t("{where}: min_pct, mode_pct and max_pct are required numbers", where=where)) from None
    if not all(math.isfinite(x) for x in v) or not 0 <= v[0] <= v[1] <= v[2]:
        raise ValueError(_t("{where}: need 0 <= min_pct <= mode_pct <= max_pct (percent of the remaining duration)",
                            where=where))
    return v


def _risk_ids(con, pid: int, cpm: _Cpm, r: Mapping, where: str, allowed: Sequence[str],
              cache: dict) -> tuple[set, str, int]:
    """(task_ids, source, depth of the WBS node or 0) of the one selector of r among allowed: task_code (one
    Activity ID), task_codes (a list of them), wbs (a WBS code, full or relative to the project node: every
    activity below it) or code_type with code_value (that value and its child codes). cache keeps the WBS tree."""
    sel = [k for k in allowed if r.get(k)]
    if len(sel) != 1:
        names = ", ".join(allowed[:-1]) + " or " + allowed[-1]
        if allowed[-1] == "code_type":
            raise ValueError(_t("{where}: give exactly one of {names} (with code_value)", where=where, names=names))
        raise ValueError(_t("{where}: give exactly one of {names}", where=where, names=names))
    k = sel[0]
    if k in ("task_code", "task_codes"):
        codes = [str(r[k])] if k == "task_code" else r[k]
        if not isinstance(codes, (list, tuple)) or not all(isinstance(c, str) and c for c in codes):
            raise ValueError(_t("{where}: task_codes is a list of Activity IDs", where=where))
        missing = [str(c) for c in codes if str(c) not in cpm.code]
        if missing:
            if len(missing) == 1:
                raise ValueError(_t("{where}: activity {code} not found in project {project}", where=where,
                                    code=missing[0], project=cpm.prj['proj_short_name']))
            raise ValueError(_t("{where}: activities {codes} not found in project {project}", where=where,
                                codes=missing[:10], project=cpm.prj['proj_short_name']))
        return {cpm.code[str(c)] for c in codes}, "task", 0
    if k == "wbs":
        sep = cache.get("sep")
        if sep is None:
            sep = cache["sep"] = _name_sep(con, pid)
            cache["nodes"] = _wbs_tree(con, pid, sep)
        nodes = cache["nodes"]
        if not nodes:
            raise ValueError(_t("{where}: the project has no project WBS node", where=where))
        top, p = nodes[0]["wbs_code"], str(r["wbs"]).strip()
        j = next((j for j, n in enumerate(nodes) if n["wbs_code"] in (p, top + sep + p)), None)
        if j is None:
            raise ValueError(_t("{where}: WBS code {wbs!r} not found (p6_list_wbs lists the codes)",
                                where=where, wbs=r['wbs']))
        d0 = nodes[j]["depth"]
        end = next((x for x in range(j + 1, len(nodes)) if nodes[x]["depth"] <= d0), len(nodes))
        sub = {n["wbs_id"] for n in nodes[j:end]}
        return {t for t, d in cpm.t.items() if d["wbs_id"] in sub}, "wbs", d0
    if not r.get("code_value"):
        raise ValueError(_t("{where}: code_type needs code_value", where=where))
    cond, cargs, _ = _code_filter(con, pid, r["code_type"], r["code_value"], True)
    return {x[0] for x in con.execute("SELECT t.task_id FROM TASK t WHERE t.proj_id = ?" + _alive(con, "TASK", "t")
                                      + cond, [pid, *cargs])}, "code", 0


def _risk_ranges(con, pid: int, cpm: _Cpm, ranges: Optional[Sequence]) -> tuple[dict[int, tuple], list[dict]]:
    """({task_id: (source, pcts, range index)} by precedence task > code > wbs (deepest node, then the earlier
    range), the ranges as understood with the activities each one matches)."""
    if ranges is None:
        return {}, []
    if not isinstance(ranges, (list, tuple)):
        raise ValueError(_t("ranges: a list of ranges"))
    keys, cache, seen = set(RiskRange.__annotations__), {}, set()
    hits: dict[int, list] = {}  # task_id -> [(rank, index, source, pcts)]
    echo = []
    for i, r in enumerate(ranges):
        where = f"ranges[{i}]"
        if not isinstance(r, Mapping):
            raise ValueError(_t("{where}: an object with min_pct, mode_pct, max_pct and one selector", where=where))
        if set(r) - keys:
            raise ValueError(_t("{where}: unknown keys {bad}; keys: {keys}", where=where, bad=sorted(set(r) - keys),
                                keys=sorted(keys)))
        if len([k for k in ("task_code", "wbs", "code_type") if r.get(k)]) != 1:
            raise ValueError(_t("{where}: give exactly one of task_code, wbs or code_type (with code_value)",
                                where=where))
        pcts = _risk_pcts(r, where)
        if r.get("task_code"):
            code = str(r["task_code"])
            if code in seen and code in cpm.code:
                raise ValueError(_t("{where}: activity {code} has two ranges", where=where, code=code))
            seen.add(code)
        ids, src, depth = _risk_ids(con, pid, cpm, r, where, ("task_code", "wbs", "code_type"), cache)
        rank = {"task": (0, 0), "code": (1, 0), "wbs": (2, -depth)}[src]
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
            raise ValueError(_t("UDF {label!r} holds {kind}, not a number of days",
                                label=y['udf_type_label'], kind=UDF_KINDS.get(y['logical_data_type'])))
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


# ----------------------------------------------------------------- correlations and risk events


def _risk_correlations(con, pid: int, cpm: _Cpm, items: Optional[Sequence], cache: dict) -> tuple[dict, list, list]:
    """({task_id: group index} (an activity in several groups keeps the first one listed), coefficients by group,
    the groups as understood: selector, coefficient, matched, in_earlier_group, applied (filled by the caller))."""
    if items is None:
        return {}, [], []
    if not isinstance(items, (list, tuple)):
        raise ValueError(_t("correlations: a list of groups"))
    keys = set(RiskCorrelation.__annotations__)
    member: dict[int, int] = {}
    coef, echo = [], []
    for g, r in enumerate(items):
        where = f"correlations[{g}]"
        if not isinstance(r, Mapping):
            raise ValueError(_t("{where}: an object with coefficient and one selector", where=where))
        if set(r) - keys:
            raise ValueError(_t("{where}: unknown keys {bad}; keys: {keys}", where=where, bad=sorted(set(r) - keys),
                                keys=sorted(keys)))
        c = _num(r.get("coefficient"))
        if c is None or isinstance(r.get("coefficient"), bool) or not 0 <= float(c) <= 1:
            raise ValueError(_t("{where}: coefficient is a number from 0 (independent) to 1 (fully correlated)",
                                where=where))
        ids, _, _ = _risk_ids(con, pid, cpm, r, where, ("task_codes", "wbs", "code_type"), cache)
        earlier = 0
        for t in ids:
            if t in member:
                earlier += 1
            else:
                member[t] = g
        coef.append(float(c))
        echo.append({**{k: r[k] for k in ("task_codes", "wbs", "code_type", "code_value") if r.get(k)},
                     "coefficient": float(c), "matched": len(ids), "in_earlier_group": earlier, "applied": 0})
    return member, coef, echo


def _p6_risks(con, pid: int, cpm: _Cpm) -> dict[str, dict]:
    """{Risk ID: {risk_code, name, task_ids, p6_values}} of the project's P6 risks (PROJRISK, activities from
    TASKRISK); {} when the database has no risk table. P6 keeps probability and impacts as levels of its risk
    scoring matrix, not numbers: they are shown as stored and never turned into numbers here."""
    pc = _cols(con, "PROJRISK")
    if not {"risk_id", "proj_id"} <= pc:
        return {}
    show = [c for c in RISK_P6_COLS if c in pc]
    sel = ", ".join(["r.risk_id", "r.risk_code" if "risk_code" in pc else "NULL",
                     "r.risk_name" if "risk_name" in pc else "NULL"] + [f"r.{c}" for c in show])
    out: dict[str, dict] = {}
    by_id: dict[int, dict] = {}
    for row in con.execute(f"SELECT {sel} FROM PROJRISK r WHERE r.proj_id = ?" + _alive(con, "PROJRISK", "r")
                           + " ORDER BY r.risk_id", (pid,)):
        code = str(row[1]) if row[1] not in (None, "") else str(row[0])
        if code in out:  # a Risk ID used twice: the first row (lowest risk_id) is kept
            continue
        out[code] = by_id[row[0]] = {"risk_code": code, "name": row[2], "task_ids": set(),
                                     "p6_values": {c: row[3 + k] for k, c in enumerate(show) if row[3 + k] is not None}}
    if by_id and {"task_id", "risk_id"} <= _cols(con, "TASKRISK"):
        for tid, rid in con.execute("SELECT x.task_id, x.risk_id FROM TASKRISK x WHERE 1 = 1"
                                    + _alive(con, "TASKRISK", "x")):
            if rid in by_id and tid in cpm.t:
                by_id[rid]["task_ids"].add(tid)
    return out


def _risk_days(v: Any, where: str) -> tuple[float, float, float]:
    """add_days as (min, likely, max): a number x is (x, x, x)."""
    bad = _t("{where}: add_days is a number of days or {{min, likely, max}} with min <= likely <= max", where=where)
    if isinstance(v, Mapping):
        if set(v) != {"min", "likely", "max"}:
            raise ValueError(bad)
        x = [None if isinstance(v[k], bool) else _num(v[k]) for k in ("min", "likely", "max")]
        if None in x or not all(math.isfinite(float(y)) for y in x) or not x[0] <= x[1] <= x[2]:
            raise ValueError(bad)
        return float(x[0]), float(x[1]), float(x[2])
    x = None if isinstance(v, bool) else _num(v)
    if x is None or not math.isfinite(float(x)):
        raise ValueError(bad)
    return float(x), float(x), float(x)


def _risk_events(con, pid: int, cpm: _Cpm, items: Optional[Sequence], use_p6: bool,
                 cache: dict) -> tuple[list[dict], Optional[dict]]:
    """(events {id, name, probability, days (min, likely, max), ids, source, selector}, the P6 risks block or None).
    An item whose id is a P6 Risk ID (use_p6) takes the P6 name when it has none and, without a selector, the risk's
    assigned activities; the P6 risks no item quantifies are listed, not simulated."""
    p6 = _p6_risks(con, pid, cpm) if use_p6 else {}
    if items is not None and not isinstance(items, (list, tuple)):
        raise ValueError(_t("risks: a list of risk events"))
    keys, ikeys = set(RiskEvent.__annotations__), set(RiskImpact.__annotations__)
    events: list[dict] = []
    seen: set = set()
    for k, r in enumerate(items or ()):
        where = f"risks[{k}]"
        if not isinstance(r, Mapping):
            raise ValueError(_t("{where}: an object with id, probability and impact", where=where))
        if set(r) - keys:
            raise ValueError(_t("{where}: unknown keys {bad}; keys: {keys}", where=where, bad=sorted(set(r) - keys),
                                keys=sorted(keys)))
        rid = r.get("id")
        if not isinstance(rid, str) or not rid.strip():
            raise ValueError(_t("{where}: id is required (a short text)", where=where))
        rid = rid.strip()
        if rid in seen:
            raise ValueError(_t("{where}: risk {rid} is given twice", where=where, rid=rid))
        seen.add(rid)
        p = None if isinstance(r.get("probability"), bool) else _num(r.get("probability"))
        if p is None or not 0 <= float(p) <= 1:
            raise ValueError(_t("{where}: probability is a number from 0 to 1", where=where))
        imp = r.get("impact")
        if not isinstance(imp, Mapping) or "add_days" not in imp:
            raise ValueError(_t("{where}: impact needs add_days and a selector (task_codes, wbs or code_type)",
                                where=where))
        if set(imp) - ikeys:
            raise ValueError(_t("{where}.impact: unknown keys {bad}; keys: {keys}",
                                where=where, bad=sorted(set(imp) - ikeys), keys=sorted(ikeys)))
        days = _risk_days(imp["add_days"], f"{where}.impact")
        sel = {x: imp[x] for x in ("task_codes", "wbs", "code_type", "code_value") if imp.get(x)}
        if sel or rid not in p6:
            ids, _, _ = _risk_ids(con, pid, cpm, imp, f"{where}.impact", ("task_codes", "wbs", "code_type"), cache)
        else:
            ids, sel = set(p6[rid]["task_ids"]), {"p6_activities": True}
        name = r.get("name") or (p6[rid]["name"] if rid in p6 else None) or rid
        events.append({"id": rid, "name": str(name), "probability": float(p), "days": days, "ids": ids,
                       "source": "p6" if rid in p6 else "risks", "selector": sel})
    block = None
    if use_p6:
        block = {"found": len(p6), "simulated": sum(1 for e in events if e["source"] == "p6"),
                 "not_quantified": [{"risk_code": c, "name": x["name"], "activities": len(x["task_ids"]),
                                     "p6_values": x["p6_values"]} for c, x in p6.items() if c not in seen],
                 "note": (_t("P6 stores the probability and schedule impact of a risk as levels of its risk scoring "
                             "matrix, not numbers: a P6 risk is simulated only when risks= gives its probability and "
                             "add_days under its Risk ID (its assigned activities when the impact has no selector)."))}
    return events, block


# ----------------------------------------------------------------- the iterations


def _tri(u: float, low: float, high: float, mode: float) -> float:
    """random.Random.triangular(low, high, mode) for the uniform u it draws (the same arithmetic, the same float)."""
    try:
        c = (mode - low) / (high - low)
    except ZeroDivisionError:
        return low
    if u > c:
        u = 1.0 - u
        c = 1.0 - c
        low, high = high, low
    return low + (high - low) * math.sqrt(u * c)


_SKIP_OK: list = []


def _skip(rng: random.Random, calls: int) -> None:
    """Advance rng past `calls` random() calls (each takes two 32-bit outputs of the generator) without building
    the floats; checked once against plain calls, which it falls back to otherwise."""
    if not _SKIP_OK:
        a, b = random.Random(12345), random.Random(12345)
        a.getrandbits(32 * 2 * 3)
        for _ in range(3):
            b.random()
        _SKIP_OK.append(a.random() == b.random())
    if not _SKIP_OK[0]:
        for _ in range(calls):
            rng.random()
        return
    words = 2 * calls
    while words:
        k = min(words, 1 << 18)
        rng.getrandbits(32 * k)
        words -= k


class _RiskSim:
    """What the iterations need, picklable (a worker process gets one copy): the network after the deterministic
    pass, the ranges drawn, the correlation groups and the risk events. Iteration i draws one uniform per drawn
    activity from the single stream random.Random(seed), at positions i x drawn onwards (so a slice of the
    iterations gives the same draws as the whole run), and the group factors and risk events from streams of
    their own seeded with (seed, i)."""

    def __init__(self, cpm: _Cpm, plan: dict, base: dict, base_drv: dict, dist: dict, affected: set, sub: list,
                 own_sub: list, fixed_end, fixed_top: list, counted: set, mids: list, seed: int,
                 member: dict, coef: list, events: list):
        net = object.__new__(_Cpm)  # the PROJECT row as a dict: sqlite3.Row does not pickle
        net.__dict__.update(cpm.__dict__)
        net.prj = {k: cpm.prj[k] for k in cpm.prj.keys()}
        self.cpm, self.plan, self.base, self.base_drv = net, plan, base, base_drv
        self.sampled = list(dist)
        self.dist = [dist[n][:3] for n in self.sampled]
        self.affected, self.sub, self.own_sub = affected, sub, own_sub
        self.fixed_end, self.fixed_top, self.counted, self.mids, self.seed = fixed_end, fixed_top, counted, mids, seed
        # correlated activities: (group, weight of the shared factor, weight of their own score); coefficient 0 = none
        self.corr = [None if member.get(n) is None or coef[member[n]] == 0 else
                     (member[n], math.sqrt(coef[member[n]]), math.sqrt(1 - coef[member[n]])) for n in self.sampled]
        self.n_groups = len(coef)
        T = cpm.t
        self.events = [(e["probability"], e["days"], [(n, T[n]["hpd"] or 8.0) for n in sorted(e["ids"]) if n in dist])
                       for e in events]
        self._fixed_ties: dict[int, tuple] = {}

    def path(self, res: dict, drv: dict, end) -> set:
        """Open activities on the critical path of one pass: from those finishing at the project finish, back
        along the links that set each one's dates (completed activities end the walk)."""
        cpm = self.cpm
        T = cpm.t
        stack = [n for n in self.own_sub if res[n]["ef"] >= end - CPM_TOL] + (
            self.fixed_top if self.fixed_end >= end - CPM_TOL else [])
        seen = set(stack)
        while stack:
            n = stack.pop()
            if n in self.affected:
                qs = cpm.ties(drv.get(n))
            else:
                qs = self._fixed_ties.get(n)
                if qs is None:
                    qs = self._fixed_ties[n] = cpm.ties(self.base_drv.get(n))
            for q in qs:
                if q not in seen and T[q]["status_code"] != "TK_Complete":
                    seen.add(q)
                    stack.append(q)
        return seen & self.counted

    def draw(self, rng: random.Random, i: int) -> tuple[dict, int]:
        """(remaining hours by task_id, bit k set when risk event k fired) of iteration i; rng: the main stream at
        the iteration's first uniform."""
        factors: list = []
        if self.n_groups:
            g = random.Random(f"p6mcp-risk:{self.seed}:{i}:corr")
            factors = [_N01.inv_cdf(max(g.random(), _U_MIN)) for _ in range(self.n_groups)]
        rem = {}
        for n, (lo, hi, mo), c in zip(self.sampled, self.dist, self.corr, strict=True):
            u = rng.random()
            if c is not None:  # Gaussian copula: the group's factor mixed with the activity's own normal score
                u = _N01.cdf(c[1] * factors[c[0]] + c[2] * _N01.inv_cdf(max(u, _U_MIN)))
            rem[n] = _tri(u, lo, hi, mo)
        fired = 0
        if self.events:
            e = random.Random(f"p6mcp-risk:{self.seed}:{i}:events")
            for k, (p, (lo, mo, hi), acts) in enumerate(self.events):
                u, x = e.random(), _tri(e.random(), lo, hi, mo)  # both drawn every time: events stay independent
                if u < p:
                    fired |= 1 << k
                    for n, hpd in acts:
                        rem[n] = max(0.0, rem[n] + x * hpd)
        return rem, fired

    def iterate(self, start: int, stop: int) -> dict:
        """Iterations start .. stop - 1: {ends, crit (Counter), ms {task_id: [(es, ef)]}, fired} in iteration order."""
        rng = random.Random(self.seed)
        _skip(rng, start * len(self.sampled))
        cpm, plan, fixed = self.cpm, self.plan, (self.base, self.sub)
        ends: list = []
        crit: Counter = Counter()
        ms: dict = {m: [] for m in self.mids}
        fired: list = []
        for i in range(start, stop):
            rem, f = self.draw(rng, i)
            drv: dict = {}  # entries of the recomputed activities (the others keep base_drv)
            res = cpm.forward(plan, rem, drv, fixed)
            end = max(self.fixed_end, max((res[n]["ef"] for n in self.own_sub), default=self.fixed_end))
            ends.append(end)
            crit.update(self.path(res, drv, end))
            for m in self.mids:
                ms[m].append((res[m]["es"], res[m]["ef"]))
            fired.append(f)
        return {"ends": ends, "crit": crit, "ms": ms, "fired": fired}


_POOL_SIM: list = []  # the simulation a worker process received (_pool_init)


def _pool_init(blob: bytes) -> None:
    _POOL_SIM[:] = [pickle.loads(blob)]


def _pool_chunk(start: int, stop: int) -> dict:
    return _POOL_SIM[0].iterate(start, stop)


def _risk_workers(workers: Any) -> int:
    """Worker processes asked (None: RISK_WORKERS_DEFAULT), at most the CPUs."""
    if workers is None:
        return RISK_WORKERS_DEFAULT
    if isinstance(workers, bool) or not isinstance(workers, (int, float)) or workers != int(workers) or workers < 1:
        raise ValueError(_t("workers must be a whole number, 1 or more (1 = no worker process)"))
    return min(int(workers), max(1, os.cpu_count() or 1))


def _risk_iterate(sim: _RiskSim, n_iter: int, workers: int) -> tuple[dict, dict]:
    """(the iterations merged in iteration order, {workers, note}). Worker processes (spawn) run slices of the
    iterations when workers > 1 and iterations x recomputed activities reach RISK_POOL_MIN_WORK; the draws of an
    iteration do not depend on its slice, so the output is the same for any worker count. Any failure of the pool
    runs every iteration in this process, with a note."""
    work = n_iter * max(1, len(sim.sub))
    if workers <= 1 or work < RISK_POOL_MIN_WORK:
        note = None if workers <= 1 else (_t("no worker process: {work:,} activity passes, worker processes start "
                                             "from {min_work:,}", work=work, min_work=RISK_POOL_MIN_WORK))
        return sim.iterate(0, n_iter), {"workers": 1, "note": note}
    n_chunks = min(n_iter, workers * RISK_POOL_CHUNKS)
    cuts = [n_iter * k // n_chunks for k in range(n_chunks + 1)]
    ex = None
    try:
        blob = pickle.dumps(sim, protocol=pickle.HIGHEST_PROTOCOL)
        ex = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                                 initializer=_pool_init, initargs=(blob,))
        futures = [ex.submit(_pool_chunk, a, b) for a, b in zip(cuts, cuts[1:], strict=False)]
        parts = [f.result() for f in futures]
    except Exception as e:  # noqa: BLE001  (a broken pool must not fail the run: the iterations run here instead)
        if ex is not None:
            ex.shutdown(wait=False, cancel_futures=True)
        return sim.iterate(0, n_iter), {"workers": 1, "note": _t("worker processes failed ({name}: {e}): every "
                                                                 "iteration ran in the server process",
                                                                 name=type(e).__name__, e=e)}
    ex.shutdown(wait=False)  # every slice is back: the workers exit on their own (waiting costs about a second)
    out: dict = {"ends": [], "crit": Counter(), "ms": {m: [] for m in sim.mids}, "fired": []}
    for p in parts:
        out["ends"] += p["ends"]
        out["crit"].update(p["crit"])
        for m in sim.mids:
            out["ms"][m] += p["ms"][m]
        out["fired"] += p["fired"]
    return out, {"workers": workers, "note": None}
