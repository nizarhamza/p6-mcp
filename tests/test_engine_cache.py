"""The network cache (core/netcache.py, plan item S7): repeated engine calls reuse the loaded network, its base run,
the critical path test and the link network while the database is unchanged, with the same results as without the
cache (P6MCP_ENGINE_CACHE=0). Every write path of this server, a change by another connection (P6) in
rollback-journal and WAL mode, a commit inside one mtime tick and a change only the SQL facts see make the next call
read the new state; parallel calls get equal results; callers get copies."""

import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

AFTER = "2026-03-03 10:00:00"  # after the F9 of DEMO-A (2026-03-02 09:15)


def mods():
    """(core.db, core.netcache), imported once the server fixture has set the test config (never at collection)."""
    from p6mcp.core import db, netcache
    return db, netcache


def strip(x):
    """A result without its timing fields."""
    if isinstance(x, dict):
        return {k: strip(v) for k, v in x.items() if k != "seconds"}
    if isinstance(x, list):
        return [strip(v) for v in x]
    return x


def stats(p6) -> dict:
    return p6.call("p6_diagnose")["engine_cache"]


def write(path: str, *statements) -> None:
    """Another connection's change (P6, or the test itself), committed."""
    con = sqlite3.connect(path)
    try:
        for q, args in statements:
            con.execute(q, args)
        con.commit()
    finally:
        con.close()


CALLS = {
    "cpm_validate": lambda p6: p6.call("p6_cpm_validate", project="DEMO-A"),
    "health_check": lambda p6: p6.call("p6_health_check", project="DEMO-A", save_snapshot=False),
    "whatif": lambda p6: p6.call("p6_schedule_whatif", project="DEMO-A",
                                 edits=[{"op": "duration", "task_code": "A1020", "add_remaining_days": 5}]),
    "driving_path": lambda p6: p6.call("p6_driving_path", project="DEMO-A", target="A1060"),
    "float_origin": lambda p6: p6.call("p6_float_origin", project="DEMO-A", task_code="A1020"),
    "risk": lambda p6: p6.call("p6_schedule_risk", project="DEMO-A", iterations=100, workers=1),
}


def results(p6) -> dict:
    return {k: strip(f(p6)) for k, f in CALLS.items()}


def fresh(p6, monkeypatch) -> dict:
    """The same calls without the cache."""
    monkeypatch.setenv("P6MCP_ENGINE_CACHE", "0")
    try:
        return results(p6)
    finally:
        monkeypatch.delenv("P6MCP_ENGINE_CACHE")


def network(p6):
    """(cached copy, fresh load) of DEMO-A's engine network and link network, each as comparable data."""
    s = p6.s
    con = s._connect()
    try:
        con.execute("BEGIN")
        cpm, tk = s._cpm_load(con, 2)
        net = s._net_load(con, 2, tk)
        ref_cpm, ref_net = s._Cpm(con, 2), s._Net(con, 2)
        con.rollback()
    finally:
        con.close()
    return (shape(cpm), shape(net)), (shape(ref_cpm), shape(ref_net))


def shape(n) -> dict:
    """The data of a network (calendars by id)."""
    cal = lambda c: None if c is None else c.clndr_id  # noqa: E731
    out = {"t": {i: {k: (cal(v) if k == "cal" else v) for k, v in d.items()} for i, d in n.t.items()},
           "links": n.links, "prj": tuple(n.prj)}
    for k in ("asg", "opts", "ext_pred", "ext_succ", "wbs_parent", "code", "rcals", "by_succ"):
        if hasattr(n, k):
            v = getattr(n, k)
            out[k] = {i: [cal(c) for c in cs] for i, cs in v.items()} if k == "rcals" else \
                [{**x, "cal": cal(x.get("cal"))} for x in v] if k in ("ext_pred", "ext_succ") else v
    return out


def check_fresh(p6, monkeypatch) -> dict:
    """The cached results equal the uncached ones, and the cached network the database's."""
    got = results(p6)
    assert got == fresh(p6, monkeypatch)
    cached, ref = network(p6)
    assert cached == ref
    return got


def test_second_calls_hit_and_equal_the_uncached_results(p6, monkeypatch):
    _netcache = mods()[1]
    s0 = stats(p6)
    first = results(p6)
    s1 = stats(p6)
    second = results(p6)
    s2 = stats(p6)
    assert first == second == fresh(p6, monkeypatch)
    assert s2["hits"] - s1["hits"] > s1["hits"] - s0["hits"] and s2["misses"] == s1["misses"]
    assert s1["projects_cached"] >= 1 and s2["enabled"] is True and s2["max_projects"] == _netcache.ENGINE_CACHE_SIZE


def test_turned_off_by_the_environment(p6, monkeypatch):
    monkeypatch.setenv("P6MCP_ENGINE_CACHE", "0")
    s0 = stats(p6)
    results(p6)
    s1 = stats(p6)
    assert s1["enabled"] is False
    assert (s1["hits"], s1["misses"], s1["bypassed"]) == (s0["hits"], s0["misses"], s0["bypassed"])


