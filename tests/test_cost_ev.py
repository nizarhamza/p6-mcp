"""Cost earned value at the data date: p6_project_summary cost and p6_progress_curve to_date.cost, on the
synthetic project enriched by test_dashboard.enrich (ENG labor on A1010 and A1020, baseline DEMO-A-BL one week
earlier) plus a material and a nonlabor assignment, the fixture's expenses and one more expense on A1010. The
oracles are computed here from the values the test writes, with the P6 earned value field definitions."""

import sqlite3
from datetime import datetime

import pytest

from conftest import fingerprint
from test_dashboard import enrich

DD = datetime(2026, 3, 2, 8)
BL_A1010 = (datetime(2026, 2, 23, 8), datetime(2026, 3, 6, 17))  # baseline planned dates of A1010 (one week earlier)


def sql(p6, *statements) -> None:
    con = sqlite3.connect(p6.db)
    for st in statements:
        con.execute(st)
    con.commit()
    con.close()
    p6.s._cols_cache.clear()
    p6.s._schema_cache.clear()
    p6.set(trusted_schemas=[fingerprint(p6.db)])


def setup(p6) -> None:
    """A1010 (task 102) 50 % duration complete, CP_Drtn: ENG 6000 (3000 actual), STEEL material 2000 (500 actual),
    an expense of 400 at its start (all actual). A1020 (103): ENG 6000, CRANE nonlabor 1000, the fixture's
    expense 10000 (uniform). A1030 (104): the fixture's expense 800 (at the end). The baseline has the same
    budgets on its copies of the activities."""
    enrich(p6)
    rsrc = "INSERT INTO RSRC (rsrc_id, rsrc_short_name, rsrc_name, rsrc_type) VALUES "
    sql(p6, rsrc + "(302, 'STEEL', 'Steel', 'RT_Mat')", rsrc + "(303, 'CRANE', 'Crane', 'RT_Equip')",
        "INSERT INTO TASKRSRC (taskrsrc_id, task_id, proj_id, rsrc_id, rsrc_type, target_qty, act_reg_qty, remain_qty, "
        "target_cost, act_reg_cost, act_ot_cost, remain_cost, target_start_date, target_end_date) SELECT 403, 102, 2, "
        "302, 'RT_Mat', 10, 2, 8, 2000, 400, 100, 1500, target_start_date, target_end_date FROM TASK "
        "WHERE task_id = 102",
        "INSERT INTO TASKRSRC (taskrsrc_id, task_id, proj_id, rsrc_id, rsrc_type, target_qty, act_reg_qty, remain_qty, "
        "target_cost, act_reg_cost, act_ot_cost, remain_cost, target_start_date, target_end_date) SELECT 404, 103, 2, "
        "303, 'RT_Equip', 0, 0, 0, 1000, 0, 0, 1000, target_start_date, target_end_date FROM TASK WHERE task_id = 103",
        "INSERT INTO PROJCOST (cost_item_id, proj_id, task_id, cost_name, act_cost, remain_cost, target_cost, "
        "cost_load_type) VALUES (3, 2, 102, 'Permit', 400, 0, 400, 'CL_Start')",
        "INSERT INTO TASKRSRC (taskrsrc_id, task_id, proj_id, rsrc_id, rsrc_type, target_qty, remain_qty, target_cost, "
        "remain_cost, target_start_date, target_end_date) SELECT a.taskrsrc_id + 1000, a.task_id + 1000, 3, a.rsrc_id, "
        "a.rsrc_type, a.target_qty, a.target_qty, a.target_cost, a.target_cost, datetime(a.target_start_date, "
        "'-7 days'), datetime(a.target_end_date, '-7 days') FROM TASKRSRC a WHERE a.taskrsrc_id IN (403, 404)",
        "UPDATE TASKRSRC SET target_cost = 6000, remain_cost = 6000 WHERE proj_id = 3 AND rsrc_id = 301",
        "INSERT INTO PROJCOST (cost_item_id, proj_id, task_id, cost_name, act_cost, remain_cost, target_cost, "
        "cost_load_type) SELECT cost_item_id + 100, 3, task_id + 1000, cost_name, 0, target_cost, target_cost, "
        "cost_load_type FROM PROJCOST WHERE proj_id = 2")
    assert p6.sql("SELECT phys_complete_pct, remain_drtn_hr_cnt, target_drtn_hr_cnt FROM TASK WHERE task_id = 102") \
        == [(50.0, 80, 160)]


def share(start: datetime, end: datetime) -> float:
    return min(1.0, max(0.0, (DD - start) / (end - start)))


