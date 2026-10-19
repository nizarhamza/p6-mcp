"""XER-only mode: synthetic XER files exported from the synthetic database, loaded into the store."""

import os
import sqlite3
import time

import pytest

from conftest import sha

from p6mcp import config, xerstore

SCHED = ("%T\tSCHEDOPTIONS\n%F\tschedoptions_id\tproj_id\tsched_calendar_on_relationship_lag\tsched_retained_logic\n"
         "%R\t1\t2\trcal_Successor\tY\n")


def add_schedoptions(path):
    text = open(path, encoding="cp1252").read()
    head, end = text.rsplit("%E", 1)
    open(path, "w", encoding="cp1252", newline="").write(head + SCHED.replace("\n", "\r\n") + "%E" + end)


@pytest.fixture
def xers(p6, tmp_path):
    """Two updates of DEMO-A: the baseline state, then 20 working days later with progress."""
    first = str(tmp_path / "demo_update_1.xer")
    p6.call("p6_export_xer", project="DEMO-A", path=first)
    con = sqlite3.connect(p6.db)
    con.execute("UPDATE TASK SET remain_drtn_hr_cnt = 40, phys_complete_pct = 75, early_end_date = "
                "'2026-03-06 17:00:00' WHERE task_code = 'A1010'")
    con.execute("UPDATE TASK SET early_end_date = '2026-04-10 17:00:00' WHERE task_code = 'A1020'")
    con.execute("UPDATE PROJECT SET last_recalc_date = '2026-03-30 08:00:00' WHERE proj_id = 2")
    con.commit()
    con.close()
    second = str(tmp_path / "demo_update_2.xer")
    p6.call("p6_export_xer", project="DEMO-A", path=second)
    add_schedoptions(second)
    return [first, second]


@pytest.fixture
def store_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))


def test_store_build_types_offsets_and_renames(xers, store_env):
    db, man = xerstore.ensure(xers)
    assert [p["project_id"] for f in man["files"] for p in f["projects"]] == ["DEMO-A", "DEMO-A~2"]
    assert man["files"][1]["id_offset"] == xerstore.ID_STEP and man["files"][0]["rows"]["TASK"] == 7
    con = sqlite3.connect(db)
    types = {r[1]: r[2] for r in con.execute("PRAGMA table_info(TASK)")}
    assert types["task_code"] == "TEXT" and types["task_id"] == "INTEGER" and types["remain_drtn_hr_cnt"] == "REAL"
    assert {"PREFER", "NEXTKEY", "PROJPROP", "TASKRSRC"} <= {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}  # TASKRSRC: in the file without rows
    ids = [r[0] for r in con.execute("SELECT task_id FROM TASK ORDER BY task_id")]
    assert ids[:7] == [101, 102, 103, 104, 105, 106, 107] and ids[7] == 101 + xerstore.ID_STEP
    preds = con.execute("SELECT COUNT(*) FROM TASKPRED p JOIN TASK s ON s.task_id = p.task_id "
                        "JOIN TASK q ON q.task_id = p.pred_task_id").fetchone()[0]
    assert preds == 14  # every link of both files still joins (ids shifted consistently)
    con.close()


def test_store_is_reused_until_a_file_changes(xers, store_env):
    db1, _ = xerstore.ensure(xers)
    t = os.path.getmtime(db1)
    db2, _ = xerstore.ensure(xers)
    assert db2 == db1 and os.path.getmtime(db2) == t
    time.sleep(0.05)
    with open(xers[0], "a", encoding="cp1252") as fh:
        fh.write("\r\n")
    db3, _ = xerstore.ensure(xers)
    assert db3 != db1


def test_bad_files(store_env, tmp_path):
    with pytest.raises(xerstore.XerStoreError, match="not found"):
        xerstore.ensure([str(tmp_path / "none.xer")])
    junk = tmp_path / "junk.xer"
    junk.write_text("hello", encoding="utf-8")
    with pytest.raises((xerstore.XerStoreError, ValueError)):
        xerstore.ensure([str(junk)])
    with pytest.raises(xerstore.XerStoreError):
        xerstore.build([], str(tmp_path / "x.db"))


@pytest.fixture
def xer_mode(p6, xers, store_env):
    p6.set(data_source="xer", xer_files=xers, xer_confirmed=True)
    yield p6
    p6.set(data_source="database", xer_files=[], xer_confirmed=False)


def test_read_tools_on_xer_files(xer_mode):
    p6 = xer_mode
    projects = [r["proj_short_name"] for r in p6.call("p6_list_projects")["rows"]]
    assert sorted(projects) == ["DEMO-A", "DEMO-A~2"]
    assert p6.s.DB_PATH.startswith(xerstore.store_dir())
    a = p6.call("p6_get_activity", project="DEMO-A~2", task_code="A1010")
    assert a["rem_dur_d"] == 5 and a["phys_complete_pct"] == 75
    cmp = p6.call("p6_compare_projects", project="DEMO-A~2", other="DEMO-A")
    var = {r["task_code"]: r.get("variance_cd") for r in cmp["rows"] if r.get("variance_cd")}
    assert var == {"A1010": -7.0, "A1020": 7.0}, cmp["rows"]
    rels = p6.call("p6_get_relationships", project="DEMO-A~2", task_code="A1030")
    assert rels["lag_rule"]["rule"] == "rcal_Successor"  # from the XER SCHEDOPTIONS
    assert p6.call("p6_get_relationships", project="DEMO-A", task_code="A1030")["lag_rule"]["rule"] == \
        "rcal_Predecessor"
    assert p6.call("p6_cpm_validate", project="DEMO-A")["project"] == "DEMO-A"
    assert p6.call("p6_sql_select", sql="select count(*) as n from TASK")["rows"][0]["n"] == 14
    d = p6.call("p6_diagnose")
    assert [f["file"] for f in d["xer_mode"]["files"]] == ["demo_update_1.xer", "demo_update_2.xer"]
    assert d["writes"].startswith("XER-only mode is read only")


def test_xer_mode_never_writes(xer_mode, xers):
    p6 = xer_mode
    before = [sha(f) for f in xers]
    store = p6.s.DB_PATH
    h = sha(store)
    with pytest.raises(ValueError, match="XER-only mode is read only"):
        p6.call("p6_update_activity", project="DEMO-A", task_code="A1020", physical_pct=10)
    with pytest.raises(RuntimeError, match="XER-only mode is read only"):
        p6.s.p6_update_activity("DEMO-A", "A1020", physical_pct=10, confirm=True)
    with pytest.raises(RuntimeError, match="XER-only mode is read only"):
        p6.s.p6_backup_database()
    p6.call("p6_health_check", project="DEMO-A", save_snapshot=False)
    assert sha(store) == h and [sha(f) for f in xers] == before
    assert config.load().allow_writes is False


def test_xer_mode_needs_a_load_check(p6, xers, store_env):
    p6.set(data_source="xer", xer_files=xers, xer_confirmed=False)
    try:
        with pytest.raises(RuntimeError, match="not loaded and checked"):
            p6.call("p6_list_projects")
        p6.set(xer_files=[], xer_confirmed=True)
        with pytest.raises(RuntimeError, match="no XER file selected"):
            p6.call("p6_list_projects")
        p6.set(xer_files=[xers[0] + ".gone"], xer_confirmed=True)
        with pytest.raises(RuntimeError, match="XER file not found"):
            p6.call("p6_list_projects")
    finally:
        p6.set(data_source="database", xer_files=[], xer_confirmed=False)
    assert p6.call("p6_list_projects")["total"] == 1  # back to the database
