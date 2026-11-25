"""p6_update_activities (many activities, all or nothing) and the local revert behind p6_eppm_discard,
checked on the synthetic database (DEMO-A, data date 2 Mar 2026)."""

import pytest

from p6mcp.eppm import pending
from test_assignment_units import assignments, row


def task(p6, code):
    return p6.sql("SELECT remain_drtn_hr_cnt, task_name, act_start_date, status_code, phys_complete_pct "
                  "FROM TASK WHERE task_code = ?", code)[0]


def test_batch_updates_several_activities_in_one_commit(p6):
    items = [{"task_code": "A1020", "remaining_duration_days": 12},
             {"task_code": "A1030", "task_name": "Renamed", "physical_pct": 10},
             {"task_code": "A1040", "actual_start": "2026-02-23"}]
    dry = p6.call("p6_update_activities", project="DEMO-A", items=items)
    assert dry["dry_run"] and not dry["would_refuse"] and dry["confirmation_token"]
    assert dry["would_do"][0] == "Update 3 activities" and dry["would_do"][1].startswith("  A1020: rem dur 15.0 -> 12")
    assert [x["ok"] for x in dry["items"]] == [True] * 3 and dry["items"][1]["task_code"] == "A1030"
    assert task(p6, "A1020")[0] == 120.0  # a dry run writes nothing
    res = p6.apply("p6_update_activities", project="DEMO-A", items=items)
    assert res["applied"] and res["edit_log"]["entries"] == 3
    assert task(p6, "A1020")[0] == 96.0
    assert task(p6, "A1030")[1:] == ("Renamed", None, "TK_NotStart", 10)
    assert task(p6, "A1040")[2:4] == ("2026-02-23 08:00:00", "TK_Active")
    # one commit, one entry per activity, as the single tool logs it
    es = [e for e in p6.s._log_entries() if e.get("tool") == "p6_update_activities"]
    assert [e["keys"]["task_code"] for e in es] == ["A1020", "A1030", "A1040"] and len({e["commit"] for e in es}) == 1
    assert all(e["operation"] == "update_activity" for e in es)


def test_batch_is_all_or_nothing(p6):
    items = [{"task_code": "A1020", "remaining_duration_days": 12},
             {"task_code": "NOPE", "remaining_duration_days": 1},
             {"task_code": "A1030", "actual_start": "2026-04-01"},  # after the data date
             {"task_code": "A1040", "bogus": 1},
             {"task_code": "A1050"}]  # nothing to change
    dry = p6.call("p6_update_activities", project="DEMO-A", items=items)
    assert dry["would_refuse"] and "4 of 5 item(s)" in dry["refuse_reasons"][0]
    errs = {x["index"]: x.get("error") for x in dry["items"] if not x["ok"]}
    assert set(errs) == {1, 2, 3, 4}
    assert "NOPE" in errs[1] and "after the data date" in errs[2] and "bogus" in errs[3] and "Nothing" in errs[4]
    assert "confirmation_token" not in dry and task(p6, "A1020")[0] == 120.0  # nothing to confirm, nothing written
    with pytest.raises(ValueError, match="At most 500"):
        p6.call("p6_update_activities", project="DEMO-A", items=[{"task_code": "A1020"}] * 501)


def test_single_tool_keeps_its_answers(p6):
    assert p6.call("p6_update_activity", project="DEMO-A", task_code="A1020") == {
        "applied": False, "error": "Nothing to change."}
    with pytest.raises(ValueError, match="duration must be 0 or more"):
        p6.call("p6_update_activity", project="DEMO-A", task_code="A1020", remaining_duration_days=-1)
    dry = p6.call("p6_update_activity", project="DEMO-A", task_code="A1030", actual_start="2026-04-01",
                  allow_after_data_date=True)
    assert dry["would_do"][0] == "A1030: actual start -> 2026-04-01 08:00:00 (In Progress)"
    assert any(x.startswith("Note: actual_start 2026-04-01 08:00:00 is after the data date") for x in dry["would_do"])


def revert(p6, changes):
    s = p6.s
    with s._write_session(True) as (con, _backup):
        log = []
        for c in changes:
            log += s._eppm_revert(con, c)["log"]
        return s._commit(con, ["revert"], True, log=log)


def test_revert_gives_back_the_pulled_values_and_nets_to_nothing(p6):
    """The local revert of p6_eppm_discard, on an ordinary database: activity, assignment (with the activity's
    unit totals) and the three relationship actions; afterwards the edit log nets to no change."""
    assignments(p6)
    start = p6.s._log_ts()
    a1020, eng = task(p6, "A1020"), row(p6, 401)
    links = p6.sql("SELECT task_pred_id, pred_task_id, task_id, pred_type, lag_hr_cnt FROM TASKPRED ORDER BY 1")
    code = dict(p6.sql("SELECT task_id, task_code FROM TASK"))
    upd, gone = links[0], links[1]
    p6.apply("p6_update_activity", project="DEMO-A", task_code="A1020", remaining_duration_days=12, task_name="X")
    p6.apply("p6_update_assignment", project="DEMO-A", task_code="A1020", resource="ENG", budgeted_units=180)
    p6.apply("p6_add_relationship", project="DEMO-A", predecessor_code="A1000", successor_code="A1060",
             rel_type="SS")
    p6.apply("p6_update_relationship", project="DEMO-A", predecessor_code=code[upd[1]], successor_code=code[upd[2]],
             lag_days=3)
    p6.apply("p6_delete_relationship", project="DEMO-A", predecessor_code=code[gone[1]], successor_code=code[gone[2]])
    es = lambda: [e for e in p6.s._log_entries() if e["ts"] >= start]  # noqa: E731
    changes = pending.net(es())
    assert sorted((c["table"], c["action"]) for c in changes) == [
        ("TASK", "update"), ("TASKPRED", "create"), ("TASKPRED", "delete"), ("TASKPRED", "update"),
        ("TASKRSRC", "update")]
    assert {c["id"] for c in changes} >= {"TASK:103", "TASKRSRC:401", f"TASKPRED:{upd[0]}", f"TASKPRED:{gone[0]}"}
    asg = next(c for c in changes if c["table"] == "TASKRSRC")
    assert asg["rollups"]["target_work_qty"] == {"pulled": 120.0, "local": 180.0}
    revert(p6, changes)
    assert task(p6, "A1020") == a1020 and row(p6, 401) == eng
    assert p6.sql("SELECT task_pred_id, pred_task_id, task_id, pred_type, lag_hr_cnt FROM TASKPRED ORDER BY 1") == links
    assert pending.net(es()) == []  # every change cancelled by its revert
