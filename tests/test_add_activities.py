"""p6_add_activities (3.8.0): new activities with their relationships, all or nothing, checked on the synthetic
database (DEMO-A, data date Monday 2 Mar 2026 08:00, calendar 08:00-12:00 + 13:00-17:00 Monday to Friday)."""

import pytest

from p6mcp import policy

NEW = [
    {"task_code": "N100", "task_name": "Added design review", "wbs": "ENG", "original_duration_days": 10,
     "predecessors": [{"task_code": "A1010"}], "successors": [{"task_code": "N110"}]},
    {"task_code": "N110", "task_name": "Review passed", "wbs": "Construction", "type": "finish_milestone",
     "predecessors": [{"task_code": "N100"}],  # the same link as N100's successor: added once
     "successors": [{"task_code": "A1030", "type": "SS", "lag_days": 2}]},
    {"task_code": "N120", "task_name": "Review support", "type": "level_of_effort",
     "predecessors": [{"task_code": "N100", "type": "SS"}], "successors": [{"task_code": "N110", "type": "FF"}]},
    {"task_code": "N130", "task_name": "Go ahead", "type": "start_milestone", "constraint": "start_on_or_after",
     "constraint_date": "2026-03-09", "calendar": "standard 5 DAY", "successors": [{"task_code": "N100"}]},
]


def task(p6, code):
    r = p6.sql("SELECT task_id, wbs_id, clndr_id, task_type, status_code, target_drtn_hr_cnt, remain_drtn_hr_cnt, "
               "phys_complete_pct, complete_pct_type, early_start_date, early_end_date, target_start_date, "
               "target_end_date, late_start_date, total_float_hr_cnt, cstr_type, cstr_date, guid, create_user "
               "FROM TASK WHERE task_code = ?", code)
    keys = ("task_id", "wbs_id", "clndr_id", "task_type", "status_code", "orig_h", "rem_h", "pct", "pct_type", "es",
            "ef", "ts", "tf_date", "ls", "tf", "cstr", "cstr_date", "guid", "user")
    return dict(zip(keys, r[0], strict=True)) if r else None


def links(p6):
    return sorted(p6.sql("SELECT p.task_code, s.task_code, r.pred_type, r.lag_hr_cnt FROM TASKPRED r JOIN TASK p ON "
                         "p.task_id = r.pred_task_id JOIN TASK s ON s.task_id = r.task_id WHERE r.task_pred_id >= 300"))


def test_add_activities_with_their_links(p6):
    before = p6.hash()
    dry = p6.call("p6_add_activities", project="DEMO-A", items=NEW)
    assert dry["dry_run"] and not dry["would_refuse"] and dry["confirmation_token"] and dry["loops"] == 0
    assert p6.hash() == before  # a dry run writes nothing
    assert dry["would_do"][0] == "Add 4 activities to DEMO-A"
    assert dry["would_do"][1] == ("  Add N100 'Added design review' (Task Dependent, 10 d, WBS DEMO-A.ENG, calendar "
                                  "Standard 5 day) task_id 200")
    assert dry["would_do"][-1].startswith("New activities are placed at the data date")
    assert dry["data_date"] == "2026-03-02 08:00:00" and dry["dates_from"] == "data date"
    rep = dry["items"]
    assert [x["task_id"] for x in rep] == [200, 201, 202, 203] and all(x["ok"] for x in rep)
    assert rep[1]["links"][0]["plan"] == "N100 -> N110 FS: given twice, added once (item 0)"
    res = p6.apply("p6_add_activities", project="DEMO-A", items=NEW)
    assert res["applied"] and res["stale_projects"] == ["DEMO-A"]
    n100 = task(p6, "N100")
    assert n100 == {"task_id": 200, "wbs_id": 11, "clndr_id": 1, "task_type": "TT_Task", "status_code": "TK_NotStart",
                    "orig_h": 80.0, "rem_h": 80.0, "pct": 0, "pct_type": "CP_Drtn",  # DEMO-A's default
                    "es": "2026-03-02 08:00:00", "ef": "2026-03-13 17:00:00", "ts": "2026-03-02 08:00:00",
                    "tf_date": "2026-03-13 17:00:00", "ls": None, "tf": None, "cstr": None, "cstr_date": None,
                    "guid": n100["guid"], "user": "ADMIN"}
    assert len(n100["guid"]) == 22
    assert task(p6, "N110")["wbs_id"] == 12 and task(p6, "N110")["ef"] == "2026-03-02 08:00:00"  # by WBS name
    assert task(p6, "N120")["wbs_id"] == 10 and task(p6, "N120")["orig_h"] == 0  # the project node by default
    n130 = task(p6, "N130")
    assert (n130["task_type"], n130["cstr"], n130["cstr_date"]) == ("TT_Mile", "CS_MSOA", "2026-03-09 08:00:00")
    assert links(p6) == [("A1010", "N100", "PR_FS", 0), ("N100", "N110", "PR_FS", 0), ("N100", "N120", "PR_SS", 0),
                         ("N110", "A1030", "PR_SS", 16.0), ("N120", "N110", "PR_FF", 0), ("N130", "N100", "PR_FS", 0)]
    assert dict(p6.sql("SELECT key_name, key_seq_num FROM NEXTKEY WHERE key_name IN "
                       "('task_task_id', 'taskpred_task_pred_id')")) == {"task_task_id": 204,
                                                                         "taskpred_task_pred_id": 306}
    es = [e for e in p6.s._log_entries() if e.get("tool") == "p6_add_activities"]
    assert [e["operation"] for e in es] == ["add_activity"] * 4 + ["add_relationship"] * 6
    assert len({e["commit"] for e in es}) == 1 and es[0]["after"]["task_code"] == "N100" and es[0]["before"] is None
    # the engine reads them as P6 activities: placed by their logic, every difference put on the missing F9
    con = p6.s._connect()
    try:
        cpm = p6.s._Cpm(con, 2)
        res = cpm.run()
    finally:
        con.close()
    assert str(res[cpm.code["N130"]]["es"]) == "2026-03-09 08:00:00"  # its constraint
    assert str(res[cpm.code["N100"]]["es"]) == "2026-03-16 08:00:00"  # after A1010 (finishes 13 Mar)
    v = p6.call("p6_cpm_validate", project="DEMO-A")
    assert v["stale_f9"] and v["cause_counts"]["engine"] == v["cause_counts"]["unknown"] == 0
    # and p6_delete_activity takes one out again
    gone = p6.apply("p6_delete_activity", project="DEMO-A", task_code="N120")
    assert gone["applied"] and task(p6, "N120") is None and ("N100", "N120", "PR_SS", 0) not in links(p6)


