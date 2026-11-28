"""Remote file handoff (plan item P4): exports through a one-time download link, an XER through a one-time upload
page and upload_id, on a real uvicorn server behind the relay guard (no network beyond 127.0.0.1). Tokens of the
OAuth connections are stubs (grant-a, grant-b); the link tokens are the real ones."""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import date

import pytest
from conftest import free_port

from p6mcp import policy

httpx = pytest.importorskip("httpx2")
pytest.importorskip("mcp.server.auth.settings")
from mcp.server.auth.middleware.auth_context import auth_context_var  # noqa: E402
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser  # noqa: E402
from mcp.server.auth.provider import AccessToken  # noqa: E402

from p6mcp.remote import RELAY_HEADER, audit, files, resource_url  # noqa: E402
from p6mcp.remote import server as remote  # noqa: E402
from p6mcp.remote.store import Store  # noqa: E402

HOST = "p6mcp-relay.example.workers.dev"
SECRET = "relay-secret-0123456789"
READ, WRITE = ["p6:read"], ["p6:read", "p6:write"]
TOKENS = {"tok-a": ("grant-a", WRITE), "tok-b": ("grant-b", WRITE), "tok-read": ("grant-r", READ)}


@pytest.fixture(autouse=True)
def _own_localappdata(tmp_path, monkeypatch):
    """The audit log, auth.db and the private file folders: never the real profile in tests."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))


class StubVerifier:
    async def verify_token(self, token):
        if token not in TOKENS:
            return None
        grant, scopes = TOKENS[token]
        return AccessToken(token=token, client_id="https://claude.ai/oauth/mcp-oauth-client-metadata", scopes=scopes,
                           expires_at=int(time.time()) + 3600, resource=resource_url(HOST),
                           claims={"grant": grant, "client_name": "Claude"})


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
    p6.set(remote_public_host=HOST, remote_allow_writes=True, remote_allow_files=True)
    guard = remote.build_app(p6.config.load(), StubVerifier(), SECRET)
    srv, th, port = _start(guard)
    yield {"base": f"http://127.0.0.1:{port}", "port": port, "p6": p6, "handoff": guard.handoff}
    srv.config.timeout_graceful_shutdown = 0.2
    srv.should_exit = True
    th.join(10)


def call(live, name, token="tok-a", **args):
    tok = asyncio.run(StubVerifier().verify_token(token))
    ctx = auth_context_var.set(AuthenticatedUser(tok))
    p6 = live["p6"]
    try:
        if name == files.UPLOAD_TOOL:
            return remote._upload_link_tool(live["handoff"], p6.s.ToolError)()
        listed = remote.remote_tools(p6.config.load())
        return remote._remote_tool(name, p6.s.REGISTERED[name], p6.s.ToolError, listed, live["handoff"])(**args)
    finally:
        auth_context_var.reset(ctx)


def local(live, url):
    """The relay's request for a public link: same path, relay secret added."""
    assert url.startswith(f"https://{HOST}/")
    return live["base"] + url[len(f"https://{HOST}"):]


def get(live, url, **kw):
    return httpx.get(local(live, url), headers={RELAY_HEADER: SECRET}, timeout=30, **kw)


def post(live, url, **kw):
    return httpx.post(local(live, url), headers={RELAY_HEADER: SECRET, **kw.pop("headers", {})}, timeout=60, **kw)


