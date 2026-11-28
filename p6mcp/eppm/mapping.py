"""REST objects -> P6 table rows (docs/EPPM_PLAN.md section 3.2, object reads).

P6 EPPM 25.12 exports a project as XER but refuses a baseline ("Could not load Project with id"), while the REST
objects of a baseline read fine (measured on the test server). So the baseline of a REST pull is rebuilt from its
objects: WBS, activities, relationships and resource assignments, with the columns the baseline tools read
(baseline variance, BEI, milestones vs baseline, compare). Values arrive as text: numbers are converted, dates lose
their 'T' and any offset (the PMDB wall clock), enums become P6 codes. Fields a server version does not list are
left out (`fields()` intersects with GET /<entity>/fields), so a missing field is a NULL column, never an error.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable, Optional
from p6mcp.messages import _t

STATUS = {"not started": "TK_NotStart", "in progress": "TK_Active", "completed": "TK_Complete"}
TASK_TYPE = {"task dependent": "TT_Task", "resource dependent": "TT_Rsrc", "level of effort": "TT_LOE",
             "start milestone": "TT_Mile", "finish milestone": "TT_FinMile", "wbs summary": "TT_WBS"}
DURATION_TYPE = {"fixed duration and units/time": "DT_FixedDUR2", "fixed duration and units": "DT_FixedDrtn",
                 "fixed units": "DT_FixedQty", "fixed units/time": "DT_FixedRate"}
PCT_TYPE = {"physical": "CP_Phys", "duration": "CP_Drtn", "units": "CP_Units"}
LINK_TYPE = {"finish to start": "PR_FS", "start to start": "PR_SS", "finish to finish": "PR_FF",
             "start to finish": "PR_SF"}
CSTR = {"start on": "CS_MSO", "start on or before": "CS_MSOB", "start on or after": "CS_MSOA", "finish on": "CS_MEO",
        "finish on or before": "CS_MEOB", "finish on or after": "CS_MEOA", "as late as possible": "CS_ALAP",
        "mandatory start": "CS_MANDSTART", "mandatory finish": "CS_MANDFIN"}
RSRC_TYPE = {"labor": "RT_Labor", "nonlabor": "RT_Equip", "material": "RT_Mat"}
PROJECT_STATUS = {"active": "WS_Open", "inactive": "WS_Planned", "planned": "WS_Planned", "what-if": "WS_Whatif",
                  "what if": "WS_Whatif", "template": "WS_Template"}
# BaselineProject -> PROJECT (column None: read for the project node only)
BASELINE_PROJECT = (
    ("ObjectId", "proj_id", "id"), ("Id", "proj_short_name", "text"), ("Name", None, "text"),
    ("Status", None, "text"), ("DataDate", "last_recalc_date", "date"),
    ("PlannedStartDate", "plan_start_date", "date"), ("MustFinishByDate", "plan_end_date", "date"),
    ("ScheduledFinishDate", "scd_end_date", "date"), ("LastScheduleDate", "last_schedule_date", "date"),
    ("OriginalProjectObjectId", "orig_proj_id", "id"),
)

# (REST field, column, kind): kind id / int / num / text / date / flag / an enum dict
ACTIVITY = (
    ("ObjectId", "task_id", "id"), ("ProjectObjectId", "proj_id", "id"), ("WBSObjectId", "wbs_id", "id"),
    ("CalendarObjectId", "clndr_id", "id"), ("Id", "task_code", "text"), ("Name", "task_name", "text"),
    ("Status", "status_code", STATUS), ("Type", "task_type", TASK_TYPE), ("DurationType", "duration_type",
                                                                          DURATION_TYPE),
    ("PercentCompleteType", "complete_pct_type", PCT_TYPE),
    ("PlannedDuration", "target_drtn_hr_cnt", "num"), ("RemainingDuration", "remain_drtn_hr_cnt", "num"),
    ("PlannedStartDate", "target_start_date", "date"), ("PlannedFinishDate", "target_end_date", "date"),
    ("ActualStartDate", "act_start_date", "date"), ("ActualFinishDate", "act_end_date", "date"),
    ("EarlyStartDate", "early_start_date", "date"), ("EarlyFinishDate", "early_end_date", "date"),
    ("LateStartDate", "late_start_date", "date"), ("LateFinishDate", "late_end_date", "date"),
    ("RemainingEarlyStartDate", "restart_date", "date"), ("RemainingEarlyFinishDate", "reend_date", "date"),
    ("ExpectedFinishDate", "expect_end_date", "date"), ("TotalFloat", "total_float_hr_cnt", "num"),
    ("FreeFloat", "free_float_hr_cnt", "num"), ("PrimaryConstraintType", "cstr_type", CSTR),
    ("PrimaryConstraintDate", "cstr_date", "date"), ("SecondaryConstraintType", "cstr_type2", CSTR),
    ("SecondaryConstraintDate", "cstr_date2", "date"), ("PhysicalPercentComplete", "phys_complete_pct", "num"),
    ("IsLongestPath", "driving_path_flag", "flag"), ("FloatPath", "float_path", "int"),
    ("FloatPathOrder", "float_path_order", "int"),
    ("PlannedLaborUnits", "target_work_qty", "num"), ("ActualLaborUnits", "act_work_qty", "num"),
    ("RemainingLaborUnits", "remain_work_qty", "num"), ("PlannedNonLaborUnits", "target_equip_qty", "num"),
    ("ActualNonLaborUnits", "act_equip_qty", "num"), ("RemainingNonLaborUnits", "remain_equip_qty", "num"),
)
RELATIONSHIP = (
    ("ObjectId", "task_pred_id", "id"), ("SuccessorActivityObjectId", "task_id", "id"),
    ("PredecessorActivityObjectId", "pred_task_id", "id"), ("SuccessorProjectObjectId", "proj_id", "id"),
    ("PredecessorProjectObjectId", "pred_proj_id", "id"), ("Type", "pred_type", LINK_TYPE),
    ("Lag", "lag_hr_cnt", "num"),
)
WBS = (
    ("ObjectId", "wbs_id", "id"), ("ProjectObjectId", "proj_id", "id"), ("ParentObjectId", "parent_wbs_id", "id"),
    ("Code", "wbs_short_name", "text"), ("Name", "wbs_name", "text"), ("SequenceNumber", "seq_num", "int"),
)
ASSIGNMENT = (
    ("ObjectId", "taskrsrc_id", "id"), ("ActivityObjectId", "task_id", "id"), ("ProjectObjectId", "proj_id", "id"),
    ("ResourceObjectId", "rsrc_id", "id"), ("RoleObjectId", "role_id", "id"), ("ResourceType", "rsrc_type", RSRC_TYPE),
    ("PlannedUnits", "target_qty", "num"), ("ActualRegularUnits", "act_reg_qty", "num"),
    ("ActualOvertimeUnits", "act_ot_qty", "num"), ("RemainingUnits", "remain_qty", "num"),
    ("PlannedUnitsPerTime", "target_qty_per_hr", "num"), ("PlannedCost", "target_cost", "num"),
    ("ActualRegularCost", "act_reg_cost", "num"), ("RemainingCost", "remain_cost", "num"),
    ("PlannedStartDate", "target_start_date", "date"), ("PlannedFinishDate", "target_end_date", "date"),
    ("ActualStartDate", "act_start_date", "date"), ("ActualFinishDate", "act_end_date", "date"),
    ("RemainingStartDate", "restart_date", "date"), ("RemainingFinishDate", "reend_date", "date"),
)
_OFFSET = re.compile(r"([+-]\d\d:\d\d|Z)$")


def value(v: Any, kind: Any) -> Any:
    """One REST value (text on the wire) -> what the P6 table stores; None for empty."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    if isinstance(kind, dict):
        return kind.get(s.lower(), s)
    if kind in ("id", "int"):
        try:
            return int(float(s))
        except ValueError:
            return None
    if kind == "num":
        try:
            return float(s)
        except ValueError:
            return None
    if kind == "date":
        s = _OFFSET.sub("", s).replace("T", " ")
        return s[:19] + (":00" if len(s) == 16 else "")
    if kind == "flag":
        return "Y" if s.lower() in ("true", "y", "yes", "1") else "N"
    return s


