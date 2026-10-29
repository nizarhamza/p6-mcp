"""p6_schedule_risk on the synthetic project DEMO-A (a chain A1000 -> A1010 -> A1020 -> A1030 -> A1040 -> A1050
with A1060 Permits beside it, 20 days of float): zero-width ranges give the engine's own finish, the same seed
gives the same output, a single-path network gives criticality 1.0 everywhere, ranges and UDF three-point
estimates by precedence, the clamp and the size cap, refusals, nothing written. Also: the cumulative-index
add_work_hours of the calendars against the day by day walk it replaced."""

import json
import random
import sqlite3
from datetime import date, datetime, time, timedelta

import pytest

ZERO = {"min_pct": 100, "mode_pct": 100, "max_pct": 100}


def risk(p6, **kw):
    return p6.call("p6_schedule_risk", project="DEMO-A", **kw)


def whatif_finish(p6, **remaining_days) -> str:
    """Project finish of the engine with these remaining durations (days), from p6_schedule_whatif."""
    edits = [{"op": "duration", "task_code": c, "remaining_days": d} for c, d in remaining_days.items()]
    return p6.call("p6_schedule_whatif", project="DEMO-A", edits=edits, compare_to="engine")["project_finish"]["after"]


def test_policy_and_read_only(p6):
    from p6mcp import policy
    assert policy.TOOL_KIND["p6_schedule_risk"] == policy.READ and "p6_schedule_risk" not in policy.BASIC_TOOLS
    before = p6.hash()
    risk(p6, iterations=100)
    assert p6.hash() == before


def test_zero_width_ranges_reproduce_the_engine(p6):
    r = risk(p6, iterations=100, default_range=ZERO)
    det = r["deterministic"]["finish"]
    assert det == r["deterministic"]["stored_finish"] == "2026-05-25 17:00:00"
    assert det == p6.call("p6_cpm_validate", project="DEMO-A")["project_finish"]["engine"]
    assert set(r["finish"].values()) == {det}
    assert r["on_time_probability"]["deterministic_pct"] == 100.0 and r["p80_buffer_wd"] == 0.0
    assert r["deterministic"]["finish_activity"] == "A1050" and [m["task_code"] for m in r["milestones"]] == ["A1050"]
    assert all(m["p10"] == m["p90"] == m["deterministic"] and m["on_time_pct"] == 100.0 for m in r["milestones"])
    assert sum(h["count"] for h in r["histogram"]) == 100 and r["histogram"][-1]["cum_pct"] == 100.0
    # the critical chain, every iteration; the permits (20 days of float) never
    crit = {x["task_code"]: x["criticality_index"] for x in r["rows"]}
    assert crit == {c: 1.0 for c in ("A1010", "A1020", "A1030", "A1040", "A1050")}
    assert all(x["on_deterministic_path"] for x in r["rows"])
    assert r["engine"] == {"match_rate": 1.0, "trusted": True, "open_mismatched": 0, "warning": None}
    assert r["engine_version"] == p6.s.ENGINE_VERSION and r["caveat"] == p6.s.RISK_CAVEAT


def test_same_seed_same_output(p6):
    a, b = risk(p6), risk(p6)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["seed"] == 1 and a["iterations"] == 1000 and a["iterations_note"] is None
    c = risk(p6, seed=7)
    assert c["seed"] == 7 and c["finish"] != a["finish"]
    # right-skewed default (85 / 100 / 135 %): the median finish is later than the engine's
    assert a["finish"]["p10"] <= a["finish"]["p50"] <= a["finish"]["p80"] <= a["finish"]["p90"]
    assert a["finish"]["p50"] > a["deterministic"]["finish"] and a["p80_buffer_wd"] > 0
    assert a["distribution"]["default"] == {"min_pct": 85.0, "mode_pct": 100.0, "max_pct": 135.0}
    assert a["distribution"]["counts"]["drawn"] == a["distribution"]["counts"]["default"] == 5  # open tasks


