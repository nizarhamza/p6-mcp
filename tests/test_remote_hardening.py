"""Remote access, milestone M5: audit log (requests, tool calls, OAuth events; no values, tokens or query strings),
the remote client in the edit log, rate limits, the optional IP allowlist and audit retention."""

import json
import os
import threading
import time
from datetime import date, timedelta

import pytest
from conftest import free_port

httpx = pytest.importorskip("httpx2")
pytest.importorskip("mcp.server.auth.settings")
from mcp.server.auth.middleware.auth_context import auth_context_var  # noqa: E402
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser  # noqa: E402
from mcp.server.auth.provider import AccessToken  # noqa: E402

from p6mcp.remote import RELAY_HEADER, audit, resource_url  # noqa: E402
from p6mcp.remote import server as remote  # noqa: E402
from p6mcp.remote.limits import Allowlist, RateLimiter  # noqa: E402
from p6mcp.remote.store import Store  # noqa: E402

HOST = "p6mcp-pc.example.workers.dev"
SECRET = "relay-secret-0123456789"


@pytest.fixture(autouse=True)
def _own_localappdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))


def token(scopes, grant="grant-a", app="Claude"):
    return AccessToken(token="t", client_id="https://claude.ai/oauth/mcp-oauth-client-metadata", scopes=scopes,
                       expires_at=int(time.time()) + 3600, resource=resource_url(HOST),
                       claims={"grant": grant, "client_name": app})


def call(p6, name, tok, **args):
    ctx = auth_context_var.set(AuthenticatedUser(tok))
    try:
        listed = remote.remote_tools(p6.config.load())
        return remote._remote_tool(name, p6.s.REGISTERED[name], p6.s.ToolError, listed)(**args)
    finally:
        auth_context_var.reset(ctx)


def audit_text() -> str:
    path = os.path.join(audit.folder(), f"audit_{date.today():%Y%m%d}.jsonl")
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


# ----------------------------------------------------------------- tool calls and the edit log

def test_tool_calls_are_audited_without_values(p6):
    p6.set(remote_public_host=HOST, remote_allow_writes=True)
    call(p6, "p6_query_activities", token(["p6:read"]), project="DEMO-A", limit=5)
    with pytest.raises(Exception, match="approved read only"):
        call(p6, "p6_update_activity", token(["p6:read"]), project="DEMO-A", task_code="A1020",
             remaining_duration_days=12)
    rows = [r for r in audit.read() if r["event"] == "tool"]
    ok, refused = rows[-2], rows[-1]
    assert ok["tool"] == "p6_query_activities" and ok["result"] == "ok" and ok["app"] == "Claude"
    assert ok["grant"] == "grant-a" and len(ok["args"]) == 16 and ok["chars"] > 0 and ok["ms"] >= 0
    assert refused["result"] == "refused" and refused["tool"] == "p6_update_activity"
    text = audit_text()
    assert "DEMO-A" not in text and "A1020" not in text  # a digest, never the values


def test_remote_edit_names_its_connection_in_the_edit_log(p6):
    p6.set(remote_public_host=HOST, remote_allow_writes=True)
    tok = token(["p6:read", "p6:write"], grant="grant-w", app="ChatGPT")
    args = {"project": "DEMO-A", "task_code": "A1020", "remaining_duration_days": 12}
    dry = call(p6, "p6_update_activity", tok, **args)
    done = call(p6, "p6_update_activity", tok, **args, dry_run=False, confirmation_token=dry["confirmation_token"])
    assert done["applied"] is True
    entries = [json.loads(ln) for f in sorted(os.listdir(p6.logs)) for ln in open(os.path.join(p6.logs, f),
                                                                                       encoding="utf-8")]
    assert entries[-1]["client"] == {"transport": "remote", "grant": "grant-w", "app": "ChatGPT"}
    tools = [r for r in audit.read() if r["event"] == "tool"]
    assert [r.get("dry_run") for r in tools[-2:]] == [True, False]
    p6.apply("p6_update_activity", project="DEMO-A", task_code="A1020", remaining_duration_days=11)  # local
    local = [json.loads(ln) for f in sorted(os.listdir(p6.logs)) for ln in open(os.path.join(p6.logs, f),
                                                                                     encoding="utf-8")][-1]
    assert "client" not in local


# ----------------------------------------------------------------- live server: requests, limits, allowlist

@pytest.fixture
def live(p6, tmp_path):
    p6.set(remote_public_host=HOST)
    guard = remote.build_app(p6.config.load(), relay_secret=SECRET, store=Store(str(tmp_path / "auth.db")),
                             fetch=lambda url: None)
    port = free_port()
    srv = remote.make_server(guard, port)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.monotonic() + 20
    while not srv.started:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    yield {"base": f"http://127.0.0.1:{port}", "guard": guard}
    srv.config.timeout_graceful_shutdown = 0.2  # 3 s in the product: open streams are not waited for here
    srv.should_exit = True
    th.join(10)


