"""The CPM engine (_Cpm) and the network analysis (_Net) against P6 25.12 F9 results.

tests/fixtures/parity holds 107 synthetic parity cases, one behaviour each (calendars, relationships, lags,
constraints, progress and out-of-sequence options, activity types, float, links to other projects), each the
XER export of a P6 25.12 schedule run (manifest.json: export route and oracle projects). Each case area is
imported with p6_import_xer (mode new) into a fresh copy of the synthetic database; holder projects of the
external cases first, then their cross-project links by test SQL (the import skips links to other projects).
The engine's results must equal the XER rows: dates to the minute, float rounded to the precision of the
export route (p6: 1/60 hour, mcp: 6 decimals), on early and late dates, total and free float, the longest
path flag, float paths, remaining and planned dates. On the stored F9 dates, every not started activity has
a driving explanation and a late date origin."""

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

import pytest
from conftest import fingerprint

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "parity")
with open(os.path.join(HERE, "manifest.json"), encoding="utf-8") as _fh:
    MANIFEST = json.load(_fh)["files"]
AREAS = sorted({k.split("/")[0] for k in MANIFEST})
EPS = {"eps_id": "QPARITY", "eps_name": "Engine parity cases"}

# engine result key -> (XER TASK column, kind)
FIELDS = {"es": ("early_start_date", "date"), "ef": ("early_end_date", "date"),
          "ls": ("late_start_date", "date"), "lf": ("late_end_date", "date"),
          "tf_h": ("total_float_hr_cnt", "hours"), "ff_h": ("free_float_hr_cnt", "hours"),
          "rs": ("restart_date", "date"), "rf": ("reend_date", "date"), "rls": ("rem_late_start_date", "date"),
          "rlf": ("rem_late_end_date", "date"), "longest": ("driving_path_flag", "flag"),
          "float_path": ("float_path", "int"), "float_path_order": ("float_path_order", "int"),
          "ps": ("target_start_date", "date"), "pf": ("target_end_date", "date"),
          "rem_h": ("remain_drtn_hr_cnt", "hours")}


def norm(kind: str, v, route: str, engine: bool):
    if v in (None, ""):
        return None
    if kind == "date":
        return (v + timedelta(seconds=30)).replace(second=0, microsecond=0) if engine else \
            datetime.strptime(v.replace("T", " ")[:16], "%Y-%m-%d %H:%M")
    if kind == "hours":
        f = float(v)
        return (round(f * 60) / 60 if route == "p6" else round(f, 6)) if engine else round(f, 6)
    if kind == "flag":
        return ("Y" if v in (True, "Y") else "N") if engine else v
    return int(float(v))


