"""Settings window, driven offscreen with pytest-qt (dialogs replaced, Credential Manager faked)."""

import json
import os
import shutil
import socket

import pytest

pytest.importorskip("PySide6")
pytestqt = pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from p6mcp import config, i18n  # noqa: E402
from p6mcp.settings_app import pages, window  # noqa: E402
from p6mcp.settings_app.clients_page import ClientsPage  # noqa: E402
from p6mcp.settings_app.pages import DatabasePage, ServerPage, StartupPage, ToolsPage  # noqa: E402
from p6mcp.settings_app.window import SettingsWindow  # noqa: E402

TOKEN = "tok-abcdefghijklmnop-7890"


@pytest.fixture
def env(tmp_path, monkeypatch, golden):
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("P6MCP_CONFIG", str(cfg))
    for var, sub in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local"), ("USERPROFILE", "User")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))  # a fake profile: the real client configs are never read
    monkeypatch.delenv("CLINE_MCP_SETTINGS_PATH", raising=False)
    monkeypatch.setattr("p6mcp.clients._running_names", lambda: set())
    monkeypatch.setattr(pages.credentials, "http_token", lambda create=True: TOKEN)
    monkeypatch.setattr("p6mcp.settings_app.clients_page.credentials.http_token", lambda create=True: TOKEN)
    monkeypatch.setattr(pages.credentials, "regenerate_http_token", lambda: "tok-new-0000")
    answers = {"value": True}
    monkeypatch.setattr(pages, "ask", lambda *a, **k: answers["value"])
    monkeypatch.setattr("p6mcp.settings_app.clients_page.ask", lambda *a, **k: answers["value"])
    monkeypatch.setattr(SettingsWindow, "ask_close", lambda self: "discard")
    registry = {"cmd": None}  # the real HKCU Run key is never touched
    monkeypatch.setattr(window.startup, "sync", lambda data: registry.__setitem__(
        "cmd", "p6mcp --minimized" if data.get("run_at_startup") else None))
    monkeypatch.setattr(pages.startup, "current", lambda: registry["cmd"])
    warnings = []
    monkeypatch.setattr(window.QMessageBox, "warning", lambda *a, **k: warnings.append(a[2] if len(a) > 2 else ""))
    db = tmp_path / "demo.db"
    shutil.copyfile(golden, db)
    return {"cfg": cfg, "db": str(db), "answers": answers, "warnings": warnings, "registry": registry}


def make(qtbot, data=None):
    if data is not None:
        config.save(data)
    w = SettingsWindow()
    qtbot.addWidget(w)
    qtbot.waitUntil(lambda: w.page(ClientsPage).job is None, timeout=15000)  # the clients scan started at launch
    return w


@pytest.fixture
def win(qtbot, env):
    w = make(qtbot, {"language": "en"})
    yield w
    i18n.set_language("en")


def test_starts_with_no_database_and_says_so(win):
    assert win.windowTitle() == "P6 MCP Settings"
    assert win.banner.isVisibleTo(win)
    assert "No database" in win.banner.text()
    assert win.page(DatabasePage).table.rowCount() == 0 and not win.page(DatabasePage).detail.isEnabled()


def test_add_test_trust_confirm_and_save(qtbot, win, env):
    page = win.page(DatabasePage)
    page._add(env["db"])
    assert page.table.rowCount() == 1 and page.selected() == "demo"
    assert not page.use_btn.isEnabled()  # never usable before a test
    page.run_test()
    qtbot.waitUntil(lambda: "demo" in page.results, timeout=15000)
    res = page.results["demo"]
    assert res["ok"] and "25.12" in page.result.text()
    assert page.trust.isVisibleTo(page) and page.use_btn.isEnabled()
    page.trust.click()  # the dialog answers yes
    assert win.data["trusted_schemas"] == [res["schema"]["fingerprint"]]
    page.use_btn.click()
    assert win.data["active_database"] == "demo" and page.table.item(0, 2).text() == "In use"
    assert win.dirty() and win.save_btn.isEnabled()
    assert win.save()
    st = config.load()
    assert st.db_path == os.path.abspath(env["db"]) and st.trusted_schemas == (res["schema"]["fingerprint"],)
    assert not win.dirty() and not win.banner.isVisibleTo(win)


