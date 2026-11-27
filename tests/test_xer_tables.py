"""XER round trip of activity notebooks, steps, expenses, roles and risks (3.4.0) on the synthetic database.

Fixture (tests/fixtures/make_db.py, project DEMO-A): notebook topics Scope and Site notes, a BLOB notebook on A1010
and a TEXT one on A1030, three steps on A1010, two expenses (categories Materials and Travel), roles ENG > LEAD and a
second LEAD at the top (the same Role ID under another parent) with a price on ENG > LEAD, risk types
Technical > Design, risk R-01 linked to A1030. The tests add a role assignment on A1040 (role ENG > LEAD).
"""

import os
import sqlite3

import pytest
from conftest import fingerprint
from make_db import memo

DETAIL_TABLES = ("MEMOTYPE", "TASKMEMO", "TASKPROC", "COSTTYPE", "PROJCOST", "ROLES", "ROLERATE", "RISKTYPE",
                 "PROJRISK", "TASKRISK")


def add_role_assignment(p6) -> None:
    con = sqlite3.connect(p6.db)
    con.execute("INSERT INTO TASKRSRC (taskrsrc_id, task_id, proj_id, role_id, rsrc_id, target_qty, remain_qty) "
                "VALUES (1, 105, 2, 2, NULL, 16, 16)")
    con.commit()
    con.close()


def export(p6, name: str = "details.xer", **kw) -> str:
    path = os.path.join(os.path.dirname(p6.db), name)
    p6.last_export = p6.call("p6_export_xer", project="DEMO-A", path=path, **kw)
    return path


def text(v):
    """Memo value as P6 shows it (BOM and NUL of a BLOB dropped)."""
    if isinstance(v, bytes):
        v = v.rstrip(b"\x00").decode("utf-8-sig")
    return v


def details(p6, project: str) -> dict:
    """Rows of the project's notebooks, steps, expenses, role assignments and risks, ids replaced by names."""
    pid = p6.sql("SELECT proj_id FROM PROJECT WHERE proj_short_name = ?", project)[0][0]

    def q(sql):
        return sorted(tuple(text(v) for v in r) for r in p6.sql(sql, pid))
    return {
        "TASKMEMO": q("SELECT t.task_code, m.memo_type, x.task_memo, x.proj_id = t.proj_id FROM TASKMEMO x "
                      "JOIN TASK t ON t.task_id = x.task_id JOIN MEMOTYPE m ON m.memo_type_id = x.memo_type_id "
                      "WHERE t.proj_id = ?"),
        "TASKPROC": q("SELECT t.task_code, x.seq_num, x.proc_name, x.complete_flag, x.proc_wt, x.complete_pct, "
                      "x.proc_descr, x.proj_id = t.proj_id FROM TASKPROC x JOIN TASK t ON t.task_id = x.task_id "
                      "WHERE t.proj_id = ?"),
        "PROJCOST": q("SELECT t.task_code, x.cost_name, c.cost_type, x.po_number, x.vendor_name, x.act_cost, "
                      "x.cost_per_qty, x.remain_cost, x.target_cost, x.cost_load_type, x.auto_compute_act_flag, "
                      "x.target_qty, x.qty_name, x.cost_descr FROM PROJCOST x JOIN TASK t ON t.task_id = x.task_id "
                      "JOIN COSTTYPE c ON c.cost_type_id = x.cost_type_id WHERE x.proj_id = ?"),
        "TASKRSRC": q("SELECT t.task_code, r.role_short_name, pr.role_short_name FROM TASKRSRC x JOIN TASK t "
                      "ON t.task_id = x.task_id JOIN ROLES r ON r.role_id = x.role_id LEFT JOIN ROLES pr "
                      "ON pr.role_id = r.parent_role_id WHERE x.proj_id = ?"),
        "PROJRISK": q("SELECT x.risk_code, x.risk_name, x.status_code, k.risk_type, pk.risk_type, x.risk_desc, "
                      "x.risk_descr FROM PROJRISK x JOIN RISKTYPE k ON k.risk_type_id = x.risk_type_id "
                      "LEFT JOIN RISKTYPE pk ON pk.risk_type_id = k.parent_risk_type_id WHERE x.proj_id = ?"),
        "TASKRISK": q("SELECT t.task_code, r.risk_code, x.proj_id = t.proj_id FROM TASKRISK x JOIN TASK t "
                      "ON t.task_id = x.task_id JOIN PROJRISK r ON r.risk_id = x.risk_id WHERE t.proj_id = ?"),
    }


