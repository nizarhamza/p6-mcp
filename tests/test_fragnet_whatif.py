"""Fragnet edits of p6_schedule_whatif (time impact analysis, 3.6.0) on the synthetic project DEMO-A: a chain
A1000 -> A1010 -> A1020 -> A1030 -> A1040 -> A1050 with A1060 Permits (20 days of float) beside it, calendar
Monday to Friday 08:00-12:00 and 13:00-17:00 with a holiday on Monday 2026-04-06.

The fragnet: F100 Change order instruction (start milestone, Start On or After 2026-03-16) -> F110 Redesign
foundations (25 days) -> F120 Redesign approved (finish milestone) -> A1030 Site works. By hand: F110 works
16 Mar to 20 Apr (25 working days: 5 + 5 + 5 + 4 (holiday on 6 Apr) + 5 + 1), so A1030 starts on 21 Apr instead of
7 Apr and its 25 days end on 25 May instead of 11 May; A1040 (10 days) ends on 8 Jun and A1050 with it: the project
finish moves from 25 May 17:00 to 8 Jun 17:00, 14 calendar days and 10 working days. A1020 and A1010 keep 10 days of
float (7 Apr to 20 Apr), Permits 30 days (9 Mar to 20 Apr). Nothing is written."""

import pytest

FRAGNET = [
    {"op": "add_activity", "task_code": "F100", "task_name": "Change order instruction", "task_type": "TT_Mile",
     "constraint": "start_on_or_after", "constraint_date": "2026-03-16"},
    {"op": "add_activity", "task_code": "F110", "task_name": "Redesign foundations", "original_days": 25, "wbs": "CON"},
    {"op": "add_activity", "task_code": "F120", "task_name": "Redesign approved", "task_type": "TT_FinMile"},
    {"op": "add_link", "predecessor_code": "A1000", "successor_code": "F100"},
    {"op": "add_link", "predecessor_code": "F100", "successor_code": "F110"},
    {"op": "add_link", "predecessor_code": "F110", "successor_code": "F120"},
    {"op": "add_link", "predecessor_code": "F120", "successor_code": "A1030"},
]


def whatif(p6, edits, **kw):
    return p6.call("p6_schedule_whatif", project="DEMO-A", edits=edits, **kw)


def refused(p6, edits, **kw) -> str:
    with pytest.raises(ValueError) as e:
        whatif(p6, edits, **kw)
    return str(e.value)


@pytest.mark.parametrize("compare_to", ["engine", "current"])
def test_fragnet_impact_computed_by_hand(p6, compare_to):
    before = p6.hash()
    w = whatif(p6, FRAGNET, compare_to=compare_to)
    assert p6.hash() == before
    f = w["fragnet"]
    assert w["edits"][:3] == [
        "add activity F100 (TT_Mile, 0 d, WBS DEMO-A), constraint Start On or After 2026-03-16 08:00:00",
        "add activity F110 (TT_Task, 25 d, WBS DEMO-A.CON)", "add activity F120 (TT_FinMile, 0 d, WBS DEMO-A)"]
    acts = {a["task_code"]: a for a in f["activities"]}
    assert list(acts) == ["F100", "F110", "F120"]
    keys = ("early_start", "early_finish", "late_start", "late_finish", "total_float_d", "free_float_d",
            "original_d", "wbs_code", "calendar", "critical", "longest_path", "predecessors", "successors",
            "driven_by", "drives_finish")
    assert {k: acts["F110"][k] for k in keys} == {
        "early_start": "2026-03-16 08:00:00", "early_finish": "2026-04-20 17:00:00",
        "late_start": "2026-03-16 08:00:00", "late_finish": "2026-04-20 17:00:00", "total_float_d": 0.0,
        "free_float_d": 0.0, "original_d": 25.0, "wbs_code": "DEMO-A.CON", "calendar": "Standard 5 day",
        "critical": True, "longest_path": True, "predecessors": ["F100"], "successors": ["F120"],
        "driven_by": ["F100"], "drives_finish": True}
    assert acts["F100"]["early_start"] == acts["F100"]["early_finish"] == "2026-03-16 08:00:00"
    assert acts["F100"]["constraint"] == "Start On or After"
    assert acts["F100"]["driven_by"] == []  # its constraint sets its date
    assert acts["F120"]["early_finish"] == "2026-04-20 17:00:00" and acts["F120"]["wbs_code"] == "DEMO-A"
    imp = f["impact"]
    assert imp["project_finish"] == {"before": "2026-05-25 17:00:00", "after": "2026-06-08 17:00:00", "delay_cd": 14.0,
                                     "delay_wd": 10.0, "finish_activity": "A1050"}
    assert imp["milestones"] == [{"task_code": "A1050", "task_name": "Project complete", "task_type": "TT_FinMile",
                                  "date_before": "2026-05-25 17:00:00", "date_after": "2026-06-08 17:00:00",
                                  "delay_cd": 14.0, "delay_wd": 10.0, "delayed": True}]
    assert imp["milestones_total"] == imp["milestones_delayed"] == 1 and imp["calendar"] == "Standard 5 day"
    dp = f["driving_path"]
    assert dp["drives_finish"] is True and dp["count"] == 6
    assert [(r["task_code"], r["fragnet"], r["driven_by"], r["on_project_finish"]) for r in dp["rows"]] == [
        ("F100", True, [], False), ("F110", True, ["F100"], False), ("F120", True, ["F110"], False),
        ("A1030", False, ["F120"], False), ("A1040", False, ["A1030"], True), ("A1050", False, ["A1040"], True)]
    a1030 = dp["rows"][3]
    assert (a1030["early_start"], a1030["early_finish"]) == ("2026-04-21 08:00:00", "2026-05-25 17:00:00")
    # the rest of the what-if sees the same network: the finish, the existing activities, the float
    assert w["project_finish"]["after"] == "2026-06-08 17:00:00" and w["project_finish"]["delta_wd"] == 10.0
    rows = {r["task_code"]: r for r in w["rows"]}
    if compare_to == "engine":  # current: A1000's stored early dates are its actual dates, the engine's the data date
        assert set(rows) == {"A1010", "A1020", "A1030", "A1040", "A1050", "A1060"}  # fragnet rows: in fragnet only
    assert rows["A1020"]["total_float_d_after"] == 10.0 and rows["A1060"]["total_float_d_after"] == 30.0
    assert w["float_change"]["critical_after"] == 3 and w["float_change"]["no_longer_critical"] == 2
    a = w["assumptions"]
    assert a["flag_count"] == 0 and a["open_ends"] == []
    # DCMA after: the fragnet counts as new activities (3 open, 4 links, one task without resources)
    d = a["dcma"]
    assert d["after"]["fs_links"]["count"] == d["before"]["fs_links"]["count"] + 4
    assert d["after"]["resources"]["count"] == d["before"]["resources"]["count"] + 1
    assert d["after"]["logic_density"]["value"] == round(11 / 9, 4)


