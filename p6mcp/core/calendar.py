"""Calendars: CALENDAR.clndr_data parsing, _Cal working time arithmetic, the lag calendar of a link."""

from __future__ import annotations

import bisect
import functools
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Optional

from p6mcp.core.db import _alive, _fmt_dt, _parse_dt


CAL_EPOCH = date(1899, 12, 30)  # P6 calendar exception dates are day serials from this date
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # Python weekday order
CAL_SCAN_DAYS = 7320  # add_work_hours gives up after 20 years without working time
CAL_WINDOW_DAYS = 3660


# ----------------------------------------------------------------- calendars

def _hm(v: Optional[str]) -> int:
    m = re.fullmatch(r"(\d{1,2}):(\d\d)", (v or "").strip())
    if not m:
        raise ValueError(f"bad shift time {v!r}")
    return int(m[1]) * 60 + int(m[2])


def _shifts_of(node: dict) -> tuple:
    """Shift children (s|HH:MM|f|HH:MM, either order) -> sorted ((start, end), ...) in minutes."""
    out = []
    for k in node["kids"]:
        s, f = _hm(k["attrs"].get("s")), _hm(k["attrs"].get("f"))
        f = 1440 if f == 0 else f  # f|00:00 is 24:00
        if f <= s:
            raise ValueError(f"shift ends before it starts: {k['attrs']}")
        out.append((s, f))
    return tuple(sorted(out))


@functools.lru_cache(maxsize=256)
def _cal_parse(data: str) -> tuple[Optional[tuple], tuple]:
    """CALENDAR.clndr_data -> (week, exceptions), immutable because cached. week: shifts per Python
    weekday (P6 day '1' is Sunday), None without a DaysOfWeek node. exceptions: ((date, shifts), ...),
    serial dates from 1899-12-30, no shifts = non-work day."""
    from p6mcp.server import _p6_find, _p6_tree
    tree = _p6_tree(data)
    try:
        week, node = None, _p6_find(tree, "DaysOfWeek")
        if node is not None:
            w: list[tuple] = [()] * 7
            for d in node["kids"]:
                n = int(d["name"])
                if not 1 <= n <= 7:
                    raise ValueError(f"day {n}")
                w[(n + 5) % 7] = _shifts_of(d)
            week = tuple(w)
        node = _p6_find(tree, "Exceptions")
        exc = tuple((CAL_EPOCH + timedelta(days=int(float(e["attrs"]["d"]))), _shifts_of(e))
                    for e in (node["kids"] if node else ()))
    except (KeyError, TypeError, ValueError, OverflowError) as e:
        raise ValueError(f"unreadable calendar data: {e}") from None
    return week, exc


