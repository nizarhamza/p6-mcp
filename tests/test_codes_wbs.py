"""p6_update_activity_codes (assign, replace, clear activity code values) and p6_move_activities (activities to
another WBS of their project) on the synthetic database: two-step writes through REGISTERED, all or nothing
batches, refusals, edit log entries, staleness."""

import sqlite3

import pytest

from conftest import fingerprint

CODE_TYPES = ((1, "Discipline", None, "AS_Global"), (2, "Area", 2, "AS_Project"), (3, "Area", 1, "AS_EPS"),
              (4, "Phase", 99, "AS_Project"))  # Phase: a project type of another project, not seen by DEMO-A
CODES = ((11, None, 1, "CON", "Construction", 1), (12, 11, 1, "MEC", "Mechanical works", 1),
         (13, None, 1, "COM", "Commissioning", 2), (14, 13, 1, "MEC", "Mechanical tests", 1),
         (15, None, 1, "ELE", "Electrical", 3), (21, None, 2, "N", "North", 1), (22, None, 2, "S", "South", 2),
         (31, None, 3, "EPS-N", "North (EPS)", 1), (41, None, 4, "P1", "Phase 1", 1))


def codes(p6) -> None:
    """Code types and values; A1010 has Discipline CON and Area N."""
    con = sqlite3.connect(p6.db)
    con.executemany("INSERT INTO ACTVTYPE (actv_code_type_id, actv_code_type, proj_id, actv_code_type_scope, seq_num) "
                    "VALUES (?, ?, ?, ?, 1)", CODE_TYPES)
    con.executemany("INSERT INTO ACTVCODE (actv_code_id, parent_actv_code_id, actv_code_type_id, short_name, "
                    "actv_code_name, seq_num) VALUES (?, ?, ?, ?, ?, ?)", CODES)
    con.executemany("INSERT INTO TASKACTV (task_id, actv_code_type_id, actv_code_id, proj_id) VALUES (102, ?, ?, 2)",
                    [(1, 11), (2, 21)])
    con.commit()
    con.close()


def value(p6, task_id, type_id):
    r = p6.sql("SELECT actv_code_id, proj_id, delete_session_id FROM TASKACTV "
               "WHERE task_id = ? AND actv_code_type_id = ?", task_id, type_id)
    return r[0] if r else None


def logged(p6, tool):
    return [e for e in p6.s._log_entries() if e.get("tool") == tool]


def test_assign_replace_clear(p6):
    codes(p6)
    items = [{"task_code": "A1020", "code_type": "discipline", "code_value": "Electrical"},  # by name, case ignored
             {"task_code": "A1010", "code_type": "Discipline", "code_value": "CON.MEC"},  # by path: replaces CON
             {"task_code": "A1010", "code_type": "AREA", "clear": True}]
    before = p6.hash()
    dry = p6.call("p6_update_activity_codes", project="DEMO-A", items=items)
    assert dry["dry_run"] and not dry["would_refuse"] and dry["confirmation_token"], dry
    assert dry["would_do"] == ["Set 3 activity code value(s) in DEMO-A", "  A1020 'Discipline': None -> 'ELE'",
                               "  A1010 'Discipline': 'CON' -> 'CON.MEC'", "  A1010 'Area': 'N' -> cleared"]
    assert dry["items"][2]["code"]["scope"] == "AS_Project" and "no F9" in dry["schedule_effect"]
    assert p6.hash() == before  # a dry run writes nothing
    res = p6.apply("p6_update_activity_codes", project="DEMO-A", items=items)
    assert res["applied"] and "stale_projects" not in res and "no F9" in res["next"], res
    assert value(p6, 103, 1) == (15, 2, None) and value(p6, 102, 1) == (12, 2, None) and value(p6, 102, 2) is None
    es = logged(p6, "p6_update_activity_codes")
    assert [e["operation"] for e in es] == ["update_activity_code"] * 2 + ["clear_activity_code"]
    assert es[1]["table"] == "TASKACTV" and es[1]["before"] == {"actv_code_id": 11}
    assert es[1]["after"] == {"actv_code_id": 12}
    assert es[2]["keys"] == {"task_id": 102, "actv_code_type_id": 2, "task_code": "A1010", "code_type": "Area"}
    assert len({e["commit"] for e in es}) == 1 and all(e["stale"] == {} for e in es)
    # the activity code filter of the read tools sees the new values
    q = p6.call("p6_query_activities", project="DEMO-A", code_type="Discipline", code_value="ELE")
    assert [r["task_code"] for r in q["rows"]] == ["A1020"]