def test_callers_get_copies(p6):
    """A caller that changes the network, its run or the critical path test it got leaves the cache intact."""
    s = p6.s
    con = s._connect()
    try:
        con.execute("BEGIN")
        cpm, tk = s._cpm_load(con, 2)
        drv: dict = {}
        res = s._cpm_run(cpm, tk, drv)
        assert drv and all(e[1] is res for e in drv.values())  # the drivers point at the caller's copy
        ref = {n: dict(r) for n, r in res.items()}
        next(iter(cpm.t.values()))["rem"] = 1e6
        cpm.links.clear()
        cpm.code["NEW"], cpm.opts["sched_float_type"], cpm.asg[:] = 1, "FT_SS", []
        next(iter(res.values()))["es"] = None
        net = s._net_load(con, 2, tk)
        net.links.clear()
        net.t.clear()
        cpt = s._cpt(con, 2)
        cpt["pass"], cpt["sample"] = "x", ["x"]
        cpm2, tk2 = s._cpm_load(con, 2)
        assert s._cpm_run(cpm2, tk2) == ref and cpm2.links and "NEW" not in cpm2.code
        assert s._net_load(con, 2).links and s._cpt(con, 2)["pass"] != "x"
        con.rollback()
    finally:
        con.close()
    cached, fresh_ = network(p6)
    assert cached == fresh_


def test_a_temp_table_connection_never_uses_the_cache(p6):
    s = p6.s
    con = s._connect()
    try:
        con.execute("BEGIN")
        con.execute("PRAGMA query_only = OFF")
        con.execute("CREATE TEMP TABLE TASK AS SELECT * FROM main.TASK")
        assert s._ticket(con, 2) is None
        con.rollback()
        assert s._ticket(con, 2) is None  # outside a transaction: no single snapshot
    finally:
        con.close()
    plain = sqlite3.connect(p6.db)  # not a connection of _connect
    try:
        plain.execute("BEGIN")
        assert s._ticket(plain, 2) is None
    finally:
        plain.close()


@pytest.mark.parametrize("path", ["activity", "relationship", "assignment", "options", "import", "restore"])
def test_every_write_path_invalidates(p6, monkeypatch, path):
    s = p6.s
    if path == "assignment":  # ENG on A1020 (not started) and A1010, rows of the copy as P6 stores them
        write(p6.db, ("INSERT INTO RSRC (rsrc_id, rsrc_short_name, rsrc_name, rsrc_type) VALUES "
                      "(301, 'ENG', 'Engineer', 'RT_Labor')", ()),
              *[("INSERT INTO TASKRSRC (taskrsrc_id, task_id, proj_id, rsrc_id, rsrc_type, cost_qty_link_flag, "
                 "target_qty, act_reg_qty, act_ot_qty, remain_qty, target_cost, act_reg_cost, act_ot_cost, "
                 "remain_cost) VALUES (?, ?, 2, 301, 'RT_Labor', 'Y', 120, ?, 0, ?, 6000, ?, 0, ?)",
                 (tid, task, done, left, done * 50, left * 50)) for tid, task, done, left in ((401, 103, 0, 120),
                                                                                             (402, 102, 60, 60))])
    if path == "restore":
        backup = os.path.basename(p6.apply("p6_backup_database")["backup"])
    if path == "import":
        xer = os.path.join(os.path.dirname(p6.db), "demo.xer")
        p6.call("p6_export_xer", project="DEMO-A", path=xer)
        write(p6.db, ("UPDATE TASK SET remain_drtn_hr_cnt = 200, update_date = ? WHERE task_code = 'A1020'", (AFTER,)))
    before = check_fresh(p6, monkeypatch)
    m0 = stats(p6)["misses"]
    if path == "activity":
        p6.apply("p6_update_activity", project="DEMO-A", task_code="A1020", remaining_duration_days=12)
    elif path == "relationship":
        p6.apply("p6_add_relationship", project="DEMO-A", predecessor_code="A1000", successor_code="A1060",
                 rel_type="SS", lag_days=2)
    elif path == "assignment":
        p6.apply("p6_update_assignment", project="DEMO-A", task_code="A1020", resource="ENG", budgeted_units=180)
    elif path == "options":
        p6.apply("p6_set_schedule_options", project="DEMO-A", options={"sched_float_type": "FT_FF"})
    elif path == "import":
        assert p6.apply("p6_import_xer", path=xer, mode="update", project="DEMO-A")["applied"]
    else:
        p6.apply("p6_update_activity", project="DEMO-A", task_code="A1020", remaining_duration_days=12)
        before = check_fresh(p6, monkeypatch)  # the restore puts the network back as it was before this edit
        assert p6.apply("p6_restore_backup", name=backup)["applied"]
    assert s._state._engine_cache == {}  # dropped at the commit
    after = check_fresh(p6, monkeypatch)
    assert stats(p6)["misses"] > m0
    assert after != before  # the write changed what the tools report