def fields(sess, entity: str, spec: tuple, cache: dict) -> list[str]:
    """The REST fields of spec that the server lists for entity (all of them when the list cannot be read)."""
    if entity not in cache:
        try:
            data = sess.call("GET", f"/{entity}/fields", name=f"fields {entity}")
        except ValueError:
            data = None
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), None)
        names = set()
        for x in data or []:
            if isinstance(x, str):
                names.add(x)
            elif isinstance(x, dict):
                n = x.get("Name") or x.get("name") or x.get("FieldName")
                if isinstance(n, str):
                    names.add(n)
        cache[entity] = names
    have = cache[entity]
    return [f for f, _c, _k in spec if not have or f in have]


def insert(out: sqlite3.Connection, table: str, spec: tuple, rows: list[dict], extra: Optional[dict] = None) -> int:
    """Rows into table (created or widened as needed: the XER build may lack a column)."""
    cols = [c for _f, c, _k in spec] + list(extra or {})
    have = {r[1].lower() for r in out.execute(f'PRAGMA table_info("{table}")')}
    if not have:
        out.execute(f'CREATE TABLE "{table}" ({", ".join(cols)})')
    else:
        for c in cols:
            if c.lower() not in have:
                out.execute(f'ALTER TABLE "{table}" ADD COLUMN {c}')
    data = [tuple(value(r.get(f), k) for f, _c, k in spec) + tuple((extra or {}).values()) for r in rows]
    if data:
        out.executemany(f'INSERT INTO "{table}" ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})', data)
    return len(data)


