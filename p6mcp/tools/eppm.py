"""P6 EPPM tools: projects, pull, status, push, discard, server schedule (p6mcp.eppm)."""

from __future__ import annotations

import os
import uuid
from contextlib import closing
from datetime import datetime
from typing import Any, Literal, Optional

from p6mcp import config as _config
from p6mcp.eppm import store as _eppm_store

from p6mcp.core import state as _state
from p6mcp.core.state import _cols_cache, _refresh, _schema_cache
from p6mcp.core.constants import REL_TYPES_REV
from p6mcp.registry import DateTimeStr, _plan_gate, _tool
from p6mcp.core.db import _alive, _check_page, _cols, _connect, _now_str, _parse_dt, _slice, _stamp_cols
from p6mcp.core.editlog import _log_entries, _log_ts, _log_write, _undone_fn
from p6mcp.core.writes import _commit, REL_LOG_COLS, _write_session


# ----------------------------------------------------------------- P6 EPPM server (p6mcp.eppm)

def _eppm_profile() -> dict:
    """The confirmed active P6 EPPM server profile; ValueError outside P6 EPPM mode."""
    st = _state.SETTINGS
    p = _config.eppm_profile(st.raw, st.eppm) if st.eppm else None
    if p is None:
        raise ValueError("Not in P6 EPPM mode: in MCP for Primavera P6 Settings (Database), choose 'A P6 EPPM "
                         "server', add the server, test the connection, click Use this server, then Save.")
    return p


def _eppm_secret(key: str, what: str) -> str:
    from p6mcp import credentials
    try:
        pw = credentials.get(key)
    except credentials.SecretStoreError as e:
        raise ValueError(str(e)) from None
    if not pw:
        raise ValueError(f"No {what} is stored for this server: enter it in MCP for Primavera P6 Settings "
                         "(Database) and click Test connection.")
    return pw


def _eppm_pending() -> list[dict]:
    """Net pending changes of the working copy (edits since the pull, not undone, not pushed)."""
    from p6mcp.eppm import pending
    m = _state.EPPM_MANIFEST
    if not m or not _state.DB_PATH:
        return []
    es = _log_entries()
    return pending.net(pending.entries_since(es, m.get("pulled_ts") or m["pulled_at"], _undone_fn(es),
                                             m.get("pushed_entries", [])))


def _eppm_session(p: dict):
    from p6mcp import credentials, eppm
    from p6mcp.eppm import rest
    try:
        return rest.from_profile(p, _eppm_secret(credentials.eppm_key(p["name"]), "P6 password"),
                                 lock_check=eppm.lock_check(p))
    except rest.EppmError as e:
        raise ValueError(str(e)) from None


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
def p6_eppm_projects(search: str = "", offset: int = 0, limit: int = 100) -> dict:
    """P6 EPPM mode: the projects this P6 user can read on the server (Project ID, name, data date, last
    schedule, whether a baseline is set, whether it is in the working copy). Reads the server.
    search: part of the Project ID or name (case ignored)."""
    offset, limit = _check_page(offset, limit)
    p = _eppm_profile()
    pulled = {x["project_id"].lower() for x in (_state.EPPM_MANIFEST or {}).get("projects", [])}
    from p6mcp.eppm import rest
    try:
        with _eppm_session(p) as s:
            rows = s.read("project", ["ObjectId", "Id", "Name", "DataDate", "LastScheduleDate", "Status",
                                      "CurrentBaselineProjectObjectId"])
    except rest.EppmError as e:
        raise ValueError(str(e)) from None
    q = search.strip().lower()
    out = sorted(({"project_id": r.get("Id"), "name": r.get("Name"), "status": r.get("Status"),
                   "data_date": (r.get("DataDate") or "").replace("T", " ")[:19] or None,
                   "last_schedule": (r.get("LastScheduleDate") or "").replace("T", " ")[:19] or None,
                   "has_baseline": r.get("CurrentBaselineProjectObjectId") not in (None, ""),
                   "pulled": str(r.get("Id", "")).lower() in pulled}
                  for r in rows if not q or q in f"{r.get('Id', '')} {r.get('Name', '')}".lower()),
                 key=lambda r: (str(r["project_id"]).lower(), str(r["name"])))
    return dict(_slice(out, offset, limit), server=p["name"])


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True})
def p6_eppm_pull(projects: list[str], include_baseline: bool = True,
                 route: Literal["auto", "sql", "rest"] = "auto", discard_pending: bool = False) -> dict:
    """P6 EPPM mode: copy projects from the server into the local working copy that every other tool
    reads (the projects pulled before are refreshed too). Reads the server only: nothing is changed there.
    projects: Project IDs (see p6_eppm_projects). include_baseline: also copy each project's current
    baseline (needed by baseline variance, BEI and milestones vs baseline).
    route: sql (the read-only SQL account set in MCP for Primavera P6 Settings: every table as stored),
    rest (the server's XER export, for servers without a SQL account), auto = sql when it is set up and
    its driver installed, else rest.
    A pull rebuilds the working copy: it is refused while local edits are pending (see p6_eppm_status)
    unless discard_pending=True, which drops them (ask the user first)."""
    p = _eppm_profile()
    if not projects:
        raise ValueError("Name at least one project (Project ID, see p6_eppm_projects).")
    waiting = _eppm_pending()
    if waiting and not discard_pending:
        raise ValueError(f"{len(waiting)} local change(s) are pending in the working copy and a pull would drop them. "
                         "Push them first (p6_eppm_push), or pull with discard_pending=True if the user agrees to "
                         "lose them. See p6_eppm_status.")
    out = _eppm_pull_now(p, projects, include_baseline, route)
    if waiting:
        out["discarded_changes"] = len(waiting)
    return out


