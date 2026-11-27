"""p6_cpm_validate explains every difference, p6_schedule_whatif flags its assumptions (server 2.11.0).

DEMO-A (the synthetic project, F9 stored on 2026-03-02 09:15, every row stamped before it, no triggers) is
the frozen copy: the test's SQL changes it with explicit update dates, one fixture per cause_class. stale_f9:
a change stamped after the F9 (the activity, the scheduling options, a deleted row). data: a missing calendar,
then no F9 recorded. engine: a constraint on an activity whose stored dates were moved before the F9. unknown:
the same on an activity without a rule of its own. The what-if flags: duration cuts, open ends, hard
constraints, negative lags, SS with lag 0, near critical, finish convergence, DCMA before and after."""

import sqlite3

import pytest

AFTER = "2026-03-03 10:00:00"  # a day after the F9 of DEMO-A
CAVEAT = ("The engine calculates these dates the way P6 would. It does not check that crews, materials or access "
          "allow them.")


def write(p6, q: str, *args) -> None:
    """The test's own change to its copy of the database (p6.sql reads only: it never commits)."""
    con = sqlite3.connect(p6.db)
    try:
        con.execute(q, args)
        con.commit()
    finally:
        con.close()


@pytest.fixture(autouse=True)
def frozen(p6):
    """DEMO-A as P6 25.12 stores it: a completed activity keeps early dates at the data date (the synthetic
    project has them on its actual dates). Stamped before the F9, like every other row."""
    write(p6, "UPDATE TASK SET early_start_date = '2026-03-02 08:00:00', early_end_date = '2026-03-02 08:00:00' "
           "WHERE task_code = 'A1000'")
    return p6


def validate(p6, **kw):
    return p6.call("p6_cpm_validate", project="DEMO-A", **kw)


def whatif(p6, edits, **kw):
    return p6.call("p6_schedule_whatif", project="DEMO-A", edits=edits, **kw)


def row(v, code):
    return next((r for r in v["rows"] if r["task_code"] == code), {})


def test_the_frozen_copy_is_not_stale(p6):
    v = validate(p6)
    assert v["total"] == 0 and v["match_rate"] == 1.0 and v["stale_f9"] is False and v["page_note"] is None
    assert v["cause_counts"] == {"stale_f9": 0, "data": 0, "engine": 0, "unknown": 0}
    d = v["stale_f9_detail"]
    assert d["changed_after"] == "2026-03-02 09:20:00" and d["basis"].startswith("last F9 + 5 min")
    assert d["activities_changed"] == {"count": 0, "sample": []} and d["latest_change"] is None
    pc = v["project_context"]
    assert pc["data_date"] == "2026-03-02 08:00:00" and pc["last_schedule_date"] == "2026-03-02 09:15:00"
    assert pc["calendar"]["work_week"] == "Mon-Fri 08:00-12:00, 13:00-17:00"
    assert pc["scheduling"]["lag_calendar"] == "predecessor calendar"
    assert pc["scheduling"]["out_of_sequence"] == "retained logic"
    assert pc["scheduling"]["critical"] == "total float <= 0 h"


def test_a_change_after_the_f9_is_stale_f9(p6):
    write(p6, "UPDATE TASK SET remain_drtn_hr_cnt = 200, update_date = ? WHERE task_code = 'A1020'", AFTER)
    v = validate(p6)
    a = row(v, "A1020")
    assert v["stale_f9"] is True and v["match_rate"] < 1
    assert v["stale_f9_detail"]["activities_changed"] == {"count": 1, "sample": ["A1020"]}
    assert v["stale_f9_detail"]["latest_change"] == AFTER
    assert a["cause_class"] == "stale_f9" and a["inherited_from"] is None
    assert a["evidence"][0] == f"the activity was changed at {AFTER}"
    assert all(r["cause_class"] == "stale_f9" for r in v["rows"])
    assert v["cause_counts"] == {"stale_f9": v["total"], "data": 0, "engine": 0, "unknown": 0}
    succ = row(v, "A1030")
    assert succ["inherited_from"] == {"task_code": "A1020", "task_name": "Procurement"}
    assert succ["probable_cause"].startswith("inherits the early date difference of A1020 - Procurement")
    ctx = a["context"]
    act = ctx["activity"]
    assert (act["task_type"], act["status_code"]) == ("TT_Task", "TK_NotStart")
    assert (act["remaining_d"], act["original_d"]) == (25.0, 15.0)
    assert act["constraint"] is None and act["actual_start"] is None
    assert act["calendar"]["name"] == "Standard 5 day" and act["calendar"]["hours_per_day"] == 8.0
    # the earliest differing date is the stored finish (3 Apr), the Easter Monday holiday falls in its window
    cal = act["calendar"]
    assert cal["around"] == "2026-04-03 17:00:00" and cal["holidays"] == ["2026-04-06"]
    assert (cal["from"], cal["to"]) == ("2026-03-20", "2026-04-20")
    assert "2026-04-04" in cal["non_work_days"] and "2026-04-06" in cal["non_work_days"]
    assert ctx["predecessors"] == [{"predecessor": "A1010", "predecessor_name": "Detailed design", "type": "FS",
                                    "lag_d": 0.0, "lag_calendar": "Standard 5 day", "drives_stored": True,
                                    "drives_engine": True}]
    assert ctx["predecessor_count"] == 1


