"""Notification area (tray) app: one instance per user, hosts and supervises the HTTP server, opens
the Settings window. Started by p6mcp-settings (with --minimized at Windows startup)."""

from __future__ import annotations

import getpass
from typing import Optional

from PySide6.QtCore import QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from p6mcp import config, runtime, supervisor
from p6mcp.i18n import tr
from p6mcp.settings_app.common import brand_file
from p6mcp.settings_app.updater import Updater

TICK_MS = 2000
MUTEX = "P6MCP-Tray"  # the installer's AppMutex: setup asks to close the app while it runs


def instance_name() -> str:
    return f"p6mcp-tray-{getpass.getuser()}"


def signal_running_instance() -> bool:
    """True when another instance answered: it shows its window and this one can exit."""
    sock = QLocalSocket()
    sock.connectToServer(instance_name())
    if not sock.waitForConnected(500):
        return False
    sock.write(b"show\n")
    sock.flush()
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


def _named_mutex(name: str):
    """A named mutex held while the app runs (None outside Windows)."""
    import sys
    if sys.platform != "win32":
        return None
    import ctypes
    return ctypes.windll.kernel32.CreateMutexW(None, False, name)


BRAND_SIZES = (16, 24, 32, 48, 64, 128, 256)  # assets/brand/png/p6mcp-icon-<size>.png (16 and 24 simplified)


