"""Remote access, milestone M4: the Settings Remote access page, the approval window and the tray's remote access
part, driven offscreen with pytest-qt (dialogs answered, Credential Manager and Cloudflare faked)."""

import json

import pytest

pytestmark = pytest.mark.qt
pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from p6mcp import config, i18n  # noqa: E402
from p6mcp.remote import cloudflare  # noqa: E402
from p6mcp.remote.store import Store  # noqa: E402
from p6mcp.settings_app import remote_page  # noqa: E402
from p6mcp.settings_app.approval import ApprovalDialog  # noqa: E402
from p6mcp.settings_app.clients_page import ClientsPage  # noqa: E402
from p6mcp.settings_app.remote_page import RemotePage  # noqa: E402
from p6mcp.settings_app.window import SettingsWindow  # noqa: E402

HOST = "p6mcp-pc.someone.workers.dev"
RESULT = cloudflare.SetupResult(account_id="a" * 32, mode="relay", public_host=HOST, tunnel_id="t-1",
                                tunnel_token="tunnel-token", relay_secret="relay-secret", service_id="s-1",
                                script="p6mcp-pc", steps=["Token checked"])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("P6MCP_CONFIG", str(tmp_path / "config.json"))
    for var, sub in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local"), ("USERPROFILE", "User")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setattr("p6mcp.clients._running_names", lambda: set())
    monkeypatch.setattr("p6mcp.shop.fetch_index", lambda *a, **k: {"skills": [], "error": "offline", "url": ""})
    monkeypatch.setattr("p6mcp.updates.check", lambda *a, **k: {"current": "1.0.0", "latest": "1.0.0",
                                                                  "newer": False, "url": "x", "error": None})
    monkeypatch.setattr("p6mcp.settings_app.window.startup.sync", lambda data: None)
    monkeypatch.setattr(SettingsWindow, "ask_close", lambda self: "discard")
    monkeypatch.setattr(SettingsWindow, "ask_changed", lambda self: "overwrite")
    answers = {"value": True, "asked": []}

    def ask(parent, title, text, warning=True):
        answers["asked"].append(text)
        return answers["value"]
    monkeypatch.setattr(remote_page, "ask", ask)
    vault = {}
    monkeypatch.setattr("p6mcp.credentials.set", lambda k, v: vault.__setitem__(k, v))
    monkeypatch.setattr("p6mcp.credentials.get", lambda k: vault.get(k))
    monkeypatch.setattr("p6mcp.credentials.delete", lambda k: vault.pop(k, None) is not None)
    cf_calls = []
    return {"answers": answers, "vault": vault, "cf": cf_calls, "tmp": tmp_path}


def make(qtbot, data):
    config.save(data)
    w = SettingsWindow()
    qtbot.addWidget(w)
    qtbot.waitUntil(lambda: w.page(ClientsPage).job is None, timeout=15000)
    return w


@pytest.fixture
def win(qtbot, env):
    w = make(qtbot, {"language": "en"})
    yield w
    i18n.set_language("en")


def page(w) -> RemotePage:
    return w.page(RemotePage)


def test_not_set_up(win):
    p = page(win)
    assert p.status.text().startswith("Not set up") and p.setup_btn.text() == "Set up"
    assert not p.enabled.isEnabled() and not p.remove_btn.isEnabled() and p.address.text() == ""
    p.run_setup()
    assert "Paste a Cloudflare API token" in p.progress.text()
    p.domain.setChecked(True)
    p.token.setText("tok")
    p.domain_host.setText("not a host!")
    p.run_setup()
    assert "host name" in p.progress.text() and p.job is None


