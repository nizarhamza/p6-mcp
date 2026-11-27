"""p6_calendar_check on the synthetic database with calendars added by the test (one per finding kind).

Fixture: DEMO-A (data date 2026-03-02, scheduled finish 2026-05-25: span 2026), project calendar 1 "Standard 5 day"
(Monday to Friday 08:00-12:00 and 13:00-17:00, holiday Monday 2026-04-06). Oracles: the calendars written below.
"""

import sqlite3
from datetime import date

import pytest

from conftest import fingerprint

EPOCH = date(1899, 12, 30)
EIGHT, TEN = [("08:00", "12:00"), ("13:00", "17:00")], [("07:00", "17:00")]


def cal_text(week: dict, holidays=()) -> str:
    """clndr_data: week = {P6 day (1 = Sunday): shifts} (None: no DaysOfWeek node); holidays = dates off."""
    days = "" if week is None else "(0||DaysOfWeek()(" + "".join(
        f"(0||{n}()(" + "".join(f"(0||{i}(s|{s}|f|{f})())" for i, (s, f) in enumerate(week.get(n, ()))) + "))"
        for n in range(1, 8)) + "))"
    exc = "".join(f"(0||{i}(d|{(d - EPOCH).days})())" for i, d in enumerate(holidays))
    return f"(0||CalendarData()({days}(0||Exceptions()({exc}))))"


def enrich(p6) -> None:
    con = sqlite3.connect(p6.db)
    cals = [  # id, name, type, base, day_hr_cnt, week_hr_cnt, clndr_data
        (2, "Seven day", "CA_Project", None, 8, 40, cal_text({n: [("08:00", "16:00")] for n in range(1, 8)},
                                                             [date(2026, 5, 1)])),
        (3, "Ten hour base", "CA_Base", None, 10, 40, cal_text({n: TEN for n in range(2, 6)})),
        (4, "Crew", "CA_Rsrc", 3, 8, 40, cal_text(None)),
        (5, "Orphan", "CA_Project", 99, 8, 40, cal_text({n: EIGHT for n in range(2, 7)})),
        (6, "Broken", "CA_Rsrc", None, 8, 40, "(0||CalendarData()((0||DaysOfWeek()((0||9()())))))"),
        (7, "Never works", "CA_Project", None, 8, 0, cal_text({})),
    ]
    for cid, name, typ, base, hpd, whc, data in cals:
        con.execute("INSERT INTO CALENDAR (clndr_id, clndr_name, clndr_type, base_clndr_id, day_hr_cnt, week_hr_cnt, "
                    "clndr_data, proj_id) VALUES (?, ?, ?, ?, ?, ?, ?, 2)", (cid, name, typ, base, hpd, whc,
                                                                            data.encode("utf-8")))
    for tid, cid in ((107, 2), (104, 5), (106, 7), (101, 555)):  # A1060, A1030, A1050, A1000 (calendar 555 missing)
        con.execute("UPDATE TASK SET clndr_id = ? WHERE task_id = ?", (cid, tid))
    con.execute("UPDATE TASK SET task_type = 'TT_Rsrc' WHERE task_id = 103")  # A1020 resource dependent
    for rid, code, cid in ((301, "CREW", 4), (302, "BROKEN", 6), (303, "NOCAL", None)):
        con.execute("INSERT INTO RSRC (rsrc_id, rsrc_short_name, rsrc_name, rsrc_type, clndr_id) VALUES (?, ?, ?, "
                    "'RT_Labor', ?)", (rid, code, code.title(), cid))
    for aid, tid, rid in ((401, 104, 301), (402, 103, 302), (403, 103, 303)):
        con.execute("INSERT INTO TASKRSRC (taskrsrc_id, task_id, proj_id, rsrc_id, rsrc_type, target_qty, remain_qty) "
                    "VALUES (?, ?, 2, ?, 'RT_Labor', 8, 8)", (aid, tid, rid))
    con.commit()
    con.close()
    p6.s._cols_cache.clear()
    p6.set(trusted_schemas=[fingerprint(p6.db)])


def by_kind(rows) -> dict:
    out: dict = {}
    for r in rows:
        out.setdefault(r["kind"], []).append(r)
    return out


def test_clean_fixture_has_no_finding(p6):
    res = p6.call("p6_calendar_check", project="DEMO-A")
    assert res["total"] == 0 and res["span"]["from_year"] == res["span"]["to_year"] == 2026
    assert res["span"]["basis"] == "data date 2026-03-02 to scheduled finish 2026-05-25"
    (cal,) = res["calendars"]
    assert (cal["clndr_id"], cal["roles"], cal["activities"]) == (1, ["project", "activity"], 7)
    assert cal["holidays_in_span"] == 1