def test_options_and_deletions_after_the_f9_mark_the_project(p6):
    write(p6, "UPDATE PROJPROP SET update_date = ? WHERE proj_id = 2 AND prop_name = 'scheduling'", AFTER)
    write(p6, "INSERT INTO REFRDEL (delete_date, table_name, proj_id) VALUES (?, 'TASKPRED', 2)", AFTER)
    write(p6, "UPDATE TASK SET remain_drtn_hr_cnt = 120 WHERE task_code = 'A1030'")  # a mismatch to explain
    v = validate(p6)
    d = v["stale_f9_detail"]
    assert v["stale_f9"] and d["schedule_options_changed"] == AFTER and d["rows_deleted"] == {"relationships": 1}
    assert d["activities_changed"]["count"] == 0
    assert v["rows"] and all(r["cause_class"] == "stale_f9" for r in v["rows"])
    assert all(any("scheduling options were changed" in e for e in r["evidence"]) for r in v["rows"])


def test_invalid_input_is_data(p6):
    write(p6, "UPDATE TASK SET clndr_id = 99 WHERE task_code = 'A1030'")
    v = validate(p6)
    a = row(v, "A1030")
    assert v["stale_f9"] is False and a["cause_class"] == "data"
    assert "calendar 99 missing" in a["probable_cause"] and a["context"]["activity"]["calendar"]["error"] == "missing"
    assert v["cause_counts"]["data"] == v["total"] > 0
    write(p6, "UPDATE PROJECT SET last_schedule_date = NULL WHERE proj_id = 2")
    v = validate(p6)
    assert v["stale_f9"] is False and v["stale_f9_detail"]["changed_after"] is None
    assert all(r["cause_class"] == "data" and r["evidence"][0].startswith("no F9 recorded") for r in v["rows"])


def test_same_inputs_another_result_is_engine_or_unknown(p6):
    write(p6, "UPDATE TASK SET cstr_type = 'CS_MSOA', cstr_date = '2026-02-02 08:00:00', "
           "early_start_date = '2026-03-03 08:00:00', early_end_date = '2026-03-09 17:00:00' WHERE task_code = 'A1060'")
    write(p6, "UPDATE TASK SET early_start_date = '2026-05-13 08:00:00', early_end_date = '2026-05-26 17:00:00' "
           "WHERE task_code = 'A1040'")
    v = validate(p6)
    e, u = row(v, "A1060"), row(v, "A1040")
    assert v["total"] == 2 and v["stale_f9"] is False
    assert v["cause_counts"] == {"stale_f9": 0, "data": 0, "engine": 1, "unknown": 1}
    assert e["cause_class"] == "engine" and "Start On or After constraint" in e["probable_cause"]
    assert e["context"]["activity"]["constraint"] == {"type": "Start On or After", "date": "2026-02-02 08:00:00"}
    assert u["cause_class"] == "unknown" and u["probable_cause"].startswith("unknown:")
    assert v["match_rate"] == round(1 - v["counts"]["open_mismatched"] / v["counts"]["open"], 4)
    assert v["trusted"] is False and p6.s.CPM_TRUST == 0.99


def test_whatif_no_change_no_flag_and_dcma_as_the_health_check(p6):
    before = p6.hash()
    w = whatif(p6, [{"op": "update_link", "predecessor_code": "A1010", "successor_code": "A1020", "lag_days": 0}])
    a = w["assumptions"]
    assert a["flag_count"] == 0 and w["caveat"] == CAVEAT and a["max_cut_pct"] == 30.0
    assert a["dcma"]["before"] == a["dcma"]["after"] and a["dcma"]["changed"] == []
    hc = p6.call("p6_health_check", project="DEMO-A", save_snapshot=False)["checks"]
    assert set(a["dcma"]["before"]) == set(hc) - {"key_dates"}
    for k, v in a["dcma"]["before"].items():
        assert v == {f: hc[k].get(f) for f in ("count", "pct", "value", "threshold", "pass")}, k
    fc = a["finish_convergence"]
    # A1040 and A1050 both finish on the project finish: one path enters them, from A1030
    assert fc["before"] == fc["after"] == {"driving_links": 1, "predecessors": ["A1030"],
                                           "finish_activities": ["A1040"]}
    assert p6.hash() == before


