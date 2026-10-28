"""Automatic updates: verified download, servers started by MCP clients, the silent install command,
the config switch, the tray's Updater state machine and the Update button (network, processes and the
installer are all faked)."""

import hashlib
import io
import os
import sys

import pytest

from p6mcp import config, updates

BASE = updates.DOWNLOAD_PREFIX + "v9.9.9/"
EXE = updates.installer_name("9.9.9")
PAYLOAD = b"MZ fake installer bytes" * 100


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def release(payload=PAYLOAD, sums_for=None):
    digest = hashlib.sha256(sums_for if sums_for is not None else payload).hexdigest()
    files = {BASE + EXE: payload, BASE + updates.SUMS: f"{digest}  {EXE}\nabc  other.zip\n".encode()}
    seen = []

    def opener(req, timeout):
        seen.append(req.full_url)
        return Resp(files[req.full_url])
    info = {"latest": "9.9.9", "newer": True, "assets": {EXE: BASE + EXE, updates.SUMS: BASE + updates.SUMS}}
    return info, opener, seen


def test_check_lists_the_assets():
    payload = {"tag_name": "v9.9.9", "html_url": "u", "assets": [{"name": EXE, "browser_download_url": BASE + EXE}]}
    import json
    r = updates.check("1.0.0", lambda req, timeout: Resp(json.dumps(payload).encode()))
    assert r["newer"] and r["assets"] == {EXE: BASE + EXE}


def test_download_verifies_the_sha256(tmp_path):
    info, opener, seen = release()
    path = updates.download(info, str(tmp_path), opener)
    assert open(path, "rb").read() == PAYLOAD and os.path.basename(path) == EXE
    n = len(seen)
    assert updates.download(info, str(tmp_path), opener) == path and len(seen) == n + 1  # reused: only the sums


def test_download_refuses_a_wrong_hash(tmp_path):
    info, opener, _ = release(sums_for=b"something else")
    with pytest.raises(ValueError, match="does not match"):
        updates.download(info, str(tmp_path), opener)
    assert not os.path.exists(tmp_path / EXE) and not os.path.exists(tmp_path / (EXE + ".part"))


def test_download_only_from_this_repository(tmp_path):
    info, opener, _ = release()
    info["assets"][EXE] = "https://example.test/evil.exe"
    with pytest.raises(ValueError, match="refusing to download"):
        updates.download(info, str(tmp_path), opener)
    with pytest.raises(ValueError, match="no newer"):
        updates.download({"latest": "1.0.0", "newer": False}, str(tmp_path), opener)


def test_parse_sums():
    h = "a" * 64
    assert updates.parse_sums(f"{h}  P6MCP-Setup-1.0.0.exe\n{h.upper()} *x.zip\nnoise") == {
        "P6MCP-Setup-1.0.0.exe": h, "x.zip": h}


def test_client_servers_skip_the_tray_own_http_server():
    procs = [(100, 1, "p6mcp-settings.exe"), (150, 100, "p6mcp-server.exe"), (151, 150, "p6mcp-server.exe"),
             (300, 1, "claude.exe"), (301, 300, "P6MCP-SERVER.EXE"), (400, 1, "Code.exe"), (401, 400, "node.exe")]
    assert updates.client_servers(procs, me=100) == [{"pid": 301, "client": "claude.exe"}]


def test_install_command_and_launch(tmp_path):
    exe = tmp_path / EXE
    exe.write_bytes(b"x")
    cmd = updates.install_command(str(exe), str(tmp_path / "setup.log"))
    assert cmd.startswith('cmd.exe /d /s /c "ping -n 4 127.0.0.1 >nul & "') and "/AUTOUPDATE=1" in cmd
    assert "/VERYSILENT" in cmd and f'"{exe}"' in cmd
    calls = []
    log = updates.launch_install(str(exe), popen=lambda c, **k: calls.append((c, k)))
    assert calls and calls[0][0] == updates.install_command(str(exe), log)
    with pytest.raises(FileNotFoundError):
        updates.launch_install(str(tmp_path / "missing.exe"), popen=lambda c, **k: None)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows")