def test_new_path_needs_a_new_test(qtbot, win, env, tmp_path):
    page = win.page(DatabasePage)
    page._add(env["db"])
    page.run_test()
    qtbot.waitUntil(lambda: "demo" in page.results, timeout=15000)
    page.use_btn.click()
    other = tmp_path / "copy.db"
    shutil.copyfile(env["db"], other)
    page.path_edit.setText(str(other))
    page._repoint()
    e = win.data["databases"][0]
    assert e["confirmed"] is False and win.data["active_database"] is None and not page.use_btn.isEnabled()


def test_failed_test_cannot_be_confirmed(qtbot, win, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_text("not a database" * 100, encoding="utf-8")
    page = win.page(DatabasePage)
    page._add(str(bad))
    page.run_test()
    qtbot.waitUntil(lambda: "bad" in page.results, timeout=15000)
    assert "SQLite" in page.result.text() and not page.use_btn.isEnabled()


def test_unsaved_changes_and_revert(win):
    tools = win.page(ToolsPage)
    tools.full.click()
    assert win.data["tool_profile"] == "full" and win.dirty() and "Unsaved" in win.status.text()
    win.revert()
    assert win.data["tool_profile"] == "basic" and tools.basic.isChecked() and not win.dirty()


def test_writes_need_full_profile_and_a_confirmation(win, env):
    tools = win.page(ToolsPage)
    assert not tools.allow.isEnabled()  # Basic profile
    tools.full.click()
    assert tools.allow.isEnabled()
    env["answers"]["value"] = False
    tools.allow.click()
    assert not tools.allow.isChecked() and win.data.get("allow_writes") is False
    env["answers"]["value"] = True
    tools.allow.click()
    assert win.data["allow_writes"] is True
    assert tools.count.text().startswith(f"{tools.table.rowCount()} of")
    env["answers"]["value"] = False
    tools.backup.click()  # turning backups off while writes are allowed asks first
    assert tools.backup.isChecked() and win.data["backup_before_write"] is True
    tools.block.click()
    assert tools.block.isChecked()
    assert win.save()
    assert win.banner.isVisibleTo(win) and "tool profile, write permission" in win.banner.text()


def test_backup_folder_and_retention(win):
    tools = win.page(ToolsPage)
    tools.keep.setValue(3)
    tools.folder.setText(r"D:\Backups\P6")
    tools._folder()
    assert win.data["backup_retention"] == 3 and win.data["backup_dir"] == os.path.normpath(r"D:\Backups\P6")
    tools._default_folder()
    assert win.data["backup_dir"] is None
    tools.keep.setValue(0)
    assert tools.keep.text() == "all"


def test_server_page(win):
    srv = win.page(ServerPage)
    assert srv.stdio.isChecked() and not srv.http_box.isEnabled()
    srv.http.click()
    assert win.data["transport"] == "http" and srv.http_box.isEnabled()
    assert srv.token_edit.text().endswith("7890") and TOKEN not in srv.token_edit.text()
    srv.show_btn.click()
    assert srv.token_edit.text() == TOKEN
    srv.copy_btn.click()
    assert QGuiApplication.clipboard().text() == TOKEN
    srv.regen_btn.click()
    assert srv.token == "tok-new-0000"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        srv.port.setValue(s.getsockname()[1])
        assert srv.check_port() is False and "In use" in srv.port_state.text()
    assert win.data["http_port"] == srv.port.value()


def test_language_switch_and_rtl(win, monkeypatch):
    win.lang.setCurrentIndex(win.lang.findData("fr"))
    assert win.nav.item(0).text() == "Base de données" and win.data["language"] == "fr"
    assert win.windowTitle() == "Paramètres P6 MCP"
    assert win.layoutDirection() == Qt.LayoutDirection.LeftToRight
    monkeypatch.setattr(i18n, "RTL", {"fr"})  # stands for Arabic until its catalog exists
    win.build(0)
    assert win.layoutDirection() == Qt.LayoutDirection.RightToLeft


def test_broken_config_file(qtbot, env):
    env["cfg"].write_text("{broken", encoding="utf-8")
    w = make(qtbot)
    assert w.banner.isVisibleTo(w) and "cannot be read" in w.banner.text() and w.banner_btn.isVisibleTo(w)
    assert w.save() is False and env["warnings"]
    w.reset_broken()
    assert os.path.exists(str(env["cfg"]) + ".broken") and w.save()
    assert json.loads(env["cfg"].read_text(encoding="utf-8"))["schema_version"] == config.CONFIG_VERSION


def test_clients_page_offers_registers_and_ignores(qtbot, env, golden, tmp_path):
    os.makedirs(tmp_path / "User" / ".cursor")
    db = env["db"]
    data = {"language": "en", "databases": [{"name": "demo", "path": db, "confirmed": True}],
            "active_database": "demo"}
    w = make(qtbot, data)
    page = w.page(ClientsPage)
    states = {s.client: s.state for s in page.statuses}
    assert states["cursor"] == "not_registered" and states["vscode"] == "not_installed"
    assert w.banner.isVisibleTo(w) and "Cursor" in w.banner.text()
    w.banner_btn.click()
    assert w.nav.currentRow() == window.CLIENTS_PAGE and page.selected() == "cursor"
    assert page.register()
    qtbot.waitUntil(lambda: page.job is None, timeout=15000)
    cfg = json.loads((tmp_path / "User" / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert "p6mcp" in cfg["mcpServers"] and page.status().state == "registered"
    assert not w.banner.isVisibleTo(w)
    assert page.unregister()  # ask answers yes
    qtbot.waitUntil(lambda: page.job is None, timeout=15000)
    page.select("cursor")
    page.toggle_ignore()
    assert w.data["clients_ignored"] == ["cursor"] and not w.banner.isVisibleTo(w)


def test_clients_page_legacy_entry_and_snippet(qtbot, env, tmp_path):
    claude = tmp_path / "Roaming" / "Claude"
    claude.mkdir()
    v2 = {"mcpServers": {"primavera-p6": {"command": "python", "args": ["C:/p6-mcp/p6_mcp_server.py"]}}}
    (claude / "claude_desktop_config.json").write_text(json.dumps(v2, indent=2), encoding="utf-8")
    w = make(qtbot, {"language": "en"})
    page = w.page(ClientsPage)
    page.select("claude_desktop")
    assert page.legacy.isVisibleTo(page) and "primavera-p6" in page.legacy.text()
    assert page.remove_legacy()
    qtbot.waitUntil(lambda: page.job is None, timeout=15000)
    page.select("claude_desktop")
    assert json.loads((claude / "claude_desktop_config.json").read_text(encoding="utf-8")) == {"mcpServers": {}}
    assert not page.legacy.isVisibleTo(page)
    page.kind.setCurrentIndex(page.kind.findData("vscode"))
    assert '"servers"' in page.text.toPlainText()
    page.kind.setCurrentIndex(page.kind.findData("http"))
    assert "Bearer " + TOKEN in page.copy_snippet() and TOKEN not in page.text.toPlainText()


def test_unreadable_client_config_asks_for_attention(qtbot, env, tmp_path):
    (tmp_path / "User" / ".lmstudio").mkdir()
    (tmp_path / "User" / ".lmstudio" / "mcp.json").write_text("{oops", encoding="utf-8")
    w = make(qtbot, {"language": "en", "databases": [{"name": "d", "path": env["db"], "confirmed": True}],
                     "active_database": "d"})
    assert "LM Studio" in w.banner.text() and "attention" in w.banner.text()


def test_startup_page_syncs_on_save(win, env):
    page = win.page(StartupPage)
    assert not page.run.isChecked() and not page.minimized.isEnabled() and "does not start" in page.registry.text()
    page.run.click()
    assert win.data["run_at_startup"] is True and page.minimized.isEnabled()
    assert win.save() and env["registry"]["cmd"] == "p6mcp --minimized"
    assert "Registered" in page.registry.text()
    env["registry"]["cmd"] = None  # removed behind our back
    page.refresh()
    assert "does not match" in page.registry.text()


class FakeSupervisor:
    def __init__(self, state):
        self.state = state
        self.restarts = 0

    def restart(self):
        self.restarts += 1
        return self.state

    def tick(self):
        return self.state

    def stop(self):
        pass


def test_server_status_from_the_supervisor(qtbot, env):
    from p6mcp import supervisor
    sup = FakeSupervisor(supervisor.State(supervisor.RUNNING, 40100, 123))
    config.save({"language": "en", "transport": "http", "http_port": 40100})
    w = SettingsWindow(supervisor=sup)
    qtbot.addWidget(w)
    qtbot.waitUntil(lambda: w.page(ClientsPage).job is None, timeout=15000)
    srv = w.page(ServerPage)
    w.server_state(sup.state)
    assert "Running on http://127.0.0.1:40100/mcp" in srv.status.text() and srv.restart_btn.isEnabled()
    srv.restart_btn.click()
    assert sup.restarts == 1
    w.server_state(supervisor.State(supervisor.ERROR, 40100, None, "Port 40100 is already in use"))
    assert "already in use" in srv.status.text()


def test_server_status_without_tray(win):
    srv = win.page(ServerPage)
    srv.http.click()
    assert "Not running" in srv.status.text() and not srv.restart_btn.isEnabled()


def test_close_hides_when_the_tray_keeps_running(qtbot, env):
    config.save({"language": "en"})
    calls = []
    w = SettingsWindow(on_close=lambda: calls.append(1) or True)
    qtbot.addWidget(w)
    qtbot.waitUntil(lambda: w.page(ClientsPage).job is None, timeout=15000)
    w.show()
    w.close()
    assert calls == [1] and not w.isVisible()


def test_tray_app(qtbot, env):
    from p6mcp import supervisor
    from p6mcp.settings_app import tray as tray_mod
    from PySide6.QtWidgets import QApplication
    config.save({"language": "en", "transport": "http"})
    sup = FakeSupervisor(supervisor.State(supervisor.STARTING, 40200, 1))
    app = tray_mod.TrayApp(QApplication.instance(), sup)
    try:
        assert app.status_text(sup.state) == "HTTP server starting on port 40200"
        assert app.status_text(supervisor.State(supervisor.DISABLED)).startswith("stdio")
        app.show_window()
        qtbot.addWidget(app.window)
        qtbot.waitUntil(lambda: app.window.page(ClientsPage).job is None, timeout=15000)
        sup.state = supervisor.State(supervisor.RUNNING, 40200, 1)
        app.tick()
        assert "Running" in app.window.page(ServerPage).status.text()
        assert tray_mod.signal_running_instance()  # a second start would only show this window
        assert not tray_mod.make_icon(supervisor.RUNNING).isNull()
    finally:
        app.timer.stop()
        app.server.close()
        if app.tray is not None:
            app.tray.hide()


def test_xer_only_mode_in_settings(qtbot, win, env, p6, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    xer = str(tmp_path / "demo.xer")
    p6.call("p6_export_xer", project="DEMO-A", path=xer)
    page = win.page(DatabasePage)
    assert not page.xer.isVisibleTo(page) and page.profiles_box.isVisibleTo(page)
    page.src_xer.click()
    assert win.data["data_source"] == "xer" and page.xer.isVisibleTo(page) and not page.profiles_box.isVisibleTo(page)
    assert "XER-only mode" in win.banner.text()
    page.xer.add_files([xer, xer])  # a duplicate is ignored
    assert win.data["xer_files"] == [os.path.normpath(xer)] and win.data["xer_confirmed"] is False
    page.xer.load()
    qtbot.waitUntil(lambda: page.xer.job is None, timeout=30000)
    assert win.data["xer_confirmed"] is True and "DEMO-A" in page.xer.details.text()
    assert not tools_allow_enabled(win)
    assert win.save()
    st = config.load()
    assert st.xer_files == (os.path.normpath(xer),) and st.allow_writes is False
    page.xer.add_files([str(tmp_path / "other.xer")])
    assert win.data["xer_confirmed"] is False  # a changed list needs a new load check


def tools_allow_enabled(w):
    tools = w.page(ToolsPage)
    tools.full.click()
    return tools.allow.isEnabled()
