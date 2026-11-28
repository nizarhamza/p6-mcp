"""Edit tools: relationships, activities, assignments, UDF values, activity code values, WBS moves,
activity delete."""

from __future__ import annotations

from typing import Mapping, Optional

from p6mcp.core.constants import ACT_LINKS_CAP, STATUS_NAMES, TYPE_NAMES
from p6mcp.registry import (
    ActEdits, ActivityId, AsgEdits, CstrName, DateTimeStr, LinkEdits, LinkIds, LinkNews, LinkRefs, NewActivities,
    NonNegDays, NonNegQty, Pct, ProjectId, RelType, _tool, UdfEdits, UdfValue,
)
from p6mcp.core.db import _alive, _cols, _connect, _project_id, _task
from p6mcp.core.props import _sched_options
from p6mcp.core.read import _act_details, _rel_code, _rel_row_fn, _rel_sql
from p6mcp.core.writes import (
    _act_add_batch, _act_check, _act_delete_rows, ACT_SNAPSHOT, _act_update, _asg_update, _batch, _batch_commit,
    _commit, _items_arg, _link_batch, MAX_BATCH, NEW_DATES_NOTE, _Refused, _rel_add, _rel_delete, _rel_update,
    _side_effects, _single_link, _sum_plan, _summaries_stale, _udf_next, _udf_set, _write_session,
)
from p6mcp.registry import CodeEdits, WbsMoves  # activity code values and WBS moves
from p6mcp.core.writes import _act_move, _code_set, _codes_next, _moves_next


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_add_relationship(
    project: ProjectId, predecessor_code: ActivityId, successor_code: ActivityId,
    rel_type: RelType = "FS", lag_days: float = 0,
    predecessor_project: Optional[ProjectId] = None, confirm: bool = False,
) -> dict:
    """Add a relationship predecessor -> successor. rel_type FS/SS/FF/SF. lag_days is converted to
    hours with the calendar named by the project schedule option for relationship lag (predecessor
    calendar when none is stored). predecessor_project for cross-project links. Two activities may be
    linked once per type (e.g. SS and FF together). Dry run unless confirm=True."""
    _rel_code(rel_type, "rel_type")
    return _single_link(_rel_add, project, {"predecessor_code": predecessor_code, "successor_code": successor_code,
                                            "rel_type": rel_type, "lag_days": lag_days,
                                            "predecessor_project": predecessor_project}, confirm)


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_update_relationship(
    project: ProjectId, predecessor_code: ActivityId, successor_code: ActivityId,
    rel_type: Optional[RelType] = None, lag_days: Optional[float] = None,
    predecessor_project: Optional[ProjectId] = None, current_type: Optional[RelType] = None,
    confirm: bool = False,
) -> dict:
    """Change type and/or lag of an existing relationship. lag_days uses the same lag calendar rule
    as p6_add_relationship. current_type selects which one when the two activities are linked by
    several types. Dry run unless confirm=True."""
    if rel_type:
        _rel_code(rel_type, "rel_type")
    return _single_link(_rel_update, project, {"predecessor_code": predecessor_code, "successor_code": successor_code,
                                               "rel_type": rel_type, "lag_days": lag_days, "current_type": current_type,
                                               "predecessor_project": predecessor_project}, confirm)


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_delete_relationship(
    project: ProjectId, predecessor_code: ActivityId, successor_code: ActivityId,
    predecessor_project: Optional[ProjectId] = None, current_type: Optional[RelType] = None,
    confirm: bool = False,
) -> dict:
    """Delete a relationship. current_type selects which one when the two activities are linked
    by several types. Dry run unless confirm=True."""
    return _single_link(_rel_delete, project, {"predecessor_code": predecessor_code, "successor_code": successor_code,
                                               "current_type": current_type,
                                               "predecessor_project": predecessor_project}, confirm)


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_delete_relationships(project: ProjectId, ids: Optional[LinkIds] = None, pairs: Optional[LinkRefs] = None,
                            confirm: bool = False) -> dict:
    """Delete many relationships in one transaction (all or nothing; dry run unless confirm=True). ids:
    task_pred_id values of links that touch the project (either end); pairs: {predecessor_code,
    successor_code, type (needed when several types link the pair), predecessor_project}. items: one
    report per id, then per pair (the link removed: task_pred_id, predecessor, successor, type, lag_d;
    or why it cannot be resolved, and then confirm writes nothing). side_effects: open activities left
    without any predecessor or successor (LOE and WBS summary links do not count). Hard delete, as P6
    and p6_delete_relationship do. One edit log entry per link."""
    items = [{"task_pred_id": n} for n in _items_arg(ids or [])] + _items_arg(pairs or [])
    if len(items) > MAX_BATCH:
        raise ValueError(f"At most {MAX_BATCH} items per call")
    return _link_batch(project, items, "delete", confirm, _rel_delete, f"Delete {len(items)} relationship(s)")


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_add_relationships(project: ProjectId, items: LinkNews, confirm: bool = False) -> dict:
    """Add many relationships in one transaction (all or nothing; dry run unless confirm=True). Item:
    {predecessor_code, successor_code, rel_type (FS default), lag_days (0 default, days of the lag
    calendar rule as in p6_add_relationship), predecessor_project}. items: one report per item
    (task_pred_id from NEXTKEY, or the error); side_effects as in p6_delete_relationships (empty for
    additions). One edit log entry per link."""
    items = _items_arg(items)
    return _link_batch(project, items, "add", confirm, _rel_add, f"Add {len(items)} relationship(s)")


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_update_relationships(project: ProjectId, items: LinkEdits, confirm: bool = False) -> dict:
    """Change type and/or lag of many relationships in one transaction (all or nothing; dry run unless
    confirm=True). Item: {predecessor_code, successor_code, rel_type, lag_days, current_type (picks
    the link when several types link the pair), predecessor_project}, as p6_update_relationship.
    items: one report per item; side_effects as in p6_delete_relationships (empty: no link is
    removed). One edit log entry per link."""
    items = _items_arg(items)
    return _link_batch(project, items, "update", confirm, _rel_update, f"Change {len(items)} relationship(s)")


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_update_activity(
    project: ProjectId, task_code: ActivityId,
    task_name: Optional[str] = None,
    original_duration_days: Optional[NonNegDays] = None,
    remaining_duration_days: Optional[NonNegDays] = None,
    constraint: Optional[CstrName] = None,
    constraint_date: Optional[DateTimeStr] = None,
    actual_start: Optional[DateTimeStr] = None,
    actual_finish: Optional[DateTimeStr] = None,
    physical_pct: Optional[Pct] = None,
    allow_after_data_date: bool = False,
    confirm: bool = False,
) -> dict:
    """Edit one activity. Planned dates are not editable: move work with durations,
    logic or constraints, then F9 in P6. constraint: one of
    none, start_on_or_after, start_on_or_before, start_on, finish_on_or_after,
    finish_on_or_before, finish_on, mandatory_start, mandatory_finish, as_late_as_possible.
    Dates as 'YYYY-MM-DD HH:MM' ('T' and seconds accepted). A date alone takes the first shift
    start of the activity calendar (actual start, start constraints) or its last shift end (actual
    finish, finish constraints) and is refused on a non-work day; an explicit time is kept.
    Durations must be 0 or more, physical_pct 0 to 100, the actual finish not before the actual
    start; actual dates after the data date need allow_after_data_date=True. Setting actual_start
    makes it In Progress, actual_finish makes it Completed. A milestone has one date: either actual
    date sets both and makes it Completed (a date alone: first shift start for a start milestone,
    last shift end for a finish milestone). Dry run unless confirm=True."""
    it = {k: v for k, v in (
        ("task_code", task_code), ("task_name", task_name), ("original_duration_days", original_duration_days),
        ("remaining_duration_days", remaining_duration_days), ("constraint", constraint),
        ("constraint_date", constraint_date), ("actual_start", actual_start), ("actual_finish", actual_finish),
        ("physical_pct", physical_pct)) if v is not None}
    if allow_after_data_date:
        it["allow_after_data_date"] = True
    _act_check(it)  # a malformed value fails before any lock or backup
    with _write_session(confirm) as (con, backup):
        try:
            r = _act_update(con, project, it)
        except _Refused as e:
            return {"applied": False, "error": str(e)}
        plan = list(r["_lines"])
        if backup:
            plan.append(f"Backup: {backup}")
        return _commit(con, plan, confirm, projects=r["projects"], log=r["log"])


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_update_activities(project: ProjectId, items: ActEdits, confirm: bool = False) -> dict:
    """Edit many activities in one transaction (all or nothing; dry run unless confirm=True), for a
    progress update: actual dates, remaining durations, physical %, names, constraints. Item: {task_code,
    task_name, original_duration_days, remaining_duration_days, constraint, constraint_date, actual_start,
    actual_finish, physical_pct, allow_after_data_date}, each with the rules of p6_update_activity. items:
    one report per item (its plan or the error); one edit log entry per activity. At most 500 items."""
    items = _items_arg(items)
    with _write_session(confirm) as (con, backup):
        _project_id(con, project)
        b = _batch(con, items, lambda i, it: _act_update(con, project, it))
        for rep, it in zip(b["items"], items, strict=False):
            rep["item"] = it
        word = "activity" if len(items) == 1 else "activities"
        return _batch_commit(con, b, confirm, f"Update {len(items)} {word}", backup)


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_add_activities(project: ProjectId, items: NewActivities, confirm: bool = False) -> dict:
    """Create activities in a project, with their relationships, in one transaction (all or nothing; dry run
    unless confirm=True). Item: {task_code (new Activity ID, unique in the project), task_name, wbs (WBS code as
    p6_list_wbs shows it, or a unique WBS name; default the project node), type (task (default),
    resource_dependent, start_milestone, finish_milestone, level_of_effort, wbs_summary), original_duration_days
    (days of the activity calendar; task and resource dependent default to the admin preference; none for
    milestones, level of effort and WBS summary), calendar (name of a global or project calendar; default the
    project calendar), constraint and constraint_date (as p6_update_activity), predecessors and successors
    [{task_code, type (FS default), lag_days (0 default, lag calendar rule as p6_add_relationship), project
    (another project)}]: they may name activities of the same call}. New activities are Not Started, remaining
    = original duration, physical % 0, duration and % complete types from the project defaults; their dates sit
    at the data date until F9 (late dates and float empty). Refused: an Activity ID already used or too long, an
    unknown WBS or calendar, a relationship P6 would refuse, a logic loop the new relationships close. items:
    one report per activity (task_id from NEXTKEY, links with task_pred_id, or the errors). One edit log entry
    per activity and per relationship. At most 500 items. Then press F9 in P6."""
    items = _items_arg(items)
    with _write_session(confirm) as (con, backup):
        b = _act_add_batch(con, project, items)
        for rep, it in zip(b["items"], items, strict=False):
            rep["item"] = it
        word = "activity" if len(items) == 1 else "activities"
        res = _batch_commit(con, b, confirm, f"Add {len(items)} {word} to {project}", backup, tail=[NEW_DATES_NOTE])
        res.update(data_date=b["data_date"], dates_from=b["dates_from"], loops=b["loops"])
        return res


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_update_assignment(
    project: ProjectId, task_code: Optional[ActivityId] = None, resource: Optional[str] = None,
    role: Optional[str] = None, taskrsrc_id: Optional[int] = None,
    budgeted_units: Optional[NonNegQty] = None, actual_units: Optional[NonNegQty] = None,
    remaining_units: Optional[NonNegQty] = None, at_completion_units: Optional[NonNegQty] = None,
    confirm: bool = False,
) -> dict:
    """Set the units of one resource assignment, as P6 recalculates them. Pick it by task_code plus
    resource (Resource ID) or role, or by taskrsrc_id (p6_get_activity assignments). at_completion_units
    = actual + remaining (sets remaining; not with remaining_units). Rules: actual units need a started
    activity, a completed one keeps 0 remaining; a not started activity whose project links budget and
    at completion keeps them equal (one value moves both); costs = units x price/unit when the cost is
    linked to units; units/time follow (Fixed Duration types only: durations never change); the
    activity's labor / nonlabor unit totals follow. Dates are not affected. P6's summary tables
    (SUMTASK, SUMTASKSPREAD, SUMTRSRC) are never written: summaries_stale {stale, tables {table:
    rows the change leaves out of date}, refreshed (always empty), advice (run Summarize in P6)}.
    Dry run unless confirm=True."""
    it = {k: v for k, v in (
        ("task_code", task_code), ("resource", resource), ("role", role), ("taskrsrc_id", taskrsrc_id),
        ("budgeted_units", budgeted_units), ("actual_units", actual_units), ("remaining_units", remaining_units),
        ("at_completion_units", at_completion_units)) if v is not None}
    with _write_session(confirm) as (con, backup):
        try:
            x = _asg_update(con, project, it)
        except _Refused as e:
            return {"applied": False, "error": str(e)}
        sums = _summaries_stale(con, [x["_sum"]])
        plan = [x["plan"], *_sum_plan(sums)]
        if backup:
            plan.append(f"Backup: {backup}")
        res = _commit(con, plan, confirm, projects=x["projects"], log=x["log"])
        res["summaries_stale"] = sums
        return res


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_update_assignments(project: ProjectId, items: AsgEdits, confirm: bool = False) -> dict:
    """Set the units of many assignments in one transaction (all or nothing; dry run unless
    confirm=True). Item: {task_code, resource, role, taskrsrc_id, budgeted_units, actual_units,
    remaining_units, at_completion_units}, as p6_update_assignment. items: one report per item
    (assignment before / after, or the error). One edit log entry per assignment. summaries_stale
    as p6_update_assignment, for every applied item."""
    items = _items_arg(items)
    with _write_session(confirm) as (con, backup):
        _project_id(con, project)
        keys: list[tuple] = []

        def apply_one(i: int, it) -> dict:
            if not isinstance(it, Mapping):
                raise ValueError("an item must be an object")
            x = _asg_update(con, project, it)
            keys.append(x["_sum"])
            return x

        b = _batch(con, items, apply_one)
        for rep, it in zip(b["items"], items, strict=False):
            rep["item"] = it
        sums = _summaries_stale(con, keys)
        res = _batch_commit(con, b, confirm, f"Update units of {len(items)} assignment(s)", backup, tail=_sum_plan(sums))
        res["summaries_stale"] = sums
        return res


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_update_udf(project: ProjectId, task_code: ActivityId, udf: str, value: Optional[UdfValue] = None,
                  clear: bool = False, confirm: bool = False) -> dict:
    """Set one activity UDF value (a 'User Defined' column of P6, labels in p6_list_udfs), or remove it
    with clear=True. value per data type: text (255 characters max), number (2 decimals), integer,
    cost, start_date / finish_date 'YYYY-MM-DD[ HH:MM]' (a date alone is stored at 00:00), indicator
    UDF_G0..UDF_G4 (as P6 stores the icons), code (short name or name of a UDF code). Formula UDFs
    are refused (P6 computes them). Dates and float are not affected (no F9 needed). Dry run unless
    confirm=True."""
    return _udf_next(_single_link(_udf_set, project, {"task_code": task_code, "udf": udf, "clear": bool(clear),
                                                      **({"value": value} if value is not None else {})}, confirm))


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_update_udfs(project: ProjectId, items: UdfEdits, confirm: bool = False) -> dict:
    """Set or clear many activity UDF values in one transaction (all or nothing; dry run unless
    confirm=True). Item: {task_code, udf, value} or {task_code, udf, clear: true}, as p6_update_udf.
    items: one report per item (udf before / after, or the error). One edit log entry per value."""
    items = _items_arg(items)
    with _write_session(confirm) as (con, backup):
        _project_id(con, project)

        def apply_one(i: int, it) -> dict:
            if not isinstance(it, Mapping):
                raise ValueError("an item must be an object")
            return _udf_set(con, project, it)

        b = _batch(con, items, apply_one)
        for rep, it in zip(b["items"], items, strict=False):
            rep["item"] = it
        return _udf_next(_batch_commit(con, b, confirm, f"Set {len(items)} activity UDF value(s) in {project}", backup))


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_update_activity_codes(project: ProjectId, items: CodeEdits, confirm: bool = False) -> dict:
    """Assign or clear activity code values in one transaction (all or nothing; dry run unless
    confirm=True). Item: {task_code, code_type, code_value} assigns the value (it replaces the activity's
    current value of that type: P6 keeps one per type), {task_code, code_type, clear: true} removes it.
    code_type: the type name as the project sees it (its own project codes, then its EPS codes, then
    global codes; case ignored). code_value: the code's short name, else its path (e.g. 'CON.MEC'), else
    its name; several matches are refused with their paths. items: one report per item (code type,
    scope, value before / after, or the error). One edit log entry per value. Codes do not move dates:
    no project is marked stale, no F9 needed (schedule_effect). At most 500 items."""
    items = _items_arg(items)
    with _write_session(confirm) as (con, backup):
        _project_id(con, project)
        b = _batch(con, items, lambda i, it: _code_set(con, project, it))
        for rep, it in zip(b["items"], items, strict=False):
            rep["item"] = it
        res = _batch_commit(con, b, confirm, f"Set {len(items)} activity code value(s) in {project}", backup)
        return _codes_next(res)


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_move_activities(project: ProjectId, items: WbsMoves, confirm: bool = False) -> dict:
    """Move activities to another WBS node of the same project in one transaction (all or nothing; dry
    run unless confirm=True). Item: {task_code, wbs}; wbs: the WBS code as p6_list_wbs shows it (with or
    without the leading Project ID, e.g. 'DEMO-A.CON' or 'CON'), else the WBS name when one node of the
    project has it. The project node itself is allowed (P6 keeps activities directly under the project);
    a WBS of another project is refused. items: one report per item (move: from_wbs, to_wbs,
    wbs_summaries = WBS summary activities whose span changes, or the error). Dates depend on the WBS only
    through WBS summary activities: the project is marked stale (F9) only when one spans the old or the
    new WBS (schedule_effect says which). WBS rollups follow at once; P6's summary tables only after Tools
    > Summarize (summaries_stale). One edit log entry per activity. At most 500 items."""
    items = _items_arg(items)
    with _write_session(confirm) as (con, backup):
        _project_id(con, project)
        trees: dict = {}
        pids: set = set()

        def apply_one(i: int, it) -> dict:
            x = _act_move(con, project, it, trees)
            pids.add(x["_pid"])
            return x

        b = _batch(con, items, apply_one)
        for rep, it in zip(b["items"], items, strict=False):
            rep["item"] = it
        sums = _summaries_stale(con, [(p, None, None) for p in sorted(pids)])
        word = "activity" if len(items) == 1 else "activities"
        res = _batch_commit(con, b, confirm, f"Move {len(items)} {word} to another WBS in {project}", backup,
                            tail=_sum_plan(sums))
        res["summaries_stale"] = sums
        return _moves_next(res, bool(b["projects"]))


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_delete_activity(project: ProjectId, task_code: ActivityId, force: bool = False, confirm: bool = False) -> dict:
    """Delete one activity and everything that belongs to it, as P6 does (hard delete; P6's triggers
    log REFRDEL): relationships both ways, resource assignments (with their period actuals, timesheet
    hours and codes), activity code assignments, UDF values, notebooks, steps, expenses, documents,
    risks, feedback, discussions and any other table of the schema with its task_id; an issue raised
    on it is kept with the activity cleared. Dry run unless confirm=True: lists what would be removed
    (would_remove: every relationship with type and lag; assignments, codes, UDFs, notebooks first 50;
    steps and expenses), rows per table (counts) and side_effects (open activities left without any
    predecessor or successor). Refused when the activity has an actual start or finish unless
    force=True (the dry run says would_refuse). Activities linked to it need F9."""
    con = _connect()
    try:
        t = _task(con, project, task_code)
        actual = {k: t[k] for k in ("act_start_date", "act_end_date") if t[k]}
    finally:
        con.close()
    reasons = ([f"{task_code} has actual dates {actual}: progress would be lost. Pass force=True to delete it anyway."]
               if actual and not force else [])
    if reasons and confirm:
        raise ValueError("Delete refused (nothing written). " + reasons[0])
    with _write_session(confirm) as (con, backup):
        t = _task(con, project, task_code)
        actual = {k: t[k] for k in ("act_start_date", "act_end_date") if t[k]}  # again, under the write lock
        if actual and not force and not reasons:
            reasons = [f"{task_code} has actual dates {actual}: progress would be lost. Pass force=True to delete it anyway."]
            if confirm:
                raise ValueError("Delete refused (nothing written). " + reasons[0])
        tid, pid = t["task_id"], t["proj_id"]
        links = [_rel_row_fn(con, pid)(r) for r in con.execute(_rel_sql(con, "both"), {"tid": tid})]
        ends = con.execute("SELECT task_id, pred_task_id FROM TASKPRED WHERE (task_id = ? OR pred_task_id = ?)"
                           + _alive(con, "TASKPRED"), (tid, tid)).fetchall()
        lost_pred = {r[0] for r in ends if r[1] == tid}  # its successors lose a predecessor
        lost_succ = {r[1] for r in ends if r[0] == tid}
        other_pids = {r[0] for r in con.execute(
            f"SELECT proj_id FROM TASK WHERE task_id IN ({','.join('?' * len(lost_pred | lost_succ)) or 'NULL'})",
            sorted(lost_pred | lost_succ))}
        details, cut = _act_details(con, tid)
        steps = ([r[0] for r in con.execute("SELECT proc_name FROM TASKPROC WHERE task_id = ?" + _alive(con, "TASKPROC")
                                            + " ORDER BY seq_num, proc_id", (tid,))]
                 if {"proc_name", "seq_num", "proc_id"} <= _cols(con, "TASKPROC") else [])
        expenses = ([{"name": r[0], "target_cost": r[1], "actual_cost": r[2]} for r in con.execute(
                     "SELECT cost_name, target_cost, act_cost FROM PROJCOST WHERE task_id = ?" + _alive(con, "PROJCOST")
                     + " ORDER BY cost_item_id", (tid,))]
                    if {"cost_name", "target_cost", "act_cost", "cost_item_id"} <= _cols(con, "PROJCOST") else [])
        warnings = []
        if _sched_options(con, pid)[0].get("key_activity_for_multiple_longest_paths") == str(tid):
            warnings.append("It is the key activity of the project's schedule options (multiple float paths): "
                            "pick another one in P6 or with p6_set_schedule_options.")
        snap = {k: t[k] for k in ACT_SNAPSHOT if k in _cols(con, "TASK")}
        counts, cleared, left = _act_delete_rows(con, [tid])
        if left:
            raise RuntimeError(f"Rows still naming the activity after the delete: {left} (nothing written)")
        side = _side_effects(con, lost_pred, lost_succ, gone={tid})
        npred = sum(1 for x in links if x["direction"] == "predecessor")
        plan = [f"Delete activity {task_code} '{t['task_name']}' ({TYPE_NAMES.get(t['task_type'], t['task_type'])}, "
                f"{STATUS_NAMES.get(t['status_code'], t['status_code'])}) from {project}",
                f"Relationships removed: {len(links)} ({npred} predecessors, {len(links) - npred} successors)",
                "Rows deleted: " + ", ".join(f"{k} {n}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))]
        if cleared:
            plan.append("Kept, activity cleared: " + ", ".join(f"{k} {n}" for k, n in cleared.items()))
        if actual:
            plan.append(f"Actual dates {actual} deleted with it (force=True)" if force else f"Note: actual dates {actual}")
        if side["count"]:
            plan.append(f"Left without predecessor: {len(side['no_predecessor'])}, without successor: "
                        f"{len(side['no_successor'])} (side_effects)")
        if backup:
            plan.append(f"Backup: {backup}")
        log = [{"operation": "delete_activity", "proj_id": pid, "table": "TASK",
                "keys": {"task_id": tid, "task_code": task_code},
                "before": {"task": snap, "rows": counts, "cleared": cleared,
                           "relationships": [{k: x[k] for k in ("direction", "task_pred_id", "other_code", "other_project",
                                                                "type", "lag_h")} for x in links]},
                "after": None, "summary": plan[0]}]
        res = _commit(con, plan, confirm, projects={pid} | other_pids, log=log)
        res.update(activity={"task_code": task_code, "task_name": t["task_name"], "task_type": t["task_type"],
                             "status_code": t["status_code"], "actual": actual or None},
                   would_remove={"relationships": links, **details, "steps": steps, "expenses": expenses},
                   counts={k: {"deleted": n} for k, n in counts.items()}, cleared=cleared, side_effects=side,
                   warnings=warnings)
        if cut:
            res["details_note"] = f"Only the first {ACT_LINKS_CAP} rows listed of: {', '.join(cut)} (counts has all)."
        if not confirm:
            res.update(would_refuse=bool(reasons), refuse_reasons=reasons)
            if reasons:
                res["next"] = "confirm=True would be refused: " + reasons[0]
        return res
