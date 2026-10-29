"""p6_update_integrity on the public demo: DEMO-SS225 (update, data date 26 Oct 2026) against DEMO-SS225_PREV (the
previous update, 19 Oct 2026, built by make_demo.previous() with the changes of PREV_CHANGES). Every flag is
checked against what the fixture changed, the reverse comparison, the drill-down page, the refusals, the DCMA
counts before and after through p6_health_check_compare on snapshots of the two projects, nothing written."""

import shutil

import pytest

from conftest import fingerprint

import make_demo  # noqa: E402  (tests/fixtures is on sys.path, see conftest)

CH = make_demo.PREV_CHANGES


@pytest.fixture(scope="module")
def demo_db(server, tmp_path_factory):
    work = tmp_path_factory.mktemp("demo_integrity")
    db = str(work / "demo.db")
    make_demo.build_db(db, str(work))
    return db


@pytest.fixture
def demo(p6, demo_db):
    shutil.copyfile(demo_db, p6.db)
    p6.s._cols_cache.clear()
    p6.s._schema_cache.clear()
    p6.set(trusted_schemas=[fingerprint(p6.db)])
    return p6


def integrity(p6, **kw):
    return p6.call("p6_update_integrity", **{"project": make_demo.PROJ, "other": make_demo.PROJ_PREV, **kw})


def test_every_seeded_change_is_flagged(demo):
    before = demo.hash()
    r = integrity(demo)
    assert demo.hash() == before
    assert r["summary"] == {"open_end_to_finish": 1, "negative_float_cleared_by_constraint": 6, "must_finish_moved": 1,
                            "actuals_changed": 2, "duration_cut": 2, "links_removed_on_critical": 1,
                            "rel_type_or_lag_changed": 2, "schedule_options_changed": 1, "calendars_changed": 2,
                            "deleted_in_progress_or_critical": 1, "data_date": 0}
    assert r["raised"] == [k for k, n in r["summary"].items() if n] and r["raised_count"] == 10
    assert list(r["flags"]) == list(demo.s.INTEGRITY_FLAGS)
    f = {k: v["sample"] for k, v in r["flags"].items()}
    assert [(x["task_code"], x["side"], x["linked_to"], x["after"]) for x in f["open_end_to_finish"]] == [
        ("COM-HO-1020", "successor", "MS-1100", "FS 0 d to MS-1100")]
    code, ctype, cdate = CH["constraint_removed"]
    neg = {x["task_code"]: x for x in f["negative_float_cleared_by_constraint"]}
    assert neg[code]["cause"] == "its own constraint was removed or changed" and neg[code]["constraint_after"] == "none"
    assert neg[code]["constraint_before"] == f"Finish On or Before {cdate:%Y-%m-%d %H:%M:%S}"
    assert neg[code]["total_float_before_d"] < 0 <= neg[code]["total_float_after_d"]
    # its predecessor (the delivery) took its negative float from that constraint
    assert neg["PRC-AUX-1060"]["source"] == code and "source of its float" in neg["PRC-AUX-1060"]["cause"]
    must = [x for x in neg.values() if x["source"] == "must finish by"]
    assert len(must) == 4 and all(x["total_float_before_d"] < 0 <= x["total_float_after_d"] for x in must)
    assert f["must_finish_moved"] == [{"before": "2027-08-02 17:00:00", "after": "2027-08-16 17:00:00",
                                       "delta_cd": 14.0, "delta_wd": float(CH["must_finish_wd_earlier"])}]
    acts = {(x["task_code"], x["field"]): x for x in f["actuals_changed"]}
    start, fin = CH["actual_start"], CH["actual_finish"]
    assert acts[(start[0], "actual_start")]["before"] == f"{start[1]:%Y-%m-%d %H:%M:%S}"
    moved = acts[(start[0], "actual_start")]
    assert (moved["after"], moved["delta_cd"]) == ("2026-07-16 08:00:00", -7.0)
    assert acts[(fin[0], "actual_finish")]["before"] == f"{fin[1]:%Y-%m-%d %H:%M:%S}"
    cut = {x["task_code"]: x for x in f["duration_cut"]}
    assert {c: (x["original_before_d"], x["original_after_d"]) for c, x in cut.items()} == {
        "COM-ENR-1050": (30.0, 20.0), "CON-SEC-1060": (20.0, 15.0)}
    assert cut["COM-ENR-1050"]["critical_before"] and r["flags"]["duration_cut"]["critical"] == 1
    removed = [(x["pred_code"], x["succ_code"], x["successor_critical_before"]) for x in f["links_removed_on_critical"]]
    assert removed == [("COM-PRE-1090", "COM-ENR-1000", True)]
    rel = {(x["pred_code"], x["change"]): (x["before"], x["after"]) for x in f["rel_type_or_lag_changed"]}
    assert rel == {("CON-HV-1060", "type"): ("SS 10 d", "FS 0 d"), ("CON-FDN-1000", "lag"): ("FS 3 d", "FS 5 d")}
    assert f["schedule_options_changed"] == [{"option": CH["option"][0], "before": "Y", "after": "N"}]
    cal = {x["kind"]: x for x in f["calendars_changed"]}
    assert (cal["activity"]["item"], cal["activity"]["before"], cal["activity"]["after"]) == (
        CH["calendar"][0], make_demo.CAL6_NAME, make_demo.CAL_NAME)
    assert cal["calendar"]["item"] == make_demo.CAL_NAME and "2027-11-11" in cal["calendar"]["detail"]
    assert [(x["task_code"], x["status_before"]) for x in f["deleted_in_progress_or_critical"]] == [
        (make_demo.TEMP, "TK_Active")]
    assert r["data_date"] == {"before": "2026-10-19 08:00:00", "after": "2026-10-26 08:00:00", "delta_cd": 7.0,
                              "advanced": True}
    doc = " ".join(demo.s.p6_update_integrity.__doc__.split())
    assert "not proof of manipulation" in r["caveat"]
    assert "questions to raise" in doc and "not proof of manipulation" in doc


