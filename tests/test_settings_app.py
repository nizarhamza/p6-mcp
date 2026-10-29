"""Settings window, driven offscreen with pytest-qt (dialogs replaced, Credential Manager faked)."""

import copy
import json
import os
import shutil
import socket

import pytest

pytest.importorskip("PySide6")
pytestqt = pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QLabel as QLabelType  # noqa: E402

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
    # config.json written by another program (the p6 helper, the shop install tool): overwrite unless a test says
    monkeypatch.setattr(SettingsWindow, "ask_changed", lambda self: answers.get("changed", "overwrite"))
    monkeypatch.setattr("p6mcp.shop.fetch_index", lambda *a, **k: {"skills": [], "error": "offline (test)", "url": ""})
    monkeypatch.setattr("p6mcp.updates.check", lambda *a, **k: {"current": "1.0.0", "latest": "1.0.0",
                                                                  "newer": False, "url": "x", "error": None})
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
    assert win.windowTitle() == "MCP for Primavera P6 Settings"
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
    assert win.windowTitle() == "Paramètres de MCP for Primavera P6"
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


def test_clients_page_installs_skills(qtbot, env, tmp_path):
    os.makedirs(tmp_path / "User" / ".cursor")
    os.makedirs(tmp_path / "Roaming" / "Claude")
    full_only = [{"name": n, "title": n, "description": f"{n} check. Use when asked.", "args": [],
                  "template": f"Run {t}.", "enabled": True}
                 for n, t in (("variance-check", "p6_compare_projects"), ("driving-check", "p6_driving_path"),
                              ("import-check", "p6_import_xer"))]
    w = make(qtbot, {"language": "en", "tool_profile": "basic", "core_skills_installed": True,
                     "custom_skills": full_only})
    page = w.page(ClientsPage)
    page.select("cursor")
    assert not page.skill_btn.isHidden() and not page.skill_btn.isEnabled()  # Basic profile: no skill offered
    assert "Full tool profile" in page.skill_state.text()
    config.save(dict(config.read_file()[0], tool_profile="full", allow_writes=False))
    page._show_selected()
    assert page.skill_btn.isEnabled() and page.skill_btn.text() == "Install skills"
    assert page.install_skills()
    qtbot.waitUntil(lambda: page.job is None, timeout=15000)
    folder = tmp_path / "User" / ".claude" / "skills"  # Cursor reads Claude Code's skills folder
    assert sorted(os.listdir(folder)) == ["p6-driving-check", "p6-variance-check"]  # writes off: no import skill
    page.select("cursor")
    assert page.status().skills == "installed" and not page.skill_btn.isEnabled()
    assert "p6-variance-check" in page.skill_state.text()
    assert page.remove_skills()  # ask answers yes
    qtbot.waitUntil(lambda: page.job is None, timeout=15000)
    page.select("cursor")
    assert os.listdir(folder) == [] and page.status().skills == "none"
    page.select("claude_desktop")
    assert page.skill_btn.isHidden() and "nothing to install" in page.skill_state.text()


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
        config.save(dict(config.read_file()[0], p6_user="SKILLS"))  # the automatic skill update saved the file
        app.updater.skills_changed.emit(["float-watch"])
        assert app.window.data["p6_user"] == "SKILLS" and not app.window.dirty()  # reloaded: no unsaved change
        app.window.set_data(dict(app.window.data, p6_user="TYPED"))
        config.save(dict(config.read_file()[0], p6_user="AGAIN"))
        app.updater.skills_changed.emit(["float-watch"])
        assert app.window.data["p6_user"] == "TYPED"  # unsaved changes are kept (Save asks overwrite or reload)
        app.window.set_data(dict(app.window.data, p6_user="AGAIN"))
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


def test_logs_and_about_pages(qtbot, win, monkeypatch):
    from p6mcp.settings_app.about_pages import AboutPage, LogsPage
    logs = win.page(LogsPage)
    logs.level.setCurrentIndex(logs.level.findData("DEBUG"))
    assert win.data["log_level"] == "DEBUG"
    logs.auto.click()
    assert win.data["auto_update"] is False
    logs.check()
    qtbot.waitUntil(lambda: logs.job is None, timeout=10000)
    assert "latest version" in logs.state.text() and not logs.open_btn.isVisibleTo(logs)
    monkeypatch.setattr("p6mcp.updates.check", lambda *a, **k: {"current": "1.0.0", "latest": "1.1.0",
                                                                  "newer": True, "url": "u", "error": None})
    logs.check()
    qtbot.waitUntil(lambda: logs.job is None, timeout=10000)
    assert "1.1.0 is available" in logs.state.text() and logs.open_btn.isVisibleTo(logs)
    assert "1.1.0" in win.banner.text() or "No database" in win.banner.text()
    about = win.page(AboutPage)
    texts = " ".join(w.text() for w in about.findChildren(QLabelType))
    assert "Apache" in texts and "Oracle" in texts and "local model" in texts


