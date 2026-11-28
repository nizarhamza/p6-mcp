"""P6 Professional on SQL Server or Oracle (p6mcp.pmdb): Test connection, the copy, the refresh, the read tools on the
copy, the refused writes, the secrets and the Settings panel, against the fakes of tests/fixtures/fake_pmdb.py.

The fake servers hold the public demo database (make_demo.build_db: DEMO-A and DEMO-SS225 in EPS-DEMO, DEMO-SS225's
project baseline DEMO-SS225_BL and its saved update DEMO-SS225_PREV), with a name outside any code page (NVARCHAR)
and a template project under the EPS. No real SQL Server or Oracle is involved.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3

import pytest

import fake_pmdb
import make_demo
from conftest import fingerprint, sha

from p6mcp import config, pmdb, policy, settings_logic
from p6mcp.eppm import store

PW = "Db-pass-1"
CUR, BL, PREV = make_demo.PROJ, make_demo.PROJ_BL, make_demo.PROJ_PREV
UNICODE_NAME = "Permis, été č 東"
DIALECTS = ("mssql", "oracle")


@pytest.fixture(scope="module")
def demo_build(server, tmp_path_factory):
    work = tmp_path_factory.mktemp("pmdb_demo")
    db = str(work / "demo.db")
    make_demo.build_db(db, str(work))
    con = sqlite3.connect(db)
    con.execute("UPDATE TASK SET task_name = ? WHERE task_code = 'A1060'", (UNICODE_NAME,))
    con.execute("INSERT INTO UDFTYPE (udf_type_id, table_name, udf_type_label, logical_data_type) "
                "VALUES (1, 'TASK', 'Area', 'FT_TEXT')")  # one activity UDF with a value in DEMO-SS225
    con.execute("INSERT INTO UDFVALUE (udf_type_id, fk_id, proj_id, udf_text) SELECT 1, task_id, proj_id, 'North' "
                f"FROM TASK WHERE proj_id = (SELECT proj_id FROM PROJECT WHERE proj_short_name = '{CUR}') LIMIT 3")
    # a template project under the EPS: an EPS selection leaves it out
    con.execute("INSERT INTO PROJECT (proj_id, project_flag, proj_short_name, clndr_id) VALUES (900, 'Y', 'TPL-1', 1)")
    con.execute("INSERT INTO PROJWBS (wbs_id, proj_id, seq_num, proj_node_flag, wbs_short_name, wbs_name, "
                "parent_wbs_id, status_code) VALUES (9000, 900, 9, 'Y', 'TPL-1', 'Template', 1, 'WS_Template')")
    con.commit()
    con.close()
    return db


@pytest.fixture
def env(p6, demo_build, tmp_path, monkeypatch):
    """The demo as the database of the session (database mode) and, beside it, as the database of a fake server."""
    shutil.copyfile(demo_build, p6.db)
    con = sqlite3.connect(p6.db)  # the template stays on the server only (an EPS copy leaves it out)
    con.execute("DELETE FROM PROJWBS WHERE proj_id = 900")
    con.execute("DELETE FROM PROJECT WHERE proj_id = 900")
    con.commit()
    con.close()
    p6.s._cols_cache.clear()
    p6.s._schema_cache.clear()
    p6.set(trusted_schemas=[fingerprint(p6.db)])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    src = str(tmp_path / "server.db")
    shutil.copyfile(demo_build, src)
    vault = {"pmdb:plant": PW}
    monkeypatch.setattr("p6mcp.credentials.get", lambda k: vault.get(k))
    monkeypatch.setattr("p6mcp.credentials.set", lambda k, v: vault.__setitem__(k, v))
    monkeypatch.setattr("p6mcp.credentials.delete", lambda k: vault.pop(k, None) is not None)
    drivers = {d: fake_pmdb.driver(src, d) for d in DIALECTS}
    monkeypatch.setattr("p6mcp.eppm.sql.driver_module", lambda d: drivers[d])
    return {"p6": p6, "src": src, "vault": vault, "drivers": drivers, "tmp": tmp_path}


def factory(env, dialect):
    return lambda _d: env["drivers"][dialect]


def rows(db, q, args=()):
    con = sqlite3.connect(db)
    try:
        return con.execute(q, args).fetchall()
    finally:
        con.close()


def use_pmdb(env, dialect="mssql", **over):
    prof = fake_pmdb.profile(dialect, confirmed=True, **over)
    env["p6"].set(data_source="pmdb", pmdb_profiles=[prof], pmdb_active="plant")
    return prof


def only_selects(drv) -> bool:
    return all(re.match(r"\s*SELECT\b", s, re.I) or s.strip().upper() == "SET TRANSACTION READ ONLY"
               for s in drv.statements())


# ----------------------------------------------------------------- Test connection

@pytest.mark.parametrize("dialect", DIALECTS)
def test_connection_reads_version_projects_and_selection(env, dialect):
    drv = env["drivers"][dialect]
    prof = fake_pmdb.profile(dialect, projects=["demo-ss225", "NOPE"], eps=["eps-demo", "NO-EPS"])
    r = pmdb.test_profile(prof, PW, factory(env, dialect))
    assert r["ok"], r
    assert r["p6_version"] == {"database_version": "SYNTHETIC,2512.0000.0000.0001", "version": "25.12"}
    assert r["projects"] == 3  # DEMO-A, DEMO-SS225 and the template; baselines are not projects
    sel = r["selection"]
    assert sel["projects"] == ["DEMO-A", "DEMO-SS225"] and sel["baselines"] == 1
    assert sel["missing"] == {"projects": ["NOPE"], "eps": ["NO-EPS"]} and sel["templates_skipped"] == ["TPL-1"]
    assert sel["eps"] == {"EPS-DEMO": ["DEMO-A", "DEMO-SS225"]}
    assert only_selects(drv)
    text = " ".join(drv.statements())
    if dialect == "oracle":  # the dialect's own parameter style and one-row limit; the fake refuses the other one
        assert ":1" in text and "FETCH FIRST 1 ROWS ONLY" in text and "TOP 1" not in text and "%s" not in text
        assert all(c.read_only for c in drv.connections)  # SET TRANSACTION READ ONLY: one point in time
    else:
        assert "%s" in text and "TOP 1" in text and "FETCH FIRST" not in text
    assert all(c.closed for c in drv.connections)

    bad = pmdb.test_profile(prof, "wrong-pass", factory(env, dialect))
    assert not bad["ok"] and "Login failed" in bad["error"] and "wrong-pass" not in json.dumps(bad)
    noschema = pmdb.test_profile(dict(prof, schema=""), PW, factory(env, dialect))
    assert not noschema["ok"] and "cannot be read" in noschema["error"]  # the login does not own the tables
    missing = pmdb.test_profile(prof, PW, lambda _d: None)
    assert missing["driver_missing"] == ("pymssql" if dialect == "mssql" else "oracledb")
    assert pmdb.test_profile(dict(prof, driver="db2"), PW)["error"].startswith("Choose SQL Server or Oracle")
    assert pmdb.digest(prof) == r["digest"] != pmdb.digest(dict(prof, host="other"))
    assert pmdb.digest(prof) == pmdb.digest(dict(prof, projects=["X"]))  # the selection is not the connection


def test_tls_options_reach_the_driver(env):
    for dialect, want in (("mssql", {"encryption": "require"}),
                          ("oracle", {"protocol": "tcps", "service_name": "P6PDB", "host": "db.test"})):
        drv = env["drivers"][dialect]
        assert pmdb.test_profile(fake_pmdb.profile(dialect, tls=True), PW, factory(env, dialect))["ok"]
        assert want.items() <= drv.calls[-1].items()
        assert pmdb.test_profile(fake_pmdb.profile(dialect), PW, factory(env, dialect))["ok"]
        assert "encryption" not in drv.calls[-1] and "protocol" not in drv.calls[-1]


# ----------------------------------------------------------------- the copy

def _norm(v, oracle):
    if oracle and v == "":
        return None  # Oracle keeps no empty string
    return v


@pytest.mark.parametrize("dialect", DIALECTS)
def test_copy_keeps_every_row_and_value(env, dialect):
    """The whole demo selected (its EPS, every baseline): each table copied equals the server's, value for value
    (dates back as text, decimals as numbers, LOBs as bytes, NVARCHAR text), with number columns typed REAL."""
    prof = fake_pmdb.profile(dialect, eps=["EPS-DEMO"], baselines="all")
    m = pmdb.copy(prof, PW, factory(env, dialect))
    assert [p["project_id"] for p in m["projects"]] == ["DEMO-A", CUR]
    assert sorted(b["project_id"] for b in m["baselines"]) == [BL, PREV]
    assert m["projects"][1]["baseline"] == BL and m["p6_version"]["version"] == "25.12"
    assert any("TPL-1" in w for w in m["warnings"])
    copy_db, src = pmdb.store_path("plant"), env["src"]
    oracle = dialect == "oracle"
    copied = {r[0] for r in rows(copy_db, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    compared = 0
    for (t,) in rows(src, "SELECT name FROM sqlite_master WHERE type = 'table'"):
        if t not in copied or t in ("PROJECT", "PROJWBS"):
            continue
        where = next(w for name, w, _o in pmdb.TABLES if name == t) if t in {x[0] for x in pmdb.TABLES} else ""
        if where:  # every project is copied: the same filter on the server's table
            ids = [r[0] for r in rows(src, "SELECT proj_id FROM PROJECT WHERE proj_id <> 900")]
            where = " WHERE " + store._in_lists(where, ids).replace("{", "").replace("}", "")
        want = sorted((tuple(_norm(v, oracle) for v in r) for r in rows(src, f"SELECT * FROM {t}{where}")), key=repr)
        got = sorted(rows(copy_db, f"SELECT * FROM {t}"), key=repr)
        assert got == want, t
        compared += 1
    assert compared >= 20 and m["rows"]["TASK"] == rows(src, "SELECT COUNT(*) FROM TASK")[0][0]
    keep = "proj_id <> 900"  # the template project is not copied
    for t in ("PROJECT", "PROJWBS"):
        want = sorted((tuple(_norm(v, oracle) for v in r) for r in rows(src, f"SELECT * FROM {t} WHERE {keep}")),
                      key=repr)
        assert sorted(rows(copy_db, f"SELECT * FROM {t}"), key=repr) == want, t
    assert rows(copy_db, "SELECT DISTINCT typeof(remain_drtn_hr_cnt), typeof(task_id) FROM TASK") == [
        ("real", "integer")]
    assert {r[0] for r in rows(copy_db, "SELECT typeof(early_start_date) FROM TASK")} == {"text"}
    for q in ("SELECT clndr_id, typeof(clndr_data) FROM CALENDAR ORDER BY 1",  # BLOB stays BLOB, CLOB text
              "SELECT memo_id, typeof(task_memo) FROM TASKMEMO ORDER BY 1"):
        assert rows(copy_db, q) == rows(src, q) and {r[1] for r in rows(src, q)} == {"blob", "text"}
    assert rows(copy_db, "SELECT task_name FROM TASK WHERE task_code = 'A1060'") == [(UNICODE_NAME,)]
    assert only_selects(env["drivers"][dialect])


def test_selection_by_project_and_baselines(env):
    for mode, want in (("current", [BL]), ("none", []), ("all", [BL, PREV])):
        m = pmdb.copy(fake_pmdb.profile("mssql", projects=["demo-ss225"], baselines=mode), PW,
                      factory(env, "mssql"))
        assert [p["project_id"] for p in m["projects"]] == [CUR]
        assert sorted(b["project_id"] for b in m["baselines"]) == want
        names = {r[0] for r in rows(pmdb.store_path("plant"), "SELECT proj_short_name FROM PROJECT "
                                                               "WHERE project_flag = 'Y'")}
        assert names == {CUR, *want}
    with pytest.raises(pmdb.CopyError, match="NOPE"):
        pmdb.copy(fake_pmdb.profile("mssql", projects=["NOPE"]), PW, factory(env, "mssql"))
    with pytest.raises(pmdb.CopyError, match="Nothing to copy"):
        pmdb.copy(fake_pmdb.profile("mssql"), PW, factory(env, "mssql"))


def test_long_id_lists_are_cut_for_oracle(env):
    """Oracle takes at most 1000 items in one IN list: the copy splits them (a 1500 ids selection here)."""
    where = store._in_lists("proj_id IN ({S}) AND x NOT IN ({S})", range(2500))
    assert where.count("proj_id IN (") == 3 and " OR " in where and where.count("x NOT IN (") == 3
    assert store._in_lists("proj_id IN ({S})", []) == "proj_id IN (NULL)"
    prof = fake_pmdb.profile("oracle")
    drv = env["drivers"]["oracle"]
    con = drv.connect(user="reader", password=PW, dsn="x")
    out = sqlite3.connect(":memory:")
    ids = list(range(10_000, 11_499)) + [int(rows(env["src"], f"SELECT proj_id FROM PROJECT WHERE "
                                                                f"proj_short_name = '{CUR}'")[0][0])]
    n = store._copy_table(con.cursor(), out, prof, "TASK", "proj_id IN ({S})", ids)
    assert n == rows(env["src"], f"SELECT COUNT(*) FROM TASK WHERE proj_id = {ids[-1]}")[0][0] > 0
    with pytest.raises(fake_pmdb.FakeError, match="ORA-01795"):
        con.cursor().execute("SELECT * FROM ADMUSER.TASK WHERE proj_id IN (" + ",".join(map(str, ids)) + ")")


def test_failed_copy_keeps_the_previous_one(env):
    prof = fake_pmdb.profile("mssql", projects=[CUR])
    pmdb.copy(prof, PW, factory(env, "mssql"))
    before, man = sha(pmdb.store_path("plant")), pmdb.read_manifest("plant")
    with pytest.raises(pmdb.CopyError, match="Login failed") as ex:
        pmdb.copy(prof, "wrong-pass", factory(env, "mssql"))
    assert "wrong-pass" not in str(ex.value)
    with pytest.raises(pmdb.CopyError, match="NOPE"):
        pmdb.copy(dict(prof, projects=["NOPE"]), PW, factory(env, "mssql"))
    assert sha(pmdb.store_path("plant")) == before and pmdb.read_manifest("plant") == man
    assert [f for f in os.listdir(pmdb.store_dir("plant")) if f.startswith(".copy.")] == []  # no temp file left


# ----------------------------------------------------------------- the server on the copy

def tool_calls(p6) -> dict:
    """The read tools a project review uses, engine, reporting and dashboard families included."""
    acts = p6.call("p6_query_activities", project=CUR, limit=200)["rows"]
    code = next(a["task_code"] for a in acts if a.get("status_code") == "TK_Active")
    finish = p6.call("p6_milestones", project=CUR)["rows"][-1]["task_code"]
    return {
        "list_projects": ("p6_list_projects", {}),
        "list_projects_bl": ("p6_list_projects", {"include_baselines": True}),
        "list_eps": ("p6_list_eps", {}),
        "list_wbs": ("p6_list_wbs", {"project": CUR}),
        "calendars": ("p6_list_calendars", {"project": CUR, "include_exceptions": True}),
        "calendar_check": ("p6_calendar_check", {"project": CUR}),
        "udfs": ("p6_list_udfs", {}),
        "options": ("p6_schedule_options", {"project": CUR}),
        "summary": ("p6_project_summary", {"project": CUR}),
        "query": ("p6_query_activities", {"project": CUR, "limit": 100}),
        "activity": ("p6_get_activity", {"project": CUR, "task_code": code}),
        "relationships": ("p6_get_relationships", {"project": CUR, "task_code": code}),
        "lookahead": ("p6_lookahead", {"project": CUR}),
        "milestones": ("p6_milestones", {"project": CUR}),
        "critical": ("p6_critical_path", {"project": CUR}),
        "driving": ("p6_driving_path", {"project": CUR, "target": finish}),
        "float_paths": ("p6_float_paths", {"project": CUR}),
        "float_origin": ("p6_float_origin", {"project": CUR, "task_code": code}),
        "float": ("p6_float_analysis", {"project": CUR}),
        "health": ("p6_health_check", {"project": CUR, "save_snapshot": False}),
        "dangling": ("p6_dangling", {"project": CUR}),
        "redundant": ("p6_redundant_links", {"project": CUR}),
        "progress": ("p6_progress_check", {"project": CUR}),
        "curve": ("p6_progress_curve", {"project": CUR}),
        "groups": ("p6_group_summary", {"project": CUR}),
        "resources": ("p6_resource_summary", {"project": CUR}),
        "loading": ("p6_resource_loading", {"project": CUR}),
        "compare": ("p6_compare_projects", {"project": CUR}),
        "integrity": ("p6_update_integrity", {"project": CUR, "other": PREV}),
        "trend": ("p6_trend", {"projects": [CUR, BL, PREV]}),
        "cpm": ("p6_cpm_validate", {"project": CUR}),
        "whatif": ("p6_schedule_whatif", {"project": CUR,
                                          "edits": [{"op": "duration", "task_code": code, "add_remaining_days": 5}]}),
        "risk": ("p6_schedule_risk", {"project": CUR, "iterations": 60, "workers": 1}),
        "sql": ("p6_sql_select", {"sql": "SELECT COUNT(*) AS n FROM TASK"}),
    }


DROP = {"copied_at", "copy_age_hours", "seconds", "elapsed_s", "engine_cache"}


def clean(v):
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items() if k not in DROP}
    if isinstance(v, list):
        return [clean(x) for x in v]
    return v


def first_diff(a, b, path=""):
    """Where two results first differ (for the failure message)."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if a.get(k, "<missing>") != b.get(k, "<missing>"):
                return first_diff(a.get(k, "<missing>"), b.get(k, "<missing>"), f"{path}.{k}")
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            if x != y:
                return first_diff(x, y, f"{path}[{i}]")
    return path, repr(a)[:200], repr(b)[:200]


