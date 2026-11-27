"""P6 text trees (CALENDAR.clndr_data, PROJPROP values), the schedule options and check settings of
PROJPROP, the lag rule, and the schedule options of XER files."""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from p6mcp.core.constants import HPD_SQL, LAG_RULES
from p6mcp.core.db import _alive, _cols, _dict, _insert, _stamp_cols, _update


def _blob_text(v) -> str:
    """TEXT or BLOB column -> str (BLOBs end with a NUL; utf-8, else cp1252)."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    b = bytes(v).rstrip(b"\x00")
    try:
        return b.decode("utf-8-sig")
    except UnicodeDecodeError:
        return b.decode("cp1252", errors="replace")


def _p6_tree(text: str, strict: bool = False) -> dict:
    """Parse P6 '(0||name(k|v|...)(children))' text (CALENDAR.clndr_data, PROJPROP values)
    into {'name', 'attrs', 'kids', 'num'}. The name may be empty. strict (before a rewrite with
    _p6_dump): a key repeated in one node is refused instead of keeping its last value."""
    s = re.sub(r"\s*[\r\n]+\s*", "", text).strip()
    pos = 0

    def eat(ch: str) -> None:
        nonlocal pos
        if s[pos] != ch:
            raise IndexError
        pos += 1

    def node() -> dict:
        nonlocal pos
        eat("(")
        start = pos
        while s[pos].isdigit():
            pos += 1
        num = s[start:pos]
        eat("|"); eat("|")
        j = s.index("(", pos)
        name, pos = s[pos:j], j + 1
        j = s.index(")", pos)
        parts = s[pos:j].split("|") if j > pos else []
        if len(parts) % 2:
            raise IndexError
        pos = j + 1
        eat("(")
        kids = []
        while s[pos] != ")":
            kids.append(node())
        pos += 1
        eat(")")
        keys = parts[::2]
        if strict and len(set(keys)) != len(keys):  # a dict keeps one: never rewrite such a node
            raise IndexError
        return {"name": name, "attrs": dict(zip(keys, parts[1::2])), "kids": kids, "num": num}

    try:
        root = node()
        if pos != len(s):
            raise IndexError
    except (IndexError, ValueError):
        raise ValueError(f"unreadable P6 data at char {pos}") from None
    return root


def _p6_dump(node: Mapping) -> str:
    """Inverse of _p6_tree (no line breaks): '(n||name(k|v|...)(kids))', keys in their order."""
    attrs = "|".join(f"{k}|{v}" for k, v in node["attrs"].items())
    return f"({node.get('num') or '0'}||{node['name']}({attrs})({''.join(_p6_dump(k) for k in node['kids'])}))"


def _p6_find(node: Optional[dict], name: str) -> Optional[dict]:
    """First node called name, depth first (the node itself included)."""
    if not node:
        return None
    if node["name"] == name:
        return node
    for k in node["kids"]:
        hit = _p6_find(k, name)
        if hit:
            return hit
    return None


def _props(con, pid: int, prop_name: str) -> Optional[dict]:
    """Parsed PROJPROP value of a project (e.g. 'scheduling', 'schedule_check'). None when the
    project has no such row; an unreadable value gives an empty tree with 'error' set."""
    r = con.execute("SELECT prop_value FROM PROJPROP WHERE proj_id = ? AND prop_name = ?"
                    + _alive(con, "PROJPROP"), (pid, prop_name)).fetchone()
    if r is None:
        return None
    try:
        return _p6_tree(_blob_text(r[0]))
    except ValueError as e:
        return {"name": None, "attrs": {}, "kids": [], "error": f"PROJPROP {prop_name}: {e}"}


# ----------------------------------------------------------------- schedule options and lag rule

def _sched_options(con, pid: int) -> tuple[dict[str, str], str]:
    """(options, source) from the project's PROJPROP 'scheduling' row, keys lower case. The level
    priority list is flattened for display only."""
    tree = _props(con, pid, "scheduling")
    if tree is None:
        return {}, "none stored for this project"
    if tree.get("error"):
        return {}, f"unreadable: {tree['error']}"
    opts = {k.lower(): v for k, v in tree["attrs"].items()}
    lpl = _p6_find(tree, "LevelPriorityList")
    if lpl is not None:
        opts["levelprioritylist"] = _lpl_text(_lpl_of(lpl))
    return opts, "PROJPROP scheduling"


def _lag_rule(con, pid: int) -> tuple[str, str]:
    """(rule, source): the project's sched_calendar_on_relationship_lag, else rcal_Predecessor."""
    opts, src = _sched_options(con, pid)
    v = opts.get("sched_calendar_on_relationship_lag")
    if v in LAG_RULES:
        return v, "project schedule options (PROJPROP scheduling)"
    if v is None:
        return "rcal_Predecessor", f"P6 default ({src})"
    return "rcal_Predecessor", f"P6 default (unrecognised value {v!r})"