def test_only_an_installed_copy_updates_itself(tmp_path):
    exe = tmp_path / "p6mcp-settings.exe"
    exe.write_bytes(b"")
    assert not updates.can_self_update(str(exe))
    (tmp_path / "unins000.exe").write_bytes(b"")
    assert updates.can_self_update(str(exe))
    assert not updates.can_self_update()  # tests run from source, not frozen


def test_config_auto_update_default_and_legacy_switch():
    assert config.validate({})["auto_update"] is True
    assert config.validate({"update_check": False})["auto_update"] is False  # who turned the old check off
    assert config.validate({"update_check": False, "auto_update": True})["auto_update"] is True
    with pytest.raises(config.ConfigError):
        config.validate({"auto_update": "yes"})


# ----------------------------------------------------------------- the tray's Updater

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from p6mcp.settings_app import updater as U  # noqa: E402
from test_settings_app import env  # noqa: E402,F401  (fixture: fake profile and config)


@pytest.fixture
def fake(monkeypatch):
    st = {"clients": [], "installed": [], "stopped": [], "quit": [], "newer": True, "skill_runs": 0,
          "skill_result": {"updated": [], "edited": [], "failed": [], "error": None}}
    monkeypatch.setattr(U.updates, "check", lambda *a, **k: {"current": "1.0.0", "latest": "9.9.9",
                                                             "newer": st["newer"], "url": "u", "assets": {},
                                                             "error": None})
    # the skill update of the same schedule (p6mcp.core_skills.run_updates, tested in test_shop)
    monkeypatch.setattr(U.core_skills, "run_updates",
                        lambda: st.update(skill_runs=st["skill_runs"] + 1) or st["skill_result"])
    monkeypatch.setattr(U.updates, "download", lambda info: "C:/x/" + EXE)
    monkeypatch.setattr(U.updates, "client_servers", lambda: list(st["clients"]))
    monkeypatch.setattr(U.updates, "launch_install", lambda p: st["installed"].append(p) or "log")
    monkeypatch.setattr(U.updates, "stop_servers", lambda pids: st["stopped"].extend(pids) or st.update(clients=[]))
    return st


def make_updater(fake, can_install=True, auto=True):
    return U.Updater(lambda v: fake["quit"].append(v), can_install=can_install, auto=lambda: auto)


def test_auto_update_installs_when_free(qtbot, fake):
    u = make_updater(fake)
    u._periodic()
    qtbot.waitUntil(lambda: u.state == U.INSTALLING, timeout=5000)
    assert fake["installed"] == ["C:/x/" + EXE] and fake["quit"] == ["9.9.9"]


def test_auto_update_waits_for_the_clients(qtbot, fake):
    fake["clients"] = [{"pid": 301, "client": "claude.exe"}]
    u = make_updater(fake)
    u._periodic()
    qtbot.waitUntil(lambda: u.state == U.WAITING, timeout=5000)
    assert not fake["installed"] and u.wait_timer.isActive() and not fake["stopped"]
    fake["clients"] = []
    u._try_install()  # what the wait timer does
    assert u.state == U.INSTALLING and fake["installed"] and not u.wait_timer.isActive()


def test_auto_off_never_checks(qtbot, fake):
    u = make_updater(fake, auto=False)
    u._periodic()
    assert u.state == U.IDLE and u.job is None and u.skills_job is None and fake["skill_runs"] == 0


def test_auto_update_also_updates_the_shop_skills(qtbot, fake):
    fake["newer"] = False
    fake["skill_result"] = {"updated": ["float-watch"], "edited": ["mine"], "failed": [], "error": None}
    u = make_updater(fake)
    got = []
    u.skills_changed.connect(got.append)
    u._periodic()
    qtbot.waitUntil(lambda: u.skills_job is None and u.state == U.LATEST, timeout=5000)
    assert fake["skill_runs"] == 1 and got == [["float-watch"]] and u.skills_report["edited"] == ["mine"]
    fake["skill_result"] = {"updated": [], "edited": [], "failed": [], "error": None}
    u._periodic()  # nothing newer: no signal, the window is not reloaded
    qtbot.waitUntil(lambda: u.skills_job is None and fake["skill_runs"] == 2, timeout=5000)
    assert got == [["float-watch"]]


