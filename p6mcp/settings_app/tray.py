"""Notification area (tray) app: one instance per user, hosts and supervises the HTTP server, opens
the Settings window. Started by p6mcp-settings (with --minimized at Windows startup)."""

from __future__ import annotations

import getpass
from typing import Optional

from PySide6.QtCore import QObject, QRect, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from p6mcp import config, runtime, supervisor
from p6mcp.i18n import tr

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


def badge(size: int, dot: Optional[str] = None) -> QPixmap:
    """The app's "P6" badge (also the source of the .ico and the bundle icon)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#1f4e79"))
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


def make_icon(state: str = supervisor.DISABLED) -> QIcon:
    """A small "P6" badge; its dot shows the HTTP server state (green running, red error)."""
    icon = QIcon()
    dot = {supervisor.RUNNING: "#2e9d57", supervisor.ERROR: "#c62828", supervisor.STARTING: "#e0a000"}.get(state)
    for size in (16, 20, 24, 32, 40, 48, 64):
        icon.addPixmap(badge(size, dot))
    return icon


class TrayApp(QObject):
    def __init__(self, app: QApplication, sup: Optional[supervisor.Supervisor] = None):
        super().__init__()
        self.app = app
        self.supervisor = sup or supervisor.Supervisor()
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
            for a in (self.open_act, self.status_act, self.restart_act):
                menu.addAction(a)
            menu.addSeparator()
            menu.addAction(self.quit_act)
            self.menu = menu
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._activated)
            self.tray.show()
            app.setQuitOnLastWindowClosed(False)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(TICK_MS)
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
        if self.tray is not None:
            text = self.status_text(st)
            self.status_act.setText(text)
            self.restart_act.setEnabled(st.state != supervisor.DISABLED)
            self.tray.setToolTip(tr("app.title") + "\n" + text)
            self.tray.setIcon(make_icon(st.state))
        if self.window is not None:
            self.window.server_state(st)
        return st

    def restart_server(self) -> None:
        self.supervisor.restart()
        self.tick()

    # -- window
    def keep_running(self) -> bool:
        """Stay in the tray after the window closes: needed to host HTTP, expected at startup."""
        if self.tray is None:
            return False
        st = config.load()
        return st.transport == "http" or bool(st.raw.get("run_at_startup"))

    def show_window(self) -> None:
        if self.window is None:
            from p6mcp.settings_app.window import SettingsWindow
            self.window = SettingsWindow(supervisor=self.supervisor, on_close=self._window_closed,
                                         auto_update_check=True)
            self.window.server_state(self.supervisor.state)
        self.window.show()
        self.window.setWindowState(self.window.windowState() & ~Qt.WindowState.WindowMinimized)
        self.window.raise_()
        self.window.activateWindow()

    def _window_closed(self) -> bool:
        """Called when the window accepted its close: True keeps the app (window hidden)."""
        if self.keep_running():
            if not self.notified and self.tray is not None:
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

    def _shutdown(self) -> None:
        self.timer.stop()
        self.supervisor.stop()
        if self.tray is not None:
            self.tray.hide()
        self.server.close()
        runtime.logging.getLogger("p6mcp.tray").info("tray app closed")
        self.app.quit()