def run_all(p6, calls) -> dict:
    out = {}
    for key, (tool, kw) in calls.items():
        try:
            out[key] = clean(json.loads(json.dumps(p6.call(tool, **kw), default=str)))
        except (ValueError, RuntimeError) as e:
            out[key] = f"refused: {e}"
    return out


@pytest.mark.parametrize("dialect", DIALECTS)
def test_read_tools_on_the_copy_give_the_database_answers(env, dialect):
    """Every read tool of a review gives on the copy exactly what it gives on the database itself."""
    p6 = env["p6"]
    calls = tool_calls(p6)
    want = run_all(p6, calls)
    assert not [k for k, v in want.items() if isinstance(v, str)], {k: v for k, v in want.items() if isinstance(v, str)}
    use_pmdb(env, dialect, eps=["EPS-DEMO"], baselines="all")
    res = p6.call("p6_pmdb_refresh")
    assert p6.s.DB_PATH == pmdb.store_path("plant") and res["rows"]["TASK"] > 200
    got = run_all(p6, calls)
    diff = [k for k in want if got[k] != want[k]]
    assert not diff, [(k, first_diff(want[k], got[k])) for k in diff]
    # the file tools read the copy too
    dash = str(env["tmp"] / "dash.xlsx")
    assert p6.call("p6_export_dashboard", project=CUR, path=dash)["path"] and os.path.isfile(dash)
    xer = str(env["tmp"] / "cur.xer")
    p6.call("p6_export_xer", project=CUR, path=xer)
    assert "%T\tTASK" in open(xer, encoding="cp1252", errors="replace").read()
    assert only_selects(env["drivers"][dialect])


