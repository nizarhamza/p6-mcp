"""P6 EPPM milestone 1: server profiles in config.json, the REST session and the SQL test (docs/EPPM_PLAN.md).

The REST server is a local fake (http.server on 127.0.0.1) that answers like P6 EPPM 25.12 did on the spike's test
server: /login with the authToken header and DatabaseName, a JSESSIONID cookie, numbers as strings, /logout.
"""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from p6mcp import config, eppm, settings_logic
from p6mcp.eppm import rest, sql

USER, PW, DB = "planner", "S3cret-pass", "PMDB"


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for k in ("P6_DB_PATH", "P6MCP_TOOL_PROFILE", "P6MCP_ALLOW_WRITES"):
        monkeypatch.delenv(k, raising=False)
    path = tmp_path / "config.json"
    monkeypatch.setenv("P6MCP_CONFIG", str(path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    return path


def profile(**kw):
    p = {"name": "test", "base_url": "http://127.0.0.1:1", "context": "p6ws", "database": DB, "user": USER,
         "auth": "auto", "ca_bundle": None, "timeout_s": 10, "sql": None, "confirmed": False}
    p.update(kw)
    return p


# ----------------------------------------------------------------- fake server

class Fake(BaseHTTPRequestHandler):
    sessions: set = set()
    seen: list = []

    def log_message(self, *a):  # quiet
        pass

    def _send(self, status, body=None, headers=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _cookie_ok(self):
        return any(f"JSESSIONID={s}" in (self.headers.get("Cookie") or "") for s in self.sessions)

    def do_POST(self):
        u = urlsplit(self.path)
        Fake.seen.append(("POST", u.path, dict(self.headers)))
        if u.path == "/p6ws/restapi/login":
            want = base64.b64encode(f"{USER}:{PW}".encode()).decode()
            if self.headers.get("authToken") != want or parse_qs(u.query).get("DatabaseName") != [DB]:
                return self._send(401, {"message": "Login failed"})
            sid = f"s{len(self.sessions) + 1}"
            self.sessions.add(sid)
            return self._send(200, {"message": "Login successful."}, {"Set-Cookie": f"JSESSIONID={sid}; Path=/p6ws"})
        if u.path == "/p6ws/oauth/token":
            want = base64.b64encode(f"{USER}:{PW}".encode()).decode()
            if self.headers.get("authToken") != want:
                return self._send(401, {"message": "bad credentials"})
            return self._send(200, {"accessToken": "tok-123", "expiresIn": 3600})
        if u.path == "/p6ws/restapi/logout":
            return self._send(200, {"message": "Logout successful."})
        return self._send(404, {"message": "no such path"})

    def do_GET(self):
        u = urlsplit(self.path)
        Fake.seen.append(("GET", u.path, dict(self.headers)))
        bearer = self.headers.get("Authorization") == "Bearer tok-123"
        if u.path == "/p6ws/restapi/project":
            if not (self._cookie_ok() or bearer):
                return self._send(401, {"message": "not signed in"})
            return self._send(200, [{"ObjectId": "370", "Id": "DEMO-SS225"}, {"ObjectId": "371", "Id": "B"}])
        return self._send(404, {"message": "no such path"})


@pytest.fixture
def server():
    Fake.sessions, Fake.seen = set(), []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


# ----------------------------------------------------------------- config

def test_profiles_validate_and_secrets_are_refused(clean_env):
    good = {"schema_version": 4, "data_source": "eppm", "eppm_profiles": [profile()], "eppm_active": "test"}
    assert config.validate(good)["eppm_profiles"][0]["name"] == "test"
    bad = [
        {"eppm_profiles": [profile(base_url="ftp://x")]},
        {"eppm_profiles": [profile(sql={"driver": "db2", "host": "h", "port": 1, "service": "s", "user": "u"})]},
        {"eppm_profiles": [profile(sql={"driver": "mssql", "host": "h", "port": 0, "service": "s", "user": "u"})]},
        {"eppm_profiles": [profile(), profile()]},
        {"eppm_profiles": [profile(timeout_s=1)]},
        {"eppm_profiles": [profile(auth="kerberos")]},
        {"eppm_profiles": [profile()], "eppm_active": "other"},
        {"eppm_profiles": [profile(password="x")]},
    ]
    for d in bad:
        with pytest.raises(config.ConfigError):
            config.validate(d)


def test_load_in_eppm_mode_reports_what_is_missing(clean_env):
    clean_env.write_text(json.dumps({"schema_version": 4, "data_source": "eppm"}), encoding="utf-8")
    assert "no server selected" in config.load().db_problem
    clean_env.write_text(json.dumps({"schema_version": 4, "data_source": "eppm", "eppm_profiles": [profile()],
                                     "eppm_active": "test", "allow_writes": True, "tool_profile": "full"}),
                         encoding="utf-8")
    st = config.load()
    assert st.db_path == "" and st.eppm is None and "not tested and confirmed" in st.db_problem
    clean_env.write_text(json.dumps({"schema_version": 4, "data_source": "eppm", "eppm_active": "test",
                                     "eppm_profiles": [profile(confirmed=True)], "allow_writes": True}),
                         encoding="utf-8")
    st = config.load()
    assert st.eppm == "test" and st.db_path == "" and "no project pulled" in st.db_problem
    assert st.allow_writes is False  # writes come with the working copy and the push


def test_migration_from_3_adds_the_eppm_keys(clean_env):
    clean_env.write_text(json.dumps({"schema_version": 3}), encoding="utf-8")
    data, steps = config.read_file()
    assert steps == ["3->4"] and data["eppm_profiles"] == [] and data["eppm_active"] is None


# ----------------------------------------------------------------- settings logic

def test_profile_edits_confirmation_and_digest():
    data, name = settings_logic.add_eppm_profile({}, "https://p6.example.com/", "planner", "PMDB")
    assert name == "p6" and settings_logic.find_eppm(data, name)["confirmed"] is False
    data2, name2 = settings_logic.add_eppm_profile(data, "https://p6.example.com", "other")
    assert name2 == "p6 2"
    p = settings_logic.find_eppm(data, name)
    ok = {"ok": True, "digest": eppm.digest(p), "tested_at": "2026-11-24T10:00:00",
          "rest": {"ok": True, "projects": 2}, "sql": None}
    with pytest.raises(config.ConfigError):
        settings_logic.set_eppm_active(data, name)  # not confirmed yet
    data = settings_logic.confirm_eppm(data, name, ok)
    p = settings_logic.find_eppm(data, name)
    assert p["confirmed"] and data["eppm_active"] == name and p["projects"] == 2 and p["sql_ok"] is None
    # a timeout change keeps the confirmation, a connection change clears it and the active choice
    assert settings_logic.find_eppm(settings_logic.update_eppm_profile(data, name, timeout_s=60), name)["confirmed"]
    changed = settings_logic.update_eppm_profile(data, name, database="OTHER")
    assert not settings_logic.find_eppm(changed, name)["confirmed"] and changed["eppm_active"] is None
    with pytest.raises(config.ConfigError):
        settings_logic.confirm_eppm(changed, name, ok)  # the test was run with the old settings
    renamed = settings_logic.update_eppm_profile(data, name, new_name="Main")
    assert renamed["eppm_active"] == "Main"
    with pytest.raises(config.ConfigError):
        settings_logic.update_eppm_profile(data, name, password="x")
    assert settings_logic.remove_eppm_profile(data, name)["eppm_active"] is None
    config.validate(data)


# ----------------------------------------------------------------- REST

def test_login_session_reads_projects_and_signs_out(server):
    res = rest.test_connection(profile(base_url=server), PW)
    assert res["ok"] and res["auth"] == "login" and res["projects"] == 2
    paths = [(m, p) for m, p, _ in Fake.seen]
    assert paths == [("POST", "/p6ws/restapi/login"), ("GET", "/p6ws/restapi/project"),
                     ("POST", "/p6ws/restapi/logout")]
    assert "Authorization" not in Fake.seen[1][2]  # the cookie carries the session


def test_oauth_bearer(server):
    res = rest.test_connection(profile(base_url=server, auth="oauth"), PW)
    assert res["ok"] and res["auth"] == "oauth" and res["projects"] == 2
    assert Fake.seen[1][2].get("Authorization") == "Bearer tok-123"
    assert rest.auth_mode("https://abc.p6.oraclecloud.com", "auto") == "oauth"
    assert rest.auth_mode("https://p6.example.com", "auto") == "login"


def test_refusals_never_show_the_password(server):
    res = rest.test_connection(profile(base_url=server), "wrong-pass")
    assert not res["ok"] and "Sign-in refused" in res["error"] and "wrong-pass" not in res["error"]
    res = rest.test_connection(profile(base_url=server, database="NOPE"), PW)
    assert not res["ok"] and "database name" in res["error"]
    res = rest.test_connection(profile(base_url=server, context="other"), PW)
    assert not res["ok"] and "404" in res["error"]
    res = rest.test_connection(profile(base_url="http://127.0.0.1:9"), PW)  # nothing listens on port 9
    assert not res["ok"] and "cannot be reached" in res["error"]
    assert not rest.test_connection(profile(base_url=server), "")["ok"]


# ----------------------------------------------------------------- SQL

class FakeCursor:
    def __init__(self, missing=()):
        self.missing, self.description, self._rows = missing, None, []

    def execute(self, q):
        if any(f" {t} " in q + " " or q.endswith(t) for t in self.missing):
            raise RuntimeError("invalid object name")
        if q.startswith("SELECT COUNT(*) FROM dbo.PROJECT") and "1 = 0" not in q:
            self._rows = [(7,)]
        elif "TOP 1 *" in q:
            self.description, self._rows = [("task_id",), ("task_code",), ("proj_id",)], [(1, "A", 7)]
        else:
            self._rows = [(0,)]

    def fetchall(self):
        return self._rows


class FakeDriver:
    def __init__(self, missing=(), fail=None):
        self.missing, self.fail, self.closed = missing, fail, False

    def connect(self, **kw):
        if self.fail:
            raise RuntimeError(f"Login failed for user {kw['user']} with {kw['password']}")
        drv = self

        class Con:
            def cursor(self):
                return FakeCursor(drv.missing)

            def close(self):
                drv.closed = True
        return Con()


SQL = {"driver": "mssql", "host": "db", "port": 1433, "service": "PMDB", "user": "mcp_read", "schema": "dbo",
       "table_prefix": ""}


def test_sql_test_connection():
    drv = FakeDriver()
    res = sql.test_connection(SQL, "pw", lambda d: drv)
    assert res["ok"] and res["project_rows"] == 7 and res["task_columns"] == 3 and drv.closed
    res = sql.test_connection(SQL, "pw", lambda d: None)
    assert not res["ok"] and res["driver_missing"] == "pymssql"
    res = sql.test_connection(SQL, "pw-123", lambda d: FakeDriver(fail=True))
    assert not res["ok"] and "pw-123" not in res["error"] and "connection failed" in res["error"]
    res = sql.test_connection(SQL, "pw", lambda d: FakeDriver(missing=("dbo.TASKPRED",)))
    assert not res["ok"] and "TASKPRED" in res["error"]
    assert sql.table(dict(SQL, schema="", table_prefix="T6_"), "TASK") == "T6_TASK"


def test_profile_test_combines_rest_and_sql(server):
    p = profile(base_url=server, sql=SQL)
    res = eppm.test_profile(p, PW, "pw", sql_module=lambda d: None)
    assert res["ok"] and res["rest"]["projects"] == 2 and res["sql"]["driver_missing"] == "pymssql"
    assert res["digest"] == eppm.digest(p)
    res = eppm.test_profile(profile(base_url=server), None)
    assert not res["ok"] and res["sql"] is None