def test_reverse_page_and_refusals(demo):
    back = integrity(demo, project=make_demo.PROJ_PREV, other=make_demo.PROJ)
    assert back["summary"]["data_date"] == 1 and back["flags"]["data_date"]["sample"][0]["advanced"] is False
    assert back["summary"]["duration_cut"] == 0  # longer durations are no cut
    page = integrity(demo, flag="negative_float_cleared_by_constraint", limit=4)
    assert (page["total"], page["returned"], page["next_offset"], page["result"]["count"]) == (6, 4, 4, 6)
    assert integrity(demo, sample_size=1)["flags"]["negative_float_cleared_by_constraint"]["sample"] == page["rows"][:1]
    for kw, msg in (({"other": make_demo.PROJ}, "another project"), ({"flag": "nope"}, "flag must be"),
                    ({"near_critical_days": -1}, "0 or more"), ({"other": "NOPE"}, "not found")):
        with pytest.raises(Exception, match=msg):
            integrity(demo, **kw)


def test_dcma_before_and_after_through_snapshots(demo):
    prev = demo.call("p6_health_check", project=make_demo.PROJ_PREV)["snapshot"]["id"]
    cur = demo.call("p6_health_check", project=make_demo.PROJ)["snapshot"]["id"]
    cmp_ = demo.call("p6_health_check_compare", project=make_demo.PROJ, before=prev, after=cur)
    assert (cmp_["before"]["id"], cmp_["after"]["id"]) == (prev, cur)
    assert f"different projects: {make_demo.PROJ_PREV} -> {make_demo.PROJ}" in cmp_["warnings"]
    assert cmp_["before"]["data_date"] == "2026-10-19 08:00:00" and cmp_["after"]["data_date"] == "2026-10-26 08:00:00"
    assert "logic_density" in cmp_["checks"] and cmp_["engine_version"]["before"] == cmp_["engine_version"]["after"]
    assert cmp_["weighted_score"]["grade"]["after"] in ("A", "B", "C", "D", "F")
