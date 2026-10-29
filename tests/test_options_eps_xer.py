"""Schedule options (read and write), EPS creation and the XER inspection on the synthetic database.

Fixture options (PROJPROP 'scheduling' of DEMO-A): lag on the predecessor calendar, retained logic Y, progress
override N, lag from the early start Y, float type FT_TotFloat. One EPS node EPS-DEMO holds DEMO-A.
"""

import os

import pytest
from make_db import SCHED

FIXTURE_OPTIONS = {"sched_calendar_on_relationship_lag": "rcal_Predecessor", "sched_retained_logic": "Y",
                   "sched_progress_override": "N", "sched_lag_early_start_flag": "Y", "sched_float_type": "FT_TotFloat"}


def db_options(p6) -> dict:
    return {o["option"]: o["db"] for o in p6.call("p6_schedule_options", project="DEMO-A")["options"]}


def prop(p6, name: str):
    rows = p6.sql("SELECT prop_value FROM PROJPROP WHERE proj_id = 2 AND prop_name = ?", name)
    v = rows[0][0] if rows else None
    return v.decode("utf-8") if isinstance(v, bytes) else v


def export(p6, name: str = "demo.xer") -> str:
    path = os.path.join(os.path.dirname(p6.db), name)
    p6.call("p6_export_xer", project="DEMO-A", path=path)
    return path


def test_schedule_options_read(p6):
    res = p6.call("p6_schedule_options", project="DEMO-A")
    assert {o["option"]: o["db"] for o in res["options"]} == FIXTURE_OPTIONS
    assert res["xer"] is None and res["summary"]["only_in_db"] == len(FIXTURE_OPTIONS)
    assert res["lag_rule"]["db_effective"] == "rcal_Predecessor"
    side = p6.call("p6_schedule_options", project="DEMO-A", xer_path=export(p6))  # the export writes SCHEDOPTIONS
    assert side["xer"]["has_schedoptions"] and side["summary"]["different"] == 0
    with pytest.raises(ValueError, match="give project, xer_path or both"):
        p6.call("p6_schedule_options")


def test_set_schedule_options(p6):
    before = p6.hash()
    dry = p6.call("p6_set_schedule_options", project="DEMO-A", options={"sched_float_type": "FT_FF"})
    assert dry["dry_run"] and p6.hash() == before
    assert dry["options"][0] == {"option": "sched_float_type", "old": "FT_TotFloat", "new": "FT_FF", "changed": True}
    assert sum(o["changed"] for o in dry["options"]) == 1

    res = p6.apply("p6_set_schedule_options", project="DEMO-A", options={"sched_float_type": "FT_FF"},
                   check_options={"logic_target": 5})
    assert res["applied"] is True
    assert prop(p6, "scheduling") == SCHED.replace("FT_TotFloat", "FT_FF")  # the rest of the text kept as stored
    assert "logic_target|5" in prop(p6, "schedule_check")
    assert db_options(p6) == {**FIXTURE_OPTIONS, "sched_float_type": "FT_FF"}

    with pytest.raises(ValueError, match="cannot both be Y"):
        p6.call("p6_set_schedule_options", project="DEMO-A", options={"sched_progress_override": "Y"})
    with pytest.raises(ValueError, match="Unknown schedule option"):
        p6.call("p6_set_schedule_options", project="DEMO-A", options={"no_such_option": "Y"})


def test_create_eps(p6):
    before = p6.hash()
    dry = p6.call("p6_create_eps", eps_id="EPS-NEW", eps_name="New EPS", parent_eps="EPS-DEMO")
    assert dry["dry_run"] and "Create EPS EPS-NEW 'New EPS' under EPS-DEMO" in dry["would_do"][0]
    assert p6.hash() == before
    assert p6.apply("p6_create_eps", eps_id="EPS-NEW", eps_name="New EPS", parent_eps="EPS-DEMO")["applied"] is True
    eps = {r["eps_id"]: r for r in p6.call("p6_list_eps", order="tree")["rows"]}
    assert set(eps) == {"EPS-DEMO", "EPS-NEW"}
    assert (eps["EPS-NEW"]["depth"], eps["EPS-NEW"]["path"]) == (1, "Demo EPS > New EPS")

    again = p6.call("p6_create_eps", eps_id="EPS-NEW", eps_name="Twice")
    assert again["applied"] is False and "already exists" in again["error"]
    taken = p6.call("p6_create_eps", eps_id="DEMO-A", eps_name="Clash")
    assert taken["applied"] is False and "already a Project ID" in taken["error"]


def test_inspect_xer(p6):
    path = export(p6)
    res = p6.call("p6_inspect_xer", path=path)
    assert res["tables"]["TASK"] == 7 and res["tables"]["TASKPRED"] == 7
    (proj,) = res["projects"]
    assert (proj["short_name"], proj["activities"]) == ("DEMO-A", 7) and proj["data_date"].startswith("2026-03-02")
    assert res["preflight"]["project"] == "DEMO-A" and res["preflight"]["errors"] == []

    with open(path, "rb") as fh:  # the second activity takes the first one's Activity ID: an import would refuse
        lines = fh.read().split(b"\r\n")
    at = lines.index(b"%T\tTASK") + 1
    col = lines[at].split(b"\t").index(b"task_code")
    first, second = (lines[at + k].split(b"\t") for k in (1, 2))
    second[col] = first[col]
    lines[at + 2] = b"\t".join(second)
    bad = os.path.join(os.path.dirname(p6.db), "duplicate.xer")
    with open(bad, "wb") as fh:
        fh.write(b"\r\n".join(lines))
    errors = p6.call("p6_inspect_xer", path=bad)["preflight"]["errors"]
    assert [(e["check"], e["count"]) for e in errors] == [("duplicate_task_code", 1)], errors
