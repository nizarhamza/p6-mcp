"""Smaller results (3.5.0) on the synthetic project DEMO-A: detail='brief' of the activity tools (rows without null
values, get_activity links without their constant keys; fields= still applies), schedule_state.note only when the
state needs action or explain=True, and explain=True giving back the fixed texts of p6_compare_projects. The
expected rows are the tools' own detail='full' rows: brief must be exactly them without the null values."""

import sqlite3

import pytest

from test_dashboard import enrich

LISTS = (("p6_query_activities", {}), ("p6_critical_path", {"include_completed": True}),
         ("p6_lookahead", {"days": 366, "include_completed": True}), ("p6_float_paths", {"include_completed": True}),
         ("p6_milestones", {"codes": ["A1000", "A1020", "A1050"]}))


@pytest.fixture
def demo(p6):
    enrich(p6)  # a baseline DEMO-A-BL, assignments and float paths
    return p6


def no_null(row: dict) -> dict:
    return {k: v for k, v in row.items() if v is not None}


@pytest.mark.parametrize("tool,kw", LISTS, ids=[t for t, _ in LISTS])
def test_brief_rows_are_full_rows_without_nulls(demo, tool, kw):
    full = demo.call(tool, project="DEMO-A", **kw)
    brief = demo.call(tool, project="DEMO-A", detail="brief", **kw)
    assert full["rows"] and any(None in r.values() for r in full["rows"])  # the case is not empty
    assert brief["rows"] == [no_null(r) for r in full["rows"]]
    assert all(None not in r.values() for r in brief["rows"])
    # paging and the sibling keys do not change
    assert {k: v for k, v in brief.items() if k != "rows"} == {k: v for k, v in full.items() if k != "rows"}
    assert demo.call(tool, project="DEMO-A", detail="full", **kw) == full
    with pytest.raises(ValueError, match="detail must be"):
        demo.call(tool, project="DEMO-A", detail="short", **kw)


def test_brief_with_fields(demo):
    fields = ["task_name", "act_end_date", "cstr_date", "total_float_d"]
    full = demo.call("p6_query_activities", project="DEMO-A", fields=fields)
    brief = demo.call("p6_query_activities", project="DEMO-A", fields=fields, detail="brief")
    assert all(list(r) == ["task_code", *fields] for r in full["rows"])
    assert brief["rows"] == [no_null(r) for r in full["rows"]]
    done = {r[0] for r in demo.sql("SELECT task_code FROM TASK WHERE proj_id = 2 AND act_end_date IS NOT NULL")}
    assert done and {r["task_code"] for r in brief["rows"] if "act_end_date" in r} == done
    page = demo.call("p6_critical_path", project="DEMO-A", fields=["task_name"], detail="brief", limit=2)
    assert all(list(r) == ["task_code", "task_name"] for r in page["rows"]) and page["returned"] == 2


def test_get_activity_brief(demo):
    full = demo.call("p6_get_activity", project="DEMO-A", task_code="A1020")
    brief = demo.call("p6_get_activity", project="DEMO-A", task_code="A1020", detail="brief")
    assert full["predecessors"] and full["successors"]
    for key in ("predecessors", "successors"):
        assert {r["direction"] for r in full[key]} == {key[:-1]}
        assert {r["other_project"] for r in full[key]} == {"DEMO-A"}
        drop = ("direction", "lag_h", "other_project")
        assert brief[key] == [{k: v for k, v in r.items() if v is not None and k not in drop} for r in full[key]]
        assert all("lag_d" in r and "type" in r for r in brief[key])
    assert brief["assignments"] == [no_null(r) for r in full["assignments"]]
    lists = {k for k, v in full.items() if isinstance(v, list)}
    assert {k: v for k, v in brief.items() if k not in lists} == no_null({k: v for k, v in full.items()
                                                                           if k not in lists})
    assert brief["schedule_state"] == full["schedule_state"] and brief["calendar"] == full["calendar"]
    with pytest.raises(ValueError, match="detail must be"):
        demo.call("p6_get_activity", project="DEMO-A", task_code="A1020", detail="")


def test_schedule_state_note_only_when_needed(demo):
    st = demo.call("p6_project_summary", project="DEMO-A")["schedule_state"]
    assert (st["stale"], st["note"], list(st)) == (False, None, ["data_date", "last_schedule_date", "stale",
                                                                 "edited_since_last_schedule", "note"])
    words = demo.call("p6_compare_projects", project="DEMO-A", explain=True)["schedule_state"]["note"]
    assert words.startswith("Dates, float and paths come from the last F9 in P6.") and "p6_edit_log" in words
    demo.apply("p6_update_activity", project="DEMO-A", task_code="A1020", remaining_duration_days=12)
    st = demo.call("p6_critical_path", project="DEMO-A")["schedule_state"]
    assert st["stale"] is True and "press F9" in st["note"]
    con = sqlite3.connect(demo.db)
    con.execute("UPDATE PROJECT SET last_schedule_date = NULL WHERE proj_id = 2")
    con.commit()
    con.close()
    st = demo.call("p6_float_paths", project="DEMO-A")["schedule_state"]
    assert "last_schedule_date is empty" in st["note"]


def test_compare_explain(demo):
    plain = demo.call("p6_compare_projects", project="DEMO-A")
    words = demo.call("p6_compare_projects", project="DEMO-A", explain=True)
    s = words["summary"]["activities"]
    assert "float_eroded" in s["float_definition"] and "project setting" in s["float_definition"]
    assert "float_definition" not in plain["summary"]["activities"]
    words["summary"]["activities"].pop("float_definition")
    words["schedule_state"]["note"] = None
    assert words == plain