BAC = 6000 + 6000 + 2000 + 1000 + 10000 + 800 + 400
EV = 6000 * 0.5 + 2000 * 0.5 + 400 * 0.5  # A1010 is 50 % complete by duration, the others not started
AC = 3000 + 500 + 400
REM = 3000 + 6000 + 1500 + 1000 + 10000 + 800
PV = 8000 * share(*BL_A1010) + 400  # A1010's baseline labor and material, its expense accrued at the start


def test_cost_block_of_the_summary(p6):
    setup(p6)
    c = p6.call("p6_project_summary", project="DEMO-A")["cost"]
    assert c["available"] and c["currency"] == "USD"
    assert (c["bac"], c["ev"], c["ac"], c["remaining"]) == (BAC, EV, AC, REM)
    assert c["pv"] == round(PV, 2) and c["baseline_bac"] == BAC
    assert c["cv"] == EV - AC and c["sv"] == round(EV - PV, 2)
    assert c["cpi"] == round(EV / AC, 4) and c["spi"] == round(EV / PV, 4)
    # no technique stored: ETC = remaining cost for activity, EAC = AC + remaining = P6's at completion cost
    assert c["eac_method"]["technique"] == "remaining_cost" and c["eac_method"]["token"] is None
    assert c["eac"] == AC + REM and c["etc"] == REM and c["vac"] == BAC - AC - REM
    assert c["tcpi"] == round((BAC - EV) / REM, 4) and c["tcpi_bac"] == round((BAC - EV) / (BAC - AC), 4)
    v = c["eac_variants"]
    assert v["ac_plus_remaining"] == AC + REM and v["ac_plus_bac_minus_ev"] == AC + BAC - EV
    assert v["bac_over_cpi"] == round(BAC * AC / EV, 2)
    assert v["ac_plus_bac_minus_ev_over_cpi_spi"] == round(AC + (BAC - EV) / ((EV / AC) * (EV / PV)), 2)
    assert v["ac_plus_pf_x_bac_minus_ev"] is None
    s = c["by_source"]
    assert s["labor"] == {"bac": 12000, "pv": round(6000 * share(*BL_A1010), 2), "ev": 3000, "ac": 3000,
                          "remaining": 9000}
    assert s["material"] == {"bac": 2000, "pv": round(2000 * share(*BL_A1010), 2), "ev": 1000, "ac": 500,
                             "remaining": 1500}
    assert s["nonlabor"] == {"bac": 1000, "pv": 0, "ev": 0, "ac": 0, "remaining": 1000}
    assert s["expenses"] == {"bac": 11200, "pv": 400, "ev": 200, "ac": 400, "remaining": 10800}
    assert s["other"] == {"bac": 0, "pv": 0, "ev": 0, "ac": 0, "remaining": 0}
    assert c["note"] is None


def test_bac_matches_resource_summary_plus_expenses_and_the_curve(p6):
    setup(p6)
    summ = p6.call("p6_project_summary", project="DEMO-A")
    tot = p6.call("p6_resource_summary", project="DEMO-A")["totals"]
    exp = p6.sql("SELECT SUM(target_cost), SUM(act_cost) FROM PROJCOST WHERE proj_id = 2")[0]
    assert summ["cost"]["bac"] == tot["planned_cost"] + exp[0] and summ["cost"]["ac"] == tot["actual_cost"] + exp[1]
    pc = p6.call("p6_progress_curve", project="DEMO-A", measure="cost")
    assert pc["to_date"]["cost"] == summ["cost"]
    s = summ["cost"]["by_source"]  # the curve itself covers the assignments only
    assert pc["to_date"]["earned"] == sum(s[k]["ev"] for k in ("labor", "nonlabor", "material", "other"))
    assert pc["to_date"]["baseline_planned"] == round(sum(s[k]["pv"] for k in ("labor", "nonlabor", "material")), 2)
    assert p6.call("p6_progress_curve", project="DEMO-A")["to_date"]["cost"] is None  # units
    one = p6.call("p6_progress_curve", project="DEMO-A", measure="cost", resource="ENG")["to_date"]["cost"]
    assert one["bac"] == 12000 and one["by_source"]["expenses"]["bac"] == 0 and "ENG" in one["scope"]
    # the units figures do not move
    assert summ["percent_complete"]["budget_units"] == 240 and summ["baseline"]["spi_units"] is not None


