"""Network and summary reports on the synthetic database (calendars, float paths, dangling ends, redundant links,
progress consistency, groups, resources, health check snapshots compared).

Fixture: DEMO-A, 7 activities (A1000 completed start milestone, A1010 in progress, A1020 to A1060 not started,
A1050 the finish milestone, A1060 the only activity with float: 160 h), 7 FS links without lag (see
test_relationships), one 8 h calendar "Standard 5 day" with the holiday 2026-04-06, data date 2026-03-02.
Oracles: the fixture's constants and the test's own SQL.
"""

import sqlite3

import pytest
from test_dashboard import enrich


def write(p6, *statements: str) -> None:
    con = sqlite3.connect(p6.db)
    try:
        for q in statements:
            con.execute(q)
        con.commit()
    finally:
        con.close()


def codes(res: dict) -> list[str]:
    return [r["task_code"] for r in res["rows"]]


def test_list_calendars(p6):
    res = p6.call("p6_list_calendars", project="DEMO-A", from_date="2026-04-06", to_date="2026-04-10",
                  include_exceptions=True)
    assert res["total"] == 1 and res["project"] == "DEMO-A"
    cal = res["rows"][0]
    assert (cal["name"], cal["hours_per_day"], cal["week_hours"]) == ("Standard 5 day", 8, 40)
    assert cal["project_default"] is True and cal["activities"] == 7
    assert cal["exceptions"]["total"] == 1 and cal["exceptions"]["non_work"] == 1
    assert [d["date"] for d in cal["exception_dates"]] == ["2026-04-06"]
    assert cal["window"]["workdays"] == 4 and cal["window"]["work_hours"] == 32  # Monday is the holiday
    assert p6.call("p6_list_calendars", name_contains="no such calendar")["total"] == 0
    with pytest.raises(ValueError, match="both from_date and to_date"):
        p6.call("p6_list_calendars", from_date="2026-04-06")


def test_float_paths_stored_or_from_the_longest_path(p6):
    res = p6.call("p6_float_paths", project="DEMO-A")
    assert res["source"] == "driving_path_flag" and res["paths"] == []
    assert codes(res) == ["A1010", "A1020", "A1030", "A1040", "A1050"]
    assert codes(p6.call("p6_float_paths", project="DEMO-A", include_completed=True))[0] == "A1000"
    with pytest.raises(ValueError, match="stored no float paths"):
        p6.call("p6_float_paths", project="DEMO-A", path=1)

    enrich(p6)  # float path 1: A1010 to A1050 (orders 1 to 5), path 2: A1060
    res = p6.call("p6_float_paths", project="DEMO-A")
    assert res["source"] == "float_path" and codes(res) == ["A1010", "A1020", "A1030", "A1040", "A1050", "A1060"]
    p1, p2 = res["paths"]
    assert (p1["path"], p1["activities"], p1["first_order"], p1["last_order"], p1["max_tf_d"]) == (1, 5, 1, 5, 0)
    assert (p2["path"], p2["activities"], p2["min_tf_d"]) == (2, 1, 20)
    assert codes(p6.call("p6_float_paths", project="DEMO-A", path=2)) == ["A1060"]


def test_dangling_finish_and_ladder_suggestion(p6):
    res = p6.call("p6_dangling", project="DEMO-A")
    assert res["total"] == 0 and res["counts"] == {"start": 0, "finish": 0}

    write(p6, "UPDATE TASKPRED SET pred_type = 'PR_SS' WHERE task_pred_id = 207")  # A1060 -> A1030 now SS
    res = p6.call("p6_dangling", project="DEMO-A")
    assert res["counts"] == {"start": 0, "finish": 1} and res["with_suggestion"] == 1
    (row,) = res["rows"]
    assert (row["kind"], row["task_code"]) == ("finish", "A1060")
    assert [(lk["code"], lk["type"]) for lk in row["links"]] == [("A1030", "SS")]
    (item,) = row["suggestion"]["items"]
    assert (item["predecessor_code"], item["successor_code"], item["rel_type"], item["lag_days"]) == \
        ("A1060", "A1030", "FF", 0)
    assert p6.call("p6_dangling", project="DEMO-A", kind="start")["counts"] == {"start": 0}


def test_redundant_links(p6):
    res = p6.call("p6_redundant_links", project="DEMO-A")
    assert [r["task_pred_id"] for r in res["rows"]] == [201, 206]  # A1000 completed, successors open
    assert all(r["categories"] == ["completed_predecessor"] and r["sole_predecessor"] for r in res["rows"])
    assert res["counts"] == {"completed_predecessor": 2, "marked_deleted": 0, "transitive": 0, "duplicate": 0}
    assert [a["task_code"] for a in res["side_effects"]["no_predecessor"]] == ["A1010", "A1060"]

    write(p6, "INSERT INTO TASKPRED (task_pred_id, task_id, pred_task_id, proj_id, pred_proj_id, pred_type, "
              "lag_hr_cnt) VALUES (208, 104, 102, 2, 2, 'PR_FS', 0), (209, 104, 103, 2, 2, 'PR_FS', 0)",
          "UPDATE TASK SET task_name = 'Permits (Deleted)' WHERE task_id = 107")
    res = p6.call("p6_redundant_links", project="DEMO-A", categories=["transitive", "duplicate"])
    assert [(r["task_pred_id"], r["predecessor"], r["successor"], r["categories"]) for r in res["rows"]] == [
        (208, "A1010", "A1030", ["transitive"]), (209, "A1020", "A1030", ["duplicate"])]
    assert res["counts"] == {"transitive": 1, "duplicate": 1}
    marked = p6.call("p6_redundant_links", project="DEMO-A", categories=["marked_deleted"])
    assert [r["task_pred_id"] for r in marked["rows"]] == [207, 206]  # by successor: A1030, then A1060
    with pytest.raises(ValueError, match="categories"):
        p6.call("p6_redundant_links", project="DEMO-A", categories=["nonsense"])


