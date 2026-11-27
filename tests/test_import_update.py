"""p6_import_xer mode update on the synthetic database (3.4.0: match keys read once per table, UPDATEs sent in
batches). DEMO-A gets assignments (two of the same resource on one activity), an activity code and UDF values,
is exported, then changed by the test's SQL: names and values edited, a code assignment and a UDF value
soft-deleted, a link and an assignment deleted, a second live activity with an existing Activity ID. The import
puts the XER back: oracles are the exported values and the match queries run by the test itself."""

import os
import sqlite3

ORACLE_TASK = "SELECT task_id FROM TASK WHERE proj_id = 2 AND task_code = ? AND delete_session_id IS NULL"


def write(p6, *statements) -> None:
    con = sqlite3.connect(p6.db)
    try:
        for q, args in statements:
            con.executemany(q, args) if isinstance(args, list) else con.execute(q, args)
        con.commit()
    finally:
        con.close()


def exported(p6) -> str:
    write(p6, ("INSERT INTO RSRC (rsrc_id, rsrc_short_name, rsrc_name, rsrc_type) VALUES (301, 'ENG', 'Engineer', "
               "'RT_Labor')", ()),
          ("INSERT INTO TASKRSRC (taskrsrc_id, task_id, proj_id, rsrc_id, rsrc_type, target_qty, remain_qty) "
           "VALUES (?, ?, 2, 301, 'RT_Labor', ?, ?)", [(401, 103, 40, 40), (402, 103, 80, 80), (403, 102, 60, 30)]),
          ("INSERT INTO ACTVTYPE (actv_code_type_id, actv_code_type, actv_code_type_scope) "
           "VALUES (701, 'Phase', 'AS_Global')", ()),
          ("INSERT INTO ACTVCODE (actv_code_id, actv_code_type_id, short_name, actv_code_name) VALUES (?, 701, ?, ?)",
           [(711, "DES", "Design"), (712, "CON", "Construction")]),
          ("INSERT INTO TASKACTV (task_id, actv_code_type_id, actv_code_id, proj_id) VALUES (?, 701, ?, 2)",
           [(102, 711), (103, 712), (104, 712)]),
          ("INSERT INTO UDFTYPE (udf_type_id, table_name, udf_type_label, logical_data_type) "
           "VALUES (501, 'TASK', 'Weight', 'FT_FLOAT_2_DECIMALS')", ()),
          ("INSERT INTO UDFVALUE (udf_type_id, fk_id, proj_id, udf_number) VALUES (501, ?, 2, ?)",
           [(102, 2.0), (103, 3.0)]))
    path = os.path.join(os.path.dirname(p6.db), "demo_update.xer")
    p6.call("p6_export_xer", project="DEMO-A", path=path)
    return path


def changed(p6) -> None:
    gone = "delete_session_id = 9, delete_date = '2026-03-05 10:00:00'"
    write(p6, ("UPDATE TASK SET task_name = 'changed' WHERE proj_id = 2", ()),
          ("INSERT INTO TASK (task_id, proj_id, wbs_id, clndr_id, task_code, task_name, task_type, status_code, "
           "remain_drtn_hr_cnt, target_drtn_hr_cnt) SELECT 150, proj_id, wbs_id, clndr_id, task_code, 'second A1030', "
           "task_type, status_code, remain_drtn_hr_cnt, target_drtn_hr_cnt FROM TASK WHERE task_id = 104", ()),
          ("UPDATE TASKRSRC SET remain_qty = 1 WHERE taskrsrc_id IN (401, 402)", ()),
          ("DELETE FROM TASKRSRC WHERE taskrsrc_id = 403", ()),
          ("DELETE FROM TASKPRED WHERE task_pred_id = (SELECT MIN(task_pred_id) FROM TASKPRED)", ()),
          ("UPDATE TASKACTV SET actv_code_id = 711 WHERE task_id = 103", ()),
          (f"UPDATE TASKACTV SET {gone} WHERE task_id = 104", ()),
          (f"UPDATE UDFVALUE SET {gone}, udf_number = 0 WHERE fk_id = 103", ()))


def test_update_import_matches_as_the_row_queries(p6, monkeypatch):
    path = exported(p6)
    names = dict(p6.sql("SELECT task_id, task_name FROM TASK WHERE proj_id = 2"))
    links = p6.sql("SELECT COUNT(*) FROM TASKPRED")[0][0]
    changed(p6)
    # two live A1030 rows: the XER row goes to the one the activity query matches (the first by rowid)
    assert [r[0] for r in p6.sql(ORACLE_TASK, "A1030")] == [104, 150]
    from p6mcp.core import props  # the schedule option writer, the import's only caller of _update
    calls, upd = [], p6.s._update
    monkeypatch.setattr(props, "_update", lambda con, table, *a: (calls.append(table), upd(con, table, *a)))
    before = p6.hash()
    kw = dict(path=path, mode="update", project="DEMO-A")
    dry = p6.call("p6_import_xer", **kw)
    assert p6.hash() == before and not dry["would_refuse"], dry
    c = dry["counts"]
    assert (c["TASK"]["updated"], c["TASK"]["inserted"]) == (7, 0)
    assert (c["TASKPRED"]["updated"], c["TASKPRED"]["inserted"]) == (links - 1, 1)
    assert (c["TASKRSRC"]["updated"], c["TASKRSRC"]["inserted"]) == (2, 1)
    assert (c["TASKACTV"]["updated"], c["TASKACTV"]["inserted"]) == (3, 0)
    assert (c["UDFVALUE"]["updated"], c["UDFVALUE"]["inserted"]) == (2, 0)
    assert dry["missing_from_xer"]["TASK"]["count"] == 1  # the other A1030 is kept

    res = p6.apply("p6_import_xer", **kw)
    assert res["applied"] and res["counts"] == c, res
    assert set(calls) <= {"PROJPROP"}, calls  # no UPDATE row by row for the imported tables
    got = dict(p6.sql("SELECT task_id, task_name FROM TASK WHERE proj_id = 2"))
    assert got == {**names, 150: "second A1030"}
    # two ENG assignments on A1020: each XER row back on its own row, the deleted one inserted again
    assert p6.sql("SELECT taskrsrc_id, remain_qty FROM TASKRSRC WHERE task_id = 103 ORDER BY 1") == [(401, 40.0),
                                                                                                    (402, 80.0)]
    assert p6.sql("SELECT target_qty, remain_qty FROM TASKRSRC WHERE task_id = 102") == [(60.0, 30.0)]
    assert p6.sql("SELECT COUNT(*) FROM TASKPRED")[0][0] == links
    # soft-deleted code assignment and UDF value come back with the XER values
    assert p6.sql("SELECT task_id, actv_code_id, delete_session_id FROM TASKACTV ORDER BY 1") == \
        [(102, 711, None), (103, 712, None), (104, 712, None)]
    assert p6.sql("SELECT fk_id, udf_number, delete_session_id FROM UDFVALUE ORDER BY 1") == \
        [(102, 2.0, None), (103, 3.0, None)]