def table_rows(p6) -> dict:
    return {t: p6.sql(f"SELECT COUNT(*) FROM {t}")[0][0] for t in DETAIL_TABLES + ("TASKRSRC",)}


def test_export_writes_the_detail_tables(p6, server):
    add_role_assignment(p6)
    path = export(p6)
    rows = p6.last_export["rows"]
    assert {t: rows[t] for t in DETAIL_TABLES} == {
        "MEMOTYPE": 2, "TASKMEMO": 2, "TASKPROC": 3, "COSTTYPE": 2, "PROJCOST": 2,
        "ROLES": 2, "ROLERATE": 1, "RISKTYPE": 2, "PROJRISK": 1, "TASKRISK": 1}  # LEAD and its parent ENG
    assert p6.last_export["warnings"] == []
    with open(path, encoding="cp1252") as fh:
        order = [ln[3:].strip() for ln in fh.read().splitlines() if ln.startswith("%T\t")]
    assert order == [t for t in server.XER_EXPORT_TABLES if t in order] and len(order) == len(server.XER_EXPORT_TABLES)
    _h, x = server._xer_read(path)
    (blob,) = [r["task_memo"] for r in x["TASKMEMO"] if r["memo_type_id"] == "1"]
    assert blob == '<html><body>Design "basis" v2\n<p>Two lines &amp; a quote</p></body></html>'  # BOM and NUL dropped
    assert {r["role_short_name"]: r["parent_role_id"] for r in x["ROLES"]} == {"ENG": None, "LEAD": "1"}
    pre = p6.call("p6_inspect_xer", path=path)["preflight"]
    assert pre["errors"] == [] and "tables_not_imported" not in {w["check"] for w in pre["warnings"]}
    assert {t: pre["db_preview"][t] for t in ("MEMOTYPE", "COSTTYPE", "ROLES", "RISKTYPE")} == {
        t: {"matched": 2, "would_create": 0} for t in ("MEMOTYPE", "COSTTYPE", "ROLES", "RISKTYPE")}


def test_import_new_then_update_round_trip(p6):
    add_role_assignment(p6)
    path = export(p6)
    before = table_rows(p6)
    res = p6.apply("p6_import_xer", path=path, new_short_name="DEMO-B")
    counts = res["counts"]
    assert {t: counts[t]["matched"] for t in ("MEMOTYPE", "COSTTYPE", "ROLES", "RISKTYPE")} == dict.fromkeys(
        ("MEMOTYPE", "COSTTYPE", "ROLES", "RISKTYPE"), 2)
    assert counts["ROLERATE"]["kept_existing"] == 1
    assert {t: counts[t]["inserted"] for t in ("TASKMEMO", "TASKPROC", "PROJCOST", "PROJRISK", "TASKRISK")} == {
        "TASKMEMO": 2, "TASKPROC": 3, "PROJCOST": 2, "PROJRISK": 1, "TASKRISK": 1}
    assert details(p6, "DEMO-B") == details(p6, "DEMO-A")
    # notebooks are written as P6 stores them: a BLOB with a BOM and a NUL (the TEXT one too)
    assert {bytes(v) for (v,) in p6.sql("SELECT x.task_memo FROM TASKMEMO x JOIN PROJECT p ON p.proj_id = x.proj_id "
                                        "WHERE p.proj_short_name = 'DEMO-B'")} == {
        memo('<html><body>Design "basis" v2\n<p>Two lines &amp; a quote</p></body></html>'),
        memo("<p>Access by the north gate</p>")}
    after_new = table_rows(p6)
    assert {t: after_new[t] - before[t] for t in after_new} == {
        "MEMOTYPE": 0, "TASKMEMO": 2, "TASKPROC": 3, "COSTTYPE": 0, "PROJCOST": 2, "ROLES": 0, "ROLERATE": 0,
        "RISKTYPE": 0, "PROJRISK": 1, "TASKRISK": 1, "TASKRSRC": 1}

    upd = p6.apply("p6_import_xer", path=path, mode="update", project="DEMO-B")  # the same file again
    assert {t: (upd["counts"][t]["inserted"], upd["counts"][t]["updated"])
            for t in ("TASKMEMO", "TASKPROC", "PROJCOST", "PROJRISK", "TASKRISK")} == {
        "TASKMEMO": (0, 2), "TASKPROC": (0, 3), "PROJCOST": (0, 2), "PROJRISK": (0, 1), "TASKRISK": (0, 1)}
    assert table_rows(p6) == after_new and details(p6, "DEMO-B") == details(p6, "DEMO-A")