def test_value_and_type_refusals(p6):
    codes(p6)
    one = lambda **it: p6.call("p6_update_activity_codes", project="DEMO-A",  # noqa: E731
                               items=[{"task_code": "A1030", **it}])["items"][0]
    err = one(code_type="Discipline", code_value="MEC")["error"]
    assert "Several Discipline codes" in err and "CON.MEC" in err and "COM.MEC" in err
    assert one(code_type="Discipline", code_value="COM.MEC")["code"]["after"] == "COM.MEC"
    assert "not found for this project" in one(code_type="Phase", code_value="P1")["error"]  # another project's type
    assert "No Area code 'EPS-N'" in one(code_type="Area", code_value="EPS-N")["error"]  # the project type shadows it
    assert "No Discipline code 'XYZ'" in one(code_type="Discipline", code_value="XYZ")["error"]
    assert "not both" in one(code_type="Area", code_value="N", clear=True)["error"]
    assert "code_value is required" in one(code_type="Area")["error"]
    assert "unknown key(s) ['value']" in one(code_type="Area", value="N")["error"]
    assert "Nothing to clear" in one(code_type="Area", clear=True)["error"]
    assert "not found in project" in p6.call("p6_update_activity_codes", project="DEMO-A", items=[
        {"task_code": "NOPE", "code_type": "Area", "code_value": "N"}])["items"][0]["error"]
    nothing = p6.call("p6_update_activity_codes", project="DEMO-A",
                      items=[{"task_code": "A1010", "code_type": "Area", "code_value": "n"}])
    assert nothing["would_refuse"] and "already 'N'" in nothing["items"][0]["error"]


def test_codes_batch_all_or_nothing_and_token(p6):
    codes(p6)
    good = [{"task_code": "A1030", "code_type": "Area", "code_value": "S"}]
    before = p6.hash()
    dry = p6.call("p6_update_activity_codes", project="DEMO-A", items=good + [
        {"task_code": "A1040", "code_type": "Area", "code_value": "West"}])
    assert dry["would_refuse"] and "1 of 2 item(s)" in dry["refuse_reasons"][0] and "confirmation_token" not in dry
    assert [x["ok"] for x in dry["items"]] == [True, False]
    with pytest.raises(ValueError, match="token"):  # no apply without the dry run's token
        p6.call("p6_update_activity_codes", project="DEMO-A", items=good, dry_run=False)
    tok = p6.call("p6_update_activity_codes", project="DEMO-A", items=good)["confirmation_token"]
    with pytest.raises(ValueError, match="token"):  # the token is bound to the arguments
        p6.call("p6_update_activity_codes", project="DEMO-A", dry_run=False, confirmation_token=tok,
                items=[{"task_code": "A1040", "code_type": "Area", "code_value": "S"}])
    assert p6.hash() == before
    # a direct call with a failing item writes nothing either
    bad = [{"task_code": "A1040", "code_type": "Nope", "code_value": "S"}]
    res = p6.s.p6_update_activity_codes("DEMO-A", good + bad, confirm=True)
    assert res["applied"] is False and "all or nothing" in res["error"] and value(p6, 104, 2) is None
    res = p6.call("p6_update_activity_codes", project="DEMO-A", items=good, dry_run=False, confirmation_token=tok)
    assert res["applied"] and value(p6, 104, 2) == (22, 2, None)


def test_soft_deleted_code_row_is_revived(p6):
    codes(p6)
    con = sqlite3.connect(p6.db)
    con.execute("INSERT INTO TASKACTV (task_id, actv_code_type_id, actv_code_id, proj_id, delete_session_id, "
                "delete_date) VALUES (104, 1, 11, 2, 7, '2026-01-01 00:00:00')")
    con.commit()
    con.close()
    dry = p6.call("p6_update_activity_codes", project="DEMO-A",
                  items=[{"task_code": "A1030", "code_type": "Discipline", "code_value": "CON"}])
    assert dry["items"][0]["code"]["before"] is None, dry  # a soft-deleted row is no value
    res = p6.apply("p6_update_activity_codes", project="DEMO-A",
                   items=[{"task_code": "A1030", "code_type": "Discipline", "code_value": "CON"}])
    assert res["applied"] and p6.sql("SELECT actv_code_id, delete_session_id, delete_date FROM TASKACTV "
                                     "WHERE task_id = 104 AND actv_code_type_id = 1") == [(11, None, None)]


# ----------------------------------------------------------------- WBS moves

WBS_SQL = "INSERT INTO PROJWBS (wbs_id, proj_id, seq_num, proj_node_flag, wbs_short_name, wbs_name, parent_wbs_id) "


def wbs_setup(p6) -> None:
    """A WBS summary activity in CON, a second project DEMO-B with its own ENG node, a SUMTASK row of DEMO-A."""
    con = sqlite3.connect(p6.db)
    con.execute("INSERT INTO TASK (task_id, proj_id, wbs_id, clndr_id, task_type, status_code, task_code, task_name) "
                "VALUES (190, 2, 12, 1, 'TT_WBS', 'TK_NotStart', 'S1000', 'Construction summary')")
    con.execute("INSERT INTO PROJECT (proj_id, project_flag, proj_short_name, clndr_id) VALUES (3, 'Y', 'DEMO-B', 1)")
    con.execute(WBS_SQL +
                "VALUES (20, 3, 0, 'Y', 'DEMO-B', 'Demo project B', 1)")
    con.execute(WBS_SQL +
                "VALUES (21, 3, 1, 'N', 'ENG', 'Engineering B', 20)")
    con.execute("CREATE TABLE SUMTASK (wbs_id INTEGER, proj_id INTEGER)")
    con.execute("INSERT INTO SUMTASK VALUES (11, 2)")
    con.commit()
    con.close()
    p6.set(trusted_schemas=[fingerprint(p6.db)])  # a P6 database has SUMTASK