def test_progress_check(p6):
    res = p6.call("p6_progress_check", project="DEMO-A")
    assert res["data_date"] == "2026-03-02 08:00:00"
    assert res["out_of_sequence_total"] == {"links": 0, "successors": 0}
    assert all(c["count"] == 0 for c in res["status_checks"].values())
    assert res["logic_option"]["mode"] == "retained logic"
    assert (res["logic_option"]["sched_retained_logic"], res["logic_option"]["sched_progress_override"]) == ("Y", "N")

    write(p6, "UPDATE TASK SET status_code = 'TK_Active', act_start_date = '2026-02-23 08:00:00' WHERE task_id = 103",
          "UPDATE TASK SET phys_complete_pct = 10 WHERE task_id = 105")
    res = p6.call("p6_progress_check", project="DEMO-A")
    fs = res["out_of_sequence"]["FS"]  # A1020 started while A1010 is not finished
    assert (fs["links"], fs["successors"], fs["predecessor_not_started"]["links"]) == (1, 1, 0)
    assert fs["sample"] == ["A1010 (TK_Active) -> A1020 (TK_Active)"]
    assert res["out_of_sequence_total"] == {"links": 1, "successors": 1}
    assert res["status_checks"]["not_started_with_actuals"] == {"count": 1,
                                                                "sample": ["A1040 Commissioning (TK_NotStart)"]}


def test_group_summary(p6):
    res = p6.call("p6_group_summary", project="DEMO-A")
    got = [(r["group"], r["activities"], r["open"], r["critical"]) for r in res["rows"]]
    assert got == [("TK_NotStart", 5, 5, 4), ("TK_Active", 1, 1, 1), ("TK_Complete", 1, 0, 0)]
    assert res["rows"][0]["max_tf_d"] == 20 and res["rows"][0]["min_tf_d"] == 0
    assert (res["totals"]["activities"], res["totals"]["open"], res["totals"]["critical"]) == (7, 6, 5)

    types = p6.call("p6_group_summary", project="DEMO-A", by="task_type")
    assert [(r["group"], r["activities"]) for r in types["rows"]] == [("TT_FinMile", 1), ("TT_Mile", 1), ("TT_Task", 5)]
    wbs = p6.call("p6_group_summary", project="DEMO-A", by="wbs")
    assert [(r["name"], r["activities"]) for r in wbs["rows"]] == [("Engineering", 3), ("Construction", 4)]
    assert p6.call("p6_group_summary", project="DEMO-A", include_completed=False)["totals"]["activities"] == 6
    with pytest.raises(ValueError, match="code_type"):
        p6.call("p6_group_summary", project="DEMO-A", by="code")


def test_resource_summary(p6):
    enrich(p6)  # ENG (labor, 50 per unit) on A1020 (120 h left) and A1010 (60 h done, 60 h left)
    res = p6.call("p6_resource_summary", project="DEMO-A")
    (row,) = res["rows"]
    assert (row["resource"], row["type"], row["assignments"], row["activities"]) == ("ENG", "RT_Labor", 2, 2)
    assert row["by_status"] == {"not_started": 1, "in_progress": 1, "completed": 0}
    qty = ("planned_qty", "actual_qty", "remaining_qty", "at_completion_qty")
    assert tuple(row[k] for k in qty) == (240, 60, 180, 240)
    assert row["units_pct"] == 25.0
    assert (row["planned_cost"], row["actual_cost"], row["remaining_cost"]) == (12000, 3000, 9000)
    assert res["totals"]["resources"] == 1 and res["totals"]["other_type_assignments"] == 0
    assert p6.call("p6_resource_summary", project="DEMO-A", resource="ENG")["rows"] == res["rows"]
    with pytest.raises(ValueError, match="Resource not found"):
        p6.call("p6_resource_summary", project="DEMO-A", resource="NOPE")


def test_health_check_compare(p6):
    first = p6.call("p6_health_check", project="DEMO-A")["snapshot"]["id"]
    second = p6.call("p6_health_check", project="DEMO-A")["snapshot"]["id"]
    res = p6.call("p6_health_check_compare", project="DEMO-A")
    assert (res["before"]["id"], res["after"]["id"]) == (first, second)
    assert res["changed"] == [] and res["totals"]["relationships"] == {"before": 7, "after": 7, "delta": 0}

    write(p6, "DELETE FROM TASKPRED WHERE task_pred_id = 207")  # A1060 keeps no successor
    res = p6.call("p6_health_check_compare", project="DEMO-A", after="current")
    assert res["before"]["id"] == second and res["after"]["id"] not in (first, second)
    assert res["totals"]["relationships"] == {"before": 7, "after": 6, "delta": -1}
    assert "logic" in res["changed"] and res["checks"]["logic"]["count"]["delta"] == 1
    assert any("A1060" in str(k) for k in res["checks"]["logic"]["entered"])
    assert len(res["snapshots"]) == 3