def test_refresh_age_and_diagnose(env):
    p6 = env["p6"]
    prof = use_pmdb(env, projects=[CUR])
    assert "no local copy yet" in p6.s.SETTINGS.db_problem
    with pytest.raises(RuntimeError, match="no local copy yet"):
        p6.call("p6_list_projects")
    d = p6.call("p6_diagnose")
    assert d["pmdb_mode"]["copy"] is None and d["pmdb_mode"]["login"] == "reader"
    res = p6.call("p6_pmdb_refresh")
    assert [p["project_id"] for p in res["projects"]] == [CUR] and res["p6_version"] == "25.12"
    d = p6.call("p6_diagnose")
    pm = d["pmdb_mode"]
    assert pm["copy"]["copied_at"] == res["copied_at"] and pm["copy"]["age_hours"] == 0.0
    assert pm["copy"]["settings_changed_since"] is False and pm["local_copy"] == pmdb.store_path("plant")
    assert d["schema"]["label"].startswith("Local copy of a P6 Professional database") and not d["schema"]["writable"]
    assert d["writes"] == pmdb.READ_ONLY and "never written" in d["writes"]
    state = p6.call("p6_project_summary", project=CUR)["schedule_state"]
    assert state["copied_at"] == res["copied_at"] and state["copy_age_hours"] == 0.0
    m = dict(pmdb.read_manifest("plant"), copied_at="2026-01-01T08:00:00")
    from datetime import datetime
    assert pmdb.age_hours(m, now=datetime(2026, 1, 2, 9, 30)) == 25.5 and pmdb.age_hours(None) is None
    # a change made in P6 reaches the copy at the next refresh
    code = rows(env["src"], "SELECT task_code FROM TASK t JOIN PROJECT p ON p.proj_id = t.proj_id WHERE "
                            f"p.proj_short_name = '{CUR}' ORDER BY task_id LIMIT 1")[0][0]
    con = sqlite3.connect(env["src"])
    con.execute("UPDATE TASK SET task_name = 'Renamed in P6' WHERE task_code = ?", (code,))
    con.commit()
    con.close()
    assert p6.call("p6_get_activity", project=CUR, task_code=code)["task_name"] != "Renamed in P6"
    p6.call("p6_pmdb_refresh")
    assert p6.call("p6_get_activity", project=CUR, task_code=code)["task_name"] == "Renamed in P6"
    # a failed refresh keeps the copy in use
    before = sha(pmdb.store_path("plant"))
    env["vault"]["pmdb:plant"] = "wrong-pass"
    with pytest.raises(ValueError, match="Login failed") as ex:
        p6.call("p6_pmdb_refresh")
    assert "wrong-pass" not in str(ex.value) and sha(pmdb.store_path("plant")) == before
    env["vault"].clear()
    with pytest.raises(ValueError, match="No password is stored"):
        p6.call("p6_pmdb_refresh")
    assert p6.call("p6_list_projects")["total"] == 1  # still the copy
    p6.set(pmdb_profiles=[dict(prof, projects=[CUR, "DEMO-A"])])
    assert p6.call("p6_diagnose")["pmdb_mode"]["copy"]["settings_changed_since"] is True
    p6.set(data_source="database")
    with pytest.raises(ValueError, match="Not in P6 Professional SQL Server / Oracle mode"):
        p6.call("p6_pmdb_refresh")