def test_every_finding_kind(p6):
    enrich(p6)
    res = p6.call("p6_calendar_check", project="DEMO-A", limit=200)
    k = by_kind(res["rows"])
    assert res["counts"] == {"calendar_missing": 1, "unreadable": 1, "base_missing": 1, "no_working_time": 1,
                             "rsrc_dep_no_calendar": 1, "project_holiday_worked": 3, "holiday_not_in_project": 1,
                             "no_exceptions_in_year": 2, "day_hours_stale": 1, "week_hours_stale": 1,
                             "work_week_differs": 2}
    assert res["total"] == 15 and res["by_severity"] == {"error": 5, "warning": 7, "info": 3}
    assert [r["severity"] for r in res["rows"]] == sorted((r["severity"] for r in res["rows"]),
                                                         key=["error", "warning", "info"].index)
    assert k["calendar_missing"][0]["clndr_id"] == 555 and k["calendar_missing"][0]["activities"] == 1
    assert k["unreadable"][0]["clndr_id"] == 6 and k["unreadable"][0]["roles"] == ["resource"]
    assert k["base_missing"][0]["clndr_id"] == 5 and "99" in k["base_missing"][0]["detail"]
    nw = k["no_working_time"][0]
    assert (nw["clndr_id"], nw["activities"]) == (7, 1) and nw["detail"].endswith("cannot be scheduled: 1.")
    rd = k["rsrc_dep_no_calendar"][0]
    assert (rd["task_code"], rd["clndr_id"]) == ("A1020", 1)
    assert "BROKEN (calendar unreadable)" in rd["detail"] and "NOCAL (no calendar)" in rd["detail"]
    # the project holiday 2026-04-06 (Monday) is worked by the seven day, crew (week of its base) and orphan calendars
    assert {(r["clndr_id"], r["date"]) for r in k["project_holiday_worked"]} == {
        (2, "2026-04-06"), (4, "2026-04-06"), (5, "2026-04-06")}
    assert "works 10 h" in next(r["detail"] for r in k["project_holiday_worked"] if r["clndr_id"] == 4)
    (hol,) = k["holiday_not_in_project"]
    assert (hol["clndr_id"], hol["date"], hol["activities"]) == (2, "2026-05-01", 1) and "works 8 h" in hol["detail"]
    assert {(r["clndr_id"], r["year"]) for r in k["no_exceptions_in_year"]} == {(4, 2026), (5, 2026)}
    (day,) = k["day_hours_stale"]
    assert day["clndr_id"] == 4 and "Hours per day is 8" in day["detail"] and "10 h" in day["detail"]
    (week,) = k["week_hours_stale"]
    assert week["clndr_id"] == 2 and "40" in week["detail"] and "56 h" in week["detail"]
    assert {r["clndr_id"] for r in k["work_week_differs"]} == {2, 4}
    assert "Sat 08:00-16:00 (project: off)" in next(r["detail"] for r in k["work_week_differs"] if r["clndr_id"] == 2)
    assert all(set(r) == set(res["rows"][0]) for r in res["rows"])  # one row shape
    roles = {c["clndr_id"]: c["roles"] for c in res["calendars"]}
    assert roles[1] == ["project", "activity"] and roles[3] == ["base"] and roles[4] == ["resource"]
    assert next(c for c in res["calendars"] if c["clndr_id"] == 2)["findings"] == 4


def test_filter_paging_and_years(p6):
    enrich(p6)
    every = p6.call("p6_calendar_check", project="DEMO-A", limit=200)
    page = p6.call("p6_calendar_check", project="DEMO-A", limit=4, offset=4)
    assert page["rows"] == every["rows"][4:8] and page["next_offset"] == 8
    only = p6.call("p6_calendar_check", project="DEMO-A", kinds=["project_holiday_worked"])
    assert only["total"] == 3 and only["counts"] == every["counts"]  # counts cover every kind
    two = p6.call("p6_calendar_check", project="DEMO-A", kinds=["no_exceptions_in_year"], from_year=2026,
                  to_year=2027)
    assert two["span"]["basis"] == "from_year / to_year"
    assert {(r["clndr_id"], r["year"]) for r in two["rows"]} == {(1, 2027), (2, 2027), (4, 2026), (4, 2027),
                                                               (5, 2026), (5, 2027)}
    for kw, why in (({"kinds": ["nope"]}, "kinds"), ({"from_year": 2027, "to_year": 2026}, "before"),
                    ({"from_year": 2020, "to_year": 2035}, "at most 10 years")):
        with pytest.raises(ValueError, match=why):
            p6.call("p6_calendar_check", project="DEMO-A", **kw)
