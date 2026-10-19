"""Read tools never write: read-only connections, p6_sql_select limits, what-if in memory."""

import os
import sqlite3

import pytest

REFUSED = [
    "INSERT INTO TASK (task_id) VALUES (1)",
    "UPDATE TASK SET task_name = 'x'",
    "DELETE FROM TASK",
    "DROP TABLE TASK",
    "ATTACH DATABASE 'x.db' AS x",
    "PRAGMA writable_schema = 1",
    "PRAGMA query_only = 0",
    "VACUUM",
    "select 1; delete from TASK",
]


@pytest.mark.parametrize("sql", REFUSED)
def test_sql_select_refuses_anything_but_reading(p6, sql):
    before = p6.hash()
    with pytest.raises((ValueError, sqlite3.Error)):
        p6.call("p6_sql_select", sql=sql)
    assert p6.hash() == before


def test_sql_select_authorizer_blocks_hidden_writes(p6):
    with pytest.raises(ValueError, match="Only reading"):
        p6.call("p6_sql_select", sql="select * from pragma_table_info('TASK') where 0 union select load_extension('x'),"
                                     "1,1,1,1,1")


def test_sql_select_reads(p6):
    rows = p6.call("p6_sql_select", sql="WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 5) "
                                        "SELECT x FROM n")["rows"]
    assert [r["x"] for r in rows] == [1, 2, 3, 4, 5]
    cols = p6.call("p6_sql_select", sql="PRAGMA table_info(TASK)")["rows"]
    assert any(r["name"] == "task_code" for r in cols)
    cols = p6.call("p6_sql_select", sql="select name from pragma_table_info('TASKPRED') order by cid")["rows"]
    assert cols[0]["name"] == "task_pred_id"
    page = p6.call("p6_sql_select", sql="select task_code from TASK order by task_id", limit=3)
    assert page["total"] == 7 and page["returned"] == 3 and page["next_offset"] == 3


def test_sql_select_timeout(p6, monkeypatch):
    monkeypatch.setattr(p6.s, "SQL_QUERY_SECONDS", 0.3)
    slow = ("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 100000000) "
            "SELECT count(*) FROM n")
    with pytest.raises(ValueError, match="stopped after"):
        p6.call("p6_sql_select", sql=slow)


def test_read_connection_cannot_write(p6):
    con = p6.s._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("UPDATE TASK SET task_name = 'x'")
    finally:
        con.close()


def test_whatif_never_modifies_the_database(p6):
    before = p6.hash()
    res = p6.call("p6_schedule_whatif", project="DEMO-A",
                  edits=[{"op": "duration", "task_code": "A1020", "remaining_days": 25},
                         {"op": "delete_link", "predecessor_code": "A1030", "successor_code": "A1040"}])
    assert "engine" in res
    assert p6.hash() == before
    assert p6.sql("SELECT remain_drtn_hr_cnt FROM TASK WHERE task_code = 'A1020'")[0][0] == 120


def test_read_tools_leave_the_file_untouched(p6):
    before = p6.hash()
    for tool, kw in [("p6_list_projects", {}), ("p6_project_summary", {"project": "DEMO-A"}),
                     ("p6_health_check", {"project": "DEMO-A", "save_snapshot": False}),
                     ("p6_cpm_validate", {"project": "DEMO-A"}), ("p6_critical_path", {"project": "DEMO-A"})]:
        p6.call(tool, **kw)
    assert p6.hash() == before


def test_export_refuses_to_overwrite(p6, tmp_path):
    out = tmp_path / "demo.xer"
    assert p6.call("p6_export_xer", project="DEMO-A", path=str(out))["rows"]["TASK"] == 7
    with pytest.raises(ValueError, match="already exists"):
        p6.call("p6_export_xer", project="DEMO-A", path=str(out))
    assert p6.call("p6_export_xer", project="DEMO-A", path=str(out), overwrite=True)["rows"]["TASK"] == 7
    with pytest.raises(ValueError, match="database itself"):
        p6.call("p6_export_xer", project="DEMO-A", path=p6.db, overwrite=True)
    assert os.path.getsize(p6.db) > 0