def test_writes_are_refused_and_nothing_is_written(env):
    p6 = env["p6"]
    use_pmdb(env, "oracle", projects=[CUR])
    p6.set(allow_writes=True)
    p6.call("p6_pmdb_refresh")
    st = config.load()
    assert st.pmdb == "plant" and st.allow_writes is False  # forced off: write tools are not even listed
    assert not policy.listed("p6_update_activity", st.tool_profile, st.allow_writes)
    copy_hash, src_hash = sha(pmdb.store_path("plant")), sha(env["src"])
    code = rows(pmdb.store_path("plant"), "SELECT task_code FROM TASK WHERE task_type = 'TT_Task' LIMIT 1")[0][0]
    for tool, kw in (("p6_update_activity", {"project": CUR, "task_code": code, "task_name": "x"}),
                     ("p6_add_relationship", {"project": CUR, "predecessor_code": code, "successor_code": code}),
                     ("p6_delete_activity", {"project": CUR, "task_code": code}),
                     ("p6_import_xer", {"path": "x.xer"}), ("p6_backup_database", {}),
                     ("p6_set_schedule_options", {"project": CUR, "options": {"sched_retained_logic": "Y"}})):
        with pytest.raises(ValueError, match="database server is never written"):
            p6.call(tool, **kw)
    with pytest.raises(RuntimeError, match="never written"):
        p6.s.p6_update_activity(CUR, code, task_name="x", confirm=True)
    with pytest.raises(RuntimeError, match="never written"):
        p6.s.p6_backup_database()
    p6.call("p6_health_check", project=CUR, save_snapshot=False)
    assert sha(pmdb.store_path("plant")) == copy_hash and sha(env["src"]) == src_hash
    assert only_selects(env["drivers"]["oracle"])
    from p6mcp import instructions
    assert instructions.build(config.read_file()[0]).startswith(instructions.PMDB_HEADER)


