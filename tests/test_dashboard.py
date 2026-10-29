"""p6_export_dashboard on the synthetic database: refusals, overwrite, the Full-only policy, branding given
as arguments, and the workbook of a project enriched in the test (units, a level 2 WBS, a baseline one
week earlier, float paths): sheets, fixed charts, protection, folded Schedule Overview, chart values."""

import os
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

from conftest import fingerprint
from test_assignment_units import assignments

openpyxl = pytest.importorskip("openpyxl")

DD = datetime(2026, 3, 2, 8)  # data date of the synthetic project
WEEK = timedelta(days=7)  # the baseline is one week earlier: 5 working days on the Monday to Friday calendar


def enrich(p6) -> None:
    """Units on A1010 / A1020 (test_assignment_units), A1010 moved to a level 2 WBS DES under ENG, a baseline
    DEMO-A-BL (same activities and assignments one week earlier), float paths 1 (critical chain) and 2."""
    assignments(p6)
    con = sqlite3.connect(p6.db)
    con.execute("UPDATE TASKRSRC SET target_start_date = (SELECT target_start_date FROM TASK t WHERE t.task_id = "
                "TASKRSRC.task_id), target_end_date = (SELECT target_end_date FROM TASK t WHERE t.task_id = "
                "TASKRSRC.task_id)")
    con.execute("INSERT INTO PROJWBS (wbs_id, proj_id, obs_id, seq_num, proj_node_flag, wbs_short_name, wbs_name, "
                "parent_wbs_id, status_code) VALUES (13, 2, 1, 1, 'N', 'DES', 'Design', 11, 'WS_Open')")
    con.execute("UPDATE TASK SET wbs_id = 13 WHERE task_id = 102")
    con.execute("INSERT INTO PROJECT (proj_id, project_flag, proj_short_name, orig_proj_id, clndr_id, name_sep_char, "
                "last_recalc_date) VALUES (3, 'Y', 'DEMO-A-BL', 2, 1, '.', '2026-02-23 08:00:00')")
    con.execute("INSERT INTO PROJWBS (wbs_id, proj_id, obs_id, seq_num, proj_node_flag, wbs_short_name, wbs_name, "
                "parent_wbs_id, status_code) VALUES (20, 3, 1, 0, 'Y', 'DEMO-A-BL', 'Baseline A', 1, 'WS_Open')")
    con.execute("UPDATE PROJECT SET sum_base_proj_id = 3 WHERE proj_id = 2")
    shift = lambda col: f"datetime({col}, '-7 days')"  # noqa: E731
    con.execute("INSERT INTO TASK (task_id, proj_id, wbs_id, clndr_id, task_code, task_name, task_type, status_code, "
                "target_drtn_hr_cnt, remain_drtn_hr_cnt, complete_pct_type, target_start_date, target_end_date, "
                "early_start_date, early_end_date) SELECT task_id + 1000, 3, 20, clndr_id, task_code, task_name, "
                f"task_type, 'TK_NotStart', target_drtn_hr_cnt, target_drtn_hr_cnt, complete_pct_type, "
                f"{shift('target_start_date')}, {shift('target_end_date')}, {shift('target_start_date')}, "
                f"{shift('target_end_date')} FROM TASK WHERE proj_id = 2")
    con.execute("INSERT INTO TASKRSRC (taskrsrc_id, task_id, proj_id, rsrc_id, rsrc_type, target_qty, remain_qty, "
                "act_reg_qty, act_ot_qty, target_start_date, target_end_date) SELECT taskrsrc_id + 1000, "
                f"task_id + 1000, 3, rsrc_id, rsrc_type, target_qty, target_qty, 0, 0, {shift('target_start_date')}, "
                f"{shift('target_end_date')} FROM TASKRSRC WHERE proj_id = 2")
    for order, tid in enumerate((102, 103, 104, 105, 106), 1):
        con.execute("UPDATE TASK SET float_path = 1, float_path_order = ? WHERE task_id = ?", (order, tid))
    con.execute("UPDATE TASK SET float_path = 2, float_path_order = 1 WHERE task_id = 107")
    con.commit()
    con.close()
    p6.s._cols_cache.clear()
    p6.s._schema_cache.clear()
    p6.set(trusted_schemas=[fingerprint(p6.db)])