def wbs_of(p6, code):
    return p6.sql("SELECT wbs_id FROM TASK WHERE task_code = ? AND proj_id = 2", code)[0][0]


def test_move_activities(p6):
    wbs_setup(p6)
    items = [{"task_code": "A1010", "wbs": "DEMO-A"},  # the project node: P6 keeps activities there too
             {"task_code": "A1020", "wbs": "Construction"},  # by name: CON holds a WBS summary activity
             {"task_code": "A1060", "wbs": "eng"}]  # by code without the Project ID, case ignored
    before = p6.hash()
    dry = p6.call("p6_move_activities", project="DEMO-A", items=items)
    assert dry["dry_run"] and not dry["would_refuse"] and dry["confirmation_token"], dry
    assert dry["would_do"][:4] == [
        "Move 3 activities to another WBS in DEMO-A", "  A1010 'Detailed design': WBS DEMO-A.ENG -> DEMO-A",
        "  A1020 'Procurement': WBS DEMO-A.ENG -> DEMO-A.CON (WBS summary activities to reschedule: S1000)",
        "  A1060 'Permits': WBS DEMO-A.CON -> DEMO-A.ENG (WBS summary activities to reschedule: S1000)"]
    assert dry["summaries_stale"]["tables"] == {"SUMTASK": 1} and "Summarize" in dry["would_do"][4]
    assert dry["items"][0]["move"]["wbs_summaries"] == [] and "F9" in dry["schedule_effect"]
    assert p6.hash() == before
    res = p6.apply("p6_move_activities", project="DEMO-A", items=items)
    assert res["applied"] and res["stale_projects"] == ["DEMO-A"] and "press F9" in res["next"], res
    assert (wbs_of(p6, "A1010"), wbs_of(p6, "A1020"), wbs_of(p6, "A1060")) == (10, 12, 11)
    es = logged(p6, "p6_move_activities")
    assert [e["operation"] for e in es] == ["move_activity"] * 3 and es[1]["before"] == {"wbs_id": 11}
    assert es[1]["after"] == {"wbs_id": 12} and es[1]["table"] == "TASK" and es[1]["keys"]["task_code"] == "A1020"
    assert p6.call("p6_project_summary", project="DEMO-A")["schedule_state"]["stale"] is True


def test_move_without_wbs_summary_is_not_stale(p6):
    res = p6.apply("p6_move_activities", project="DEMO-A", items=[{"task_code": "A1010", "wbs": "DEMO-A.CON"}])
    assert res["applied"] and "stale_projects" not in res and "no F9 needed" in res["next"], res
    assert wbs_of(p6, "A1010") == 12 and logged(p6, "p6_move_activities")[0]["stale"] == {}
    rows = {r["wbs_code"]: r for r in p6.call("p6_list_wbs", project="DEMO-A")["rows"]}
    assert rows["DEMO-A.CON"]["rollup"]["activities"] == 5  # rollups follow at once


def test_move_refusals(p6):
    wbs_setup(p6)
    con = sqlite3.connect(p6.db)
    con.execute(WBS_SQL +
                "VALUES (13, 2, 3, 'N', 'ENG2', 'Engineering', 10)")  # a second WBS named Engineering
    con.commit()
    con.close()
    one = lambda **it: p6.call("p6_move_activities", project="DEMO-A",  # noqa: E731
                               items=[{"task_code": "A1030", **it}])["items"][0]
    assert "belongs to project DEMO-B" in one(wbs="DEMO-B.ENG")["error"]
    assert "not found in DEMO-A" in one(wbs="DEMO-A.XYZ")["error"]
    assert "Several WBS nodes" in one(wbs="Engineering")["error"]
    assert one(wbs="DEMO-A.ENG2")["ok"]
    assert "already in DEMO-A.CON" in one(wbs="CON")["error"]
    assert "wbs is required" in one()["error"]
    assert "unknown key(s) ['code']" in one(wbs="CON", code="x")["error"]
    before = p6.hash()
    dry = p6.call("p6_move_activities", project="DEMO-A", items=[{"task_code": "A1030", "wbs": "ENG"},
                                                                 {"task_code": "A1040", "wbs": "NOPE"}])
    assert dry["would_refuse"] and "confirmation_token" not in dry and p6.hash() == before
    items = [{"task_code": "A1030", "wbs": "ENG"}, {"task_code": "A1040", "wbs": "NOPE"}]
    res = p6.s.p6_move_activities("DEMO-A", items, confirm=True)
    assert res["applied"] is False and wbs_of(p6, "A1030") == 12  # all or nothing