def test_secrets_stay_out_of_the_config_and_the_results(env):
    p6 = env["p6"]
    prof = use_pmdb(env, projects=[CUR])
    with pytest.raises(config.ConfigError, match="secrets must not be stored"):
        config.save(dict(p6.cfg, pmdb_profiles=[dict(prof, password=PW)]), os.environ["P6MCP_CONFIG"])
    outs = [pmdb.test_profile(prof, PW, factory(env, "mssql")), p6.call("p6_pmdb_refresh"), p6.call("p6_diagnose"),
            pmdb.test_profile(prof, "wrong-" + PW, factory(env, "mssql"))]
    text = json.dumps(outs, default=str)
    assert PW not in text, text[max(0, text.find(PW) - 300):text.find(PW) + 50]
    assert "password" not in text.lower().replace("with password '***'", "")
    assert PW not in open(os.environ["P6MCP_CONFIG"], encoding="utf-8").read()
    for f in os.listdir(pmdb.store_dir("plant")):
        assert PW.encode() not in open(os.path.join(pmdb.store_dir("plant"), f), "rb").read(), f
    assert not rows(pmdb.store_path("plant"), "SELECT name FROM sqlite_master WHERE name = 'USERS'")


def test_config_checks():
    base = fake_pmdb.profile("mssql")
    ok = config.validate({"pmdb_profiles": [base], "pmdb_active": "plant", "data_source": "pmdb"})
    assert ok["pmdb_profiles"][0]["name"] == "plant"
    for bad, msg in (({"driver": "db2"}, "driver must be one of"), ({"port": 0}, "port must be"),
                     ({"baselines": "some"}, "baselines must be one of"), ({"projects": "A1"}, "list of IDs"),
                     ({"tls": "yes"}, "tls must be true or false"), ({"host": 5}, "host must be text")):
        with pytest.raises(config.ConfigError, match=msg):
            config.validate({"pmdb_profiles": [dict(base, **bad)]})
    with pytest.raises(config.ConfigError, match="pmdb_active 'x' is not in pmdb_profiles"):
        config.validate({"pmdb_profiles": [base], "pmdb_active": "x"})
    with pytest.raises(config.ConfigError, match="used twice"):
        config.validate({"pmdb_profiles": [base, base]})
    assert config.DB_KINDS == ("sqlite",) and "pmdb" in config.DATA_SOURCES