def test_instructions_and_skills_pages(qtbot, win, env, monkeypatch):
    from p6mcp.settings_app import guidance_pages
    from p6mcp.settings_app.guidance_pages import InstructionsPage, SkillDialog, SkillsPage
    monkeypatch.setattr(guidance_pages, "ask", lambda *a, **k: True)
    act = "When you show an activity, write it as Activity ID - Activity name."
    ins = win.page(InstructionsPage)
    assert ins.builtin.count() == len(guidance_pages.instructions.BUILTIN)
    monkeypatch.setattr(ins, "ask_text", lambda title, text="": act)
    assert ins.add() and ins.custom.count() == 1 and ins.preview.toPlainText().endswith(act)
    sql = next(i for i in range(ins.builtin.count()) if ins.builtin.item(i).data(Qt.ItemDataRole.UserRole) == "sql")
    ins.builtin.item(sql).setCheckState(Qt.CheckState.Unchecked)
    assert win.data["instructions_disabled"] == ["sql"] and "p6_sql_select" not in ins.preview.toPlainText()
    assert win.save_btn.isEnabled() and win.save()
    saved = json.loads(env["cfg"].read_text(encoding="utf-8"))
    assert saved["custom_instructions"] == [{"text": act, "enabled": True}]
    assert "your instructions" in win.banner.text()  # restart the client to read them

    cmds = win.page(SkillsPage)
    assert cmds.view.items == [] and cmds.view.empty_text.text().startswith("No skill yet")  # the config's only
    dlg = SkillDialog(cmds, win.data)
    qtbot.addWidget(dlg)
    dlg.name.setText("my-check")
    dlg.desc.setText("Checks a project. Use when asked to check one.")
    monkeypatch.setattr(dlg, "edit_file", lambda f: {"path": "ref/limits.md", "text": "Float under 5 days."})
    dlg.add_file()
    assert dlg.file_list.count() == 1
    dlg.text.setPlainText("Check {project} with p6_update_activity(project=\"{project}\").")
    dlg._add_arg({"name": "project", "description": "Project ID", "default": None})
    dlg.accept()
    assert dlg.result_data["custom_skills"][0]["args"] == [{"name": "project", "description": "Project ID",
                                                              "default": None}]
    assert dlg.result_data["custom_skills"][0]["files"] == [{"path": "ref/limits.md", "text": "Float under 5 days."}]
    monkeypatch.setattr(cmds, "edit_skill", lambda item, old: dlg.result_data)
    assert cmds.add() and len(cmds.view.items) == 1 and cmds.current()["name"] == "my-check"
    card = cmds.view.item("my-check")
    assert card.offered.text() == "Offered: No: needs p6_update_activity"  # Basic profile
    assert card.source.text() == "Yours" and card.ident.text() == "p6-my-check"
    shown = []
    monkeypatch.setattr(cmds, "show_dialog", shown.append)
    md = cmds.details().md.toPlainText()  # the SKILL.md written for the clients, now in the details
    assert "Check <project> with" in md and "- ref/limits.md" in md
    card.check.setChecked(False)  # the On checkbox: applied once the click is handled
    qtbot.waitUntil(lambda: win.data["custom_skills"][0]["enabled"] is False, timeout=5000)
    assert win.data["skills_disabled"] == [] and win.dirty()
    assert cmds.view.item("my-check").offered.text() == "Offered: No (turned off)"
    assert cmds.remove() and cmds.view.items == []