def chart_data(wb, chart) -> tuple[list, dict]:
    """Categories and {series title: values} of a chart, read from the hidden cells it names."""
    def cells(ref: str) -> list:
        sheet, rng = ref.rsplit("!", 1)
        got = wb[sheet.strip("'")][rng.replace("$", "")]
        return [c.value for row in got for c in row] if ":" in rng else [got.value]
    cat = chart.series[0].cat
    return cells((cat.strRef or cat.numRef).f), {cells(s.tx.strRef.f)[0]: cells(s.val.numRef.f) for s in chart.series}


def planned_share(start: datetime, end: datetime) -> float:
    """Part of an assignment planned by the data date, linear in calendar time (as the server)."""
    return min(1.0, max(0.0, (DD - start) / (end - start)))


def test_policy_lists_it_in_the_full_profile_only(p6):
    from p6mcp import policy
    assert policy.TOOL_KIND["p6_export_dashboard"] == policy.FILE
    assert policy.listed("p6_export_dashboard", "full", False)
    assert not policy.listed("p6_export_dashboard", "basic", True)
    p6.set(tool_profile="basic")
    with pytest.raises(ValueError, match="not available in the Basic tool profile"):
        p6.call("p6_export_dashboard", project="DEMO-A", path=os.path.join(os.path.dirname(p6.db), "d.xlsx"))
    assert p6.s.p6_diagnose()["openpyxl"] == openpyxl.__version__


@pytest.mark.parametrize("kw, why", [
    ({"path": "d.xer"}, "must end with .xlsx"),
    ({"colors": "red"}, "not a hex color"),
    ({"colors": "111111,222222,333333"}, "at most two"),
    ({"company": "x" * 121}, "120 characters"),
    ({"prepared_by": "x" * 101}, "100 characters"),
    ({"near_critical_days": -1}, "near_critical_days"),
    ({"milestones": ["NO-SUCH"]}, "not found"),
])
def test_refusals_write_nothing(p6, tmp_path, kw, why):
    out = tmp_path / "out"  # tmp_path also holds the test's database
    out.mkdir()
    kw = {"path": str(out / "d.xlsx"), **kw}
    if kw["path"] == "d.xer":
        kw["path"] = str(out / "d.xer")
    with pytest.raises(ValueError, match=why):
        p6.call("p6_export_dashboard", project="DEMO-A", **kw)
    assert not os.listdir(out)


def test_without_openpyxl_the_error_says_how_to_get_it(p6, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "openpyxl", None)
    with pytest.raises(ValueError, match="openpyxl"):
        p6.call("p6_export_dashboard", project="DEMO-A", path=str(tmp_path / "d.xlsx"))
    assert p6.s.p6_diagnose()["openpyxl"] is None


def test_plain_project_exports_with_explanations(p6, tmp_path):
    """No units, no baseline: no chart, each panel says why; an existing file needs overwrite=True."""
    path = str(tmp_path / "plain.xlsx")
    before = p6.hash()
    res = p6.call("p6_export_dashboard", project="DEMO-A", path=path)
    assert p6.hash() == before and res["baseline"] is None and res["sheets"]["Schedule Overview"] == 7
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["Dashboard", "Schedule Overview", "Critical Path", "Reports"]
    d = wb["Dashboard"]
    assert len(d._charts) == 0
    assert str(d["O13"].value).startswith("No baseline") and "No level 1 WBS" in str(d["W13"].value)
    with pytest.raises(ValueError, match="already exists"):
        p6.call("p6_export_dashboard", project="DEMO-A", path=path)
    assert p6.call("p6_export_dashboard", project="DEMO-A", path=path, overwrite=True)["path"] == path


