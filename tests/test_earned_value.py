"""Earned value techniques per WBS node (PROJWBS ev_compute_type) in p6_progress_curve and the spi_units of
p6_project_summary, on the synthetic project enriched by test_dashboard.enrich: ENG 120 units on A1010 (in
progress, WBS DES) and A1020 (not started). A1010 is set to 25 % duration complete (CP_Drtn) and 50 % units
complete (60 actual, 60 remaining), so every technique gives its own earned value. The rules follow the P6
documentation (Earned Value tab of WBS Details): no P6 figure of these techniques is stored anywhere to
compare with."""

import sqlite3

import pytest

from conftest import fingerprint
from test_dashboard import enrich


def setup(p6) -> None:
    enrich(p6)
    con = sqlite3.connect(p6.db)
    con.execute("ALTER TABLE PROJWBS ADD COLUMN ev_user_pct REAL")
    con.execute("ALTER TABLE TASKRSRC ADD COLUMN curv_id INTEGER")
    con.execute("CREATE TABLE WBSSTEP (wbs_step_id INTEGER PRIMARY KEY, proj_id INTEGER, wbs_id INTEGER, "
                "seq_num INTEGER, complete_flag TEXT, step_name TEXT, step_wt REAL, delete_session_id INTEGER)")
    con.execute("UPDATE TASK SET remain_drtn_hr_cnt = 120 WHERE task_id = 102")  # (160 - 120) / 160 = 25 %
    con.commit()
    con.close()
    p6.s._cols_cache.clear()
    p6.s._schema_cache.clear()
    p6.set(trusted_schemas=[fingerprint(p6.db)])


def sql(p6, *statements) -> None:
    con = sqlite3.connect(p6.db)
    for st in statements:
        con.execute(st)
    con.commit()
    con.close()


def test_activity_percent_complete_is_the_default(p6):
    setup(p6)
    assert p6.sql("SELECT target_qty, act_reg_qty, remain_qty FROM TASKRSRC WHERE taskrsrc_id = 402") == [(120, 60, 60)]
    pc = p6.call("p6_progress_curve", project="DEMO-A")
    assert pc["to_date"]["earned"] == 30.0  # 120 x 25 % (A1010) + 120 x 0 % (A1020)
    assert pc["checks"]["ev_techniques"] == {"activity_pct": 4} and pc["checks"]["wbs_other_ev_technique"] == 0
    assert "another earned value technique" not in pc["note"]


@pytest.mark.parametrize("token, user_pct, steps, curve, earned", [
    ("EC_0_pct", None, (), False, 0.0),  # not completed: 0
    ("EC_50_pct", None, (), False, 60.0),  # started: 50 %
    ("EC_User_pct", 30, (), False, 36.0),  # started: the custom 30 %
    ("EC_MS", None, (("Y", 3), ("N", 1)), False, 90.0),  # 3 of 4 milestone weights completed
    ("EC_MS", None, (), False, 0.0),  # no milestone
    ("EC_Cmp_pct_rsrc_curve", None, (), True, 60.0),  # a resource curve: units % complete 60 / 120
    ("EC_Cmp_pct_rsrc_curve", None, (), False, 30.0),  # no curve: activity % complete
])
def test_technique_of_the_wbs_node(p6, token, user_pct, steps, curve, earned):
    setup(p6)
    sql(p6, f"UPDATE PROJWBS SET ev_compute_type = '{token}', ev_user_pct = {user_pct or 'NULL'} WHERE wbs_id = 13",
        *(f"INSERT INTO WBSSTEP (proj_id, wbs_id, seq_num, complete_flag, step_name, step_wt) "
          f"VALUES (2, 13, {i}, '{done}', 'Gate {i}', {wt})" for i, (done, wt) in enumerate(steps)),
        "UPDATE TASKRSRC SET curv_id = 11 WHERE taskrsrc_id = 402" if curve else "SELECT 1")
    pc = p6.call("p6_progress_curve", project="DEMO-A")
    td = pc["to_date"]
    assert td["earned"] == earned and td["earned_pct"] == round(100 * earned / 240, 2)
    assert pc["checks"]["wbs_other_ev_technique"] == 1 and "applies each node's technique" in pc["note"]
    bp = td["baseline_planned"]
    assert bp and td["spi"] == pytest.approx(earned / bp, rel=1e-3)  # bp is rounded to 2 decimals
    summ = p6.call("p6_project_summary", project="DEMO-A")["baseline"]
    assert summ["spi_units"] == td["spi"]


def test_defaults_and_unknown_tokens(p6):
    setup(p6)
    # DES has no technique: it takes the project node's
    sql(p6, "UPDATE PROJWBS SET ev_compute_type = 'EC_0_pct' WHERE wbs_id = 10")
    pc = p6.call("p6_progress_curve", project="DEMO-A")
    assert pc["to_date"]["earned"] == 0.0 and pc["checks"]["ev_techniques"] == {"0_100": 2, "activity_pct": 2}
    sql(p6, "UPDATE PROJWBS SET ev_compute_type = 'EC_Other' WHERE wbs_id = 13")
    pc = p6.call("p6_progress_curve", project="DEMO-A")
    assert pc["to_date"]["earned"] == 30.0 and pc["checks"]["ev_unknown_techniques"] == ["EC_Other"]
    assert "Unknown earned value technique(s) ['EC_Other']" in pc["note"]
