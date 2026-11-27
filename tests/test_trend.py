"""p6_trend and the Trend sheet of p6_export_dashboard on the public demo: DEMO-SS225_BL (baseline, data date
2 Feb 2026), DEMO-SS225_PREV (previous update, 19 Oct 2026) and DEMO-SS225 (update, 26 Oct 2026). Every row is
checked against the tools it says it follows (p6_project_summary, p6_health_check, p6_milestones,
p6_compare_projects, p6_update_integrity), finish variances against the demo's own calendar, float statistics
against test SQL; then paging, the EPS mode, refusals, nothing written, and the dashboard sheet."""

import os
import shutil
import sqlite3
import statistics
from datetime import datetime

import pytest

from conftest import fingerprint

import make_demo  # noqa: E402  (tests/fixtures is on sys.path, see conftest)

BL, PREV, CUR = make_demo.PROJ_BL, make_demo.PROJ_PREV, make_demo.PROJ
SERIES = [BL, PREV, CUR]


@pytest.fixture(scope="module")
def demo_build(server, tmp_path_factory):
    work = tmp_path_factory.mktemp("demo_trend")
    db = str(work / "demo.db")
    info = make_demo.build_db(db, str(work))
    return db, info


@pytest.fixture
def demo(p6, demo_build):
    shutil.copyfile(demo_build[0], p6.db)
    p6.s._cols_cache.clear()
    p6.s._schema_cache.clear()
    p6.set(trusted_schemas=[fingerprint(p6.db)])
    return p6


def dt(v):
    return datetime.strptime(v[:16], "%Y-%m-%d %H:%M") if v else None


def wd(a, b):
    return round(make_demo.wh(dt(a), dt(b)) / make_demo.HPD, 2)


def test_rows_follow_the_tools(demo, demo_build):
    before = demo.hash()
    health = os.path.join(demo.logs, "health")
    snaps = set(os.listdir(health)) if os.path.isdir(health) else set()
    r = demo.call("p6_trend", projects=[CUR, BL, PREV])  # any order: sorted by data date
    assert demo.hash() == before  # read only
    assert (set(os.listdir(health)) if os.path.isdir(health) else set()) == snaps  # no health snapshot saved
    assert (r["total"], r["offset"], r["returned"], r["next_offset"]) == (3, 0, 3, None)
    assert r["series"]["projects"] == SERIES and r["series"]["source"] == "projects="
    assert [x["update"] for x in r["rows"]] == [1, 2, 3]
    first = r["rows"][0]
    for i, row in enumerate(r["rows"]):
        p = row["project"]
        sm = demo.call("p6_project_summary", project=p)
        hc = demo.call("p6_health_check", project=p, save_snapshot=False)
        dates = sm["dates"]
        assert (row["data_date"], row["last_schedule_date"]) == (dates["data_date"], dates["last_schedule_run"])
        assert (row["finish"], row["must_finish"]) == (sm["dates"]["scheduled_finish"], sm["dates"]["must_finish_by"])
        assert row["finish_vs_must_finish_wd"] == sm["dates"]["finish_vs_must_finish_by_wd"]
        assert row["units_pct"] == sm["percent_complete"]["units"]
        b = sm["baseline"]
        assert (row["baseline"], row["baseline_finish"], row["baseline_finish_variance_wd"], row["planned_units_pct"],
                row["spi_units"]) == (b["used"], b["finish"], b["finish_variance_wd"],
                                      b["planned_units_pct_at_data_date"], b["spi_units"])
        cr = sm["critical"]
        assert (row["critical"], row["negative_float"]) == (cr["critical"], cr["negative_float"])
        assert row["dcma"] == {"points": hc["weighted_score"]["points"], "grade": hc["weighted_score"]["grade"],
                               "failed": hc["summary"]["failed"], "bei": hc["checks"]["bei"].get("value"),
                               "bei_pass": hc["checks"]["bei"]["pass"]}
        # finish variances on the demo calendar (working days) and in calendar days
        assert row["finish_variance_first_wd"] == wd(first["finish"], row["finish"])
        cd = (dt(row["finish"]) - dt(first["finish"])).total_seconds() / 86400
        assert row["finish_variance_first_cd"] == round(cd, 2)
        # float statistics and near critical counts from test SQL (open = not completed, LOE and WBS summary out)
        tf = demo.sql("SELECT t.total_float_hr_cnt / COALESCE(NULLIF(c.day_hr_cnt, 0), 8), t.total_float_hr_cnt, "
                      "COALESCE(NULLIF(c.day_hr_cnt, 0), 8) FROM TASK t JOIN PROJECT p ON p.proj_id = t.proj_id "
                      "LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id WHERE p.proj_short_name = ? AND t.status_code "
                      "<> 'TK_Complete' AND t.task_type NOT IN ('TT_LOE', 'TT_WBS') AND t.total_float_hr_cnt IS NOT "
                      "NULL", p)
        thr = demo.sql("SELECT critical_drtn_hr_cnt FROM PROJECT WHERE proj_short_name = ?", p)[0][0] or 0
        days = sorted(x[0] for x in tf)
        assert row["total_float"]["with_float"] == len(days)
        assert (row["total_float"]["min_d"], row["total_float"]["max_d"], row["total_float"]["median_d"]) == (
            round(days[0], 2), round(days[-1], 2), round(statistics.median(days), 2))
        assert row["near_critical"] == sum(1 for _, h, hpd in tf if thr < h <= 10 * hpd)
        # milestones: the dates p6_milestones gives
        ms = {m["task_code"]: m for m in demo.call("p6_milestones", project=p, limit=200)["rows"]}
        assert [x["task_code"] for x in row["milestones"]] == [m["task_code"] for m in r["milestones"]]
        for x in row["milestones"]:
            assert (x["date"], x["date_kind"]) == (ms[x["task_code"]]["date"], ms[x["task_code"]]["date_kind"])
            if i:
                prev = next(y for y in r["rows"][i - 1]["milestones"] if y["task_code"] == x["task_code"])
                assert x["slip_wd"] == wd(prev["date"], x["date"])
            else:
                assert x["slip_wd"] is None and x["variance_first_wd"] == 0.0
    # the update against the baseline: the demo's own figures
    info = demo_build[1]
    last = r["rows"][-1]
    assert last["finish"][:16] == info["forecast_finish"] and first["finish"][:16] == info["baseline_finish"]
    assert last["finish_variance_first_wd"] == info["slip_wd"] == last["baseline_finish_variance_wd"]
    assert last["finish_variance_previous_wd"] == wd(r["rows"][1]["finish"], last["finish"])
    assert first["finish_variance_previous_wd"] is None and first["changes"] is None
    # changes since the previous update, as p6_compare_projects and p6_update_integrity count them
    for row, other in ((r["rows"][1], BL), (r["rows"][2], PREV)):
        cp = demo.call("p6_compare_projects", project=row["project"], other=other)["summary"]
        ig = demo.call("p6_update_integrity", project=row["project"], other=other)["summary"]
        assert row["changes"] == {"since": other, "activities_added": cp["activities"]["added"],
                                  "activities_removed": cp["activities"]["removed"],
                                  "links_added": cp["relationships"]["added"],
                                  "links_removed": cp["relationships"]["removed"], "durations_cut": ig["duration_cut"]}
    assert r["rows"][2]["changes"]["durations_cut"] == 2  # the two cuts make_demo.previous() undoes
    assert all(m["updates"] == 3 for m in r["milestones"]) and r["notes"] == []