def rows(live):
    con = sqlite3.connect(live["handoff"].store.path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM files")]
    finally:
        con.close()


def audit_text() -> str:
    path = os.path.join(audit.folder(), f"audit_{date.today():%Y%m%d}.jsonl")
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def xer_bytes(p6, tmp_path) -> bytes:
    path = tmp_path / "source.xer"
    p6.call("p6_export_xer", project="DEMO-A", path=str(path))
    return path.read_bytes()


# ----------------------------------------------------------------- downloads

def test_xer_export_downloads_once_then_404(live, caplog):
    caplog.set_level(logging.DEBUG)
    res = call(live, "p6_export_xer", project="DEMO-A")
    assert "path" not in res and res["file_name"] == "DEMO-A.xer" and res["single_use"] is True
    assert res["download_url"].startswith(f"https://{HOST}/files/") and res["expires_in_s"] == files.LINK_TTL
    token = res["download_url"].rsplit("/", 1)[1]
    assert len(token) >= 43  # 256 bits, url-safe base64
    assert ":\\" not in json.dumps(res)
    stored = rows(live)
    assert len(stored) == 1 and stored[0]["link"] != token and len(stored[0]["link"]) == 64  # a hash at rest
    folder = os.path.join(files.root(), files._grant_dir("grant-a"), stored[0]["id"])
    assert os.path.isfile(os.path.join(folder, "DEMO-A.xer"))
    page = get(live, res["download_url"])  # the page: no side effect (a link preview never spends the link)
    assert page.status_code == 200 and "DEMO-A.xer" in page.text and 'method="post"' in page.text
    assert "form-action 'self'" in page.headers["content-security-policy"]
    assert get(live, res["download_url"]).status_code == 200
    r = post(live, res["download_url"])
    assert r.status_code == 200 and r.content.startswith(b"ERMHDR") and len(r.content) == res["size_bytes"]
    assert r.headers["content-disposition"].startswith("attachment") and "DEMO-A.xer" in r.headers[
        "content-disposition"]
    assert r.headers["content-type"] == "application/octet-stream" and r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    for again in (post(live, res["download_url"]), get(live, res["download_url"])):
        assert again.status_code == 404 and "already used" in again.text
    assert rows(live) == [] and not os.path.exists(folder)
    events = [e for e in audit.read() if e["event"] == "file"]
    assert [e["what"] for e in events] == ["link_minted", "link_used"]
    text = audit_text()
    assert token not in text and "DEMO-A" not in text and "/files/<link>" in text
    server_side = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]  # not the test client
    assert server_side and not [r for r in server_side if token in r.getMessage()]


def test_dashboard_downloads_as_xlsx(live):
    res = call(live, "p6_export_dashboard", project="DEMO-A", file_name="Board report")
    assert res["file_name"] == "Board report.xlsx" and res["cards"]
    r = post(live, res["download_url"])
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert r.headers["content-type"] == files.CONTENT_TYPES[".xlsx"]
    assert post(live, res["download_url"]).status_code == 404
    la = call(live, "p6_export_lookahead", project="DEMO-A")
    assert la["file_name"] == "DEMO-A_lookahead.xlsx" and post(live, la["download_url"]).content[:2] == b"PK"


def test_export_arguments_and_signature(live):
    with pytest.raises(Exception, match="path is not available to remote clients"):
        call(live, "p6_export_xer", project="DEMO-A", path="C:\\temp\\x.xer")
    with pytest.raises(Exception, match="without a folder"):
        call(live, "p6_export_xer", project="DEMO-A", file_name="..\\..\\x.xer")
    with pytest.raises(Exception, match="not found"):  # a failed export leaves nothing behind
        call(live, "p6_export_xer", project="NOPE")
    assert rows(live) == []
    p6 = live["p6"]
    sig = remote._remote_tool("p6_export_xer", p6.s.REGISTERED["p6_export_xer"], None, [], live["handoff"])
    import inspect
    params = inspect.signature(sig).parameters
    assert "file_name" in params and "path" not in params and "overwrite" not in params
    params = inspect.signature(remote._remote_tool("p6_import_xer", p6.s.REGISTERED["p6_import_xer"], None, [],
                                                   live["handoff"])).parameters
    assert "upload_id" in params and "path" not in params and "confirmation_token" in params