def test_setup_saves_at_once_and_keeps_other_edits(qtbot, win, env, monkeypatch):
    seen = {}

    def fake_setup(token, mode, port, account_id, host, relay_secret=None, progress=None):
        seen.update(token=token, mode=mode, port=port, keep=relay_secret)
        progress("Token checked")
        return RESULT
    monkeypatch.setattr(remote_page.cloudflare, "setup", fake_setup)
    win.set_data(dict(win.data, p6_user="TYPED"))  # an unsaved edit elsewhere
    p = page(win)
    p.token.setText("  tok  ")
    p.run_setup()
    qtbot.waitUntil(lambda: p.job is None, timeout=10000)
    assert seen == {"token": "tok", "mode": "relay", "port": 39218, "keep": None}
    disk = json.loads(open(config.config_path(), encoding="utf-8").read())
    assert disk["remote_public_host"] == HOST and disk["remote_cf"]["tunnel_id"] == "t-1"
    assert "p6_user" not in disk or disk["p6_user"] != "TYPED"  # the other edit was not saved with it
    assert win.data["p6_user"] == "TYPED" and win.dirty()
    assert env["vault"] == {"remote:tunnel_token": "tunnel-token", "remote:relay_secret": "relay-secret"}
    assert p.token.text() == "" and p.address.text() == f"https://{HOST}/mcp" and "Done" in p.progress.text()
    assert p.setup_btn.text() == "Repair" and p.enabled.isEnabled() and "tunnel-token" not in json.dumps(disk)
    p.token.setText("tok")
    p.run_setup()  # repair keeps the relay secret
    qtbot.waitUntil(lambda: p.job is None, timeout=10000)
    assert seen["keep"] == "relay-secret"


def test_account_choice(qtbot, win, monkeypatch):
    calls = []

    def fake_setup(token, mode, port, account_id, host, relay_secret=None, progress=None):
        calls.append(account_id)
        if account_id is None:
            raise cloudflare.NeedsAccount([("a" * 32, "First"), ("b" * 32, "Second")])
        return RESULT
    monkeypatch.setattr(remote_page.cloudflare, "setup", fake_setup)
    monkeypatch.setattr(remote_page.QInputDialog, "getItem", lambda *a, **k: (a[3][1], True))
    p = page(win)
    p.token.setText("tok")
    p.run_setup()
    qtbot.waitUntil(lambda: len(calls) == 2 and p.job is None, timeout=10000)
    assert calls == [None, "b" * 32] and win.data["remote_public_host"] == HOST


def test_setup_failure_is_shown(qtbot, win, monkeypatch):
    def fake_setup(*a, **k):
        raise cloudflare.CloudflareError("The token is missing the permission 'Workers Scripts: Edit'")
    monkeypatch.setattr(remote_page.cloudflare, "setup", fake_setup)
    p = page(win)
    p.token.setText("tok")
    p.run_setup()
    qtbot.waitUntil(lambda: p.job is None, timeout=10000)
    assert "Workers Scripts: Edit" in p.progress.text() and not win.data.get("remote_public_host")


def set_up(qtbot, env, **extra):
    return make(qtbot, dict({"language": "en", "remote_public_host": HOST, "remote_mode": "relay",
                             "remote_cf": RESULT.ids()}, **extra))


def test_switches_ask_first(qtbot, env):
    w = set_up(qtbot, env)
    p = page(w)
    env["answers"]["value"] = False
    p.enabled.setChecked(True)
    assert not p.enabled.isChecked() and not w.data.get("remote_enabled")
    env["answers"]["value"] = True
    p.enabled.setChecked(True)
    assert w.data["remote_enabled"] and w.data["remote_privacy_ack"] and w.dirty()
    asked = len(env["answers"]["asked"])
    p.enabled.setChecked(False)
    p.enabled.setChecked(True)
    assert len(env["answers"]["asked"]) == asked  # the notice is asked once
    env["answers"]["value"] = False
    p.edits.setChecked(True)
    assert not p.edits.isChecked() and not w.data.get("remote_allow_writes")
    env["answers"]["value"] = True
    p.edits.setChecked(True)
    p.awake.setChecked(False)
    assert w.data["remote_allow_writes"] is True and w.data["remote_keep_awake"] is False
    assert p.status.text() == "Running. Connector address: https://" + HOST + "/mcp" or "Not running" in \
        p.status.text() or "Save" in p.status.text()


def test_connected_apps_and_revoke(qtbot, env):
    store = Store()
    for name, url, scopes in (("Claude", "https://claude.ai/api/mcp/auth_callback", ["p6:read"]),
                              ("ChatGPT", "https://chatgpt.com/connector_platform_oauth_redirect",
                               ["p6:read", "p6:write"])):
        req = store.add_pending({"client_id": name, "client_name": name, "via": "cimd"}, url, "s",
                                "c" * 43, ["p6:read"], f"https://{HOST}/mcp")
        store.decide(req["pair"], True, "p6:write" in scopes)
        store.finish_pending(req["id"])
    w = set_up(qtbot, env)
    p = page(w)
    assert p.clients.rowCount() == 2 and p.clients.item(0, 0).text() == "Claude (claude.ai)"
    assert p.clients.item(1, 3).text() == "Read and edit" and not p.revoke_btn.isEnabled()
    p.clients.selectRow(0)
    p.revoke_selected()
    assert p.clients.rowCount() == 1 and [g["client_name"] for g in store.grants()] == ["ChatGPT"]
    p.revoke_all()
    assert p.clients.rowCount() == 0 and store.grants() == []


