"""Report fields the shop skills read (server 2.8.0): working-day variances of p6_project_summary, start
variance and slip cause of p6_compare_projects, critical flag and weeks of p6_lookahead, add_remaining_days
and float_change of p6_schedule_whatif, the health check score, EPS fields of p6_list_projects and
p6_list_eps, bl_planned_pct of p6_list_wbs and p6_milestones, on the synthetic project enriched with a
baseline one week earlier (test_dashboard.enrich). Working days are counted by the test itself: Monday to
Friday, 2026-04-06 a holiday (the synthetic calendar), stamps at the same time of day."""

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from test_dashboard import enrich

HOLIDAY = date(2026, 4, 6)


def wd(a: str, b: str) -> float:
    """Working days from a to b (negative when b is before a), both at the same time of day."""
    x, y = datetime.fromisoformat(a).date(), datetime.fromisoformat(b).date()
    sign = 1 if y >= x else -1
    x, y = min(x, y), max(x, y)
    days = (x + timedelta(days=k) for k in range((y - x).days))
    n = sum(1 for d in days if d.weekday() < 5 and d != HOLIDAY)
    return float(sign * n)


def rows(p6, tool: str, **kw) -> list[dict]:
    out, off = [], 0
    while off is not None:
        e = p6.call(tool, **kw, offset=off)
        out, off = out + e["rows"], e["next_offset"]
    return out


def test_policy_lists_the_new_tool(server):
    from p6mcp import policy
    assert policy.TOOL_KIND["p6_milestones"] == policy.READ
    assert "p6_milestones" in server.REGISTERED


def test_summary_and_compare(p6):
    enrich(p6)
    con = sqlite3.connect(p6.db)
    con.execute("UPDATE PROJECT SET plan_end_date = '2026-05-18 17:00:00' WHERE proj_id = 2")
    con.execute("UPDATE PROJECT SET scd_end_date = '2026-05-18 17:00:00' WHERE proj_id = 3")
    con.commit()
    con.close()
    sm = p6.call("p6_project_summary", project="DEMO-A")
    assert sm["dates"]["finish_vs_must_finish_by_wd"] == wd("2026-05-18 17:00:00", "2026-05-25 17:00:00") == 5.0
    assert sm["baseline"]["finish_variance_wd"] == 5.0 and sm["baseline"]["finish_variance_cd"] == 7.0
    cmp_ = rows(p6, "p6_compare_projects", project="DEMO-A")
    by = {r["task_code"]: r for r in cmp_}
    # A1010 started four weeks early but finishes a week late: earlier start, longer duration
    a = by["A1010"]
    assert (a["start"], a["other_start"]) == ("2026-02-02 08:00:00", "2026-02-23 08:00:00")
    assert a["start_variance_wd"] == wd("2026-02-23 08:00:00", "2026-02-02 08:00:00") == -15.0
    assert a["variance_wd"] == 5.0 and a["duration_delta_wd"] == 20.0 and a["slip_cause"] == "longer_duration"
    # A1020: everything one week later
    assert (by["A1020"]["start_variance_wd"], by["A1020"]["duration_delta_wd"], by["A1020"]["slip_cause"]) == (
        5.0, 0.0, "late_start")
    # A1030 starts after the Easter Monday holiday: 4 working days later, finishes 5 later
    assert by["A1030"]["start_variance_wd"] == wd("2026-03-31 08:00:00", "2026-04-07 08:00:00") == 4.0
    assert (by["A1030"]["duration_delta_wd"], by["A1030"]["slip_cause"]) == (1.0, "late_start_and_longer_duration")
    for r in cmp_:
        if r["change"] == "matched" and r["variance_wd"] is not None:
            assert abs(r["start_variance_wd"] + r["duration_delta_wd"] - r["variance_wd"]) < 0.011, r
    head = p6.call("p6_compare_projects", project="DEMO-A", limit=1)
    causes = {}
    for r in cmp_:
        causes[r["slip_cause"]] = causes.get(r["slip_cause"], 0) + 1
    assert {k: v for k, v in head["summary"]["activities"]["slip_causes"].items() if v} == causes