def test_import_creates_missing_globals_under_their_parents(p6):
    add_role_assignment(p6)
    path = export(p6)
    con = sqlite3.connect(p6.db)  # another database: the topics, categories, roles and risk types are named otherwise
    for t, c in (("MEMOTYPE", "memo_type"), ("COSTTYPE", "cost_type"), ("RISKTYPE", "risk_type")):
        con.execute(f"UPDATE {t} SET {c} = {c} || ' (old)'")
    con.execute("UPDATE ROLES SET role_short_name = 'ENG-OLD' WHERE role_id = 1")  # LEAD stays, under other parents
    con.commit()
    con.close()
    res = p6.apply("p6_import_xer", path=path, new_short_name="DEMO-C")
    counts = res["counts"]
    globals_ = ("MEMOTYPE", "COSTTYPE", "ROLES", "RISKTYPE")
    assert {t: (counts[t]["matched"], counts[t]["inserted"]) for t in globals_} == dict.fromkeys(globals_, (0, 2))
    assert counts["ROLERATE"]["inserted"] == 1  # prices come with a new role
    (eng,) = p6.sql("SELECT role_id FROM ROLES WHERE role_short_name = 'ENG' AND parent_role_id IS NULL")
    assert p6.sql("SELECT role_short_name FROM ROLES WHERE parent_role_id = ?", eng[0]) == [("LEAD",)]
    assert p6.sql("SELECT r.cost_per_qty FROM ROLERATE r JOIN ROLES o ON o.role_id = r.role_id "
                  "WHERE o.parent_role_id = ?", eng[0]) == [(95.0,)]
    got = details(p6, "DEMO-C")
    assert got["TASKRSRC"] == [("A1040", "LEAD", "ENG")]
    assert got["PROJRISK"][0][3:5] == ("Design", "Technical")
    assert [r[1] for r in got["TASKMEMO"]] == ["Scope", "Site notes"]


def test_missing_table_skipped_with_a_warning(p6, server):
    full = export(p6, "full.xer")
    con = sqlite3.connect(p6.db)
    con.execute("DROP TABLE TASKPROC")
    con.execute("DELETE FROM NEXTKEY WHERE key_name = 'projrisk_risk_id'")  # PROJRISK exists, without a counter
    con.commit()
    con.close()
    server._cols_cache.clear()  # the schema changed under the server
    p6.set(trusted_schemas=[fingerprint(p6.db)])
    export(p6, "no_steps.xer")
    assert "TASKPROC" not in p6.last_export["rows"]
    assert "TASKPROC is not in this database: not exported." in p6.last_export["warnings"]

    pre = p6.call("p6_inspect_xer", path=full)["preflight"]
    (w,) = [w for w in pre["warnings"] if w["check"] == "tables_not_in_database"]
    assert w["sample"] == ["PROJRISK: 1 rows", "TASKPROC: 3 rows"]
    res = p6.apply("p6_import_xer", path=full, new_short_name="DEMO-D")
    assert "TASKPROC is not in this database: 3 XER row(s) not imported." in res["warnings"]
    assert "PROJRISK has no NEXTKEY counter in this database: 1 XER row(s) not imported." in res["warnings"]
    assert res["counts"]["TASKRISK"]["skipped_unmapped"] == 1  # its risk was not imported
    assert res["counts"]["TASKMEMO"]["inserted"] == 2