@pytest.mark.parametrize("token, pf, technique, eac", [
    ("EE_PF_cpi", None, "pf_cpi", BAC * AC / EV),
    ("EE_PF_cpi_spi", None, "pf_cpi_spi", AC + (BAC - EV) / ((EV / AC) * (EV / PV))),
    ("EE_PF_1", None, "pf_1", AC + BAC - EV),
    ("EE_PF_user", 1.25, "pf_custom", AC + 1.25 * (BAC - EV)),
    ("EE_Rem_hr", 0.88, "remaining_cost", AC + REM),
])
def test_estimate_to_complete_technique_of_the_project(p6, token, pf, technique, eac):
    setup(p6)
    sql(p6, "ALTER TABLE PROJWBS ADD COLUMN ev_etc_compute_type TEXT",
        "ALTER TABLE PROJWBS ADD COLUMN ev_etc_user_value REAL",
        "ALTER TABLE PREFER ADD COLUMN ev_etc_compute_type TEXT", "UPDATE PREFER SET ev_etc_compute_type = 'EE_Rem_hr'",
        f"UPDATE PROJWBS SET ev_etc_compute_type = '{token}', ev_etc_user_value = {pf or 'NULL'} WHERE wbs_id = 10")
    c = p6.call("p6_project_summary", project="DEMO-A")["cost"]
    m = c["eac_method"]
    assert (m["technique"], m["token"], m["pf"]) == (technique, token, pf)
    assert m["source"].startswith("project WBS node")
    assert c["eac"] == round(eac, 2) and c["etc"] == round(eac - AC, 2) and c["vac"] == round(BAC - eac, 2)
    assert c["tcpi"] == round((BAC - EV) / (eac - AC), 4)


def test_admin_default_other_nodes_and_unknown_tokens(p6):
    setup(p6)
    sql(p6, "ALTER TABLE PROJWBS ADD COLUMN ev_etc_compute_type TEXT",
        "ALTER TABLE PREFER ADD COLUMN ev_etc_compute_type TEXT", "UPDATE PREFER SET ev_etc_compute_type = 'EE_PF_cpi'",
        "UPDATE PROJWBS SET ev_etc_compute_type = 'EE_Rem_hr' "
        "WHERE wbs_id = 11")
    c = p6.call("p6_project_summary", project="DEMO-A")["cost"]
    assert c["eac_method"]["technique"] == "pf_cpi" and c["eac_method"]["source"].startswith("admin preferences")
    assert c["eac_method"]["wbs_nodes_other_technique"] == 1 and "1 WBS nodes store another" in c["note"]
    sql(p6, "UPDATE PROJWBS SET ev_etc_compute_type = 'EE_Strange' WHERE wbs_id = 10")
    c = p6.call("p6_project_summary", project="DEMO-A")["cost"]
    assert c["eac_method"]["technique"] == "remaining_cost" and c["eac"] == AC + REM
    assert "Unknown estimate to complete technique EE_Strange" in c["note"]


def test_earned_technique_of_the_wbs_applies_to_cost(p6):
    setup(p6)
    sql(p6, "UPDATE PROJWBS SET ev_compute_type = 'EC_50_pct' WHERE wbs_id = 13")  # A1010's node: 50 % once started
    sql(p6, "UPDATE TASK SET remain_drtn_hr_cnt = 120 WHERE task_id = 102")  # 25 % by duration, 50 % by 50/50
    assert p6.call("p6_project_summary", project="DEMO-A")["cost"]["ev"] == EV
    sql(p6, "UPDATE PROJWBS SET ev_compute_type = 'EC_0_pct' WHERE wbs_id = 13")
    assert p6.call("p6_project_summary", project="DEMO-A")["cost"]["ev"] == 0


def test_no_costs_and_no_baseline(p6):
    c = p6.call("p6_project_summary", project="DEMO-A")["cost"]  # the fixture: expenses only, no baseline
    assert c["available"] and c["bac"] == 10800 and c["ac"] == 0 and c["ev"] == 0
    assert c["pv"] is None and c["sv"] is None and c["spi"] is None and c["baseline_bac"] is None
    assert c["cpi"] is None and c["eac_variants"]["bac_over_cpi"] is None and "No baseline" in c["note"]
    sql(p6, "DELETE FROM PROJCOST")
    c = p6.call("p6_project_summary", project="DEMO-A")["cost"]
    assert not c["available"] and c["bac"] is None and c["eac"] is None
    assert c["eac_variants"]["ac_plus_remaining"] is None
    assert "No costs" in c["note"] and set(c["by_source"]) == {"labor", "nonlabor", "material", "other", "expenses"}
