"""Working copy of P6 EPPM projects (docs/EPPM_PLAN.md sections 3.1 and 3.3).

One SQLite file per server profile, %LOCALAPPDATA%\\P6MCP\\eppm\\<profile>\\working.db, with P6's table and column
names, so every read tool of the server runs on it unchanged. `pull.json` beside it records the server, the route,
the pulled projects (Project ID, ObjectId, baseline), the pull time and the largest LastUpdateDate read (the
"changed since" stamp of the conflict check, without its offset).

A pull rebuilds the whole file for the set of pulled projects (the new one added): a temporary file is filled and
checked, then replaces the old one, so a failed pull leaves the previous copy intact. Ids are the server's
(SQL task_id = REST ObjectId). The SQL route copies rows as stored; the schedule options are not in the PMDB, so
they come from REST `scheduleOptions` into PROJPROP 'scheduling' through the server's own writer. The REST route
(no SQL account) builds the copy from the server's XER export with the XER mode's builder, then adds the schedule
options and the baseline links the same way.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Iterable, Optional

from p6mcp import config
from p6mcp.eppm import sql as _sql

MANIFEST_FILE = "pull.json"
STORE_VERSION = 2  # 2: the file is named after the profile (the edit log keys its entries by file name)
# Rows created locally (a new relationship) get ids from here up, above every server ObjectId: the push sends them
# as creations and maps them to the ObjectIds the server returns. Seeded in NEXTKEY for each LOCAL_KEYS table.
LOCAL_ID_BASE = 2_000_000_000
LOCAL_KEYS = (("TASKPRED", "task_pred_id"),)
BATCH = 1000

# (table, WHERE clause with {S} = the pulled project ids, optional table)
TABLES: tuple[tuple[str, str, bool], ...] = (
    ("PROJECT", "proj_id IN ({S}) OR project_flag = 'N'", False),
    ("PROJWBS", "proj_id IN ({S}) OR proj_id IN (SELECT proj_id FROM {PROJECT} WHERE project_flag = 'N')", False),
    ("TASK", "proj_id IN ({S})", False),
    ("TASKPRED", "proj_id IN ({S}) AND pred_proj_id IN ({S})", False),
    ("CALENDAR", "", False),
    ("TASKRSRC", "proj_id IN ({S})", True),
    ("RSRC", "rsrc_id IN (SELECT rsrc_id FROM {TASKRSRC} WHERE proj_id IN ({S}))", True),
    ("ROLES", "role_id IN (SELECT role_id FROM {TASKRSRC} WHERE proj_id IN ({S}))", True),
    ("ACTVTYPE", "", True),
    ("ACTVCODE", "", True),
    ("TASKACTV", "proj_id IN ({S})", True),
    ("UDFTYPE", "", True),
    ("UDFCODE", "", True),
    ("UDFVALUE", "proj_id IN ({S})", True),
    ("MEMOTYPE", "", True),
    ("TASKMEMO", "proj_id IN ({S})", True),
    ("PREFER", "", True),
    ("NEXTKEY", "", True),
)
INDEXES = (("TASK", "task_id"), ("TASK", "task_code"), ("TASK", "proj_id"), ("TASKPRED", "task_id"),
           ("TASKPRED", "pred_task_id"), ("PROJWBS", "wbs_id"), ("PROJWBS", "proj_id"), ("TASKRSRC", "task_id"),
           ("TASKACTV", "task_id"), ("UDFVALUE", "fk_id"), ("CALENDAR", "clndr_id"))

# REST scheduleOptions -> PROJPROP 'scheduling' (keys of the server's SCHED_SPEC); values are text on the wire
SCHED_FLAGS = {
    "MakeOpenEndedActivitiesCritical": "sched_open_critical_flag",
    "StartToStartLagCalculationType": "sched_lag_early_start_flag",  # true = from early start
    "UseExpectedFinishDates": "sched_use_expect_end_flag",
    "CalculateFloatBasedOnFinishDate": "sched_use_project_end_date_for_float",
    "MultipleFloatPathsEnabled": "enable_multiple_longest_path_calc",
    "MultipleFloatPathsUseTotalFloat": "use_total_float_multiple_longest_paths",
    "LevelAllResources": "level_all_rsrc_flag",
    "LevelWithinFloat": "level_within_float_flag",
    "PreserveScheduledEarlyAndLateDates": "level_keep_sched_date_flag",
    "IncludeExternalResAss": "level_outer_assign_flag",
}
SCHED_NUMS = {"MaximumMultipleFloatPaths": "max_multiple_longest_path", "MinFloatToPreserve": "level_float_thrs_cnt",
              "OverAllocationPercentage": "level_over_alloc_pct",
              "ExternalProjectPriorityLimit": "level_outer_assign_priority"}
OOS = {"retained logic": ("Y", "N"), "progress override": ("N", "Y"), "actual dates": ("N", "N")}
FLOAT_TYPE = {"finish": "FT_FF", "start": "FT_SS", "smallest": "FT_Min"}
LAG_CAL = {"predecessor": "rcal_Predecessor", "successor": "rcal_Successor", "24": "rcal_24Hour",
           "project": "rcal_ProjDefault"}


class PullError(ValueError):
    """A pull failed; the message is meant for the user. The previous working copy is unchanged."""


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name).strip("._") or "server"


def store_dir(profile_name: str) -> str:
    return os.path.join(config.localdata_dir(), "eppm", _safe(profile_name))


def store_path(profile_name: str) -> str:
    """eppm_<profile>.db: a name of its own, so the edit log (one folder, entries keyed by file name) keeps the
    servers apart."""
    return os.path.join(store_dir(profile_name), f"eppm_{_safe(profile_name)}.db")


def read_manifest(profile_name: str) -> Optional[dict]:
    path = os.path.join(store_dir(profile_name), MANIFEST_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        return None
    if m.get("store_version") != STORE_VERSION or not os.path.isfile(store_path(profile_name)):
        return None
    return m


def _value(v: Any) -> Any:
    """A driver value -> what the standalone database stores (dates as 'YYYY-MM-DD HH:MM:SS' text)."""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d 00:00:00")
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() and abs(v) < 2 ** 63 else float(v)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, memoryview):
        return bytes(v)
    return v


def _copy_table(cur, out: sqlite3.Connection, sqlcfg: dict, table: str, where: str, ids: str) -> Optional[int]:
    """Rows of one PMDB table into the working copy; None when the table cannot be read (optional tables)."""
    names = {t: _sql.table(sqlcfg, t) for t in ("PROJECT", "TASKRSRC")}
    clause = where.format(S=ids, **names) if where else ""
    q = f"SELECT * FROM {_sql.table(sqlcfg, table)}" + (f" WHERE {clause}" if clause else "")
    cur.execute(q)
    cols = [d[0].lower() for d in cur.description or []]
    if not cols:
        return None
    out.execute(f'CREATE TABLE "{table}" ({", ".join(f"{c!s}" for c in cols)})')
    ins = f'INSERT INTO "{table}" VALUES ({", ".join("?" for _ in cols)})'
    n = 0
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            break
        out.executemany(ins, [tuple(_value(v) for v in r) for r in rows])
        n += len(rows)
    return n


def sched_values(row: dict, task_ids: Callable[[str], Optional[int]]) -> tuple[dict, Optional[list], list[str]]:
    """REST scheduleOptions row -> (PROJPROP 'scheduling' values, level priority list, warnings)."""
    vals, warn = {}, []

    def flag(v) -> Optional[str]:
        s = str(v).strip().lower()
        return "Y" if s in ("true", "y", "yes", "1") else "N" if s in ("false", "n", "no", "0") else None

    for k, key in SCHED_FLAGS.items():
        if k in row and flag(row[k]) is not None:
            vals[key] = flag(row[k])
    for k, key in SCHED_NUMS.items():
        try:
            if row.get(k) not in (None, ""):
                vals[key] = str(float(row[k]))
        except ValueError:
            warn.append(f"schedule option {k}={row[k]!r} is not a number: the default is kept")
    ign = flag(row.get("IgnoreOtherProjectRelationships", ""))
    if ign is not None:
        vals["sched_outer_depend_type"] = "SD_None" if ign == "Y" else "SD_Both"
    text = str(row.get("OutOfSequenceScheduleType") or "").strip().lower()
    if text in OOS:
        vals["sched_retained_logic"], vals["sched_progress_override"] = OOS[text]
    elif text:
        warn.append(f"out of sequence option {row['OutOfSequenceScheduleType']!r} not known: the default is kept")
    for field, table, key in (("ComputeTotalFloatType", FLOAT_TYPE, "sched_float_type"),
                              ("RelationshipLagCalendar", LAG_CAL, "sched_calendar_on_relationship_lag")):
        text = str(row.get(field) or "").strip().lower()
        hit = next((v for w, v in table.items() if text.startswith(w)), None)
        if hit:
            vals[key] = hit
        elif text:
            warn.append(f"schedule option {field}={row[field]!r} not known: the default is kept")
    end = str(row.get("MultipleFloatPathsEndingActivityShortName") or "").strip()
    if end:
        tid = task_ids(end)
        vals["key_activity_for_multiple_longest_paths"] = str(tid) if tid else "-99"
        if not tid:
            warn.append(f"multiple float paths ending activity {end!r} not found in the project: none kept")
    lpl = [(f, o) for f, o in re.findall(r"\(0\|\|(\w+)\(sort_type\|(ASC|DESC)", str(row.get("PriorityList") or ""))]
    return vals, (lpl or None), warn


SCHED_FIELDS = list(SCHED_FLAGS) + list(SCHED_NUMS) + [
    "IgnoreOtherProjectRelationships", "OutOfSequenceScheduleType", "ComputeTotalFloatType", "RelationshipLagCalendar",
    "MultipleFloatPathsEndingActivityShortName", "PriorityList", "ProjectObjectId"]
ROUTES = ("auto", "sql", "rest")


def _session(profile: dict, password: str, rest_opener: Optional[Callable] = None):
    from p6mcp.eppm import rest
    return rest.Session(profile["base_url"], profile.get("user", ""), password, profile.get("database") or "",
                        profile.get("auth", "auto"), profile.get("context") or rest.DEFAULT_CONTEXT,
                        profile.get("ca_bundle") or None, int(profile.get("timeout_s", 120)),
                        opener=rest_opener() if rest_opener else None)


def _wanted(profile: dict, project_ids: Iterable[str]) -> set[str]:
    old = read_manifest(profile["name"]) or {}
    wanted = {str(p).strip() for p in project_ids if str(p).strip()}
    wanted |= {p["project_id"] for p in old.get("projects", [])}
    if not wanted:
        raise PullError("Name at least one project (its Project ID).")
    return wanted


def _server_info(sess, wanted: set[str], include_baseline: bool) -> tuple[list, list[dict], dict]:
    """(project list, the pulled projects, their REST schedule options), read with the user's own rights."""
    listed = sess.read("project", ["ObjectId", "Id", "Name", "CurrentBaselineProjectObjectId", "DataDate",
                                   "LastUpdateDate"])
    by_id = {str(p.get("Id", "")).lower(): p for p in listed}
    missing = sorted(w for w in wanted if w.lower() not in by_id)
    if missing:
        raise PullError(f"Not found among the projects this P6 user can read: {', '.join(missing)}")
    projects = []
    for w in sorted(wanted, key=str.lower):
        p = by_id[w.lower()]
        bl = p.get("CurrentBaselineProjectObjectId")
        projects.append({"project_id": p["Id"], "object_id": int(p["ObjectId"]), "name": p.get("Name"),
                         "baseline_object_id": int(bl) if include_baseline and bl not in (None, "") else None})
    sched_rows = {}
    for p in projects:
        rows = sess.read("scheduleOptions", SCHED_FIELDS, f"ProjectObjectId :eq: {p['object_id']}")
        sched_rows[p["object_id"]] = rows[0] if rows else None
    return listed, projects, sched_rows