def test_risk_table_alias_and_unmapped_details(p6, server):
    path = export(p6)
    with open(path, encoding="cp1252", newline="") as fh:
        body = fh.read().replace("%T\tPROJRISK", "%T\tPRORISK")  # the name of Oracle's XER data map
    lines = body.split("\n")
    at = lines.index("%T\tTASKPROC\r") + 1
    fields = lines[at].rstrip("\r").split("\t")
    row = lines[at + 1].rstrip("\r").split("\t")
    row[fields.index("task_id")] = "999"  # a step of an activity not in the file
    lines[at + 1] = "\t".join(row) + "\r"
    alias = os.path.join(os.path.dirname(p6.db), "alias.xer")
    with open(alias, "w", encoding="cp1252", newline="") as fh:
        fh.write("\n".join(lines))
    _h, x = server._xer_read(alias)
    assert "PRORISK" not in x and len(x["PROJRISK"]) == 1
    pre = p6.call("p6_inspect_xer", path=alias)["preflight"]
    (info,) = [i for i in pre["info"] if i["check"] == "activity_details_unmapped"]
    assert info["count"] == 1 and info["sample"][0].endswith("task 999")
    res = p6.apply("p6_import_xer", path=alias, new_short_name="DEMO-E")
    assert res["counts"]["TASKPROC"]["skipped_unmapped"] == 1 and res["counts"]["TASKPROC"]["inserted"] == 2
    assert res["counts"]["PROJRISK"]["inserted"] == 1 and res["counts"]["TASKRISK"]["inserted"] == 1


@pytest.mark.parametrize("what", ["activity", "project"])
def test_deletes_cover_the_detail_tables(p6, what):
    con = sqlite3.connect(p6.db)  # a UDF value on a step and one on an expense
    con.execute("INSERT INTO UDFTYPE (udf_type_id, table_name, udf_type_label, logical_data_type) VALUES "
                "(50, 'TASKPROC', 'Step owner', 'FT_TEXT'), (51, 'PROJCOST', 'Invoice', 'FT_TEXT')")
    con.execute("INSERT INTO UDFVALUE (udf_type_id, fk_id, proj_id, udf_text) VALUES (50, 1, NULL, 'Ann'), "
                "(51, 2, NULL, 'INV-9')")  # no proj_id: found through the step and the expense
    con.commit()
    con.close()
    if what == "activity":
        p6.apply("p6_delete_activity", project="DEMO-A", task_code="A1010", force=True)  # notebook, steps
        p6.apply("p6_delete_activity", project="DEMO-A", task_code="A1030")  # notebook, expense, risk link
        assert p6.sql("SELECT COUNT(*) FROM TASKMEMO")[0][0] == 0 and p6.sql("SELECT COUNT(*) FROM TASKPROC")[0][0] == 0
        assert p6.sql("SELECT task_id FROM PROJCOST") == [(103,)] and p6.sql("SELECT COUNT(*) FROM TASKRISK")[0][0] == 0
        assert p6.sql("SELECT COUNT(*) FROM PROJRISK")[0][0] == 1  # the risk belongs to the project
        assert p6.sql("SELECT udf_type_id FROM UDFVALUE WHERE udf_type_id IN (50, 51)") == []
    else:
        p6.apply("p6_delete_project", project=["DEMO-A"])
        assert {t: p6.sql(f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("TASKMEMO", "TASKPROC", "PROJCOST", "PROJRISK",
                                                                        "TASKRISK")} == dict.fromkeys(
            ("TASKMEMO", "TASKPROC", "PROJCOST", "PROJRISK", "TASKRISK"), 0)
        assert p6.sql("SELECT udf_type_id FROM UDFVALUE WHERE udf_type_id IN (50, 51)") == []
    # global rows stay
    assert {t: p6.sql(f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("MEMOTYPE", "COSTTYPE", "ROLES", "RISKTYPE")} == {
        "MEMOTYPE": 2, "COSTTYPE": 2, "ROLES": 3, "RISKTYPE": 2}
