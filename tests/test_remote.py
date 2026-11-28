"""Remote access server (3.2, milestone M1): remote tool policy, confirmation tokens bound to the OAuth connection,
local paths redacted, size budget, and a real uvicorn server on 127.0.0.1 behind the relay guard, on the synthetic
database. Token verification is a stub here (the authorization server is milestone M2)."""

import asyncio
import json
import socket
import threading
import time

import pytest
from conftest import free_port

from p6mcp import confirm, policy
from p6mcp.remote import RELAY_HEADER, resource_url, shaping

httpx = pytest.importorskip("httpx2")
pytest.importorskip("mcp.server.auth.settings")
from mcp.server.auth.middleware.auth_context import auth_context_var  # noqa: E402
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser  # noqa: E402
from mcp.server.auth.provider import AccessToken  # noqa: E402

from p6mcp.remote import server as remote  # noqa: E402

HOST = "p6mcp-relay.example.workers.dev"
SECRET = "relay-secret-0123456789"
READ, WRITE = ["p6:read"], ["p6:read", "p6:write"]
TOKENS = {"tok-read": ("grant-a", READ), "tok-write": ("grant-b", WRITE), "tok-write-2": ("grant-c", WRITE)}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
HDR = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture(autouse=True)
def _own_localappdata(tmp_path, monkeypatch):
    """The remote access server writes its audit log under LOCALAPPDATA: never the real profile in tests."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))


class StubVerifier:
    """Tokens of TOKENS, bound to the test's resource (what the authorization server of M2 will issue)."""

    def __init__(self, host=HOST):
        self.host = host

    async def verify_token(self, token):
        if token not in TOKENS:
            return None
        grant, scopes = TOKENS[token]
        return AccessToken(token=token, client_id="https://claude.ai/oauth/mcp-oauth-client-metadata",
                           scopes=scopes, expires_at=int(time.time()) + 3600, resource=resource_url(self.host),
                           claims={"grant": grant})


# ----------------------------------------------------------------- policy

EXPECTED_NEVER = {"p6_delete_activity", "p6_delete_project", "p6_eppm_push", "p6_restore_backup", "p6_export_xer",
                  "p6_export_dashboard", "p6_export_lookahead", "p6_import_xer", "p6_inspect_xer", "p6_shop_search",
                  "p6_shop_get", "p6_shop_install"}


def test_every_tool_has_a_remote_decision(server):
    assert set(server.REGISTERED) == set(policy.TOOL_KIND)
    kinds = {t: policy.remote_kind(t) for t in policy.TOOL_KIND}
    assert {t for t, k in kinds.items() if k == "never"} == EXPECTED_NEVER
    for t, k in kinds.items():
        if policy.is_write(t) and t not in EXPECTED_NEVER:
            assert k == "write", t
    assert kinds["p6_eppm_pull"] == "write" and kinds["p6_list_projects"] == "read"
    assert kinds["p6_pmdb_refresh"] == "write"  # rewrites the local copy: remote edits switch + scope, as a pull
    assert kinds["p6_trend"] == "read"  # 3.5.0: read only, offered to remote clients
    # 3.8.0: activity code values and WBS moves are writes, as p6_update_activities: remote writes switch + scope
    assert kinds["p6_update_activity_codes"] == kinds["p6_move_activities"] == "write"
    full = dict(profile="full", allow_writes=True)
    for t in ("p6_update_activity_codes", "p6_move_activities"):
        assert not policy.remote_listed(t, **full, remote_writes=False)
        assert policy.remote_listed(t, **full, remote_writes=True)
        assert policy.remote_refusal(t, "full", True, False, {"p6:read", "p6:write"}) == policy.REMOTE_WRITES_OFF
        assert policy.remote_refusal(t, "full", True, True, {"p6:read"}) == policy.REMOTE_SCOPE_OFF
        assert policy.remote_refusal(t, "full", True, True, {"p6:read", "p6:write"}) is None


def test_remote_listing():
    full = dict(profile="full", allow_writes=True)
    assert policy.remote_listed("p6_list_projects", **full, remote_writes=False)
    assert not policy.remote_listed("p6_update_activity", **full, remote_writes=False)
    assert policy.remote_listed("p6_update_activity", **full, remote_writes=True)
    assert not policy.remote_listed("p6_update_activity", "full", False, True)  # local writes off: not listed
    assert not policy.remote_listed("p6_delete_project", **full, remote_writes=True)
    assert not policy.remote_listed("p6_export_dashboard", **full, remote_writes=True)
    assert not policy.remote_listed("p6_sql_select", "basic", True, True)  # Basic profile still applies


