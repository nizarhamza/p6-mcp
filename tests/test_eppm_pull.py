"""P6 EPPM milestone 2: the SQL pull into the working copy, the schedule options from REST, the server tools on the
copy, and the on-demand driver install (docs/EPPM_PLAN.md).

The PMDB is stood in by the synthetic database read through sqlite3 as if it were the SQL driver (same DB-API
calls), and the REST side by a fake server on 127.0.0.1 built from the same database.
"""

import base64
import hashlib
import io
import json
import re
import sqlite3
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from p6mcp.eppm import drivers, store

USER, PW, SPW = "planner", "S3cret-pass", "sql-pass"
SCHED = {"ProjectObjectId": "2", "OutOfSequenceScheduleType": "Progress Override", "ComputeTotalFloatType":
         "Start Float = Late Start - Early Start", "RelationshipLagCalendar": "Successor Activity Calendar",
         "IgnoreOtherProjectRelationships": "true", "MakeOpenEndedActivitiesCritical": "true",
         "UseExpectedFinishDates": "false", "MaximumMultipleFloatPaths": "7", "StartToStartLagCalculationType": "true",
         "PriorityList": "[(0||priority_type(sort_type|DESC|source_field|priority_type)())]"}


class SqliteDriver:
    """The synthetic database read like a PMDB through the DB-API (what pymssql gives)."""

    def __init__(self, path):
        self.path = path

    def connect(self, **kw):
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)


BL, SHIFT = 9002, 9000  # the baseline's ObjectId, and how far its objects' ids move from the project's


def rest_objects(db) -> dict:
    """The synthetic project as P6 EPPM REST objects of a baseline (text values, ids moved by SHIFT): the reverse of
    p6mcp.eppm.mapping, so a pull must give the database's own column values back."""
    from p6mcp.eppm import mapping
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    def objs(table, spec, where=""):
        out = []
        for r in con.execute(f"SELECT * FROM {table} {where}"):
            row, keys = {}, set(r.keys())
            for f, c, k in spec:
                v = r[c] if c in keys else None
                if v is None:
                    continue
                if isinstance(k, dict):
                    v = next((t.title() for t, code in k.items() if code == v), v)
                elif k == "id":
                    v = v + SHIFT
                elif k == "date":
                    v = str(v).replace(" ", "T")
                row[f] = str(v)
            out.append(row)
        return out
    o = {"activity": objs("TASK", mapping.ACTIVITY), "relationship": objs("TASKPRED", mapping.RELATIONSHIP),
         "wbs": objs("PROJWBS", mapping.WBS, "WHERE proj_node_flag = 'N'"),
         "resourceAssignment": objs("TASKRSRC", mapping.ASSIGNMENT)}
    for a in o["activity"] + o["wbs"] + o["resourceAssignment"]:
        a["ProjectObjectId"] = str(BL)
    for r in o["relationship"]:
        r["ProjectObjectId"] = r["SuccessorProjectObjectId"] = r["PredecessorProjectObjectId"] = str(BL)
    cal = con.execute("SELECT clndr_id, clndr_name, day_hr_cnt FROM CALENDAR").fetchall()
    o["calendar"] = [{"ObjectId": str(c[0] + SHIFT), "Name": c[1], "HoursPerDay": str(c[2])} for c in cal]
    p = con.execute("SELECT * FROM PROJECT WHERE project_flag = 'Y'").fetchone()
    o["baselineProject"] = [{"ObjectId": str(BL), "Id": "DEMO-A_BL", "Name": "Demo baseline", "Status": "Active",
                             "DataDate": str(p["last_recalc_date"]).replace(" ", "T"),
                             "ScheduledFinishDate": str(p["scd_end_date"]).replace(" ", "T") + "-08:00",
                             "OriginalProjectObjectId": "2"}]
    con.close()
    return o