def test_paging_and_options(demo):
    full = demo.call("p6_trend", projects=SERIES)
    page = demo.call("p6_trend", projects=SERIES, offset=1, limit=1)
    assert (page["total"], page["offset"], page["returned"], page["next_offset"]) == (3, 1, 1, 2)
    assert page["rows"] == full["rows"][1:2]  # the previous update is read for its variances and changes
    last = demo.call("p6_trend", projects=SERIES, offset=2, limit=50)
    assert last["rows"] == full["rows"][2:] and last["next_offset"] is None
    fast = demo.call("p6_trend", projects=SERIES, dcma=False, milestones="none")
    assert all(x["dcma"] is None and x["milestones"] == [] for x in fast["rows"]) and fast["engine_version"] is None
    assert [{k: v for k, v in x.items() if k not in ("dcma", "milestones")} for x in fast["rows"]] == \
        [{k: v for k, v in x.items() if k not in ("dcma", "milestones")} for x in full["rows"]]
    # codes=: any activity type (a task takes its finish), in the order given; IDs found nowhere are noted
    task = demo.sql("SELECT task_code, early_end_date FROM TASK t JOIN PROJECT p ON p.proj_id = t.proj_id "
                    "WHERE p.proj_short_name = ? AND task_type = 'TT_Task' AND status_code = 'TK_NotStart' "
                    "ORDER BY task_code LIMIT 1", CUR)[0]
    r = demo.call("p6_trend", projects=SERIES, codes=f"{task[0]}, MS-1100, NOPE-1", dcma=False)
    assert [m["task_code"] for m in r["milestones"]] == [task[0], "MS-1100"]
    assert r["rows"][-1]["milestones"][0]["date"] == task[1]
    assert r["notes"] == ["Activity IDs in none of the updates: ['NOPE-1']."]
    # baseline= for every update: the earliest one is its own baseline
    b = demo.call("p6_trend", projects=SERIES, baseline=BL, dcma=False)
    assert all(x["baseline"] == BL for x in b["rows"]) and b["rows"][0]["baseline_finish_variance_wd"] == 0.0


