"""Fields added in 1.10.0 on the synthetic project DEMO-A (A1000 -> A1010 -> A1020 -> A1030 -> A1040 -> A1050, A1060
Permits beside it with 20 days of float): the health check's logic_density, weighted_score and worst activities,
health snapshots compared with their weighted score and engine version, p6_milestones codes= and its constraint
and float origin fields, p6_compare_projects float before and after, differences_text of p6_cpm_validate and
engine_version. Expected values come from the fixture's own numbers and test SQL."""

import json
import os
import sqlite3

import pytest

from test_dashboard import enrich


def hc(p6, **kw):
    return p6.call("p6_health_check", project="DEMO-A", save_snapshot=False, **kw)


def sql1(p6, q: str, *args):
    return p6.sql(q, *args)[0][0]


def test_logic_density_outside_score(p6):
    r = hc(p6)
    n_open = sql1(p6, "SELECT COUNT(*) FROM TASK WHERE proj_id = 2 AND status_code <> 'TK_Complete'")
    n_links = sql1(p6, "SELECT COUNT(*) FROM TASKPRED r JOIN TASK s ON s.task_id = r.task_id "
                       "WHERE s.proj_id = 2 AND s.status_code <> 'TK_Complete'")
    ld = r["checks"]["logic_density"]
    got = (ld["count"], ld["denominator"], ld["threshold"], ld["pct"])
    assert got == (n_links, n_open, 1.5, None) == (7, 6, 1.5, None)
    assert ld["value"] == round(7 / 6, 4) and ld["pass"] is False
    assert list(r["checks"])[:2] == ["logic", "logic_density"]
    # score and summary stay the DCMA 14
    assert all("logic_density" not in v for v in r["summary"].values())
    dcma = [k for k in r["checks"] if k != "logic_density"]
    assert r["score"]["evaluated"] == sum(r["checks"][k]["pass"] is not None for k in dcma)
    assert hc(p6, thresholds={"logic_density": 1.0})["checks"]["logic_density"]["pass"] is True
    with pytest.raises(Exception, match="without an item list"):
        hc(p6, check="logic_density")


def expected_status(name: str, c: dict) -> str:
    """The weighted score rule, written again: warn inside 2 x an at most threshold, 0.9 x an at least one."""
    if c["pass"] is None:
        return "not_evaluated"
    if c["pass"]:
        return "pass"
    thr = c["threshold"]
    if not thr:
        return "fail"
    if name in ("logic_density",):
        return "warn" if c["count"] / c["denominator"] >= 0.9 * thr else "fail"
    pct = 100 * c["count"] / c["denominator"]
    if name == "fs_links":
        return "warn" if pct >= 0.9 * thr else "fail"
    return "warn" if pct <= 2 * thr else "fail"


def test_weighted_score(p6):
    weights = {"logic_density": 20, "logic": 20, "critical_path_test": 15, "negative_float": 15, "high_float": 10,
               "hard_constraints": 10, "high_duration": 5, "leads": 5, "lags": 5, "fs_links": 5}
    for kw, want in (({}, {"logic_density": "fail"}),
                     ({"thresholds": {"logic_density": 1.25}}, {"logic_density": "warn"}),  # 1.1667 >= 1.125
                     ({"thresholds": {"high_float": 10}, "high_float_days": 5}, {"high_float": "warn"}),  # 16.7 % <= 20
                     ({"thresholds": {"high_float": 5}, "high_float_days": 5}, {"high_float": "fail"})):  # 16.7 % > 10
        r = hc(p6, **kw)
        w = r["weighted_score"]
        st = {k: expected_status(k, r["checks"][k]) for k in weights}
        assert {k: v["status"] for k, v in w["checks"].items()} == st and all(st[k] == v for k, v in want.items())
        earned = sum(weights[k] * {"pass": 1, "warn": 0.5, "fail": 0}[s] for k, s in st.items() if s != "not_evaluated")
        top = sum(weights[k] for k, s in st.items() if s != "not_evaluated")
        assert (w["earned"], w["max"], w["points"]) == (earned, top, round(100 * earned / top, 1))
        grade = next((g for g, lo in (("A", 90), ("B", 80), ("C", 70), ("D", 55)) if w["points"] >= lo), "F")
        assert w["grade"] == grade and w["checks"]["logic"]["weight"] == 20
        assert w["checks"]["logic"]["status"] == st["logic"]
    r = hc(p6, high_float_days=5)
    assert r["checks"]["high_float"]["worst"] == {"task_code": "A1060", "task_name": "Permits", "total_float_d": 20.0}
    assert r["checks"]["negative_float"]["worst"] is None and r["engine_version"] == p6.s.ENGINE_VERSION
    con = sqlite3.connect(p6.db)  # negative float: no warn band at a zero threshold
    con.execute("UPDATE TASK SET total_float_hr_cnt = -8 WHERE task_id = 107")
    con.commit()
    con.close()
    r = hc(p6)
    assert r["weighted_score"]["checks"]["negative_float"]["status"] == "fail"
    worst = {"task_code": "A1060", "task_name": "Permits", "total_float_d": -1.0}
    assert r["checks"]["negative_float"]["worst"] == worst


