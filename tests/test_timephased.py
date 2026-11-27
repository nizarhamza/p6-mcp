"""Time-phased spread (3.6.0): p6_progress_curve periods over working time and resource curves, stored period
actuals (TRSRCFIN), and p6_resource_loading against max units. The synthetic project enriched by
test_dashboard.enrich: ENG (labor) 120 units on A1020 (not started, planned 2026-03-16 08:00 to 2026-04-03 17:00)
and on A1010 (in progress, 60 actual, 60 remaining); the calendar works Monday to Friday 8 h with a holiday on
2026-04-06. Every expected value below is counted by hand on that calendar."""

import sqlite3

import pytest

from conftest import fingerprint
from test_dashboard import enrich

# P6's built-in Back Loaded curve as RSRCCURV stores it (BLOB, NUL ended): 10 x 3.5 % then 10 x 6.5 %
BACK_LOADED = ("(0||CurveData()(\n\n  (0||0(PctUsage|0)())\n\n"
               + "".join(f"  (0||{k}(PctUsage|{3.5 if k <= 10 else 6.5})())\n\n" for k in range(1, 21))
               + "))").encode("utf-8") + b"\x00"


def sql(p6, *statements, params=()) -> None:
    con = sqlite3.connect(p6.db)
    for st in statements:
        con.execute(st, params) if params else con.execute(st)
    con.commit()
    con.close()


def setup(p6) -> None:
    """enrich, then the assignment dates P6 stores (A1010: actual start, remaining from the data date), resource
    curve and period actual tables, a calendar and max units for ENG, a material resource on A1030."""
    enrich(p6)
    sql(p6,
        "ALTER TABLE TASKRSRC ADD COLUMN curv_id INTEGER",
        "CREATE TABLE RSRCCURV (curv_id INTEGER PRIMARY KEY, curv_name TEXT, curv_data BLOB, "
        "delete_session_id INTEGER)",
        "CREATE TABLE FINDATES (fin_dates_id INTEGER PRIMARY KEY, fin_dates_name TEXT, start_date TEXT, end_date TEXT, "
        "delete_session_id INTEGER)",
        "CREATE TABLE TRSRCFIN (fin_dates_id INTEGER, taskrsrc_id INTEGER, task_id INTEGER, proj_id INTEGER, "
        "act_qty REAL, act_cost REAL, delete_session_id INTEGER)",
        "ALTER TABLE RSRCRATE ADD COLUMN max_qty_per_hr REAL",
        "ALTER TABLE RSRCRATE ADD COLUMN start_date TEXT",
        "UPDATE TASKRSRC SET restart_date = target_start_date, reend_date = target_end_date WHERE taskrsrc_id = 401",
        "UPDATE TASKRSRC SET act_start_date = '2026-02-02 08:00:00', restart_date = '2026-03-02 08:00:00', "
        "reend_date = '2026-03-13 17:00:00' WHERE taskrsrc_id = 402",
        "UPDATE RSRC SET clndr_id = 1 WHERE rsrc_id = 301",
        "INSERT INTO RSRCRATE (rsrc_rate_id, rsrc_id, cost_per_qty, max_qty_per_hr, start_date) "
        "VALUES (1, 301, 50, 1.0, NULL), (2, 301, 50, 0.1, '2026-04-01 00:00:00')",
        "INSERT INTO RSRC (rsrc_id, rsrc_short_name, rsrc_name, rsrc_type, clndr_id) "
        "VALUES (302, 'CEM', 'Cement', 'RT_Mat', 1)",
        "INSERT INTO TASKRSRC (taskrsrc_id, task_id, proj_id, rsrc_id, rsrc_type, target_qty, act_reg_qty, act_ot_qty, "
        "remain_qty, target_start_date, target_end_date, restart_date, reend_date) SELECT 403, 104, 2, 302, 'RT_Mat', "
        "50, 0, 0, 50, target_start_date, target_end_date, target_start_date, target_end_date FROM TASK "
        "WHERE task_id = 104")
    sql(p6, "INSERT INTO RSRCCURV (curv_id, curv_name, curv_data) VALUES (10, 'Back Loaded', ?)", params=(BACK_LOADED,))
    p6.s._cols_cache.clear()
    p6.s._schema_cache.clear()
    p6.set(trusted_schemas=[fingerprint(p6.db)])


