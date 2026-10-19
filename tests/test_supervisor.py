"""Supervisor of the HTTP server process: real child processes, a fake clock and fake settings."""

import os
import socket
import sys
import time
import uuid

import pytest

from p6mcp import config, runtime, supervisor

SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]
CRASH = [sys.executable, "-c", "raise SystemExit(4)"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def settings(**kw):
    base = {"source": "config", "transport": "http", "http_port": 40001}
    base.update(kw)
    return config.Settings(**base)


@pytest.fixture(autouse=True)
def localdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))


def make(cmd, clock=None, **kw):
    box = {"st": settings(**kw)}
    sup = supervisor.Supervisor(settings=lambda: box["st"], command=lambda port: list(cmd), clock=clock or Clock())
    return sup, box


def test_stdio_means_nothing_to_host():
    sup, _ = make(SLEEP, transport="stdio")
    assert sup.tick().state == supervisor.DISABLED and sup.proc is None


def test_start_port_change_and_stop():
    sup, box = make(SLEEP)
    try:
        st = sup.tick()
        assert st.state == supervisor.STARTING and st.port == 40001 and sup.proc.poll() is None
        first = sup.proc.pid
        box["st"] = settings(http_port=40002)
        st = sup.tick()
        assert st.port == 40002 and sup.proc.pid != first and not runtime.pid_alive(first)
        box["st"] = settings(transport="stdio")
        assert sup.tick().state == supervisor.DISABLED and sup.proc is None
    finally:
        sup.stop()


def test_crash_backoff_and_give_up():
    clock = Clock()
    sup, _ = make(CRASH, clock)
    sup.tick()
    for _ in range(supervisor.MAX_FAILS):
        sup.proc.wait(10)
        st = sup.tick()  # sees the exit, waits for the backoff delay
        assert st.state == supervisor.ERROR
        if sup.given_up:
            break
        assert sup.proc is None
        clock.t += 60
        sup.tick()  # restarted after the delay
    assert sup.given_up and "exit code 4" in sup.state.error and "stopped after" in sup.state.error
    clock.t += 600
    assert sup.tick().state == supervisor.ERROR and sup.proc is None  # stays stopped
    sup.command = lambda port: list(SLEEP)
    try:
        assert sup.restart().state == supervisor.STARTING  # an explicit restart tries again
    finally:
        sup.stop()


def test_real_http_server_process(tmp_path, monkeypatch, golden):
    """The real p6mcp-server --http child, supervised until it serves."""
    import shutil
    db = str(tmp_path / "demo.db")
    shutil.copyfile(golden, db)
    cfg = tmp_path / "config.json"
    from p6mcp import credentials, settings_logic
    port = settings_logic.free_port(42100)
    config.save({"databases": [{"name": "d", "path": db, "confirmed": True}], "active_database": "d",
                 "transport": "http", "http_port": port}, str(cfg))
    monkeypatch.setenv("P6MCP_CONFIG", str(cfg))
    monkeypatch.setenv("P6MCP_SECRET_SERVICE", f"P6MCP-test-{uuid.uuid4().hex[:8]}")
    try:
        credentials.get("probe")
    except credentials.SecretStoreError as e:
        pytest.skip(f"no credential store: {e}")
    sup = supervisor.Supervisor()
    try:
        deadline = time.monotonic() + 60
        while sup.tick().state != supervisor.RUNNING:
            assert time.monotonic() < deadline, (sup.state, runtime.read_status())
            time.sleep(0.5)
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        st = runtime.read_status()
        assert sup.proc.pid in (st["pid"], st["ppid"])  # a venv launcher may sit between the two
    finally:
        sup.stop()
        try:
            credentials.delete(credentials.HTTP_TOKEN)
        except credentials.SecretStoreError:
            pass
    assert not runtime.port_open(port)


def test_server_command():
    cmd = supervisor.server_command(40000)
    assert cmd[-3:] == ["--http", "--port", "40000"] and os.path.basename(cmd[0]).lower().startswith("python")