class _Cal:
    """Working time of one calendar (minutes from midnight), base calendar already merged."""
    __slots__ = ("clndr_id", "name", "hpd", "week", "exc", "inherited", "week_from_base", "error", "_cum")

    def __init__(self, clndr_id: int, name: str, hpd: float, week: Optional[tuple], exc: dict,
                 inherited: frozenset = frozenset(), week_from_base: bool = False, error: Optional[str] = None):
        self.clndr_id, self.name, self.hpd, self.week = clndr_id, name, hpd, week or ((),) * 7
        self.exc, self.inherited, self.week_from_base, self.error = exc, inherited, week_from_base, error
        self._cum: Optional[tuple] = None  # (first day ordinal, cumulative working seconds) for work_hours

    def shifts(self, d: date) -> tuple:
        return self.exc[d] if d in self.exc else self.week[d.weekday()]

    def is_workday(self, d: date) -> bool:
        return bool(self.shifts(d))

    def day_hours(self, d: date) -> float:
        return sum(f - s for s, f in self.shifts(d)) / 60

    def first_start(self, d: date) -> Optional[datetime]:
        sh = self.shifts(d)
        return datetime.combine(d, time()) + timedelta(minutes=sh[0][0]) if sh else None

    def last_end(self, d: date) -> Optional[datetime]:
        sh = self.shifts(d)  # 24:00 gives the next day at 00:00
        return datetime.combine(d, time()) + timedelta(minutes=sh[-1][1]) if sh else None

    def _day_secs(self, d: date, a: Optional[datetime] = None, b: Optional[datetime] = None) -> float:
        """Working seconds of day d between a and b (None: the day's edges)."""
        day0 = datetime.combine(d, time())
        secs = 0.0
        for s, f in self.shifts(d):
            lo, hi = day0 + timedelta(minutes=s), day0 + timedelta(minutes=f)
            lo, hi = (max(a, lo) if a else lo), (min(b, hi) if b else hi)
            if hi > lo:
                secs += (hi - lo).total_seconds()
        return secs

    def _cum_secs(self, d: date) -> float:
        """Working seconds of the days before d, from an index built on demand (CAL_WINDOW_DAYS margin)."""
        o = d.toordinal()
        c0, cum = self._cum if self._cum else (o, [])
        if not c0 <= o < c0 + len(cum):  # cum[i] = seconds of the days c0 .. c0 + i - 1
            lo, hi = min(o, c0) - CAL_WINDOW_DAYS // 10, max(o, c0 + len(cum)) + CAL_WINDOW_DAYS // 10
            cum = [0.0]
            for k in range(lo, hi + 1):
                cum.append(cum[-1] + self._day_secs(date.fromordinal(k)))
            self._cum, c0 = (lo, cum), lo
        return cum[o - c0]

    def work_hours(self, a: datetime, b: datetime) -> float:
        """Working hours between a and b (negative when b is before a)."""
        if b < a:
            return -self.work_hours(b, a)
        da, db = a.date(), b.date()
        if da == db:
            return self._day_secs(da, a, b) / 3600
        self._cum_secs(da)  # both ends in one index: a rebuild changes its origin
        mid = self._cum_secs(db) - self._cum_secs(da + timedelta(days=1))
        return (self._day_secs(da, a, None) + mid + self._day_secs(db, None, b)) / 3600

    def work_days(self, a: datetime, b: datetime) -> float:
        return self.work_hours(a, b) / self.hpd

    def count_workdays(self, d1: date, d2: date) -> int:
        """Work days from d1 to d2, both included."""
        return sum(1 for i in range((d2 - d1).days + 1) if self.is_workday(d1 + timedelta(days=i)))

    def add_work_hours(self, start: datetime, hours: float) -> datetime:
        """End of `hours` of work begun at start; lands on a shift end when it fills it (P6 finish). The start day
        is walked shift by shift from start, the following whole days are skipped through the cumulative index
        (_cum_secs: same result as walking them), the last day is walked shift by shift."""
        if hours < 0:
            raise ValueError("hours must be 0 or more")
        left = round(hours * 60)
        if not left:
            return start
        d, t0 = start.date(), (start - datetime.combine(start.date(), time())).total_seconds() / 60
        for s, f in self.shifts(d):
            lo = max(s, t0)
            if f > lo:
                if left <= f - lo:
                    return datetime.combine(d, time()) + timedelta(minutes=lo + left)
                left -= f - lo
        o, span, need = d.toordinal() + 1, 32, left * 60  # working seconds still to place, from the next day
        while True:
            self._cum_secs(date.fromordinal(o))  # both ends in one index (a rebuild keeps the earlier range)
            self._cum_secs(date.fromordinal(o + span))
            c0, cum = self._cum
            base = cum[o - c0]
            if cum[o + span - c0] - base >= need:
                break
            if span >= CAL_SCAN_DAYS:
                raise ValueError(f"calendar {self.name!r} has no working time within {CAL_SCAN_DAYS} days")
            span = min(span * 4, CAL_SCAN_DAYS)
        i = bisect.bisect_left(cum, base + need, o - c0 + 1, o + span - c0 + 1)  # the day that fills it
        day, left = date.fromordinal(c0 + i - 1), (base + need - cum[i - 1]) / 60
        for s, f in self.shifts(day):
            if left <= f - s:
                return datetime.combine(day, time()) + timedelta(minutes=s + left)
            left -= f - s
        raise AssertionError("cumulative index out of step with the shifts")  # cannot happen

    def subtract_work_hours(self, end: datetime, hours: float) -> datetime:
        """Start of `hours` of work that finish at end; lands on a shift start when it fills it
        (backward pass mirror of add_work_hours)."""
        if hours < 0:
            raise ValueError("hours must be 0 or more")
        left = round(hours * 60)
        if not left:
            return end
        d, t1 = end.date(), (end - datetime.combine(end.date(), time())).total_seconds() / 60
        for _ in range(CAL_SCAN_DAYS):
            for s, f in reversed(self.shifts(d)):
                hi = min(f, t1)
                if hi > s:
                    if left <= hi - s:
                        return datetime.combine(d, time()) + timedelta(minutes=hi - left)
                    left -= hi - s
            d, t1 = d - timedelta(days=1), 1440
        raise ValueError(f"calendar {self.name!r} has no working time within {CAL_SCAN_DAYS} days")

    def work_hours_between(self, a: datetime, b: datetime) -> float:
        """Working hours from a to b (negative when b is before a); same as work_hours."""
        return self.work_hours(a, b)

    def is_work_time(self, dt: datetime) -> bool:
        """dt falls inside a shift: [start, finish), so a shift finish is not work time."""
        m = (dt - datetime.combine(dt.date(), time())).total_seconds() / 60
        return any(s <= m < f for s, f in self.shifts(dt.date()))

    def next_work_start(self, dt: datetime) -> datetime:
        """dt when it is work time, else the next shift start after it."""
        d, m = dt.date(), (dt - datetime.combine(dt.date(), time())).total_seconds() / 60
        for _ in range(CAL_SCAN_DAYS):
            for s, f in self.shifts(d):
                if m < f:
                    return dt if s <= m else datetime.combine(d, time()) + timedelta(minutes=s)
            d, m = d + timedelta(days=1), -1
        raise ValueError(f"calendar {self.name!r} has no working time within {CAL_SCAN_DAYS} days")

    def prev_work_end(self, dt: datetime) -> datetime:
        """dt when work runs just before it (inside a shift, or at its finish), else the last shift
        finish before it: a shift start gives the previous finish (backward mirror of next_work_start)."""
        d, m = dt.date(), (dt - datetime.combine(dt.date(), time())).total_seconds() / 60
        for _ in range(CAL_SCAN_DAYS):
            for s, f in reversed(self.shifts(d)):
                if m > s:
                    return dt if m <= f else datetime.combine(d, time()) + timedelta(minutes=f)
            d, m = d - timedelta(days=1), 1441
        raise ValueError(f"calendar {self.name!r} has no working time within {CAL_SCAN_DAYS} days")