def test_lookahead_critical_and_weeks(p6):
    la = p6.call("p6_lookahead", project="DEMO-A", weeks=3, limit=200)
    crit = {r["task_code"] for r in rows(p6, "p6_critical_path", project="DEMO-A", limit=200)}
    assert [w["week"] for w in la["weeks"]] == [1, 2, 3] and la["weeks"][0]["from"] == "2026-03-02 08:00:00"
    assert la["counts"]["critical"] == sum(r["critical"] for r in la["rows"])
    for r in la["rows"]:
        assert r["critical"] == (r["task_code"] in crit)
    a1010 = next(r for r in la["rows"] if r["task_code"] == "A1010")
    assert (a1010["fcst_start"], a1010["fcst_finish"]) == ("2026-02-02 08:00:00", "2026-03-13 17:00:00")
    assert (a1010["start_week"], a1010["finish_week"]) == (None, 2)
    a1020 = next(r for r in la["rows"] if r["task_code"] == "A1020")
    assert (a1020["start_week"], a1020["finish_week"]) == (3, None)


def test_whatif_add_remaining_days(p6):
    add = p6.call("p6_schedule_whatif", project="DEMO-A",
                  edits=[{"op": "duration", "task_code": "A1020", "add_remaining_days": 5}])
    absolute = p6.call("p6_schedule_whatif", project="DEMO-A",
                       edits=[{"op": "duration", "task_code": "A1020", "remaining_days": 20}])
    assert add["edits"] == ["A1020: remaining 15 d +5 d = 20 d"]
    assert add["project_finish"] == absolute["project_finish"] and add["float_change"] == absolute["float_change"]
    fc = add["float_change"]
    assert fc["critical_before"] == fc["critical_after"]
    assert set(fc) >= {"became_critical", "no_longer_critical", "negative_float_before", "negative_float_after"}


def test_health_score_and_lists(p6):
    hc = p6.call("p6_health_check", project="DEMO-A", save_snapshot=False)
    sm = hc["summary"]
    ev = len(sm["passed"]) + len(sm["failed"])
    assert hc["score"] == {"passed": len(sm["passed"]), "failed": len(sm["failed"]),
                           "not_evaluated": len(sm["not_evaluated"]), "evaluated": ev,
                           "pct_passed": round(100 * len(sm["passed"]) / ev, 1) if ev else None}
    eps = p6.call("p6_list_eps", order="tree")["rows"]
    assert [(n["eps_id"], n["depth"], n["path"]) for n in eps] == [("EPS-DEMO", 0, "Demo EPS")]
    prj = p6.call("p6_list_projects", exclude_templates=True)["rows"]
    assert [(r["proj_short_name"], r["eps_id"], r["eps_path"]) for r in prj] == [("DEMO-A", "EPS-DEMO", "Demo EPS")]


def test_wbs_baseline_planned_and_milestones(p6):
    enrich(p6)
    w = p6.call("p6_list_wbs", project="DEMO-A")
    assert w["baseline"]["project"] == "DEMO-A-BL"
    assert all("bl_planned_pct" in r["rollup"] for r in w["rows"])
    ms = p6.call("p6_milestones", project="DEMO-A", limit=200)
    got = [(r["task_code"], r["date"], r["baseline_date"], r["variance_wd"], r["vs_baseline"]) for r in ms["rows"]]
    assert got == [("A1000", "2026-02-02 08:00:00", "2026-01-26 08:00:00", 5.0, "late"),
                   ("A1050", "2026-05-25 17:00:00", "2026-05-18 17:00:00", 5.0, "late")]
    assert ms["counts"] == {"milestones": 2, "completed": 1, "open": 1, "critical": 1, "late": 2, "early": 0,
                            "on_time": 0, "no_baseline": 0, "open_late": 1}


def test_filters_and_orders(p6):
    la = p6.call("p6_lookahead", project="DEMO-A", weeks=3, critical_only=True, limit=200)
    every = p6.call("p6_lookahead", project="DEMO-A", weeks=3, limit=200)
    assert [r["task_code"] for r in la["rows"]] == [r["task_code"] for r in every["rows"] if r["critical"]]
    assert la["total"] == every["counts"]["critical"]
    eps = p6.call("p6_list_eps", order="tree")["rows"]
    assert [(n["projects"], n["projects_subtree"]) for n in eps] == [(1, 1)]
    rows_ = p6.call("p6_list_projects", eps="EPS-DEMO", order="eps")["rows"]
    assert [r["proj_short_name"] for r in rows_] == ["DEMO-A"]
    with pytest.raises(ValueError, match="not found"):
        p6.call("p6_list_projects", eps="NO-SUCH-EPS")
    enrich(p6)
    ms = p6.call("p6_milestones", project="DEMO-A", order="variance")["rows"]
    assert [r["task_code"] for r in ms] == ["A1000", "A1050"]  # equal variances keep the date order