def baseline(sess, out: sqlite3.Connection, bl_oid: int, project_oid: int, warnings: list[str],
             progress: Optional[Callable[[str], None]] = None) -> dict:
    """Add the baseline project bl_oid (a BaselineProject of project_oid) to the working copy from its REST objects.
    Returns the row counts. Its ObjectIds are the server's, distinct from the project's."""
    cache: dict = {}

    def read(entity: str, spec: tuple, filt: str) -> list[dict]:
        if progress:
            progress(entity)
        return sess.read(entity, fields(sess, entity, spec, cache), filt)

    info = sess.read("baselineProject", fields(sess, "baselineProject", BASELINE_PROJECT, cache),
                     f"ObjectId :eq: {int(bl_oid)}")
    if not info:
        raise ValueError(_t("baseline {id} is not readable", id=bl_oid))
    b = info[0]
    wbs = read("wbs", WBS, f"ProjectObjectId :eq: {int(bl_oid)}")
    acts = read("activity", ACTIVITY, f"ProjectObjectId :eq: {int(bl_oid)}")
    rels = read("relationship", RELATIONSHIP, f"SuccessorProjectObjectId :eq: {int(bl_oid)}")
    asg = read("resourceAssignment", ASSIGNMENT, f"ProjectObjectId :eq: {int(bl_oid)}")
    # the project node: the parent of the top WBS nodes (no WBS has its ObjectId)
    ids = {value(w.get("ObjectId"), "id") for w in wbs}
    parents = [value(w.get("ParentObjectId"), "id") for w in wbs]
    node = next((p for p in parents if p and p not in ids), None) or -int(bl_oid)
    proj_cols = {r[1].lower() for r in out.execute("PRAGMA table_info(PROJECT)")}
    proj = {c: value(b.get(f), k) for f, c, k in BASELINE_PROJECT if c}
    proj.update(proj_id=int(bl_oid), orig_proj_id=int(project_oid), project_flag="Y")
    for c in proj:
        if c not in proj_cols:
            out.execute(f"ALTER TABLE PROJECT ADD COLUMN {c}")
    keys = list(proj)
    out.execute(f"INSERT INTO PROJECT ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                tuple(proj.values()))
    counts = {"PROJECT": 1}
    insert(out, "PROJWBS", (("_n", "wbs_id", "id"), ("_p", "proj_id", "id"), ("_s", "wbs_short_name", "text"),
                            ("_m", "wbs_name", "text"), ("_st", "status_code", PROJECT_STATUS)),
           [{"_n": node, "_p": bl_oid, "_s": b.get("Id"), "_m": b.get("Name"), "_st": b.get("Status") or "Active"}],
           {"proj_node_flag": "Y"})
    counts["PROJWBS"] = 1 + insert(out, "PROJWBS", WBS, wbs, {"proj_node_flag": "N"})
    counts["TASK"] = insert(out, "TASK", ACTIVITY, acts)
    inside = {value(a.get("ObjectId"), "id") for a in acts}
    keep = [r for r in rels if value(r.get("PredecessorActivityObjectId"), "id") in inside]
    if len(keep) < len(rels):
        warnings.append(_t("baseline {baseline}: {n} link(s) to other projects left out",
                           baseline=b.get("Id"), n=len(rels) - len(keep)))
    counts["TASKPRED"] = insert(out, "TASKPRED", RELATIONSHIP, keep)
    counts["TASKRSRC"] = insert(out, "TASKRSRC", ASSIGNMENT, asg)
    counts["CALENDAR"] = _calendars(sess, out, {value(a.get("CalendarObjectId"), "id") for a in acts}, b.get("Id"),
                                    warnings)
    return counts


def _calendars(sess, out: sqlite3.Connection, used: set, label: str, warnings: list[str]) -> int:
    """Calendars of the baseline that the working copy lacks (P6 copies the project calendars into a baseline).
    The REST calendar object has no working time on 25.12, so each one takes the working time of the calendar of
    the same name already in the copy (the project's own, from its XER export); else only its hours per day."""
    have = {r[0] for r in out.execute("SELECT clndr_id FROM CALENDAR")}
    missing = sorted(i for i in used - have if i is not None)
    if not missing:
        return 0
    rows = sess.read("calendar", ["ObjectId", "Name", "HoursPerDay"],
                     " :or: ".join(f"ObjectId :eq: {i}" for i in missing))
    cols = [r[1] for r in out.execute("PRAGMA table_info(CALENDAR)")]
    added, guessed = 0, []
    for r in rows:
        cid, name = value(r.get("ObjectId"), "id"), r.get("Name") or ""
        src = out.execute("SELECT * FROM CALENDAR WHERE clndr_name = ? ORDER BY clndr_id LIMIT 1", (name,)).fetchone()
        if src is not None:
            row = dict(zip(cols, tuple(src), strict=True))
            row["clndr_id"] = cid
        else:
            row = {"clndr_id": cid, "clndr_name": name, "day_hr_cnt": value(r.get("HoursPerDay"), "num")}
            guessed.append(name)
        keys = [k for k in row if k in cols]
        out.execute(f"INSERT INTO CALENDAR ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                    tuple(row[k] for k in keys))
        added += 1
    if guessed:
        warnings.append(_t("baseline {label}: calendar(s) {guessed} have no working time in the copy (hours per day "
                           "only)", label=label, guessed=", ".join(guessed)))
    if added < len(missing):
        warnings.append(_t("baseline {label}: {n} calendar(s) not readable",
                           label=label, n=len(missing) - added))
    return added
