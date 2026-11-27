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
    oauth_broken = False
    locked = False

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
            if Fake.locked:  # what P6 EPPM 25.12 answered for a locked account, even with the right password
                return self._send(500, {"message": "Request failed"})
            want = base64.b64encode(f"{USER}:{PW}".encode()).decode()
            if self.headers.get("authToken") != want or parse_qs(u.query).get("DatabaseName") != [DB]:
                return self._send(401, {"message": "Login failed"})
            sid = f"s{len(self.sessions) + 1}"
            self.sessions.add(sid)
            return self._send(200, {"message": "Login successful."}, {"Set-Cookie": f"JSESSIONID={sid}; Path=/p6ws"})
        if u.path == "/p6ws/oauth/token":
            if Fake.oauth_broken:  # what an on-premises P6 EPPM 25.12 without IDCS answered
                page = (b"<h2>Token Generation Failed</h2> <h3>Reason:Cannot invoke &quot;String.toCharArray()&quot; "
                        b"because &quot;this.m_secret&quot; is null</h3> <h6>Stacktrace:</h6> <p>java.lang."
                        b"NullPointerException at com.primavera.infr.idcs.IDCSServiceImpl.requestOAuthTo</p>")
                self.send_response(500)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
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
    Fake.sessions, Fake.seen, Fake.oauth_broken, Fake.locked = set(), [], False, False
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    t = threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True)
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
    assert "later version" not in st.db_problem and "p6_eppm_pull" in st.db_problem
    assert st.allow_writes is True  # the setting holds: EPPM writes change the local working copy only


def test_instructions_name_the_eppm_tools_in_eppm_mode_only():
    from p6mcp import instructions
    text = instructions.build({"data_source": "eppm"})
    assert "p6_eppm_push" in text and "p6_eppm_schedule" in text and text.isascii() and "--" not in text
    # nothing of P6 Professional: no SQLite header, no "P6 must be closed", no XER import of new activities
    assert text.startswith(instructions.EPPM_HEADER) and "standalone SQLite" not in text
    assert "P6 must be closed" not in text and "p6_import_xer" not in text and "dry_run=true" in text
    assert "P6 must be closed" in instructions.build(None)
    assert "p6_eppm" not in instructions.build(None) and "p6_eppm" not in instructions.build({"data_source": "xer"})
    assert "data_source" in settings_logic.RESTART_KEYS  # the server reads its instructions at start


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


def test_oauth_on_a_server_without_it_says_what_to_do(server):
    Fake.oauth_broken = True
    err = rest.test_connection(profile(base_url=server, auth="oauth"), PW)["error"]
    assert "OAuth sign-in is not set up on this server" in err and "Session sign-in" in err
    assert "<" not in err and "java.lang" not in err and "Token Generation Failed" in err  # plain text, no stack


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


class UsersDriver:
    """A SQL driver whose USERS table holds one P6 user with this many failed sign-ins (None: no such row)."""

    def __init__(self, failed=None, fail=False):
        self.failed, self.fail, self.queries = failed, fail, []

    def connect(self, **kw):
        if self.fail:
            raise RuntimeError("Login failed")
        drv = self

        class Cur:
            description = [("task_id",)]

            def execute(self, q, params=()):
                drv.queries.append((q, params))
                if "USERS" not in q:  # the SQL test of test_profile
                    self._rows = [(0,)]
                else:
                    self._rows = [] if drv.failed is None else [(drv.failed,)]

            def fetchall(self):
                return self._rows

        class Con:
            def cursor(self):
                return Cur()

            def close(self):
                pass
        return Con()


def test_locked_account_sign_in_says_so(server):
    Fake.locked = True
    err = rest.test_connection(profile(base_url=server), PW)["error"]
    assert "HTTP 500" in err and "Request failed" in err and "locked" in err and "5 failed sign-ins" in err
    assert "administrator" in err and PW not in err
    # with the read-only SQL account, the count of failed sign-ins confirms it or rules it out
    drv = UsersDriver(failed=6)
    err = eppm.test_profile(profile(base_url=server, sql=SQL), PW, "pw", sql_module=lambda d: drv)["rest"]["error"]
    assert "confirms it" in err and "6 failed sign-ins" in err and USER in err
    q, params = next(x for x in drv.queries if "USERS" in x[0])
    assert "dbo.USERS" in q and "failed_login_attempts_cnt" in q and "%s" in q and params == (USER,)
    err = eppm.test_profile(profile(base_url=server, sql=SQL), PW, "pw",
                            sql_module=lambda d: UsersDriver(failed=2))["rest"]["error"]
    assert "probably not locked" in err and "2 failed" in err
    for d in (UsersDriver(failed=None), UsersDriver(fail=True)):  # nothing readable: only the hint
        res = eppm.test_profile(profile(base_url=server, sql=SQL), PW, "pw", sql_module=lambda _d, d=d: d)
        err = res["rest"]["error"]
        assert "locked" in err and "SQL account" not in err
    # a plain refusal (401) is not a lockout
    Fake.locked = False
    err = rest.test_connection(profile(base_url=server), "wrong")["error"]
    assert "Sign-in refused" in err and "locked" not in err