def prepare(s, db: str, x: dict) -> None:
    """The synthetic database has fewer columns and NEXTKEY counters than a P6 database: add the columns of the
    XER tables it lacks and a counter for every table the import writes."""
    con = sqlite3.connect(db)
    try:
        tables = {r[0].lower(): r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for t, rows in x.items():
            if t.lower() not in tables or not rows:
                continue
            have = {r[1].lower() for r in con.execute(f"PRAGMA table_info({tables[t.lower()]})")}
            for c in rows[0]:
                if c.lower() not in have:
                    con.execute(f"ALTER TABLE {tables[t.lower()]} ADD COLUMN {c}")
        keys = {r[0].lower() for r in con.execute("SELECT key_name FROM NEXTKEY")}
        for t, pk in s.XER_PK.items():
            if t.lower() in tables and f"{t}_{pk}".lower() not in keys:
                top = con.execute(f"SELECT COALESCE(MAX({pk}), 0) + 1 FROM {tables[t.lower()]}").fetchone()[0]
                con.execute("INSERT INTO NEXTKEY (key_name, key_seq_num) VALUES (?, ?)", (f"{t}_{pk}", top))
        con.commit()
    finally:
        con.close()
    s._cols_cache.clear()
    s._schema_cache.pop(s.DB_PATH, None)


def import_case(p6, path: str, entry: dict) -> list[str]:
    """Import the projects of one case file; returns the oracle Project IDs."""
    s, db = p6.s, p6.db
    _, x = s._xer_read(path)
    prepare(s, db, x)
    p6.set(trusted_schemas=[fingerprint(db)])  # the columns added change the schema fingerprint
    names = [p["proj_short_name"] for p in x.get("PROJECT", [])]
    oracle = entry.get("oracle_projects") or names
    for name in [n for n in names if n not in oracle] + [n for n in names if n in oracle]:
        res = s.p6_import_xer(path, mode="new", xer_project=name, create_eps=EPS, confirm=True)
        assert res.get("applied") is not False and not res.get("would_refuse"), res
    if len(names) > 1:
        cross_links(db, x)
    return oracle


def cross_links(db: str, x: dict) -> None:
    pname = {p["proj_id"]: p["proj_short_name"] for p in x["PROJECT"]}
    tk = {t["task_id"]: (pname[t["proj_id"]], t["task_code"]) for t in x["TASK"]}
    con = sqlite3.connect(db)
    try:
        ids = {(r[0], r[1]): (r[2], r[3]) for r in con.execute(
            "SELECT p.proj_short_name, t.task_code, t.task_id, t.proj_id FROM TASK t "
            "JOIN PROJECT p ON p.proj_id = t.proj_id")}
        cols = [r[1].lower() for r in con.execute("PRAGMA table_info(TASKPRED)")]
        last = con.execute("SELECT * FROM TASKPRED ORDER BY task_pred_id DESC LIMIT 1").fetchone()
        tmpl = dict(zip(cols, last, strict=True))
        nxt = con.execute("SELECT MAX(task_pred_id) FROM TASKPRED").fetchone()[0] + 1
        for r in x.get("TASKPRED", []):
            p, q = tk[r["pred_task_id"]], tk[r["task_id"]]
            if p[0] == q[0]:
                continue
            row = {**tmpl, "task_pred_id": nxt, "task_id": ids[q][0], "proj_id": ids[q][1], "pred_task_id": ids[p][0],
                   "pred_proj_id": ids[p][1], "pred_type": r["pred_type"], "lag_hr_cnt": float(r["lag_hr_cnt"] or 0)}
            if "delete_session_id" in row:
                row["delete_session_id"] = None
            sql = f"INSERT INTO TASKPRED ({','.join(row)}) VALUES ({','.join('?' * len(row))})"
            con.execute(sql, list(row.values()))
            nxt += 1
        con.commit()
    finally:
        con.close()


def compare(s, path: str, entry: dict, oracle: list[str]) -> tuple[Counter, list]:
    route = entry["exported_by"]
    _, x = s._xer_read(path)
    diff, ex = Counter(), []
    for name in oracle:
        xp = next(p for p in x["PROJECT"] if p["proj_short_name"] == name)
        rows = {r["task_code"]: r for r in x.get("TASK", []) if r.get("proj_id") == xp["proj_id"]}
        con = s._connect()
        try:
            pid = s._project_id(con, name)
            cpm = s._Cpm(con, pid)
            res = cpm.run()
            net = s._net_snapshot(con, pid)
        finally:
            con.close()
        for i, d in cpm.t.items():
            r, got = rows[d["task_code"]], res[i]
            for key, (col, kind) in FIELDS.items():
                if col not in r or (key in ("ps", "pf") and r.get("status_code") != "TK_NotStart") \
                        or (key == "rem_h" and key not in got):
                    continue
                a, b = norm(kind, got[key], route, True), norm(kind, r[col], route, False)
                same = a == b if kind != "hours" or a is None or b is None else abs(a - b) <= 1e-6
                if not same:
                    diff[key] += 1
                    ex.append(f"{name} {d['task_code']} {key}: engine {a}, P6 {b}")
        for n, d in net.t.items():
            if d["proj_id"] != pid or d["status_code"] != "TK_NotStart":
                continue
            tol = s.GAP_TOL_H
            early = (net.gap(lk) for lk in net.by_succ.get(n, []))
            if not any(g is not None and abs(g) <= tol for g in early) \
                    and s._root_driver(net, d, [])["type"] == "unexplained":
                diff["drive_unexplained"] += 1
                ex.append(f"{name} {d['task_code']}: no driving explanation")
            late = (net.gap(lk, late=True) for lk in net.by_pred.get(n, []))
            if not any(g is not None and abs(g) <= tol for g in late) \
                    and not s._cstr_sources(net, d) and s._late_origin(net, d)[0]["type"] == "unexplained":
                diff["late_unexplained"] += 1
                ex.append(f"{name} {d['task_code']}: no late date origin")
    return diff, ex


@pytest.mark.parametrize("area", AREAS)
def test_engine_matches_p6(p6, area):
    s = p6.s
    total, examples, files = Counter(), [], 0
    for rel, entry in sorted(MANIFEST.items()):
        if not rel.startswith(area + "/") or not entry["oracle"]:
            continue
        files += 1
        path = os.path.join(HERE, *rel.split("/"))
        oracle = import_case(p6, path, entry)
        diff, ex = compare(s, path, entry, oracle)
        total.update(diff)
        examples += [f"{rel}: {e}" for e in ex[:5]]
    assert files and not total, (dict(total), examples[:20])


def test_manifest_lists_every_case():
    on_disk = {os.path.relpath(os.path.join(dp, f), HERE).replace("\\", "/")
               for dp, _, fs in os.walk(HERE) for f in fs if f.endswith(".xer")}
    assert on_disk == set(MANIFEST) and len(on_disk) == 107
