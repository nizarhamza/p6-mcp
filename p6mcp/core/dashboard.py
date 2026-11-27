"""The Excel workbooks: the project dashboard (p6_export_dashboard) and the look-ahead (p6_export_lookahead).
openpyxl is imported on use."""

from __future__ import annotations

import functools
import itertools
import math
import re
import sys
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Mapping, Optional, Sequence

from p6mcp.core.constants import STATUS_NAMES
from p6mcp.registry import SERVER_VERSION
from p6mcp.core.db import _act_from, _alive, _cols, _days, _db_dt, _dict, _fmt_dt
from p6mcp.core.calendar import _Cal, _calendars
from p6mcp.core.wbs_codes import _act_agg_sql, _merge_agg, _name_sep, _rollup, _wbs_tree
from p6mcp.core.analysis import DCMA_CHECKS, HC_UNSCORED
from p6mcp.core.reporting import _bl_planned_by_wbs


# ----------------------------------------------------------------- dashboard export (Excel)
# p6_export_dashboard gathers the report tools' results (each from its own snapshot: the data date and
# the last F9 are compared at the start and the end) and writes a workbook built by _dash_book with
# openpyxl, imported on use so the server runs without it. The template is this code: sheets are sized
# to the data, values are written as values (the file is regenerated at every update) and charts are
# native Excel charts.

DASH_WBS_ROWS = 5000  # WBS bands of the Schedule Overview sheet
DASH_MILESTONES = 30  # milestones listed without milestones= (every milestone, in date order)
DASH_MILESTONES_MAX = 50  # Activity IDs milestones= accepts
DASH_PANEL_MILESTONES = 11  # milestones on the one-page dashboard
DCMA_LABELS = {"logic": "Logic (no predecessor or successor)", "leads": "Leads (negative lags)", "lags": "Lags",
               "long_lags": "Long lags", "fs_links": "Finish to start links", "hard_constraints": "Hard constraints",
               "high_float": "High float", "negative_float": "Negative float", "high_duration": "High duration",
               "invalid_dates": "Invalid dates", "resources": "Resources", "missed_tasks": "Missed tasks",
               "critical_path_test": "Critical path test", "cpli": "Critical path length index (CPLI)",
               "bei": "Baseline execution index (BEI)", "key_dates": "Key dates (information only)"}
# Colors (RGB): text, rules, headers; float classes (told apart by lightness too, always labeled); tints
# for cells; good status.
DASH_INK, DASH_MUTED, DASH_RULE, DASH_HEAD = "1F2937", "6B7280", "D1D5DB", "1F3A5F"
DASH_FLOAT = {"neg": "B2182B", "near": "E69F00", "ok": "0072B2"}  # negative, 0 to near_critical_days, above
DASH_TINT = {"neg": "F6D5D9", "near": "FCEBC7", "ok": "D6E7F3", "pass": "DCEFE2", "fail": "F6D5D9", "na": "EEF0F3"}
DASH_GOOD = "1B7F3B"
# Sheets in order, with the sidebar glyph of each (Segoe UI Symbol); the Dashboard sidebar links to the others.
# Trend only with trend= (p6_export_dashboard).
DASH_SHEETS = ("Dashboard", "Schedule Overview", "Critical Path", "Trend", "Reports")
DASH_NAV = {"Dashboard": "⌂", "Schedule Overview": "▤", "Critical Path": "◎", "Trend": "↗", "Reports": "▥"}
DASH_ACT_CAP = 20000  # activities of the Schedule Overview sheet
DASH_TABLE_ROWS = 12  # critical and delayed activities on the Dashboard sheet
DASH_PAGE = "F3F5F8"  # page background of the Dashboard sheet (cards and panels are white)
DASH_STATUS = {"Delayed": "B2182B", "At risk": "E69F00", "On track": "009E73", "Completed": "9CA3AF"}  # finish variance
DASH_SO_LEVEL = 2  # the Schedule Overview opens folded to the WBS bands of this level (activities hidden)
DASH_WBS_BARS = 20  # WBS per progress chart of the Dashboard (more: the ones with the most budget units)
DASH_PILL = {"Critical": ("F6D5D9", "B2182B"), "Delayed": ("FCEBC7", "8A5300"), "At risk": ("FDF3D7", "8A5300"),
             "On track": ("DCEFE2", "1B7F3B"), "Completed": ("EEF0F3", "4B5563"), "No baseline": ("EEF0F3", "4B5563")}


def _openpyxl():
    """The openpyxl module (a dependency, imported on use), or a ValueError saying how to get it back."""
    try:
        import openpyxl
    except ImportError:
        if getattr(sys, "frozen", False):
            raise ValueError("p6_export_dashboard needs the openpyxl package, missing from this MCP for Primavera P6 build: "
                             "reinstall MCP for Primavera P6") from None
        raise ValueError(f"p6_export_dashboard needs the openpyxl package: run '\"{sys.executable}\" -m pip install "
                         "openpyxl', then restart the server") from None
    return openpyxl


def _openpyxl_version() -> Optional[str]:
    try:
        return _openpyxl().__version__
    except ValueError:
        return None


def _dash_brand(company: Optional[str] = None, colors: Optional[str] = None) -> dict:
    """Dashboard branding from the export's own arguments (nothing is stored): company name (at most
    120 characters) and colors 'primary[,accent]' in hex, else the defaults. Raises before anything is
    written when a value cannot be used."""
    name = (str(company).strip() or None) if company is not None else None
    if name and len(name) > 120:
        raise ValueError("company: at most 120 characters")
    out: dict[str, Any] = {"company": name, "primary": DASH_HEAD, "accent": "0072B2"}
    cols = [c.strip().lstrip("#") for c in str(colors or "").split(",") if c.strip()]
    if len(cols) > 2:
        raise ValueError("colors: at most two hex colors, 'primary,accent' (e.g. '1F3A5F,0072B2')")
    for key, c in zip(("primary", "accent"), cols, strict=False):
        if not re.fullmatch(r"[0-9A-Fa-f]{6}", c):
            raise ValueError(f"colors: {c!r} is not a hex color such as 1F3A5F")
        out[key] = c.upper()
    return out