def test_latest_and_portable(qtbot, fake):
    fake["newer"] = False
    u = make_updater(fake)
    u._periodic()
    qtbot.waitUntil(lambda: u.state == U.LATEST, timeout=5000)
    fake["newer"] = True
    p = make_updater(fake, can_install=False)
    p._periodic()
    qtbot.waitUntil(lambda: p.state == U.MANUAL, timeout=5000)
    assert not fake["installed"]


@pytest.mark.parametrize("choice, state, stopped", [(True, U.INSTALLING, [301]), (False, U.WAITING, []),
                                                    (None, U.IDLE, [])])
def test_update_button_asks_about_the_clients(qtbot, fake, choice, state, stopped):
    fake["clients"] = [{"pid": 301, "client": "claude.exe"}]
    u = make_updater(fake)
    u.check()
    qtbot.waitUntil(lambda: u.state == U.IDLE and u.newer, timeout=5000)  # found, not installed (manual check)
    asked = []
    u.update_now(lambda c: asked.append(c) or choice)
    qtbot.waitUntil(lambda: u.state == state and bool(asked), timeout=5000)
    assert fake["stopped"] == stopped and bool(fake["installed"]) == (state == U.INSTALLING)


def test_settings_window_update_button(qtbot, env, fake):  # noqa: F811  (fake after env: its check wins)
    from p6mcp.settings_app.about_pages import LogsPage
    from p6mcp.settings_app.window import SettingsWindow
    fake["clients"] = [{"pid": 301, "client": "claude.exe"}]
    config.save({"language": "en"})
    u = make_updater(fake)
    win = SettingsWindow(updater=u)
    qtbot.addWidget(win)
    logs = win.page(LogsPage)
    assert logs.auto.isChecked() and not logs.update_btn.isVisibleTo(logs)
    logs.check()
    qtbot.waitUntil(lambda: u.newer and u.state == U.IDLE, timeout=5000)
    assert logs.update_btn.isVisibleTo(logs) and "9.9.9 is available" in logs.state.text()
    logs._ask_clients = lambda clients: False  # "Update when they close"
    logs.update_btn.click()
    qtbot.waitUntil(lambda: u.state == U.WAITING, timeout=5000)
    assert "claude" in logs.state.text() and not fake["installed"]


# -- a program started while setup runs exits at once (it would lock files setup is replacing)

@pytest.mark.skipif(sys.platform != "win32", reason="Windows")
def test_setup_running_sees_the_setup_mutex():
    import ctypes
    from p6mcp import runtime
    name = f"P6MCP-Setup-test-{os.getpid()}"
    assert not runtime.setup_running(name)
    h = ctypes.windll.kernel32.CreateMutexW(None, False, name)
    try:
        assert runtime.setup_running(name)
    finally:
        ctypes.windll.kernel32.CloseHandle(h)
    assert not runtime.setup_running(name)


def test_settings_app_exits_during_setup_and_opens_after(monkeypatch, tmp_path):
    from p6mcp import runtime
    from p6mcp.settings_app.__main__ import main
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(runtime, "setup_running", lambda name=runtime.SETUP_MUTEX: True)
    assert main(["p6mcp-settings", "--minimized"]) == 0 and not runtime.take_window_request()
    assert main(["p6mcp-settings"]) == 0  # a click during setup: the window opens after it
    assert runtime.take_window_request() and not runtime.take_window_request()


def test_server_refuses_to_start_during_setup(monkeypatch, capsys):
    import p6mcp.__main__ as entry
    from p6mcp import runtime
    monkeypatch.setattr(runtime, "setup_running", lambda name=runtime.SETUP_MUTEX: True)
    for argv in (["p6mcp-server"], ["p6mcp-server", "--http"]):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as e:
            entry.main()
        assert e.value.code == 1 and "being updated" in capsys.readouterr().err
    monkeypatch.setattr("p6mcp.setup_cli.main", lambda args: 7)  # setup runs these commands itself
    monkeypatch.setattr(sys, "argv", ["p6mcp-server", "setup", "detected-clients"])
    with pytest.raises(SystemExit) as e:
        entry.main()
    assert e.value.code == 7