def test_snapshots_carry_weighted_score_and_engine_version(p6):
    a = p6.call("p6_health_check", project="DEMO-A")
    b = p6.call("p6_health_check", project="DEMO-A", thresholds={"logic_density": 1.0})
    cmp_ = p6.call("p6_health_check_compare", project="DEMO-A")
    wa, wb = a["weighted_score"], b["weighted_score"]
    assert cmp_["weighted_score"] == {"points": {"before": wa["points"], "after": wb["points"],
                                                 "delta": round(wb["points"] - wa["points"], 4)},
                                      "grade": {"before": wa["grade"], "after": wb["grade"]}}
    assert cmp_["checks"]["logic_density"]["pass"] == {"before": False, "after": True}
    assert cmp_["engine_version"] == {"before": p6.s.ENGINE_VERSION, "after": p6.s.ENGINE_VERSION}
    assert not any("engine_version" in w for w in cmp_["warnings"])
    path = os.path.join(p6.s._hc_snap_dir(), a["snapshot"]["id"] + ".json")
    snap = json.load(open(path, encoding="utf-8"))
    assert snap["engine_version"] == p6.s.ENGINE_VERSION and snap["weighted_score"]["points"] == wa["points"]
    del snap["engine_version"]  # a snapshot saved before engine versions were recorded
    json.dump(snap, open(path, "w", encoding="utf-8"))
    cmp_ = p6.call("p6_health_check_compare", project="DEMO-A")
    assert any(w.startswith(f"engine_version differs: None -> {p6.s.ENGINE_VERSION}") for w in cmp_["warnings"])


def test_milestones_codes_and_constraint_fields(p6):
    r = p6.call("p6_milestones", project="DEMO-A")
    assert r["not_found"] == [] and [x["task_code"] for x in r["rows"]] == ["A1000", "A1050"]
    done, fin = r["rows"]
    assert (done["float_origin_type"], done["float_origin_explanation"], done["predecessor_count"]) == (None, None, 0)
    fo = p6.call("p6_float_origin", project="DEMO-A", task_code="A1050")
    assert (fin["float_origin_type"], fin["float_origin_explanation"]) == (fo["origin"]["type"], fo["explanation"])
    assert (fin["predecessor_count"], fin["hard_constraint"], fin["vs_constraint_wd"]) == (1, False, None)
    for codes in (["A1030", "A1050", "NOPE", "A1030"], " A1030, A1050 ,NOPE,"):
        c = p6.call("p6_milestones", project="DEMO-A", codes=codes)
        assert [x["task_code"] for x in c["rows"]] == ["A1030", "A1050"] and c["not_found"] == ["NOPE"]
        assert c["rows"][0]["date"] == "2026-05-11 17:00:00" and c["counts"]["milestones"] == 2  # a task: its finish
    with pytest.raises(Exception, match="at least one Activity ID"):
        p6.call("p6_milestones", project="DEMO-A", codes=" , ")
    con = sqlite3.connect(p6.db)  # Finish On or Before one week before its date: 5 working days later
    con.execute("UPDATE TASK SET cstr_type = 'CS_MEOB', cstr_date = '2026-05-18 17:00:00' WHERE task_id = 106")
    con.execute("UPDATE TASK SET cstr_type = 'CS_MSOA', cstr_date = '2026-03-16 08:00:00' WHERE task_id = 103")
    con.commit()
    con.close()
    c = {x["task_code"]: x for x in p6.call("p6_milestones", project="DEMO-A", codes=["A1020", "A1050"])["rows"]}
    assert (c["A1050"]["hard_constraint"], c["A1050"]["vs_constraint_wd"]) == (True, 5.0)
    assert (c["A1020"]["hard_constraint"], c["A1020"]["vs_constraint_wd"]) == (False, 15.0)  # its finish, 3 weeks on
    assert p6.call("p6_milestones", project="DEMO-A", codes="A1000", open_only=True)["not_found"] == []


def test_compare_float_before_and_after(p6):
    enrich(p6)
    con = sqlite3.connect(p6.db)
    con.execute("UPDATE TASK SET total_float_hr_cnt = 40 WHERE proj_id = 3")  # 5 days in the baseline
    con.commit()
    con.close()
    rows, off = [], 0
    while off is not None:
        e = p6.call("p6_compare_projects", project="DEMO-A", offset=off)
        rows, off = rows + e["rows"], e["next_offset"]
    by = {r["task_code"]: r for r in rows}
    assert (by["A1020"]["total_float_before_d"], by["A1020"]["total_float_after_d"], by["A1020"]["tf_delta_d"]) == (
        5.0, 0.0, -5.0)
    assert by["A1060"]["tf_delta_d"] == 15.0
    assert (by["A1020"]["critical_before"], by["A1020"]["critical_after"]) == (False, True)
    assert by["A1000"]["critical_after"] is False and by["A1000"]["tf_delta_d"] is None  # completed: no float
    s = e["summary"]["activities"]
    open_ = [r for r in rows if r["change"] == "matched" and r["status"] != "TK_Complete"]
    assert s["float_eroded"] == sum(r["tf_delta_d"] < 0 for r in open_) == 5
    assert s["became_critical"] == sum(r["critical_after"] and not r["critical_before"] for r in open_) == 5
    assert s["no_longer_critical"] == 0 and "float_eroded" in s["float_definition"]


def test_cpm_differences_text_and_engine_version(p6):
    con = sqlite3.connect(p6.db)
    con.execute("UPDATE TASK SET early_end_date = '2026-04-02 17:00:00' WHERE task_id = 103")
    con.commit()
    con.close()
    v = p6.call("p6_cpm_validate", project="DEMO-A")
    row = next(r for r in v["rows"] if r["task_code"] == "A1020")
    assert row["differences_text"] == "early finish: stored 2026-04-02 17:00:00, engine 2026-04-03 17:00:00"
    assert v["engine_version"] == p6.s.ENGINE_VERSION
    w = p6.call("p6_schedule_whatif", project="DEMO-A", edits=[{"op": "duration", "task_code": "A1060",
                                                               "remaining_days": 1}])
    assert w["engine_version"] == p6.s.ENGINE_VERSION