DEFAULT_WEEK = (((480, 960),),) * 5 + ((),) * 2  # Monday to Friday 08:00 to 16:00 (Python weekday order)


def _calendars(con, only: Optional[int] = None) -> dict[int, _Cal]:
    """Live calendars by clndr_id (only= one calendar and its bases). A project or global calendar without a
    DaysOfWeek node works DEFAULT_WEEK, with or without a base (P6 25.12, parity case calendars/cal-base); a
    resource calendar without one takes its base calendar's week (P6 stores the base's week hours on it).
    A base calendar gives its exceptions (the calendar's own dates win). An unreadable clndr_data gives a
    calendar without working time (nothing taken from its base) and .error set."""
    from p6mcp.server import _blob_text
    has_type = any(r[1].lower() == "clndr_type" for r in con.execute("PRAGMA table_info(CALENDAR)"))
    typ = "clndr_type" if has_type else "NULL AS clndr_type"
    sql = (f"SELECT clndr_id, clndr_name, base_clndr_id, day_hr_cnt, clndr_data, {typ} FROM CALENDAR WHERE "
           + ("clndr_id = ?" if only is not None else "1=1") + _alive(con, "CALENDAR"))
    if only is None:
        rows = {r["clndr_id"]: r for r in con.execute(sql)}
    else:
        rows, cid = {}, only
        while cid is not None and cid not in rows and len(rows) <= 10:
            r = con.execute(sql, (cid,)).fetchone()
            if r is None:
                break
            rows[cid], cid = r, r["base_clndr_id"]
    out: dict[int, _Cal] = {}

    def build(cid: int, depth: int = 0) -> _Cal:
        if cid in out:
            return out[cid]
        r = rows[cid]
        try:
            (week, exc), err = _cal_parse(_blob_text(r["clndr_data"])), None
        except ValueError as e:
            week, exc, err = None, (), str(e)
        own, inherited, from_base = dict(exc), frozenset(), False
        if err is None and r["base_clndr_id"] in rows and r["base_clndr_id"] != cid and depth < 10:
            b = build(r["base_clndr_id"], depth + 1)
            if week is None and r["clndr_type"] == "CA_Rsrc":
                week, from_base = b.week, True
            inherited = frozenset(set(b.exc) - set(own))
            own = {**b.exc, **own}
        if week is None and err is None and r["clndr_type"] != "CA_Rsrc":
            week = DEFAULT_WEEK
        out[cid] = _Cal(cid, r["clndr_name"], float(r["day_hr_cnt"] or 8), week, own, inherited, from_base, err)
        return out[cid]

    for cid in rows:
        build(cid)
    return out


def _calendar(con, clndr_id: int) -> _Cal:
    cal = _calendars(con, int(clndr_id)).get(int(clndr_id))
    if cal is None:
        raise ValueError(f"Calendar not found: {clndr_id}")
    return cal


