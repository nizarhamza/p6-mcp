"""Database administration tools: backups and restore, edit log, EPS, schedule options, project delete."""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timedelta
from time import monotonic
from typing import Optional, get_args

from p6mcp.core import state as _state
from p6mcp.core.state import _cols_cache, _engine_cache_clear, _schema_cache
from p6mcp.registry import (
    DateTimeStr, EpsOrder, LevelPriority, Limit, Offset, _plan_gate, ProjectId, ProjectList, SchedValues, _tool,
)
from p6mcp.core.db import (
    _alive, _backup, _backup_dir, _backup_file, BACKUP_NAME, _backup_taken_at, _check_page, _cols, _connect, _fmt_dt,
    _guard_write, _now_str, _parse_dt, _project_id, _projects_of, _ro_uri, _slice, _stamp_cols, _update, _write_refusal,
)
from p6mcp.core.props import (
    CHECK_SPEC, _lpl_arg, _norm_opt, _opt_rows, _project_row, _props, _sched_write, _set_props, _task_id_by_code,
    _xer_project, _xer_sched,
)
from p6mcp.core.editlog import _log_dir, _log_entries, _log_ts, _log_write, _schedule_state, _undone_fn
from p6mcp.core.wbs_codes import _eps_nodes, _eps_tree, _eps_wbs_id
from p6mcp.core.writes import (
    _commit, _create_eps, _cross_links, DEL_CLEAR, _del_sets, _delete_rows, _delete_targets, _empty_eps, _eps_ancestors,
    _orphans, _write_session,
)
from p6mcp.core.xer import _xer_read
from p6mcp.tools.database import p6_schedule_options