def test_skill_sharing_files_and_shop_page(qtbot, win, env, monkeypatch, tmp_path):
    from p6mcp import settings_logic, shop
    from p6mcp.settings_app import guidance_pages, shop_page
    from p6mcp.settings_app.guidance_pages import SkillsPage
    from p6mcp.settings_app.shop_page import ShopPage
    monkeypatch.setattr(guidance_pages, "ask", lambda *a, **k: True)
    monkeypatch.setattr(shop_page, "ask", lambda *a, **k: True)
    mine = {"name": "float-watch", "title": "Float watch", "description": "Low float. Use when asked.", "args": [],
            "template": "Run p6_float_analysis.", "files": [{"path": "limits.md", "text": "Under 10 days."}]}
    win.set_data(settings_logic.put_skill(win.data, mine))
    page = win.page(SkillsPage)
    page.refresh("float-watch")
    opened = []
    monkeypatch.setattr(page, "open_url", opened.append)
    assert page.allowed(page.current())["share"][0] and page.share()
    assert opened[0] == shop.REPO_URL + "/issues/new?template=share-skill.yml&title=Share%3A+float-watch"
    assert json.loads(QGuiApplication.clipboard().text())["name"] == "float-watch"  # pasted in the form
    path = str(tmp_path / "p6-float-watch.zip")
    monkeypatch.setattr(page, "save_path", lambda name: path)
    assert page.export() == path and os.path.exists(path)
    monkeypatch.setattr(page, "open_path", lambda: path)
    monkeypatch.setattr(page, "edit_skill", lambda item, old: settings_logic.put_skill(win.data, item))
    assert page.import_skill() and page.current()["name"] == "float-watch-2"  # the name was taken: renamed
    assert win.data["custom_skills"][1]["files"] == mine["files"]

    sp = win.page(ShopPage)
    assert "read when you open this page" in sp.state.text() and sp.view.items == []  # no network yet
    shared = shop.meta(dict(mine, name="lookahead-plus", title="Look-ahead plus", template="Use p6_lookahead."),
                       version=1, author="planner1", issue=3)
    entry = {k: v for k, v in shared.items() if k not in ("args", "template", "files")} | {"sha256": "ab"}
    monkeypatch.setattr(shop, "fetch_index", lambda *a, **k: {"skills": [entry], "error": None, "url": ""})
    monkeypatch.setattr(shop, "fetch_skill", lambda e, *a, **k: dict(shared, sha256=e["sha256"]))
    sp.load()
    qtbot.waitUntil(lambda: sp.job is None, timeout=15000)  # the index; a skill is read when it is needed
    card = sp.view.item("lookahead-plus")
    assert len(sp.view.items) == 1 and card.badge.text() == "Not installed" and not sp.items
    assert card.install_btn.text() == "Install" and card.remove_btn.isHidden()
    shown = []
    monkeypatch.setattr(sp, "show_dialog", shown.append)
    sp.view.set_current("lookahead-plus")
    dlg = sp.details()  # the details read the skill (sha256 checked by fetch_skill)
    qtbot.waitUntil(lambda: "name: p6-lookahead-plus" in dlg.md.toPlainText(), timeout=15000)
    assert "planner1" in dlg.author.text() and "/issues/3" in dlg.issue.text()
    assert "p6_lookahead" in dlg.tools.text() and dlg.buttons["install"].isEnabled()
    assert sp.install() and sp.view.item("lookahead-plus").badge.text() == "Installed"
    assert not sp.view.item("lookahead-plus").remove_btn.isHidden()
    assert "remove" in dlg.buttons and "install" not in dlg.buttons  # the open dialog follows the install
    stored = next(c for c in win.data["custom_skills"] if c["name"] == "lookahead-plus")
    from p6mcp import skills
    assert stored["source"] == {"shop": "lookahead-plus", "version": 1, "sha256": "ab",
                                "content_sha": skills.content_sha(stored)}
    assert len(win.page(SkillsPage).rows) == 3 and win.save()  # the Skills page updated at once
    assert sp.remove() and shop.installed_as(win.data, "lookahead-plus") is None
    assert "install" in dlg.buttons and len(win.page(SkillsPage).rows) == 2
    dlg.done(0)
    assert sp.dialog is None

    old = {"schema": 1, "kind": shop.KIND, "builtin": True, "name": "delay-whatif", "title": "Delay what-if",
           "description": "Delay. Use when asked.", "tools": [], "writes": False, "min_app_version": "1.8.2",
           "version": 1, "author": "nizarhamza", "sha256": "cd"}  # a built-in entry of a 1.8.2 catalog
    sp.index = {"skills": [entry, old], "error": None}
    assert shop_page.FILTERS == ("all", "read", "writes", "installed", "updates")  # no Built-in filter
    sp.refresh()
    sp.view.set_current("delay-whatif")
    card = sp.view.item("delay-whatif")
    dlg = sp.details()
    assert card.ident.text() == "p6-delay-whatif" and sp.job is None  # never read from the catalog
    assert card.badge.text() == "Needs a newer MCP for Primavera P6" == dlg.md.toPlainText()
    assert not card.install_btn.isEnabled() and card.remove_btn.isHidden() and not sp.install()
    assert not dlg.buttons["install"].isEnabled() and "remove" not in dlg.buttons
    dlg.done(0)
    win.set_data(dict(win.data, shop_enabled=False))
    sp.refresh()
    assert "turned off" in sp.state.text() and sp.view.items == []