def test_eps_mode(demo):
    # PREV becomes an ordinary project of the demo EPS (imported as a baseline of the update): with DEMO-A
    # and DEMO-SS225 the EPS holds three projects; the baseline DEMO-SS225_BL stays out
    con = sqlite3.connect(demo.db)
    con.execute("UPDATE PROJECT SET orig_proj_id = NULL WHERE proj_short_name = ?", (PREV,))
    con.commit()
    con.close()
    r = demo.call("p6_trend", eps=make_demo.EPS, dcma=False)
    assert r["series"]["source"] == "eps=" and r["series"]["projects"] == ["DEMO-A", PREV, CUR]
    assert [x["data_date"] for x in r["rows"]] == sorted(x["data_date"] for x in r["rows"])
    assert r["milestones"] == []  # DEMO-A has no milestone of the demo


def test_refusals(demo):
    def bad(match, **kw):
        with pytest.raises(ValueError, match=match):
            demo.s.p6_trend(**kw)
    bad("at least two updates", projects=[CUR])
    bad("given twice", projects=[CUR, CUR])
    bad("Project not found", projects=[CUR, "NOPE"])
    bad("one of them", projects=SERIES, eps=make_demo.EPS)
    bad("one of them")
    bad("EPS node", projects=[CUR, make_demo.EPS])
    bad("milestones must be one of", projects=SERIES, milestones="all")
    bad("near_critical_days", projects=SERIES, near_critical_days=-1)
    bad("a list of Project IDs", projects=CUR)
    with pytest.raises(ValueError, match="not found"):
        demo.s.p6_trend(eps="NOPE")
    con = sqlite3.connect(demo.db)
    con.execute("UPDATE PROJECT SET last_recalc_date = NULL WHERE proj_short_name = ?", (PREV,))
    con.commit()
    con.close()
    bad("no data date", projects=SERIES)


def test_dashboard_trend_sheet(demo, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    plain = demo.call("p6_export_dashboard", project=CUR, path=str(tmp_path / "plain.xlsx"))
    assert "trend" not in plain and "Trend" not in plain["sheets"]
    wb0 = openpyxl.load_workbook(plain["path"])
    assert wb0.sheetnames == ["Dashboard", "Schedule Overview", "Critical Path", "Reports"]
    res = demo.call("p6_export_dashboard", project=CUR, path=str(tmp_path / "trend.xlsx"), trend=[PREV, BL])
    assert res["trend"] == {"updates": 3, "projects": SERIES} and res["sheets"]["Trend"] == 3
    assert res["cards"] == plain["cards"] and res["dcma_failed"] == plain["dcma_failed"]
    wb = openpyxl.load_workbook(res["path"])
    assert wb.sheetnames == ["Dashboard", "Schedule Overview", "Critical Path", "Trend", "Reports"]
    side = wb["Dashboard"].cell(18, 1)
    assert "Trend" in str(side.value) and side.hyperlink.location == "'Trend'!A1"
    assert wb["Dashboard"].cell(20, 1).hyperlink.location == "'Reports'!A1"
    ws = wb["Trend"]
    assert ws["A3"].hyperlink.location == "'Dashboard'!A1"
    t = demo.call("p6_trend", projects=SERIES, codes=None)
    for i, row in enumerate(t["rows"]):
        cells = [ws.cell(6 + i, c).value for c in range(1, 28)]
        assert cells[0] == row["update"] and cells[1] == row["project"]
        assert cells[2] == datetime.strptime(row["data_date"], "%Y-%m-%d %H:%M:%S")
        assert cells[4] == datetime.strptime(row["finish"], "%Y-%m-%d %H:%M:%S")
        assert cells[7:13] == [row["finish_variance_first_wd"], row["finish_variance_previous_wd"],
                               row["baseline_finish_variance_wd"], row["units_pct"], row["planned_units_pct"],
                               row["spi_units"]]
        assert cells[18:20] == [row["dcma"]["points"], row["dcma"]["grade"]]
        ch = row["changes"] or {}
        assert cells[22:27] == [ch.get(k) for k in ("activities_added", "activities_removed", "links_added",
                                                    "links_removed", "durations_cut")]
    charts = ws._charts
    assert len(charts) == 2
    for c in charts:
        assert type(c.anchor).__name__ == "TwoCellAnchor" and c.anchor.editAs == "absolute"
        assert c.anchor._from.row >= 5 + len(t["rows"])  # below the table
    reports = wb["Reports"]
    texts = [str(reports.cell(r, 2).value or "") for r in range(1, reports.max_row + 1)]
    assert any(x.startswith("Trend sheet:") for x in texts)
    assert any(x.startswith("3 updates, data dates 2026-02-02 to 2026-10-26") for x in texts)
    # the exported project must be the latest update of the series
    with pytest.raises(ValueError, match="must be the latest update"):
        demo.s.p6_export_dashboard(PREV, str(tmp_path / "x.xlsx"), trend=[CUR])
    assert not os.path.exists(tmp_path / "x.xlsx")