def test_single_path_network_is_critical_everywhere(p6):
    con = sqlite3.connect(p6.db)
    con.execute("DELETE FROM TASKPRED WHERE task_pred_id IN (206, 207)")
    con.execute("DELETE FROM TASK WHERE task_id = 107")
    con.commit()
    con.close()
    r = risk(p6, iterations=300)
    assert r["total"] == 5 and {x["criticality_index"] for x in r["rows"]} == {1.0}
    assert {x["task_code"] for x in r["rows"]} == {"A1010", "A1020", "A1030", "A1040", "A1050"}
    assert {x["criticality_pct"] for x in r["rows"]} == {100.0}


def test_ranges_and_udfs_by_precedence(p6):
    ranges = [{"wbs": "ENG", "min_pct": 50, "mode_pct": 50, "max_pct": 50},
              {"task_code": "A1020", "min_pct": 200, "mode_pct": 200, "max_pct": 200}]
    r = risk(p6, iterations=100, default_range=ZERO, ranges=ranges)
    # A1010 (WBS ENG) 10 d -> 5 d; A1020 (its own range wins over ENG) 15 d -> 30 d
    assert r["finish"]["p50"] == r["finish"]["p90"] == whatif_finish(p6, A1010=5, A1020=30)
    assert [(x["matched"], x["applied"]) for x in r["distribution"]["ranges"]] == [(3, 1), (1, 1)]
    counts = r["distribution"]["counts"]
    assert (counts["task"], counts["wbs"], counts["default"]) == (1, 1, 3)
    by = {x["task_code"]: x for x in r["rows"]}
    assert (by["A1010"]["range_source"], by["A1010"]["max_d"]) == ("wbs", 5.0)
    assert (by["A1020"]["range_source"], by["A1020"]["min_d"]) == ("task", 30.0)
    # number UDFs in days beat every range: A1030 (25 d) drawn from 10 / 10 / 10
    con = sqlite3.connect(p6.db)
    con.executemany("INSERT INTO UDFTYPE (udf_type_id, table_name, udf_type_label, logical_data_type) "
                    "VALUES (?, 'TASK', ?, ?)", [(601, "Opt", "FT_FLOAT_2_DECIMALS"),
                                                 (602, "ML", "FT_FLOAT_2_DECIMALS"),
                                                 (603, "Pess", "FT_FLOAT_2_DECIMALS"), (604, "Note", "FT_TEXT")])
    con.executemany("INSERT INTO UDFVALUE (udf_type_id, fk_id, proj_id, udf_number) VALUES (?, 104, 2, 10)",
                    [(601,), (602,), (603,)])
    con.execute("INSERT INTO UDFVALUE (udf_type_id, fk_id, proj_id, udf_number) VALUES (601, 105, 2, 9)")  # alone
    con.commit()
    con.close()
    u = risk(p6, iterations=100, default_range=ZERO, ranges=[{"task_code": "A1030", "min_pct": 300, "mode_pct": 300,
                                                               "max_pct": 300}],
             udf_optimistic="Opt", udf_most_likely="ML", udf_pessimistic="Pess")
    assert u["finish"]["p50"] == whatif_finish(p6, A1030=10)
    assert u["distribution"]["udfs"] == {"optimistic": "Opt", "most_likely": "ML", "pessimistic": "Pess",
                                         "activities_with_values": 1, "invalid": 1}
    assert u["distribution"]["counts"]["udf"] == 1 and u["distribution"]["ranges"][0]["applied"] == 0
    with pytest.raises(Exception, match="go together"):
        risk(p6, udf_optimistic="Opt")
    with pytest.raises(Exception, match="not a number"):
        risk(p6, udf_optimistic="Opt", udf_most_likely="ML", udf_pessimistic="Note")