def test_refusals_are_all_or_nothing(p6):
    before = p6.hash()
    bad = [
        {"task_code": "A1020", "task_name": "Used", "original_duration_days": 1},
        {"task_code": "N1", "task_name": "Twice", "original_duration_days": 1},
        {"task_code": "N1", "task_name": "Twice again", "original_duration_days": 1},
        {"task_code": "N2", "task_name": "No WBS", "wbs": "NOPE", "original_duration_days": 1},
        {"task_code": "N3", "task_name": "No calendar", "calendar": "Nope", "original_duration_days": 1},
        {"task_code": "N4", "task_name": "Milestone", "type": "start_milestone", "original_duration_days": 2},
        {"task_code": "N5", "task_name": "LOE", "type": "level_of_effort", "original_duration_days": 2},
        {"task_code": "N6", "task_name": "No duration"},  # no default duration in this database's preferences
        {"task_code": "N7-ABCDEFGHIJKLMNOPQRS", "task_name": "Too long", "original_duration_days": 1},
        {"task_code": "N8", "task_name": "Unknown link", "original_duration_days": 1,
         "predecessors": [{"task_code": "ZZZ"}]},
        {"task_code": "N9", "task_name": "Loop", "original_duration_days": 1,
         "predecessors": [{"task_code": "A1050"}], "successors": [{"task_code": "A1010"}]},
        {"task_code": "N10", "task_name": "WBS summary", "type": "wbs_summary", "constraint": "as_late_as_possible"},
        {"task_code": "N11", "task_name": "Bad key", "bogus": 1},
        {"task_code": "N12 ", "task_name": "Trailing space"},
        {"task_code": "N13", "task_name": "Twice, two lags", "original_duration_days": 1,
         "successors": [{"task_code": "A1040"}, {"task_code": "A1040", "lag_days": 1}]},
    ]
    dry = p6.call("p6_add_activities", project="DEMO-A", items=bad)
    assert dry["would_refuse"] and "confirmation_token" not in dry and dry["loops"] == 1
    err = {x["index"]: x.get("error") for x in dry["items"]}
    assert [x["index"] for x in dry["items"] if x["ok"]] == [1]  # valid, but nothing is written
    assert f"{len(bad) - 1} of {len(bad)} item(s)" in dry["refuse_reasons"][0]
    assert "already used in DEMO-A" in err[0]
    assert "already given by item 1" in err[2]
    assert "WBS 'NOPE' not found" in err[3] and "calendar 'Nope' not found" in err[4]
    assert "has no duration" in err[5] and "computes the duration" in err[6]
    assert "original_duration_days is required" in err[7] and "at most 20 characters" in err[8]
    assert "relationship(s) refused" in err[9] and "Activity ZZZ not found" in dry["items"][9]["links"][0]["error"]
    assert err[10] == "logic loop: N9 -> A1010 -> A1020 -> A1030 -> A1040 -> A1050 -> N9"
    assert "no constraint on a WBS Summary" in err[11] and "bogus" in err[12] and "trailing spaces" in err[13]
    assert "given twice with lags 0 and 1 d" in dry["items"][14]["links"][1]["error"]
    assert p6.hash() == before
    res = p6.s.p6_add_activities(project="DEMO-A", items=bad, confirm=True)  # direct call: no token needed
    assert res["applied"] is False and "all or nothing" in res["error"]
    assert p6.hash() == before
    with pytest.raises(ValueError, match="At most 500"):
        p6.call("p6_add_activities", project="DEMO-A", items=[{"task_code": "X", "task_name": "X"}] * 501)
    with pytest.raises(ValueError, match="Project not found"):
        p6.call("p6_add_activities", project="NOPE", items=[{"task_code": "X", "task_name": "X"}])


def test_policy_of_the_tool():
    assert policy.kind("p6_add_activities") == policy.WRITE and policy.remote_kind("p6_add_activities") == "write"
    from p6mcp.core.constants import EPPM_EDIT_TOOLS
    assert "p6_add_activities" not in EPPM_EDIT_TOOLS  # the push does not create activities