def _lag_info(con, pid: int) -> dict:
    rule, src = _lag_rule(con, pid)
    return {"rule": rule, "source": src, "basis": LAG_RULES[rule]}


def _lag_hpd(con, pred, succ, proj_id: Optional[int] = None, rule: Optional[str] = None) -> tuple[float, str]:
    """(hours per lag day, basis) under the lag rule of proj_id (default: the successor's project,
    which also decides cross-project links). pred and succ carry 'hpd'; rule= overrides the lookup."""
    pid = succ["proj_id"] if proj_id is None else proj_id
    rule = rule or _lag_rule(con, pid)[0]
    if rule == "rcal_Successor":
        h = succ["hpd"]
    elif rule == "rcal_24Hour":
        h = 24
    elif rule == "rcal_ProjDefault":
        r = con.execute(f"SELECT {HPD_SQL} FROM PROJECT p LEFT JOIN CALENDAR c ON c.clndr_id = p.clndr_id "
                        "WHERE p.proj_id = ?" + _alive(con, "PROJECT", "p"), (pid,)).fetchone()
        h = r[0] if r else None
    else:
        rule, h = "rcal_Predecessor", pred["hpd"]
    return float(h or 8), LAG_RULES[rule]


def _lag_hpd_sql(con, pid: int) -> tuple[str, str]:
    """(SQL hours per lag day, basis) under the lag rule of pid, for links with the predecessor's
    calendar as cp and the successor's as cs."""
    rule = _lag_rule(con, pid)[0]
    if rule in ("rcal_Predecessor", "rcal_Successor"):
        return f"COALESCE(NULLIF({'cp' if rule == 'rcal_Predecessor' else 'cs'}.day_hr_cnt, 0), 8)", LAG_RULES[rule]
    h, basis = _lag_hpd(con, {"hpd": None}, {"hpd": None}, pid, rule)
    return repr(h), basis


def _xer_project(x: dict, xer_project: Optional[str]) -> dict:
    projects = [p for p in x.get("PROJECT", []) if xer_project in (None, p["proj_short_name"])]
    if len(projects) != 1:
        raise ValueError("Choose the project with xer_project=: "
                         f"{[p['proj_short_name'] for p in x.get('PROJECT', [])]}")
    return projects[0]


def _xer_sched(x: dict, xp: dict) -> dict[str, Optional[str]]:
    """SCHEDOPTIONS of XER project xp (or the only row, when it has no proj_id) without its ids; {}
    when absent. Another project's row is never used."""
    out = {k: v for k, v in _xer_sched_row(x, xp).items() if k not in ("schedoptions_id", "proj_id")}
    if out.get("levelprioritylist"):
        lpl = _xer_lpl(out["levelprioritylist"])
        out["levelprioritylist"] = out["levelprioritylist"].strip() if lpl is None else _lpl_text(lpl)
    return out


def _xer_sched_row(x: dict, xp: dict) -> dict:
    """The SCHEDOPTIONS row of XER project xp, or the only row when it has no proj_id; {} otherwise."""
    rows = x.get("SCHEDOPTIONS", [])
    return next((r for r in rows if r.get("proj_id") == xp["proj_id"]),
                rows[0] if len(rows) == 1 and rows[0].get("proj_id") is None else None) or {}