def test_failed_sign_ins_query_and_finding():
    drv = UsersDriver(failed=3)
    assert sql.failed_sign_ins(dict(SQL, driver="oracle", schema="ADMUSER"), "pw", "Admin", drv) == 3
    q, params = drv.queries[0]
    assert "ADMUSER.USERS" in q and ":1" in q and "UPPER(user_name)" in q and params == ("Admin",)
    assert sql.failed_sign_ins(SQL, "pw", "x", UsersDriver(fail=True)) is None
    assert rest.lock_finding(None, "x") == ""
    assert "is locked" in rest.lock_finding(rest.LOCK_AFTER, "x")
    assert eppm.lock_check(profile()) is None  # no SQL account: no check


# ----------------------------------------------------------------- proxy (3.4.0)

class Proxy:
    """A tiny forward proxy on 127.0.0.1: absolute-URI requests (plain http) and CONNECT tunnels. It resolves the host
    names of `hosts` itself (name -> port on 127.0.0.1), so the client never looks them up, and records each request
    (method, target, Proxy-Authorization). need_auth: the Proxy-Authorization value it requires (407 otherwise)."""

    def __init__(self, hosts: dict, need_auth=None):
        import socket
        self.hosts, self.need, self.seen = hosts, need_auth, []
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.sock.close()

    def _accept(self):
        while True:
            try:
                c, _a = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    @staticmethod
    def _pipe(a, b):
        import socket

        def copy(src, dst):
            try:
                while True:
                    d = src.recv(65536)
                    if not d:
                        break
                    dst.sendall(d)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
        t = threading.Thread(target=copy, args=(b, a), daemon=True)
        t.start()
        copy(a, b)
        t.join(5)
        a.close()
        b.close()

    def _serve(self, c):
        import socket
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = c.recv(65536)
            if not d:
                c.close()
                return
            buf += d
        head, rest = buf.split(b"\r\n\r\n", 1)
        line, *hdrs = head.decode("latin-1").split("\r\n")
        method, target, ver = line.split(" ")
        headers = {k.strip().lower(): v.strip() for k, v in (h.split(":", 1) for h in hdrs if ":" in h)}
        self.seen.append((method, target, headers.get("proxy-authorization")))
        if self.need and headers.get("proxy-authorization") != self.need:
            c.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"t\"\r\n"
                      b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            c.close()
            return
        host = target.rsplit(":", 1)[0] if method == "CONNECT" else urlsplit(target).hostname
        if host not in self.hosts:
            c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            c.close()
            return
        up = socket.create_connection(("127.0.0.1", self.hosts[host]))
        if method == "CONNECT":
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:
            u = urlsplit(target)
            keep = [h for h in hdrs if not h.lower().startswith("proxy-")]
            up.sendall("\r\n".join([f"{method} {u.path}{'?' + u.query if u.query else ''} {ver}"] + keep)
                       .encode("latin-1") + b"\r\n\r\n")
        if rest:
            up.sendall(rest)
        self._pipe(c, up)


@pytest.fixture
def no_proxy_env(monkeypatch):
    """No proxy from the environment or from Windows unless a test sets one."""
    from p6mcp.eppm import proxy
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "REQUEST_METHOD"):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(k.lower(), raising=False)
    monkeypatch.setattr(proxy, "_registry", lambda: ({}, lambda _h: False))
    monkeypatch.setattr(proxy, "_pac", lambda: None)
    return proxy