def _ink_on(color: str) -> str:
    """Text color for a fill: white or the ink color, whichever contrasts more (WCAG luminance)."""
    def lum(c: str) -> float:
        ch = [int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        ch = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
    fill = lum(color)
    return "FFFFFF" if 1.05 / (fill + 0.05) >= (fill + 0.05) / (lum(DASH_INK) + 0.05) else DASH_INK


def _dash_read(con, pid: int, bid: Optional[int], codes: Optional[Sequence[str]], baseline_dates: str) -> dict:
    """What the dashboard reads itself, in the caller's snapshot: the project and baseline dates; every
    activity (baseline dates of the baseline activity with the same Activity ID, slip in working days of
    its own calendar, % complete by its percent complete type, labor units, original, remaining and
    actual durations, P6 float path); every WBS node in P6 order with its units % (as p6_list_wbs), the
    baseline planned units % at the data date, the finish variance (latest baseline finish to latest
    finish of its activities) and the band values of its activities and of the WBS below it (dates,
    labor units, % complete; finish variance and durations in working days of the project calendar); the
    milestones (milestones= codes, else every milestone) with their baseline date and variance; the
    project figures in working days of the project calendar."""
    al = functools.partial(_alive, con)
    pc = _cols(con, "PROJECT")
    pf = ("proj_short_name", "last_recalc_date", "last_schedule_date", "scd_end_date", "plan_end_date", "clndr_id")

    def proj(p: int) -> dict:
        return _dict(con.execute("SELECT " + ", ".join(c if c in pc else f"NULL AS {c}" for c in pf)
                                 + " FROM PROJECT WHERE proj_id = ?", (p,)).fetchone())
    prj, base = proj(pid), proj(bid) if bid is not None else None
    cals, wd_notes = _calendars(con), set()
    pcal = cals.get(prj["clndr_id"]) if prj["clndr_id"] is not None else None

    def wd(cal: Optional[_Cal], a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
        """Working days from a to b (negative when b is earlier), 1 decimal; calendar days when the
        calendar is missing or unreadable (noted)."""
        if a is None or b is None:
            return None
        if cal is None or cal.error:
            wd_notes.add("a calendar is missing or unreadable: calendar days used instead of working days there")
            return round((b - a).total_seconds() / 86400, 1)
        return round(cal.work_days(a, b), 1) + 0.0  # + 0.0: -0.0 becomes 0.0
    sep = _name_sep(con, pid)
    nodes = _wbs_tree(con, pid, sep)
    if not nodes:
        raise ValueError(f"Project {prj['proj_short_name']} has no project WBS node.")
    own = {r["wbs_id"]: {k: r[k] for k in r.keys() if k != "wbs_id"} for r in con.execute(
        f"SELECT t.wbs_id AS wbs_id, {_act_agg_sql(con)} FROM TASK t LEFT JOIN CALENDAR c ON c.clndr_id = t.clndr_id "
        "WHERE t.proj_id = ?" + al("TASK", "t") + " GROUP BY t.wbs_id", (pid,))}

    def bdate(x: str, kind: str) -> str:  # baseline date: actual else early ('current'), or planned
        return (f"COALESCE({x}.act_{kind}_date, {x}.early_{kind}_date)" if baseline_dates == "current"
                else f"{x}.target_{kind}_date")
    bl_finish, bl_act = None, {}  # baseline project finish (its latest activity date); dates by Activity ID
    if bid is not None:
        bl_finish = _db_dt(con.execute(f"SELECT MAX({bdate('x', 'end')}) FROM TASK x WHERE x.proj_id = ?" + al("TASK", "x"),
                                       (bid,)).fetchone()[0])
        bl_act = {r[0]: (_db_dt(r[1]), _db_dt(r[2])) for r in con.execute(
            f"SELECT x.task_code, {bdate('x', 'start')}, {bdate('x', 'end')} FROM TASK x WHERE x.proj_id = ?"
            + al("TASK", "x") + " ORDER BY x.task_id DESC", (bid,))}  # lowest task_id wins
    dd = _db_dt(prj["last_recalc_date"])
    top = nodes[0]["wbs_code"]
    code_of = {n["wbs_id"]: n["wbs_code"][len(top) + len(sep):] if n["depth"] else top for n in nodes}
    acts = []
    for r in con.execute(_act_from(con) + " ORDER BY COALESCE(t.act_start_date, t.early_start_date), t.task_code, "
                         "t.task_id", (pid,)):
        a = _dict(r)
        num = lambda k, a=a: float(a.get(k) or 0)  # noqa: E731
        bs, bf = bl_act.get(a["task_code"], (None, None))
        s_act, f_act = _db_dt(a["act_start_date"]), _db_dt(a["act_end_date"])
        f = f_act or _db_dt(a["early_end_date"])
        done = a["status_code"] == "TK_Complete"
        orig_h, rem_h = num("target_drtn_hr_cnt"), num("remain_drtn_hr_cnt")
        kind = a.get("complete_pct_type")  # the activity's % complete as P6 shows it, by its percent complete type
        if kind == "CP_Drtn":
            pct = 100 * (orig_h - rem_h) / orig_h if orig_h else 100.0 * done
        elif kind == "CP_Units":
            at = sum(num(f"{k}_{u}_qty") for k in ("act", "remain") for u in ("work", "equip"))
            pct = 100 * (num("act_work_qty") + num("act_equip_qty")) / at if at else 100.0 * done
        else:
            pct = num("phys_complete_pct")
        end = f_act or dd
        acts.append({"task_code": a["task_code"], "task_name": a["task_name"], "task_type": a["task_type"],
                     "status_code": a["status_code"], "wbs_id": a["wbs_id"], "wbs_name": a["wbs_name"],
                     "wbs_code": code_of.get(a["wbs_id"]), "start": _db_dt(a["act_start_date"] or a["early_start_date"]),
                     "finish": f, "bl_start": bs, "bl_finish": bf, "slip_d": wd(cals.get(a["clndr_id"]), bf, f),
                     "act_start": s_act, "act_finish": f_act, "plan_start": _db_dt(a["target_start_date"]),
                     "plan_finish": _db_dt(a["target_end_date"]), "pct": round(min(100.0, max(0.0, pct)), 2),
                     "lab_t": num("target_work_qty"), "lab_a": num("act_work_qty"), "lab_r": num("remain_work_qty"),
                     "orig_h": orig_h, "rem_h": rem_h, "orig_d": _days(a["target_drtn_hr_cnt"], a["hpd"]),
                     "rem_d": _days(a["remain_drtn_hr_cnt"], a["hpd"]),
                     "act_d": max(0.0, wd(cals.get(a["clndr_id"]), s_act, end)) if s_act and end else None,
                     "tf_d": _days(a["total_float_hr_cnt"], a["hpd"]) if not done else None,
                     "longest": a.get("driving_path_flag") == "Y", "float_path": a.get("float_path"),
                     "float_path_order": a.get("float_path_order")})
    # baseline planned units at the data date per WBS (baseline assignments by the Activity ID of the current
    # activities, linear between their planned dates as p6_progress_curve), rolled up below
    bpl: dict[int, dict] = {}
    if bid is not None and dd is not None:
        bpl = _bl_planned_by_wbs(con, bid, dd, {a["task_code"]: a["wbs_id"] for a in acts})
    # band values of every WBS: its activities and the WBS below it
    lows, highs = ("bl_s", "s", "as", "ps", "os"), ("bl_f", "f", "af", "pf", "of")

    def merge(acc: dict, part: Mapping) -> dict:  # minimum or maximum of dates, sum of the rest; None ignored
        for k, v in part.items():
            if v is not None:
                a0 = acc.get(k)
                acc[k] = v if a0 is None else min(a0, v) if k in lows else max(a0, v) if k in highs else a0 + v
        return acc
    band_own: dict[int, dict] = {}
    for a in acts:
        is_open = a["status_code"] != "TK_Complete"
        merge(band_own.setdefault(a["wbs_id"], {}), {
            "n": 1, "done": 0 if is_open else 1, "bl_s": a["bl_start"], "bl_f": a["bl_finish"], "s": a["start"],
            "f": a["finish"], "as": a["act_start"], "af": a["act_finish"], "ps": a["plan_start"], "pf": a["plan_finish"],
            "os": a["start"] if is_open else None, "of": a["finish"] if is_open else None, "lt": a["lab_t"],
            "la": a["lab_a"], "lr": a["lab_r"], "oh": a["orig_h"], "rh": a["rem_h"]})
    ids = {n["wbs_id"] for n in nodes}
    roll: dict[int, dict] = {}
    proll: dict[int, dict] = {}
    band: dict[int, dict] = {}
    for n in reversed(nodes):  # descendants first
        w, par = n["wbs_id"], n["parent_wbs_id"]
        roll[w] = _merge_agg(roll.get(w, {}), own.get(w, {}))
        proll[w] = _merge_agg(proll.get(w, {}), bpl.get(w, {}))
        band[w] = merge(band.get(w, {}), band_own.get(w, {}))
        if par in ids:
            roll[par] = _merge_agg(roll.get(par, {}), roll[w])
            proll[par] = _merge_agg(proll.get(par, {}), proll[w])
            band[par] = merge(band.get(par, {}), band[w])
    rows = []
    for n in nodes:
        r, pl, b = _rollup(roll[n["wbs_id"]]), proll[n["wbs_id"]], band[n["wbs_id"]]
        n_, all_done = b.get("n", 0), b.get("n", 0) and b.get("done", 0) == b.get("n", 0)
        lab, oh = b.get("la", 0) + b.get("lr", 0), b.get("oh", 0)
        os_ = max(b["os"], dd) if b.get("os") and dd else b.get("os")
        rows.append({"wbs_id": n["wbs_id"], "depth": n["depth"], "code": code_of[n["wbs_id"]], "name": n["wbs_name"],
                     "units_pct": r["units_pct"], "budget_units": r["budget_units"],
                     "bl_planned_pct": round(100 * pl["bl_plan"] / pl["bl_budget"], 2) if pl.get("bl_budget") else None,
                     "finish_var_d": wd(pcal, b.get("bl_f"), b.get("f")), "all_done": bool(all_done),
                     "band": {"activities": n_, "bl_start": b.get("bl_s"), "bl_finish": b.get("bl_f"), "start": b.get("s"),
                              "finish": b.get("f"), "act_start": b.get("as"), "act_finish": b.get("af") if all_done else None,
                              "pct": (round(100 * b["la"] / lab, 2) if lab else round(100 * (oh - b.get("rh", 0)) / oh, 2)
                                      if oh else 100.0 if all_done else None) if n_ else None,
                              "lab_t": b.get("lt"), "lab_a": b.get("la"), "lab_r": b.get("lr"),
                              "orig_d": wd(pcal, b.get("ps"), b.get("pf")),
                              "rem_d": max(0.0, wd(pcal, os_, b["of"])) if os_ and b.get("of") else 0.0 if n_ else None,
                              "act_d": max(0.0, wd(pcal, b["as"], b.get("af") if all_done else dd))
                              if b.get("as") and (all_done or dd) else None}})
    cut = len(rows) > DASH_WBS_ROWS
    q, args = _act_from(con), [pid]
    if codes:
        q += f" AND t.task_code IN ({', '.join('?' * len(codes))})"
        args += list(codes)
    else:
        q += " AND t.task_type IN ('TT_Mile', 'TT_FinMile')"
    found: dict[str, dict] = {}
    for r in con.execute(q + " ORDER BY t.task_code, t.task_id", args):
        found.setdefault(r["task_code"], _dict(r))
    if codes and set(codes) - set(found):
        raise ValueError(f"Activity IDs not found in {prj['proj_short_name']}: {sorted(set(codes) - set(found))}")

    def mdate(m: Mapping, planned: bool = False) -> Optional[datetime]:  # the milestone's one date
        kind = "start" if m["task_type"] == "TT_Mile" else "end"
        return _db_dt(m[f"target_{kind}_date"] if planned else m[f"act_{kind}_date"] or m[f"early_{kind}_date"])
    ms = sorted(found.values(), key=lambda m: (mdate(m) or datetime.max, m["task_code"]))
    ms_note = None
    if not codes and len(ms) > DASH_MILESTONES:
        ms_note = f"{len(ms)} milestones: the first {DASH_MILESTONES} by date are shown (pass milestones= to choose)."
        ms = ms[:DASH_MILESTONES]
    bl: dict[str, dict] = {}
    if bid is not None and ms:
        bl = {r["task_code"]: _dict(r) for r in con.execute(
            "SELECT task_code, act_start_date, act_end_date, early_start_date, early_end_date, target_start_date, "
            f"target_end_date FROM TASK WHERE proj_id = ? AND task_code IN ({', '.join('?' * len(ms))})" + al("TASK")
            + " ORDER BY task_id DESC", [bid, *(m["task_code"] for m in ms)])}
    out_ms = []
    for m in ms:
        cur, b = mdate(m), bl.get(m["task_code"])
        bd = mdate({**b, "task_type": m["task_type"]}, baseline_dates == "planned") if b else None
        out_ms.append({"task_code": m["task_code"], "task_name": m["task_name"], "task_type": m["task_type"],
                       "status_code": m["status_code"], "date": cur, "baseline": bd,
                       "variance_d": wd(cals.get(m["clndr_id"]), bd, cur),
                       "total_float_d": _days(m["total_float_hr_cnt"], m["hpd"])})
    scd, must = (_db_dt(prj[k]) for k in ("scd_end_date", "plan_end_date"))
    kpi = {"vs_must_d": wd(pcal, must, scd), "vs_baseline_d": wd(pcal, bl_finish, scd),
           "remaining_d": max(0.0, wd(pcal, dd, scd)) if dd and scd else None}
    return {"project": prj, "baseline": base, "bl_finish": bl_finish, "rows": rows[:DASH_WBS_ROWS], "rows_cut": cut,
            "milestones": out_ms, "ms_note": ms_note, "calendar": pcal.name if pcal else None,
            "hpd": pcal.hpd if pcal else 8.0, "kpi": kpi, "acts": acts, "wd_notes": sorted(wd_notes)}


def _xl_shown(value, fmt: str) -> str:
    """A card value as Excel shows it with the dashboard's number formats (halves away from zero, as Excel);
    None is "n/a"."""
    if value is None:
        return "n/a"
    if isinstance(value, datetime):
        return value.strftime("%d-%b-%y") if fmt == "dd-mmm-yy" else _fmt_dt(value)
    def num(v, nd: int) -> str:
        q = Decimal(repr(float(v))).quantize(Decimal(1).scaleb(-nd), rounding=ROUND_HALF_UP)
        return f"{q:,.{nd}f}" if "," in fmt else f"{q:.{nd}f}"
    if fmt == '0.00"%"':
        return num(value, 2) + "%"
    if fmt.startswith('+0.0"%"'):
        t = num(abs(value), 1)
        return ("+" if value > 0 else "-" if value < 0 else "") + t + "%"  # Excel picks the section by the value
    if fmt in ('0" d"', '0.0" d"'):
        return num(value, 0 if fmt == '0" d"' else 1) + " d"
    if fmt in ("#,##0", "0"):
        return num(value, 0)
    return str(value)


def _dash_book(d: dict) -> tuple[Any, dict, list]:
    """The dashboard workbook (openpyxl) of what p6_export_dashboard gathered, the data rows written
    per sheet and the 7 card values ({card, value, detail}, dates as YYYY-MM-DD). Sheets in DASH_SHEETS
    order; the Dashboard sheet (fixed objects, protected) has a sidebar of links to the others, which
    link back to it."""
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont
    from openpyxl.chart import BarChart, LineChart, Reference
    from openpyxl.chart.axis import ChartLines
    from openpyxl.chart.label import DataLabelList
    from openpyxl.chart.marker import DataPoint
    from openpyxl.chart.shapes import GraphicalProperties
    from openpyxl.drawing.line import LineProperties
    from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, TwoCellAnchor
    from openpyxl.formatting.rule import DataBarRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Protection, Side
    from openpyxl.utils import get_column_letter as letter
    from openpyxl.utils.units import pixels_to_EMU
    from openpyxl.worksheet.hyperlink import Hyperlink
    from openpyxl.worksheet.page import PageMargins

    DATE, DAYS, SIGNED, PCT = "dd-mmm-yy", "0.0", "+0.0;-0.0;0.0", '0.0"%"'
    near, rd, st, hc, sm = d["near"], d["rd"], d["state"], d["hc"], d["summary"]
    dd = _db_dt(rd["project"]["last_recalc_date"])
    lsd = _db_dt(rd["project"]["last_schedule_date"])
    bname = rd["baseline"]["proj_short_name"] if rd["baseline"] else None
    counts: dict[str, int] = {}
    fills: dict[str, Any] = {}
    wb = Workbook()
    wb.properties.creator = "MCP for Primavera P6 server"
    wb.properties.title = f"{d['project']} project controls dashboard"
    trend = d.get("trend")
    names = [n for n in DASH_SHEETS if n != "Trend" or trend]
    sheets = {names[0]: wb.active}
    sheets[names[0]].title = names[0]
    for n in names[1:]:
        sheets[n] = wb.create_sheet(n)
    brand = d["brand"]
    head, accent = brand["primary"], brand["accent"]  # headers, sidebar and dark bars; S-curve actual line
    head_ink = _ink_on(head)
    sheets[DASH_SHEETS[0]].sheet_properties.tabColor = head
    crit, acts, ms = d["crit"], rd["acts"], rd["milestones"]

    def fclass(tf: Optional[float]) -> Optional[str]:
        return None if tf is None else "neg" if tf < 0 else "near" if tf <= near else "ok"

    def late(v: Optional[float]) -> str:  # a slip in working days against the baseline
        return "No baseline" if v is None else "Delayed" if v > near else "At risk" if v > 0 else "On track"

    def act_status(a: Mapping) -> str:  # Completed, Critical (project definition), else by the slip
        return "Completed" if a["status_code"] == "TK_Complete" else "Critical" if a["task_code"] in crit else late(a["slip_d"])

    def fill(color: str):
        if color not in fills:
            fills[color] = PatternFill("solid", fgColor=color)
        return fills[color]

    def mix(color: str, f: float) -> str:  # color moved toward white by f (0 to 1)
        return "".join(f"{round(int(color[i:i + 2], 16) + (255 - int(color[i:i + 2], 16)) * f):02X}" for i in (0, 2, 4))

    fonts: dict = {}
    aligns: dict = {}

    def put(ws, r: int, c: int, v, fmt: Optional[str] = None, bold: bool = False, size: float = 10,
            color: str = DASH_INK, bg: Optional[str] = None, h: Optional[str] = None, wrap: bool = False, indent: int = 0,
            valign: str = "center", font: str = "Calibri"):
        if isinstance(v, str):
            v = ILLEGAL_CHARACTERS_RE.sub("", v)[:32000]
        cell = ws.cell(row=r, column=c, value=v)
        if isinstance(v, str) and v.startswith("="):
            cell.data_type = "s"  # text from the database, never a formula
        # One Font / Alignment object per distinct style, as fill(): openpyxl hashes every new style
        # object it is given, which was most of the export time on a large project.
        fk = (font, size, bold, color)
        if fk not in fonts:
            fonts[fk] = Font(name=font, size=size, bold=bold, color=color)
        cell.font = fonts[fk]
        ak = (h, valign, wrap, indent)
        if ak not in aligns:
            aligns[ak] = Alignment(horizontal=h, vertical=valign, wrap_text=wrap, indent=indent)
        cell.alignment = aligns[ak]
        if fmt:
            cell.number_format = fmt
        if bg:
            cell.fill = fill(bg)
        return cell

    def merged(ws, r: int, c0: int, c1: int, v, r1: Optional[int] = None, **kw):
        if c1 > c0 or (r1 or r) > r:
            ws.merge_cells(start_row=r, start_column=c0, end_row=r1 or r, end_column=c1)
        return put(ws, r, c0, v, **kw)

    def rich(*parts: tuple) -> Any:  # (text, color, symbol font?, bold?, size) blocks in one cell
        return CellRichText(*(TextBlock(InlineFont(rFont="Segoe UI Symbol" if sym else "Calibri", sz=size, b=bold or None,
                                                   color=color), text) for text, color, sym, bold, size in parts))

    def link(ws, r: int, c: int, text: str, sheet: str, color: str = "0563C1", size: float = 9) -> None:
        cell = put(ws, r, c, text, size=size, color=color)
        cell.font = Font(name="Calibri", size=size, color=color, underline="single")
        cell.hyperlink = Hyperlink(ref=cell.coordinate, location=f"'{sheet}'!A1", display=text)

    def top(ws, title: str, sub: Optional[str] = None) -> None:
        put(ws, 1, 1, title, bold=True, size=14)
        if sub:
            put(ws, 2, 1, sub, color=DASH_MUTED)
        ws.row_dimensions[1].height = 22
        link(ws, 3, 1, "Back to the Dashboard", DASH_SHEETS[0])

    def header(ws, r: int, c0: int, labels: Sequence[str], widths: Optional[Sequence[float]] = None) -> None:
        for i, lab in enumerate(labels):
            put(ws, r, c0 + i, lab, bold=True, size=9, color=head_ink, bg=head, h="center", wrap=True)
            if widths:
                ws.column_dimensions[letter(c0 + i)].width = widths[i]
        ws.row_dimensions[r].height = 30

    def page(ws, one_page: bool = False, title_rows: Optional[str] = None) -> None:
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_A3
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 1 if one_page else 0
        ws.page_margins = PageMargins(left=0.4, right=0.4, top=0.5, bottom=0.5)
        if title_rows:
            ws.print_title_rows = title_rows

    def section(ws, r: int, c0: int, c1: int, text: str) -> None:
        put(ws, r, c0, text, bold=True, size=11)
        for c in range(c0, c1 + 1):
            ws.cell(r, c).border = Border(bottom=Side(style="thin", color=DASH_RULE))

    def axes(ch, legend: str = "b") -> None:
        ch.roundedCorners = False
        ch.x_axis.delete = False
        ch.y_axis.delete = False
        ch.y_axis.majorGridlines = ChartLines(spPr=GraphicalProperties(ln=LineProperties(solidFill="E5E7EB")))
        ch.x_axis.majorGridlines = None
        if legend:
            ch.legend.position = legend
            ch.legend.overlay = False  # Excel overlays the plot when the flag is missing
        else:
            ch.legend = None

    def borderless(ch) -> Any:  # a chart inside a white panel: no frame of its own
        ch.graphical_properties = GraphicalProperties(ln=LineProperties(noFill=True))
        return ch

    def values_only():  # data labels with the value alone (Excel adds series and category names otherwise)
        return DataLabelList(showVal=True, showSerName=False, showCatName=False, showLegendKey=False,
                             showPercent=False, showBubbleSize=False, dLblPos="outEnd")

    def line(s, color: str, width: float = 2.0, dash: Optional[str] = None) -> None:
        s.graphicalProperties.line.solidFill = color
        s.graphicalProperties.line.width = int(width * 12700)  # points to EMU
        if dash:
            s.graphicalProperties.line.dashStyle = dash
        s.smooth = False
        s.marker.symbol = "none"

    def bars(s, color: str) -> None:
        s.graphicalProperties = GraphicalProperties(solidFill=color)
        s.graphicalProperties.line.solidFill = color
        s.invertIfNegative = False  # a negative bar keeps its color

    def col_px(width: float) -> int:  # pixels of a column of that width (Calibri 11: 7 px per character)
        return int((256 * width + int(128 / 7)) / 256 * 7)

    def row_px(height: Optional[float]) -> int:  # pixels of a row of that height in points (Excel rounds down)
        return int((height or 15) * 4 / 3 + 1e-9)

    def act_label(code: str, name: Optional[str]) -> str:
        return f"{code} - {name or ''}".rstrip(" -")

    def pill(ws, r: int, c: int, status: str) -> None:  # a status as colored text on its tint
        bg, fg = DASH_PILL.get(status, DASH_PILL["Completed"])
        cell = ws.cell(r, c)
        cell.fill = fill(bg)
        cell.font = Font(name="Calibri", size=9, bold=True, color=fg)

    # ---- Schedule Overview: every activity grouped by WBS, a band row per WBS (all levels), outline levels
    ws = sheets["Schedule Overview"]
    shown = acts[:DASH_ACT_CAP]
    top(ws, "Schedule Overview")  # what the columns hold: the Reports notes
    labels =["Activity ID", "Activity Name", "BL Start", "BL Finish", "Start", "Finish", "Actual Start", "Actual Finish",
              "% Complete", "Budgeted Labor Units", "Actual Labor Units", "Remaining Labor Units", "Original Duration",
              "Remaining Duration", "Actual Duration"]
    header(ws, 4, 1, labels, [16, 62, 11, 11, 11, 11, 11, 11, 10, 12, 12, 12, 10, 10, 10])
    fmts = (None, None, DATE, DATE, DATE, DATE, DATE, DATE, PCT, "#,##0.0", "#,##0.0", "#,##0.0", DAYS, DAYS, DAYS)
    by_wbs: dict[int, list] = {}
    for a in shown:
        by_wbs.setdefault(a["wbs_id"], []).append(a)
    r = 5
    for w in rd["rows"]:
        b, depth = w["band"], w["depth"]
        bg = mix(head, min(0.94, 0.55 + 0.1 * depth))  # the band color lightens with the depth, as P6 bands
        ink = _ink_on(bg)
        vals = [w["code"], w["name"], b["bl_start"], b["bl_finish"], b["start"], b["finish"], b["act_start"],
                b["act_finish"], b["pct"], b["lab_t"], b["lab_a"], b["lab_r"], b["orig_d"], b["rem_d"], b["act_d"]]
        for j, (v, f) in enumerate(zip(vals, fmts, strict=False)):
            put(ws, r, 1 + j, v, fmt=f, size=9, bold=True, color=ink, bg=bg, indent=depth if j == 1 else 0,
                h="center" if j == 0 else None)
        ws.row_dimensions[r].outline_level = min(depth, 7)
        ws.row_dimensions[r].hidden = depth > DASH_SO_LEVEL
        r += 1
        for a in by_wbs.get(w["wbs_id"], []):
            vals = [a["task_code"], a["task_name"], a["bl_start"], a["bl_finish"], a["start"], a["finish"], a["act_start"],
                    a["act_finish"], a["pct"], a["lab_t"], a["lab_a"], a["lab_r"], a["orig_d"], a["rem_d"], a["act_d"]]
            for j, (v, f) in enumerate(zip(vals, fmts, strict=False)):
                put(ws, r, 1 + j, v, fmt=f, size=9, indent=depth + 1 if j == 1 else 0, h="center" if j == 0 else None)
            ws.row_dimensions[r].outline_level = min(depth + 1, 7)
            ws.row_dimensions[r].hidden = True  # folded to the WBS bands of DASH_SO_LEVEL
            r += 1
    for rr in range(5, r - 1):  # a band whose rows below are folded shows +, as Excel marks it (nested ones too)
        nxt = ws.row_dimensions[rr + 1]
        if nxt.hidden and nxt.outline_level > ws.row_dimensions[rr].outline_level:
            ws.row_dimensions[rr].collapsed = True
    ws.sheet_properties.outlinePr.summaryBelow = False  # a band is above its activities
    ws.auto_filter.ref = f"A4:{letter(len(labels))}{r - 1}"
    ws.freeze_panes = "C5"
    page(ws, title_rows="4:4")
    counts["Schedule Overview"] = len(shown)

    # ---- Critical Path: P6 float paths (path 1 = most critical), a bold line between paths
    ws = sheets["Critical Path"]
    fp = sorted((a for a in acts if a["float_path"] is not None and a["task_type"] not in ("TT_LOE", "TT_WBS")),
                key=lambda a: (a["float_path"], a["float_path_order"] or 0, a["task_code"]))
    if fp:
        groups = [(f"Path {k}", list(g)) for k, g in itertools.groupby(fp, key=lambda a: a["float_path"])]
        cp_how = (f"the {len(groups)} float paths P6 calculated at the last F9 (path 1 = most critical), each in P6's "
                  "order (float_path_order)")
    else:
        near_rows = sorted((a for a in acts if a["status_code"] != "TK_Complete" and a["task_type"] not in ("TT_LOE", "TT_WBS")
                            and a["tf_d"] is not None and a["tf_d"] <= near),
                           key=lambda a: (a["tf_d"], a["start"] or datetime.max, a["task_code"]))
        groups = [(f"TF {k:g} d", list(g)) for k, g in itertools.groupby(near_rows, key=lambda a: a["tf_d"])]
        cp_how = (f"no float paths are stored (enable multiple float paths in the P6 schedule options), so it lists the "
                  f"open activities with total float up to {near:g} d, grouped by total float value")
    top(ws, "Critical Path")  # how the paths were chosen: the Reports notes
    labels = ["Path", "Order", "Activity ID", "Activity Name", "WBS", "P6 status", "Start", "Finish", "BL Finish",
              "Slip (wd)", "Remaining (d)", "Total float (d)", "Status"]
    header(ws, 4, 1, labels, [10, 7, 18, 56, 30, 12, 11, 11, 11, 10, 11, 11, 12])
    thick = Side(style="thick", color=DASH_INK)
    r = 5
    for name, rows_ in groups:
        for i, a in enumerate(rows_):
            status = act_status(a)
            vals = [name if i == 0 else None, a["float_path_order"] if fp else i + 1, a["task_code"], a["task_name"],
                    a["wbs_name"], STATUS_NAMES.get(a["status_code"], a["status_code"]), a["start"], a["finish"],
                    a["bl_finish"], a["slip_d"], a["rem_d"], a["tf_d"], status]
            for j, (v, f) in enumerate(zip(vals, (None, "0", None, None, None, None, DATE, DATE, DATE, SIGNED, DAYS, DAYS,
                                                  None), strict=False)):
                put(ws, r, 1 + j, v, fmt=f, size=9, bold=j == 0, h="center" if j in (1, 12) else None)
            pill(ws, r, 13, status)
            cls = fclass(a["tf_d"])
            if cls:
                ws.cell(r, 12).fill = fill(DASH_TINT[cls])
            if i == len(rows_) - 1:  # the bold line under the last activity of the path
                for c in range(1, len(labels) + 1):
                    ws.cell(r, c).border = Border(bottom=thick)
            r += 1
    if not groups:
        put(ws, 5, 1, "No activity.", color=DASH_MUTED)
    ws.freeze_panes = "E5"
    page(ws, title_rows="4:4")
    counts["Critical Path"] = r - 5

    # ---- Reports: schedule quality (DCMA 14), about
    ws, chk = sheets["Reports"], hc["checks"]
    top(ws, "Reports","Schedule quality (DCMA 14) and the sources, settings and definitions of this dashboard.")
    section(ws, 4, 1, 6, "Schedule quality (DCMA 14)")
    header(ws, 5, 1, ["Check", "Result", "Count", "Of", "Value", "Threshold"], [44, 16, 12, 12, 12, 14])
    qrows = [k for k in DCMA_CHECKS if k in chk and k not in HC_UNSCORED] + (["key_dates"] if "key_dates" in chk else [])
    for i, k in enumerate(qrows):
        c, rule = chk[k], DCMA_CHECKS.get(k, (None, None))[1]
        ratio = rule == "ratio"
        thr = c.get("threshold")
        thr_txt = "" if thr is None or rule is None else ("<= " if rule == "max" else ">= ") + (f"{thr:g}" if ratio else f"{thr:g} %")
        res = "Information" if k == "key_dates" else {True: "Pass", False: "Fail", None: "Not evaluated"}[c.get("pass")]
        vals = [DCMA_LABELS.get(k, k), res, c.get("count"), c.get("denominator"), c.get("value") if ratio else c.get("pct"),
                thr_txt]
        for j, (v, f) in enumerate(zip(vals, (None, None, "#,##0", "#,##0", "0.00" if ratio else '0.00"%"', None), strict=False)):
            put(ws, 6 + i, 1 + j, v, fmt=f, size=9, h="center" if j in (1, 5) else None)
        ws.cell(6 + i, 2).fill = fill(DASH_TINT["pass" if res == "Pass" else "fail" if res == "Fail" else "na"])
    counts["Reports"] = len(qrows)
    r = 6 + len(qrows) + 1
    section(ws, r, 1, 6, "About this dashboard")
    cv = d["curve"]
    cvb = cv and cv["baseline"]
    facts = [("Project", d["project"]), ("Project name", d["name"]), ("Company", brand["company"]),
             ("Prepared by", d["prepared_by"]), ("Revision", d["revision"]), ("Database file", d["db"]),
             ("Project calendar", rd["calendar"]), ("Data date", dd), ("Last schedule run (F9)", lsd),
             ("Edited since the last F9", "Yes: open P6 and press F9, then export again" if st["stale"] else "No"),
             ("Baseline", f"{bname} ({'baseline=' if d['baseline_arg'] else 'project baseline'}), {d['baseline_dates']} dates"
              if bname else "None: set a project baseline in P6 or pass baseline="),
             ("Critical", sm["critical"]["definition"]), ("Delayed and at risk", f"more than {near:g} / up to {near:g} "
                                                                                  "working days after the baseline finish"),
             ("S-curve", f"{cv['measure']['kind']} per {cv['period']}" + (f", baseline {cvb['project']}" if cvb else "")
              if cv else "not drawn"),
             ("DCMA thresholds", hc["threshold_source"]),
             *([("Trend", f"{len(trend['rows'])} updates, data dates {trend['rows'][0]['data_date'][:10]} to "
                          f"{trend['rows'][-1]['data_date'][:10]}: " + ", ".join(u["project"] for u in trend["rows"]))]
               if trend else []),
             ("Generated", d["now"]), ("Server version", SERVER_VERSION)]
    r += 1
    for i, (k, v) in enumerate(facts):
        put(ws, r + i, 1, k, bold=True, size=9)
        merged(ws, r + i, 2, 6, v, fmt="dd-mmm-yyyy hh:mm" if isinstance(v, datetime) else None, size=9, h="left")
    r += len(facts) + 1
    put(ws, r, 1, "Definitions and notes", bold=True, size=11)
    notes = [
        "Dates, float and the critical path are those of the last F9 in P6; changes made after it show only once P6 "
        "reschedules.",
        f"Slip and variance: working days between the baseline date and the forecast or actual date, on the project "
        f"calendar ({rd['calendar'] or 'none'}) for the project figures and on each activity's or milestone's own calendar "
        "otherwise, as P6 variance columns.",
        f"Status: Critical = the project's critical definition; Delayed = open and more than {near:g} working days later "
        f"than the baseline finish; At risk = later by up to {near:g}; On track = not later.",
        "Remaining duration (card): working days of the project calendar from the data date to the forecast finish.",
        f"Schedule Overview: every activity of the last F9 ({len(acts):,}" + (f", the first {DASH_ACT_CAP:,} listed"
        if len(acts) > DASH_ACT_CAP else "") + f") under a band per WBS in P6 order; it opens folded to the WBS bands of "
        f"level {DASH_SO_LEVEL} (outline buttons on the left show the activities). Baseline dates of the same Activity "
        "ID; % complete by each activity's percent complete type; labor units as stored; original and remaining "
        "durations in days of the activity calendar, actual duration in working days to the actual finish or data date.",
        "Schedule Overview WBS bands: earliest start and latest finish of their activities (actual finish once all are "
        "completed), summed labor units, % complete = actual / (actual + remaining) labor units, else the duration %; "
        "original duration from the earliest planned start to the latest planned finish, remaining from the data date "
        "(or the earliest open start) to the latest open finish, actual from the earliest actual start to the data date "
        "(or the last actual finish), in working days of the project calendar.",
        f"Critical Path sheet: {cp_how}. A bold line separates the paths. Slip against the baseline finish in working "
        "days of the activity calendar; total float in days.",
        "DCMA checks: open activities = not completed, LOE and WBS summary left out; the relationship checks count the "
        "links whose successor is open.",
        "WBS Progress and Level 2 Progress (dashboard): units % complete = actual / (actual + remaining) labor and "
        "nonlabor units; baseline planned % = baseline assignments of the activities with the same Activity ID, spread "
        "linearly between their planned dates, at the data date.",
        "Finish Variance by WBS (dashboard): working days of the project calendar from the latest baseline finish to "
        "the latest finish of the level 1 WBS's activities; colored by the status rule above (gray: all completed).",
        "S-curve: quantities are spread linearly over calendar time between the assignment dates (P6 spreads over "
        "working time and resource curves) and the actual curve is rebuilt from the actual dates, so only the values at "
        "the data date are exact. Earned value = budget x activity % complete (its percent complete type).",
        *([f"Trend sheet: one row per update of trend=, ordered by data date, each from its own last F9: scheduled "
           "finish and must finish date; the finish against the must finish date, the first and the previous update "
           "and the update's baseline in working days of the update's project calendar; units % complete, baseline "
           "planned units % at the data date and SPI (units) as the cards; critical activities (project definition), "
           f"near critical (open, not critical, total float up to {near:g} d), negative float, minimum and median total "
           "float of open activities; the DCMA weighted score with its failed checks and BEI; activities and links added "
           "and removed and durations cut since the previous update (activities by Activity ID). Charts: forecast "
           "finish and must finish, then units % complete, baseline planned % and SPI (right axis) per data date. "
           "Milestones: those in every update (or milestones=), dates actual (A) else forecast."] if trend else []),
        *d["warnings"]]
    for i, n in enumerate(notes):
        put(ws, r + 1 + i, 1, "Warning" if i >= len(notes) - len(d["warnings"]) else None, size=9, bold=True,
            color=DASH_FLOAT["neg"], valign="top")
        merged(ws, r + 1 + i, 2, 6, n, size=9, wrap=True, valign="top")
        ws.row_dimensions[r + 1 + i].height = 13 * max(1, math.ceil(len(n) / 95))
    page(ws)

    # ---- Dashboard: sidebar, header, 7 cards, then 6 panels, on one A3 page; objects fixed, sheet protected
    ws = sheets["Dashboard"]
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 85
    G0, NG, LAST = 3, 27, 60  # first grid column (C), grid columns, last row
    col = lambda k: G0 + k  # noqa: E731
    hc0 = col(NG) + 2  # hidden helper data of the charts, right of the print area
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 26, 1.5
    for k in range(NG):  # 7 groups of 3 wide columns (a card) and a narrow gutter, the last without: the A3 width
        ws.column_dimensions[letter(col(k))].width = 6.14 if k % 4 == 3 else 10.29
    side_ink = _ink_on(head)
    side_soft = mix(head, 0.65) if side_ink == "FFFFFF" else DASH_MUTED
    for rr in range(1, LAST + 1):
        ws.cell(rr, 1).fill = fill(head)
        for c in range(2, col(NG)):
            ws.cell(rr, c).fill = fill(DASH_PAGE)
    placed: list[tuple] = []  # (chart, row, column, (width, height) in cm): anchored once every size is final

    def area(r0: int, r1: int, c0_: int, c1: int, color: str = "FFFFFF") -> None:
        for rr in range(r0, r1 + 1):
            for c in range(c0_, c1 + 1):
                ws.cell(rr, c).fill = fill(color)
    frames: list[tuple] = []  # (r0, r1, c0, c1) drawn last, after every merge

    def box(r0: int, r1: int, k0: int, k1: int) -> None:
        area(r0, r1, col(k0), col(k1))
        frames.append((r0, r1, col(k0), col(k1)))

    def panel_title(r: int, k: int, text: str) -> None:
        put(ws, r, col(k), text, bold=True, size=12, indent=1)
        ws.row_dimensions[r].height = 22

    # sidebar: company, links to every sheet, the project at the bottom
    merged(ws, 1, 1, 1, brand["company"] or "Schedule dashboard", r1=2, bold=True, size=13, color=side_ink, bg=head,
           indent=1, wrap=True)
    put(ws, 3, 1, "Primavera P6 schedule", size=9, color=side_soft, bg=head, indent=1)
    for i, name in enumerate(names):  # one link every other row from row 12 (rows of the panels, 15 points)
        cell = ws.cell(12 + 2 * i, 1)
        cell.value = rich((DASH_NAV[name] + "  ", side_ink, True, False, 11), (name, side_ink, False, i == 0, 11))
        cell.fill = fill(mix(head, 0.18) if i == 0 else head)
        cell.alignment = Alignment(vertical="center", indent=1)
        if i:
            cell.hyperlink = Hyperlink(ref=cell.coordinate, location=f"'{name}'!A1", display=name)
            cell.protection = Protection(locked=False)  # the only cells that can be selected: the links work
    put(ws, LAST - 7, 1, d["project"], bold=True, size=11, color=side_ink, bg=head, indent=1)
    merged(ws, LAST - 6, 1, 1, d["name"] or "", r1=LAST - 3, size=9, color=side_soft, bg=head, indent=1, wrap=True,
           valign="top")
    put(ws, LAST - 2, 1, f"Data date {dd:%d %b %Y}" if dd else "No data date", size=9, color=side_soft, bg=head, indent=1)

    # header: title, subtitle, data date (its dot red when this server changed the schedule after the last F9)
    put(ws, 1, col(0), "Project Controls Dashboard", bold=True, size=20)
    put(ws, 2, col(0), f"Primavera P6 schedule analysis  |  Excel export  |  {d['project']}" + (f" - {d['name']}" if d["name"]
        else ""), size=10, color=DASH_MUTED)
    put(ws, 3, col(0), "  |  ".join(x for x in (d["prepared_by"] and f"Prepared by {d['prepared_by']}",
                                                 d["revision"] and f"Revision {d['revision']}",
                                                 f"Generated {d['now']:%d %b %Y %H:%M}") if x), size=9, color=DASH_MUTED)
    for rr, hh in ((1, 32), (2, 18), (3, 16)):
        ws.row_dimensions[rr].height = hh
    state = ws.cell(1, col(NG - 1))  # right-aligned on the right edge, running on to the left
    state.value = rich(("● ", DASH_GOOD if not st["stale"] else DASH_FLOAT["neg"], True, False, 12),
                       ("Data date " + (f"{dd:%d %b %Y %H:%M}" if dd else "none"), DASH_INK, False, True, 12))
    state.alignment = Alignment(horizontal="right", vertical="center")
    if st["stale"]:
        put(ws, 4, col(0), "Warning: this server changed the schedule after the last F9. Open P6, press F9, then export "
            "again.", bold=True, size=9, color=DASH_FLOAT["neg"])

    # cards
    pc, bl, cr = sm["percent_complete"], sm["baseline"], sm["critical"]
    kpi, ms_open = rd["kpi"], [m for m in ms if m["status_code"] != "TK_Complete" and m["variance_d"] is not None]
    real = [a for a in acts if a["task_type"] not in ("TT_LOE", "TT_WBS")]
    open_real = [a for a in real if a["status_code"] != "TK_Complete"]
    n_delayed = sum(1 for a in open_real if late(a["slip_d"]) == "Delayed")
    n_risk = sum(1 for a in open_real if late(a["slip_d"]) == "At risk")
    plan = bl.get("planned_units_pct_at_data_date")
    td = cv["to_date"] if cv else None
    sv = 100 * td["variance"] / td["baseline_planned"] if td and td["baseline_planned"] else None
    thr_h = cr.get("threshold_h")
    crit_sub = ("On the longest path (project setting)" if thr_h is None
                else f"Total float <= {thr_h / (rd['hpd'] or 8):g} d (project setting)")
    bfin, vb, rem = rd["bl_finish"], kpi["vs_baseline_d"], kpi["remaining_d"]
    n_late = sum(1 for m in ms_open if m["variance_d"] > 0)
    cards = [
        ("◔", "Overall Progress", pc["primary_pct"], '0.00"%"',
         f"Planned: {plan:.2f}%  |  Variance: {pc['primary_pct'] - plan:+.2f}%" if plan is not None and pc["primary_pct"]
         is not None else "No baseline plan at the data date", "0072B2", pc["primary_pct"]),
        ("◎", "Critical Activities", cr["critical"], "#,##0", crit_sub, DASH_FLOAT["neg"], None),
        ("◷", "Delayed Activities", n_delayed if bname else None, "#,##0",
         f"Behind baseline by over {near:g} wd; {n_risk:,} at risk" if bname else "No baseline", DASH_FLOAT["near"], None),
        ("Δ", "Schedule Variance", sv, '+0.0"%";-0.0"%";0.0"%"',
         f"Earned vs baseline planned (SPI {td['spi']:.2f})" if sv is not None else "Needs a baseline with resources",
         "7B61C9", None),
        ("⚑", "Forecast Completion", _db_dt(rd["project"]["scd_end_date"]), DATE,
         f"Baseline: {bfin:%d %b %Y} ({vb:+.1f} wd)" if bfin and vb is not None else "No baseline", "009E73", None),
        ("▸", "Remaining Duration", rem, '0" d"' if rem is None or float(rem).is_integer() else '0.0" d"',
         "Working days to the forecast finish", head, None),
        ("◆", "Milestones Late", n_late if ms_open else None, "0",
         f"of {len(ms_open)} open milestones with a baseline" if ms_open else "No open milestone with a baseline",
         DASH_FLOAT["neg"] if n_late else DASH_GOOD, None)]
    for k, (glyph, title, value, fmt, sub, color, bar) in enumerate(cards):
        c0_ = col(4 * k)
        merged(ws, 5, c0_, c0_ + 2, None, indent=1)  # the card's symbol in its color, then the title
        ws.cell(5, c0_).value = rich((glyph + "  ", color, glyph != "Δ", True, 12), (title, DASH_INK, False, True, 9))
        merged(ws, 6, c0_, c0_ + 2, "n/a" if value is None else value, r1=7, fmt=fmt, bold=True, size=18, h="center")
        merged(ws, 8, c0_, c0_ + 2, sub, size=8, color=DASH_MUTED, wrap=True, indent=1, valign="top")
        box(5, 9, 4 * k, 4 * k + 2)
        if bar is None:
            ws.cell(9, c0_).fill = fill(color)  # the short accent line of the card
        else:
            merged(ws, 9, c0_, c0_ + 2, bar, fmt=";;;", bg="FFFFFF")
            ws.conditional_formatting.add(f"{letter(c0_)}9:{letter(c0_ + 2)}9", DataBarRule(
                start_type="num", start_value=0, end_type="num", end_value=100, color=color, showValue=False))
    for rr, hh in ((5, 20), (6, 18), (7, 18), (8, 30), (9, 8)):
        ws.row_dimensions[rr].height = hh
    card_values = [{"card": title, "value": value.strftime("%Y-%m-%d") if isinstance(value, datetime)
                    else round(value, 2) if isinstance(value, float) else value, "text": _xl_shown(value, fmt),
                    "detail": sub} for _, title, value, fmt, sub, _, _ in cards]

    def rows_cm(r0: int, r1: int) -> float:  # height of rows r0 to r1, less a margin, in cm
        return sum(row_px(ws.row_dimensions[rr].height) for rr in range(r0, r1 + 1)) / 37.8 - 0.4

    def cols_cm(k0: int, k1: int) -> float:  # width of grid columns k0 to k1, less a margin, in cm
        return sum(col_px(ws.column_dimensions[letter(col(k))].width or 8.43) for k in range(k0, k1 + 1)) / 37.8 - 0.4

    def wlabel(w: Mapping) -> str:  # a WBS on a chart axis: its code (relative to the project) and name
        return (f"{w['code']}. {w['name']}" if w["depth"] == 1 else f"{w['code']} {w['name']}")[:40]

    def pick(level: int) -> tuple[list, int]:  # WBS of a level with units in P6 order; past the cap, the most budget
        all_ = [w for w in rd["rows"] if w["depth"] == level and w["units_pct"] is not None]
        keep = {w["wbs_id"] for w in sorted(all_, key=lambda w: (-(w["budget_units"] or 0), w["code"]))[:DASH_WBS_BARS]}
        return [w for w in all_ if w["wbs_id"] in keep], len(all_)

    def bar_chart(cats: int, c0_: int, series: Sequence[tuple[int, str]]) -> Any:
        """Horizontal bars of the helper rows 2 to cats + 1: categories in column c0_, a series (title in row 1)
        per (column, color); the data labels show the cells' number format."""
        ch = BarChart()
        ch.type, ch.grouping, ch.gapWidth, ch.overlap = "bar", "clustered", 60, 0
        ch.visible_cells_only = False
        for c, color in series:
            ch.add_data(Reference(ws, min_col=c, min_row=1, max_row=1 + cats), titles_from_data=True)
            bars(ch.series[-1], color)
        ch.set_categories(Reference(ws, min_col=c0_, min_row=2, max_row=1 + cats))  # after the series: it sets theirs
        ch.x_axis.axPos, ch.y_axis.axPos = "l", "b"
        return ch

    def progress_chart(rows_: list, c0_: int, both: bool) -> Any:
        """Baseline planned % (with a baseline) and actual units % of WBS rows, the first one on top; data in
        the hidden columns c0_ to c0_ + 2; data labels on both series when there is room, else on the actual."""
        for j, lab in enumerate(("WBS", "Planned (baseline)", "Actual")):
            put(ws, 1, c0_ + j, lab, size=8)
        for i, w in enumerate(reversed(rows_)):  # Excel draws the first category at the bottom
            put(ws, 2 + i, c0_, wlabel(w), size=8)
            put(ws, 2 + i, c0_ + 1, w["bl_planned_pct"], fmt='0"%"', size=8)
            put(ws, 2 + i, c0_ + 2, w["units_pct"], fmt='0"%"', size=8)
        ch = bar_chart(len(rows_), c0_, ([(c0_ + 1, "A9B8CC")] if bname else []) + [(c0_ + 2, head)])
        for s_ in ch.series[-1:] if not both else ch.series:
            s_.dLbls = values_only()
        axes(ch, "t")
        ch.y_axis.number_format = '0"%"'
        ch.y_axis.scaling.min, ch.y_axis.scaling.max = 0, 110
        ch.y_axis.majorUnit = 20
        return borderless(ch)

    def per_bar(r0: int, r1: int, n: int) -> float:  # pixels of plot height per category, about
        return rows_cm(r0, r1) * 37.8 / max(1, n)

    # panel row 1: S-curve (grid 0-10), finish variance by WBS (12-18), WBS progress (20-26), rows 11 to 27
    box(11, 27, 0, 10)
    panel_title(11, 0, "Planned vs Actual S-Curve")
    per = cv["periods"] if cv else []
    if per:  # the S-curve's own data in hidden columns (the dashboard needs no other sheet)
        ref = cv["baseline"] is not None
        sc0 = hc0 + 9
        for j, lab in enumerate(["Period", "Planned (baseline)" if ref else "Planned (current)", "Actual", "Forecast"]):
            put(ws, 1, sc0 + j, lab, size=8)
        for i, p in enumerate(per):
            a = datetime.strptime(p["start"], "%Y-%m-%d")
            for j, v in enumerate([f"{a:%b-%y}" if cv["period"] == "month" else f"{a:%d-%b-%y}",
                                   p.get("baseline_pct" if ref else "planned_pct"), p.get("actual_pct"),
                                   p.get("forecast_pct")]):
                put(ws, 2 + i, sc0 + j, v, size=8)
        ch = LineChart()
        ch.visible_cells_only = False
        for j, (color, width, dash) in enumerate((("7F8A99", 2.0, "dash"), (accent, 2.75, None), ("E69F00", 2.0, "sysDash"))):
            ch.add_data(Reference(ws, min_col=sc0 + 1 + j, min_row=1, max_row=1 + len(per)), titles_from_data=True)
            line(ch.series[-1], color, width, dash)
        ch.set_categories(Reference(ws, min_col=sc0, min_row=2, max_row=1 + len(per)))
        axes(ch, "t")
        ch.y_axis.number_format = '0"%"'
        ch.y_axis.scaling.min = 0
        if max([v for p in per for v in (p.get("baseline_pct"), p.get("planned_pct"), p.get("actual_pct"),
                                         p.get("forecast_pct")) if v is not None] or [0]) <= 100:
            ch.y_axis.scaling.max = 100
        placed.append((borderless(ch), 12, col(0), (cols_cm(0, 10), rows_cm(12, 27))))
    else:
        merged(ws, 13, col(0), col(10), f"Not drawn: {d['curve_error']}", color=DASH_MUTED, wrap=True)
    box(11, 27, 12, 18)
    panel_title(11, 12, "Finish Variance by WBS")
    fv = [w for w in rd["rows"] if w["depth"] == 1 and w["finish_var_d"] is not None][:DASH_WBS_BARS]
    if fv:  # working days from the latest baseline finish to the latest finish of each level 1 WBS
        put(ws, 1, hc0 + 3, "WBS", size=8)
        put(ws, 1, hc0 + 4, "Finish variance (wd)", size=8)
        tone = [DASH_STATUS["Completed" if w["all_done"] else late(w["finish_var_d"])] for w in reversed(fv)]
        for i, w in enumerate(reversed(fv)):
            put(ws, 2 + i, hc0 + 3, wlabel(w), size=8)
            put(ws, 2 + i, hc0 + 4, w["finish_var_d"], fmt=SIGNED, size=8)
        ch = bar_chart(len(fv), hc0 + 3, [(hc0 + 4, DASH_STATUS["On track"])])
        sv_ = ch.series[0]
        for i, color in enumerate(tone):  # each bar in its status color
            sv_.dPt.append(DataPoint(idx=i, invertIfNegative=False,
                                     spPr=GraphicalProperties(solidFill=color, ln=LineProperties(solidFill=color))))
        sv_.dLbls = values_only()
        axes(ch, None)
        ch.x_axis.tickLblPos = "low"  # the WBS names stay on the left beside a negative bar
        ch.y_axis.number_format = "0"
        placed.append((borderless(ch), 12, col(12), (cols_cm(12, 18), rows_cm(12, 26))))
        keyc = ws.cell(27, col(12))  # the color key, as the bars carry no legend
        keyc.value = rich(*(part for lab in ("Delayed", "At risk", "On track", "Completed") for part in (
            ("● ", DASH_STATUS[lab], True, False, 9),
            (lab + (f" (> {near:g} wd)" if lab == "Delayed" else "") + "    ", DASH_MUTED, False, False, 8))))
        keyc.alignment = Alignment(vertical="center", indent=1)
    else:
        merged(ws, 13, col(12), col(18), "Needs a baseline: no level 1 WBS has a baseline finish." if bname
               else "No baseline: the finish variance needs one.", size=9, color=DASH_MUTED, indent=1, wrap=True)
    box(11, 27, 20, 26)
    l1, n_l1 = pick(1)
    panel_title(11, 20, "WBS Progress" + (f" ({len(l1)} of {n_l1}, by budget units)" if n_l1 > len(l1) else ""))
    if l1:
        placed.append((progress_chart(l1, hc0, per_bar(12, 27, len(l1)) >= 40), 12, col(20),
                       (cols_cm(20, 26), rows_cm(12, 27))))
    else:
        merged(ws, 13, col(20), col(26), "No level 1 WBS with resource units.", size=9, color=DASH_MUTED, indent=1)

    # panel row 2: milestones, level 2 progress (rows 29 to 42); milestone rows hold two lines of a long name
    for rr in range(31, 31 + DASH_PANEL_MILESTONES):
        ws.row_dimensions[rr].height = 27.75
    box(29, 42, 0, 10)
    panel_title(29, 0, "Milestones")
    for a, b, lab in ((0, 4, "Milestone"), (5, 6, "Baseline"), (7, 8, "Forecast"), (9, 10, "Status")):
        merged(ws, 30, col(a), col(b), lab, bold=True, size=9, color=DASH_MUTED, bg="F7F8FA", indent=1 if a == 0 else 0,
               h=None if a == 0 else "center")
    shown_ms = ms if len(ms) <= DASH_PANEL_MILESTONES else sorted(
        ([m for m in ms if m["status_code"] != "TK_Complete"] + [m for m in ms if m["status_code"] == "TK_Complete"])
        [:DASH_PANEL_MILESTONES], key=lambda m: (m["date"] or datetime.max, m["task_code"]))
    for i, m in enumerate(shown_ms):
        rr = 31 + i
        status = "Completed" if m["status_code"] == "TK_Complete" else late(m["variance_d"])
        merged(ws, rr, col(0), col(4), act_label(m["task_code"], m["task_name"]), size=9, indent=1, wrap=True, h="left")
        merged(ws, rr, col(5), col(6), m["baseline"], fmt=DATE, size=9, h="center")
        merged(ws, rr, col(7), col(8), m["date"], fmt=DATE, size=9, h="center")
        merged(ws, rr, col(9), col(10), None)
        ws.cell(rr, col(9)).value = rich(("● ", DASH_PILL.get(status, DASH_PILL["Completed"])[1], True, False, 9),
                                         (status, DASH_INK, False, False, 9))
        for c in range(col(0), col(10) + 1):
            ws.cell(rr, c).border = Border(bottom=Side(style="hair", color=DASH_RULE))
    if not ms:
        merged(ws, 31, col(0), col(10), "No milestones in this project.", size=9, color=DASH_MUTED, indent=1)
    elif len(ms) > len(shown_ms):
        merged(ws, 31 + len(shown_ms), col(0), col(10), f"{len(ms) - len(shown_ms)} more milestones not shown.", size=8,
               color=DASH_MUTED, indent=1)
    box(29, 42, 12, 26)
    l2, n_l2 = pick(2)
    panel_title(29, 12, "Level 2 Progress" + (f" ({len(l2)} of {n_l2}, by budget units)" if n_l2 > len(l2) else ""))
    if l2:
        placed.append((progress_chart(l2, hc0 + 5, per_bar(30, 42, len(l2)) >= 40), 30, col(12),
                       (cols_cm(12, 26), rows_cm(30, 42))))
    else:
        merged(ws, 31, col(12), col(26), "No level 2 WBS with resource units.", size=9, color=DASH_MUTED, indent=1)
    ws.column_dimensions.group(letter(hc0), letter(hc0 + 12), hidden=True)

    # panel row 3: critical and delayed activities (rows 44 to 58)
    box(44, 58, 0, 26)
    flagged = [a for a in open_real if a["task_code"] in crit or late(a["slip_d"]) == "Delayed"]
    flagged.sort(key=lambda a: (a["task_code"] not in crit, (a["tf_d"] or 0) if a["task_code"] in crit
                                else -(a["slip_d"] or 0), a["start"] or datetime.max, a["task_code"]))
    panel_title(44, 0, "Critical & Delayed Activities")
    put(ws, 44, col(26), "Full lists on the Schedule Overview and Critical Path sheets", size=8, color=DASH_MUTED, h="right")
    cols = ((0, 1, "ID"), (2, 3, "WBS"), (4, 11, "Activity Name"), (12, 13, "Start (BL)"), (14, 15, "Finish (BL)"),
            (16, 17, "Start"), (18, 19, "Finish"), (20, 21, "Total Float"), (22, 23, "Slip (wd)"), (24, 26, "Status"))
    for j, (a, b, lab) in enumerate(cols):
        merged(ws, 45, col(a), col(b), lab, bold=True, size=9, color=DASH_MUTED, bg="F7F8FA",
               indent=1 if j == 0 else 0, h=None if j < 3 else "center")
    for i, a in enumerate(flagged[:DASH_TABLE_ROWS]):
        rr = 46 + i
        status = act_status(a)
        vals = (a["task_code"], a["wbs_code"], a["task_name"], a["bl_start"], a["bl_finish"], a["start"], a["finish"],
                a["tf_d"], a["slip_d"], status)
        for j, ((c_a, c_b, _), v) in enumerate(zip(cols, vals, strict=False)):  # j: 7 total float, 8 slip, 9 status
            fmt = (DATE if isinstance(v, datetime) else '0.0" d"' if j == 7 else SIGNED if j == 8 else None)
            merged(ws, rr, col(c_a), col(c_b), v, fmt=fmt, size=9, indent=1 if j < 3 else 0,
                   h=None if j < 3 else "center", bold=j == 9,
                   color=DASH_FLOAT["neg"] if j == 7 and (v or 0) < 0 else DASH_INK)
        pill(ws, rr, col(cols[-1][0]), status)
        for c in range(col(0), col(26) + 1):
            ws.cell(rr, c).border = Border(bottom=Side(style="hair", color=DASH_RULE))
    put(ws, 58, col(0), f"Showing {min(len(flagged), DASH_TABLE_ROWS)} of {len(flagged):,} critical or delayed activities.",
        size=8, color=DASH_MUTED, indent=1)
    put(ws, LAST, col(0), f"Source: {d['db']}, project {d['project']}. Values of the last F9"
        + (f" ({lsd:%d-%b-%Y %H:%M})" if lsd else "") + f". MCP for Primavera P6 server {SERVER_VERSION}.", size=8, color=DASH_MUTED)
    rule = Side(style="thin", color=DASH_RULE)
    for r0, r1, c0_, c1 in frames:  # panel and card frames, after the merges (a merge rewrites its edge borders)
        for rr in range(r0, r1 + 1):
            for c in range(c0_, c1 + 1):
                if rr in (r0, r1) or c in (c0_, c1):
                    old = ws.cell(rr, c).border
                    ws.cell(rr, c).border = Border(left=rule if c == c0_ else old.left, right=rule if c == c1 else old.right,
                                                   top=rule if rr == r0 else old.top, bottom=rule if rr == r1 else old.bottom)
    # fixed charts: two-cell anchors that never move or size with cells (Excel's "Don't move or size with cells"),
    # from the final column widths and row heights. An absolute anchor would keep its points while printing widens
    # the columns, so the charts would print off their panels.
    def marker(r: int, c: int, dx: int, dy: int, sheet=None) -> AnchorMarker:  # dx, dy pixels right of and below (r, c)
        sh = sheet or ws
        while dx >= (w := col_px(sh.column_dimensions[letter(c)].width or 8.43)):
            dx, c = dx - w, c + 1
        while dy >= (h := row_px(sh.row_dimensions[r].height)):
            dy, r = dy - h, r + 1
        return AnchorMarker(col=c - 1, colOff=pixels_to_EMU(dx), row=r - 1, rowOff=pixels_to_EMU(dy))

    for obj, r, c, size in placed:  # 4 px inside the cell corner, so a chart never covers its panel's frame
        w, h = (round(v * 96 / 2.54) for v in size)
        obj.anchor = TwoCellAnchor(editAs="absolute", _from=marker(r, c, 4, 4), to=marker(r, c, 4 + w, 4 + h))
        ws.add_chart(obj)
    ws.protection.sheet = True  # a page: locked cells cannot be selected or changed, objects cannot be moved
    ws.protection.selectLockedCells = True
    ws.protection.objects = True
    page(ws, one_page=True)
    ws.print_area = f"A1:{letter(col(NG - 1))}{LAST}"
    counts["Dashboard"] = 1
    # ---- Trend (trend= only): a row per update, then the finish and progress charts and the milestones
    if trend:
        ws = sheets["Trend"]
        tr = trend["rows"]
        top(ws, "Trend")  # what the columns hold: the Reports notes
        tcols = [("Update", "0", 14), ("Project", None, 22), ("Data Date", DATE, 11), ("Last F9", DATE, 11),
                 ("Finish", DATE, 11), ("Must Finish", DATE, 11), ("vs Must Finish (wd)", SIGNED, 10),
                 ("vs First Update (wd)", SIGNED, 10), ("vs Previous Update (wd)", SIGNED, 10),
                 ("vs Baseline (wd)", SIGNED, 10), ("Units % Complete", PCT, 10), ("Baseline Planned %", PCT, 10),
                 ("SPI (units)", "0.00", 9), ("Critical", "#,##0", 9), ("Near Critical", "#,##0", 9),
                 ("Negative Float", "#,##0", 9), ("Min Total Float (d)", DAYS, 10), ("Median Total Float (d)", DAYS, 10),
                 ("DCMA Points", "0.0", 9), ("Grade", None, 7), ("DCMA Checks Failed", None, 40), ("BEI", "0.00", 8),
                 ("Activities Added", "#,##0", 10), ("Activities Removed", "#,##0", 10), ("Links Added", "#,##0", 9),
                 ("Links Removed", "#,##0", 9), ("Durations Cut", "#,##0", 9)]
        header(ws, 5, 1, [c[0] for c in tcols], [c[2] for c in tcols])
        for i, u in enumerate(tr):
            dc, ch, tfs = u["dcma"] or {}, u["changes"] or {}, u["total_float"]
            vals = [u["update"], u["project"], _db_dt(u["data_date"]), _db_dt(u["last_schedule_date"]), _db_dt(u["finish"]),
                    _db_dt(u["must_finish"]), u["finish_vs_must_finish_wd"], u["finish_variance_first_wd"],
                    u["finish_variance_previous_wd"], u["baseline_finish_variance_wd"], u["units_pct"],
                    u["planned_units_pct"], u["spi_units"], u["critical"], u["near_critical"], u["negative_float"],
                    tfs["min_d"], tfs["median_d"], dc.get("points"), dc.get("grade"),
                    ", ".join(DCMA_LABELS.get(k, k) for k in dc.get("failed", [])), dc.get("bei"),
                    ch.get("activities_added"), ch.get("activities_removed"), ch.get("links_added"),
                    ch.get("links_removed"), ch.get("durations_cut")]
            for j, (v, (_, f, _w)) in enumerate(zip(vals, tcols, strict=False)):
                put(ws, 6 + i, 1 + j, v, fmt=f, size=9, h="center" if j in (0, 19) else None, wrap=j == 20,
                    bold=u["project"] == d["project"] and j == 1)
            ws.row_dimensions[6 + i].height = 12.75 * max(1, math.ceil(len(vals[20]) / 50))  # set: the charts sit below
        last_row = 5 + len(tr)
        ws.freeze_panes = "C6"

        def serial(v: Optional[datetime]) -> Optional[float]:  # an Excel date number
            return None if v is None else (v - datetime(1899, 12, 30)).total_seconds() / 86400

        def tchart(ch, r: int, dx: int, w: int, h: int) -> None:  # fixed on the sheet, w x h pixels
            ch.anchor = TwoCellAnchor(editAs="absolute", _from=marker(r, 1, dx, 4, ws), to=marker(r, 1, dx + w, 4 + h, ws))
            ws.add_chart(ch)
        r0, cw, chh = last_row + 2, 620, 330
        cats = Reference(ws, min_col=3, min_row=6, max_row=last_row)
        fin = LineChart()  # forecast finish (and must finish) per data date
        fin.title = "Forecast finish by data date"
        fin.visible_cells_only = False
        must_any = any(u["must_finish"] for u in tr)
        for c, color, dash in [(5, head, None)] + ([(6, DASH_FLOAT["neg"], "dash")] if must_any else []):
            fin.add_data(Reference(ws, min_col=c, min_row=5, max_row=last_row), titles_from_data=True)
            line(fin.series[-1], color, 2.25, dash)
            fin.series[-1].marker.symbol = "circle"
            fin.series[-1].marker.size = 6
        fin.set_categories(cats)
        axes(fin, "b")
        fin.y_axis.number_format = DATE
        fin.x_axis.number_format = DATE
        ys = [serial(_db_dt(u[k])) for u in tr for k in ("finish", "must_finish") if _db_dt(u[k])]
        if ys:  # a date axis from about a month before the earliest to a month after the latest date
            fin.y_axis.scaling.min, fin.y_axis.scaling.max = math.floor(min(ys)) - 30, math.ceil(max(ys)) + 30
        tchart(fin, r0, 4, cw, chh)
        prog = LineChart()  # units % complete and baseline planned % (left axis), SPI units (right axis)
        prog.title = "Progress and SPI by data date"
        prog.visible_cells_only = False
        for c, color, dash in ((11, accent, None), (12, "7F8A99", "dash")):
            prog.add_data(Reference(ws, min_col=c, min_row=5, max_row=last_row), titles_from_data=True)
            line(prog.series[-1], color, 2.25, dash)
            prog.series[-1].marker.symbol = "circle"
            prog.series[-1].marker.size = 6
        prog.set_categories(cats)
        axes(prog, "b")
        prog.y_axis.number_format = '0"%"'
        prog.y_axis.scaling.min, prog.y_axis.scaling.max = 0, 100
        prog.x_axis.number_format = DATE
        spi = LineChart()
        spi.add_data(Reference(ws, min_col=13, min_row=5, max_row=last_row), titles_from_data=True)
        line(spi.series[-1], "E69F00", 2.25)
        spi.series[-1].marker.symbol = "diamond"
        spi.series[-1].marker.size = 7
        spi.y_axis.axId = 200
        spi.y_axis.number_format = "0.00"
        spi.y_axis.majorGridlines = None
        spi.y_axis.crosses = "max"
        spi.y_axis.delete = False
        prog += spi
        tchart(prog, r0, 4 + cw + 24, cw, chh)
        # milestones followed: their date in every update, then the slip since the first update
        mr = r0 + math.ceil((chh + 8) / row_px(None)) + 2
        section(ws, mr, 1, 3 + len(tr), "Milestones (date in each update, actual else forecast)")
        header(ws, mr + 1, 1, ["Activity ID", "Activity Name"] + [f"Update {u['update']}" for u in tr]
               + ["vs First Update (wd)"])
        for i, m in enumerate(trend["milestones"]):
            put(ws, mr + 2 + i, 1, m["task_code"], size=9)
            put(ws, mr + 2 + i, 2, m["task_name"], size=9, wrap=True)
            ws.row_dimensions[mr + 2 + i].height = 12.75 * max(1, math.ceil(len(m["task_name"] or "") / 26))
            for j, u in enumerate(tr):
                it = u["milestones"][i]
                put(ws, mr + 2 + i, 3 + j, _db_dt(it["date"]), fmt=DATE + ('" A"' if it["date_kind"] == "actual" else ""),
                    size=9, h="center")
            put(ws, mr + 2 + i, 3 + len(tr), tr[-1]["milestones"][i]["variance_first_wd"], fmt=SIGNED, size=9)
        for c in range(3, 4 + len(tr)):  # dates with an " A" suffix
            ws.column_dimensions[letter(c)].width = max(12, ws.column_dimensions[letter(c)].width or 0)
        if not trend["milestones"]:
            put(ws, mr + 2, 1, "No milestone followed in every update.", color=DASH_MUTED)
        page(ws, title_rows="5:5")
        counts["Trend"] = len(tr)
    wb.active = 0
    return wb, counts, card_values


# ----------------------------------------------------------------- look-ahead export (p6_export_lookahead)

LA_COLS = (("Activity ID", 13), ("Activity name", 44), ("Start", 12.5), ("Finish", 12.5), ("Rem. dur. (d)", 8),
           ("Total float (d)", 8), ("Critical", 8), ("Status", 11.5))
LA_BAR = {"critical": "B2182B", "normal": "2B6CB0", "completed": "9CA3AF"}  # bar fills
LA_NONWORK, LA_BAND, LA_DD = "E5E7EB", "DCE6F1", "E67E22"  # non-work day, WBS band, data date marker


class _XlCells:
    """Cell writer for openpyxl sheets with one Font / Alignment / PatternFill / Border object per distinct style
    (openpyxl hashes every new style object it is given). Text from the database is never a formula."""

    def __init__(self):
        from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        self._re, self._mk = ILLEGAL_CHARACTERS_RE, (Alignment, Border, Font, PatternFill, Side)
        self._cache: dict = {}

    def _get(self, key: tuple, make: Callable):
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]

    def fill(self, color: str):
        return self._get(("fill", color), lambda: self._mk[3]("solid", fgColor=color))

    def border(self, left: Optional[tuple] = None, bottom: Optional[tuple] = None):
        """left / bottom: (style, color) or None."""
        side = self._mk[4]
        return self._get(("border", left, bottom), lambda: self._mk[1](
            left=side(style=left[0], color=left[1]) if left else side(),
            bottom=side(style=bottom[0], color=bottom[1]) if bottom else side()))

    def put(self, ws, r: int, c: int, v, fmt: Optional[str] = None, bold: bool = False, size: float = 9,
            color: str = DASH_INK, bg: Optional[str] = None, h: Optional[str] = None, wrap: bool = False):
        if isinstance(v, str):
            v = self._re.sub("", v)[:32000]
        cell = ws.cell(row=r, column=c, value=v)
        if isinstance(v, str) and v.startswith("="):
            cell.data_type = "s"  # text from the database, never a formula
        cell.font = self._get(("font", size, bold, color), lambda: self._mk[2](name="Calibri", size=size, bold=bold,
                                                                               color=color))
        cell.alignment = self._get(("align", h, wrap), lambda: self._mk[0](horizontal=h, vertical="center",
                                                                           wrap_text=wrap))
        if fmt:
            cell.number_format = fmt
        if bg:
            cell.fill = self.fill(bg)
        return cell


def _la_book(d: dict) -> tuple[Any, dict]:
    """The look-ahead workbook of what p6_export_lookahead read: one sheet, the fixed columns LA_COLS then a day or
    week grid of the window with bars. Returns (workbook, {activities, wbs_bands, grid_columns, bars,
    data_date_column})."""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter as letter
    from openpyxl.worksheet.page import PageMargins

    x, frm, to, cal = _XlCells(), d["frm"], d["to"], d["cal"]
    wb = Workbook()
    wb.properties.creator = "MCP for Primavera P6 server"
    wb.properties.title = f"{d['project']} look-ahead"
    ws = wb.active
    ws.title = "Look-ahead"
    nfix, g0 = len(LA_COLS), len(LA_COLS) + 1
    if d["grid"] == "day":
        last = (to - timedelta(microseconds=1)).date()
        cols = [(datetime.combine(frm.date() + timedelta(days=k), time()),
                 datetime.combine(frm.date() + timedelta(days=k + 1), time()))
                for k in range((last - frm.date()).days + 1)]
    else:
        cols = [(_db_dt(w["from"]), _db_dt(w["to"])) for w in d["weeks"]]
    DATE = "dd-mmm-yy"
    head, ink = DASH_HEAD, _ink_on(DASH_HEAD)
    # title, window, legend, source
    x.put(ws, 1, 1, f"Look-ahead of {d['project']}" + (f", {d['name']}" if d["name"] else ""), bold=True, size=14)
    ws.row_dimensions[1].height = 22
    dd_txt = _db_dt(d["data_date"]).strftime("%d-%b-%y %H:%M") if _db_dt(d["data_date"]) else "none"
    lsd = _db_dt(d["last_schedule_date"])
    x.put(ws, 2, 1, f"Data date {dd_txt}. Window {frm:%d-%b-%y %H:%M} to {to:%d-%b-%y %H:%M} ({d['days']} calendar "
          f"days, {d['grid']} grid). Last F9 {lsd.strftime('%d-%b-%y %H:%M') if lsd else 'not recorded'}.", color=DASH_MUTED)
    x.put(ws, 3, 1, "Bars:", bold=True)
    legend = [("Critical", LA_BAR["critical"]), ("Not critical", LA_BAR["normal"]), ("Completed", LA_BAR["completed"]),
              ("Data date", LA_DD), ("Non-work", LA_NONWORK)]
    for i, (text, color) in enumerate(legend):
        x.put(ws, 3, 3 + i, text, bold=True, color=_ink_on(color), bg=color, h="center")
    x.put(ws, 4, 1, f"Source: {d['db']}. Values of the last F9 in P6; non-work days of the project calendar"
          + (f" {d['cal'].name!r}." if cal else " (not shaded: the project calendar is missing or unreadable)."),
          color=DASH_MUTED, size=8)
    # headers: fixed columns over rows 5 and 6, the grid's weeks in row 5 and its days (or weeks) in row 6
    for i, (lab, width) in enumerate(LA_COLS, 1):
        ws.merge_cells(start_row=5, start_column=i, end_row=6, end_column=i)
        x.put(ws, 5, i, lab, bold=True, color=ink, bg=head, h="center", wrap=True)
        x.put(ws, 6, i, None, bg=head)
        ws.column_dimensions[letter(i)].width = width
    ids = [len(str(a["task_code"] or "")) for _, items in d["groups"] for a in items]
    ws.column_dimensions["A"].width = min(32, max(LA_COLS[0][1], 1.1 * max(ids, default=0) + 2))  # the longest ID
    dd = _db_dt(d["data_date"])
    try:  # the line goes before the next work time from the data date (a data date at 18:00 marks the next day)
        mark_at = cal.next_work_start(dd) if cal and dd else dd
    except ValueError:
        mark_at = dd
    dd_col = next((g0 + k for k, (a, b) in enumerate(cols) if mark_at and a <= mark_at < b), None)
    if d["grid"] == "day":
        k = 0
        while k < len(cols):  # one merged label per window week
            wk = min((cols[k][0].date() - frm.date()).days // 7, len(d["weeks"]) - 1)
            j = k
            while j + 1 < len(cols) and min((cols[j + 1][0].date() - frm.date()).days // 7, len(d["weeks"]) - 1) == wk:
                j += 1
            if j > k:
                ws.merge_cells(start_row=5, start_column=g0 + k, end_row=5, end_column=g0 + j)
            x.put(ws, 5, g0 + k, f"Week {wk + 1}" + (f", {cols[k][0]:%d-%b}" if j - k >= 3 else ""), bold=True,
                  color=ink, bg=head, h="center")
            for c in range(k + 1, j + 1):
                x.put(ws, 5, g0 + c, None, bg=head)
            k = j + 1
        for k, (a, _) in enumerate(cols):
            off = cal is not None and not cal.is_workday(a.date())
            on_dd = g0 + k == dd_col
            x.put(ws, 6, g0 + k, f"{'MTWTFSS'[a.weekday()]}\n{a.day}", bold=on_dd, size=8,
                  color="FFFFFF" if on_dd else DASH_INK, bg=LA_DD if on_dd else LA_NONWORK if off else "F3F4F6",
                  h="center", wrap=True)
            ws.column_dimensions[letter(g0 + k)].width = 3.6
    else:
        for k, (a, _) in enumerate(cols):
            on_dd = g0 + k == dd_col
            x.put(ws, 5, g0 + k, f"Week {k + 1}", bold=True, color=ink, bg=head, h="center")
            x.put(ws, 6, g0 + k, f"{a:%d-%b}", bold=on_dd, size=8, color="FFFFFF" if on_dd else DASH_INK,
                  bg=LA_DD if on_dd else "F3F4F6", h="center")
            ws.column_dimensions[letter(g0 + k)].width = 7
    ws.row_dimensions[6].height = 24
    nonwork = [d["grid"] == "day" and cal is not None and not cal.is_workday(a.date()) for a, _ in cols]

    r, bars, acts, bands = 7, 0, 0, 0
    for band, items in d["groups"]:
        x.put(ws, r, 1, band, bold=True, bg=LA_BAND)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=nfix)
        for c in range(2, nfix + len(cols) + 1):
            x.put(ws, r, c, None, bg=LA_BAND)
        r, bands = r + 1, bands + 1
        for a in items:
            s, f = _db_dt(a["fcst_start"]), _db_dt(a["fcst_finish"])
            done = a["status_code"] == "TK_Complete"
            x.put(ws, r, 1, a["task_code"])
            x.put(ws, r, 2, a["task_name"])
            x.put(ws, r, 3, s, fmt=DATE + ('" A"' if a["act_start_date"] else ""), h="center")
            x.put(ws, r, 4, f, fmt=DATE + ('" A"' if done and a["act_end_date"] else ""), h="center")
            x.put(ws, r, 5, a["rem_dur_d"], fmt="0.0", h="right")
            x.put(ws, r, 6, None if done else a["total_float_d"], fmt="0.0", h="right")
            x.put(ws, r, 7, "Yes" if a["critical"] else None, bold=True, color=LA_BAR["critical"], h="center")
            x.put(ws, r, 8, STATUS_NAMES.get(a["status_code"], a["status_code"]), h="center")
            color = LA_BAR["completed"] if done else LA_BAR["critical"] if a["critical"] else LA_BAR["normal"]
            mile = a["task_type"] in ("TT_Mile", "TT_FinMile")
            for k, (c0, c1) in enumerate(cols):
                on = s is not None and f is not None and (
                    (c0 <= s < c1) if (mile or s == f) else (s < c1 and f > c0))
                if on:
                    bars += 1
                    x.put(ws, r, g0 + k, "◆" if mile else None, color=color if mile else "FFFFFF",
                          bg=None if mile else color, h="center", size=8)
                elif nonwork[k]:
                    x.put(ws, r, g0 + k, None, bg=LA_NONWORK)
            r, acts = r + 1, acts + 1
    if dd_col is not None:  # the data date: a line on the left edge of its column, header to the last row
        for rr in range(5, r):
            ws.cell(rr, dd_col).border = x.border(left=("medium", LA_DD))
    ws.freeze_panes = ws.cell(7, g0)
    ws.print_title_rows = "5:6"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A3
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.page_margins = PageMargins(left=0.4, right=0.4, top=0.5, bottom=0.6)
    ws.oddFooter.center.text = "Page &P of &N"
    ws.oddFooter.left.text = f"{d['project']} look-ahead".replace("&", "&&")  # & starts a header code
    ws.print_area = f"A1:{letter(nfix + len(cols))}{max(r - 1, 6)}"
    return wb, {"activities": acts, "wbs_bands": bands, "grid_columns": len(cols), "bars": bars,
                "data_date_column": letter(dd_col) if dd_col else None}