def _write(profile: dict, tmp: str, route: str, started: datetime, listed: list, projects: list[dict],
           counts: dict, warnings: list[str]) -> dict:
    oids = {p["object_id"] for p in projects}
    stamp = max((str(p.get("LastUpdateDate") or "") for p in listed if int(p["ObjectId"]) in oids), default="")
    manifest = {"store_version": STORE_VERSION, "server": profile["base_url"], "profile": profile["name"],
                "route": route, "pulled_at": started.isoformat(timespec="seconds"),
                "pulled_ts": started.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],  # edit log precision (pending changes)
                "seconds": round((datetime.now() - started).total_seconds(), 2),
                "server_stamp": stamp[:19], "projects": projects, "rows": counts, "warnings": warnings}
    os.replace(tmp, store_path(profile["name"]))
    with open(os.path.join(store_dir(profile["name"]), MANIFEST_FILE), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def _temp(profile: dict) -> str:
    folder = store_dir(profile["name"])
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".working.", suffix=".db", dir=folder)
    os.close(fd)
    return tmp


def _drop(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def pull(profile: dict, project_ids: Iterable[str], password: str, sql_password: Optional[str] = None,
         include_baseline: bool = True, route: str = "auto", **kw) -> dict:
    """Pull by SQL when the profile has a SQL account with a password and its driver (route auto), else by REST."""
    if route not in ROUTES:
        raise PullError(f"route must be one of {list(ROUTES)}")
    if route == "auto":
        sqlcfg = profile.get("sql")
        usable = bool(sqlcfg and sql_password and (kw.get("sql_module") or _sql.driver_module(sqlcfg["driver"])))
        route = "sql" if usable else "rest"
    if route == "sql":
        return pull_sql(profile, project_ids, password, sql_password or "", include_baseline, **kw)
    kw.pop("sql_module", None)
    return pull_rest(profile, project_ids, password, include_baseline, **kw)


def pull_sql(profile: dict, project_ids: Iterable[str], password: str, sql_password: str,
             include_baseline: bool = True, rest_opener: Optional[Callable] = None, sql_module: Any = None,
             server_module: Any = None) -> dict:
    """Pull the given projects (Project IDs, plus the ones already pulled) by SQL, with REST for the project list
    (the user's rights) and the schedule options. Returns the manifest written to pull.json."""
    if not profile.get("sql"):
        raise PullError("This server has no SQL account: pull by REST (route 'rest'), or add the read-only SQL "
                        "account in MCP for Primavera P6 Settings (Database).")
    sqlcfg = profile["sql"]
    wanted = _wanted(profile, project_ids)
    started = datetime.now()
    with _session(profile, password, rest_opener) as sess:
        listed, projects, sched_rows = _server_info(sess, wanted, include_baseline)
    oids = [p["object_id"] for p in projects]
    id_set = sorted(set(oids) | {p["baseline_object_id"] for p in projects if p["baseline_object_id"]})
    ids = ",".join(str(int(i)) for i in id_set)
    tmp = _temp(profile)
    warnings: list[str] = []
    counts: dict[str, int] = {}
    out: Optional[sqlite3.Connection] = None
    try:
        con = _sql.connect(sqlcfg, sql_password, sql_module)
        out = sqlite3.connect(tmp)
        try:
            cur = con.cursor()
            for table, where, optional in TABLES:
                try:
                    n = _copy_table(cur, out, sqlcfg, table, where, ids)
                except Exception as e:  # noqa: BLE001  (each driver raises its own classes)
                    if hasattr(con, "rollback"):
                        try:
                            con.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                    if optional:
                        warnings.append(f"{table} not copied: {' '.join(str(e).split())[:160]}")
                        continue
                    raise PullError(f"{table} cannot be read by SQL: {' '.join(str(e).split())[:200]}") from e
                if n is not None:
                    counts[table] = n
            cur.execute(f"SELECT COUNT(*) FROM {_sql.table(sqlcfg, 'TASKPRED')} WHERE (proj_id IN ({ids}) AND "
                        f"pred_proj_id NOT IN ({ids})) OR (pred_proj_id IN ({ids}) AND proj_id NOT IN ({ids}))")
            cross = int(cur.fetchall()[0][0])
            if cross:
                warnings.append(f"{cross} relationship(s) to or from projects that were not pulled are left out")
        finally:
            con.close()
        _finish(out, profile, projects, sched_rows, warnings, server_module)
        out.commit()
        out.close()
    except BaseException:
        if out is not None:
            try:
                out.close()
            except sqlite3.Error:
                pass
        _drop(tmp)
        raise
    return _write(profile, tmp, "sql", started, listed, projects, counts, warnings)


def _export(sess, oids: list[int], folder: str, name: str) -> str:
    """POST /export/exportXERProject for these ObjectIds; the XER is in the answer (measured on 25.12)."""
    from p6mcp.eppm import rest
    st, raw = sess._send("POST", sess.rest + "/export/exportXERProject",
                         body={"FileType": "XER", "ProjectObjectId": oids}, name="export xer")
    if st != 200 or not raw.lstrip().startswith(b"ERMHDR"):
        detail = sess._detail(raw) if raw[:2] != b"PK" else "a zip file (not handled yet)"
        raise rest.EppmError(f"XER export refused (HTTP {st}): {detail}")
    path = os.path.join(folder, name)
    with open(path, "wb") as fh:
        fh.write(raw)
    return path


def pull_rest(profile: dict, project_ids: Iterable[str], password: str, include_baseline: bool = True,
              rest_opener: Optional[Callable] = None, server_module: Any = None) -> dict:
    """Pull by REST alone: the server's XER export of the projects (one file, so the ids stay the ObjectIds), built
    by the XER mode's store builder; their baselines from REST object reads (P6 EPPM refuses to export a baseline,
    measured on 25.12; p6mcp.eppm.mapping); then the schedule options from REST."""
    from p6mcp import xerstore
    from p6mcp.eppm import mapping, rest
    wanted = _wanted(profile, project_ids)
    started = datetime.now()
    tmp = _temp(profile)
    work = tempfile.mkdtemp(prefix=".export.", dir=store_dir(profile["name"]))
    warnings: list[str] = []
    out: Optional[sqlite3.Connection] = None
    try:
        with _session(profile, password, rest_opener) as sess:
            listed, projects, sched_rows = _server_info(sess, wanted, include_baseline)
            xer = _export(sess, [p["object_id"] for p in projects], work, "projects.xer")
            manifest = xerstore.build([xer], tmp)
            warnings += manifest["files"][0]["warnings"]
            counts = dict(manifest["files"][0]["rows"])
            out = sqlite3.connect(tmp)
            cols = {r[1].lower() for r in out.execute("PRAGMA table_info(PROJECT)")}
            if "sum_base_proj_id" not in cols:
                out.execute("ALTER TABLE PROJECT ADD COLUMN sum_base_proj_id INTEGER")
            done: set[int] = set()
            for p in projects:
                bl = p["baseline_object_id"]
                if not bl:
                    continue
                try:
                    if bl not in done:
                        for t, n in mapping.baseline(sess, out, bl, p["object_id"], warnings).items():
                            counts[f"baseline {t}"] = counts.get(f"baseline {t}", 0) + n
                        done.add(bl)
                    out.execute("UPDATE PROJECT SET sum_base_proj_id = ? WHERE proj_id = ?", (bl, p["object_id"]))
                except (rest.EppmError, ValueError, sqlite3.Error) as e:
                    warnings.append(f"{p['project_id']}: baseline not pulled: {e}")
                    p["baseline_object_id"] = None
        _finish(out, profile, projects, sched_rows, warnings, server_module)
        out.commit()
        out.close()
        out = None
    except BaseException:
        if out is not None:
            try:
                out.close()
            except sqlite3.Error:
                pass
        _drop(tmp)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return _write(profile, tmp, "rest", started, listed, projects, counts, warnings)


def _finish(out: sqlite3.Connection, profile: dict, projects: list[dict], sched_rows: dict, warnings: list[str],
            server_module: Any) -> None:
    """Support tables, indexes and the schedule options of the working copy."""
    from p6mcp import xerstore
    out.row_factory = sqlite3.Row  # the server's writers read rows by column name
    xerstore._support_tables(out, None)
    have = {r[0].upper() for r in out.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for t, c in INDEXES:
        if t in have:
            out.execute(f"CREATE INDEX IF NOT EXISTS ix_{t}_{c} ON {t} ({c})")
    for t, c in LOCAL_KEYS:  # local ids start above the server's (the server's own counters are not ours to use)
        key = f"{t}_{c}"
        if out.execute("UPDATE NEXTKEY SET key_seq_num = ? WHERE lower(key_name) = lower(?)",
                       (LOCAL_ID_BASE, key)).rowcount == 0:
            out.execute("INSERT INTO NEXTKEY (key_name, key_seq_num) VALUES (?, ?)", (key, LOCAL_ID_BASE))
    s = server_module
    if s is None:
        from p6mcp import server as s
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    s._cols_cache.clear()
    try:
        for p in projects:
            row = sched_rows.get(p["object_id"])
            if not row:
                warnings.append(f"{p['project_id']}: no schedule options from the server, P6 defaults assumed")
                continue

            def task_ids(code: str, pid=p["object_id"]) -> Optional[int]:
                r = out.execute("SELECT task_id FROM TASK WHERE proj_id = ? AND task_code = ?", (pid, code)).fetchone()
                return int(r[0]) if r else None
            vals, lpl, warn = sched_values(row, task_ids)
            warnings += [f"{p['project_id']}: {w}" for w in warn]
            try:
                vals = {k: (v if k == "key_activity_for_multiple_longest_paths"
                            else s._norm_opt("scheduling", k, v, lenient=True)[0]) for k, v in vals.items()}
                s._set_props(out, p["object_id"], "scheduling", vals, lpl, now)
            except (ValueError, sqlite3.Error) as e:
                warnings.append(f"{p['project_id']}: schedule options not stored: {e}")
    finally:
        s._cols_cache.clear()  # the cache describes the active database, not this copy