def test_whatif_duration_cut_flag(p6):
    cut = whatif(p6, [{"op": "duration", "task_code": "A1030", "remaining_days": 10}], dcma=False)["assumptions"]
    assert cut["dcma"] is None
    assert cut["duration_cuts"] == [{"task_code": "A1030", "task_name": "Site works", "edit": "A1030: remaining 10 d",
                                     "remaining_before_d": 25.0, "remaining_after_d": 10.0, "original_d": 25.0,
                                     "cut_d": 15.0, "cut_pct_of_remaining": 60.0, "cut_pct_of_original": 60.0,
                                     "no_effect": None}]
    small = whatif(p6, [{"op": "duration", "task_code": "A1030", "remaining_days": 20}], dcma=False)["assumptions"]
    wide = whatif(p6, [{"op": "duration", "task_code": "A1030", "remaining_days": 10}], max_cut_pct=70,
                  dcma=False)["assumptions"]
    assert small["duration_cuts"] == [] and wide["duration_cuts"] == []


def test_whatif_open_end_flag_and_dcma_logic(p6):
    a = whatif(p6, [{"op": "delete_link", "predecessor_code": "A1000", "successor_code": "A1060"}])["assumptions"]
    assert a["open_ends"] == [{"task_code": "A1060", "task_name": "Permits", "left_with": "no predecessor",
                               "edit": "delete FS A1000 -> A1060"}]
    assert a["dcma"]["after"]["logic"]["count"] == a["dcma"]["before"]["logic"]["count"] + 1
    assert "logic" in a["dcma"]["changed"]


def test_whatif_constraint_lag_and_ss_flags(p6):
    a = whatif(p6, [{"op": "constraint", "task_code": "A1030", "constraint": "mandatory_finish",
                     "constraint_date": "2026-05-11"},
                    {"op": "constraint", "task_code": "A1040", "constraint": "start_on_or_after",
                     "constraint_date": "2026-05-12"},
                    {"op": "add_link", "predecessor_code": "A1060", "successor_code": "A1040", "lag_days": -2},
                    {"op": "add_link", "predecessor_code": "A1020", "successor_code": "A1060", "rel_type": "SS",
                     "lag_days": 0}], compare_to="engine", dcma=False)["assumptions"]
    assert [(r["task_code"], r["constraint"], r["before"]) for r in a["hard_constraints"]] == \
        [("A1030", "Mandatory Finish", None)]
    assert [(r["predecessor"], r["successor"], r["type"], r["lag_d"]) for r in a["negative_lags"]] == \
        [("A1060", "A1040", "FS", -2.0)]
    ss = a["ss_zero_lag"]
    assert [(r["predecessor"], r["successor"]) for r in ss] == [("A1020", "A1060")]
    assert ss[0]["starts_together"] is True and ss[0]["overlap_d"] == 5.0  # Permits (5 d) inside Procurement (15 d)
    assert a["flag_count"] == 3 + a["near_critical"]["became_near_critical"]


def test_whatif_near_critical_flag(p6):
    a = whatif(p6, [{"op": "duration", "task_code": "A1060", "add_remaining_days": 15}], dcma=False)["assumptions"]
    assert a["near_critical"]["became_near_critical"] == 1
    assert a["near_critical"]["rows"] == [{"task_code": "A1060", "task_name": "Permits", "total_float_d_before": 20.0,
                                           "total_float_d_after": 5.0}]


EDIT_KINDS = [[{"op": "duration", "task_code": "A1030", "remaining_days": 30}],
              [{"op": "duration", "task_code": "A1060", "add_remaining_days": 25}],
              [{"op": "add_link", "predecessor_code": "A1060", "successor_code": "A1040", "lag_days": 3}],
              [{"op": "delete_link", "predecessor_code": "A1000", "successor_code": "A1060"}],
              [{"op": "constraint", "task_code": "A1040", "constraint": "start_on_or_after",
                "constraint_date": "2026-06-15"}]]


@pytest.mark.parametrize("edits", EDIT_KINDS,
                         ids=["duration", "add_remaining", "add_link", "delete_link", "constraint"])
@pytest.mark.parametrize("compare_to", ["current", "engine"])
def test_whatif_critical_path_test_reuses_the_engine(p6, monkeypatch, edits, compare_to):
    """The DCMA critical path test of the what-if takes the network and run it already has (3.4.0): the stored
    database before (compare_to current), the edited network after, as the temp tables hold it. The engine is
    loaded once; the test it gives equals the one that reloads the network from the temp tables and runs it."""
    s = p6.s
    cpt, init, loads, seen = s._cpt, s._Cpm.__init__, [], []

    def load(self, con, pid):
        loads.append(pid)
        init(self, con, pid)

    def spy(con, pid, net=None):
        out = cpt(con, pid, net)
        n = len(loads)
        seen.append((net is not None, out == cpt(con, pid)))  # the old way: load and run the engine here
        del loads[n:]
        return out

    monkeypatch.setattr(s._Cpm, "__init__", load)
    monkeypatch.setattr(s, "_cpt", spy)
    a = whatif(p6, edits, compare_to=compare_to)["assumptions"]["dcma"]
    assert seen == [(True, True), (True, True)] and len(loads) == 1
    assert None not in (a["before"]["critical_path_test"]["pass"], a["after"]["critical_path_test"]["pass"])