def _opt_eq(a, b) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def _sched_diff(db_opts: dict, xer_opts: dict, db_code: Callable[[str], Optional[str]],
                xer_code: Callable[[str], Optional[str]]) -> list[dict]:
    """Options side by side: different first, then only in the XER, only in the database, equal, and
    the level priority list last ('field:ORDER' on both sides, compared when both are readable).
    Numbers compare as floats; the key activity id is shown as an Activity ID ('-99' = none; an id
    the XER does not contain resolves to none, as P6 cannot use it either)."""
    def label(v, code, where):
        if v is None or str(v).strip() in ("", "-99"):
            return None if v is None else "(none)"
        return code(str(v).strip()) or f"{v} (not in this {where}: none)"

    def none_like(v):
        return v is not None and (v == "(none)" or v.endswith(": none)"))

    out = []
    for k in sorted(set(db_opts) | set(xer_opts)):
        d, x = db_opts.get(k), xer_opts.get(k)
        if k == "key_activity_for_multiple_longest_paths":  # present but empty (an XER cell) = none
            d = label("" if d is None and k in db_opts else d, db_code, "database")
            x = label("" if x is None and k in xer_opts else x, xer_code, "XER")
        if k == "levelprioritylist":
            readable = all(v is not None and _xer_lpl(v, ":") is not None for v in (d, x))
            rank, same = 4, (d == x if readable else None)
        elif k == "key_activity_for_multiple_longest_paths" and d is not None and x is not None:
            same = d == x or (none_like(d) and none_like(x))
            rank = 3 if same else 0
        elif k not in db_opts or k not in xer_opts:
            rank, same = (1 if k in xer_opts else 2), None
        else:
            same = _opt_eq(d, x)
            rank = 3 if same else 0
        out.append((rank, {"option": k, "db": d, "xer": x, "same": same}))
    return [o for _, o in sorted(out, key=lambda t: t[0])]


def _db_task_code(con, task_id: str) -> Optional[str]:
    try:
        r = con.execute("SELECT task_code FROM TASK WHERE task_id = ?" + _alive(con, "TASK"), (int(task_id),)).fetchone()
    except ValueError:
        return None
    return r[0] if r else None


# PROJPROP option rows written by p6_set_schedule_options and p6_import_xer: key -> (kind, P6 default),
# in P6's key order. Kinds: flag (Y/N), int, float (written as P6 does: 25.0), num (int when whole),
# task (key activity: a task_id, -99 = none) or a tuple of allowed values. Keys P6 stores that are not
# listed here are kept as they are. Defaults (a project without the row) follow the P6 template rows.
SCHED_SPEC: dict[str, tuple[Any, str]] = {
    "enable_multiple_longest_path_calc": ("flag", "N"),
    "key_activity_for_multiple_longest_paths": ("task", "-99"),
    "max_multiple_longest_path": ("int", "10"),
    # P6 25.12 codes (parity cases basics/float-type-*): finish float, start float, smallest of the two
    "sched_float_type": (("FT_FF", "FT_SS", "FT_Min"), "FT_FF"),
    "level_outer_assign_priority": ("int", "5"),
    "use_total_float_multiple_longest_paths": ("flag", "Y"),
    "sched_calendar_on_relationship_lag": (tuple(LAG_RULES), "rcal_Predecessor"),
    "limit_multiple_longest_path_calc": ("flag", "Y"),
    "sched_open_critical_flag": ("flag", "N"),
    "sched_lag_early_start_flag": ("flag", "Y"),
    "sched_progress_override": ("flag", "N"),
    "sched_retained_logic": ("flag", "Y"),
    "level_over_alloc_pct": ("float", "25.0"),
    "sched_outer_depend_type": (("SD_Both", "SD_None"), "SD_Both"),
    "sched_use_expect_end_flag": ("flag", "Y"),
    "level_within_float_flag": ("flag", "N"),
    "level_all_rsrc_flag": ("flag", "Y"),
    "level_keep_sched_date_flag": ("flag", "Y"),
    "level_float_thrs_cnt": ("num", "0"),
    "level_outer_assign_flag": ("flag", "N"),
    "sched_use_project_end_date_for_float": ("flag", "Y"),
    "sched_setplantoforecast": ("flag", "N"),
}
# PROJPROP 'schedule_check' (Check Schedule thresholds read by p6_health_check, see DCMA_CHECKS): targets
# in %, *_constraint limits in hours, bei_target a ratio, check_* switches.
CHECK_SPEC: dict[str, tuple[Any, str]] = {k: ("flag" if k.startswith("check_") else "float" if k == "bei_target"
                                             else "num", v) for k, v in (
    ("check_cpli", "Y"), ("soft_constraint_target", "5"), ("logic_target", "5"), ("long_lags_target", "5"),
    ("progress_date_target", "1"), ("check_links_to_closed_projects", "Y"), ("check_large_duration", "Y"),
    ("check_soft_constraints", "Y"), ("large_float_target", "1"), ("long_lags_constraint", "352"),
    ("large_float_constraint", "352"), ("late_activities_target", "5"), ("bei_target", "0.95"),
    ("large_duration_target", "5"), ("check_hard_constraints", "Y"), ("resouces_target", "1"),
    ("check_negative_float", "Y"), ("check_lag", "Y"), ("cpt_constraint", "1"), ("cpt_target", "5"),
    ("check_resources", "Y"), ("check_logic", "Y"), ("check_bei", "Y"), ("relationship_target", "90"),
    ("check_negative_lag", "Y"), ("check_long_lags", "Y"), ("check_large_float", "Y"),
    ("check_progress_duration", "Y"), ("negative_float_target", "1"), ("hard_constraint_target", "1"),
    ("cpli_target", "5"), ("check_cpt", "Y"), ("check_late_activities", "Y"), ("check_relationships", "Y"),
    ("negative_lag_target", "1"), ("lag_target", "5"), ("large_durations_constraint", "352"))}