def test_fragnet_with_float_does_not_drive_the_finish(p6):
    w = whatif(p6, [{"op": "add_activity", "task_code": "F200", "task_name": "Extra permit", "original_days": 10},
                    {"op": "add_link", "predecessor_code": "A1060", "successor_code": "F200"},
                    {"op": "add_link", "predecessor_code": "F200", "successor_code": "A1030"}], compare_to="engine",
               dcma=False)
    f = w["fragnet"]
    a = f["activities"][0]
    # 9 Mar to 20 Mar, late finish 3 Apr (A1030 starts on 7 Apr, 6 Apr is a holiday): 10 days of float
    assert (a["early_start"], a["early_finish"], a["late_finish"], a["total_float_d"], a["critical"]) == (
        "2026-03-09 08:00:00", "2026-03-20 17:00:00", "2026-04-03 17:00:00", 10.0, False)
    assert a["driven_by"] == ["A1060"] and a["drives_finish"] is False
    assert f["driving_path"] == {**f["driving_path"], "drives_finish": False, "count": 0, "rows": []}
    assert f["impact"]["project_finish"]["delay_wd"] == 0.0 and f["impact"]["milestones_delayed"] == 0
    # Permits keeps its dates; its float drops from 20 to 10 days
    assert [(r["task_code"], r["early_finish_after"], r["total_float_d_before"], r["total_float_d_after"])
            for r in w["rows"]] == [("A1060", "2026-03-06 17:00:00", 20.0, 10.0)]


def test_fragnet_edits_on_fragnet_activities_and_open_ends(p6):
    w = whatif(p6, [{"op": "add_activity", "task_code": "F300", "task_name": "Late inspection", "original_days": 5,
                     "calendar": "standard 5 DAY"},
                    {"op": "add_link", "predecessor_code": "A1040", "successor_code": "F300"},
                    {"op": "duration", "task_code": "F300", "remaining_days": 8},
                    {"op": "add_activity", "task_code": "F310", "task_name": "Loose end", "original_days": 2}],
               compare_to="engine", dcma=False)
    acts = {a["task_code"]: a for a in w["fragnet"]["activities"]}
    # F300 follows A1040 (ends 25 May) for 8 days: 26 May to 4 Jun, the new project finish (no successor)
    assert acts["F300"]["original_d"] == 8.0 and acts["F300"]["early_finish"] == "2026-06-04 17:00:00"
    assert w["project_finish"]["after"] == "2026-06-04 17:00:00" and w["project_finish"]["finish_activity"] == "F300"
    assert w["fragnet"]["impact"]["project_finish"]["delay_wd"] == 8.0
    # A1050 (the finish milestone) does not move: nothing links F300 to it
    assert w["fragnet"]["impact"]["milestones"][0]["delay_wd"] == 0.0
    assert [r["task_code"] for r in w["fragnet"]["driving_path"]["rows"]] == ["F300"]
    ends = {(r["task_code"], r["left_with"]) for r in w["assumptions"]["open_ends"]}
    assert ends == {("F300", "no successor"), ("F310", "no predecessor"), ("F310", "no successor")}
    assert acts["F310"]["early_start"] == "2026-03-02 08:00:00"  # no predecessor: from the data date