def _resolve_dt(cal: Optional[_Cal], value: str, field: str, edge: str) -> tuple[str, list[str]]:
    """User date of an activity -> (DB date string, notes). An explicit time is kept (noted when the
    day is not a work day of cal). A date alone takes the first shift start (edge 'start') or the
    last shift end (edge 'finish', 24:00 = next day 00:00) of that day, and is refused on a non-work
    day. Without a readable calendar a date alone starts at 08:00 and a finish needs a time."""
    dt, has_time = _parse_dt(value, field)
    d = dt.date()
    if has_time:
        if cal is not None and not cal.is_workday(d):
            return _fmt_dt(dt), [f"{field} {_fmt_dt(dt)} is on a non-work day of calendar {cal.name!r} (kept)"]
        return _fmt_dt(dt), []
    if cal is None:
        if edge == "finish":
            raise ValueError(f"{field}: give the time ('YYYY-MM-DD HH:MM'): the activity has no readable calendar")
        return _fmt_dt(dt.replace(hour=8)), [f"{field}: date only and no readable calendar, 08:00 used"]
    if not cal.is_workday(d):
        raise ValueError(f"{field}: {d} is not a work day on calendar {cal.name!r}: give the time ('YYYY-MM-DD HH:MM')")
    v = cal.first_start(d) if edge == "start" else cal.last_end(d)
    return _fmt_dt(v), [f"{field}: date only, {'first shift start' if edge == 'start' else 'last shift end'} "
                        f"of calendar {cal.name!r} used ({_fmt_dt(v)})"]


def _shift_txt(shifts: tuple) -> list[str]:
    return [f"{s // 60:02d}:{s % 60:02d}-{f // 60:02d}:{f % 60:02d}" for s, f in shifts]


def _cal_row(r: sqlite3.Row, cal: _Cal, win: Optional[tuple], span: Optional[tuple], exc_cap: Optional[int]) -> dict:
    """p6_list_calendars row: stored values, the parsed week, exceptions and warnings; exc_cap = how
    many exception dates to list (None: no list)."""
    hpd = float(r["day_hr_cnt"] or 8)
    hours = [sum(f - s for s, f in sh) / 60 for sh in cal.week]
    week_h = sum(hours)
    warn = [] if r["day_hr_cnt"] else ["day_hr_cnt is empty, 8 h per day used"]
    if r["base_clndr_id"] is not None and r["base_name"] is None:
        warn.append(f"base calendar {r['base_clndr_id']} not found")
    if not week_h:
        warn.append("no working time in the work week")
    if r["week_hr_cnt"] is not None and abs(float(r["week_hr_cnt"]) - week_h) > 1e-6:
        warn.append(f"stored week_hr_cnt {float(r['week_hr_cnt']):g} differs from the work week {week_h:g} h")
    warn += [f"a work day has {h:g} h, hours_per_day is {hpd:g}" for h in sorted({h for h in hours if h})
             if abs(h - hpd) > 1e-6]
    non_work = sum(1 for v in cal.exc.values() if not v)
    row = {"clndr_id": r["clndr_id"], "name": r["clndr_name"], "type": r["clndr_type"],
           "project": r["cal_project"], "base_calendar": r["base_name"],
           "project_default": None if r["project_default"] is None else bool(r["project_default"]),
           "activities": r["activities"], "hours_per_day": hpd, "week_hours_stored": r["week_hr_cnt"],
           "work_week": {WEEKDAYS[(n + 5) % 7]: _shift_txt(cal.week[(n + 5) % 7]) for n in range(1, 8)},
           "week_hours": week_h, "week_inherited": cal.week_from_base,
           "exceptions": {"total": len(cal.exc), "inherited": len(cal.inherited), "non_work": non_work,
                          "changed_hours": len(cal.exc) - non_work}}
    if win:
        wh = cal.work_hours(datetime.combine(win[0], time()), datetime.combine(win[1] + timedelta(days=1), time()))
        row["window"] = {"from": win[0].isoformat(), "to": win[1].isoformat(), "work_hours": round(wh, 4),
                         "work_days": round(wh / hpd, 2), "workdays": cal.count_workdays(*win)}
    if exc_cap is not None:
        lo, hi = win or span or (None, None)
        dates = sorted(d for d in cal.exc if (lo is None or d >= lo) and (hi is None or d <= hi))
        row["exception_dates"] = [{"date": d.isoformat(), "work": _shift_txt(cal.exc[d]),
                                   "inherited": d in cal.inherited} for d in dates[:exc_cap]]
        row["exception_dates_truncated"] = len(dates) > exc_cap
    if cal.error:
        row["error"] = cal.error
    row["warnings"] = warn
    return row


def _lag_cal_of(rule: str, p_cal, s_cal, proj_cal) -> Optional[_Cal]:
    """Calendar a lag is counted on under the lag rule (None: 24-hour days; default: the predecessor's)."""
    return {"rcal_Successor": s_cal, "rcal_24Hour": None, "rcal_ProjDefault": proj_cal}.get(rule, p_cal)


def _lag_hpd_of(rule: str, p_hpd, s_hpd, proj_cal) -> float:
    """Hours per lag day under the lag rule, from loaded calendars (_lag_hpd reads the database)."""
    return {"rcal_Successor": s_hpd, "rcal_24Hour": 24.0,
            "rcal_ProjDefault": proj_cal.hpd if proj_cal else 8.0}.get(rule, p_hpd) or 8.0