PROP_SPEC = {"scheduling": SCHED_SPEC, "schedule_check": CHECK_SPEC}
LPL_ORDERS = ("ASC", "DESC")


def _lpl_of(node: Mapping) -> list[tuple[str, str]]:
    """LevelPriorityList node -> [(field, order)]: each child is '(0||field(sort_type|ASC)())' (older
    P6 rows write '(ASC|ASC)': the order is the last value)."""
    return [(k["name"], (list(k["attrs"].values()) or ["ASC"])[-1]) for k in node["kids"]]


def _lpl_text(lpl: Sequence[tuple[str, str]]) -> str:
    return ";".join(f"{f}:{o}" for f, o in lpl)


def _xer_lpl(text: str, sep: str = ",") -> Optional[list[tuple[str, str]]]:
    """Level priority list text -> [(field, order)], None when unreadable. XER SCHEDOPTIONS writes
    'priority_type,ASC_BY_FIELD/ASC' followed by a line break per entry (field, then the order after
    the last '/'); this server shows and takes 'field:ORDER', entries separated by ';' (line breaks
    are read as separators too)."""
    out = []
    for part in (p.strip() for p in re.split(r"[;\r\n]+", str(text))):
        if not part:
            continue
        field, s, rest = part.partition(sep)
        order = (rest.rsplit("/", 1)[-1] if s else "ASC").strip().upper()
        if not re.fullmatch(r"\w+", field.strip()) or order not in LPL_ORDERS:
            return None
        out.append((field.strip(), order))
    return out or None


def _norm_opt(prop: str, key: str, v: Any, lenient: bool = False) -> tuple[str, Optional[str]]:
    """(text as P6 stores it, warning) for one option value; ValueError when invalid. lenient (values
    P6 wrote in an XER): an allowed-value token this server does not know is written with a warning."""
    kind = PROP_SPEC[prop].get(key, ("text",))[0]
    s = ("Y" if v else "N") if isinstance(v, bool) else "" if v is None else str(v).strip()
    if kind == "flag":
        if s.upper() not in ("Y", "N"):
            raise ValueError(f"{key}: Y or N (or true/false), not {v!r}")
        return s.upper(), None
    if kind in ("int", "num", "float"):
        try:
            f = float(s)
        except ValueError:
            raise ValueError(f"{key}: a number, not {v!r}") from None
        if not math.isfinite(f) or f < 0:
            raise ValueError(f"{key}: a number 0 or more, not {v!r}")
        if kind == "int" and not f.is_integer():
            raise ValueError(f"{key}: a whole number, not {v!r}")
        return (repr(f) if kind == "float" or not f.is_integer() else str(int(f))), None
    if isinstance(kind, tuple):
        hit = next((k for k in kind if k.lower() == s.lower()), None)
        if hit:
            return hit, None
        if lenient and re.fullmatch(r"\w+", s):
            return s, f"{key}: XER value {s!r} is not one this server knows ({', '.join(kind)}); written as is"
        raise ValueError(f"{key}: one of {list(kind)}, not {v!r}")
    if re.search(r"[|()\r\n]", s):
        raise ValueError(f"{key}: P6 option values cannot contain | ( ) or line breaks: {v!r}")
    return s, None