def period(pc, name):
    return next(r for r in pc["periods"] if r["period"] == name)


def test_curve_shape_reads_p6_curves(server):
    shape = server._curve_shape(BACK_LOADED)
    assert len(shape) == 21 and shape[0] == 0 and shape[10] == pytest.approx(0.35) and shape[-1] == 1.0
    assert server._shape_at(shape, 0.8) == pytest.approx(0.74)  # 10 x 3.5 + 6 x 6.5
    user = "(0||()((0||0(PctUsage|10)())" + "".join(f"(0||{k}(PctUsage|9)())" for k in range(1, 21)) + "))"
    assert server._curve_shape(user)[0] == pytest.approx(10 / 190)  # scaled by the sum: point 0 at the start
    for bad in ("(0||CurveData()((0||0(PctUsage|5)())))", "(0||()((0||0(PctUsage|-1)()) ", "not a curve"):
        with pytest.raises(ValueError):
            server._curve_shape(bad)


def test_progress_curve_spreads_over_working_time(p6):
    setup(p6)
    pc = p6.call("p6_progress_curve", project="DEMO-A")
    # A1020: 120 h from 16 Mar to 3 Apr = 12 working days in March (96 h) and 3 in April (24 h); calendar time
    # would give March 376 h / 441 h of the span. A1010: all its planned dates in March.
    mar, apr = period(pc, "2026-03"), period(pc, "2026-04")
    assert (mar["planned"], apr["planned"]) == (216.0, 24.0)
    # A1010's actual: 2 Feb 08:00 to the data date 2 Mar 08:00, all February working time
    assert period(pc, "2026-02")["actual"] == 60.0 and mar["actual"] == 0.0 and apr["actual"] is None
    assert (mar["forecast"], apr["forecast"]) == (156.0, 24.0)
    assert pc["checks"]["totals_reached"] and pc["checks"]["spread"]["curves"] == 0
    assert pc["to_date"]["actual"] == 60.0 and "working time" in pc["note"]


def test_resource_curve_and_period_actuals(p6):
    setup(p6)
    before = p6.call("p6_progress_curve", project="DEMO-A")
    sql(p6, "UPDATE TASKRSRC SET curv_id = 10 WHERE taskrsrc_id = 401",
        "INSERT INTO FINDATES VALUES (1, '2026-02', '2026-02-01 00:00:00', '2026-02-28 00:00:00', NULL)",
        "INSERT INTO TRSRCFIN VALUES (1, 402, 102, 2, 40, 2000, NULL)")
    pc = p6.call("p6_progress_curve", project="DEMO-A")
    # Back Loaded over A1020's working time: 80 % of it in March = 74 % of the units
    assert period(pc, "2026-03")["planned"] == round(120 + 0.74 * 120, 2)
    assert period(pc, "2026-04")["planned"] == round(0.26 * 120, 2)
    # TRSRCFIN: 40 in February; the other 20 from the end of February (1 Mar, no working time before the data
    # date: calendar time) to the data date
    assert period(pc, "2026-02")["actual"] == 40.0 and period(pc, "2026-03")["actual"] == 20.0
    sp = pc["checks"]["spread"]
    assert (sp["curves"], sp["period_actuals"], sp["trsrcfin"], sp["calendar_time"]) == (1, 1, 1, 1)
    assert "Stored period actuals (TRSRCFIN)" in pc["note"]
    assert pc["to_date"] == before["to_date"] and pc["checks"]["totals_reached"]  # data date values unchanged
    sql(p6, "UPDATE TASKRSRC SET curv_id = 99 WHERE taskrsrc_id = 401")  # a curve that does not exist
    pc = p6.call("p6_progress_curve", project="DEMO-A")
    assert pc["checks"]["spread"]["unknown_curve"] == 1 and period(pc, "2026-04")["planned"] == 24.0
    assert "missing or unreadable" in pc["note"]


