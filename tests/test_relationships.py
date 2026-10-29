"""Relationship edits on the synthetic database: one link (update, delete) and the batch tools (all or nothing).

Fixture links (task_pred_id: predecessor -> successor, FS, no lag): 201 A1000 -> A1010, 202 A1010 -> A1020,
203 A1020 -> A1030, 204 A1030 -> A1040, 205 A1040 -> A1050, 206 A1000 -> A1060, 207 A1060 -> A1030. The lag
rule is the predecessor calendar (8 h a day). Oracles are the test's own SQL on TASKPRED.
"""

TID = {"A1000": 101, "A1010": 102, "A1020": 103, "A1030": 104, "A1040": 105, "A1050": 106, "A1060": 107}


def links(p6) -> dict[int, tuple]:
    """task_pred_id -> (predecessor task_id, successor task_id, type, lag hours)."""
    rows = p6.sql("SELECT task_pred_id, pred_task_id, task_id, pred_type, lag_hr_cnt FROM TASKPRED")
    return {r[0]: r[1:] for r in rows}


def test_update_then_delete_one_link(p6):
    kw = dict(project="DEMO-A", predecessor_code="A1060", successor_code="A1030")
    before = p6.hash()
    dry = p6.call("p6_update_relationship", **kw, rel_type="SS", lag_days=1.5)
    assert dry["dry_run"] and "type FS -> SS" in str(dry["would_do"]) and p6.hash() == before
    assert p6.apply("p6_update_relationship", **kw, rel_type="SS", lag_days=1.5)["applied"] is True
    assert links(p6)[207] == (TID["A1060"], TID["A1030"], "PR_SS", 12.0)

    res = p6.call("p6_update_relationship", **kw, rel_type="SS")  # already SS, no lag given
    assert res["applied"] is False and "Nothing to change" in res["error"] and "confirmation_token" not in res

    updated = p6.hash()
    dry = p6.call("p6_delete_relationship", **kw)
    assert dry["dry_run"] and "Delete SS A1060 -> A1030" in str(dry["would_do"]) and p6.hash() == updated
    assert p6.apply("p6_delete_relationship", **kw)["applied"] is True
    assert 207 not in links(p6) and len(links(p6)) == 6
    res = p6.call("p6_delete_relationship", **kw)
    assert res["applied"] is False and res["error"]


def test_add_relationships_all_or_nothing(p6):
    before = p6.hash()
    bad = p6.call("p6_add_relationships", project="DEMO-A", items=[
        {"predecessor_code": "A1010", "successor_code": "A1040"},
        {"predecessor_code": "A1000", "successor_code": "A1010"}])  # FS already links this pair
    assert bad["would_refuse"] and "confirmation_token" not in bad
    assert [i["ok"] for i in bad["items"]] == [True, False] and "already exists" in bad["items"][1]["error"]
    assert p6.hash() == before and len(links(p6)) == 7

    items = [{"predecessor_code": "A1010", "successor_code": "A1040"},
             {"predecessor_code": "A1020", "successor_code": "A1040", "rel_type": "SS", "lag_days": 2}]
    res = p6.apply("p6_add_relationships", project="DEMO-A", items=items)
    assert res["applied"] is True and [i["ok"] for i in res["items"]] == [True, True]
    new = [i["task_pred_id"] for i in res["items"]]
    got = links(p6)
    assert len(got) == 9 and len(set(new)) == 2 and all(n > 207 for n in new)
    assert got[new[0]] == (TID["A1010"], TID["A1040"], "PR_FS", 0.0)
    assert got[new[1]] == (TID["A1020"], TID["A1040"], "PR_SS", 16.0)
    assert res["side_effects"]["count"] == 0 and res["lag_rule"]


def test_update_relationships(p6):
    p6.apply("p6_add_relationship", project="DEMO-A", predecessor_code="A1010", successor_code="A1020", rel_type="SS")
    refused = p6.call("p6_update_relationships", project="DEMO-A", items=[
        {"predecessor_code": "A1000", "successor_code": "A1010", "lag_days": 1},
        {"predecessor_code": "A1010", "successor_code": "A1020", "current_type": "FS", "rel_type": "SS"}])
    assert refused["would_refuse"] and [i["ok"] for i in refused["items"]] == [True, False]
    assert "already links" in refused["items"][1]["error"]

    res = p6.apply("p6_update_relationships", project="DEMO-A", items=[
        {"predecessor_code": "A1000", "successor_code": "A1010", "lag_days": 1},
        {"predecessor_code": "A1040", "successor_code": "A1050", "rel_type": "FF"},
        {"predecessor_code": "A1010", "successor_code": "A1020", "current_type": "SS", "lag_days": 0.5}])
    assert res["applied"] is True
    got = links(p6)
    assert got[201] == (TID["A1000"], TID["A1010"], "PR_FS", 8.0)
    assert got[205] == (TID["A1040"], TID["A1050"], "PR_FF", 0.0)
    assert got[202] == (TID["A1010"], TID["A1020"], "PR_FS", 0.0)
    ss = [v for v in got.values() if v[:3] == (TID["A1010"], TID["A1020"], "PR_SS")]
    assert ss == [(TID["A1010"], TID["A1020"], "PR_SS", 4.0)]


def test_delete_relationships_reports_side_effects(p6):
    before = p6.hash()
    dry = p6.call("p6_delete_relationships", project="DEMO-A", ids=[206],
                  pairs=[{"predecessor_code": "A1060", "successor_code": "A1030"}])
    assert dry["dry_run"] and not dry["would_refuse"] and p6.hash() == before
    side = dry["side_effects"]
    assert [a["task_code"] for a in side["no_predecessor"]] == ["A1060"]
    assert [a["task_code"] for a in side["no_successor"]] == ["A1060"] and side["count"] == 2
    assert [(i["predecessor"], i["successor"], i["type"]) for i in dry["items"]] == [
        ("A1000", "A1060", "FS"), ("A1060", "A1030", "FS")]

    res = p6.apply("p6_delete_relationships", project="DEMO-A", ids=[206],
                   pairs=[{"predecessor_code": "A1060", "successor_code": "A1030"}])
    assert res["applied"] is True and set(links(p6)) == {201, 202, 203, 204, 205}


def test_delete_relationships_same_link_twice_and_unknown_id(p6):
    res = p6.call("p6_delete_relationships", project="DEMO-A", ids=[207],
                  pairs=[{"predecessor_code": "A1060", "successor_code": "A1030"}])
    assert not res["would_refuse"] and "already removed by item 0" in res["items"][1]["plan"]
    bad = p6.call("p6_delete_relationships", project="DEMO-A", ids=[999])
    assert bad["would_refuse"] and "not found" in bad["items"][0]["error"]