def test_remote_refusal_rules():
    r = policy.remote_refusal
    assert r("p6_list_projects", "full", True, False, None) == policy.REMOTE_NO_GRANT
    assert r("p6_list_projects", "full", True, False, READ) is None
    assert r("p6_update_activity", "full", True, False, WRITE) == policy.REMOTE_WRITES_OFF
    assert r("p6_update_activity", "full", True, True, READ) == policy.REMOTE_SCOPE_OFF
    assert r("p6_update_activity", "full", True, True, WRITE) is None
    assert r("p6_update_activity", "full", False, True, WRITE) == policy.WRITES_OFF  # local rule on top
    assert "not available to remote clients" in r("p6_delete_project", "full", True, True, WRITE)
    assert "xer_path" in r("p6_schedule_options", "full", True, False, READ, args={"xer_path": "C:\\a.xer"})
    assert r("p6_schedule_options", "full", True, False, READ, args={"project": "DEMO-A"}) is None


def test_confirmation_token_is_bound_to_its_connection():
    store = confirm.TokenStore()
    g = store.issue("p6_update_activity", {"a": 1}, "x.db", "plan", owner="grant-a")
    with pytest.raises(confirm.TokenError, match="another connection"):
        store.check("p6_update_activity", {"a": 1}, "x.db", g.token, owner="grant-b")
    with pytest.raises(confirm.TokenError, match="another connection"):
        store.check("p6_update_activity", {"a": 1}, "x.db", g.token)  # a local client cannot use it either
    assert store.check("p6_update_activity", {"a": 1}, "x.db", g.token, owner="grant-a") is g
    local = store.issue("p6_update_activity", {"a": 1}, "x.db", "plan")
    assert store.check("p6_update_activity", {"a": 1}, "x.db", local.token) is local


# ----------------------------------------------------------------- shaping

def test_paths_are_redacted():
    out = shaping.redact({
        "db_path": "C:\\Users\\someone\\Documents\\PPMDBSQLite.db", "unc": "\\\\server\\share\\p6\\x.db",
        "fwd": "D:/data/p6.db", "rows": [{"file": "C:\\logs\\edits_20261126.jsonl"}],
        "plan": ["Copy demo.db (1.0 MB) to C:\\Users\\someone\\AppData\\Local\\P6MCP\\backups"],
        "note": "Activity 'A1000 - Build: phase 1' is fine", "n": 3})
    assert out["db_path"] == "PPMDBSQLite.db" and out["unc"] == "x.db" and out["fwd"] == "p6.db"
    assert out["rows"][0]["file"] == "edits_20261126.jsonl"
    assert out["plan"] == ["Copy demo.db (1.0 MB) to backups"]
    assert out["note"] == "Activity 'A1000 - Build: phase 1' is fine" and out["n"] == 3


def test_size_budget_cuts_a_page_and_refuses_the_rest():
    rows = [{"i": i, "text": "x" * 500} for i in range(400)]
    page = {"total": 900, "offset": 100, "returned": 400, "next_offset": 500, "rows": rows}
    cut = shaping.fit(page, limit=50_000)
    assert shaping.size(cut) <= 50_000 and 1 <= cut["returned"] < 400
    assert cut["rows"] == rows[:cut["returned"]] and cut["next_offset"] == 100 + cut["returned"]
    assert "offset=" in cut["remote_note"]
    small = {"rows": rows[:2], "offset": 0, "next_offset": None, "total": 2, "returned": 2}
    assert shaping.fit(small, limit=50_000) is small
    with pytest.raises(shaping.TooLarge, match="too large"):
        shaping.fit({"text": "x" * 60_000}, limit=50_000)
    with pytest.raises(shaping.TooLarge):  # a single row over the budget cannot be paged down
        shaping.fit({"rows": [{"t": "x" * 60_000}], "offset": 0, "next_offset": None}, limit=50_000)


LIST_TOOLS = {"p6_list_projects": {}, "p6_list_eps": {}, "p6_list_wbs": {"project": "DEMO-A"},
              "p6_query_activities": {"project": "DEMO-A"}, "p6_critical_path": {"project": "DEMO-A"},
              "p6_float_paths": {"project": "DEMO-A"},
              "p6_get_relationships": {"project": "DEMO-A", "task_code": "A1020"},
              "p6_list_calendars": {"include_exceptions": True}, "p6_lookahead": {"project": "DEMO-A"},
              "p6_milestones": {"project": "DEMO-A"}, "p6_group_summary": {"project": "DEMO-A"},
              "p6_resource_summary": {"project": "DEMO-A"}, "p6_resource_loading": {"project": "DEMO-A"},
              "p6_calendar_check": {"project": "DEMO-A"},
              "p6_sql_select": {"sql": "SELECT * FROM TASK"}}


