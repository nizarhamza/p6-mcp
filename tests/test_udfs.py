"""User defined fields on the synthetic database: p6_list_udfs, the UDF filter and columns of
p6_query_activities, p6_update_udf / p6_update_udfs (two-step writes, all or nothing batches, refusals)."""

import sqlite3

import pytest

TYPES = ((501, "Weight", "FT_FLOAT_2_DECIMALS"), (502, "Remarks", "FT_TEXT"), (503, "Due date", "FT_END_DATE"),
         (504, "Crew size", "FT_INT"), (505, "Status flag", "FT_STATICTYPE"))


def udfs(p6) -> None:
    """Five activity UDFs; Weight on A1010 (2) and A1020 (3), Remarks on A1010, Due date on A1020."""
    con = sqlite3.connect(p6.db)
    con.executemany("INSERT INTO UDFTYPE (udf_type_id, table_name, udf_type_label, logical_data_type) "
                    "VALUES (?, 'TASK', ?, ?)", TYPES)
    con.executemany("INSERT INTO UDFVALUE (udf_type_id, fk_id, proj_id, udf_number, udf_text, udf_date) "
                    "VALUES (?, ?, 2, ?, ?, ?)", [(501, 102, 2.0, None, None), (501, 103, 3.0, None, None),
                                                  (502, 102, None, "check supplier", None),
                                                  (503, 103, None, None, "2026-11-15 00:00:00")])
    con.commit()
    con.close()


def value(p6, uid, tid):
    r = p6.sql("SELECT udf_text, udf_number, udf_date, proj_id FROM UDFVALUE WHERE udf_type_id = ? AND fk_id = ?",
               uid, tid)
    return r[0] if r else None


def test_list_udfs(p6):
    udfs(p6)
    res = p6.call("p6_list_udfs", project="DEMO-A")
    rows = {r["label"]: r for r in res["rows"]}
    assert res["total"] == 5 and res["used"] == 3 and res["subjects"] == ["TASK"], res
    assert rows["Weight"]["data_type"] == "number" and rows["Weight"]["values"] == 2 and rows["Weight"]["sum"] == 5
    assert rows["Due date"]["min"] == "2026-11-15 00:00:00" and rows["Crew size"]["values"] == 0
    with pytest.raises(Exception, match="tables with UDFs"):
        p6.call("p6_list_udfs", table="NOPE")


def test_filter_and_columns(p6):
    udfs(p6)
    q = lambda **kw: p6.call("p6_query_activities", project="DEMO-A", **kw)  # noqa: E731
    assert [r["task_code"] for r in q(udf_filter=[{"udf": "weight", "op": "ge", "value": 2.5}])["rows"]] == ["A1020"]
    assert q(udf_filter=[{"udf": "Remarks", "op": "not_empty"}])["total"] == 1
    assert q(udf_filter=[{"udf": "Remarks", "op": "empty"}])["total"] == q()["total"] - 1
    assert q(udf_filter=[{"udf": "Due date", "op": "eq", "value": "2026-11-15"}])["total"] == 1  # the whole day
    assert q(udf_filter=[{"udf": "Due date", "op": "lt", "value": "2026-11-15"}])["total"] == 0
    assert q(udf_filter=[{"udf": "Remarks", "op": "contains", "value": "SUPPLIER"}])["total"] == 1
    rows = q(udfs=["Weight", "Remarks"], fields=["task_name"], code_prefix="A1010")["rows"]
    assert rows[0]["udfs"] == {"Weight": 2.0, "Remarks": "check supplier"}, rows
    with pytest.raises(Exception, match="Close labels"):
        q(udf_filter=[{"udf": "Nope"}])


def test_update_udf_two_steps(p6):
    udfs(p6)
    before = p6.hash()
    dry = p6.call("p6_update_udf", project="DEMO-A", task_code="A1030", udf="Weight", value=1.236)
    assert dry["dry_run"] and "None -> 1.24" in dry["would_do"][0] and p6.hash() == before, dry
    res = p6.apply("p6_update_udf", project="DEMO-A", task_code="A1030", udf="Weight", value=1.236)
    assert res["applied"] and "stale_projects" not in res and "no F9" in res["next"], res
    assert value(p6, 501, 104) == (None, 1.24, None, 2)
    res = p6.apply("p6_update_udf", project="DEMO-A", task_code="A1010", udf="Remarks", clear=True)
    assert res["applied"] and value(p6, 502, 102) is None, res


def test_batch_all_or_nothing(p6):
    udfs(p6)
    items = [{"task_code": "A1030", "udf": "Due date", "value": "2026-12-01"},
             {"task_code": "A1030", "udf": "Crew size", "value": 4},
             {"task_code": "A1030", "udf": "Status flag", "value": "udf_g2"}]
    dry = p6.call("p6_update_udfs", project="DEMO-A", items=items + [{"task_code": "A1030", "udf": "Crew size",
                                                                      "value": 2.5}])
    assert dry["would_refuse"] and "whole number" in dry["items"][-1]["error"], dry
    res = p6.apply("p6_update_udfs", project="DEMO-A", items=items)
    assert res["applied"], res
    assert value(p6, 503, 104)[2] == "2026-12-01 00:00:00" and value(p6, 504, 104)[1] == 4
    assert value(p6, 505, 104)[0] == "UDF_G2"


@pytest.mark.parametrize("kw, why", [
    ({"udf": "Due date", "value": "soon"}, "not a date"),
    ({"udf": "Status flag", "value": "red"}, "indicator"),
    ({"udf": "Remarks", "value": "x", "clear": True}, "not both"),
    ({"udf": "Remarks"}, "value is required"),
    ({"udf": "Nope", "value": "x"}, "Close labels"),
])
def test_refusals(p6, kw, why):
    udfs(p6)
    with pytest.raises(Exception, match=why):
        p6.call("p6_update_udf", project="DEMO-A", task_code="A1030", **kw)