def test_enriched_project_workbook(p6, tmp_path):
    enrich(p6)
    path = str(tmp_path / "demo.xlsx")
    res = p6.call("p6_export_dashboard", project="DEMO-A", path=path, company="Acme Builders",
                  colors="#2b4c7e, C0392B", prepared_by="A. Planner", revision="R1", near_critical_days=3)
    assert res["baseline"] == "DEMO-A-BL" and not res["warnings"], res
    wb = openpyxl.load_workbook(path)
    d, so, cp = wb["Dashboard"], wb["Schedule Overview"], wb["Critical Path"]
    # fixed charts on a protected page; the branding of this export only
    assert len(d._charts) == 4 and all(type(c.anchor).__name__ == "TwoCellAnchor" and c.anchor.editAs == "absolute"
                                       for c in d._charts)
    assert d.protection.sheet and not d.protection.password
    assert d["A1"].value == "Acme Builders" and so["A4"].fill.fgColor.rgb.endswith("2B4C7E")
    assert "Prepared by A. Planner" in d["C3"].value and "Data date" in str(d["AC1"].value)
    # the result gives the 7 card values as written (row 6 of each card) and the DCMA checks that fail
    def shown(v):
        return v.strftime("%Y-%m-%d") if isinstance(v, datetime) else round(v, 2) if isinstance(v, float) else v
    assert [c["card"] for c in res["cards"]] == ["Overall Progress", "Critical Activities", "Delayed Activities",
                                                "Schedule Variance", "Forecast Completion", "Remaining Duration",
                                                "Milestones Late"]
    assert [shown(d.cell(6, 3 + 4 * k).value) for k in range(7)] == ["n/a" if c["value"] is None else c["value"]
                                                                    for c in res["cards"]]
    assert all(c["detail"] == d.cell(8, 3 + 4 * k).value for k, c in enumerate(res["cards"]))
    hc = p6.call("p6_health_check", project="DEMO-A", save_snapshot=False)
    assert res["dcma_failed"] == [k for k, c in hc["checks"].items()
                                  if c.get("pass") is False and k not in p6.s.HC_UNSCORED]  # the DCMA 14 only
    with __import__("zipfile").ZipFile(path) as z:
        charts = "".join(z.read(n).decode() for n in z.namelist() if n.startswith("xl/charts/"))
    assert 'srgbClr val="C0392B"' in charts
    # Schedule Overview: bands DEMO-A, ENG, DES, CON shown, every activity folded; Critical Path: 2 paths
    shown = [so.cell(r, 2).value for r in range(5, so.max_row + 1) if not so.row_dimensions[r].hidden]
    assert shown == ["Demo project A", "Engineering", "Design", "Construction"]
    assert [cp.cell(5 + i, 3).value for i in range(6)] == ["A1010", "A1020", "A1030", "A1040", "A1050", "A1060"]
    assert cp.cell(9, 1).border.bottom.style == "thick" and cp.cell(10, 1).value == "Path 2"
    # finish variance: one week late = 5 working days, At risk under 10, Delayed over near_critical_days=3
    cats, ser = chart_data(wb, d._charts[1])
    tones = [p.spPr.solidFill.srgbClr for p in d._charts[1].series[0].dPt]
    assert cats[::-1] == ["ENG. Engineering", "CON. Construction"]
    assert next(iter(ser.values())) == [5.0, 5.0] and tones == [p6.s.DASH_STATUS["Delayed"]] * 2
    # WBS progress (level 1) and level 2 progress: units % and the baseline planned % at the data date
    a1010 = 120 * planned_share(datetime(2026, 2, 23, 8), datetime(2026, 3, 6, 17))
    a1020 = 120 * planned_share(datetime(2026, 3, 9, 8), datetime(2026, 3, 27, 17))
    cats, ser = chart_data(wb, d._charts[2])
    assert cats == ["ENG. Engineering"] and ser["Actual"] == [25.0]
    assert ser["Planned (baseline)"] == [pytest.approx(round(100 * (a1010 + a1020) / 240, 2))]
    cats, ser = chart_data(wb, d._charts[3])
    assert cats == ["ENG.DES Design"] and ser["Actual"] == [50.0]
    assert ser["Planned (baseline)"] == [pytest.approx(round(100 * a1010 / 120, 2))]