def test_expired_and_revoked_links(live):
    a = call(live, "p6_export_xer", project="DEMO-A")
    b = call(live, "p6_export_xer", project="DEMO-A", file_name="second")
    con = sqlite3.connect(live["handoff"].store.path)
    with con:
        con.execute("UPDATE files SET exp = 0 WHERE name = 'DEMO-A.xer'")
    con.close()
    r = get(live, a["download_url"])
    assert r.status_code == 404 and [x["name"] for x in rows(live)] == ["second.xer"]
    assert "link_expired" in [e["what"] for e in audit.read() if e["event"] == "file"]
    Store(live["handoff"].store.path).revoke("grant-a")  # the tray or the CLI revokes the connection
    assert post(live, b["download_url"]).status_code == 404 and rows(live) == []
    live["handoff"].sweep()
    assert not os.path.exists(os.path.join(files.root(), files._grant_dir("grant-a")))


def test_only_the_grant_that_made_a_file_can_mint_its_link(live):
    h = live["handoff"]
    fid, path = h.new_download("grant-a", "p6_export_xer", None, "DEMO-A")
    open(path, "wb").write(b"ERMHDR\t25.12")
    with pytest.raises(ValueError, match="another connection"):
        h.publish(fid, "grant-b", HOST)
    assert h.publish(fid, "grant-a", HOST)["download_url"]
    with pytest.raises(ValueError, match="another connection"):  # minted once
        h.publish(fid, "grant-a", HOST)


def test_per_connection_limit(live, monkeypatch):
    monkeypatch.setattr(files, "MAX_PER_CONNECTION", 2)
    call(live, files.UPLOAD_TOOL)
    call(live, "p6_export_xer", project="DEMO-A")
    with pytest.raises(Exception, match="Too many files"):
        call(live, "p6_export_xer", project="DEMO-A")
    assert call(live, files.UPLOAD_TOOL, token="tok-b")["upload_id"]  # another connection has its own room


# ----------------------------------------------------------------- uploads

def test_upload_then_inspect_and_import_by_upload_id(live, tmp_path):
    data = xer_bytes(live["p6"], tmp_path)
    up = call(live, files.UPLOAD_TOOL)
    assert up["upload_url"].startswith(f"https://{HOST}/upload/") and up["max_bytes"] == files.UPLOAD_MAX
    token = up["upload_url"].rsplit("/", 1)[1]
    with pytest.raises(Exception, match="not uploaded yet"):
        call(live, "p6_inspect_xer", upload_id=up["upload_id"])
    page = get(live, up["upload_url"])
    assert page.status_code == 200 and 'enctype="multipart/form-data"' in page.text and 'accept=".xer"' in page.text
    assert "form-action 'self'" in page.headers["content-security-policy"]
    r = post(live, up["upload_url"], files={"file": ("Update week 48.xer", data, "application/octet-stream")})
    assert r.status_code == 200 and "File received" in r.text and "Update week 48.xer" in r.text
    assert get(live, up["upload_url"]).status_code == 404  # single use
    assert post(live, up["upload_url"], files={"file": ("x.xer", data)}).status_code == 404
    seen = call(live, "p6_inspect_xer", upload_id=up["upload_id"])
    assert seen["upload_id"] == up["upload_id"] and seen["path"] == "Update week 48.xer"
    assert seen["projects"][0]["short_name"] == "DEMO-A" and seen["preflight"] is not None
    assert ":\\" not in json.dumps(seen)
    with pytest.raises(Exception, match="unknown, expired or not uploaded"):  # another connection: nothing to see
        call(live, "p6_inspect_xer", token="tok-b", upload_id=up["upload_id"])
    args = {"upload_id": up["upload_id"], "mode": "new", "new_short_name": "DEMO-UP"}
    dry = call(live, "p6_import_xer", **args)
    assert dry["dry_run"] and dry["confirmation_token"] and dry["upload_id"] == up["upload_id"]
    with pytest.raises(Exception, match="unknown, expired"):  # another connection cannot reach the upload at all
        call(live, "p6_import_xer", token="tok-b", **args, dry_run=False,
             confirmation_token=dry["confirmation_token"])
    done = call(live, "p6_import_xer", **args, dry_run=False, confirmation_token=dry["confirmation_token"])
    assert done["applied"] is True
    assert live["p6"].sql("SELECT COUNT(*) FROM PROJECT WHERE proj_short_name = 'DEMO-UP'")[0][0] == 1
    text = audit_text()
    assert token not in text and "Update week 48" not in text and "/upload/<link>" in text
    assert [e["what"] for e in audit.read() if e["event"] == "file"] == ["link_minted", "upload_received"]