def test_every_remote_list_tool_stays_in_budget(p6):
    p6.set(remote_public_host=HOST)
    st = p6.config.load()
    names = remote.remote_tools(st)
    assert set(LIST_TOOLS) <= set(names)
    tok = auth_context_var.set(AuthenticatedUser(asyncio.run(StubVerifier().verify_token("tok-read"))))
    try:
        for name, args in LIST_TOOLS.items():
            fn = remote._remote_tool(name, p6.s.REGISTERED[name], p6.s.ToolError)
            res = fn(**args, limit=200)
            assert shaping.size(res) <= shaping.MAX_CHARS, name
            assert ":\\" not in json.dumps(res), name
    finally:
        auth_context_var.reset(tok)


def test_diagnose_shows_no_local_path(p6):
    p6.set(remote_public_host=HOST)
    tok = auth_context_var.set(AuthenticatedUser(asyncio.run(StubVerifier().verify_token("tok-read"))))
    try:
        listed = remote.remote_tools(p6.config.load())
        res = remote._remote_tool("p6_diagnose", p6.s.REGISTERED["p6_diagnose"], p6.s.ToolError, listed)()
    finally:
        auth_context_var.reset(tok)
    assert res["tools_listed"] == len(listed) and res["restart_required"] is False
    assert res["remote"]["public_url"] == f"https://{HOST}/mcp" and res["remote"]["connection"]["scopes"] == READ
    text = json.dumps(res)
    assert p6.db.replace("\\", "\\\\") not in text and ":\\\\" not in text and "demo.db" in text


def test_remote_instructions_are_ascii(server):
    text = remote.instructions()
    assert text.isascii() and remote.REMOTE_LINE in text


# ----------------------------------------------------------------- live server

def _start(app):
    port = free_port()
    srv = remote.make_server(app, port)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.monotonic() + 20
    while not srv.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    return srv, th, port


@pytest.fixture
def live(p6):
    p6.set(remote_public_host=HOST, remote_allow_writes=True)
    st = p6.config.load()
    srv, th, port = _start(remote.build_app(st, StubVerifier(), SECRET))
    yield {"url": f"http://127.0.0.1:{port}/mcp", "port": port, "p6": p6, "st": st}
    srv.config.timeout_graceful_shutdown = 0.2  # 3 s in the product: open streams are not waited for here
    srv.should_exit = True
    th.join(10)


def post(url, headers, body=None):
    return httpx.post(url, json=body or INIT, headers={**HDR, **headers}, timeout=10)


def test_relay_secret_is_required(live):
    u = live["url"]
    assert post(u, {"authorization": "Bearer tok-read"}).status_code == 403
    assert post(u, {"authorization": "Bearer tok-read", RELAY_HEADER: "wrong"}).status_code == 403
    assert post(u, {"authorization": "Bearer tok-read", RELAY_HEADER: SECRET}).status_code == 200


def test_401_points_to_the_resource_metadata(live):
    r = post(live["url"], {RELAY_HEADER: SECRET})
    assert r.status_code == 401
    meta_url = f"https://{HOST}/.well-known/oauth-protected-resource/mcp"
    assert f'resource_metadata="{meta_url}"' in r.headers["www-authenticate"]
    meta = httpx.get(f"http://127.0.0.1:{live['port']}/.well-known/oauth-protected-resource/mcp",
                     headers={RELAY_HEADER: SECRET}, timeout=5).json()
    assert meta["resource"] == f"https://{HOST}/mcp" and meta["authorization_servers"] == [f"https://{HOST}"]
    assert post(live["url"], {RELAY_HEADER: SECRET, "authorization": "Bearer unknown"}).status_code == 401


def test_body_cap(live):
    r = httpx.post(live["url"], content=b"x" * (remote.BODY_CAP + 1),
                   headers={**HDR, RELAY_HEADER: SECRET, "authorization": "Bearer tok-read"}, timeout=10)
    assert r.status_code == 413


def _body(res):
    assert not res.is_error, res.content[0].text
    if res.structured_content is not None:
        return res.structured_content.get("result", res.structured_content)
    return json.loads(res.content[0].text)