def _eppm_pull_now(p: dict, projects: list[str], include_baseline: bool = True, route: str = "auto") -> dict:
    """Pull into the working copy (the checks on pending changes are the caller's)."""
    from p6mcp import credentials
    from p6mcp.eppm import rest, sql
    if route not in _eppm_store.ROUTES:
        raise ValueError(f"route must be one of {list(_eppm_store.ROUTES)}")
    spw = None
    if route == "sql" or (route == "auto" and p.get("sql")):
        if not p.get("sql"):
            raise ValueError("This server has no read-only SQL account: use route='rest', or add the account in MCP "
                             "for Primavera P6 Settings (Database).")
        driver_ok = sql.driver_module(p["sql"]["driver"]) is not None
        if route == "sql" and not driver_ok:
            raise ValueError(f"The {sql.DRIVERS[p['sql']['driver']]} driver is not installed: install it from MCP "
                             "for Primavera P6 Settings (Database, Test connection), or use route='rest'.")
        if driver_ok:
            spw = _eppm_secret(credentials.eppm_sql_key(p["name"]), "SQL password")
        route = "sql" if driver_ok else "rest"
    elif route == "auto":
        route = "rest"
    pw = _eppm_secret(credentials.eppm_key(p["name"]), "P6 password")
    try:
        m = _eppm_store.pull(p, projects, pw, spw, include_baseline, route)
    except (rest.EppmError, sql.SqlError, _eppm_store.PullError) as e:
        raise ValueError(str(e)) from None
    except OSError as e:
        raise ValueError(f"The working copy could not be replaced (is another tool reading it?): {e}") from None
    _cols_cache.clear()
    _schema_cache.clear()
    _refresh()
    return {"server": p["name"], "pulled_at": m["pulled_at"], "seconds": m["seconds"], "route": m["route"],
            "projects": m["projects"], "rows": m["rows"], "warnings": m["warnings"],
            "next": "Every tool now reads the working copy. Edits change the copy only (p6_eppm_status lists them) "
                    "until p6_eppm_push."}


def _srv_time(v: Any) -> str:
    """A server LastUpdateDate as 'YYYY-MM-DD HH:MM:SS' on the server's clock (offset dropped, as the filters take it)."""
    return str(v or "").replace("T", " ")[:19]


def _eppm_pushed_stamps(sess, sent: list[dict]) -> dict[str, str]:
    """{activity ObjectId: server LastUpdateDate} read back just after a push, for the activities its calls touched
    (updated ones, both ends of a relationship, the activity of an assignment). p6_eppm_status leaves an activity
    out of the server changes while its LastUpdateDate is still this one. A read failure only loses that filter."""
    from p6mcp.eppm import rest
    ids: set[int] = set()
    for x in sent:
        if x["table"] == "TASK":
            ids.add(int(x["object_id"]))
        for k in ("task_id", "pred_task_id"):
            v = (x["change"].get("refs") or {}).get(k)
            if v is not None:
                ids.add(int(v))
    out: dict[str, str] = {}
    todo = sorted(ids)
    try:
        for i in range(0, len(todo), 100):
            for r in sess.read("activity", ["ObjectId", "LastUpdateDate"],
                               " :or: ".join(f"ObjectId :eq: {x}" for x in todo[i:i + 100])):
                out[str(int(r["ObjectId"]))] = _srv_time(r.get("LastUpdateDate"))
    except rest.EppmError:
        return {}
    return out