def test_resource_loading_against_max_units(p6):
    setup(p6)
    r = p6.call("p6_resource_loading", project="DEMO-A")
    rows = {(x["resource"], x["period"]): x for x in r["rows"]}
    assert [x["resource"] for x in r["resources"]] == ["ENG", "CEM"]  # labor first, material apart
    assert [k[1] for k in rows if k[0] == "ENG"] == ["2026-02", "2026-03", "2026-04"]
    feb, mar, apr = rows["ENG", "2026-02"], rows["ENG", "2026-03"], rows["ENG", "2026-04"]
    # available = working hours x max units: February 20 days, March 22 days at 1.0; April 21 days (the holiday
    # on 6 Apr) at 0.1 from 1 Apr
    assert (feb["available"], mar["available"], apr["available"]) == (160.0, 176.0, 16.8)
    assert (feb["actual"], feb["load"], feb["over"]) == (60.0, 60.0, False)
    assert (mar["remaining"], mar["load"], mar["load_pct"], mar["over"]) == (156.0, 156.0, 88.64, False)
    assert (apr["load"], apr["over"], apr["over_by"]) == (24.0, True, 7.2)
    eng = r["resources"][0]
    assert eng["peak"] == {"period": "2026-03", "load": 156.0, "available": 176.0, "load_pct": 88.64}
    assert (eng["max_units_per_hour"], eng["limit_source"], eng["over_periods"], eng["calendar"]) == \
        (1.0, "RSRCRATE", 1, "Standard 5 day")
    summ = {x["resource"]: x for x in p6.call("p6_resource_summary", project="DEMO-A")["rows"]}
    for x in r["resources"]:
        s_ = summ[x["resource"]]
        assert (x["budget"], x["actual"], x["remaining"], x["at_completion"]) == \
            (s_["planned_qty"], s_["actual_qty"], s_["remaining_qty"], s_["at_completion_qty"])
    cem = rows["CEM", "2026-04"]
    assert cem["kind"] == "material" and cem["available"] is None and cem["over"] is None
    assert r["counts"] == {"resources": 2, "over_allocated_periods": 1, "resources_over": 1, "material_resources": 1,
                           "no_limit": [], "spread": r["counts"]["spread"]}
    assert r["periods"][0] == {"period": "2026-02", "start": "2026-02-01", "end": "2026-03-01"}
    page = p6.call("p6_resource_loading", project="DEMO-A", limit=2)
    assert page["returned"] == 2 and page["next_offset"] == 2 and page["total"] == r["total"]
    # role= keeps A1020 (role LEAD), resource= one resource; weeks start on PREFER's day (Sunday)
    sql(p6, "UPDATE TASKRSRC SET role_id = 2 WHERE taskrsrc_id = 401")
    only = p6.call("p6_resource_loading", project="DEMO-A", role="LEAD")
    assert [x["resource"] for x in only["resources"]] == ["ENG"] and only["resources"][0]["budget"] == 120.0
    wk = p6.call("p6_resource_loading", project="DEMO-A", resource="ENG", period="week", from_date="2026-03-15",
                 to_date="2026-03-21")
    assert [(x["start"], x["budget"], x["available"]) for x in wk["rows"]] == [("2026-03-15", 40.0, 40.0)]


def test_resource_loading_refusals_and_empty_project(p6):
    empty = p6.call("p6_resource_loading", project="DEMO-A")
    assert empty["total"] == 0 and empty["resources"] == [] and empty["periods"] == []
    setup(p6)
    sql(p6, "DELETE FROM RSRCRATE")
    r = p6.call("p6_resource_loading", project="DEMO-A")
    assert r["counts"]["no_limit"] == ["ENG"] and all(x["available"] is None for x in r["rows"])
    for kw, msg in (({"resource": "NOPE"}, "Resource not found"), ({"role": "NOPE"}, "Role not found"),
                    ({"period": "day"}, "period must be"), ({"from_date": "2026-05-01", "to_date": "2026-04-01"},
                                                            "before from_date")):
        with pytest.raises(ValueError, match=msg):
            p6.s.p6_resource_loading("DEMO-A", **kw)