def test_progress_curve_period_count(p6):
    enrich(p6)
    pc = p6.call("p6_progress_curve", project="DEMO-A")
    assert pc["period_count"] == len(pc["periods"]) > 0


def test_critical_path_min_float(p6):
    con = sqlite3.connect(p6.db)
    con.execute("UPDATE TASK SET total_float_hr_cnt = 40 WHERE task_id = 104")  # A1030: 5 d of float
    con.commit()
    con.close()
    near = p6.call("p6_critical_path", project="DEMO-A", float_threshold_days=10, min_float_days=0)
    assert [r["task_code"] for r in near["rows"]] == ["A1030"] and "total float > 0 d" in near["definition"]
    with pytest.raises(ValueError, match="longest path"):
        p6.call("p6_critical_path", project="DEMO-A", mode="longest_path", min_float_days=0)


def test_null_safe_baseline_and_names(p6):
    b = p6.call("p6_project_summary", project="DEMO-A")["baseline"]
    assert b["used"] is None and all(b[k] is None for k in ("finish", "data_date", "finish_variance_cd",
                                                              "finish_variance_wd", "planned_units_pct_at_data_date",
                                                              "spi_units"))
    enrich(p6)
    b = p6.call("p6_project_summary", project="DEMO-A")["baseline"]
    assert (b["used"], b["data_date"]) == ("DEMO-A-BL", "2026-02-23 08:00:00")
    pc = p6.call("p6_progress_curve", project="DEMO-A")
    assert all("baseline_cum" in r for r in pc["periods"])
    con = sqlite3.connect(p6.db)
    con.execute("DELETE FROM TASKPRED WHERE task_pred_id = 201")  # a link removed since the baseline
    con.commit()
    con.close()
    rel = p6.call("p6_compare_projects", project="DEMO-A", section="relationships")["rows"]
    assert all(r["pred_name"] and r["succ_name"] for r in rel)


def test_driver_and_origin_names(p6):
    roots = p6.call("p6_driving_path", project="DEMO-A", target="A1040")["drivers_at_root"]
    assert roots and all(r["task_name"] for r in roots)
    o = p6.call("p6_float_origin", project="DEMO-A", task_code="A1020")["origin"]
    assert {"task_code", "task_name", "date"} <= set(o)
    assert all(s_["task_name"] and "date" in s_ for s_ in o["sources"])


def test_xl_shown_matches_excel_rules(server):
    from datetime import datetime as dt
    xl = server._xl_shown
    assert xl(49.665, '0.00"%"') == "49.67%" and xl(None, "0") == "n/a"
    assert [xl(v, '+0.0"%";-0.0"%";0.0"%"') for v in (-15.45, 0.04, 0.0)] == ["-15.5%", "+0.0%", "0.0%"]
    assert (xl(235.0, '0" d"'), xl(12.25, '0.0" d"'), xl(1234, "#,##0")) == ("235 d", "12.3 d", "1,234")
    assert xl(dt(2027, 10, 5, 17), "dd-mmm-yy") == "05-Oct-27"
    assert set(server.IMPORT_ACTIONS) >= {"matched", "inserted", "updated", "deleted"}


def test_driving_rows_fixed_shape(p6):
    rows = p6.call("p6_driving_path", project="DEMO-A", target="A1020")["rows"]
    assert rows[0]["level"] == 0 and rows[0]["successor"] is None and rows[0]["also_drives"] == []
    assert all(r["successor_name"] for r in rows[1:])
    fo = p6.call("p6_float_origin", project="DEMO-A", task_code="A1020")["rows"]
    assert all(isinstance(r["controlled_by"], list) for r in fo)


def test_critical_path_test_keys_when_not_run(p6):
    cpt = p6.call("p6_health_check", project="DEMO-A", save_snapshot=False)["checks"]["critical_path_test"]
    assert {"activity", "activity_name"} <= set(cpt)
    if cpt["pass"] is None:
        assert cpt["activity"] is None or cpt["activity_name"]
