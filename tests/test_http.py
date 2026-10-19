"""Local HTTP transport: a real uvicorn server on 127.0.0.1 in a thread, on the synthetic database."""

import asyncio
import json
import socket
import threading
import time

import pytest

from p6mcp import http_server, runtime, settings_logic

httpx = pytest.importorskip("httpx2")
TOKEN = "test-token-0123456789abcdef"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
HDR = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture
def live(p6):
    port = settings_logic.free_port(41500)
    srv = http_server.make_server(port, TOKEN)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.monotonic() + 20
    while not srv.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    yield {"port": port, "url": f"http://127.0.0.1:{port}/mcp", "p6": p6, "srv": srv}
    srv.should_exit = True
    th.join(10)


def post(url, headers):
    return httpx.post(url, json=INIT, headers={**HDR, **headers}, timeout=10)


def test_token_is_required(live):
    u = live["url"]
    assert post(u, {}).status_code == 401
    assert post(u, {"authorization": "Bearer wrong"}).status_code == 401
    assert post(u, {"authorization": f"Basic {TOKEN}"}).status_code == 401
    r = post(u, {"authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and "serverInfo" in r.text


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1", "127.0.0.1:1", "0.0.0.0:{port}", "[::1]:{port}"])
def test_foreign_host_header_is_refused(live, host):
    r = post(live["url"], {"authorization": f"Bearer {TOKEN}", "host": host.format(port=live["port"])})
    assert r.status_code == 421


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "https://127.0.0.1:{port}", "http://127.0.0.1:1"])
def test_foreign_origin_is_refused(live, origin):
    r = post(live["url"], {"authorization": f"Bearer {TOKEN}", "origin": origin.format(port=live["port"])})
    assert r.status_code == 403


def test_local_origin_and_localhost_are_accepted(live):
    p = live["port"]
    assert post(live["url"], {"authorization": f"Bearer {TOKEN}", "origin": f"http://localhost:{p}"}).status_code == 200
    r = post(f"http://localhost:{p}/mcp", {"authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_other_paths_need_the_token_too(live):
    base = f"http://127.0.0.1:{live['port']}"
    assert httpx.get(base + "/", timeout=5).status_code == 401
    assert httpx.get(base + "/", headers={"authorization": f"Bearer {TOKEN}"}, timeout=5).status_code == 404


def test_bound_to_loopback_only(live):
    assert live["srv"].config.host == "127.0.0.1"
    try:
        lan = socket.gethostbyname(socket.gethostname())
    except OSError:
        pytest.skip("no network address")
    if lan.startswith("127."):
        pytest.skip("no non-loopback address")
    with pytest.raises(OSError):
        socket.create_connection((lan, live["port"]), timeout=1).close()


def _body(res):
    assert not res.is_error, res.content[0].text
    if res.structured_content is not None:
        return res.structured_content.get("result", res.structured_content)
    return json.loads(res.content[0].text)


def test_two_step_write_over_http(live):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def go():
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30) as hc, \
                streamable_http_client(live["url"], http_client=hc) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                args = {"project": "DEMO-A", "task_code": "A1020", "remaining_duration_days": 12}
                dry = await s.call_tool("p6_update_activity", args)
                body = _body(dry)
                done = await s.call_tool("p6_update_activity", dict(args, dry_run=False,
                                                                   confirmation_token=body["confirmation_token"]))
                return names, body, _body(done)
    names, dry, done = asyncio.run(go())
    assert "p6_update_activity" in names and dry["dry_run"] and done["applied"] is True
    assert live["p6"].sql("SELECT remain_drtn_hr_cnt FROM TASK WHERE task_code = 'A1020'")[0][0] == 96


def test_guard_needs_a_token():
    with pytest.raises(ValueError):
        http_server.Guard(lambda *a: None, 1234, "")


def test_serve_refuses_a_busy_port(p6, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(http_server.credentials, "http_token", lambda create=True: TOKEN)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert http_server.serve(port) == 3
    st = json.loads((tmp_path / "P6MCP" / "http_status.json").read_text(encoding="utf-8"))
    assert st["state"] == "error" and "already in use" in st["error"]


def test_status_file_and_liveness(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert runtime.read_status()["state"] == "stopped"
    runtime.write_status("running", 40000)
    assert runtime.read_status()["state"] == "running" and runtime.pid_alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not runtime.pid_alive(p.pid)
    data = json.loads(open(runtime.status_path(), encoding="utf-8").read())
    data["pid"] = p.pid
    open(runtime.status_path(), "w", encoding="utf-8").write(json.dumps(data))
    st = runtime.read_status()
    assert st["state"] == "stopped" and "ended" in st["error"]