@_tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
def p6_eppm_status(check_server: bool = True) -> dict:
    """P6 EPPM mode: the working copy (server, pull time, route, projects, rows, warnings) and, with
    check_server, how many activities of each pulled project someone changed on the server since the pull
    (one read per project: pull again to bring them in); the user's own pushes are counted apart."""
    p = _eppm_profile()
    m = _state.EPPM_MANIFEST
    out: dict[str, Any] = {"server": p["name"], "address": p["base_url"], "pulled": m is not None}
    if m is None:
        out["next"] = "Nothing pulled yet: list the projects with p6_eppm_projects, then p6_eppm_pull."
        return out
    out.update(working_copy=_eppm_store.store_path(p["name"]), pulled_at=m["pulled_at"], route=m["route"],
               projects=m["projects"], rows=m["rows"], warnings=m["warnings"])
    from p6mcp.eppm import pending
    changes = _eppm_pending()
    out["pending"] = {"count": len(changes), "by_table": pending.summary(changes),
                      "changes": [{k: v for k, v in c.items() if k not in ("entries", "rollups")}
                                  for c in changes[:50]],
                      "truncated": len(changes) > 50}
    if m.get("pushes"):
        out["last_push"] = m["pushes"][-1]
    if check_server and m.get("server_stamp"):
        from p6mcp.eppm import rest
        stamp = m["server_stamp"].replace("T", " ")
        mine = m.get("pushed_activities") or {}
        changed, yours = {}, {}
        try:
            with _eppm_session(p) as s:
                for x in m["projects"]:
                    rows = s.read("activity", ["ObjectId", "LastUpdateDate"],
                                  f"ProjectObjectId :eq: {int(x['object_id'])} :and: LastUpdateDate:gt:'{stamp}'")
                    # the server keeps fractions of a second: the activity whose change is the stamp matches gt
                    rows = [r for r in rows if _srv_time(r.get("LastUpdateDate")) > stamp]
                    own = [r for r in rows if str(int(r["ObjectId"])) in mine
                           and _srv_time(r.get("LastUpdateDate")) <= mine[str(int(r["ObjectId"]))]]
                    changed[x["project_id"]] = len(rows) - len(own)
                    yours[x["project_id"]] = len(own)
            out["changed_on_server"] = {
                "since": m["pulled_at"], "since_server_clock": stamp, "activities": changed, "pushed_by_you": yours,
                "note": "activities = changed on the server by someone else since the pull (since: the pull, on this "
                        "PC's clock; since_server_clock: where the server comparison starts, on the server's clock). "
                        "pushed_by_you = activities your own pushes changed: the working copy already has them."}
        except rest.EppmError as e:
            out["changed_on_server"] = {"error": str(e)}
    return out


def _eppm_pushed_projects(changes: list[dict], m: dict) -> list[str]:
    """Project IDs of the pulled projects the pending changes touch (a relationship: both ends' projects), in the
    working copy's order."""
    by_oid = {int(x["object_id"]): x["project_id"] for x in m.get("projects", [])}
    by_name = {str(x["project_id"]).lower(): x["project_id"] for x in m.get("projects", [])}
    hit: set[str] = set()
    for c in changes:
        for oid in (c.get("proj_id"), (c.get("refs") or {}).get("pred_proj_id")):
            if oid is not None and str(oid).lstrip("-").isdigit() and int(oid) in by_oid:
                hit.add(by_oid[int(oid)])
        if c.get("project") and str(c["project"]).lower() in by_name:
            hit.add(by_name[str(c["project"]).lower()])
    return [x["project_id"] for x in m.get("projects", []) if x["project_id"] in hit]


def _eppm_targets(sess, names: list[str]) -> list[dict]:
    """The server's project rows (ObjectId, Id, DataDate) of these Project IDs; ValueError for one this user cannot
    read."""
    listed = {str(r.get("Id", "")).lower(): r for r in sess.read("project", ["ObjectId", "Id", "DataDate"])}
    missing = [n for n in names if n.lower() not in listed]
    if missing:
        raise ValueError(f"Not found among the projects this P6 user can read: {', '.join(missing)}")
    return [listed[n.lower()] for n in names]


def _eppm_schedule_lines(p: dict, targets: list[dict], dd: Optional[str], lead: str = "Schedule") -> list[str]:
    return [f"{lead} {t['Id']} on {p['name']} (data date "
            + (f"{(t.get('DataDate') or '')[:16].replace('T', ' ')} -> {dd[:16].replace('T', ' ')}" if dd
               else f"{(t.get('DataDate') or '?')[:16].replace('T', ' ')}, unchanged") + ")" for t in targets]