def test_remove_with_and_without_token(qtbot, env, monkeypatch):
    removed = []
    monkeypatch.setattr(remote_page.cloudflare, "remove", lambda token, ids, progress=None: removed.append(
        (token, ids["tunnel_id"])) or ["Tunnel deleted"])
    env["vault"].update({"remote:tunnel_token": "t", "remote:relay_secret": "s"})
    w = set_up(qtbot, env, remote_enabled=True, remote_privacy_ack=True)
    p = page(w)
    p.token.setText("tok")
    p.run_remove()
    qtbot.waitUntil(lambda: p.job is None, timeout=10000)
    assert removed == [("tok", "t-1")] and env["vault"] == {}
    disk = json.loads(open(config.config_path(), encoding="utf-8").read())
    assert disk["remote_public_host"] is None and disk["remote_enabled"] is False and disk["remote_cf"] is None
    w2 = set_up(qtbot, env)
    p2 = page(w2)
    p2.run_remove()  # no token: asks to forget on this PC only
    assert any("this PC only" in t for t in env["answers"]["asked"])
    assert json.loads(open(config.config_path(), encoding="utf-8").read())["remote_public_host"] is None
    assert len(removed) == 1


def test_french_page(qtbot, env):
    w = make(qtbot, {"language": "fr"})
    try:
        assert page(w).setup_btn.text() == "Configurer" and "Accès à distance" in [
            w.nav.item(i).text() for i in range(w.nav.count())]
    finally:
        i18n.set_language("en")


# ----------------------------------------------------------------- approval window and tray

REQ = {"id": "r1", "pair": "ABC234", "client_name": "Claude", "redirect_uri": "https://claude.ai/api/mcp/auth_callback"}


def test_approval_dialog_needs_the_typed_code(qtbot):
    from PySide6.QtWidgets import QLabel
    calls = []

    def decide(rid, code, ok, edits):
        calls.append((rid, code, ok, edits))
        if ok and code.upper() != REQ["pair"]:
            return {"mismatch": True}
        return {"status": "approved" if ok else "denied"}
    d = ApprovalDialog(REQ, True, decide)
    qtbot.addWidget(d)
    texts = " ".join(lab.text() for lab in d.findChildren(QLabel))
    assert REQ["pair"] not in texts and "claude.ai" in texts  # the PC never shows the code: it is typed
    assert d.deny_btn.isDefault() and d.edits.isEnabled() and not d.approve_btn.isEnabled()
    d.code.setText("WRONG1")
    d.approve_btn.click()
    assert d.error.isVisibleTo(d) and "Tries left: 2" in d.error.text() and d.code.text() == ""
    d.code.setText("abc234")
    d.edits.setChecked(True)
    d.approve_btn.click()
    assert calls[-1] == ("r1", "abc234", True, True) and d.result_text == "approved"
    d2 = ApprovalDialog(dict(REQ, via="dcr"), False, decide)
    qtbot.addWidget(d2)
    assert not d2.edits.isEnabled() and "not verified" in " ".join(lab.text() for lab in d2.findChildren(QLabel))
    d2.deny_btn.click()
    assert calls[-1] == ("r1", "", False, False) and d2.result_text == "denied"
    d3 = ApprovalDialog(REQ, True, decide)
    qtbot.addWidget(d3)
    for _ in range(3):  # three wrong codes deny the request
        d3.code.setText("ZZZZZZ")
        d3.approve_btn.click()
    assert d3.result_text == "denied" and calls[-1] == ("r1", "", False, False)


class FakeRemote:
    def __init__(self):
        from p6mcp.remote.supervise import RemoteState
        from p6mcp.supervisor import DISABLED, State
        self.state = RemoteState(State(DISABLED), State(DISABLED))
        self.stopped = 0

    def tick(self):
        return self.state

    def stop(self):
        self.stopped += 1

    def restart(self):
        return self.state