def test_mode_without_a_confirmed_database(env):
    p6 = env["p6"]
    p6.set(data_source="pmdb", pmdb_profiles=[], pmdb_active=None)
    assert "no database selected" in p6.s.SETTINGS.db_problem
    p6.set(pmdb_profiles=[fake_pmdb.profile("mssql")], pmdb_active="plant")
    assert "not tested and confirmed" in p6.s.SETTINGS.db_problem
    with pytest.raises(RuntimeError, match="not tested and confirmed"):
        p6.call("p6_list_projects")


def test_settings_logic_profiles():
    data, name = settings_logic.add_pmdb_profile({}, "oracle")
    p = settings_logic.find_pmdb(data, name)
    assert name == "P6 database" and p["port"] == 1521 and p["schema"] == "ADMUSER" and not p["confirmed"]
    data, name2 = settings_logic.add_pmdb_profile(data, "mssql")
    assert name2 == "P6 database 2"
    res = {"ok": True, "digest": pmdb.digest(p), "tested_at": "2026-11-28T10:00:00",
           "p6_version": {"version": "25.12"}, "projects": 3}
    data = settings_logic.confirm_pmdb(data, name, res)
    assert data["pmdb_active"] == name and settings_logic.find_pmdb(data, name)["p6_version"] == "25.12"
    data = settings_logic.update_pmdb_profile(data, name, projects=["A", "B"], baselines="all")
    assert settings_logic.find_pmdb(data, name)["confirmed"]  # the selection is not the connection
    data = settings_logic.update_pmdb_profile(data, name, host="other")
    assert not settings_logic.find_pmdb(data, name)["confirmed"] and data["pmdb_active"] is None
    with pytest.raises(config.ConfigError, match="changed after the test"):
        settings_logic.confirm_pmdb(data, name, res)
    with pytest.raises(config.ConfigError, match="Unknown database server setting"):
        settings_logic.update_pmdb_profile(data, name, password="x")
    data = settings_logic.update_pmdb_profile(data, name, new_name="Plant")
    assert settings_logic.find_pmdb(data, "Plant") and not settings_logic.find_pmdb(data, name)
    data = settings_logic.remove_pmdb_profile(data, "Plant")
    assert [p["name"] for p in data["pmdb_profiles"]] == [name2]
    config.validate(data)
    assert pmdb.split_ids("A1, B 2;C3\nA1") == ["A1", "B 2", "C3", "A1"]