def req(live, method, path, ip="160.79.106.5", **kw):
    hdr = {RELAY_HEADER: SECRET, "x-p6mcp-client-ip": ip, **kw.pop("headers", {})}
    return httpx.request(method, live["base"] + path, headers=hdr, timeout=10, follow_redirects=False, **kw)


def test_requests_are_audited_without_query_strings(live):
    r = req(live, "GET", "/authorize", params={"client_id": "https://evil.example/c", "state": "s3cret-state",
                                               "code_challenge": "x" * 43})
    assert r.status_code == 400
    req(live, "POST", "/token", data={"grant_type": "authorization_code", "code": "c0de-value"})
    rows = [r for r in audit.read() if r["event"] == "http"]
    assert {"/authorize", "/token"} <= {r["path"] for r in rows}
    assert all(r["ip"] == "160.79.106.5" and isinstance(r["ms"], int) for r in rows)
    text = audit_text()
    assert "s3cret-state" not in text and "c0de-value" not in text and "evil.example" not in text
    errors = [r for r in audit.read() if r["event"] == "oauth" and r.get("what") == "error"]
    assert errors and errors[-1]["error"] == "invalid_grant"


def test_rate_limit(live):
    live["guard"].limiter = RateLimiter({"/token": (60, 2)})
    codes = [req(live, "POST", "/token", data={"grant_type": "x"}).status_code for _ in range(3)]
    assert codes[:2] == [400, 400] and codes[2] == 429
    r = req(live, "POST", "/token", data={"grant_type": "x"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert req(live, "POST", "/token", ip="160.79.106.99", data={"grant_type": "x"}).status_code == 400  # per IP
    assert any(r.get("reason") == "rate limit" for r in audit.read())
    for _ in range(5):  # the browser page polls this one
        assert req(live, "GET", "/authorize/wait", params={"id": "x" * 20}).status_code == 404


def test_ip_allowlist(live, tmp_path):
    al = Allowlist(fetch=lambda: {"prefixes": [{"ipv4Prefix": "9.129.22.0/24"}]}, cache=str(tmp_path / "o.json"))
    assert al.refresh()
    live["guard"].allowlist = al
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    assert req(live, "POST", "/mcp", ip="1.2.3.4", json=init).status_code == 403
    assert req(live, "POST", "/mcp", ip="160.79.106.5", json=init).status_code == 401  # Anthropic: on to OAuth
    assert req(live, "POST", "/mcp", ip="9.129.22.7", json=init).status_code == 401  # OpenAI's list
    assert req(live, "POST", "/token", ip="1.2.3.4", data={}).status_code == 403
    assert req(live, "GET", "/authorize", ip="1.2.3.4").status_code == 400  # the user's browser: not filtered
    assert req(live, "GET", "/.well-known/oauth-protected-resource/mcp", ip="1.2.3.4").status_code == 200


def test_allowlist_keeps_its_last_good_copy(tmp_path):
    cache = str(tmp_path / "o.json")
    assert Allowlist(fetch=lambda: {"prefixes": [{"ipv4Prefix": "9.129.22.0/24"}]}, cache=cache).refresh()

    def boom():
        raise OSError("offline")
    al = Allowlist(fetch=boom, cache=cache)  # loads the cached copy
    assert not al.refresh() and al.allows("9.129.22.1") and al.allows("153.61.208.9")
    assert not al.allows("10.0.0.1") and not al.allows(None) and not al.allows("not an ip")


def test_rate_limiter_refills():
    now = [0.0]
    rl = RateLimiter({"/register": (6, 2)}, clock=lambda: now[0])
    assert rl.allow("/register", "a") == (True, 0) and rl.allow("/register", "a")[0]
    ok, wait = rl.allow("/register", "a")
    assert not ok and wait >= 1
    now[0] += 10  # 6 a minute: one more after 10 s
    assert rl.allow("/register", "a")[0] and not rl.allow("/register", "a")[0]
    assert rl.allow("/.well-known/oauth-authorization-server", "a") == (True, 0)


def test_audit_retention(monkeypatch):
    os.makedirs(audit.folder(), exist_ok=True)
    old = date.today() - timedelta(days=audit.KEEP_DAYS + 3)
    recent = date.today() - timedelta(days=2)
    for d in (old, recent):
        open(os.path.join(audit.folder(), f"audit_{d:%Y%m%d}.jsonl"), "w").close()
    monkeypatch.setattr(audit, "_pruned", None)
    audit.write("http", path="/x")
    names = os.listdir(audit.folder())
    assert f"audit_{old:%Y%m%d}.jsonl" not in names and f"audit_{recent:%Y%m%d}.jsonl" in names


def test_remote_sql_hides_credential_columns(p6):
    p6.set(remote_public_host=HOST)
    con = __import__("sqlite3").connect(p6.db)
    con.execute("CREATE TABLE USERS (user_id INTEGER, user_name TEXT, passwd TEXT, email_srv_passwd TEXT)")
    con.execute("INSERT INTO USERS VALUES (1, 'admin', 'hash-value', 'mail-secret')")
    con.commit()
    con.close()
    p6.s._cols_cache.clear()
    p6.s._schema_cache.clear()
    res = call(p6, "p6_sql_select", token(["p6:read"]), sql="SELECT user_name, passwd, email_srv_passwd FROM USERS")
    assert res["rows"] == [{"user_name": "admin", "passwd": None, "email_srv_passwd": None}]
    local = p6.call("p6_sql_select", sql="SELECT passwd FROM USERS")  # a local client is not changed
    assert local["rows"] == [{"passwd": "hash-value"}]


def test_redaction_of_known_folders_with_spaces():
    from p6mcp.remote import shaping
    roots = shaping.known_roots({"C:\\Users\\John Smith\\OneDrive - Acme\\backups": None,
                                 "C:\\Users\\John Smith": "~"})
    out = shaping.redact("Copy demo.db to C:\\Users\\John Smith\\OneDrive - Acme\\backups now", roots)
    assert out == "Copy demo.db to backups now"
    assert shaping.redact("Log in c:\\users\\john smith\\notes.txt, done", roots) == "Log in ~\\notes.txt, done"
    # another folder (not one of the PC's known ones) is cut at its first space: the known limitation
    assert shaping.redact("Folder C:\\Users\\John Smithson\\x", roots) == "Folder John Smithson\\x"


def test_approval_cap_and_decide_by_id(tmp_path):
    from p6mcp.remote.store import MAX_OPEN, TooManyRequests
    store = Store(str(tmp_path / "a.db"))
    client = {"client_id": "c", "client_name": "Claude", "via": "cimd"}
    reqs = [store.add_pending(client, "https://claude.ai/api/mcp/auth_callback", "s", "c" * 43, ["p6:read"],
                              f"https://{HOST}/mcp") for _ in range(MAX_OPEN)]
    with pytest.raises(TooManyRequests):
        store.add_pending(client, "https://claude.ai/api/mcp/auth_callback", "s", "c" * 43, ["p6:read"],
                          f"https://{HOST}/mcp")
    assert store.decide_request(reqs[0]["id"], "WRONG1", True) == {"mismatch": True}
    assert store.pending(reqs[0]["id"])["status"] == "pending"
    assert store.decide_request(reqs[0]["id"], reqs[0]["pair"].lower(), True)["status"] == "approved"
    assert store.decide_request(reqs[0]["id"], reqs[0]["pair"], True) is None  # decided already
    assert store.decide_request(reqs[1]["id"], "", False)["status"] == "denied"


def test_authorize_refuses_past_the_cap(live, monkeypatch):
    from p6mcp.remote import store as store_mod

    def full(*a, **k):
        raise store_mod.TooManyRequests("3 waiting")
    monkeypatch.setattr(store_mod.Store, "add_pending", full)
    monkeypatch.setattr("p6mcp.remote.auth.resolve_client", _fake_client)
    r = req(live, "GET", "/authorize", params={
        "client_id": "https://claude.ai/oauth/mcp-oauth-client-metadata", "response_type": "code",
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback", "code_challenge": "c" * 43,
        "code_challenge_method": "S256"})
    assert r.status_code == 429 and "Too many connection requests" in r.text


async def _fake_client(client_id, store, fetch=None):
    return {"client_id": client_id, "client_name": "Claude", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "via": "cimd"}


def test_bad_port_in_client_id_is_refused(tmp_path):
    import asyncio
    from p6mcp.remote import auth
    store = Store(str(tmp_path / "a.db"))
    assert asyncio.run(auth.resolve_client("https://claude.ai:abc/x", store, lambda u: None)) is None


def test_remote_needs_mcp_2(p6, monkeypatch):
    from mcp.server.auth.settings import AuthSettings
    p6.set(remote_public_host=HOST)
    fields = dict(AuthSettings.model_fields)
    fields.pop("validate_token_resource")
    monkeypatch.setattr(AuthSettings, "model_fields", fields)
    with pytest.raises(remote.SetupError, match="mcp package 2.x"):
        remote.build_mcp(p6.config.load(), None)


def test_dcr_and_revoke_events(live):
    r = req(live, "POST", "/register", json={"client_name": "Claude", "redirect_uris":
                                                ["https://claude.ai/api/mcp/auth_callback"]})
    assert r.status_code == 201
    events = [e for e in audit.read() if e["event"] == "oauth"]
    assert events[-1]["what"] == "registered" and events[-1]["hosts"] == ["claude.ai"]