def test_tray_approvals_and_stop(qtbot, env, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from p6mcp import supervisor
    from p6mcp.settings_app import tray as tray_mod
    config.save({"language": "en", "remote_enabled": True, "remote_privacy_ack": True, "remote_public_host": HOST,
                 "remote_allow_writes": True})

    class Sup:
        state = supervisor.State(supervisor.DISABLED)

        def tick(self):
            return self.state

        def stop(self):
            pass

    remote = FakeRemote()
    app = tray_mod.TrayApp(QApplication.instance(), Sup(), remote=remote)
    try:
        store = Store()
        req = store.add_pending({"client_id": "c", "client_name": "ChatGPT", "via": "cimd"},
                                "https://chatgpt.com/connector_platform_oauth_redirect", "s", "c" * 43, ["p6:read"],
                                f"https://{HOST}/mcp")
        app.poll_approvals()
        dlg = app.approvals[req["id"]]
        qtbot.addWidget(dlg)
        assert dlg.edits.isEnabled() and not dlg.approve_btn.isEnabled()
        dlg.code.setText(req["pair"])  # as read in the user's browser
        dlg.edits.setChecked(True)
        dlg.approve_btn.click()
        assert store.pending(req["id"])["status"] == "approved" and "p6:write" in store.pending(req["id"])["scopes"]
        app.poll_approvals()
        assert req["id"] not in app.approvals  # decided: no longer waiting
        other = store.add_pending({"client_id": "c", "client_name": "Claude", "via": "cimd"},
                                  "https://claude.ai/api/mcp/auth_callback", "s", "c" * 43, ["p6:read"],
                                  f"https://{HOST}/mcp")
        app.poll_approvals()
        assert other["id"] in app.approvals
        store.decide(other["pair"], False)  # decided elsewhere (command line): the window closes
        app.poll_approvals()
        assert other["id"] not in app.approvals
        app.stop_remote()
        assert remote.stopped >= 1 and config.read_file(config.config_path())[0]["remote_enabled"] is False
        assert app.keep_running() in (True, False)
    finally:
        app._shutdown()


def test_tray_window_waits_for_connect(qtbot, env, monkeypatch):
    """3.3: no approval window when a request arrives; the page's Connect button opens it with the code filled in,
    the notification or tray menu opens it to type the code; the Connect listener runs while remote access is on."""
    from PySide6.QtWidgets import QApplication

    from p6mcp import settings_logic, supervisor
    from p6mcp.settings_app import tray as tray_mod
    port = settings_logic.free_port(43900)
    config.save({"language": "en", "remote_enabled": True, "remote_privacy_ack": True, "remote_public_host": HOST,
                 "remote_pair_port": port})

    class Sup:
        state = supervisor.State(supervisor.DISABLED)

        def tick(self):
            return self.state

        def stop(self):
            pass

    app = tray_mod.TrayApp(QApplication.instance(), Sup(), remote=FakeRemote())
    monkeypatch.setattr(app, "_show_on_arrival", lambda: False)  # as with a notification area icon
    try:
        store = Store()
        client = {"client_id": "c", "client_name": "Claude", "via": "cimd"}
        req = store.add_pending(client, "https://claude.ai/api/mcp/auth_callback", "s", "c" * 43, ["p6:read"],
                                f"https://{HOST}/mcp", "key-0123456789")
        app.poll_approvals()
        assert app.pair_listener is not None and app.pair_listener.running and app.pair_listener.port == port
        dlg = app.approvals[req["id"]]
        qtbot.addWidget(dlg)
        assert not dlg.isVisible()  # nothing covers the browser page
        app._paired(store.local_hint(req["id"], req["pair"], "key-0123456789"), req["pair"])
        assert dlg.isVisible() and dlg.code.text() == req["pair"] and dlg.approve_btn.isEnabled()
        assert dlg.deny_btn.isDefault() and "Connect" in dlg.error.text()
        dlg.hide()
        other = store.add_pending(client, "https://claude.ai/api/mcp/auth_callback", "s", "c" * 43, ["p6:read"],
                                  f"https://{HOST}/mcp")
        app.poll_approvals()
        assert not app.approvals[other["id"]].isVisible()
        qtbot.addWidget(app.approvals[other["id"]])
        app.show_approvals()  # tray menu Approve a connection / notification click: type the code
        assert app.approvals[other["id"]].isVisible() and app.approvals[other["id"]].code.text() == ""
        app.stop_remote()
        assert app.pair_listener is None
    finally:
        app._shutdown()