def _key_task(con, pid: int, code: Any) -> str:
    """Key activity for multiple float paths: Activity ID of project pid -> task_id text ('-99' = none)."""
    c = "" if code is None else str(code).strip()
    if c.lower() in ("", "-99", "none", "(none)"):
        return "-99"
    tid = _task_id_by_code(con, pid, c)
    if tid is None:
        raise ValueError(f"key activity: Activity ID {c!r} not found in the project")
    return str(tid)


def _xer_sched_values(x: dict, xp: dict, key_map: Callable[[str], Optional[int]]
                      ) -> tuple[dict[str, str], Optional[list[tuple[str, str]]], list[str]]:
    """XER SCHEDOPTIONS of project xp -> (PROJPROP 'scheduling' values, level priority list, warnings).
    key_map turns the XER key activity task_id into the database task_id (None when it has no match:
    -99 is written, with a warning). Option names are kept as the XER spells them (lower case)."""
    row = _xer_sched_row(x, xp)
    vals, warn, lpl = {}, [], None
    for k, v in row.items():
        if k in ("schedoptions_id", "proj_id"):
            continue
        if k == "levelprioritylist":
            lpl = _xer_lpl(v) if v else None
            if v and lpl is None:
                warn.append(f"XER level priority list {v!r} not readable: the stored list is kept")
            continue
        if k == "key_activity_for_multiple_longest_paths":
            raw = "" if v is None else str(v).strip()
            tid = None if raw in ("", "-99") else key_map(raw)
            if raw not in ("", "-99") and tid is None:
                warn.append(f"key_activity_for_multiple_longest_paths: XER task_id {raw} has no activity in the "
                            "project (not in the XER, or no Activity ID match): -99 (none) written")
            vals[k] = "-99" if tid is None else str(tid)
            continue
        if v is None:
            continue
        vals[k], w = _norm_opt("scheduling", k, v, lenient=True)
        if w:
            warn.append(w)
    return vals, lpl, warn


# SCHEDOPTIONS block written by p6_export_xer: the column layout of P6's own XER exports.
SCHED_XER_COLS = ("schedoptions_id", "proj_id", "sched_outer_depend_type", "sched_open_critical_flag",
                  "sched_lag_early_start_flag", "sched_retained_logic", "sched_setplantoforecast", "sched_float_type",
                  "sched_calendar_on_relationship_lag", "sched_use_expect_end_flag", "sched_progress_override",
                  "level_float_thrs_cnt", "level_outer_assign_flag", "level_outer_assign_priority",
                  "level_over_alloc_pct", "level_within_float_flag", "level_keep_sched_date_flag",
                  "level_all_rsrc_flag", "sched_use_project_end_date_for_float", "enable_multiple_longest_path_calc",
                  "limit_multiple_longest_path_calc", "max_multiple_longest_path",
                  "use_total_float_multiple_longest_paths", "key_activity_for_multiple_longest_paths",
                  "LevelPriorityList")


