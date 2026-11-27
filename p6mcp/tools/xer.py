"""XER tools: import, inspect, export."""

from __future__ import annotations

import os
from contextlib import closing
from typing import Optional, get_args

from p6mcp.registry import ImportMode, NewEps, ProjectId, Removable, _tool, XerTable
from p6mcp.core.db import _alive, _connect, _db_dt, _fmt_dt, _ids_sql, _insert, _next_key, _prefer, _stamp_cols
from p6mcp.core.props import (
    _db_task_code, _lag_rule, _opt_rows, _sched_diff, _sched_options, _sched_write, _xer_project, _xer_sched,
)
from p6mcp.core.wbs_codes import _eps_nodes, _eps_wbs_id, _guid
from p6mcp.core.writes import _commit, _create_eps, _write_session
from p6mcp.core.xer import (
    _export_target, _export_xer, _import_target, _missing_rows, _removable, _remove_rows, UDF_FK_TABLES, _upsert,
    XER_CHILD_NEEDS, XER_EXPORT_TABLES, _xer_only, _xer_preflight, _xer_read_meta, XER_TABLES, _XerImport,
)


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False})
def p6_import_xer(
    path: str,
    mode: ImportMode = "new",
    project: Optional[ProjectId] = None,
    new_short_name: Optional[str] = None,
    eps: Optional[str] = None,
    baseline_type: Optional[str] = None,
    set_as_project_baseline: bool = False,
    xer_project: Optional[str] = None,
    allow_older_data_date: bool = False,
    remove_missing: Optional[list[Removable]] = None,
    import_schedule_options: Optional[bool] = None,
    create_eps: Optional[NewEps] = None,
    confirm: bool = False,
) -> dict:
    """Import a P6 XER file. Dry run (full import then rollback, returns the counts) unless confirm=True.

    mode:
      new       Create a new project (also for a standalone baseline copy). eps = EPS id
                (p6_list_eps); new_short_name renames it (required if the project ID already exists).
      update    Update an existing project: `project` (default: the XER project ID). WBS matched by
                path, activities by Activity ID, relationships by pred/succ/type, assignments by
                activity + resource, codes and UDFs by type. Matches are updated, new items inserted.
                missing_from_xer reports the project's activities, relationships and assignments
                that are not in the XER. Nothing is deleted unless remove_missing lists 'TASKPRED'
                and/or 'TASKRSRC'; activities are never deleted. An XER whose data date is older
                than the project's is refused unless allow_older_data_date=True.
      baseline  Create a baseline attached to existing `project` (stored like P6 baselines,
                hidden from the EPS). new_short_name names it (default: the XER project ID).
                baseline_type = a BASETYPE name; set_as_project_baseline makes it the project baseline.

    import_schedule_options: write the XER SCHEDOPTIONS into the project's schedule options
    (PROJPROP scheduling, as p6_set_schedule_options from_xer; the key activity is mapped to the
    imported activity, -99 with a warning when the XER has no such activity). Default True for new
    and update, False for baseline; when off, the options that differ are listed in warnings.
    create_eps {eps_id, eps_name, parent_eps?} (mode new): the EPS node to put the project in,
    created in the same transaction when it does not exist (reused when it does). Without it, a
    database with no EPS node makes the dry run set would_refuse and confirm=True refuse.

    The file is checked first (preflight, as p6_inspect_xer): with pre-flight errors, or an older
    data date not allowed, confirm=True is refused before anything is locked or backed up, and the
    dry run sets would_refuse and refuse_reasons (it imports nothing when the XER has no project WBS
    node). The target of update and baseline must be a project, not an EPS node. data_date shows
    database -> XER.
    Global data (currencies, OBS, UDF types, calendars, resources, code types and codes, notebook
    topics, expense categories, roles and risk types) is matched by name and reused; missing items
    are created. Resource and role rates are only imported for new resources and roles. Notebooks,
    steps, expenses and risks are imported with the activities (update mode: matched by activity and
    topic, step name, expense name or Risk ID; the ones missing from the XER are kept).
    xer_project selects the project when the XER contains several. Press F9 in P6 afterwards."""
    if mode not in get_args(ImportMode):
        raise ValueError("mode must be new, update or baseline")
    remove = _removable(remove_missing, mode)
    new_eps = None
    if create_eps is not None:
        if mode != "new":
            raise ValueError("create_eps only applies to mode='new'")
        new_eps = dict(create_eps)
        if not all(isinstance(new_eps.get(k), str) and new_eps[k].strip() for k in ("eps_id", "eps_name")):
            raise ValueError("create_eps needs eps_id and eps_name, e.g. {'eps_id': 'PV-01', 'eps_name': 'Solar PV'}")
        if eps is not None and eps != new_eps["eps_id"]:
            raise ValueError(f"eps={eps!r} and create_eps eps_id {new_eps['eps_id']!r} differ: give one")
    header, x, meta = _xer_read_meta(path)
    xp = _xer_project(x, xer_project)
    xpid = xp["proj_id"]
    x = _xer_only(x, xpid)  # rows of the chosen project and global rows
    con = _connect()
    try:  # read-only checks: a refused confirm takes no lock and no backup
        pf = _xer_preflight(header, x, meta, xer_project, con)
        target = _import_target(con, mode, project, xp)
        no_eps = mode == "new" and not _eps_nodes(con)
        if new_eps and new_eps.get("parent_eps") and not any(n["eps_id"] == new_eps["eps_id"] for n in _eps_nodes(con)):
            _eps_wbs_id(con, new_eps["parent_eps"])  # parent must exist
    finally:
        con.close()
    db_dd = _db_dt(target.get("last_recalc_date")) if mode == "update" else None
    x_dd = _db_dt(xp.get("last_recalc_date"))
    data_date = {"database": db_dd and _fmt_dt(db_dd), "xer": x_dd and _fmt_dt(x_dd),
                 "older": bool(db_dd and x_dd and x_dd < db_dd)}
    reasons = []
    if pf["errors"]:
        reasons.append("XER pre-flight failed (" + ", ".join(f"{e['check']} {e['count']}" for e in pf["errors"])
                       + "): fix the file, see preflight or p6_inspect_xer.")
    if no_eps and not new_eps:
        reasons.append("No EPS node in this database: pass create_eps={'eps_id': ..., 'eps_name': ...} to create one "
                       "in the same transaction (or p6_create_eps first).")
    if data_date["older"] and not allow_older_data_date:
        reasons.append(f"The XER data date {data_date['xer']} is older than the project's {data_date['database']}: "
                       "the update would move the data date back. Pass allow_older_data_date=True to import it anyway.")
    if reasons and confirm:
        raise ValueError("Import refused. " + " ".join(reasons))
    if any(e["check"] == "project_wbs_node_missing" for e in pf["errors"]):  # nothing to import into
        return {"dry_run": True, "would_do": [f"Import {os.path.basename(path)}: not possible, the XER has no "
                                              "project WBS node"], "counts": {}, "warnings": [], "data_date": data_date,
                "preflight": pf, "would_refuse": True, "refuse_reasons": reasons,
                "next": "confirm=True would be refused: " + " ".join(reasons)}

    with _write_session(confirm) as (con, backup):
        imp = _XerImport(con, x, mode)
        target = _import_target(con, mode, project, xp)
        short = new_short_name or xp["proj_short_name"]
        if mode != "update":
            if con.execute("SELECT 1 FROM PROJECT WHERE proj_short_name = ?" + _alive(con, "PROJECT"),
                           (short,)).fetchone():
                raise ValueError(f"Project ID {short!r} already exists: pass new_short_name=, "
                                 "or mode='update' to update it.")
            limit = _prefer(con, "proj_short_len") or 40
            if len(short) > limit:
                raise ValueError(f"Project ID is limited to {limit} characters in this database: pass a shorter "
                                 "new_short_name.")
        eps_txt = None
        if mode == "new":
            hit = new_eps and next((n for n in _eps_nodes(con) if n["eps_id"] == new_eps["eps_id"]), None)
            if hit:
                parent_wbs, eps_txt = hit["wbs_id"], f"EPS {hit['eps_id']} exists: used"
            elif new_eps:
                par = new_eps.get("parent_eps")
                parent_wbs = _create_eps(con, new_eps["eps_id"], new_eps["eps_name"],
                                         _eps_wbs_id(con, par) if par else None, imp.now)
                eps_txt = (f"Create EPS {new_eps['eps_id']} '{new_eps['eps_name']}'"
                           + (f" under {par}" if par else " (top level)"))
            elif not _eps_nodes(con):  # dry run only: confirm=True was refused above
                parent_wbs, eps_txt = None, "No EPS node: the project would have no parent (refused with confirm=True)"
            else:
                parent_wbs = _eps_wbs_id(con, eps)
        elif mode == "baseline":
            parent_wbs = con.execute("SELECT parent_wbs_id FROM PROJWBS WHERE proj_id = ? AND "
                                     "proj_node_flag = 'Y'", (target["proj_id"],)).fetchone()[0]

        # ---- global data, matched by name
        imp.match_global("CURRTYPE", x.get("CURRTYPE", []))
        imp.match_global("FINTMPL", x.get("FINTMPL", []))
        imp.match_global("OBS", x.get("OBS", []), parent_col="parent_obs_id")
        imp.match_global("UDFTYPE", x.get("UDFTYPE", []))
        for t in ("MEMOTYPE", "COSTTYPE", "ROLES", "RISKTYPE"):  # notebook topics, expense categories, roles
            if x.get(t) and imp.can(t):
                imp.match_global(t, x[t])

        # ---- project id first: project calendars and code types belong to it
        if mode == "update":
            pid = target["proj_id"]
        else:
            pid = _next_key(con, "PROJECT", "proj_id")
        imp.idmap["PROJECT"][xpid] = pid

        cals = x.get("CALENDAR", [])
        imp.match_global("CALENDAR", [c for c in cals if c.get("proj_id") is None], parent_col="base_clndr_id")
        proj_cal_sql = ("SELECT clndr_id FROM CALENDAR WHERE proj_id = ? AND clndr_name = ?"
                        if mode == "update" else None)
        imp.match_or_create("CALENDAR", [c for c in cals if c.get("proj_id") is not None],
                            lambda r: (pid, r["clndr_name"]), proj_cal_sql, parent_col="base_clndr_id")

        # ---- project row
        if mode == "update":
            imp.upd_row("PROJECT", pid, xp, {"proj_short_name": target["proj_short_name"],
                                             "orig_proj_id": target.get("orig_proj_id"),
                                             "sum_base_proj_id": target.get("sum_base_proj_id"),
                                             "project_flag": "Y"})
            imp.flush()
        else:
            extra = {"proj_short_name": short, "project_flag": "Y", "proj_id": pid,
                     "guid": _guid(), "sum_base_proj_id": None, "orig_proj_id": None}
            if mode == "baseline":
                extra["orig_proj_id"] = target["proj_id"]
                if baseline_type:
                    bt = con.execute("SELECT base_type_id FROM BASETYPE WHERE base_type = ?",
                                     (baseline_type,)).fetchone()
                    if not bt:
                        raise ValueError(f"Baseline type not found: {baseline_type!r}: "
                                         f"{[r[0] for r in con.execute('SELECT base_type FROM BASETYPE')]}")
                    extra["base_type_id"] = bt[0]
            row = imp.prep("PROJECT", xp)
            row.update(extra)
            row.update(_stamp_cols(con, "PROJECT", imp.now, create=True))
            _insert(con, "PROJECT", row)
            imp.count("PROJECT", "inserted")
            if mode == "baseline" and set_as_project_baseline:
                con.execute("UPDATE PROJECT SET sum_base_proj_id = ? WHERE proj_id = ?",
                            (pid, target["proj_id"]))

        # ---- resources, code types, codes
        imp.match_global("RSRC", x.get("RSRC", []), parent_col="parent_rsrc_id")
        types = x.get("ACTVTYPE", [])
        imp.match_global("ACTVTYPE", [t for t in types if t.get("proj_id") is None])
        proj_type_sql = ("SELECT actv_code_type_id FROM ACTVTYPE WHERE proj_id = ? AND actv_code_type = ?"
                         if mode == "update" else None)
        imp.match_or_create("ACTVTYPE", [t for t in types if t.get("proj_id") is not None],
                            lambda r: (pid, r["actv_code_type"]), proj_type_sql)
        imp.match_or_create("ACTVCODE", x.get("ACTVCODE", []),
                            lambda r: (imp.idmap["ACTVTYPE"].get(r["actv_code_type_id"]), r["short_name"],
                                       imp.idmap["ACTVCODE"].get(r.get("parent_actv_code_id"))),
                            "SELECT actv_code_id FROM ACTVCODE WHERE actv_code_type_id = ? AND short_name = ? "
                            "AND parent_actv_code_id IS ?", parent_col="parent_actv_code_id")
        for t, owner, col in (("RSRCRATE", "RSRC", "rsrc_id"), ("ROLERATE", "ROLES", "role_id")):
            for r in (x.get(t, []) if t == "RSRCRATE" or imp.can(t) else []):  # rates of new resources and roles
                if r.get(col) in imp.created[owner]:
                    imp.new_row(t, r)
                else:
                    imp.count(t, "kept_existing")

        # ---- WBS: project node maps to the project node, others matched by path in update mode
        wbs = x.get("PROJWBS", [])
        by_id = {w["wbs_id"]: w for w in wbs}
        node = next((w for w in wbs if w.get("proj_node_flag") == "Y"), None)
        if node is None:
            raise ValueError("XER has no project WBS node.")
        if mode == "update":
            db_node = con.execute("SELECT wbs_id FROM PROJWBS WHERE proj_id = ? AND proj_node_flag = 'Y'"
                                  + _alive(con, "PROJWBS"), (pid,)).fetchone()[0]
            imp.idmap["PROJWBS"][node["wbs_id"]] = db_node
            imp.count("PROJWBS", "matched")
        else:
            imp.idmap["PROJWBS"][node["wbs_id"]] = imp.new_row(
                "PROJWBS", node, {"parent_wbs_id": parent_wbs, "wbs_short_name": short})

        def depth(w, n=0):
            p = by_id.get(w.get("parent_wbs_id"))
            return n if p is None or n > 100 else depth(p, n + 1)

        imp.flush()
        # update mode: the match queries' keys read once (_XerImport.index / find), inserted rows added
        wbs_sql = "SELECT wbs_id FROM PROJWBS WHERE proj_id = ? AND parent_wbs_id = ? AND wbs_short_name = ?" \
            + _alive(con, "PROJWBS")
        wbs_ix = imp.index("SELECT wbs_id, parent_wbs_id, wbs_short_name FROM PROJWBS WHERE proj_id = ?"
                           + _alive(con, "PROJWBS"), (pid,)) if mode == "update" else {}
        for w in sorted((w for w in wbs if w is not node), key=depth):
            parent = imp.idmap["PROJWBS"].get(w.get("parent_wbs_id"), imp.idmap["PROJWBS"][node["wbs_id"]])
            hit = None
            if mode == "update":
                hit = imp.find(wbs_ix, (parent, w["wbs_short_name"]), wbs_sql, (pid, parent, w["wbs_short_name"]))
            if hit:
                imp.idmap["PROJWBS"][w["wbs_id"]] = hit
                imp.upd_row("PROJWBS", hit, w, {"parent_wbs_id": parent, "proj_id": pid})
            else:
                new = imp.idmap["PROJWBS"][w["wbs_id"]] = imp.new_row("PROJWBS", w, {"parent_wbs_id": parent})
                if imp.idmap["PROJECT"].get(w.get("proj_id")) == pid:
                    wbs_ix.setdefault((parent, w["wbs_short_name"]), []).append(new)
        imp.flush()

        # ---- activities
        task_sql = "SELECT task_id FROM TASK WHERE proj_id = ? AND task_code = ?" + _alive(con, "TASK")
        task_ix = imp.index("SELECT task_id, task_code FROM TASK WHERE proj_id = ?" + _alive(con, "TASK"),
                            (pid,)) if mode == "update" else {}
        for t in x.get("TASK", []):
            hit = None
            if mode == "update":
                hit = imp.find(task_ix, (t["task_code"],), task_sql, (pid, t["task_code"]))
            if hit:
                imp.idmap["TASK"][t["task_id"]] = hit
                imp.upd_row("TASK", hit, t)
            else:
                new = imp.idmap["TASK"][t["task_id"]] = imp.new_row("TASK", t)
                if imp.idmap["PROJECT"].get(t.get("proj_id")) == pid:
                    task_ix.setdefault((t["task_code"],), []).append(new)
        imp.flush()
        # links, assignments and codes are read once for the project's activities (tids, soft-deleted ones too);
        # a row of another activity (a new one outside the project) is matched with its query as before
        own = "IN (SELECT task_id FROM TASK WHERE proj_id = ?)"
        tids = {r[0] for r in con.execute("SELECT task_id FROM TASK WHERE proj_id = ?", (pid,))}

        # ---- relationships (one link per pair and type: a repeated XER row is skipped)
        seen_links = set()
        pred_sql = "SELECT task_pred_id FROM TASKPRED WHERE task_id = ? AND pred_task_id = ? AND pred_type = ?" \
            + _alive(con, "TASKPRED")
        pred_ix = imp.index("SELECT task_pred_id, task_id, pred_task_id, pred_type FROM TASKPRED WHERE task_id "
                            + own + _alive(con, "TASKPRED"), (pid,)) if mode == "update" else {}
        for r in x.get("TASKPRED", []):
            succ, pred = imp.idmap["TASK"].get(r["task_id"]), imp.idmap["TASK"].get(r["pred_task_id"])
            if succ is None or pred is None:
                imp.count("TASKPRED", "skipped_external")
                continue
            if (succ, pred, r.get("pred_type")) in seen_links:
                imp.count("TASKPRED", "skipped_duplicate")
                continue
            seen_links.add((succ, pred, r.get("pred_type")))
            hit = None
            if mode == "update":
                hit = imp.find(pred_ix, (succ, pred, r["pred_type"]), pred_sql, (succ, pred, r["pred_type"]),
                               succ in tids)
            if hit:
                imp.upd_row("TASKPRED", hit, r)
                imp.touched["TASKPRED"].add(hit)
            else:
                new = imp.new_row("TASKPRED", r)
                imp.touched["TASKPRED"].add(new)
                pred_ix.setdefault((succ, pred, r["pred_type"]), []).append(new)
        imp.flush()

        # ---- assignments (a database row is matched once: touched holds matched and new ids)
        used = imp.touched["TASKRSRC"]
        asg_sql = "SELECT taskrsrc_id FROM TASKRSRC WHERE task_id = ? AND rsrc_id IS ?" + _alive(con, "TASKRSRC")
        asg_ix = imp.index("SELECT taskrsrc_id, task_id, rsrc_id FROM TASKRSRC WHERE task_id " + own
                           + _alive(con, "TASKRSRC"), (pid,)) if mode == "update" else {}
        for r in x.get("TASKRSRC", []):
            task, rsrc = imp.idmap["TASK"].get(r["task_id"]), imp.idmap["RSRC"].get(r.get("rsrc_id"))
            if task is None:
                imp.count("TASKRSRC", "skipped_unmapped")
                continue
            hit = None
            if mode == "update":
                free = [i for i in asg_ix.get((task, rsrc), ()) if i not in used]
                if len(free) > 1 or task not in tids:  # the database's row order decides, as before
                    imp.flush()
                    free = [h[0] for h in con.execute(asg_sql, (task, rsrc)) if h[0] not in used]
                hit = free[0] if free else None
            if hit:
                imp.idmap["TASKRSRC"][r["taskrsrc_id"]] = hit
                imp.upd_row("TASKRSRC", hit, r)
            else:
                hit = imp.idmap["TASKRSRC"][r["taskrsrc_id"]] = imp.new_row("TASKRSRC", r)
                asg_ix.setdefault((task, rsrc), []).append(hit)
            used.add(hit)
        imp.flush()

        # ---- risks, notebooks, steps and expenses (before the UDF values that may point to them)
        for t in ("PROJRISK", "TASKMEMO", "TASKPROC", "PROJCOST"):
            imp.children(t, x.get(t, []))
        if x.get("TASKRISK") and imp.can("TASKRISK"):
            for r in x["TASKRISK"]:
                row = imp.prep("TASKRISK", r)
                if any(row.get(c) is None for c in XER_CHILD_NEEDS["TASKRISK"]):
                    imp.count("TASKRISK", "skipped_unmapped")
                    continue
                _upsert(imp, "TASKRISK", row, ("task_id", "risk_id"))

        # ---- activity code assignments and UDF values (upsert on their composite keys)
        # keys present (soft-deleted rows too: an upsert revives them); rows of other activities checked one by one
        actv = (lambda k: k[0] in tids, {tuple(r) for r in con.execute(
            "SELECT task_id, actv_code_type_id FROM TASKACTV WHERE task_id " + own, (pid,))})
        for r in x.get("TASKACTV", []):
            row = imp.prep("TASKACTV", r)
            if None in (row.get("task_id"), row.get("actv_code_type_id"), row.get("actv_code_id")):
                imp.count("TASKACTV", "skipped_unmapped")
                continue
            _upsert(imp, "TASKACTV", row, ("task_id", "actv_code_type_id"), actv)
        imp.flush()
        udf_tables = {u["udf_type_id"]: u["table_name"] for u in x.get("UDFTYPE", [])}
        types = {v for v in imp.idmap["UDFTYPE"].values() if v is not None}
        udfv = (lambda k: k[0] in types, {tuple(r) for r in con.execute(
            f"SELECT udf_type_id, fk_id FROM UDFVALUE WHERE udf_type_id IN {_ids_sql(sorted(types))}")})
        for r in x.get("UDFVALUE", []):
            tbl = udf_tables.get(r["udf_type_id"])
            row = imp.prep("UDFVALUE", r)
            row["fk_id"] = imp.idmap.get(tbl, {}).get(r["fk_id"]) if tbl in UDF_FK_TABLES else None
            if row.get("udf_type_id") is None or row["fk_id"] is None:
                imp.count("UDFVALUE", "skipped_unmapped")
                continue
            _upsert(imp, "UDFVALUE", row, ("udf_type_id", "fk_id"), udfv)
        imp.flush()

        # ---- update: rows of the project missing from the XER, deleted only when asked (never activities)
        missing, gone_txt = None, []
        if mode == "update":
            missing, drop = _missing_rows(imp, pid)
            for t, label in (("TASKPRED", "relationships"), ("TASKRSRC", "assignments")):
                missing[t]["removed"] = t in remove and bool(drop[t])
                if missing[t]["removed"]:
                    _remove_rows(imp, t, drop[t])
                    gone_txt.append(f"Delete {len(drop[t])} {label} missing from the XER (remove_missing)")
                elif drop[t]:
                    gone_txt.append(f"Keep {len(drop[t])} {label} missing from the XER "
                                    f"(remove_missing=['{t}'] deletes them)")

        do_opts = (mode != "baseline") if import_schedule_options is None else bool(import_schedule_options)
        sched = None
        if x.get("SCHEDOPTIONS") and do_opts and _xer_sched(x, xp):
            sched, w = _sched_write(con, pid, (x, xp, imp.idmap["TASK"].get), {}, None, None, imp.now)
            imp.warnings += w
            imp.count("PROJPROP", sched["action"])
        elif x.get("SCHEDOPTIONS"):
            imp.warnings.append("SCHEDOPTIONS not imported (import_schedule_options False): check Schedule Options "
                                "in P6 before F9, or use p6_set_schedule_options from_xer.")
            if target:
                db_opts, lag_db = _sched_options(con, target["proj_id"])[0], _lag_rule(con, target["proj_id"])[0]
            else:
                db_opts, lag_db = {}, "rcal_Predecessor"
            xcodes = {t["task_id"]: t["task_code"] for t in x.get("TASK", [])}
            diff = [o for o in _sched_diff(db_opts, _xer_sched(x, xp), lambda v: _db_task_code(con, v), xcodes.get)
                    if o["same"] is False or (o["option"] not in db_opts and o["option"] != "levelprioritylist")]
            where = f"project {target['proj_short_name']}" if target else "the new project"
            if diff and db_opts:
                imp.warnings.append(f"XER schedule options that differ from {where}: " + "; ".join(
                    f"{o['option']} XER {o['xer']}, database {o['db'] if o['option'] in db_opts else 'not stored'}"
                    for o in diff))
            elif diff:
                imp.warnings.append(f"No schedule options stored for {where} (P6 defaults apply, lag rule "
                                    f"{lag_db}). XER options: " + "; ".join(f"{o['option']} {o['xer']}" for o in diff))
        skipped = sorted(set(x) - set(XER_TABLES) - {"SCHEDOPTIONS"})
        if skipped:
            imp.warnings.append(f"Tables not handled, ignored: {skipped}")

        target_txt = {"new": f"new project {short}", "baseline": f"baseline {short} of {target and target['proj_short_name']}",
                      "update": f"update of {target and target['proj_short_name']}"}[mode]
        plan = [f"Import {os.path.basename(path)} (P6 {header[1] if len(header) > 1 else '?'}, "
                f"project {xp['proj_short_name']}) as {target_txt}"]
        if eps_txt:
            plan.append(eps_txt)
        if sched:
            ch = [o for o in sched["options"] if o["changed"]]
            plan.append(f"Schedule options: XER SCHEDOPTIONS written to PROJPROP scheduling ({len(ch)} values "
                        f"change, row {sched['action']})")
        if mode == "update":
            plan.append(f"Data date: database {data_date['database']} -> XER {data_date['xer']}" + (
                (" (older, allowed)" if allow_older_data_date else " (older)") if data_date["older"] else ""))
            plan.append(f"Missing from the XER: {missing['TASK']['count']} activities (kept), "
                        f"{missing['TASKPRED']['count']} relationships, {missing['TASKRSRC']['count']} assignments")
            plan += gone_txt
        else:
            plan.append(f"Data date: XER {data_date['xer']}")
        if backup:
            plan.append(f"Backup: {backup}")
        # a new project or baseline holds the XER's own F9 results; an update mixes old and new data
        sched_rows = _opt_rows(con, sched["options"]) if sched else None
        res = _commit(con, plan, confirm, projects=(pid,) if mode == "update" else (),
                      log=[{"operation": f"import_xer_{mode}", "proj_id": pid, "table": None,
                            "keys": {"proj_id": pid, "file": os.path.abspath(path), "xer_project": xp["proj_short_name"]},
                            "after": {"counts": imp.stats}, "summary": plan[0]}])
        res.update({"counts": imp.counts(), "warnings": imp.warnings, "data_date": data_date, "preflight": pf})
        if missing is not None:
            res["missing_from_xer"] = missing
        res["schedule_options"] = {"imported": sched is not None, "action": sched and sched["action"],
                                   "changed": [o for o in sched_rows if o["changed"]] if sched_rows else []}
        if not confirm:
            res.update(would_refuse=bool(reasons), refuse_reasons=reasons)
            if reasons:
                res["next"] = "confirm=True would be refused: " + " ".join(reasons)
        return res


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def p6_inspect_xer(path: str, xer_project: Optional[str] = None, db_preview: bool = True) -> dict:
    """Read an XER file without importing it: header, encoding (UTF-8, else cp1252 with the offset of
    the first non UTF-8 byte), BOM, %E end marker, rows per table, projects (Project ID, data date,
    activities) and the pre-flight p6_import_xer runs: errors (they block a confirmed import),
    warnings, info, tables not imported, XER columns this database lacks (dropped_columns) and,
    with db_preview, the global items (calendars, resources, code types...) the import would reuse
    or create. Read only. xer_project picks the project when the file holds several."""
    header, x, meta = _xer_read_meta(path)
    res = {"path": path, "header": header, **meta, "tables": {t: len(r) for t, r in x.items()},
           "projects": [{"short_name": p.get("proj_short_name"),
                         "data_date": _fmt_dt(d) if (d := _db_dt(p.get("last_recalc_date"))) else None,
                         "activities": sum(1 for t in x.get("TASK", []) if t.get("proj_id") == p.get("proj_id"))}
                        for p in x.get("PROJECT", [])]}
    if xer_project is None and len(res["projects"]) != 1:
        res.update(preflight=None, note="Several projects: pass xer_project= for the pre-flight."
                   if res["projects"] else "No PROJECT row: nothing to import.")
        return res
    try:
        con = _connect()
    except RuntimeError as e:
        con, res["db_note"] = None, str(e)
    try:
        res["preflight"] = _xer_preflight(header, x, meta, xer_project, con, db_preview)
    finally:
        if con is not None:
            con.close()
    return res


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_export_xer(project: ProjectId, path: str, tables: Optional[list[XerTable]] = None,
                  xer_version: Optional[str] = None, overwrite: bool = False) -> dict:
    """Export one project to an XER file (read only on the database). An existing file is refused
    unless overwrite=True.
    tables: which XER tables to write, any of
      COSTTYPE, CURRTYPE, FINTMPL, MEMOTYPE, OBS, RISKTYPE, ROLES, UDFTYPE, PROJECT, CALENDAR,
      SCHEDOPTIONS, PROJWBS, RSRC, ACTVTYPE, PROJRISK, ROLERATE, RSRCRATE, TASK, ACTVCODE, PROJCOST,
      TASKMEMO, TASKPRED, TASKPROC, TASKRSRC, TASKACTV, TASKRISK, UDFVALUE (default: all; a table
      this database lacks is skipped with a warning).
    Activity notebooks (TASKMEMO, topics in MEMOTYPE), steps (TASKPROC), expenses (PROJCOST,
    categories in COSTTYPE), roles (ROLES, ROLERATE) and risks (RISKTYPE, PROJRISK, TASKRISK) are
    written with the activities.
    Global data is limited to what the project uses (calendars, resources, roles, codes, UDF types,
    OBS, notebook topics, expense categories, risk types).
    SCHEDOPTIONS carries the project's schedule options (skipped with a warning when none are stored).
    xer_version: version written in the header (default: this database's P6 version); lower it
    when the file must be imported into an older P6."""
    tables = [t.upper() for t in (tables or XER_EXPORT_TABLES)]
    bad = [t for t in tables if t not in XER_EXPORT_TABLES]
    if bad:
        raise ValueError(f"Unknown tables {bad}; choose from {XER_EXPORT_TABLES}")
    path = _export_target(path)
    if os.path.exists(path) and not overwrite:
        raise ValueError(f"{path} already exists: pass overwrite=True to replace it, or choose another path.")
    with closing(_connect()) as con:
        return _export_xer(con, project, path, tables, xer_version)