def _eppm_scheduled(p: dict, targets: list[dict], job: dict, dd: Optional[str], plan: list[str], pull_after: bool,
                    tool: str) -> dict:
    """The result of a finished schedule job: its log in the edit log, then the pull when asked (its failure is
    reported, not raised: the server was scheduled). Shared by p6_eppm_schedule and p6_eppm_push(schedule=True)."""
    from p6mcp.eppm import jobs
    out = {"applied": job["finished"], "server": p["name"], "projects": [t["Id"] for t in targets],
           "job": {k: job[k] for k in ("job_id", "status", "seconds")}, "log_facts": jobs.log_facts(job["log"]),
           "log": job["log"], "log_truncated": job["log_truncated"]}
    try:
        _log_write([{"ts": _log_ts(), "commit": uuid.uuid4().hex[:12], "db": os.path.basename(_state.DB_PATH or "eppm"),
                     "tool": tool, "operation": "schedule_eppm", "project": ", ".join(out["projects"]),
                     "proj_id": None, "table": None, "keys": {"server": p["name"]}, "before": None,
                     "after": {"job": out["job"], "new_data_date": dd}, "summary": "; ".join(plan)}])
    except OSError:
        pass
    if not job["finished"]:
        out["next"] = (f"The schedule job ended as {job['status']!r}: read the log, fix the cause in P6 EPPM, then "
                       "schedule again (p6_eppm_schedule).")
        return out
    if not pull_after:
        out["next"] = "Scheduled on the server. Pull again (p6_eppm_pull) to read the new dates."
        return out
    waiting = _eppm_pending()
    if waiting:  # a pull would drop them
        out["pull"] = {"skipped": f"{len(waiting)} local change(s) are still pending: the working copy was not "
                                  "pulled again."}
        out["next"] = "Scheduled on the server. Push the pending changes, then pull again to read the new dates."
        return out
    m = _state.EPPM_MANIFEST or {}
    try:
        pulled = _eppm_pull_now(p, sorted({x["project_id"] for x in m.get("projects", [])} | set(out["projects"])),
                                route=m.get("route") if m.get("route") in ("sql", "rest") else "auto")
    except ValueError as e:
        out["pull"] = {"error": str(e)}
        out["next"] = "Scheduled on the server, but the pull failed: pull again (p6_eppm_pull) to read the new dates."
        return out
    out["pull"] = {k: pulled[k] for k in ("pulled_at", "seconds", "route", "rows", "warnings")}
    out["next"] = "Scheduled on the server and pulled again: the tools read P6's new dates."
    return out


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
def p6_eppm_push(schedule: bool = False, new_data_date: Optional[DateTimeStr] = None, pull_after: bool = True,
                 confirm: bool = False) -> dict:
    """P6 EPPM mode: send the pending changes of the working copy to the server (see p6_eppm_status).
    The dry run reads every changed object on the server and lists the calls; a change made on the
    server since the pull is a conflict and refuses the push (pull again, or drop the local change).
    Calls go one object at a time (relationship deletes, activity updates, new relationships,
    relationship updates, assignments) and the first failure stops the push: the result and the journal
    say what reached the server, and a new push sends the rest.
    schedule: once every call has succeeded, also schedule the pushed projects on the server with P6's
    own scheduler (as p6_eppm_schedule: new_data_date moves the data date, YYYY-MM-DD [HH:MM]; pull_after
    pulls them again so the tools read the new dates); the same confirmation covers both steps, and a push
    that fails or has a conflict schedules nothing. Without schedule the server is not rescheduled: dates
    move when the project is scheduled in P6 EPPM (or with p6_eppm_schedule)."""
    from p6mcp.eppm import jobs
    from p6mcp.eppm import push as _push
    from p6mcp.eppm import rest
    p = _eppm_profile()
    m = _state.EPPM_MANIFEST
    if not m:
        raise ValueError("Nothing pulled yet: there is nothing to push.")
    if new_data_date and not schedule:
        raise ValueError("new_data_date moves the data date of a schedule: pass schedule=True with it.")
    dd = _parse_dt(new_data_date, "new_data_date")[0].strftime("%Y-%m-%dT%H:%M:%S") if new_data_date else None
    changes = _eppm_pending()
    if not changes:
        return {"dry_run": not confirm, "would_refuse": True, "refuse_reasons": ["No pending change to push."],
                "would_do": ["Nothing to send."]} if not confirm else {"applied": False,
                                                                    "error": "No pending change to push."}
    names = _eppm_pushed_projects(changes, m) if schedule else []
    if schedule and not names:
        raise ValueError("The pending changes name no pulled project to schedule: push without schedule, then use "
                         "p6_eppm_schedule.")
    try:
        with _eppm_session(p) as sess:
            pl = _push.plan(sess, changes, _eppm_store.LOCAL_ID_BASE)
            targets = _eppm_targets(sess, names) if schedule else []
            sched_plan = _eppm_schedule_lines(p, targets, dd, "After the push, schedule") + (
                ["Then pull them again into the working copy"] if pull_after else []) if schedule else []
            view = {"calls": len(pl["calls"]),
                    "already_done": [{"what": x["label"], "why": x["reason"]} for x in pl["already_done"]],
                    "conflicts": [{"what": x["label"], "why": x["reason"], "id": x["change"].get("id")}
                                  for x in pl["conflicts"]],
                    "unsupported": [f"{c['table']} {c['action']}" for c in pl["unsupported"]],
                    "not_sent_fields": {x["label"]: x["not_sent"] for x in pl["calls"] if x.get("not_sent")}}
            reasons = ([f"conflict: {x['what']}: {x['why']}" for x in view["conflicts"]]
                       + [f"not supported by the push: {u}" for u in view["unsupported"]])
            mismatch = _plan_gate(pl["plan"] + sched_plan, confirm)
            if not confirm:
                out = {"dry_run": True, "server": p["name"], "would_do": pl["plan"] + sched_plan, **view}
                if schedule:
                    out["schedule"] = {"projects": [t["Id"] for t in targets], "new_data_date": dd,
                                       "pull_after": pull_after,
                                       "note": "Runs only when every call of the push succeeds. P6 rewrites the "
                                               "early and late dates and the float of every activity of these "
                                               "projects on the server."}
                if reasons:
                    out.update(would_refuse=True, refuse_reasons=reasons)
                return out
            if mismatch:
                return {"applied": False, "error": mismatch}
            if reasons:
                return {"applied": False, "error": "The push is refused: " + "; ".join(reasons)}
            journal = os.path.join(_eppm_store.store_dir(p["name"]), f"push_{datetime.now():%Y%m%d_%H%M%S}.jsonl")
            res = _push.apply(sess, pl["calls"], journal)
            mine = _eppm_pushed_stamps(sess, res["sent"])
    except rest.EppmError as e:
        raise ValueError(str(e)) from None
    # what reached the server (or was there already) no longer counts as pending; new relationships take their ids
    created = dict(res["created"])
    created.update({x["object_id"]: x["server_id"] for x in pl["already_done"] if x.get("server_id")})
    if created:
        with closing(_connect(readonly=False)) as con:
            for local, server_id in created.items():
                con.execute("UPDATE TASKPRED SET task_pred_id = ? WHERE task_pred_id = ?", (server_id, local))
            con.commit()
    done = [e for x in res["sent"] + pl["already_done"] for e in x["change"]["entries"]]
    record = {"at": datetime.now().isoformat(timespec="seconds"), "sent": len(res["sent"]),
              "already_done": len(pl["already_done"]), "failed": (res["failed"] or {}).get("label"),
              "journal": journal}
    _eppm_store.update_manifest(p["name"], pushed_entries=done, push=record, pushed_activities=mine)
    try:
        _log_write([{"ts": _log_ts(), "commit": uuid.uuid4().hex[:12], "db": os.path.basename(_state.DB_PATH),
                     "tool": "p6_eppm_push", "operation": "push_eppm", "project": None, "proj_id": None,
                     "table": None, "keys": {"server": p["name"]}, "before": None, "after": record,
                     "summary": f"pushed {len(res['sent'])} change(s) to {p['name']}"}])
    except OSError:
        pass
    _refresh()
    out = {"applied": res["failed"] is None, "server": p["name"], "sent": [x["label"] for x in res["sent"]],
           "already_done": [x["label"] for x in pl["already_done"]], "journal": journal,
           "new_ids": {str(k): v for k, v in created.items()},
           "next": "The server is not rescheduled: schedule the project in P6 EPPM (or with p6_eppm_schedule), then "
                   "pull it again to read the new dates."}
    if res["failed"]:
        out["failed"] = {"what": res["failed"]["label"], "error": res["failed"].get("error")}
        out["next"] = ("The push stopped at the first failure; the changes listed in sent reached the server. Fix "
                       "the cause, then push again: only the rest is sent.")
        if schedule:
            out["schedule"] = {"applied": False, "projects": [t["Id"] for t in targets],
                               "skipped": "The push did not complete: nothing was scheduled."}
        return out
    if schedule:
        try:
            with _eppm_session(p) as sess:
                job = jobs.schedule(sess, [int(t["ObjectId"]) for t in targets], dd)
        except (ValueError, jobs.JobError) as e:  # the push is done: report, never raise
            out["schedule"] = {"applied": False, "projects": [t["Id"] for t in targets], "error": str(e)}
            out["next"] = ("Pushed, but the schedule could not run on the server: read schedule.error, then schedule "
                           "with p6_eppm_schedule.")
            return out
        out["schedule"] = _eppm_scheduled(p, targets, job, dd, sched_plan, pull_after, "p6_eppm_push")
        out["next"] = "Pushed. " + out["schedule"].pop("next")
    return out