def test_restore_core_skills_and_edited_shop_skills(qtbot, win, env, monkeypatch):
    from p6mcp import core_skills, settings_logic, shop
    from p6mcp.settings_app import guidance_pages, shop_page
    from p6mcp.settings_app.guidance_pages import SkillsPage
    from p6mcp.settings_app.shop_page import ShopPage
    monkeypatch.setattr(guidance_pages, "ask", lambda *a, **k: True)
    page, sp, names = win.page(SkillsPage), win.page(ShopPage), core_skills.names()
    assert sp.restore_btn.text() == page.restore_btn.text() == "Restore core skills..."
    assert page.restore_core() and not page.restore_btn.isEnabled()  # the shop is offline in these tests (env)
    qtbot.waitUntil(lambda: page.job is None, timeout=15000)
    assert [r["name"] for r in page.rows] == names and page.restore_btn.isEnabled() and win.dirty()
    assert [page.view.item(n).source.text() for n in names] == ["Shop"] * len(names)
    assert "copy shipped with MCP for Primavera P6" in win.statusBar().currentMessage()
    stored = {c["name"]: c for c in win.data["custom_skills"]}
    assert [stored[r["name"]]["source"]["sha256"] for r in core_skills.bundle()["skills"]] == \
        [r["sha256"] for r in core_skills.bundle()["skills"]]  # the Shop page sees later updates

    first = names[0]  # edited here: it keeps its source, the automatic update leaves it alone
    win.set_data(settings_logic.put_skill(win.data, dict(stored[first], template="Mine p6_lookahead."), first))
    page.refresh(first)
    assert page.view.item(first).source.text() == "Shop, edited here"
    later = shop.meta(dict(core_skills.bundled(first), version=99), version=99, author="nizarhamza")
    entry = {k: v for k, v in later.items() if k not in ("args", "template", "files")} | {"sha256": "v99"}
    monkeypatch.setattr(shop, "fetch_index", lambda *a, **k: {"skills": [entry], "error": None, "url": ""})
    monkeypatch.setattr(shop, "fetch_skill", lambda e, *a, **k: dict(later, sha256=e["sha256"]))
    sp.load()
    qtbot.waitUntil(lambda: sp.job is None, timeout=15000)
    card = sp.view.item(first)
    assert card.badge.text() == "Update available, edited here" and card.install_btn.text() == "Update"
    asked = []
    monkeypatch.setattr(shop_page, "ask", lambda *a, **k: asked.append(a[2]) or False)
    card.install_btn.click()  # Update on the card: the skill is read first, then install() asks
    qtbot.waitUntil(lambda: bool(asked) and not sp.reading, timeout=15000)
    assert asked[0].startswith(f"You edited the skill {first} here") and bool(sp.items)
    assert next(c for c in win.data["custom_skills"] if c["name"] == first)["template"] == "Mine p6_lookahead."

    sp.restore_btn.click()  # online now: the edited one gets the shop's version back, the others are current
    qtbot.waitUntil(lambda: page.job is None, timeout=15000)
    restored = next(c for c in win.data["custom_skills"] if c["name"] == first)
    assert restored["source"]["version"] == 99 and restored["template"] == core_skills.bundled(first)["template"]
    assert win.statusBar().currentMessage().startswith("Core skills restored from the shop: 1 installed")
    assert sp.view.item(first).badge.text() == "Installed"  # the Shop page was refreshed

    data = settings_logic.remove_skill(win.data, names[-1])  # one of the user's own takes a core name
    win.set_data(settings_logic.put_skill(data, {"name": names[-1], "title": "Mine", "description": "Mine. Use "
                                                 "when asked.", "args": [], "template": "Run p6_lookahead."}))
    assert page.restore_core()
    qtbot.waitUntil(lambda: page.job is None, timeout=15000)
    assert f"Not restored: {names[-1]}" in env["warnings"][-1]
    assert next(c for c in win.data["custom_skills"] if c["name"] == names[-1])["title"] == "Mine"


# ----------------------------------------------------------------- Skills and Skill shop cards (2.0)

def three_skills(win) -> None:
    """One of the user's own skills, one shop skill and one shop skill edited here."""
    from p6mcp import settings_logic, skills
    brief = settings_logic.clean_skill({"name": "lookahead-brief", "title": "Look-ahead brief", "args": [],
                                        "description": "Short look-ahead. Use when asked.",
                                        "template": "Run p6_lookahead."})
    tracker = settings_logic.clean_skill({"name": "milestones", "title": "Milestone tracker", "args": [],
                                          "description": "Milestones against the baseline. Use when asked.",
                                          "template": "Run p6_milestones."})
    data = settings_logic.put_skill(win.data, {"name": "float-watch", "title": "Float watch", "args": [],
                                               "description": "Low float. Use when asked.",
                                               "template": "Run p6_float_analysis."})
    data = settings_logic.put_skill(data, dict(brief, source={"shop": "lookahead-brief", "version": 2, "sha256": "aa",
                                                              "content_sha": skills.content_sha(brief)}))
    data = settings_logic.put_skill(data, dict(tracker, source={"shop": "milestones", "version": 1, "sha256": "bb",
                                                                "content_sha": "changed here"}))
    win.set_data(data)


def test_list_columns_fit_the_width():
    from p6mcp.settings_app.cards import fit_columns
    cols = [("id", 100, 60), ("offered", 80, 40), ("source", 50, 30)]  # most useful first
    assert fit_columns(cols, 300) == {"id": 100, "offered": 80, "source": 50}
    narrowed = fit_columns(cols, 180)  # every column toward its minimum, in proportion
    assert sum(narrowed.values()) <= 180 and all(m <= narrowed[k] < n for k, n, m in cols)
    assert fit_columns(cols, 120) == {"id": 70, "offered": 50, "source": 0}  # the least useful hidden first
    assert fit_columns(cols, 50) == {"id": 0, "offered": 0, "source": 0}
    assert fit_columns(cols, 255, gap=10) == {"id": 98, "offered": 78, "source": 49}  # each shown column + its gap
    assert fit_columns(cols, 150, gap=10) == {"id": 75, "offered": 55, "source": 0}