def _session(live, token, steps):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def go():
        hdr = {"Authorization": f"Bearer {token}", RELAY_HEADER: SECRET}
        async with httpx.AsyncClient(headers=hdr, timeout=30) as hc, \
                streamable_http_client(live["url"], http_client=hc) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                return await steps(s)
    return asyncio.run(go())


def test_tool_list_and_read_call(live):
    async def steps(s):
        names = {t.name for t in (await s.list_tools()).tools}
        res = await s.call_tool("p6_list_projects", {})
        return names, _body(res)
    names, body = _session(live, "tok-read", steps)
    assert names == set(remote.remote_tools(live["st"]))
    assert not names & EXPECTED_NEVER and "p6_update_activity" in names
    assert any(r.get("proj_short_name", r.get("project")) == "DEMO-A" or "DEMO-A" in json.dumps(r)
               for r in body["rows"])


def test_write_needs_the_write_scope(live):
    args = {"project": "DEMO-A", "task_code": "A1020", "remaining_duration_days": 12}

    async def steps(s):
        return await s.call_tool("p6_update_activity", args)
    res = _session(live, "tok-read", steps)
    assert res.is_error and "approved read only" in res.content[0].text


def test_two_step_write_is_bound_to_the_connection(live):
    args = {"project": "DEMO-A", "task_code": "A1020", "remaining_duration_days": 12}

    async def dry(s):
        return _body(await s.call_tool("p6_update_activity", args))
    d = _session(live, "tok-write", dry)
    assert d["dry_run"] and d["confirmation_token"]

    async def other(s):  # another connection (ChatGPT, say) replays the token
        return await s.call_tool("p6_update_activity", dict(args, dry_run=False,
                                                            confirmation_token=d["confirmation_token"]))
    res = _session(live, "tok-write-2", other)
    assert res.is_error and "another connection" in res.content[0].text
    assert live["p6"].sql("SELECT remain_drtn_hr_cnt FROM TASK WHERE task_code = 'A1020'")[0][0] != 96

    async def same(s):  # the same connection, in a new MCP session: allowed
        return _body(await s.call_tool("p6_update_activity", dict(args, dry_run=False,
                                                                  confirmation_token=d["confirmation_token"])))
    done = _session(live, "tok-write", same)
    assert done["applied"] is True
    assert live["p6"].sql("SELECT remain_drtn_hr_cnt FROM TASK WHERE task_code = 'A1020'")[0][0] == 96
    assert ":\\" not in json.dumps(done)


def test_writes_off_hides_write_tools(p6):
    p6.set(remote_public_host=HOST, remote_allow_writes=False)
    st = p6.config.load()
    names = set(remote.remote_tools(st))
    assert "p6_list_projects" in names and not {t for t in names if policy.remote_kind(t) == "write"}


@pytest.mark.slow
def test_domain_mode_checks_the_host(p6):
    p6.set(remote_public_host="p6.example.com", remote_mode="domain")
    srv, th, port = _start(remote.build_app(p6.config.load(), StubVerifier("p6.example.com")))
    try:
        u = f"http://127.0.0.1:{port}/mcp"
        assert post(u, {"authorization": "Bearer tok-read"}).status_code == 421
        assert post(u, {"authorization": "Bearer tok-read", "host": "p6.example.com"}).status_code == 200
    finally:
        srv.config.timeout_graceful_shutdown = 0.2  # 3 s in the product: open streams are not waited for here
        srv.should_exit = True
        th.join(10)


def test_setup_errors(p6):
    p6.set(remote_public_host=None)
    with pytest.raises(remote.SetupError, match="no public address"):
        remote.build_app(p6.config.load(), StubVerifier(), SECRET)
    p6.set(remote_public_host=HOST)
    with pytest.raises(remote.SetupError, match="relay secret"):
        remote.build_app(p6.config.load(), StubVerifier(), None)


def test_serve_reports_setup_and_port_problems(p6, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    p6.set(remote_public_host=None)
    assert remote.serve(free_port()) == 2
    st = json.loads((tmp_path / "P6MCP" / "remote_status.json").read_text(encoding="utf-8"))
    assert st["state"] == "error" and "public address" in st["error"]
    p6.set(remote_public_host=HOST)
    monkeypatch.setattr(remote.credentials, "get", lambda name: SECRET)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        assert remote.serve(s.getsockname()[1]) == 3
    st = json.loads((tmp_path / "P6MCP" / "remote_status.json").read_text(encoding="utf-8"))
    assert "already in use" in st["error"]