def _eppm_revert(con, c: dict) -> dict:
    """Give one pending change's object back the values it had at the pull, on an open write connection, as an
    ordinary logged edit (same operations as the edit tools): the netting of pending.net then cancels the change.
    Returns {plan, log, projects}."""
    t, act, keys, refs, lb = c["table"], c["action"], c["keys"], c.get("refs") or {}, c.get("labels") or {}
    fields = c.get("fields") or {}
    stamp = lambda table, create=False: _stamp_cols(con, table, _now_str(con), create=create)  # noqa: E731
    if t == "TASK" and act == "update":
        tid = int(keys["task_id"])
        row = con.execute("SELECT * FROM TASK WHERE task_id = ?", (tid,)).fetchone()
        if row is None:
            raise ValueError(f"activity {lb.get('task_code', tid)} is no longer in the working copy")
        cols = [k for k in fields if k in _cols(con, "TASK")]
        sets = {k: fields[k]["pulled"] for k in cols}
        before = {k: row[k] for k in cols}
        full = {**sets, **{k: v for k, v in stamp("TASK").items() if k in _cols(con, "TASK")}}
        con.execute(f"UPDATE TASK SET {', '.join(k + '=?' for k in full)} WHERE task_id=?", [*full.values(), tid])
        plan = f"activity {lb.get('task_code', tid)}: " + ", ".join(f"{k} {before[k]!r} -> {sets[k]!r}" for k in cols)
        return {"plan": plan, "projects": [c.get("proj_id")],
                "log": [{"operation": "update_activity", "proj_id": c.get("proj_id"), "table": "TASK",
                         "keys": {"task_id": tid, "task_code": lb.get("task_code")}, "before": before,
                         "after": sets, "summary": "discard: " + plan}]}
    if t == "TASKRSRC" and act == "update":
        rid, tid = int(keys["taskrsrc_id"]), refs.get("task_id")
        row = con.execute("SELECT * FROM TASKRSRC WHERE taskrsrc_id = ?", (rid,)).fetchone()
        if row is None:
            raise ValueError(f"assignment {rid} is no longer in the working copy")
        tid = int(tid if tid is not None else row["task_id"])
        cols = [k for k in fields if k in _cols(con, "TASKRSRC")]
        sets = {k: fields[k]["pulled"] for k in cols}
        before = {k: row[k] for k in cols}
        full = {**sets, **{k: v for k, v in stamp("TASKRSRC").items() if k in _cols(con, "TASKRSRC")}}
        con.execute(f"UPDATE TASKRSRC SET {', '.join(k + '=?' for k in full)} WHERE taskrsrc_id=?", [*full.values(), rid])
        trow = con.execute("SELECT * FROM TASK WHERE task_id = ?", (tid,)).fetchone()
        tbefore, tafter = {}, {}
        for col, v in (c.get("rollups") or {}).items():  # the activity totals lose this assignment's delta only
            if trow is None or col not in _cols(con, "TASK"):
                continue
            delta = float(v["local"] or 0) - float(v["pulled"] or 0)
            tbefore[col] = trow[col]
            tafter[col] = float(trow[col] or 0) - delta
        if tafter:
            con.execute(f"UPDATE TASK SET {', '.join(k + '=?' for k in tafter)} WHERE task_id=?", [*tafter.values(), tid])
        who = lb.get("resource") or lb.get("role") or rid
        plan = f"assignment {lb.get('task_code', tid)} {who}: " + ", ".join(
            f"{k} {before[k]!r} -> {sets[k]!r}" for k in cols)
        return {"plan": plan, "projects": [],
                "log": [{"operation": "update_assignment", "proj_id": c.get("proj_id"), "table": "TASKRSRC",
                         "keys": {"taskrsrc_id": rid, "task_id": tid, "task_code": lb.get("task_code"),
                                  "resource": lb.get("resource"), "role": lb.get("role")},
                         "before": {**before, **{f"TASK.{k}": v for k, v in tbefore.items()}},
                         "after": {**sets, **{f"TASK.{k}": v for k, v in tafter.items()}},
                         "summary": "discard: " + plan}]}
    if t == "TASKPRED":
        pid = int(keys["task_pred_id"])
        link = f"relationship {lb.get('predecessor', refs.get('pred_task_id'))} -> {lb.get('successor', refs.get('task_id'))}"
        lkeys = {"task_pred_id": pid, "task_id": refs.get("task_id"), "pred_task_id": refs.get("pred_task_id"),
                 "successor": lb.get("successor"), "predecessor": lb.get("predecessor"),
                 "pred_proj_id": refs.get("pred_proj_id")}
        projects = [c.get("proj_id"), refs.get("pred_proj_id")]
        row = con.execute("SELECT * FROM TASKPRED WHERE task_pred_id = ?", (pid,)).fetchone()
        if act == "create":  # added locally: removed
            if row is not None:
                con.execute("DELETE FROM TASKPRED WHERE task_pred_id = ?", (pid,))
            before = None if row is None else {k: row[k] for k in REL_LOG_COLS}
            plan = f"{link}: added locally, removed"
            return {"plan": plan, "projects": projects,
                    "log": [{"operation": "delete_relationship", "proj_id": c.get("proj_id"), "table": "TASKPRED",
                             "keys": lkeys, "before": before, "after": None, "summary": "discard: " + plan}]}
        if act == "update":
            if row is None:
                raise ValueError(f"{link} is no longer in the working copy")
            cols = [k for k in fields if k in _cols(con, "TASKPRED")]
            sets = {k: fields[k]["pulled"] for k in cols}
            before = {k: row[k] for k in cols}
            full = {**sets, **{k: v for k, v in stamp("TASKPRED").items() if k in _cols(con, "TASKPRED")}}
            con.execute(f"UPDATE TASKPRED SET {', '.join(k + '=?' for k in full)} WHERE task_pred_id=?",
                        [*full.values(), pid])
            plan = f"{link}: " + ", ".join(f"{k} {before[k]!r} -> {sets[k]!r}" for k in cols)
            return {"plan": plan, "projects": projects,
                    "log": [{"operation": "update_relationship", "proj_id": c.get("proj_id"), "table": "TASKPRED",
                             "keys": lkeys, "before": before, "after": sets, "summary": "discard: " + plan}]}
        if act == "delete":  # deleted locally: put back with its server id
            vals = {k: (fields.get(k) or {}).get("pulled") for k in REL_LOG_COLS}
            if row is not None:
                raise ValueError(f"{link}: id {pid} is in use again in the working copy")
            if None in (refs.get("task_id"), refs.get("pred_task_id"), vals["pred_type"]):
                raise ValueError(f"{link}: the edit log lacks what is needed to put it back (pull again)")
            same = con.execute("SELECT task_pred_id FROM TASKPRED WHERE task_id=? AND pred_task_id=? AND pred_type=?"
                               + _alive(con, "TASKPRED", "TASKPRED"),
                               (refs["task_id"], refs["pred_task_id"], vals["pred_type"])).fetchone()
            if same is not None:
                raise ValueError(f"{link}: a relationship of this type links these activities again in the working "
                                 f"copy (TASKPRED:{same[0]}): drop that change first")
            row = {"task_pred_id": pid, "task_id": refs["task_id"], "pred_task_id": refs["pred_task_id"],
                   "proj_id": c.get("proj_id"), "pred_proj_id": refs.get("pred_proj_id") or c.get("proj_id"),
                   "pred_type": vals["pred_type"], "lag_hr_cnt": vals["lag_hr_cnt"] or 0.0, **stamp("TASKPRED", True)}
            row = {k: v for k, v in row.items() if k in _cols(con, "TASKPRED")}
            con.execute(f"INSERT INTO TASKPRED ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                        list(row.values()))
            plan = f"{link}: deleted locally, put back ({REL_TYPES_REV.get(vals['pred_type'], vals['pred_type'])})"
            return {"plan": plan, "projects": projects,
                    "log": [{"operation": "add_relationship", "proj_id": c.get("proj_id"), "table": "TASKPRED",
                             "keys": lkeys, "before": None, "after": {k: row.get(k) for k in REL_LOG_COLS},
                             "summary": "discard: " + plan}]}
    raise ValueError(f"{c.get('id')}: a {t} {act} cannot be dropped (pull again)")


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
def p6_eppm_discard(changes: list[str], confirm: bool = False) -> dict:
    """P6 EPPM mode: drop pending changes of the local working copy, by their id in p6_eppm_status
    (pending.changes[].id, also given with each conflict of a p6_eppm_push dry run), or ["all"]. Each object
    gets back the values it had at the pull and stops counting as pending; the other changes stay pending.
    The server is not touched. Use it after a push conflict on one change, instead of a new pull that drops
    every change. Dry run unless confirm=True."""
    _eppm_profile()
    if not _state.EPPM_MANIFEST:
        raise ValueError("Nothing pulled yet: there is nothing to discard.")
    want = list(dict.fromkeys(str(x).strip() for x in (changes or []) if str(x).strip()))
    if not want:
        raise ValueError("Name the changes to drop: their id in p6_eppm_status (pending.changes), or ['all'].")
    pend = _eppm_pending()
    by_id = {c["id"]: c for c in pend}
    if [w.lower() for w in want] == ["all"]:
        sel = pend
    else:
        unknown = [w for w in want if w not in by_id]
        if unknown:
            ids = ", ".join(list(by_id)[:20]) or "none"
            raise ValueError(f"Not a pending change: {', '.join(unknown)}. Pending: {ids}.")
        sel = [by_id[w] for w in want]
    if not sel:
        return {"applied": False, "error": "No pending change to drop."}
    with _write_session(confirm) as (con, backup):
        plan, log, projects = [], [], set()
        for c in sel:
            r = _eppm_revert(con, c)
            plan.append(f"  {c['id']}: {r['plan']}")
            log += r["log"]
            projects |= {x for x in r["projects"] if x is not None}
        left = len(pend) - len(sel)
        plan = ([f"Drop {len(sel)} pending change(s) from the working copy (the server is not touched); "
                 f"{left} stay pending"] + plan)
        if backup:
            plan.append(f"Backup: {backup}")
        res = _commit(con, plan, confirm, projects=tuple(projects), log=log)
    if confirm and res.get("applied"):
        res["next"] = ("Dropped from the working copy: these objects hold their pulled values again. "
                       "p6_eppm_status lists what is still pending.")
    return res


@_tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
def p6_eppm_schedule(projects: Optional[list[str]] = None, new_data_date: Optional[DateTimeStr] = None,
                     pull_after: bool = True, confirm: bool = False) -> dict:
    """P6 EPPM mode: schedule projects on the server with P6's own scheduler (F9 in P6), wait for the job,
    return P6's scheduling log, then pull them again so the tools read the new dates.
    projects: Project IDs (default: the pulled ones). new_data_date: move the data date (YYYY-MM-DD
    [HH:MM]); omitted, the data date stays. It rewrites the dates of every activity on the server: ask the
    user first. With pull_after, it is refused while local changes are pending (push them first)."""
    from p6mcp.eppm import jobs, rest
    p = _eppm_profile()
    m = _state.EPPM_MANIFEST or {}
    names = [str(x).strip() for x in (projects or [x["project_id"] for x in m.get("projects", [])]) if str(x).strip()]
    if not names:
        raise ValueError("Name the projects to schedule (Project IDs, see p6_eppm_projects).")
    dd = None
    if new_data_date:
        dd = _parse_dt(new_data_date, "new_data_date")[0].strftime("%Y-%m-%dT%H:%M:%S")
    waiting = _eppm_pending() if pull_after else []
    try:
        with _eppm_session(p) as sess:
            targets = _eppm_targets(sess, names)
            plan = _eppm_schedule_lines(p, targets, dd)
            if pull_after:
                plan.append("Then pull them again into the working copy")
            mismatch = _plan_gate(plan, confirm)
            if not confirm:
                out = {"dry_run": True, "would_do": plan,
                       "note": "P6 rewrites the early and late dates and the float of every activity of these "
                               "projects on the server."}
                if waiting:
                    out.update(would_refuse=True, refuse_reasons=[
                        f"{len(waiting)} local change(s) are pending: push them (p6_eppm_push) before scheduling, or "
                        "schedule with pull_after=False"])
                return out
            if mismatch:
                return {"applied": False, "error": mismatch}
            if waiting:
                return {"applied": False, "error": f"{len(waiting)} local change(s) are pending: push them first."}
            job = jobs.schedule(sess, [int(t["ObjectId"]) for t in targets], dd)
    except (rest.EppmError, jobs.JobError) as e:
        raise ValueError(str(e)) from None
    return _eppm_scheduled(p, targets, job, dd, plan, pull_after, "p6_eppm_schedule")