def test_fragnet_link_ops_and_no_fragnet_block_without_it(p6):
    w = whatif(p6, [{"op": "update_link", "predecessor_code": "A1010", "successor_code": "A1020", "lag_days": 0}],
               dcma=False)
    assert "fragnet" not in w
    w = whatif(p6, FRAGNET + [{"op": "update_link", "predecessor_code": "F120", "successor_code": "A1030",
                               "lag_days": 5},
                              {"op": "delete_link", "predecessor_code": "A1000", "successor_code": "F100"}],
               compare_to="engine", dcma=False)
    assert w["fragnet"]["impact"]["project_finish"]["delay_wd"] == 15.0  # 10 + the 5 days of lag
    assert ("F100", "no predecessor") in {(r["task_code"], r["left_with"]) for r in w["assumptions"]["open_ends"]}


def test_fragnet_validation(p6):
    act = {"op": "add_activity", "task_code": "F1", "task_name": "New work", "original_days": 3}
    assert "already exists in project DEMO-A" in refused(p6, [{**act, "task_code": "A1030"}])
    assert "already a fragnet activity" in refused(p6, [act, act])
    assert "edit 1 (add_link): Activity F9 not found in project DEMO-A" in refused(
        p6, [act, {"op": "add_link", "predecessor_code": "F9", "successor_code": "F1"}])
    assert "add_activity edit before its links" in refused(
        p6, [{"op": "add_link", "predecessor_code": "F1", "successor_code": "A1030"}, act])
    loop = refused(p6, FRAGNET + [{"op": "add_link", "predecessor_code": "A1030", "successor_code": "F110"}])
    assert loop == ("edit 7 (add_link): A1030 -> F110 would close a loop (P6 refuses to schedule it): "
                    "A1030 -> F110 -> F120 -> A1030")
    assert "would close a loop" in refused(
        p6, [{"op": "add_link", "predecessor_code": "A1050", "successor_code": "A1010"}])  # real activities too
    assert "a milestone has no duration" in refused(p6, [{**act, "task_type": "TT_Mile"}])
    assert "task_type must be one of" in refused(p6, [{**act, "task_type": "TT_LOE"}])
    assert "original_days is required" in refused(p6, [{k: v for k, v in act.items() if k != "original_days"}])
    assert "original_days must be 0 or more" in refused(p6, [{**act, "original_days": -1}])
    assert "task_name is required" in refused(p6, [{**act, "task_name": " "}])
    assert "calendar 'Night' not found" in refused(p6, [{**act, "calendar": "Night"}])
    assert "WBS code 'XYZ' not found in DEMO-A" in refused(p6, [{**act, "wbs": "XYZ"}])
    assert "constraint_date is required" in refused(p6, [{**act, "constraint": "start_on_or_after"}])
    assert "is a fragnet activity" in refused(p6, FRAGNET, milestones=["F110"])


def test_fragnet_ids_never_collide_and_temp_tables_hold_the_fragnet(p6, monkeypatch):
    """Fragnet task_ids start above every TASK row; the DCMA critical path test of the edited network (the
    reloaded network in memory) equals the one read back from the temp tables that hold the fragnet rows."""
    s = p6.s
    con = s._connect()
    try:
        top = con.execute("SELECT MAX(task_id) FROM TASK").fetchone()[0]
        ctx = s._fragnet_ctx(con, 2)
    finally:
        con.close()
    assert ctx["next_id"] == top + 1 and ctx["wbs"][0]["wbs_code"] == "DEMO-A"
    cpt, seen = s._cpt, []

    def spy(con, pid, net=None):
        out = cpt(con, pid, net)
        n = con.execute("SELECT COUNT(*) FROM TASK WHERE task_id > ?", (top,)).fetchone()[0]
        seen.append((n, out == cpt(con, pid)))
        return out

    from p6mcp.core import explain
    monkeypatch.setattr(explain, "_cpt", spy)
    d = whatif(p6, FRAGNET, compare_to="engine")["assumptions"]["dcma"]
    assert seen == [(0, True), (3, True)]
    assert d["after"]["critical_path_test"]["pass"] is True