def badge(size: int, dot: Optional[str] = None) -> QPixmap:
    """The app icon (assets/brand; a drawn "P6" tile when the files are absent) with an optional state dot."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    path = brand_file(f"png/p6mcp-icon-{next((s for s in BRAND_SIZES if s >= size), BRAND_SIZES[-1])}.png")
    icon = QPixmap(path) if path else QPixmap()
    if not icon.isNull():
        p.drawPixmap(QRect(0, 0, size, size), icon)
    else:
        p.setBrush(QColor("#0a1a33"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, size, size, size * 0.2, size * 0.2)
        f = QFont("Segoe UI")
        f.setBold(True)
        f.setPixelSize(int(size * 0.5))
        p.setFont(f)
        p.setPen(QColor("white"))
        p.drawText(QRect(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "P6")
    if dot:
        r = max(4, size // 4)
        p.setBrush(QColor(dot))
        p.setPen(QColor("white"))
        p.drawEllipse(size - r - 1, size - r - 1, r, r)
    p.end()
    return pm


def app_icon() -> QIcon:
    """Window and taskbar icon: assets/brand/ico/p6mcp.ico (the badge when absent)."""
    path = brand_file("ico/p6mcp.ico")
    return QIcon(path) if path else make_icon()


def make_icon(state: str = supervisor.DISABLED) -> QIcon:
    """The app icon; its dot shows the HTTP server state (green running, red error)."""
    icon = QIcon()
    dot = {supervisor.RUNNING: "#2e9d57", supervisor.ERROR: "#c62828", supervisor.STARTING: "#e0a000"}.get(state)
    for size in (16, 20, 24, 32, 40, 48, 64):
        icon.addPixmap(badge(size, dot))
    return icon


class TrayApp(QObject):
    paired = Signal(dict, str)  # the browser page's Connect button (listener thread -> Qt thread): request, code

    def __init__(self, app: QApplication, sup: Optional[supervisor.Supervisor] = None, remote=None):
        super().__init__()
        self.app = app
        self.supervisor = sup or supervisor.Supervisor()
        from p6mcp.remote.supervise import RemoteHost
        self.remote = remote or RemoteHost()  # remote access server and tunnel (only while remote access is on)
        self.approvals: dict = {}  # pending request id -> ApprovalDialog
        self.pair_listener = None  # p6mcp.remote.pairing.PairListener while remote access is on
        self._last_message = None  # what the last tray notification was about ("approve": a connection request)
        self.paired.connect(self._paired)
        self._mutex = _named_mutex(MUTEX)
        self.window = None
        self.notified = False
        self.server = QLocalServer(self)
        QLocalServer.removeServer(instance_name())  # a stale name left by a crash
        self.server.listen(instance_name())
        self.server.newConnection.connect(self._other_instance)
        self.tray: Optional[QSystemTrayIcon] = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(make_icon(), self)
            menu = QMenu()
            self.open_act = QAction(tr("tray.open"), menu)
            self.open_act.triggered.connect(self.show_window)
            self.status_act = QAction("", menu)
            self.status_act.setEnabled(False)
            self.restart_act = QAction(tr("tray.restart"), menu)
            self.restart_act.triggered.connect(self.restart_server)
            self.quit_act = QAction(tr("tray.quit"), menu)
            self.quit_act.triggered.connect(self.quit)
            self.remote_status_act = QAction("", menu)
            self.remote_status_act.setEnabled(False)
            self.remote_stop_act = QAction(tr("tray.remote.stop"), menu)
            self.remote_stop_act.triggered.connect(self.stop_remote)
            self.approve_act = QAction(tr("tray.remote.approve"), menu)
            self.approve_act.triggered.connect(self.show_approvals)
            self.approve_act.setVisible(False)
            for a in (self.open_act, self.status_act, self.restart_act):
                menu.addAction(a)
            menu.addSeparator()
            menu.addAction(self.remote_status_act)
            menu.addAction(self.approve_act)
            menu.addAction(self.remote_stop_act)
            menu.addSeparator()
            menu.addAction(self.quit_act)
            self.menu = menu
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._activated)
            self.tray.messageClicked.connect(self._message_clicked)
            self.tray.show()
            app.setQuitOnLastWindowClosed(False)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(TICK_MS)
        self.updater = Updater(self._quit_for_update, self)
        self.updater.skills_changed.connect(self._skills_updated)
        if self.tray is not None:
            self.updater.start()
        self.tick()

    # -- status
    def status_text(self, st: supervisor.State) -> str:
        if st.state == supervisor.DISABLED:
            return tr("tray.status.stdio")
        if st.state == supervisor.RUNNING:
            return tr("tray.status.running", port=st.port)
        if st.state == supervisor.STARTING:
            return tr("tray.status.starting", port=st.port)
        if st.state == supervisor.ERROR:
            return tr("tray.status.error", err=st.error or "?")
        return tr("tray.status.stopped")

    def tick(self) -> supervisor.State:
        try:
            st = self.supervisor.tick()
        except Exception as e:  # a supervisor bug must not kill the tray app
            st = supervisor.State(supervisor.ERROR, error=f"{type(e).__name__}: {e}")
        try:
            rst = self.remote.tick()
            self._remote_ui(rst)
            self.poll_approvals()
        except Exception:  # nor a remote access problem
            runtime.logging.getLogger("p6mcp.tray").exception("remote access supervisor failed")
        if self.tray is not None:
            text = self.status_text(st)
            self.status_act.setText(text)
            self.restart_act.setEnabled(st.state != supervisor.DISABLED)
            self.tray.setToolTip(tr("app.title") + "\n" + text)
            self.tray.setIcon(make_icon(st.state))
        if self.window is not None:
            self.window.server_state(st)
        return st

    # -- remote access
    def _remote_ui(self, rst) -> None:
        on = rst.state != supervisor.DISABLED
        if self.tray is not None:
            self.remote_status_act.setVisible(on)
            self.remote_stop_act.setVisible(on)
            if on:
                key = {"running": "tray.remote.running", "error": "tray.remote.error"}.get(rst.state,
                                                                                         "tray.remote.starting")
                self.remote_status_act.setText(tr(key, err=rst.error or "?"))
        if self.window is not None:
            self.window.remote_state(rst)

    def stop_remote(self) -> None:
        """Stop remote access now: the tunnel and the server stop, and remote access is turned off (saved), so it
        does not start again on the next tick."""
        self.remote.stop()
        st = config.load()
        data, _steps = config.read_file(st.config_path) if st.config_path else (None, [])
        if data is not None and data.get("remote_enabled"):
            config.save(dict(data, remote_enabled=False))
        if self.window is not None and not self.window.dirty():
            self.window.reload()
        for dlg in list(self.approvals.values()):
            dlg.reject()
        self.approvals.clear()
        self._pairing(None)
        self.tick()

    def _store(self):
        from p6mcp.remote.store import Store
        return Store()

    def poll_approvals(self) -> None:
        """Show an approval window for each new connection request; close the ones decided elsewhere or expired."""
        from p6mcp.remote.supervise import remote_wanted
        st = config.load()
        if not remote_wanted(st):
            self._pairing(None)
            return
        self._pairing(st)
        store = self._store()
        waiting = {r["id"]: r for r in store.pending_list()}
        for rid in [r for r in self.approvals if r not in waiting]:
            dlg = self.approvals.pop(rid)
            if dlg.isVisible():
                dlg.reject()
        for rid, req in waiting.items():
            if rid in self.approvals:
                continue
            from p6mcp.settings_app.approval import ApprovalDialog
            dlg = ApprovalDialog(req, bool(st.remote_allow_writes),
                                 lambda rid, code, ok, edits: self._store().decide_request(rid, code, ok, edits))
            self.approvals[rid] = dlg
            if self.tray is not None:
                self._last_message = "approve"
                self.tray.showMessage(tr("remote.approve.title"), tr("remote.approve.notify",
                                                                     client=req["client_name"]),
                                      QSystemTrayIcon.MessageIcon.Information, 8000)
            if self._show_on_arrival():
                self._present(dlg)
        if self.tray is not None:
            self.approve_act.setVisible(bool(self.approvals))

    def _show_on_arrival(self) -> bool:
        """The approval window waits for the browser page's Connect button (it then opens with the code filled in),
        the notification or the tray menu: it no longer covers the page as soon as a request arrives. Without a
        notification area there is no other way to reach it, so it opens at once."""
        return self.tray is None

    @staticmethod
    def _present(dlg) -> None:
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def show_approvals(self) -> None:
        """Tray menu or notification: open the approval window of every waiting request (to type the code)."""
        self.poll_approvals()
        for dlg in self.approvals.values():
            self._present(dlg)

    def _message_clicked(self) -> None:
        if self._last_message == "approve":
            self.show_approvals()

    def _pairing(self, st) -> None:
        """Run the Connect button listener on 127.0.0.1:remote_pair_port while remote access is wanted (a port in use
        is not retried until the setting changes: the page then falls back to the typed code)."""
        port = st.raw.get("remote_pair_port") if st is not None else None
        cur = self.pair_listener
        if cur is not None and cur.port != port:
            cur.stop()
            self.pair_listener = cur = None
        if port and cur is None:
            from p6mcp.remote.pairing import PairListener
            self.pair_listener = PairListener(int(port), self._public_origin,
                                              lambda rid, code, key: self._store().local_hint(rid, code, key),
                                              lambda req, code: self.paired.emit(req, code))
            self.pair_listener.start()

    @staticmethod
    def _public_origin():
        host = config.load().remote_public_host
        return f"https://{host}" if host else None

    def _paired(self, req: dict, code: str) -> None:
        """The browser page on this PC sent the code of a waiting request: fill it in its approval window."""
        if req["id"] not in self.approvals:
            self.poll_approvals()
        dlg = self.approvals.get(req["id"])
        if dlg is None:
            return
        dlg.prefill(code)
        self._present(dlg)

    def restart_server(self) -> None:
        self.supervisor.restart()
        self.tick()

    # -- window
    def keep_running(self) -> bool:
        """Stay in the tray after the window closes: needed to host HTTP, expected at startup."""
        if self.tray is None:
            return False
        st = config.load()
        return (st.transport == "http" or bool(st.raw.get("run_at_startup")) or bool(st.remote_enabled)
                or bool(st.raw.get("auto_update", True)))  # automatic updates run from the tray

    def show_window(self) -> None:
        if self.window is None:
            from p6mcp.settings_app.window import SettingsWindow
            self.window = SettingsWindow(supervisor=self.supervisor, on_close=self._window_closed,
                                         auto_update_check=True, updater=self.updater, remote=self.remote)
            self.window.server_state(self.supervisor.state)
            self.window.remote_state(self.remote.state)
        self.window.show()
        self.window.setWindowState(self.window.windowState() & ~Qt.WindowState.WindowMinimized)
        self.window.raise_()
        self.window.activateWindow()

    def _skills_updated(self, _names: list) -> None:
        """The updater saved newer shop skills: an open window without unsaved changes reads them (one with
        unsaved changes asks on Save, as for any change made by another program)."""
        w = self.window
        if w is not None and not w.load_error and not w.dirty():
            w.reload()

    def _window_closed(self) -> bool:
        """Called when the window accepted its close: True keeps the app (window hidden)."""
        if self.keep_running():
            if not self.notified and self.tray is not None:
                self._last_message = None
                self.tray.showMessage(tr("app.title"), tr("tray.still_running"),
                                      QSystemTrayIcon.MessageIcon.Information, 4000)
                self.notified = True
            return True
        self._shutdown()
        return False

    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def _other_instance(self) -> None:
        conn = self.server.nextPendingConnection()
        if conn is not None:
            conn.disconnected.connect(conn.deleteLater)
        self.show_window()

    def quit(self) -> None:
        """Tray menu Quit: asks about unsaved settings first."""
        if self.window is not None and self.window.isVisible() and not self.window.close_for_quit():
            return
        self._shutdown()

    def _quit_for_update(self, version: Optional[str]) -> None:
        """The installer replaces this app's files: quit (releasing the AppMutex); it starts the app again."""
        if self.tray is not None:
            self._last_message = None
            self.tray.showMessage(tr("app.title"), tr("upd.installing", latest=version or "?"),
                                  QSystemTrayIcon.MessageIcon.Information, 4000)
        if self.window is not None:
            self.window.hide()
        if self._mutex:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self._mutex)
            self._mutex = None
        self._shutdown()

    def _shutdown(self) -> None:
        self.timer.stop()
        self._pairing(None)
        self.supervisor.stop()
        self.remote.stop()
        if self.tray is not None:
            self.tray.hide()
        self.server.close()
        runtime.logging.getLogger("p6mcp.tray").info("tray app closed")
        self.app.quit()