def make_handler(db, xer: bytes = b"", baseline: str = ""):
    objects = rest_objects(db) if baseline else {}
    con = sqlite3.connect(db)
    projects = [{"ObjectId": str(r[0]), "Id": r[1], "Name": r[2], "CurrentBaselineProjectObjectId": baseline,
                 "DataDate": "2026-01-05T08:00:00", "LastUpdateDate": "2026-01-06T10:00:00-08:00",
                 "LastScheduleDate": "2026-01-05T17:00:00-08:00", "Status": "Active"}
                for r in con.execute("SELECT p.proj_id, p.proj_short_name, w.wbs_name FROM PROJECT p JOIN PROJWBS w "
                                     "ON w.proj_id = p.proj_id AND w.proj_node_flag = 'Y' WHERE project_flag = 'Y'")]
    con.close()

    class H(BaseHTTPRequestHandler):
        filters: list = []
        exports: list = []

        def log_message(self, *a):
            pass

        def _send(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if self.path.startswith("/p6ws/restapi/login") and status == 200:
                self.send_header("Set-Cookie", "JSESSIONID=x1; Path=/p6ws")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            if self.path.startswith("/p6ws/restapi/login"):
                ok = self.headers.get("authToken") == base64.b64encode(f"{USER}:{PW}".encode()).decode()
                return self._send(200 if ok else 401, {"message": "ok" if ok else "Login failed"})
            if self.path.endswith("/export/exportXERProject"):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                H.exports.append(body.get("ProjectObjectId"))
                if not xer or body.get("FileType") != "XER":
                    return self._send(400, {"message": "export failed"})
                self.send_response(200)
                self.send_header("Content-Type", "text/xml; charset=UTF-8")  # as P6 EPPM 25.12 labels it
                self.send_header("Content-Length", str(len(xer)))
                self.end_headers()
                self.wfile.write(xer)
                return
            return self._send(200, {"message": "bye"})

        def do_GET(self):
            u = urlsplit(self.path)
            if "JSESSIONID=x1" not in (self.headers.get("Cookie") or ""):
                return self._send(401, {"message": "not signed in"})
            filt = parse_qs(u.query).get("Filter", [""])[0]
            H.filters.append((u.path, filt))
            entity = u.path.rsplit("/", 1)[1]
            if entity == "fields":
                return self._send(404, {"message": "no field list on this fake"})
            if entity == "project":
                return self._send(200, projects)
            if entity == "scheduleOptions":
                return self._send(200, [SCHED] if filt == "ProjectObjectId :eq: 2" else [])
            if entity in objects and (f":eq: {BL}" in filt or entity == "calendar"):
                rows = objects[entity]
                if entity == "calendar":  # ObjectId :eq: a :or: ObjectId :eq: b
                    want = set(re.findall(r"ObjectId :eq: (\d+)", filt))
                    rows = [r for r in rows if r["ObjectId"] in want]
                return self._send(200, rows)
            if entity == "activity":
                return self._send(200, [{"ObjectId": "1"}])
            return self._send(404, {"message": "no"})
    return H


@pytest.fixture
def eppm_env(tmp_path, monkeypatch, golden):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    handler = make_handler(golden)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    prof = {"name": "srv", "base_url": f"http://127.0.0.1:{srv.server_address[1]}", "context": "p6ws",
            "database": "PMDB", "user": USER, "auth": "login", "ca_bundle": None, "timeout_s": 10,
            "sql": {"driver": "mssql", "host": "db", "port": 1433, "service": "PMDB", "user": "r", "schema": "",
                    "table_prefix": ""}, "confirmed": True}
    yield {"profile": prof, "driver": SqliteDriver(golden), "golden": golden, "handler": handler}
    srv.shutdown()
    srv.server_close()


def rows(db, q):
    con = sqlite3.connect(db)
    try:
        return con.execute(q).fetchall()
    finally:
        con.close()


def test_pull_copies_the_project_and_maps_the_schedule_options(server, eppm_env):
    e = eppm_env
    m = store.pull_sql(e["profile"], ["demo-a"], PW, SPW, sql_module=e["driver"], server_module=server)
    assert [p["project_id"] for p in m["projects"]] == ["DEMO-A"] and m["route"] == "sql"
    assert m["server_stamp"] == "2026-01-06T10:00:00" and store.read_manifest("srv")["rows"] == m["rows"]
    wc = store.store_path("srv")
    for t, key in (("TASK", "task_id"), ("TASKPRED", "task_pred_id"), ("PROJWBS", "wbs_id"), ("CALENDAR", "clndr_id"),
                   ("TASKRSRC", "taskrsrc_id")):
        assert rows(wc, f"SELECT * FROM {t} ORDER BY {key}") == rows(e["golden"], f"SELECT * FROM {t} ORDER BY {key}")
    assert any("UDFCODE" in w for w in m["warnings"])  # an optional table the database lacks
    con = sqlite3.connect(wc)
    try:
        val = con.execute("SELECT prop_value FROM PROJPROP WHERE proj_id = 2 AND prop_name = 'scheduling'").fetchone()
    finally:
        con.close()
    text = val[0] if isinstance(val[0], str) else val[0].decode()
    for frag in ("sched_progress_override|Y", "sched_retained_logic|N", "sched_float_type|FT_SS",
                 "sched_calendar_on_relationship_lag|rcal_Successor", "sched_outer_depend_type|SD_None",
                 "sched_open_critical_flag|Y", "sched_use_expect_end_flag|N", "max_multiple_longest_path|7",
                 "priority_type(sort_type|DESC)"):
        assert frag in text, frag


def test_failed_pull_keeps_the_previous_copy(server, eppm_env):
    e = eppm_env
    store.pull_sql(e["profile"], ["DEMO-A"], PW, SPW, sql_module=e["driver"], server_module=server)
    before = hashlib.sha256(open(store.store_path("srv"), "rb").read()).hexdigest()
    with pytest.raises(store.PullError, match="NOPE"):
        store.pull_sql(e["profile"], ["NOPE"], PW, SPW, sql_module=e["driver"], server_module=server)

    class Broken(SqliteDriver):
        def connect(self, **kw):
            raise RuntimeError(f"Login failed for {kw['password']}")
    with pytest.raises(ValueError, match="SQL connection failed") as ex:
        store.pull_sql(e["profile"], [], PW, SPW, sql_module=Broken(e["golden"]), server_module=server)
    assert SPW not in str(ex.value)
    assert hashlib.sha256(open(store.store_path("srv"), "rb").read()).hexdigest() == before
    with pytest.raises(store.PullError, match="no SQL account"):
        store.pull_sql(dict(e["profile"], sql=None), ["DEMO-A"], PW, SPW, server_module=server)


def test_server_tools_in_eppm_mode(p6, eppm_env, monkeypatch):
    e = eppm_env
    vault = {"eppm:srv": PW, "eppm-sql:srv": SPW}
    monkeypatch.setattr("p6mcp.credentials.get", lambda k: vault.get(k))
    monkeypatch.setattr("p6mcp.eppm.sql.driver_module", lambda d: e["driver"])
    p6.set(data_source="eppm", eppm_profiles=[e["profile"]], eppm_active="srv")
    assert "no project pulled" in p6.s.SETTINGS.db_problem
    with pytest.raises(RuntimeError, match="no project pulled"):
        p6.call("p6_list_projects")
    st = p6.call("p6_eppm_status")
    assert st["pulled"] is False
    lst = p6.call("p6_eppm_projects")
    assert lst["total"] == 1 and lst["rows"][0]["project_id"] == "DEMO-A" and lst["rows"][0]["pulled"] is False
    res = p6.call("p6_eppm_pull", projects=["DEMO-A"])
    assert res["rows"]["TASK"] == 7 and p6.s.DB_PATH == store.store_path("srv")
    assert [r["proj_short_name"] for r in p6.call("p6_list_projects")["rows"]] == ["DEMO-A"]
    v = p6.call("p6_cpm_validate", project="DEMO-A")
    assert v["match_rate"] is not None
    with pytest.raises(ValueError, match="P6 EPPM mode is read only"):
        p6.call("p6_update_activity", project="DEMO-A", task_code="A1000", name="x")
    st = p6.call("p6_eppm_status")
    assert st["pulled"] and st["changed_on_server"]["activities"] == {"DEMO-A": 1}
    assert ("/p6ws/restapi/activity", "ProjectObjectId :eq: 2 :and: LastUpdateDate:gt:'2026-01-06 10:00:00'") \
        in e["handler"].filters
    assert p6.call("p6_eppm_projects", search="demo")["rows"][0]["pulled"] is True
    d = p6.call("p6_diagnose")
    assert d["eppm_mode"]["projects"] == ["DEMO-A"]
    vault.clear()
    with pytest.raises(ValueError, match="No P6 password is stored"):
        p6.call("p6_eppm_projects")
    p6.set(data_source="database")
    with pytest.raises(ValueError, match="Not in P6 EPPM mode"):
        p6.call("p6_eppm_status")


def test_rest_route_builds_the_copy_from_the_xer_export(p6, tmp_path, monkeypatch):
    """No SQL account: the server's XER export (the project, then its baseline) through the XER mode's builder,
    schedule options and baseline link from REST. The ids of the project stay the server's ObjectIds."""
    xer = str(tmp_path / "demo.xer")
    p6.call("p6_export_xer", project="DEMO-A", path=xer)
    data = open(xer, "rb").read()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    handler = make_handler(p6.db, data, baseline=str(BL))  # its REST objects stand in for its baseline
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        prof = {"name": "srv", "base_url": f"http://127.0.0.1:{srv.server_address[1]}", "context": "p6ws",
                "database": "PMDB", "user": USER, "auth": "login", "ca_bundle": None, "timeout_s": 10, "sql": None,
                "confirmed": True}
        vault = {"eppm:srv": PW}
        monkeypatch.setattr("p6mcp.credentials.get", lambda k: vault.get(k))
        p6.set(data_source="eppm", eppm_profiles=[prof], eppm_active="srv")
        res = p6.call("p6_eppm_pull", projects=["DEMO-A"])
        assert res["route"] == "rest" and res["rows"]["TASK"] == 7 and handler.exports == [[2]]  # no baseline XER
        assert res["rows"]["baseline TASK"] == 7 and not [w for w in res["warnings"] if "baseline" in w]
        wc = store.store_path("srv")
        assert rows(wc, "SELECT task_id, task_code FROM TASK WHERE proj_id = 2 ORDER BY task_id") == \
            rows(p6.db, "SELECT task_id, task_code FROM TASK ORDER BY task_id")  # ids = the server's
        assert rows(wc, "SELECT sum_base_proj_id FROM PROJECT WHERE proj_id = 2") == [(BL,)]
        assert rows(wc, f"SELECT orig_proj_id, proj_short_name FROM PROJECT WHERE proj_id = {BL}") == \
            [(2, "DEMO-A_BL")]
        # the baseline's objects come back as the database's own column values (mapping round trip)
        from p6mcp.eppm import mapping
        golden_cols = {r[1] for r in sqlite3.connect(p6.db).execute("PRAGMA table_info(TASK)")}
        cols = [c for _f, c, _k in mapping.ACTIVITY if c in golden_cols and c not in ("proj_id",)]
        got = rows(wc, f"SELECT {', '.join(cols)} FROM TASK WHERE proj_id = {BL} ORDER BY task_id")
        want = rows(p6.db, f"SELECT {', '.join(cols)} FROM TASK ORDER BY task_id")

        def norm(r):
            return tuple((v - SHIFT if c.endswith("_id") and isinstance(v, int) else
                          str(v)[:19] if isinstance(v, str) and re.match(r"\d{4}-\d\d-\d\d", v) else
                          float(v) if isinstance(v, (int, float)) else v) for c, v in zip(cols, r, strict=True))
        want_n = [tuple((str(v)[:19] if isinstance(v, str) and re.match(r"\d{4}-\d\d-\d\d", v) else
                         float(v) if isinstance(v, (int, float)) else v) for v in r) for r in want]
        assert [norm(r) for r in got] == want_n
        assert rows(wc, f"SELECT COUNT(*) FROM TASKPRED WHERE proj_id = {BL}") == rows(p6.db, "SELECT COUNT(*) "
                                                                                        "FROM TASKPRED")
        assert rows(wc, f"SELECT clndr_data IS NOT NULL FROM CALENDAR WHERE clndr_id > {SHIFT}") == [(1,)] * len(
            rows(p6.db, "SELECT clndr_id FROM CALENDAR"))  # working time taken from the calendar of the same name
        summary = p6.call("p6_project_summary", project="DEMO-A")
        assert summary["baseline"]["used"] == "DEMO-A_BL" and summary["baseline"]["finish"] is not None
        lst = {r["proj_short_name"]: r for r in p6.call("p6_list_projects", include_baselines=True)["rows"]}
        assert lst["DEMO-A_BL"]["baseline_of"] == "DEMO-A"
        prop = rows(wc, "SELECT prop_value FROM PROJPROP WHERE proj_id = 2 AND prop_name = 'scheduling'")[0][0]
        assert "sched_progress_override|Y" in (prop if isinstance(prop, str) else prop.decode())
        assert p6.call("p6_cpm_validate", project="DEMO-A")["match_rate"] is not None
        with pytest.raises(ValueError, match="no read-only SQL account"):
            p6.call("p6_eppm_pull", projects=["DEMO-A"], route="sql")
        handler.exports.clear()
        res = p6.call("p6_eppm_pull", projects=["DEMO-A"], include_baseline=False)
        assert handler.exports == [[2]] and res["projects"][0]["baseline_object_id"] is None
    finally:
        srv.shutdown()
        srv.server_close()


# ----------------------------------------------------------------- driver install

def wheel(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


class FakePypi:
    def __init__(self, blobs: dict, meta_sha: dict = None):
        self.blobs, self.meta_sha, self.urls = blobs, meta_sha or {}, []

    def open(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        if url.endswith("/json"):
            name = url.split("/pypi/")[1].split("/")[0]
            fname = f"{name}-1.0-{drivers.py_tag()}-{drivers.py_tag()}-win_amd64.whl"
            sha = self.meta_sha.get(name) or hashlib.sha256(self.blobs[name]).hexdigest()
            body = {"info": {"version": "1.0"}, "urls": [
                {"packagetype": "sdist", "filename": f"{name}-1.0.tar.gz", "url": "https://x/s.tgz"},
                {"packagetype": "bdist_wheel", "filename": fname, "url": f"https://files.example/{fname}",
                 "digests": {"sha256": sha}}]}
            return io.BytesIO(json.dumps(body).encode())
        name = url.rsplit("/", 1)[1].split("-")[0]
        return io.BytesIO(self.blobs[name])


def test_driver_install_checks_the_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    good = FakePypi({"pymssql": wheel({"pymssql_fake_mod.py": "X = 1\n"})})
    res = drivers.install("mssql", opener=good)
    assert res["wheels"][0]["name"] == "pymssql" and (tmp_path / "local" / "P6MCP" / "drivers" / drivers.py_tag()
                                                      / "pymssql_fake_mod.py").is_file()
    assert all(u.startswith("https://") for u in good.urls)
    bad = FakePypi({"pymssql": wheel({"other.py": ""})}, {"pymssql": "0" * 64})
    with pytest.raises(drivers.DriverError, match="checksum"):
        drivers.install("mssql", opener=bad)
    assert (tmp_path / "local" / "P6MCP" / "drivers" / drivers.py_tag() / "pymssql_fake_mod.py").is_file()  # kept
    evil = FakePypi({"pymssql": wheel({"../escape.py": ""})})
    with pytest.raises(drivers.DriverError, match="unsafe"):
        drivers.install("mssql", opener=evil)
    assert drivers._pick([{"packagetype": "bdist_wheel", "filename": "c-44.0-cp39-abi3-win_amd64.whl"}])
    assert drivers._pick([{"packagetype": "bdist_wheel", "filename": "c-44.0-cp39-abi3-manylinux.whl"}]) is None