def _xer_sched_export(con, pid: int, tasks: set) -> tuple[Optional[list], list[str]]:
    """(SCHEDOPTIONS values in SCHED_XER_COLS order, warnings) from the project's PROJPROP 'scheduling'
    (the reverse of _xer_sched_values); None when the project has none or it is unreadable. The key
    activity is the exported task_id (empty = none, also for an activity outside the project); the
    level priority list is written as P6 does, 'field,ASC_BY_FIELD/ORDER' plus a line break per entry."""
    tree = _props(con, pid, "scheduling")
    if tree is None:
        return None, ["SCHEDOPTIONS not exported: the project has no schedule options stored (PROJPROP "
                      "scheduling); P6 applies its defaults on import."]
    if tree.get("error"):
        return None, [f"SCHEDOPTIONS not exported: {tree['error']}"]
    opts = {k.lower(): v for k, v in tree["attrs"].items()}
    warn = []
    key = str(opts.get("key_activity_for_multiple_longest_paths") or "").strip()
    known = key.isdigit() and int(key) in tasks
    if key not in ("", "-99") and not known:
        warn.append(f"SCHEDOPTIONS: key activity task_id {key} is not an activity of the project: written as none")
    opts["key_activity_for_multiple_longest_paths"] = key if known else ""
    node = _p6_find(tree, "LevelPriorityList")
    opts["levelprioritylist"] = "".join(f"{f},ASC_BY_FIELD/{o}\n" for f, o in _lpl_of(node)) if node else None
    extra = sorted(set(opts) - {c.lower() for c in SCHED_XER_COLS})
    if extra:
        warn.append(f"SCHEDOPTIONS: stored options with no XER column, not exported: {extra}")
    opts["schedoptions_id"], opts["proj_id"] = 1, pid
    return [opts.get(c.lower()) for c in SCHED_XER_COLS], warn


def _set_props(con, pid: int, prop: str, values: Mapping[str, str], lpl: Optional[Sequence[tuple[str, str]]],
               now: str) -> dict:
    """Write PROJPROP (pid, prop): the stored tree (or P6's defaults when the project has no row) with
    values set and, for 'scheduling', the level priority list replaced when lpl is given. Keys keep
    their order, new ones follow in P6's order, unknown keys and other nodes are kept; the text form
    (TEXT or BLOB) of the stored row is kept, a new row is TEXT as P6 writes it. Nothing is written
    when the text is unchanged. Returns {action, options: [{option, old, new, changed}]}."""
    spec = PROP_SPEC[prop]
    row = con.execute("SELECT * FROM PROJPROP WHERE proj_id = ? AND prop_name = ?", (pid, prop)).fetchone()
    live = row is not None and not _dict(row).get("delete_session_id")
    raw = row["prop_value"] if live else None
    if live:
        try:
            tree = _p6_tree(_blob_text(raw), strict=True)
        except ValueError as e:
            raise ValueError(f"PROJPROP {prop} of this project is not readable ({e}): fix it in P6 "
                             "(Schedule Options) before writing it here") from None
    else:
        tree = {"num": "0", "name": "", "attrs": {k: d for k, (_, d) in spec.items()}, "kids": []}
        if prop == "scheduling":
            tree["kids"] = [{"num": "0", "name": "LevelResourceList", "attrs": {}, "kids": []},
                            {"num": "0", "name": "LevelPriorityList", "attrs": {}, "kids": [
                                {"num": "0", "name": "priority_type", "attrs": {"sort_type": "ASC"}, "kids": []}]}]
    old = dict(tree["attrs"])
    order = [k for k in spec if k in values] + [k for k in values if k not in spec]
    for k in order:
        tree["attrs"][k] = values[k]
    rows = [{"option": k, "old": old.get(k), "new": tree["attrs"][k], "changed": old.get(k) != tree["attrs"][k]}
            for k in tree["attrs"]]
    if prop == "scheduling":
        node = _p6_find(tree, "LevelPriorityList")
        before = _lpl_text(_lpl_of(node)) if node else None
        if lpl is not None:
            if node is None:
                node = {"num": "0", "name": "LevelPriorityList", "attrs": {}, "kids": []}
                tree["kids"].append(node)
            node["kids"] = [{"num": "0", "name": f, "attrs": {"sort_type": o}, "kids": []} for f, o in lpl]
        after = _lpl_text(_lpl_of(node)) if node else None
        rows.append({"option": "levelprioritylist", "old": before, "new": after, "changed": before != after})
    text = _p6_dump(tree)
    if live and _blob_text(raw) == text:
        return {"action": "unchanged", "options": rows}
    value = text
    if isinstance(raw, (bytes, bytearray)):  # keep a BLOB row a BLOB, with its trailing NUL if it had one
        value = text.encode("utf-8") + (b"\x00" if bytes(raw).endswith(b"\x00") else b"")
    cols = _cols(con, "PROJPROP")
    if row is None:
        _insert(con, "PROJPROP", {"proj_id": pid, "prop_name": prop, "prop_value": value,
                                  **_stamp_cols(con, "PROJPROP", now, create=True)})
        action = "inserted"
    else:
        upd = {"prop_value": value, **_stamp_cols(con, "PROJPROP", now, create=False)}
        upd.update({c: None for c in ("delete_session_id", "delete_date") if c in cols})
        _update(con, "PROJPROP", {"proj_id": pid, "prop_name": prop}, upd)
        action = "updated" if live else "restored"
    return {"action": action, "options": rows}