def test_import_needs_remote_writes_and_the_write_scope(live, tmp_path):
    data = xer_bytes(live["p6"], tmp_path)
    up = call(live, files.UPLOAD_TOOL, token="tok-read")  # a read-only connection may upload and inspect
    post(live, up["upload_url"], files={"file": ("a.xer", data)})
    assert call(live, "p6_inspect_xer", token="tok-read", upload_id=up["upload_id"])["preflight"]
    with pytest.raises(Exception, match="approved read only"):
        call(live, "p6_import_xer", token="tok-read", upload_id=up["upload_id"], mode="new", new_short_name="X1")
    live["p6"].set(remote_allow_writes=False)
    up = call(live, files.UPLOAD_TOOL)
    post(live, up["upload_url"], files={"file": ("a.xer", data)})
    with pytest.raises(Exception, match="Edits from remote clients are off"):
        call(live, "p6_import_xer", upload_id=up["upload_id"], mode="new", new_short_name="X2")


def test_non_xer_and_oversize_are_refused(live, tmp_path, monkeypatch):
    up = call(live, files.UPLOAD_TOOL)
    r = post(live, up["upload_url"], files={"file": ("notes.xer", b"hello, not a schedule\r\n")})
    assert r.status_code == 400 and "not a P6 XER file" in r.text and "<form" in r.text
    r = post(live, up["upload_url"], files={"file": ("empty.xer", b"")})
    assert r.status_code == 400 and "No file was sent" in r.text
    monkeypatch.setattr(files, "UPLOAD_MAX", 1000)
    r = post(live, up["upload_url"], files={"file": ("big.xer", b"ERMHDR\t25.12\n" + b"x" * 5000)})
    assert r.status_code == 413 and "larger than" in r.text
    assert get(live, up["upload_url"]).status_code == 200  # a refused file leaves the page usable
    with pytest.raises(Exception, match="not uploaded yet"):
        call(live, "p6_inspect_xer", upload_id=up["upload_id"])
    reasons = [e.get("reason") for e in audit.read() if e.get("what") == "upload_refused"]
    assert reasons == ["not_xer", "no_file", "too_large"]
    assert not os.path.exists(os.path.join(files.root(), files._grant_dir("grant-a"), up["upload_id"]))


def test_guard_caps_the_upload_body(live, monkeypatch):
    monkeypatch.setattr(remote, "BODY_CAP", 1000)
    monkeypatch.setattr(files, "UPLOAD_BODY_CAP", 1000)
    up = call(live, files.UPLOAD_TOOL)
    hdr = {RELAY_HEADER: SECRET, "content-type": "multipart/form-data; boundary=b"}
    assert httpx.post(local(live, up["upload_url"]), content=b"x" * 2001, headers=hdr, timeout=10).status_code == 413
    r = httpx.post(local(live, up["upload_url"]), content=b"x" * 1500, headers=hdr, timeout=10)
    assert r.status_code == 413 and "larger than" in r.text  # past the guard: the route's own page
    r = httpx.post(live["base"] + "/mcp", content=b"x" * 1001, timeout=10,
                   headers={RELAY_HEADER: SECRET, "authorization": "Bearer tok-a", "content-type": "application/json"})
    assert r.status_code == 413  # every other path keeps BODY_CAP


# ----------------------------------------------------------------- switch, cleanup, MCP tool list

