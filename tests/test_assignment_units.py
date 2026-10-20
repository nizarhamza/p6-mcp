"""p6_update_assignment / p6_update_assignments on the synthetic database, with the columns P6 uses for
units added to the copy: linked costs, units/time, budget = remaining for a not started activity, the
activity's labor totals, refusals, all or nothing batches, and a schema without the optional columns."""

import sqlite3

import pytest

from conftest import fingerprint

EXTRA = {"PROJECT": [("rem_target_link_flag", "TEXT")],
         "TASK": [("duration_type", "TEXT"), ("target_work_qty", "REAL"), ("remain_work_qty", "REAL"),
                  ("act_work_qty", "REAL"), ("target_equip_qty", "REAL"), ("remain_equip_qty", "REAL")],
         "TASKRSRC": [("cost_per_qty", "REAL"), ("target_qty_per_hr", "REAL"), ("remain_qty_per_hr", "REAL")]}


def assignments(p6, full: bool = True) -> None:
    """ENG (labor, 50/unit) on A1020 (not started, 120 h) and on A1010 (in progress, 60 done, 60 left)."""
    con = sqlite3.connect(p6.db)
    if full:
        for t, cols in EXTRA.items():
            for c, typ in cols:
                con.execute(f"ALTER TABLE {t} ADD COLUMN {c} {typ}")
        con.execute("UPDATE PROJECT SET rem_target_link_flag = 'Y'")
        con.execute("UPDATE TASK SET duration_type = 'DT_FixedDUR2', target_work_qty = 0, remain_work_qty = 0, "
                    "act_work_qty = 0, target_equip_qty = 0, remain_equip_qty = 0")
        con.execute("UPDATE TASK SET target_work_qty = 120, remain_work_qty = 120 WHERE task_id = 103")
        con.execute("UPDATE TASK SET target_work_qty = 120, remain_work_qty = 60, act_work_qty = 60 "
                    "WHERE task_id = 102")
    con.execute("INSERT INTO RSRC (rsrc_id, rsrc_short_name, rsrc_name, rsrc_type) VALUES (301, 'ENG', 'Engineer', "
                "'RT_Labor')")
    for tid, task, done, left in ((401, 103, 0, 120), (402, 102, 60, 60)):
        con.execute("INSERT INTO TASKRSRC (taskrsrc_id, task_id, proj_id, rsrc_id, rsrc_type, cost_qty_link_flag, "
                    "target_qty, act_reg_qty, act_ot_qty, remain_qty, target_cost, act_reg_cost, act_ot_cost, "
                    "remain_cost) VALUES (?, ?, 2, 301, 'RT_Labor', 'Y', 120, ?, 0, ?, 6000, ?, 0, ?)",
                    (tid, task, done, left, done * 50, left * 50))
        if full:
            con.execute("UPDATE TASKRSRC SET cost_per_qty = 50, target_qty_per_hr = 1, remain_qty_per_hr = 1 "
                        "WHERE taskrsrc_id = ?", (tid,))
    con.commit()
    con.close()
    p6.s._cols_cache.clear()
    p6.s._schema_cache.clear()
    p6.set(trusted_schemas=[fingerprint(p6.db)])


def row(p6, tid):
    return p6.sql("SELECT a.target_qty, a.remain_qty, a.act_reg_qty, a.target_cost, a.remain_cost, "
                  "a.target_qty_per_hr, a.remain_qty_per_hr, t.target_work_qty, t.remain_work_qty "
                  "FROM TASKRSRC a JOIN TASK t ON t.task_id = a.task_id WHERE a.taskrsrc_id = ?", tid)[0]


def test_budget_follows_p6_rules(p6):
    assignments(p6)
    before = p6.hash()
    dry = p6.call("p6_update_assignment", project="DEMO-A", task_code="A1020", resource="ENG", budgeted_units=180)
    assert dry["dry_run"] and "budget 120 -> 180, remaining 120 -> 180" in dry["would_do"][0], dry
    assert p6.hash() == before
    res = p6.apply("p6_update_assignment", project="DEMO-A", task_code="A1020", resource="ENG", budgeted_units=180)
    assert res["applied"] and "stale_projects" not in res, res
    # budget and remaining linked (not started), costs at 50/unit, units/time x 1.5, labor totals + 60
    assert row(p6, 401) == (180, 180, 0, 9000, 9000, 1.5, 1.5, 180, 180)


def test_at_completion_on_an_activity_in_progress(p6):
    assignments(p6)
    res = p6.apply("p6_update_assignment", project="DEMO-A", taskrsrc_id=402, at_completion_units=100)
    assert res["applied"], res
    assert row(p6, 402) == (120, 40, 60, 6000, 2000, 1, pytest.approx(40 / 60), 120, 40)


@pytest.mark.parametrize("kw, why", [
    ({"task_code": "A1020", "resource": "ENG", "actual_units": 5}, "not started"),
    ({"task_code": "A1020", "resource": "ENG", "budgeted_units": 100, "remaining_units": 90}, "give one value"),
    ({"task_code": "A1010", "resource": "ENG", "at_completion_units": 50}, "less than the actual"),
    ({"task_code": "A1020", "resource": "ENG", "remaining_units": -1}, "0 or more"),
    ({"task_code": "A1020", "budgeted_units": 100}, "resource or role is required"),
    ({"task_code": "A1030", "resource": "ENG", "budgeted_units": 100}, "no resource assignment"),
    ({"taskrsrc_id": 402, "task_code": "A1020", "budgeted_units": 100}, "not of"),
])
def test_refusals(p6, kw, why):
    assignments(p6)
    before = p6.hash()
    with pytest.raises(Exception, match=why):
        p6.call("p6_update_assignment", project="DEMO-A", **kw)
    assert p6.hash() == before


def test_other_duration_types_refused(p6):
    assignments(p6)
    con = sqlite3.connect(p6.db)
    con.execute("UPDATE TASK SET duration_type = 'DT_FixedQty' WHERE task_id = 103")
    con.commit()
    con.close()
    with pytest.raises(Exception, match="never changes durations"):
        p6.call("p6_update_assignment", project="DEMO-A", task_code="A1020", resource="ENG", budgeted_units=100)


def test_batch_is_all_or_nothing(p6):
    assignments(p6)
    items = [{"task_code": "A1020", "resource": "ENG", "budgeted_units": 150},
             {"taskrsrc_id": 402, "remaining_units": 30}]
    dry = p6.call("p6_update_assignments", project="DEMO-A", items=items + [{"taskrsrc_id": 999, "budgeted_units": 1}])
    assert dry["would_refuse"] and [i["ok"] for i in dry["items"]] == [True, True, False], dry
    before = p6.hash()
    res = p6.apply("p6_update_assignments", project="DEMO-A", items=items)
    assert res["applied"] and p6.hash() != before, res
    assert row(p6, 401)[:2] == (150, 150) and row(p6, 402)[1] == 30


def test_without_the_optional_columns(p6):
    """A schema without cost_per_qty, units/time and activity totals: units written, costs left as they are."""
    assignments(p6, full=False)
    res = p6.apply("p6_update_assignment", project="DEMO-A", task_code="A1020", resource="ENG", budgeted_units=150)
    assert res["applied"] and "costs unchanged" in res["done"][0], res
    got = p6.sql("SELECT target_qty, remain_qty, target_cost FROM TASKRSRC WHERE taskrsrc_id = 401")[0]
    assert got == (150, 120, 6000)