def test_skills_page_search_source_filter_and_saved_view(qtbot, win, env):
    from p6mcp import settings_logic
    from p6mcp.settings_app import guidance_pages
    from p6mcp.settings_app.cards import GRID, LIST
    from p6mcp.settings_app.guidance_pages import SkillsPage
    from p6mcp.settings_app.shop_page import ShopPage
    three_skills(win)
    page = win.page(SkillsPage)
    page.refresh()
    before = copy.deepcopy(win.data)

    def shown():
        return [it.key for it in page.view.items]
    assert shown() == ["float-watch", "lookahead-brief", "milestones"]
    for text, want in (("LOOK ahead", ["lookahead-brief"]), ("p6-mile", ["milestones"]),  # every word, case ignored
                       ("baseline use", ["milestones"]), ("low", ["float-watch"]), ("", shown())):
        page.search.setText(text)
        assert shown() == want, text
    assert [page.source.itemData(i) for i in range(page.source.count())] == ["all", "shop", "shop_edited", "custom"]
    assert [page.source.itemText(i) for i in range(page.source.count())] == \
        ["All sources", "Shop", "Shop, edited here", "Yours"]  # the values of the former Source column
    for src, want in (("shop", ["lookahead-brief"]), ("shop_edited", ["milestones"]), ("custom", ["float-watch"])):
        page.source.setCurrentIndex(page.source.findData(src))
        assert shown() == want and page.view.item(want[0]).source.text() == page.source.currentText()
    page.search.setText("brief")  # a shop skill, the filter is on Yours
    assert shown() == [] and page.view.empty_text.text() == "No skill matches the search or the filter."
    page.search.setText("")
    page.source.setCurrentIndex(0)
    assert win.data == before  # searching and filtering change no setting

    # Grid or List: window state kept for the next launch, never a setting (no Save, no Revert)
    assert page.view.mode == GRID and page.toggle.buttons[GRID].isChecked()
    page.toggle.buttons[LIST].click()
    assert page.view.mode == LIST and page.view.header is not None and win.data == before
    assert settings_logic.load_ui_state() == {"skills_view": "list"}
    row = page.view.item("milestones")
    assert row.source.text() == "Shop, edited here" and row.ident.text() == "p6-milestones"
    assert row.offered.text() == guidance_pages.offered(page.row("milestones"))[1]  # the value alone in a row
    w2 = make(qtbot)  # the next launch
    assert w2.page(SkillsPage).view.mode == LIST and w2.page(SkillsPage).toggle.buttons[LIST].isChecked()
    assert w2.page(ShopPage).view.mode == GRID  # one setting per page
    w2.page(ShopPage).toggle.buttons[LIST].click()
    assert settings_logic.load_ui_state() == {"skills_view": "list", "shop_view": "list"}


