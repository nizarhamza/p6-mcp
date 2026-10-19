"""Run at startup, in a test registry key (never the real Run key)."""

import sys

import pytest

from p6mcp import startup

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
KEY = r"Software\P6MCP-tests\Run"


@pytest.fixture
def key():
    yield KEY
    import winreg
    for sub in (KEY, r"Software\P6MCP-tests"):
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


def test_command(monkeypatch):
    monkeypatch.setattr(startup.sys, "executable", r"C:\py\python.exe")
    assert startup.command() == r'"C:\py\pythonw.exe" -m p6mcp.settings_app --minimized'
    monkeypatch.setattr(startup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(startup.sys, "executable", r"C:\Program Files\P6 MCP\p6mcp-settings.exe")
    assert startup.command(False) == r'"C:\Program Files\P6 MCP\p6mcp-settings.exe"'