def test_clamp_cap_and_refusals(p6, monkeypatch):
    low = risk(p6, iterations=5)
    assert low["iterations"] == 100 and low["iterations_requested"] == 5 and "clamped" in low["iterations_note"]
    monkeypatch.setattr(p6.s, "RISK_WORK", 1200)  # 6 activities recomputed per iteration: at most 200 iterations
    capped = risk(p6, iterations=1000)
    assert capped["iterations"] == 200 and capped["distribution"]["counts"]["recomputed_per_iteration"] == 6
    assert "capped at 1,200 activity passes" in capped["iterations_note"]
    for bad, msg in (({"min_pct": 120, "mode_pct": 100, "max_pct": 130}, "min_pct <= mode_pct"),
                     ({"mode_pct": 100, "max_pct": 130}, "required numbers")):
        with pytest.raises(Exception, match=msg):
            risk(p6, default_range=bad)
    for rng, msg in (({"task_code": "NOPE", **ZERO}, "not found"), ({"task_code": "A1010", "wbs": "ENG", **ZERO},
                                                                    "exactly one"),
                     ({"wbs": "NOPE", **ZERO}, "WBS code"),
                     ({"task_code": "A1010", "colour": 1, **ZERO}, "unknown keys"),
                     ({"code_type": "Phase", **ZERO}, "needs code_value")):
        with pytest.raises(Exception, match=msg):
            risk(p6, ranges=[rng])
    with pytest.raises(Exception, match="two ranges"):
        risk(p6, ranges=[{"task_code": "A1010", **ZERO}, {"task_code": "A1010", **ZERO}])
    with pytest.raises(Exception, match="not found"):
        risk(p6, milestones=["NOPE"])
    con = sqlite3.connect(p6.db)
    con.execute("INSERT INTO TASKPRED (task_pred_id, task_id, pred_task_id, proj_id, pred_proj_id, pred_type, "
                "lag_hr_cnt) VALUES (290, 102, 105, 2, 2, 'PR_FS', 0)")  # A1040 -> A1010 closes a loop
    con.commit()
    con.close()
    with pytest.raises(Exception, match="loop"):
        risk(p6)


def _walk(cal, start: datetime, hours: float) -> datetime:
    """add_work_hours as it was before the cumulative index: day by day, shift by shift."""
    left = round(hours * 60)
    if not left:
        return start
    d, t0 = start.date(), (start - datetime.combine(start.date(), time())).total_seconds() / 60
    for _ in range(10000):
        for s, f in cal.shifts(d):
            lo = max(s, t0)
            if f > lo:
                if left <= f - lo:
                    return datetime.combine(d, time()) + timedelta(minutes=lo + left)
                left -= f - lo
        d, t0 = d + timedelta(days=1), 0
    raise AssertionError("no working time")


def test_add_work_hours_matches_the_day_walk(server):
    day = ((480, 720), (780, 1020))  # 08:00-12:00, 13:00-17:00
    week = (day, day, day, day, ((480, 720),), (), ())  # Monday to Thursday, Friday morning
    exc = {date(2026, 4, 6): (), date(2026, 5, 1): (), date(2026, 5, 2): ((600, 900),), date(2026, 12, 24): ()}
    cals = [server._Cal(1, "two shifts", 8.0, week, exc),
            server._Cal(2, "24 hours", 24.0, (((0, 1440),),) * 7, {date(2026, 6, 1): ()}),
            server._Cal(3, "nights", 8.0, (((0, 360), (1320, 1440)),) * 5 + ((), ()), {})]
    rnd = random.Random(3)
    for cal in cals:
        for _ in range(3000):
            start = datetime(2026, 1, 1) + timedelta(minutes=rnd.randrange(0, 600 * 1440),
                                                     seconds=rnd.choice((0, 0, 30)))
            hours = rnd.choice((rnd.uniform(0, 12), rnd.uniform(0, 400), rnd.uniform(0, 3000), 8.0, 40.0))
            assert cal.add_work_hours(start, hours) == _walk(cal, start, hours), (cal.name, start, hours)