# ----------------------------------------------------------------- schedule options (write)

def _task_id_by_code(con, pid: int, code: Optional[str]) -> Optional[int]:
    if not code:
        return None
    r = con.execute("SELECT task_id FROM TASK WHERE proj_id = ? AND task_code = ?" + _alive(con, "TASK"),
                    (pid, code)).fetchone()
    return r[0] if r else None


def _project_row(con, project: str) -> dict:
    """Live PROJECT row of a project (not an EPS node), lowercase keys."""
    r = con.execute("SELECT * FROM PROJECT WHERE proj_short_name = ?" + _alive(con, "PROJECT"), (project,)).fetchone()
    if r is None:
        raise ValueError(f"Project not found: {project}")
    if (_dict(r).get("project_flag") or "Y") != "Y":
        raise ValueError(f"{project} is an EPS node, not a project")
    return _dict(r)


def _opt_rows(con, rows: Iterable[dict]) -> list[dict]:
    """_set_props option rows for the caller: key activity as Activity ID, changed options first."""
    def lab(v):
        if v is None or v in ("", "-99"):
            return None if v is None else "(none)"
        return _db_task_code(con, v) or f"{v} (no such activity)"
    out = [{**r, "old": lab(r["old"]), "new": lab(r["new"])}
           if r["option"] == "key_activity_for_multiple_longest_paths" else r for r in rows]
    return sorted(out, key=lambda r: not r["changed"])


def _sched_write(con, pid: int, xer: Optional[tuple], options: Mapping, key_activity: Optional[str],
                 lpl: Optional[list], now: str) -> tuple[Optional[dict], list[str]]:
    """PROJPROP 'scheduling' of pid from an XER (x, xp, key_map), then explicit options (the key
    activity as an Activity ID). Returns (_set_props result, None when nothing is asked; warnings)."""
    warn, vals = [], {}
    if xer:
        x, xp, key_map = xer
        vals, xlpl, warn = _xer_sched_values(x, xp, key_map)
        lpl = xlpl if lpl is None else lpl
    stored = (_props(con, pid, "scheduling") or {}).get("attrs", {})
    key = "key_activity_for_multiple_longest_paths"
    for k, v in options.items():
        if k == key:
            vals[k] = _key_task(con, pid, v)
        elif k not in SCHED_SPEC and k not in stored:
            raise ValueError(f"Unknown schedule option {k!r}: {sorted(SCHED_SPEC)} (or levelprioritylist)")
        else:
            vals[k] = _norm_opt("scheduling", k, v)[0]
    if key_activity is not None:
        vals[key] = _key_task(con, pid, key_activity)
    if not vals and lpl is None:
        return None, warn
    # sched_retained_logic and sched_progress_override both Y is a valid P6 25.12 state (parity case prg-oos-both:
    # progress override forward, retained logic backward), so the pair is never refused
    return _set_props(con, pid, "scheduling", vals, lpl, now), warn


def _lpl_arg(level_priority_list) -> Optional[list[tuple[str, str]]]:
    if level_priority_list is None:
        return None
    items = [level_priority_list] if isinstance(level_priority_list, str) else list(level_priority_list)
    lpl = _xer_lpl(";".join(str(i) for i in items), ":")
    if lpl is None:
        raise ValueError(f"level_priority_list: entries 'field:ASC' or 'field:DESC', not {level_priority_list!r}")
    return lpl
