"""Run at startup, in a test registry key (never the real Run key)."""

import os
import sys
import uuid

import pytest

from p6mcp import startup

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
# one key per process: pytest-xdist workers and other test runs share HKCU
PARENT = r"Software\P6MCP-tests"
OWN = rf"{PARENT}\{os.getpid()}-{uuid.uuid4().hex[:8]}"
KEY = rf"{OWN}\Run"


@pytest.fixture
def key():
    yield KEY
    import winreg
    for sub in (KEY, OWN, PARENT):  # the shared parent goes only when no other run still has a key in it
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            pass


def test_enable_disable(key):
    assert startup.current(key) is None
    cmd = startup.enable(True, key)
    assert startup.current(key) == cmd and cmd.endswith(" --minimized")
    assert startup.disable(key) is True and startup.current(key) is None
    assert startup.disable(key) is False


def test_sync(key):
    assert startup.sync({"run_at_startup": True, "start_minimized": False}, key) == startup.command(False)
    assert startup.current(key) == startup.command(False) and "--minimized" not in startup.current(key)
    startup.sync({"run_at_startup": True, "start_minimized": True}, key)
    assert startup.current(key).endswith("--minimized")
    assert startup.sync({"run_at_startup": False}, key) is None and startup.current(key) is None


def test_heal(key):
    on = {"run_at_startup": True, "start_minimized": True}
    assert startup.heal({"run_at_startup": False}, key) is None and startup.current(key) is None
    assert startup.heal(on, key) == startup.command(True) and startup.current(key) == startup.command(True)
    assert startup.heal(on, key) is None  # already right: nothing written
    startup.enable(False, key)  # another command (a moved install, no --minimized)
    assert startup.heal(on, key) == startup.command(True)
    assert startup.heal({"run_at_startup": False}, key) is None and startup.current(key)  # never removes


def test_heal_only_in_the_installed_app(monkeypatch):
    from p6mcp import config
    from p6mcp.settings_app import __main__ as entry
    calls = []
    monkeypatch.setattr(startup, "heal", lambda data, key=startup.RUN_KEY: calls.append(data))
    monkeypatch.setattr(config, "read_file", lambda *a, **k: ({"run_at_startup": True}, None))
    monkeypatch.delattr(entry.sys, "frozen", raising=False)
    entry._heal_startup()
    assert calls == []  # a run from source never registers its Python
    monkeypatch.setattr(entry.sys, "frozen", True, raising=False)
    entry._heal_startup()
    assert calls == [{"run_at_startup": True}]


def test_command(monkeypatch):
    monkeypatch.setattr(startup.sys, "executable", r"C:\py\python.exe")
    assert startup.command() == r'"C:\py\pythonw.exe" -m p6mcp.settings_app --minimized'
    monkeypatch.setattr(startup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(startup.sys, "executable", r"C:\Program Files\P6 MCP\p6mcp-settings.exe")
    assert startup.command(False) == r'"C:\Program Files\P6 MCP\p6mcp-settings.exe"'