@_tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
def p6_backup_database() -> dict:
    """Take a manual backup copy of the P6 database now (kept apart from the automatic retention)."""
    why = _write_refusal()
    if why:
        raise RuntimeError(why)
    return {"backup": _backup("manual")}


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_edit_log(project: Optional[ProjectId] = None, since: Optional[DateTimeStr] = None,
                offset: Offset = 0, limit: Limit = 50) -> dict:
    """Confirmed writes of this server on this database, newest first, from the persistent edit log
    (p6_mcp_logs/edits_YYYYMMDD.jsonl next to the database, kept across restarts): {total, offset,
    returned, next_offset, rows, log_dir}. Row: ts, tool, operation, project, table, keys (task_id,
    task_pred_id...), before, after (changed values as stored: hours, codes), summary, backup in
    force, commit (one id per confirmed call), stale_projects, undone (made after a backup that
    p6_restore_backup copied back). project= keeps its entries (also where it is a stale project;
    restores are always listed) and adds schedule_state; since='YYYY-MM-DD[ HH:MM]' keeps entries
    at or after it."""
    offset, limit = _check_page(offset, limit)
    lo = _fmt_dt(_parse_dt(since, "since")[0]) if since else None
    es = _log_entries()
    undone = _undone_fn(es)
    state, keep = None, None
    if project:
        con = _connect()
        try:
            pid = _project_id(con, project)
            state = _schedule_state(con, pid)
        except ValueError:
            pid = None  # deleted or renamed: match the name recorded in the log
        finally:
            con.close()
        key = None if pid is None else str(pid)
        keep = lambda e: (e.get("operation") == "restore_backup" or (pid is not None and e.get("proj_id") == pid)  # noqa: E731
                          or (key is not None and key in (e.get("stale") or {}))
                          or e.get("project") == project or project in (e.get("stale_projects") or ()))
    rows = [{**{k: e.get(k) for k in ("ts", "tool", "operation", "project", "table", "keys", "before", "after",
                                      "summary", "backup", "commit", "stale_projects")}, "undone": undone(e)}
            for e in reversed(es) if (keep is None or keep(e)) and (lo is None or e["ts"] >= lo)]
    res = _slice(rows, offset, limit)
    res["log_dir"] = _log_dir()
    if state is not None:
        res["schedule_state"] = state
    return res


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_list_backups(offset: Offset = 0, limit: Limit = 200) -> dict:
    """Backup files (.db) in the backup folder, newest first: {total, offset, returned, next_offset,
    rows, folder}. Row: name (pass it to p6_restore_backup), taken_at (from the file name, else the
    file time), size_bytes, size_mb, kind ('backup', 'pre_restore' = safety copy taken by a
    restore), this_database (named after the current database file)."""
    folder = _backup_dir()
    stem = os.path.splitext(os.path.basename(_state.DB_PATH))[0].lower()
    rows = []
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if not name.lower().endswith(".db") or not os.path.isfile(path):
                continue
            m = BACKUP_NAME.search(name)
            size = os.path.getsize(path)
            rows.append({"name": name, "taken_at": _backup_taken_at(path), "size_bytes": size,
                         "size_mb": round(size / 1048576, 1), "kind": (m and m[2]) or "backup",
                         "this_database": name.lower().startswith(stem + "_")})
    rows.sort(key=lambda r: (r["taken_at"], r["name"]), reverse=True)
    res = _slice(rows, offset, limit)
    res["folder"] = folder
    return res


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False})
def p6_restore_backup(name: str, confirm: bool = False) -> dict:
    """Replace the database with a backup from the backup folder (name as p6_list_backups shows it).
    Dry run: the backup's projects compared with the current ones (only_in_backup, only_in_database,
    changed data date or last schedule date). confirm=True: refused while P6 is running; checks the
    backup (PRAGMA quick_check), takes a safety backup of the current database (kind pre_restore),
    copies the backup in with the SQLite backup API and logs the restore in the edit log (logged
    edits made after the backup was taken become undone). Afterwards open P6 and check the projects."""
    why = _write_refusal()
    if why:
        raise RuntimeError(why)
    path = _backup_file(name)
    taken = _backup_taken_at(path)
    # the copy's end in milliseconds (file time) when it matches the name: a commit made earlier in the same
    # second as the backup is in it, a commit made after it is undone by the restore
    mt = datetime.fromtimestamp(os.path.getmtime(path))
    copied = (mt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
              if timedelta(0) <= mt - datetime.strptime(taken, "%Y-%m-%d %H:%M:%S") < timedelta(minutes=10) else taken)
    cur, old = _projects_of(_state.DB_PATH), _projects_of(path)
    changed = sorted(p for p in set(cur) & set(old) if cur[p] != old[p])
    diff = {"only_in_backup": sorted(set(old) - set(cur))[:50], "only_in_database": sorted(set(cur) - set(old))[:50],
            "changed": [{"project": p, "database": {"data_date": cur[p][0], "last_schedule_date": cur[p][1]},
                         "backup": {"data_date": old[p][0], "last_schedule_date": old[p][1]}} for p in changed[:50]],
            "counts": {"backup_projects": len(old), "database_projects": len(cur),
                       "only_in_backup": len(set(old) - set(cur)), "only_in_database": len(set(cur) - set(old)),
                       "changed": len(changed)}}
    if os.path.splitext(name)[0].lower().startswith(os.path.splitext(os.path.basename(_state.DB_PATH))[0].lower() + "_"):
        warn = []
    else:
        warn = [f"{name} is not named after {os.path.basename(_state.DB_PATH)}: it may come from another database"]
    plan = [f"Restore {name} (taken {taken}, {os.path.getsize(path) / 1048576:.1f} MB) over {_state.DB_PATH}",
            f"Projects: {diff['counts']['only_in_backup']} only in the backup, {diff['counts']['only_in_database']} only "
            f"in the database (lost), {diff['counts']['changed']} with another data date or last schedule date",
            "A safety backup of the current database is taken first."]
    base = {"backup": {"name": name, "taken_at": taken, "size_bytes": os.path.getsize(path)}, "projects": diff,
            "warnings": warn}
    mismatch = _plan_gate(plan, confirm)
    if mismatch:
        return {"applied": False, "error": mismatch, **base}
    if not confirm:
        return {"dry_run": True, "would_do": plan, **base,
                "next": "Call again with confirm=True to restore"
                        + (" (the local working copy only)." if _state.SETTINGS.eppm else " (P6 must be closed).")}
    err = _guard_write()
    if err:
        raise RuntimeError(err)
    src = sqlite3.connect(_ro_uri(path), uri=True, timeout=5)
    try:
        bad = src.execute("PRAGMA quick_check(5)").fetchall()
        if [r[0] for r in bad] != ["ok"]:
            raise ValueError(f"Backup {name} fails PRAGMA quick_check: {[r[0] for r in bad]} (nothing restored)")
        safety = _backup("pre_restore")
        err = _guard_write()  # P6 may have been opened during the safety copy
        if err:
            raise RuntimeError(err + f" Nothing restored (safety backup {safety} kept).")
        dst = sqlite3.connect(_state.DB_PATH, timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    _state._backup_done = False  # the next write backs up the restored state
    _cols_cache.clear()
    _schema_cache.clear()
    _engine_cache_clear()
    entry = {"ts": _log_ts(), "commit": uuid.uuid4().hex[:12], "db": os.path.basename(_state.DB_PATH),
             "tool": "p6_restore_backup", "operation": "restore_backup", "project": None, "proj_id": None,
             "table": None, "keys": {"backup": name}, "before": None, "after": None, "backup_taken_at": taken,
             "backup_copied_at": copied,
             "summary": plan[0], "change": plan[0][:160], "backup": safety, "stale": {}, "stale_projects": []}
    res = {"applied": True, "done": plan + [f"Safety backup: {safety}"], **base, "safety_backup": safety,
           "next": "Open P6 and check the projects (press F9 where the data needs it)."}
    try:
        res["edit_log"] = {"file": _log_write([entry]), "entries": 1}
    except OSError as e:
        res["edit_log"] = {"error": f"edit log not written: {e}"}
    return res


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_list_eps(order: EpsOrder = "seq", offset: Offset = 0, limit: Limit = 200) -> dict:
    """EPS nodes, one page: {total, offset, returned, next_offset, rows}. eps_id is the short name
    used by p6_import_xer and p6_create_eps; parent_wbs_id links a node to its parent EPS; depth (0 = top
    level) and path (EPS names from the top, joined with ' > '), projects (projects directly under the
    node) and projects_subtree (under the node and every EPS below it); baselines and template projects
    (status WS_Template) are not counted. order: 'seq' (P6 sequence, then EPS ID) or 'tree' (each node
    before its children, siblings in 'seq' order)."""
    if order not in get_args(EpsOrder):
        raise ValueError(f"order must be one of {list(get_args(EpsOrder))}")
    con = _connect()
    con.execute("BEGIN")  # nodes, tree and counts from one snapshot
    try:
        nodes, tree = _eps_nodes(con), _eps_tree(con)
        status = "w.status_code" if "status_code" in _cols(con, "PROJWBS") else "NULL"
        direct = Counter(r[0] for r in con.execute(
            "SELECT w.parent_wbs_id FROM PROJECT p JOIN PROJWBS w ON w.proj_id = p.proj_id AND w.proj_node_flag = 'Y'"
            + _alive(con, "PROJWBS", "w") + " WHERE COALESCE(p.project_flag, 'Y') = 'Y' AND p.orig_proj_id IS NULL"
            + _alive(con, "PROJECT", "p") + f" AND {status} IS NOT 'WS_Template'"))
    finally:
        con.rollback()
    sub = {n["wbs_id"]: direct.get(n["wbs_id"], 0) for n in tree}
    for n in reversed(tree):  # children come after their parent: add them up from the bottom
        if n["parent_wbs_id"] in sub:
            sub[n["parent_wbs_id"]] += sub[n["wbs_id"]]
    tree = [{**n, "projects": direct.get(n["wbs_id"], 0), "projects_subtree": sub[n["wbs_id"]]} for n in tree]
    if order == "seq":
        at = {n["wbs_id"]: n for n in tree}
        tree = [at[n["wbs_id"]] for n in nodes if n["wbs_id"] in at]
    return _slice(tree, offset, limit)


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_create_eps(eps_id: str, eps_name: str, parent_eps: Optional[str] = None,
                  confirm: bool = False) -> dict:
    """Create an EPS node (top level, or under parent_eps). Dry run unless confirm=True.
    Check the result in P6 (Enterprise > EPS) after the first use."""
    with _write_session(confirm) as (con, backup):
        if any(n["eps_id"] == eps_id for n in _eps_nodes(con)):
            return {"applied": False, "error": f"EPS {eps_id!r} already exists."}
        if con.execute("SELECT 1 FROM PROJECT WHERE proj_short_name = ?" + _alive(con, "PROJECT"), (eps_id,)).fetchone():
            return {"applied": False, "error": f"{eps_id!r} is already a Project ID: choose another EPS ID."}
        parent = _eps_wbs_id(con, parent_eps) if parent_eps else None
        _create_eps(con, eps_id, eps_name, parent, _now_str(con))
        plan = [f"Create EPS {eps_id} '{eps_name}'" + (f" under {parent_eps}" if parent_eps else " (top level)")]
        if backup:
            plan.append(f"Backup: {backup}")
        return _commit(con, plan, confirm, log=[{"operation": "create_eps", "table": "PROJECT", "keys": {"eps_id": eps_id},
                                                 "after": {"eps_name": eps_name, "parent_eps": parent_eps},
                                                 "summary": plan[0]}])


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_set_schedule_options(project: ProjectId, options: Optional[SchedValues] = None,
                            from_xer: Optional[str] = None, xer_project: Optional[str] = None,
                            key_activity: Optional[str] = None, level_priority_list: Optional[LevelPriority] = None,
                            check_options: Optional[SchedValues] = None, confirm: bool = False) -> dict:
    """Write a project's schedule options (P6 Tools > Schedule > Options, stored in PROJPROP
    'scheduling'). Dry run unless confirm=True; the result lists old and new values, changes first.
    from_xer: take every option from that XER's SCHEDOPTIONS (xer_project picks the project); the
    XER key activity is mapped by Activity ID, -99 (none) with a warning when it has no match.
    options: {name: value} with the names p6_schedule_options shows (Y/N flags, numbers, enumerations
    such as sched_float_type FT_FF/FT_SS/FT_Min, sched_calendar_on_relationship_lag rcal_Predecessor/
    rcal_Successor/rcal_24Hour/rcal_ProjDefault, sched_outer_depend_type SD_Both/SD_None), applied
    after from_xer. key_activity: Activity ID of the key activity for multiple float paths ('none'
    clears it). level_priority_list: leveling priorities, e.g. ['priority_type:ASC'].
    check_options: Check Schedule thresholds (PROJPROP 'schedule_check', read by p6_health_check),
    e.g. {'logic_target': 5, 'check_cpli': 'N', 'large_float_constraint': 352} (limits in hours).
    Options not given keep their stored value (P6 defaults for a project that stores none); keys this
    server does not know are kept. With from_xer, a confirmed run returns verify (the
    p6_schedule_options summary, 0 different expected). Open P6, check the options, then press F9."""
    if all(v is None for v in (options, from_xer, key_activity, level_priority_list, check_options)):
        raise ValueError("give options, from_xer, key_activity, level_priority_list or check_options")
    opts = {str(k).strip().lower(): v for k, v in (options or {}).items()}
    lpl = _lpl_arg(level_priority_list)
    if "levelprioritylist" in opts:
        if lpl is not None:
            raise ValueError("give the level priority list once: level_priority_list or options['levelprioritylist']")
        lpl = _lpl_arg(str(opts.pop("levelprioritylist")).split(";"))
    chk_in = {str(k).strip().lower(): v for k, v in (check_options or {}).items()}
    xer = None
    if from_xer:
        _, x = _xer_read(from_xer)
        xp = _xer_project(x, xer_project)
        if not _xer_sched(x, xp):
            raise ValueError(f"{from_xer} has no SCHEDOPTIONS row for project {xp['proj_short_name']}")
        xer = (x, xp)
    with _write_session(confirm) as (con, backup):
        pid = _project_row(con, project)["proj_id"]
        now = _now_str(con)
        xarg = None
        if xer:
            xcode = {t["task_id"]: t["task_code"] for t in xer[0].get("TASK", []) if t.get("proj_id") == xer[1]["proj_id"]}
            xarg = (*xer, lambda xid: _task_id_by_code(con, pid, xcode.get(xid)))
        sched, warnings = _sched_write(con, pid, xarg, opts, key_activity, lpl, now)
        chk = None
        if chk_in:
            stored = (_props(con, pid, "schedule_check") or {}).get("attrs", {})
            bad = [k for k in chk_in if k not in CHECK_SPEC and k not in stored]
            if bad:
                raise ValueError(f"Unknown check_options {bad}: {sorted(CHECK_SPEC)}")
            chk = _set_props(con, pid, "schedule_check", {k: _norm_opt("schedule_check", k, v)[0]
                                                          for k, v in chk_in.items()}, None, now)
        plan, counts, shown = [], Counter(), {}
        for label, prop, r in (("Schedule options", "scheduling", sched),
                               ("Check Schedule thresholds", "schedule_check", chk)):
            if r is None:
                continue
            counts[r["action"]] += 1
            shown[prop] = _opt_rows(con, r["options"])
            ch = [o for o in shown[prop] if o["changed"]]
            plan.append(f"{label} of {project} (PROJPROP {prop}): {len(ch)} of {len(r['options'])} values change, "
                        f"row {r['action']}")
            plan += [f"  {o['option']}: {o['old']} -> {o['new']}" for o in ch]
        if xer:
            plan.append(f"Source: {os.path.basename(from_xer)} (project {xer[1]['proj_short_name']}), then the "
                        "options given")
        if backup:
            plan.append(f"Backup: {backup}")
        log = [{"operation": "set_schedule_options", "proj_id": pid, "table": "PROJPROP",
                "keys": {"proj_id": pid, "prop_name": prop},
                "before": {o["option"]: o["old"] for o in rows if o["changed"]},
                "after": {o["option"]: o["new"] for o in rows if o["changed"]},
                "summary": f"PROJPROP {prop}: {sum(o['changed'] for o in rows)} values change"}
               for prop, rows in shown.items()]
        res = _commit(con, plan, confirm, projects=(pid,) if sched and sched["action"] != "unchanged" else (),
                      log=log)
        res.update(counts={"PROJPROP": dict(counts)}, warnings=warnings,
                   options=shown.get("scheduling"), check_options=shown.get("schedule_check"))
        if res.get("applied"):
            if xer:
                res["verify"] = p6_schedule_options(project, from_xer, xer_project)["summary"]
            res["next"] = "Open P6, check Tools > Schedule > Options, then press F9."
        return res


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_delete_project(project: ProjectList, include_baselines: bool = True, allow_templates: bool = False,
                      delete_empty_eps: bool = False, confirm: bool = False) -> dict:
    """Delete one or more projects (Project IDs, a list deletes them in one transaction with one
    backup) and everything that belongs to them, as P6 does: WBS, activities, relationships (links to
    or from other projects too, listed in cross_project_links), assignments, activity code
    assignments, project activity code types and codes, UDF values, notebooks, steps, issues,
    thresholds, documents, PROJPROP rows, project calendars and every other table with a proj_id,
    task_id, wbs_id or assignment id of the project. Rows are hard-deleted like a P6 GUI delete
    (P6's triggers log them in REFRDEL). Dry run unless confirm=True: it performs the delete in a
    transaction, returns rows per table, and rolls back.
    include_baselines: also delete the baselines of these projects (False: they are detached and
    become ordinary projects in the same EPS). Projects whose project baseline is deleted lose that
    link (sum_base_proj_id cleared). Templates (WS_Template) are refused unless allow_templates=True.
    delete_empty_eps: remove the EPS nodes above these projects that are left without any project
    or child EPS (eps.would_delete). orphan_check compares rows pointing to missing parents before
    and after (new must be 0); new orphans or rows of other projects (other than links) refuse the
    confirmed run. Close P6 first; activities of other projects linked to these need F9."""
    names = [project] if isinstance(project, str) else list(dict.fromkeys(project or []))
    if not names or not all(isinstance(n, str) and n for n in names):
        raise ValueError("project: a Project ID or a list of Project IDs")
    con = _connect()
    try:  # names checked before any lock or backup
        _delete_targets(con, names, include_baselines, allow_templates)
    finally:
        con.close()
    with _write_session(confirm) as (con, backup):
        t0 = monotonic()
        named, bls = _delete_targets(con, names, include_baselines, allow_templates)
        targets = named + (bls if include_baselines else [])
        pids = [p["proj_id"] for p in targets]
        before = _orphans(con)
        eps_cand = _eps_ancestors(con, pids) if delete_empty_eps else []
        _del_sets(con, pids)
        cross = _cross_links(con)
        plan = [f"Delete {len(named)} project(s): " + ", ".join(f"{p['project']} ({p['activities']} activities)"
                                                             for p in named)]
        cleared, warnings = Counter(), []
        if bls and include_baselines:
            plan.append(f"Delete {len(bls)} baseline(s): " + ", ".join(f"{b['project']} of {b['of']}" for b in bls))
        elif bls:
            now = _now_str(con)
            for b in bls:
                node = con.execute("SELECT w.wbs_id, w.parent_wbs_id, o.parent_wbs_id FROM PROJWBS w JOIN PROJECT bp ON "
                                   "bp.proj_id = w.proj_id JOIN PROJWBS o ON o.proj_id = bp.orig_proj_id AND "
                                   "o.proj_node_flag = 'Y' WHERE w.proj_id = ? AND w.proj_node_flag = 'Y'",
                                   (b["proj_id"],)).fetchone()
                _update(con, "PROJECT", {"proj_id": b["proj_id"]},
                        {"orig_proj_id": None, **_stamp_cols(con, "PROJECT", now, create=False)})
                if node and (node[1] is None or node[1] in {r[0] for r in con.execute(
                        "SELECT id FROM temp._dw")} or not con.execute("SELECT 1 FROM PROJWBS WHERE wbs_id = ?",
                                                                       (node[1],)).fetchone()):
                    _update(con, "PROJWBS", {"wbs_id": node[0]}, {"parent_wbs_id": node[2]})
                cleared["PROJECT.orig_proj_id"] += 1
            plan.append(f"Keep {len(bls)} baseline(s) as ordinary projects (include_baselines=False): "
                        + ", ".join(b["project"] for b in bls))
        for t, c in DEL_CLEAR:
            if c in _cols(con, t):
                where = f"{c} IN (SELECT id FROM temp._dp)" + (" AND proj_id NOT IN (SELECT id FROM temp._dp)"
                                                               if "proj_id" in _cols(con, t) else "")
                n = con.execute(f"UPDATE {t} SET {c} = NULL WHERE {where}").rowcount
                if n:
                    cleared[f"{t}.{c}"] += n
        counts, foreign = _delete_rows(con, pids)
        if cross["count"]:
            plan.append(f"Delete {cross['count']} relationship(s) with activities of other projects")
        eps_rows, eps_left = [], None
        if delete_empty_eps:
            empty = _empty_eps(con, eps_cand)
            if empty:
                more, more_foreign = _delete_rows(con, [n["proj_id"] for n in empty])
                for t, n in more.items():
                    counts[t] = counts.get(t, 0) + n
                for t, n in more_foreign.items():
                    foreign[t] = foreign.get(t, 0) + n
                eps_rows = [n["eps_id"] for n in empty]
                plan.append(f"Delete {len(empty)} EPS node(s) left empty: {', '.join(eps_rows)}")
            eps_left = len(_eps_nodes(con))
            if not eps_left:
                warnings.append("No EPS node is left: p6_import_xer needs create_eps= for a new project, and P6 "
                                "asks for an EPS before creating one.")
        after = _orphans(con)
        new = {k: n - before.get(k, 0) for k, n in after.items() if n > before.get(k, 0)}
        foreign_bad = {t: n for t, n in foreign.items() if t != "TASKPRED"}
        reasons = []
        if new:
            reasons.append(f"The delete would leave rows pointing to deleted rows: {new}")
        if foreign_bad:
            reasons.append(f"The delete would remove rows of other projects: {foreign_bad} (for example project "
                           "calendars or code types used elsewhere): fix them in P6 first")
        plan.append("Rows deleted: " + ", ".join(f"{t} {n}" for t, n in sorted(counts.items(), key=lambda kv: -kv[1])))
        if cleared:
            plan.append("References cleared: " + ", ".join(f"{k} {n}" for k, n in cleared.items()))
        if backup:
            plan.append(f"Backup: {backup}")
        extra = {"projects": named, "baselines": {"included": include_baselines, "rows": bls},
                 "counts": {t: {"deleted": n} for t, n in counts.items()}, "cleared": dict(cleared),
                 "cross_project_links": cross,
                 "eps": {"would_delete" if not confirm else "deleted": eps_rows, "eps_nodes_left": eps_left}
                 if delete_empty_eps else None,
                 "orphan_check": {"new": new, "total_new": sum(new.values()), "existing_before": before,
                                  "note": "rows whose proj_id, task_id, wbs_id, assignment, code, calendar or UDF "
                                          "fk_id names a missing row; existing_before were there already"},
                 "warnings": warnings, "seconds": round(monotonic() - t0, 1)}
        if reasons and confirm:
            raise ValueError("Delete refused (nothing written). " + " ".join(reasons))
        log = [{"operation": "delete_project", "proj_id": p["proj_id"], "project": p["project"], "table": "PROJECT",
                "keys": {"proj_id": p["proj_id"]}, "before": {"activities": p.get("activities")}, "after": None,
                "summary": plan[0][:300]} for p in targets]
        res = _commit(con, plan, confirm, projects=cross["other_proj_ids"], log=log)
        res.update(extra)
        if not confirm:
            res.update(would_refuse=bool(reasons), refuse_reasons=reasons)
            if reasons:
                res["next"] = "confirm=True would be refused: " + " ".join(reasons)
        elif res.get("applied"):
            res["next"] = "Open P6: the projects are gone. Reschedule (F9) the projects listed in cross_project_links."
        return res