def test_skill_cards_keyboard_menu_and_names(qtbot, win, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from p6mcp import settings_logic
    from p6mcp.settings_app import guidance_pages
    from p6mcp.settings_app.cards import GRID, LIST
    from p6mcp.settings_app.guidance_pages import SkillsPage
    three_skills(win)
    assert win.save()  # Revert, at the end, goes back to these three
    page = win.page(SkillsPage)
    win.nav.setCurrentRow(win.pages.index(page))
    win.resize(1400, 900)
    win.show()
    qtbot.waitExposed(win)
    win.activateWindow()
    page.refresh()
    qtbot.waitUntil(lambda: page.view.column_count() >= 2)
    cards = page.view.items
    assert [c.focusPolicy() for c in cards] == [Qt.FocusPolicy.StrongFocus] + [Qt.FocusPolicy.ClickFocus] * 2
    cards[0].setFocus()
    qtbot.waitUntil(cards[0].hasFocus)
    for key, target in ((Qt.Key.Key_Right, 1), (Qt.Key.Key_Left, 0), (Qt.Key.Key_End, 2), (Qt.Key.Key_Home, 0)):
        qtbot.keyClick(QApplication.focusWidget(), key)
        assert cards[target].hasFocus(), key
    assert page.view.current == cards[0].key and cards[0].focusPolicy() == Qt.FocusPolicy.StrongFocus  # one tab stop
    qtbot.keyClick(cards[0], Qt.Key.Key_Down)
    assert cards[page.view.column_count()].hasFocus() if page.view.column_count() < 3 else cards[0].hasFocus()

    name = "milestones"
    page.view.item(name).setFocus()
    qtbot.waitUntil(page.view.item(name).hasFocus)
    qtbot.keyClick(page.view.item(name), Qt.Key.Key_Space)  # On / off, applied after the key, kept until Save
    qtbot.waitUntil(lambda: page.row(name)["enabled"] is False)
    assert win.dirty() and page.view.item(name).hasFocus()  # the rebuilt card keeps the keyboard
    shown = []
    monkeypatch.setattr(page, "show_dialog", shown.append)
    qtbot.keyClick(page.view.item(name), Qt.Key.Key_Return)  # Enter: details
    qtbot.waitUntil(lambda: bool(shown))
    assert shown[0].ident.text() == "p6-milestones" and shown[0].offered.text() == "No (turned off)"
    asked = []
    monkeypatch.setattr(guidance_pages, "ask", lambda *a, **k: asked.append(a[2]) or True)
    qtbot.keyClick(page.view.item(name), Qt.Key.Key_Delete)  # Delete: Remove, with the question naming it
    qtbot.waitUntil(lambda: page.row(name) is None)
    assert asked == ["Remove the skill milestones?"]

    mine, theirs = page.view.item("float-watch"), page.view.item("lookahead-brief")
    acts = {a.objectName(): a for a in mine.menu().actions() if a.objectName()}
    assert list(acts) == ["details", "edit", "copy", "export", "share"] and all(a.isEnabled() for a in acts.values())
    assert "GitHub form" in acts["share"].toolTip() and "Export" in acts["share"].toolTip()  # the Share help
    acts = {a.objectName(): a for a in theirs.menu().actions() if a.objectName()}
    assert not acts["share"].isEnabled() and "already shared" in acts["share"].toolTip()
    copied = []
    monkeypatch.setattr(page, "edit_skill", lambda item, old: copied.append(item) or None)
    acts["copy"].trigger()  # a menu action runs on its own card
    qtbot.waitUntil(lambda: bool(copied))
    assert copied[0]["name"] == "my-lookahead-brief" and "source" not in copied[0]
    assert mine.accessibleName() == "p6-float-watch, Float watch" and "Space" in mine.accessibleDescription()
    assert mine.more.accessibleName() == mine.more.toolTip() == "More actions"
    assert mine.check.accessibleName() == "Offer p6-float-watch to the MCP clients (applied on Save)"
    assert page.toggle.buttons[GRID].accessibleName() == "Grid view" == page.toggle.buttons[GRID].toolTip()
    assert page.toggle.buttons[LIST].accessibleName() == "List view"
    mine.remove_btn.click()  # the card's Remove asks first (answered yes here)
    qtbot.waitUntil(lambda: page.row("float-watch") is None)
    assert asked[-1] == "Remove the skill float-watch?"
    win.revert()  # Revert undoes the removals and the on / off change made on this page
    assert [r["name"] for r in page.rows] == ["float-watch", "lookahead-brief", "milestones"]
    assert page.row("milestones")["enabled"] is True and not win.dirty()
    assert "Source: Shop." in page.view.item("lookahead-brief").accessibleDescription()  # through the catalog

    page.search.setText("look")  # the rebuilt cards keep the view's place in the tab order
    page.search.setText("")
    page.toggle.buttons[LIST].setFocus()
    qtbot.waitUntil(page.toggle.buttons[LIST].hasFocus)
    qtbot.keyClick(page.toggle.buttons[LIST], Qt.Key.Key_Tab)
    assert page.view.item(page.view.current).hasFocus()  # not the language box, Revert or Save
    page.search.setText("no such skill")  # the empty state spans the whole width, not the first column
    qtbot.waitUntil(lambda: page.view.empty.width() > page.view.avail() * 0.9)
    page.search.setText("")
    page.source.setCurrentIndex(page.source.findData("shop"))  # a new skill of yours under the Shop filter
    new = {"name": "my-new", "title": "Mine", "description": "Mine. Use when asked.", "args": [],
           "template": "Run p6_lookahead."}
    monkeypatch.setattr(page, "edit_skill", lambda item, old: settings_logic.put_skill(win.data, new))
    assert page.add() and page.current()["name"] == "my-new" and page.source.currentData() == "all"


def test_list_rows_line_up_and_fit_the_window(qtbot, win):
    from p6mcp.settings_app.cards import LIST
    from p6mcp.settings_app.guidance_pages import SkillsPage
    three_skills(win)
    page = win.page(SkillsPage)
    win.nav.setCurrentRow(win.pages.index(page))
    win.resize(1400, 900)
    win.show()
    qtbot.waitExposed(win)
    page.toggle.buttons[LIST].click()
    head, rows = page.view.header, page.view.items

    def lined_up():  # once the rows are laid out: each cell at the same x in every row and the header
        return all(len({r.cells[k].x() for r in rows + [head]}) == 1 for k in ("id", "source", "offered"))
    qtbot.waitUntil(lambda: rows[0].cells["source"].isVisible() and lined_up())
    assert head.title.x() == rows[0].title.x() and head.cells["id"].x() < head.title.x() < head.cells["source"].x()
    win.resize(win.minimumSizeHint())  # the narrowest window: cells narrowed, then hidden, never cut
    qtbot.waitUntil(lambda: page.view.inner.width() <= page.view.viewport().width())
    assert rows[0].remove_btn.isVisible() and rows[0].more.isVisible()
    assert rows[0].geometry().right() <= page.view.viewport().width()
    assert page.view.column_count() == 1


def test_details_dialog_keeps_its_size_and_edit_opens_the_editor(qtbot, win, monkeypatch):
    from PySide6.QtCore import QSize
    from p6mcp import settings_logic
    from p6mcp.settings_app.guidance_pages import SkillsPage
    three_skills(win)
    page = win.page(SkillsPage)
    page.refresh("float-watch")
    shown = []
    monkeypatch.setattr(page, "show_dialog", shown.append)
    dlg = page.details()
    assert dlg.isSizeGripEnabled() and dlg.head.text() == "Float watch" and dlg.ident.text() == "p6-float-watch"
    assert not dlg.form.isRowVisible(dlg.author) and not dlg.form.isRowVisible(dlg.version)  # the user's own
    assert dlg.source.text() == "Yours" and "p6_float_analysis" in dlg.tools.text()
    assert "uses write tools" not in dlg.tools.text() and dlg.buttons["edit"].isEnabled()
    size = dlg.minimumSizeHint() + QSize(30, 20)
    dlg.resize(size)
    closed = []
    dlg.finished.connect(closed.append)
    dlg.buttons["close"].click()  # Close (the button box's reject role)
    assert closed == [0] and dlg.result() == 0
    assert settings_logic.load_ui_state()["details_size"] == [size.width(), size.height()]
    assert page.details().size() == size  # kept for the next opening (window state, not a setting)
    page.refresh("lookahead-brief")
    dlg = page.details()
    assert dlg.version.text() == "2" and dlg.source.text() == "Shop" and dlg.form.isRowVisible(dlg.version)
    edited = []
    monkeypatch.setattr(page, "edit_skill", lambda item, old: edited.append(old) or None)
    monkeypatch.setattr(page, "show_dialog", lambda d: d.buttons["edit"].click())  # Edit... closes, then edits
    dlg = page.details()
    assert dlg.result() == 1 and edited == ["lookahead-brief"]


def test_shop_cards_busy_retry_install_and_remove(qtbot, win, monkeypatch):
    import threading
    from p6mcp import shop
    from p6mcp.settings_app import shop_page
    from p6mcp.settings_app.cards import LIST
    from p6mcp.settings_app.guidance_pages import SkillsPage
    from p6mcp.settings_app.shop_page import ShopPage
    sp = win.page(ShopPage)
    sp.load()  # the shop is offline in these tests (env)
    assert sp.busy.isVisibleTo(sp) and not sp.reload_btn.isEnabled() and "Reading the shop" in sp.state.text()
    qtbot.waitUntil(lambda: sp.job is None, timeout=15000)
    assert not sp.busy.isVisibleTo(sp) and "cannot be read" in sp.state.text() and sp.view.items == []
    assert sp.view.empty_text.text().startswith("The skill shop could not be reached")
    assert sp.view.empty_btn.text() == "Retry" and not sp.view.empty_btn.isHidden()

    def meta(name, title, template, author):
        m = shop.meta({"name": name, "title": title, "description": title + ". Use when asked.", "args": [],
                       "template": template, "files": []}, version=1, author=author)
        return m, {k: v for k, v in m.items() if k not in ("args", "template", "files")} | {"sha256": "s-" + name}
    plus, plus_entry = meta("lookahead-plus", "Look-ahead plus", "Use p6_lookahead.", "planner1")
    relink, relink_entry = meta("relink-dangling", "Fix dangling ends", "Use p6_add_relationships.", "scheduler-x")
    items, reads, gate = {"lookahead-plus": plus, "relink-dangling": relink}, [], threading.Event()

    def fetch(e, *a, **k):
        gate.wait(10)
        reads.append(e["name"])
        return dict(items[e["name"]], sha256=e["sha256"])
    monkeypatch.setattr(shop, "fetch_index", lambda *a, **k: {"skills": [plus_entry, relink_entry], "error": None,
                                                               "url": ""})
    monkeypatch.setattr(shop, "fetch_skill", fetch)
    sp.view.empty_btn.click()  # Retry = Refresh
    assert sp.view.empty_btn.isHidden() and sp.view.empty_text.text() == "" and sp.busy.isVisibleTo(sp)
    qtbot.waitUntil(lambda: sp.job is None and len(sp.view.items) == 2, timeout=15000)
    assert sp.state.text() == "2 skills in the shop."
    for text, f, want in (("planner1", "all", ["lookahead-plus"]), ("", "writes", ["relink-dangling"]),
                          ("", "read", ["lookahead-plus"]), ("", "installed", [])):
        sp.search.setText(text)
        sp.filter.setCurrentIndex(sp.filter.findData(f))
        assert [c.key for c in sp.view.items] == want, (text, f)
    assert sp.view.empty_text.text() == "No skill matches the search or the filter."
    sp.filter.setCurrentIndex(0)

    asked = []
    monkeypatch.setattr(shop_page, "ask", lambda *a, **k: asked.append(a[2]) or True)
    sp.view.item("relink-dangling").install_btn.click()  # Install on a card: read first, then install()
    qtbot.waitUntil(lambda: "relink-dangling" in sp.reading)
    card = sp.view.item("relink-dangling")
    assert card.install_btn.text() == "Reading..." and not card.install_btn.isEnabled()
    sp.search.setText("planner1")  # the card is hidden while its skill is read: the Install still runs
    assert sp.view.item("relink-dangling") is None
    gate.set()
    qtbot.waitUntil(lambda: shop.installed_as(win.data, "relink-dangling") is not None, timeout=15000)
    sp.search.setText("")
    assert reads == ["relink-dangling"] and asked[0].startswith("The skill relink-dangling uses write tools")
    assert sp.view.item("relink-dangling").badge.text() == "Installed" and win.dirty()
    assert "relink-dangling" in [r["name"] for r in win.page(SkillsPage).rows]  # the Skills page at once
    acts = {a.objectName(): a for a in sp.view.item("lookahead-plus").menu().actions() if a.objectName()}
    assert acts["install"].isEnabled() and not acts["remove"].isEnabled() and acts["details"].isEnabled()

    sp.toggle.buttons[LIST].click()  # a row has one action: Install or Update, else Remove
    row = sp.view.item("relink-dangling")
    assert row.install_btn.isHidden() and not row.remove_btn.isHidden()
    assert row.details_btn.toolTip() == row.details_btn.accessibleName().split(" p6-")[0] == "View details"
    qtbot.keyClick(row, Qt.Key.Key_Delete)  # Delete: Remove, after the question
    qtbot.waitUntil(lambda: shop.installed_as(win.data, "relink-dangling") is None, timeout=5000)
    assert asked[-1] == "Remove the skill relink-dangling?"
    assert "relink-dangling" not in [r["name"] for r in win.page(SkillsPage).rows]


def test_skill_cards_mirror_right_to_left(qtbot, win, monkeypatch):
    from p6mcp.settings_app.cards import LIST
    from p6mcp.settings_app.guidance_pages import SkillsPage
    monkeypatch.setattr(i18n, "RTL", {"en"})  # stands for Arabic until its catalog exists
    three_skills(win)
    win.build(win.pages.index(win.page(SkillsPage)))
    page = win.page(SkillsPage)
    win.resize(1400, 900)
    win.show()
    qtbot.waitExposed(win)
    win.activateWindow()
    qtbot.waitUntil(lambda: page.view.column_count() >= 2 and page.view.items[1].isVisible())
    first, second = page.view.items[:2]
    assert first.layoutDirection() == Qt.LayoutDirection.RightToLeft
    assert first.x() > second.x() and first.check.x() > first.ident.x()  # the first card on the right
    first.setFocus()
    qtbot.waitUntil(first.hasFocus)
    qtbot.keyClick(first, Qt.Key.Key_Left)  # Left goes to the next card in a right-to-left layout
    assert second.hasFocus()
    page.toggle.buttons[LIST].click()
    head, row = page.view.header, page.view.items[0]
    qtbot.waitUntil(lambda: row.cells["source"].isVisible() and head.cells["id"].x() == row.cells["id"].x())
    assert row.cells["id"].x() > row.cells["source"].x()  # the ID on the right, the Source to its left
    assert row.check.x() > row.remove_btn.x()
    assert row.menu().layoutDirection() == Qt.LayoutDirection.RightToLeft  # a menu is a window: set, not inherited
    shown = []
    monkeypatch.setattr(page, "show_dialog", shown.append)
    page.details()
    assert shown[0].layoutDirection() == Qt.LayoutDirection.RightToLeft


def test_shop_details_dialog_acts_on_its_skill_and_reads_once(qtbot, win, monkeypatch):
    from p6mcp import shop
    from p6mcp.settings_app import shop_page
    from p6mcp.settings_app.shop_page import ShopPage
    monkeypatch.setattr(shop_page, "ask", lambda *a, **k: True)
    sp = win.page(ShopPage)
    shown, reads, errors = [], [], []
    monkeypatch.setattr(sp, "show_dialog", shown.append)
    monkeypatch.setattr(win, "error", errors.append)
    m = shop.meta({"name": "lookahead-plus", "title": "Look-ahead plus", "description": "Plus. Use when asked.",
                   "args": [], "template": "Use p6_lookahead.", "files": []}, version=1, author="planner1")
    entry = {k: v for k, v in m.items() if k not in ("args", "template", "files")} | {"sha256": "v1"}
    index = {"skills": [entry], "error": None, "url": ""}
    monkeypatch.setattr(shop, "fetch_index", lambda *a, **k: dict(index, skills=[dict(e) for e in index["skills"]]))

    def offline(e, *a, **k):
        reads.append(e["name"])
        raise OSError("offline (test)")
    monkeypatch.setattr(shop, "fetch_skill", offline)
    sp.load()
    qtbot.waitUntil(lambda: sp.job is None and len(sp.view.items) == 1, timeout=15000)
    sp.view.set_current("lookahead-plus")
    dlg = sp.details()
    qtbot.waitUntil(lambda: "offline" in dlg.md.toPlainText(), timeout=15000)
    dlg.buttons["install"].click()  # one more read for the Install: refreshing the dialog never reads again
    qtbot.waitUntil(lambda: bool(errors) and not sp.reading, timeout=15000)
    assert reads == ["lookahead-plus"] * 2 and len(errors) == 1 and "offline" in dlg.md.toPlainText()

    # the catalog changed since its index was read: the newer, checked version installs, the index is read again
    newer = dict(m, version=2, sha256="v2")
    monkeypatch.setattr(shop, "fetch_skill", lambda e, *a, **k: reads.append(e["name"]) or dict(newer))
    index["skills"] = [dict(entry, version=2, sha256="v2")]
    dlg.buttons["install"].click()
    qtbot.waitUntil(lambda: shop.installed_as(win.data, "lookahead-plus") is not None and sp.job is None,
                    timeout=15000)
    assert shop.installed_as(win.data, "lookahead-plus")["source"]["version"] == 2
    assert sp.view.item("lookahead-plus").badge.text() == "Installed"  # after the new read of the index
    assert "remove" in dlg.buttons and "install" not in dlg.buttons

    # the dialog acts on its own skill even when the filter no longer lists it
    sp.filter.setCurrentIndex(sp.filter.findData("installed"))
    dlg.buttons["remove"].click()
    qtbot.waitUntil(lambda: shop.installed_as(win.data, "lookahead-plus") is None, timeout=5000)
    assert sp.view.items == [] and dlg.buttons["install"].isEnabled()
    dlg.buttons["install"].click()
    qtbot.waitUntil(lambda: shop.installed_as(win.data, "lookahead-plus") is not None, timeout=5000)
    dlg.done(0)
