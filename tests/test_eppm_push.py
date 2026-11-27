"""P6 EPPM milestone 5: the push of pending changes (dry run, conflict check, ordered calls, stop at the first
failure, journal, resume). The server is a fake with a live state built from the synthetic database: it answers
reads by ObjectId, PUT, POST and DELETE as P6 EPPM 25.12 did on the test server (`true`, `["<new id>"]`)."""

import base64
import json
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from p6mcp.eppm import mapping, store

USER, PW, SPW = "planner", "S3cret-pass", "sql-pass"


class SqliteDriver:
    def __init__(self, path):
        self.path = path

    def connect(self, **kw):
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)


def live_objects(db) -> dict:
    """{entity: {ObjectId: object}} of project 2, as REST text values."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = {}
    for entity, table, spec in (("activity", "TASK", mapping.ACTIVITY), ("relationship", "TASKPRED",
                                                                          mapping.RELATIONSHIP),
                                ("resourceAssignment", "TASKRSRC", mapping.ASSIGNMENT)):
        objs = {}
        for r in con.execute(f"SELECT * FROM {table}"):
            o = {}
            for f, c, k in spec:
                v = r[c] if c in r.keys() else None
                if v is None:
                    continue
                if isinstance(k, dict):
                    v = {"PR_FS": "Finish to Start", "PR_SS": "Start to Start", "PR_FF": "Finish to Finish",
                         "PR_SF": "Start to Finish"}.get(v, v) if c == "pred_type" else v
                elif k == "date":
                    v = str(v).replace(" ", "T")
                o[f] = str(v)
            objs[o["ObjectId"]] = o
        out[entity] = objs
    con.close()
    return out


def conds(filt: str) -> list[tuple[str, str]]:
    return re.findall(r"(\w+) :eq: '?([^' ]+)'?", filt)


def make_handler(db):
    con = sqlite3.connect(db)
    projects = [{"ObjectId": "2", "Id": "DEMO-A", "Name": "Demo", "CurrentBaselineProjectObjectId": "",
                 "LastUpdateDate": "2026-01-06T10:00:00-08:00", "DataDate": "2026-01-05T08:00:00"}]
    con.close()
    state = live_objects(db)
    seq = {"n": 900000}
    clock = {"n": 0}

    def tick() -> str:  # LastUpdateDate the server stamps on what it writes (after the pull stamp, server clock)
        clock["n"] += 1
        return f"2026-01-07T09:00:{clock['n']:02d}-08:00"

    class H(BaseHTTPRequestHandler):
        live = state
        calls: list = []
        fail_on: set = set()  # (method, entity) answered 500
        jobs: list = []
        job_polls = 0
        job_state = "Running"

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

        def _body(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")

        def _auth(self):
            return "JSESSIONID=x1" in (self.headers.get("Cookie") or "")

        def do_POST(self):
            u = urlsplit(self.path)
            if u.path.endswith("/login"):
                ok = self.headers.get("authToken") == base64.b64encode(f"{USER}:{PW}".encode()).decode()
                return self._send(200 if ok else 401, {"message": "ok"})
            if u.path.endswith("/logout"):
                return self._send(200, {"message": "bye"})
            if u.path.endswith("/job/schedule"):
                body = self._body()
                H.jobs.append(body)
                H.job_polls = 0
                return self._send(201, [{"JobId": "1:7", "JobStatus": "Pending"}])
            if u.path.endswith("/job/readJobLog"):
                self._body()  # read the request body first: an unread body can reset the connection on Windows
                return self._send(201, [{"JobLog": "Primavera P6 Scheduling Log\nStatistics\n  Number of Activities: "
                                                   "7\nErrors\n  Activities that have invalid relationship loops\n"
                                                   "    none\nScheduling Results\n  Earliest Data Date: January-05-2026"
                                                   "\n  Latest Early Finish Date: March-02-2026 05:00 PM\nExceptions\n"
                                                   "  Critical Activities: 3\n"}])
            entity = u.path.rsplit("/", 1)[1]
            body = self._body()
            H.calls.append(("POST", entity, body))
            if ("POST", entity) in H.fail_on:
                return self._send(500, {"message": "server error"})
            ids = []
            for o in body:
                seq["n"] += 1
                o = {k: str(v) for k, v in o.items()}
                o["ObjectId"] = str(seq["n"])
                o["LastUpdateDate"] = tick()
                H.live[entity][o["ObjectId"]] = o
                ids.append(o["ObjectId"])
            return self._send(201, ids)

        def do_PUT(self):
            entity = urlsplit(self.path).path.rsplit("/", 1)[1]
            body = self._body()
            H.calls.append(("PUT", entity, body))
            if ("PUT", entity) in H.fail_on:
                return self._send(500, {"message": "server error"})
            for o in body:
                H.live[entity][str(o["ObjectId"])].update({k: str(v) for k, v in o.items()}, LastUpdateDate=tick())
            return self._send(200, True)

        def do_DELETE(self):
            u = urlsplit(self.path)
            entity = u.path.rsplit("/", 1)[1]
            ids = parse_qs(u.query)["ObjectId"][0].split(",")
            H.calls.append(("DELETE", entity, ids))
            for i in ids:
                H.live[entity].pop(i, None)
            return self._send(200, True)

        def do_GET(self):
            u = urlsplit(self.path)
            if not self._auth():
                return self._send(401, {"message": "not signed in"})
            entity = u.path.rsplit("/", 1)[1]
            filt = parse_qs(u.query).get("Filter", [""])[0]
            if entity == "readJobStatus":
                H.job_polls += 1
                return self._send(200, {"JobStatus": "Complete" if H.job_polls > 1 else H.job_state})
            if entity == "fields":
                return self._send(404, {"message": "no"})
            if entity == "project":
                return self._send(200, projects)
            if entity == "scheduleOptions":
                return self._send(200, [])
            if entity in H.live:
                cs = conds(filt)
                rows = list(H.live[entity].values())
                gt = re.search(r"LastUpdateDate:gt:'([^']+)'", filt)
                if gt:  # as P6 EPPM: the stored date has fractions of a second (.4), the filter whole seconds
                    rows = [r for r in rows if str(r.get("LastUpdateDate") or "").replace("T", " ")[:19] + ".4"
                            > gt[1]]
                if " :and: " in filt:
                    rows = [r for r in rows if all(r.get(f) == v for f, v in cs)]
                else:
                    rows = [r for r in rows if any(r.get(f) == v for f, v in cs)]
                return self._send(200, rows)
            return self._send(404, {"message": "no"})
    return H


@pytest.fixture
def push_env(p6, tmp_path, monkeypatch, golden):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    handler = make_handler(golden)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
    prof = {"name": "srv", "base_url": f"http://127.0.0.1:{srv.server_address[1]}", "context": "p6ws",
            "database": "PMDB", "user": USER, "auth": "login", "ca_bundle": None, "timeout_s": 10,
            "sql": {"driver": "mssql", "host": "db", "port": 1433, "service": "PMDB", "user": "r", "schema": "",
                    "table_prefix": ""}, "confirmed": True}
    vault = {"eppm:srv": PW, "eppm-sql:srv": SPW}
    monkeypatch.setattr("p6mcp.credentials.get", lambda k: vault.get(k))
    monkeypatch.setattr("p6mcp.eppm.sql.driver_module", lambda d: SqliteDriver(golden))
    p6.set(data_source="eppm", eppm_profiles=[prof], eppm_active="srv")
    p6.call("p6_eppm_pull", projects=["DEMO-A"])
    yield p6, handler
    srv.shutdown()
    srv.server_close()


def rows(db, q):
    con = sqlite3.connect(db)
    try:
        return con.execute(q).fetchall()
    finally:
        con.close()


def push(p6, **extra):
    """Dry run, then apply with its token and confirmation text (as a client does after the user's yes)."""
    dry = p6.call("p6_eppm_push")
    assert dry.get("confirmation_token"), dry
    return dry, p6.call("p6_eppm_push", dry_run=False, confirmation_token=dry["confirmation_token"],
                        confirmation_text=dry["confirmation_text_required"], **extra)


def test_push_sends_the_changes_in_order_and_records_them(push_env):
    p6, H = push_env
    wc = store.store_path("srv")
    ids = dict(rows(wc, "SELECT task_code, task_id FROM TASK WHERE proj_id = 2"))
    codes = [r[0] for r in rows(wc, "SELECT task_code FROM TASK WHERE proj_id = 2 ORDER BY task_id")]
    a, c = codes[0], codes[-1]
    links = rows(wc, "SELECT task_pred_id, pred_task_id, task_id FROM TASKPRED ORDER BY task_pred_id")
    code_of = {v: k for k, v in ids.items()}
    upd_link, del_link = links[0], links[1]
    p6.apply("p6_update_activity", project="DEMO-A", task_code=a, remaining_duration_days=9)
    p6.apply("p6_add_relationship", project="DEMO-A", predecessor_code=a, successor_code=c, rel_type="SS")
    p6.apply("p6_update_relationship", project="DEMO-A", predecessor_code=code_of[upd_link[1]],
             successor_code=code_of[upd_link[2]], lag_days=2)
    p6.apply("p6_delete_relationship", project="DEMO-A", predecessor_code=code_of[del_link[1]],
             successor_code=code_of[del_link[2]])
    assert p6.call("p6_eppm_status", check_server=False)["pending"]["count"] == 4
    dry, res = push(p6)
    assert dry["calls"] == 4 and not dry.get("would_refuse") and dry["confirmation_text_required"] == "PUSH TO srv"
    assert [x.split(" ")[1] for x in dry["would_do"]] == ["DELETE", "PUT", "POST", "PUT"]  # deletes first
    assert res["applied"] and len(res["sent"]) == 4 and not res.get("failed")
    assert [(m, e) for m, e, _b in H.calls] == [("DELETE", "relationship"), ("PUT", "activity"),
                                                 ("POST", "relationship"), ("PUT", "relationship")]
    act = H.live["activity"][str(ids[a])]
    assert float(act["RemainingDuration"]) == 72.0  # 9 days of 8 h
    assert str(del_link[0]) not in H.live["relationship"]
    new_server_id = int(res["new_ids"][next(iter(res["new_ids"]))])
    new = H.live["relationship"][str(new_server_id)]
    assert (new["PredecessorActivityObjectId"], new["SuccessorActivityObjectId"], new["Type"]) == \
        (str(ids[a]), str(ids[c]), "Start to Start")
    # the working copy now uses the server's id for the new relationship, and nothing is pending
    assert rows(wc, f"SELECT COUNT(*) FROM TASKPRED WHERE task_pred_id = {new_server_id}") == [(1,)]
    assert rows(wc, f"SELECT COUNT(*) FROM TASKPRED WHERE task_pred_id >= {store.LOCAL_ID_BASE}") == [(0,)]
    st = p6.call("p6_eppm_status", check_server=False)
    assert st["pending"]["count"] == 0 and st["last_push"]["sent"] == 4
    lines = [json.loads(x) for x in open(res["journal"], encoding="utf-8")]
    assert [x["ok"] for x in lines] == [True] * 4 and lines[2]["server_id"] == new_server_id
    again = p6.call("p6_eppm_push")
    assert again["would_refuse"] and again["refuse_reasons"] == ["No pending change to push."]
    assert "confirmation_token" not in again


def test_status_counts_own_pushes_apart_from_server_changes(push_env):
    p6, H = push_env
    wc = store.store_path("srv")
    ids = dict(rows(wc, "SELECT task_code, task_id FROM TASK WHERE proj_id = 2"))
    codes = [r[0] for r in rows(wc, "SELECT task_code FROM TASK WHERE proj_id = 2 ORDER BY task_id")]
    a, b, c = codes[0], codes[1], codes[-1]
    st = p6.call("p6_eppm_status")["changed_on_server"]
    assert st["activities"] == {"DEMO-A": 0} and st["pushed_by_you"] == {"DEMO-A": 0}
    assert st["since"] == p6.call("p6_eppm_status", check_server=False)["pulled_at"]  # this PC's clock
    assert st["since_server_clock"] == "2026-01-06 10:00:00"
    p6.apply("p6_update_activity", project="DEMO-A", task_code=a, remaining_duration_days=9)
    p6.apply("p6_add_relationship", project="DEMO-A", predecessor_code=a, successor_code=c, rel_type="SS")
    _dry, res = push(p6)
    assert res["applied"]
    # the push changed activity a on the server: that is the user's own change, not someone else's
    st = p6.call("p6_eppm_status")["changed_on_server"]
    assert st["activities"] == {"DEMO-A": 0} and st["pushed_by_you"] == {"DEMO-A": 1}
    # someone edits b, then a again, in P6 EPPM: both count as server changes
    H.live["activity"][str(ids[b])]["LastUpdateDate"] = "2026-01-07T11:00:00-08:00"
    H.live["activity"][str(ids[a])]["LastUpdateDate"] = "2026-01-07T11:00:01-08:00"
    st = p6.call("p6_eppm_status")["changed_on_server"]
    assert st["activities"] == {"DEMO-A": 2} and st["pushed_by_you"] == {"DEMO-A": 0}


def test_status_starts_from_the_latest_activity_change_of_the_pull(push_env):
    # an activity edit leaves the project's LastUpdateDate as it was (P6 EPPM 25.12): an activity changed before
    # the pull, so already in the copy, must not count as changed since the pull
    p6, H = push_env
    first = next(iter(H.live["activity"].values()))
    first["LastUpdateDate"] = "2026-01-06T12:00:00-08:00"  # after the project's 10:00
    assert p6.call("p6_eppm_status")["changed_on_server"]["activities"] == {"DEMO-A": 1}  # after the old pull
    p6.call("p6_eppm_pull", projects=["DEMO-A"])
    st = p6.call("p6_eppm_status")["changed_on_server"]
    assert st["activities"] == {"DEMO-A": 0} and st["since_server_clock"] == "2026-01-06 12:00:00"
    assert "_stamp" not in p6.call("p6_eppm_status", check_server=False)["projects"][0]


def test_conflicts_refuse_the_push(push_env):
    p6, H = push_env
    wc = store.store_path("srv")
    code, tid = rows(wc, "SELECT task_code, task_id FROM TASK WHERE proj_id = 2 ORDER BY task_id")[1]
    p6.apply("p6_update_activity", project="DEMO-A", task_code=code, task_name="Local name")
    H.live["activity"][str(tid)]["Name"] = "Changed by someone else"  # a change on the server since the pull
    dry = p6.call("p6_eppm_push")
    assert dry["would_refuse"] and "conflict" in dry["refuse_reasons"][0] and "confirmation_token" not in dry
    H.live["activity"][str(tid)]["Name"] = "Local name"  # the server already holds the local value
    dry, res = push(p6)
    assert dry["calls"] == 0 and dry["already_done"] and res["applied"] and not [c for c in H.calls if c[0] == "PUT"]
    assert p6.call("p6_eppm_status", check_server=False)["pending"]["count"] == 0


def test_first_failure_stops_and_a_new_push_resumes(push_env):
    p6, H = push_env
    wc = store.store_path("srv")
    (a, ta), (b, tb) = rows(wc, "SELECT task_code, task_id FROM TASK WHERE proj_id = 2 ORDER BY task_id")[:2]
    links = rows(wc, "SELECT pred_task_id, task_id FROM TASKPRED ORDER BY task_pred_id")
    code_of = dict(rows(wc, "SELECT task_id, task_code FROM TASK"))
    p6.apply("p6_delete_relationship", project="DEMO-A", predecessor_code=code_of[links[0][0]],
             successor_code=code_of[links[0][1]])
    p6.apply("p6_update_activity", project="DEMO-A", task_code=a, task_name="First")
    p6.apply("p6_update_activity", project="DEMO-A", task_code=b, task_name="Second")
    H.fail_on.add(("PUT", "activity"))
    _dry, res = push(p6)
    assert res["applied"] is False and res["sent"] == ["relationship " + code_of[links[0][0]] + " -> "
                                                       + code_of[links[0][1]]]
    assert "500" in res["failed"]["error"]
    assert p6.call("p6_eppm_status", check_server=False)["pending"]["count"] == 2  # the delete is done
    H.fail_on.clear()
    H.calls.clear()
    _dry, res = push(p6)
    assert res["applied"] and len(res["sent"]) == 2 and [m for m, _e, _b in H.calls] == ["PUT", "PUT"]
    assert H.live["activity"][str(ta)]["Name"] == "First" and H.live["activity"][str(tb)]["Name"] == "Second"


def test_schedule_on_the_server_then_pull(push_env, monkeypatch):
    from p6mcp.eppm import jobs
    monkeypatch.setattr(jobs, "POLL_S", 0.01)
    p6, H = push_env
    dry = p6.call("p6_eppm_schedule")
    assert dry["would_do"][0].startswith("Schedule DEMO-A on srv (data date 2026-01-05 08:00, unchanged)")
    assert "confirmation_text_required" not in dry and dry.get("confirmation_token")
    res = p6.call("p6_eppm_schedule", dry_run=False, confirmation_token=dry["confirmation_token"])
    assert res["applied"] and H.jobs == [{"ProjectObjectId": [2]}] and res["job"]["status"] == "Complete"
    assert res["log_facts"] == {"activities": 7, "critical": 3, "data_date": "January-05-2026",
                                "latest_early_finish": "March-02-2026 05:00 PM", "loops": False}
    assert res["pull"]["rows"]["TASK"] == 7
    # a new data date is sent as such
    dry = p6.call("p6_eppm_schedule", new_data_date="2026-02-02 08:00", pull_after=False)
    assert "-> 2026-02-02 08:00" in dry["would_do"][0]
    p6.call("p6_eppm_schedule", new_data_date="2026-02-02 08:00", pull_after=False, dry_run=False,
            confirmation_token=dry["confirmation_token"])
    assert H.jobs[-1] == {"ProjectObjectId": [2], "NewDataDate": "2026-02-02T08:00:00"}
    # local changes pending: scheduling then pulling would drop them, so it is refused
    code = rows(store.store_path("srv"), "SELECT task_code FROM TASK WHERE proj_id = 2 ORDER BY task_id")[0][0]
    p6.apply("p6_update_activity", project="DEMO-A", task_code=code, task_name="Not pushed")
    dry = p6.call("p6_eppm_schedule")
    assert dry["would_refuse"] and "pending" in dry["refuse_reasons"][0] and "confirmation_token" not in dry
    # a job that fails is reported with its log, and nothing is pulled
    H.job_state = "Failed"  # the first status read answers it
    dry = p6.call("p6_eppm_schedule", pull_after=False)
    res = p6.call("p6_eppm_schedule", pull_after=False, dry_run=False, confirmation_token=dry["confirmation_token"])
    assert res["applied"] is False and res["job"]["status"] == "Failed" and "pull" not in res


def test_push_needs_the_confirmation_text_and_writes_allowed(push_env):
    p6, _H = push_env
    code = rows(store.store_path("srv"), "SELECT task_code FROM TASK WHERE proj_id = 2 ORDER BY task_id")[0][0]
    p6.apply("p6_update_activity", project="DEMO-A", task_code=code, task_name="X")
    dry = p6.call("p6_eppm_push")
    with pytest.raises(ValueError, match="PUSH TO srv"):
        p6.call("p6_eppm_push", dry_run=False, confirmation_token=dry["confirmation_token"], confirmation_text="yes")
    p6.set(allow_writes=False)
    with pytest.raises(ValueError):
        p6.call("p6_eppm_push")


def test_discard_drops_chosen_changes_and_keeps_the_others(push_env):
    p6, H = push_env
    wc = store.store_path("srv")
    ids = dict(rows(wc, "SELECT task_code, task_id FROM TASK WHERE proj_id = 2"))
    codes = [r[0] for r in rows(wc, "SELECT task_code FROM TASK WHERE proj_id = 2 ORDER BY task_id")]
    a, b, c = codes[0], codes[1], codes[-1]
    links = rows(wc, "SELECT task_pred_id, pred_task_id, task_id, pred_type, lag_hr_cnt FROM TASKPRED ORDER BY 1")
    code_of = {v: k for k, v in ids.items()}
    upd_link, del_link = links[0], links[1]
    rem = dict(rows(wc, "SELECT task_code, remain_drtn_hr_cnt FROM TASK WHERE proj_id = 2"))
    # the batch tool's edits are pending changes like the single tool's
    p6.apply("p6_update_activities", project="DEMO-A", items=[{"task_code": a, "remaining_duration_days": 9},
                                                              {"task_code": b, "task_name": "Renamed"}])
    p6.apply("p6_add_relationship", project="DEMO-A", predecessor_code=a, successor_code=c, rel_type="SS")
    p6.apply("p6_update_relationship", project="DEMO-A", predecessor_code=code_of[upd_link[1]],
             successor_code=code_of[upd_link[2]], lag_days=2)
    p6.apply("p6_delete_relationship", project="DEMO-A", predecessor_code=code_of[del_link[1]],
             successor_code=code_of[del_link[2]])
    pend = p6.call("p6_eppm_status", check_server=False)["pending"]
    by = {(x["table"], x["action"], x.get("labels", {}).get("task_code")): x["id"] for x in pend["changes"]}
    assert pend["count"] == 5 and all("rollups" not in x for x in pend["changes"])
    new_id = by[("TASKPRED", "create", None)]
    assert by[("TASK", "update", a)] == f"TASK:{ids[a]}"
    assert by[("TASKPRED", "delete", None)] == f"TASKPRED:{del_link[0]}"
    with pytest.raises(ValueError, match="Not a pending change: TASK:1. Pending: TASK:"):
        p6.call("p6_eppm_discard", changes=["TASK:1"])
    # drop the new link and the deletion: the copy has the pulled links again, the other three stay pending
    dry = p6.call("p6_eppm_discard", changes=[new_id, f"TASKPRED:{del_link[0]}"])
    assert dry["would_do"][0].startswith("Drop 2 pending change(s)") and "3 stay pending" in dry["would_do"][0]
    assert "added locally, removed" in dry["would_do"][1] and "deleted locally, put back (" in dry["would_do"][2]
    res = p6.apply("p6_eppm_discard", changes=[new_id, f"TASKPRED:{del_link[0]}"])
    assert res["applied"] and "pulled values" in res["next"]
    now = rows(wc, "SELECT task_pred_id, pred_task_id, task_id, pred_type, lag_hr_cnt FROM TASKPRED ORDER BY 1")
    assert del_link in now and len(now) == len(links)  # put back with its server id, type and lag
    assert rows(wc, f"SELECT COUNT(*) FROM TASKPRED WHERE task_pred_id >= {store.LOCAL_ID_BASE}") == [(0,)]
    left = p6.call("p6_eppm_status", check_server=False)["pending"]
    assert left["count"] == 3 and {x["id"] for x in left["changes"]} == {
        f"TASK:{ids[a]}", f"TASK:{ids[b]}", f"TASKPRED:{upd_link[0]}"}
    # the push sends only what is left
    dry = p6.call("p6_eppm_push")
    assert dry["calls"] == 3 and [x.split(" ")[1] for x in dry["would_do"]] == ["PUT", "PUT", "PUT"]
    # all: the copy is as pulled, nothing to push
    p6.apply("p6_eppm_discard", changes=["all"])
    assert p6.call("p6_eppm_status", check_server=False)["pending"]["count"] == 0
    assert dict(rows(wc, "SELECT task_code, remain_drtn_hr_cnt FROM TASK WHERE proj_id = 2")) == rem
    assert rows(wc, "SELECT task_pred_id, pred_task_id, task_id, pred_type, lag_hr_cnt FROM TASKPRED ORDER BY 1") \
        == links
    assert p6.call("p6_eppm_push")["refuse_reasons"] == ["No pending change to push."]
    assert p6.call("p6_eppm_discard", changes=["all"]) == {"applied": False, "error": "No pending change to drop."}
    assert H.calls == []  # the server was never written


def test_a_conflict_is_cleared_by_discarding_that_change(push_env):
    p6, H = push_env
    wc = store.store_path("srv")
    ids = dict(rows(wc, "SELECT task_code, task_id FROM TASK WHERE proj_id = 2"))
    codes = [r[0] for r in rows(wc, "SELECT task_code FROM TASK WHERE proj_id = 2 ORDER BY task_id")]
    a, b = codes[0], codes[1]
    p6.apply("p6_update_activity", project="DEMO-A", task_code=a, remaining_duration_days=9)
    p6.apply("p6_update_activity", project="DEMO-A", task_code=b, task_name="Mine")
    H.live["activity"][str(ids[a])]["RemainingDuration"] = "99.0"  # someone else, in P6 EPPM
    dry = p6.call("p6_eppm_push")
    assert dry["would_refuse"] and dry["conflicts"] == [
        {"what": f"activity {a}", "why": dry["conflicts"][0]["why"], "id": f"TASK:{ids[a]}"}]
    p6.apply("p6_eppm_discard", changes=[dry["conflicts"][0]["id"]])
    dry = p6.call("p6_eppm_push")
    assert not dry.get("would_refuse") and dry["calls"] == 1 and dry["would_do"][0].startswith(f"1. PUT activity {b}")