def test_files_switch_off(live, tmp_path):
    res = call(live, "p6_export_xer", project="DEMO-A")
    live["p6"].set(remote_allow_files=False)
    with pytest.raises(Exception, match="Allow file downloads and uploads"):
        call(live, "p6_export_xer", project="DEMO-A")
    with pytest.raises(Exception, match="Allow file downloads and uploads"):
        call(live, files.UPLOAD_TOOL)
    r = post(live, res["download_url"])
    assert r.status_code == 404 and "turned off" in r.text
    st = live["p6"].config.load()
    assert files.UPLOAD_TOOL not in remote.remote_tools(st) and "p6_export_xer" not in remote.remote_tools(st)
    remote.build_app(st, StubVerifier(), SECRET)  # a start with the switch off removes every waiting file
    assert rows(live) == [] and not os.listdir(files.root())


def test_sweep_removes_expired_rows_and_orphan_folders(live):
    h = live["handoff"]
    keep = call(live, files.UPLOAD_TOOL)
    gone = call(live, "p6_export_xer", project="DEMO-A")
    orphan = os.path.join(files.root(), files._grant_dir("grant-x"), "abcdefgh12345678")
    os.makedirs(orphan)
    open(os.path.join(orphan, "left.xer"), "w").close()
    con = sqlite3.connect(h.store.path)
    with con:
        con.execute("UPDATE files SET exp = 0 WHERE kind = 'download'")
    con.close()
    assert h.sweep() == 1
    assert [r["id"] for r in rows(live)] == [keep["upload_id"]]
    assert not os.path.exists(os.path.dirname(orphan))
    assert get(live, gone["download_url"]).status_code == 404 and get(live, keep["upload_url"]).status_code == 200
    stop = files.start_sweeper(h, every=0.05)  # the server's timer
    try:
        con = sqlite3.connect(h.store.path)
        with con:
            con.execute("UPDATE files SET exp = 0")
        con.close()
        deadline = time.monotonic() + 10
        while rows(live) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert rows(live) == []
    finally:
        stop.set()


def test_safe_names():
    assert files.safe_name("Week 48", ".xer", "x.xer") == "Week 48.xer"
    assert files.safe_name("C:\\Users\\a\\b.XER", ".xer", "x.xer") == "b.XER"
    assert files.safe_name('a<b>:"c', ".xlsx", "x.xlsx") == "a_b___c.xlsx"
    assert files.safe_name("con", ".xer", "x.xer") == "_con.xer" and files.safe_name("", ".xer", "x.xer") == "x.xer"
    assert files.safe_name("..", ".xer", "x.xer") == "x.xer" and len(files.safe_name("n" * 300, ".xer", "x")) == 100


def test_mcp_tool_list_and_export_over_mcp(live):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def go():
        hdr = {"Authorization": "Bearer tok-a", RELAY_HEADER: SECRET}
        async with httpx.AsyncClient(headers=hdr, timeout=60) as hc, \
                streamable_http_client(live["base"] + "/mcp", http_client=hc) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                init = await s.initialize()
                tools = {t.name: t for t in (await s.list_tools()).tools}
                res = await s.call_tool("p6_export_xer", {"project": "DEMO-A"})
                up = await s.call_tool(files.UPLOAD_TOOL, {})
                return init, tools, res, up
    init, tools, res, up = asyncio.run(go())
    assert set(policy.REMOTE_FILE_TOOLS) | {files.UPLOAD_TOOL} <= set(tools)
    assert "p6_remote_upload_link" in (init.instructions or "")
    def props(name):
        t = tools[name]
        return (getattr(t, "input_schema", None) or t.inputSchema)["properties"]
    assert "file_name" in props("p6_export_xer") and not {"path", "overwrite"} & set(props("p6_export_xer"))
    assert "upload_id" in props("p6_import_xer") and "path" not in props("p6_inspect_xer")
    assert "one-time" in tools["p6_export_xer"].description
    ann = tools[files.UPLOAD_TOOL].annotations
    assert getattr(ann, "destructive_hint", getattr(ann, "destructiveHint", None)) is False
    body = json.loads(res.content[0].text)
    assert body["download_url"].startswith(f"https://{HOST}/files/")
    assert post(live, body["download_url"]).content.startswith(b"ERMHDR")
    assert json.loads(up.content[0].text)["upload_url"].startswith(f"https://{HOST}/upload/")