def test_proxy_resolution_follows_windows_the_environment_and_the_profile(no_proxy_env, monkeypatch):
    proxy = no_proxy_env
    r = proxy.resolve("https://p6.example.com")
    assert r["url"] is None and r["mode"] == "system" and "no proxy" in r["note"]
    monkeypatch.setattr(proxy, "_pac", lambda: "http://wpad.example.com/proxy.pac")
    r = proxy.resolve("https://p6.example.com")
    assert r["url"] is None and r["pac"] == "http://wpad.example.com/proxy.pac" and "setup script" in r["note"]
    # Windows Internet Settings, with its bypass list (ProxyOverride)
    monkeypatch.setattr(proxy, "_registry", lambda: ({"https": "corp:8080", "http": "http://corp:8081"},
                                                     lambda h: h == "eppm.local"))
    r = proxy.resolve("https://p6.example.com")
    assert (r["url"], r["source"], r["bypassed"]) == ("http://corp:8080", "windows", False)
    assert proxy.resolve("http://p6.example.com")["url"] == "http://corp:8081"
    r = proxy.resolve("https://eppm.local")
    assert r["url"] is None and r["bypassed"] and "bypass list" in r["note"]
    for local in ("http://127.0.0.1:9", "http://localhost:9", "http://[::1]:9", "http://p6.localhost"):
        r = proxy.resolve(local)
        assert r["url"] is None and r["bypassed"], local
    # HTTP(S)_PROXY and NO_PROXY win over Windows when set
    monkeypatch.setenv("HTTPS_PROXY", "http://envproxy:3128")
    monkeypatch.setenv("NO_PROXY", ".corp.example")
    r = proxy.resolve("https://p6.example.com")
    assert (r["url"], r["source"]) == ("http://envproxy:3128", "environment")
    assert proxy.resolve("https://p6.corp.example")["bypassed"]
    # the profile: no proxy at all, or its own proxy whatever the bypass lists say (loopback stays direct)
    assert proxy.resolve("https://p6.example.com", {"proxy": "none"})["url"] is None
    mine = {"proxy": "custom", "proxy_url": "http://mine:8080", "proxy_user": "me"}
    r = proxy.resolve("https://p6.corp.example", mine)
    assert (r["url"], r["source"], r["user"]) == ("http://mine:8080", "profile", "me")
    assert "password" not in json.dumps(r)
    assert proxy.resolve("http://127.0.0.1:9", mine)["url"] is None
    # urllib's own system proxy handler is never added: the session's decision is the only one
    import urllib.request
    s = rest.Session("http://127.0.0.1:9", USER, PW, proxy={"proxy": "custom", "proxy_url": "http://mine:8080"})
    assert not [h for h in s.opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    s = rest.Session("http://p6.example.com", USER, PW, proxy={"proxy": "custom", "proxy_url": "http://mine:8080"})
    assert [type(h) for h in s.opener.handlers if isinstance(h, urllib.request.ProxyHandler)] == [proxy.Via]


def test_proxy_settings_in_config_and_profile_edits():
    from p6mcp.eppm import proxy
    assert proxy.check_url("http://proxy.example.com:8080") is None
    assert "Credential Manager" in proxy.check_url("http://me:secret@proxy.example.com:8080")
    for bad in ("https://proxy:8080", "http://proxy", "proxy:8080", "http://proxy:8080/x", "", None):
        assert proxy.check_url(bad), bad
    ok = profile(proxy="custom", proxy_url="http://proxy.example.com:8080", proxy_user="me")
    assert config.validate({"eppm_profiles": [ok]})["eppm_profiles"][0]["proxy_url"].endswith(":8080")
    config.validate({"eppm_profiles": [profile(proxy="none", proxy_url=None)]})
    for bad in (profile(proxy="pac"), profile(proxy="custom"), profile(proxy="custom", proxy_url="http://u:p@x:1"),
                profile(proxy_url="ftp://x:1"), profile(proxy_user=5)):
        with pytest.raises(config.ConfigError):
            config.validate({"eppm_profiles": [bad]})
    # the digest: the default proxy setting keeps a profile's old fingerprint, a proxy change is a new one
    old = profile()
    data, name = settings_logic.add_eppm_profile({}, "https://p6.example.com", "planner", "PMDB")
    new = settings_logic.find_eppm(data, name)
    assert new["proxy"] == "system" and new["proxy_url"] is None and new["proxy_user"] is None
    assert eppm.digest(old) == eppm.digest(dict(old, proxy="system", proxy_url=None, proxy_user=None))
    assert eppm.digest(old) != eppm.digest(dict(old, proxy="none"))
    assert eppm.digest(ok) != eppm.digest(dict(ok, proxy_user="other"))
    res = {"ok": True, "digest": eppm.digest(new), "tested_at": "t", "rest": {"ok": True, "projects": 1}, "sql": None}
    data = settings_logic.confirm_eppm(data, name, res)
    changed = settings_logic.update_eppm_profile(data, name, proxy="custom", proxy_url="http://proxy:3128")
    assert not settings_logic.find_eppm(changed, name)["confirmed"] and changed["eppm_active"] is None
    config.validate(changed)


def test_forgetting_a_server_deletes_its_proxy_password(monkeypatch):
    gone = []
    monkeypatch.setattr("p6mcp.credentials.delete", lambda k: gone.append(k) or True)
    settings_logic.forget_eppm_passwords("srv")
    assert gone == ["eppm:srv", "eppm-sql:srv", "eppm-proxy:srv"]


def test_rest_calls_go_through_the_proxy_when_set_and_not_when_bypassed(server, no_proxy_env, monkeypatch):
    port = int(server.rsplit(":", 1)[1])
    px = Proxy({"eppm.test": port})
    try:
        far = f"http://eppm.test:{port}"  # a name only the proxy resolves: a direct call could not reach it
        res = rest.test_connection(profile(base_url=far, proxy="custom", proxy_url=px.url), PW)
        assert res["ok"] and res["projects"] == 2, res
        assert res["proxy"]["url"] == px.url and res["proxy"]["source"] == "profile"
        assert [(m, t) for m, t, _a in px.seen] == [("POST", f"{far}/p6ws/restapi/login?DatabaseName={DB}"),
                                                     ("GET", f"{far}/p6ws/restapi/project?Fields=ObjectId,Id"),
                                                     ("POST", f"{far}/p6ws/restapi/logout")]
        # the Windows / environment proxy, used without any profile setting
        px.seen.clear()
        monkeypatch.setenv("HTTP_PROXY", px.url)
        res = rest.test_connection(profile(base_url=far), PW)
        assert res["ok"] and res["proxy"]["source"] == "environment" and len(px.seen) == 3
        # a server on this PC is never proxied, whatever the setting
        px.seen.clear()
        res = rest.test_connection(profile(base_url=server, proxy="custom", proxy_url=px.url), PW)
        assert res["ok"] and res["proxy"]["bypassed"] and px.seen == []
        # a proxy that does not answer: the message names it
        res = rest.test_connection(profile(base_url=far, proxy="custom", proxy_url="http://127.0.0.1:1"), PW)
        assert not res["ok"] and "through the proxy http://127.0.0.1:1 (profile)" in res["error"]
    finally:
        px.close()


def test_proxy_sign_in_uses_the_stored_password_and_never_shows_it(server, no_proxy_env, monkeypatch):
    port = int(server.rsplit(":", 1)[1])
    ppw = "p@ss:w0rd"  # characters a proxy URL could not carry
    px = Proxy({"eppm.test": port}, need_auth="Basic " + base64.b64encode(f"puser:{ppw}".encode()).decode())
    far = f"http://eppm.test:{port}"
    vault = {}
    monkeypatch.setattr("p6mcp.credentials.get", lambda k: vault.get(k))
    try:
        p = profile(base_url=far, proxy="custom", proxy_url=px.url)
        res = rest.test_connection(p, PW)
        assert not res["ok"] and "407" in res["error"] and "enter the proxy user and password" in res["error"]
        p = dict(p, proxy_user="puser")
        res = eppm.test_profile(p, PW, proxy_password="wrong-proxy-pw")
        assert not res["ok"] and "check the proxy user and password" in res["rest"]["error"]
        assert "wrong-proxy-pw" not in json.dumps(res)
        vault["eppm-proxy:test"] = ppw  # stored by the panel after a test that worked
        res = eppm.test_profile(p, PW)
        assert res["ok"] and res["rest"]["proxy"]["user"] == "puser" and ppw not in json.dumps(res)
        assert px.seen[-1][2] == "Basic " + base64.b64encode(f"puser:{ppw}".encode()).decode()
        assert not any("authorization" in k.lower() and "proxy" in k.lower() for _m, _p, h in Fake.seen for k in h)
        # https goes through a CONNECT tunnel, which carries the proxy credentials
        px.seen.clear()
        res = eppm.test_profile(dict(p, base_url="https://eppm.test:443"), PW)
        assert not res["ok"]  # the fake server speaks plain http in the tunnel
        assert px.seen[0][:2] == ("CONNECT", "eppm.test:443") and px.seen[0][2].startswith("Basic ")
        res = eppm.test_profile(dict(p, base_url="https://eppm.test:443"), PW, proxy_password="nope")
        assert not res["ok"] and "407" in res["rest"]["error"] and "nope" not in res["rest"]["error"]
    finally:
        px.close()
