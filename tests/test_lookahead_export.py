"""p6_export_lookahead on the synthetic database: refusals, the Full-only policy, and the workbook opened again
(cells, WBS bands, bars, non-work days, data date line, print settings, text never a formula).

Fixture: DEMO-A, data date Monday 2026-03-02 08:00, calendar Monday to Friday. Three weeks from the data date list
A1010 (in progress, actual start 2026-02-02, finish 2026-03-13 17:00, critical), A1020 (2026-03-16 to 2026-04-03,
critical) under ENG and A1060 (2026-03-02 to 2026-03-06, 160 h of float) under CON. Oracles: the fixture's
constants and p6_lookahead.
"""

import os
import sqlite3
from datetime import datetime

import pytest

openpyxl = pytest.importorskip("openpyxl")

G0 = 9  # first grid column (I): eight fixed columns before it


def fill(cell) -> str:
    return (cell.fill.fgColor.rgb or "")[-6:] if cell.fill and cell.fill.fill_type == "solid" else ""


def test_policy_and_refusals(p6, tmp_path):
    from p6mcp import policy
    assert policy.TOOL_KIND["p6_export_lookahead"] == policy.FILE
    assert policy.remote_kind("p6_export_lookahead") == "never"
    assert not policy.listed("p6_export_lookahead", "basic", True)
    out = tmp_path / "out"
    out.mkdir()
    for kw, why in (({"path": str(out / "l.xer")}, "must end with .xlsx"),
                    ({"weeks": 14, "grid": "day"}, "at most 92 days"),
                    ({"weeks": 0}, "1 to 52"), ({"grid": "month"}, "grid must be"),
                    ({"from_date": "2026-13-01"}, "from_date")):
        with pytest.raises(ValueError, match=why):
            p6.call("p6_export_lookahead", project="DEMO-A", **{"path": str(out / "l.xlsx"), **kw})
    assert not os.listdir(out)
    path = str(out / "l.xlsx")
    p6.call("p6_export_lookahead", project="DEMO-A", path=path)
    with pytest.raises(ValueError, match="already exists"):
        p6.call("p6_export_lookahead", project="DEMO-A", path=path)
    assert p6.call("p6_export_lookahead", project="DEMO-A", path=path, overwrite=True)["path"] == path
    p6.set(tool_profile="basic")
    with pytest.raises(ValueError, match="not available in the Basic tool profile"):
        p6.call("p6_export_lookahead", project="DEMO-A", path=path, overwrite=True)


def test_day_grid_workbook(p6, tmp_path):
    con = sqlite3.connect(p6.db)
    con.execute("UPDATE TASK SET task_name = '=1+1' WHERE task_code = 'A1060'")  # text, never a formula
    con.commit()
    con.close()
    path = str(tmp_path / "la.xlsx")
    before = p6.hash()
    res = p6.call("p6_export_lookahead", project="DEMO-A", path=path)
    assert p6.hash() == before and res["grid"] == "day" and not res["warnings"] and not res["truncated"]
    la = p6.call("p6_lookahead", project="DEMO-A", weeks=3)
    assert res["counts"] == la["counts"] and res["window"] == la["window"]
    assert res["rows"] == {"activities": 3, "wbs_bands": 2, "grid_columns": 22, "bars": 25, "data_date_column": "I"}
    ws = openpyxl.load_workbook(path)["Look-ahead"]
    assert ws["A1"].value == "Look-ahead of DEMO-A, Demo project A"
    assert "Data date 02-Mar-26 08:00" in ws["A2"].value
    assert "Window 02-Mar-26 08:00 to 23-Mar-26 08:00" in ws["A2"].value
    # WBS bands in P6 order, activities in look-ahead order inside each
    assert [ws.cell(r, 1).value for r in range(7, 12)] == ["DEMO-A.ENG  Engineering", "A1010", "A1020",
                                                          "DEMO-A.CON  Construction", "A1060"]
    a1010 = [c.value for c in ws[8][:8]]
    assert a1010 == ["A1010", "Detailed design", datetime(2026, 2, 2, 8), datetime(2026, 3, 13, 17), 10.0, 0.0,
                     "Yes", "In Progress"]
    assert ws["C8"].number_format == 'dd-mmm-yy" A"' and ws["D8"].number_format == "dd-mmm-yy"
    assert ws["B11"].value == "=1+1" and ws["B11"].data_type == "s"
    assert ws["G11"].value is None and ws["E11"].value == 5.0 and ws["F11"].value == 20.0
    # grid: days from 2026-03-02 (column I) to 2026-03-23 (AD); bars of each activity on its dates
    assert ws.cell(6, G0).value == "M\n2" and ws.cell(6, G0 + 21).value == "M\n23"

    def bar(row) -> list[int]:
        return [k for k in range(22) if fill(ws.cell(row, G0 + k)) in ("B2182B", "2B6CB0")]
    assert bar(8) == list(range(0, 12)) and fill(ws.cell(8, G0)) == "B2182B"  # 2 to 13 March, critical
    assert bar(9) == list(range(14, 22)) and fill(ws.cell(9, G0 + 14)) == "B2182B"  # from 16 March
    assert bar(11) == list(range(0, 5)) and fill(ws.cell(11, G0)) == "2B6CB0"  # 2 to 6 March, not critical
    assert fill(ws.cell(11, G0 + 5)) == "E5E7EB" and fill(ws.cell(11, G0 + 7)) == ""  # Saturday 7 shaded, Monday 9 not
    assert fill(ws.cell(6, G0)) == "E67E22"  # the data date column
    assert all(ws.cell(r, G0).border.left.style == "medium" for r in range(5, 12))
    # print settings
    assert ws.page_setup.orientation == "landscape" and ws.sheet_properties.pageSetUpPr.fitToPage
    assert (ws.page_setup.fitToWidth, ws.page_setup.fitToHeight) == (1, 0)
    assert ws.print_title_rows == "$5:$6" and ws.freeze_panes == "I7"


def test_week_grid_and_milestone(p6, tmp_path):
    path = str(tmp_path / "weeks.xlsx")
    res = p6.call("p6_export_lookahead", project="DEMO-A", path=path, weeks=13)
    assert res["grid"] == "week" and res["rows"]["grid_columns"] == 13
    ws = openpyxl.load_workbook(path)["Look-ahead"]
    row = next(r for r in range(7, ws.max_row + 1) if ws.cell(r, 1).value == "A1050")
    # the finish milestone (2026-05-25 17:00) is a diamond in week 13 (from 2026-05-25 08:00)
    assert ws.cell(row, G0 + 12).value == "◆" and all(ws.cell(row, G0 + k).value is None for k in range(12))
    assert ws.cell(6, G0 + 12).value == "25-May"
    a1030 = next(r for r in range(7, ws.max_row + 1) if ws.cell(r, 1).value == "A1030")
    # 7 April to 11 May: weeks 6 to 11
    assert [k for k in range(13) if fill(ws.cell(a1030, G0 + k))] == list(range(5, 11))