@pytest.mark.parametrize("wal", [False, True], ids=["rollback_journal", "wal"])
def test_a_change_by_another_connection(p6, monkeypatch, wal):
    """P6 writing the file while the server runs: rollback-journal mode, and WAL mode with P6's connection held open
    (its commits stay in the -wal file)."""
    _db = mods()[0]
    held = None
    if wal:
        held = sqlite3.connect(p6.db)
        assert held.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    try:
        before = check_fresh(p6, monkeypatch)
        sig = _db._file_sig(p6.db)
        q = ("UPDATE TASK SET remain_drtn_hr_cnt = remain_drtn_hr_cnt + 40, update_date = ? WHERE task_code = 'A1020'",
             (AFTER,))
        if held is not None:
            held.execute(*q)
            held.commit()
            assert _db._file_sig(p6.db)[3] != sig[3]  # the frames are in the -wal file
        else:
            write(p6.db, q)
        after = check_fresh(p6, monkeypatch)
        assert after != before
        if held is not None:  # a second commit in the same second: the -wal file grows
            sig = _db._file_sig(p6.db)
            held.execute("UPDATE TASK SET remain_drtn_hr_cnt = remain_drtn_hr_cnt + 8, update_date = ? "
                         "WHERE task_code = 'A1020'", (AFTER,))
            held.commit()
            assert _db._file_sig(p6.db) != sig
            assert check_fresh(p6, monkeypatch) != after
    finally:
        if held is not None:
            held.close()


def test_a_commit_within_one_mtime_tick(p6, monkeypatch):
    """Same size, mtime put back: the file change counter in the header still tells the commit apart."""
    _db = mods()[0]
    before = check_fresh(p6, monkeypatch)
    st = os.stat(p6.db)
    sig = _db._file_sig(p6.db)
    write(p6.db, ("UPDATE TASK SET remain_drtn_hr_cnt = remain_drtn_hr_cnt + 40 WHERE task_code = 'A1020'", ()))
    os.utime(p6.db, ns=(st.st_atime_ns, st.st_mtime_ns))
    now = _db._file_sig(p6.db)
    assert now[:2] == sig[:2] and now[2] != sig[2]
    assert check_fresh(p6, monkeypatch) != before


def test_the_sql_facts_alone_see_a_new_f9_and_a_stamped_change(p6, monkeypatch):
    """With a file signature that never changes, the facts read in the snapshot still invalidate: a new
    last_schedule_date, then an activity changed with a later update_date."""
    _db, _netcache = mods()
    monkeypatch.setattr(_db, "_file_sig", lambda path: ("same",))
    monkeypatch.setattr(_netcache, "_file_sig", lambda path: ("same",))
    v0 = strip(p6.call("p6_cpm_validate", project="DEMO-A"))
    assert strip(p6.call("p6_cpm_validate", project="DEMO-A")) == v0
    write(p6.db, ("UPDATE PROJECT SET last_schedule_date = ? WHERE proj_id = 2", (AFTER,)))
    m0 = stats(p6)["misses"]
    v1 = strip(p6.call("p6_cpm_validate", project="DEMO-A"))
    assert stats(p6)["misses"] > m0 and v1["project_context"]["last_schedule_date"] == AFTER
    write(p6.db, ("UPDATE TASK SET remain_drtn_hr_cnt = remain_drtn_hr_cnt + 40, update_date = '2026-03-04 10:00:00' "
                  "WHERE task_code = 'A1020'", ()))
    v2 = strip(p6.call("p6_cpm_validate", project="DEMO-A"))
    assert v2 == fresh(p6, monkeypatch)["cpm_validate"] and v2 != v1
    cached, ref = network(p6)
    assert cached == ref


def test_parallel_calls_get_equal_results(p6, monkeypatch):
    want = fresh(p6, monkeypatch)
    p6.s._state._engine_cache_clear()
    start = threading.Barrier(6)

    def one(k):
        start.wait()
        name = list(CALLS)[k % 3]  # cpm_validate, health_check, whatif: cold cache, misses at once
        return name, strip(CALLS[name](p6))

    with ThreadPoolExecutor(6) as ex:
        got = list(ex.map(one, range(6)))
    for name, r in got:
        assert r == want[name], name
    with ThreadPoolExecutor(6) as ex:  # warm: hits in parallel
        got = list(ex.map(lambda k: (list(CALLS)[k % 6], strip(CALLS[list(CALLS)[k % 6]](p6))), range(12)))
    for name, r in got:
        assert r == want[name], name


def test_least_recently_used_projects_leave(p6, monkeypatch):
    _db, _netcache = mods()
    monkeypatch.setattr(_netcache, "ENGINE_CACHE_SIZE", 2)
    p6.s._state._engine_cache_clear()
    sig = (_db._file_sig(p6.db), ("facts",))
    tickets = [_netcache._Ticket((p6.db, i), sig) for i in range(3)]
    e0 = stats(p6)["evicted"]
    for i, tk in enumerate(tickets):
        assert _netcache._cached(tk, "x", lambda i=i: i) == i
    assert stats(p6)["evicted"] == e0 + 1 and stats(p6)["projects_cached"] == 2
    assert _netcache._cached(tickets[0], "x", lambda: "rebuilt") == "rebuilt"  # the oldest left first
    assert _netcache._cached(tickets[2], "x", lambda: "rebuilt") == 2
