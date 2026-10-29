"""Automatic and one-click updates, run by the tray app (the only process that stays up).

With auto_update on (default), the tray checks the latest release a minute after it starts and then
every CHECK_EVERY_H hours. A newer installed-version release is downloaded and verified (SHA256SUMS),
then installed silently as soon as no MCP client uses MCP for Primavera P6 (no p6mcp-server.exe started by a client:
the files of a running server cannot be replaced). The tray app quits for the install and the
installer starts it again. The Update button of the Settings window does the same at once, and can
close the clients' servers when the user agrees.

On the same schedule, auto_update also brings the skills installed from the skill shop to the shop's
version, except the ones edited here (p6mcp.core_skills.run_updates: settings saved, clients' skill
folders rewritten); skills_changed tells the tray, which reloads an open window without unsaved changes.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from p6mcp import __version__, config, core_skills, updates
from p6mcp.settings_app.common import Job, start

log = logging.getLogger("p6mcp.updater")
FIRST_CHECK_MS = 60_000
CHECK_EVERY_H = 6
WAIT_POLL_MS = 60_000

IDLE, CHECKING, LATEST, DOWNLOADING, WAITING, INSTALLING, ERROR, MANUAL = (
    "idle", "checking", "latest", "downloading", "waiting", "installing", "error", "manual")


def auto_enabled() -> bool:
    try:
        return bool(config.read_file()[0].get("auto_update", config.DEFAULTS["auto_update"]))
    except Exception:  # an unreadable config: keep the default
        return bool(config.DEFAULTS["auto_update"])


class Updater(QObject):
    """state: idle, checking, latest, downloading, waiting (clients use MCP for Primavera P6), installing, error,
    manual (a newer release exists but this copy cannot install it: portable or source run).
    changed is emitted on every state change; info is the last check() result."""

    changed = Signal()
    skills_changed = Signal(list)  # names of the skills the automatic skill update installed and saved

    def __init__(self, before_install: Callable[[str], None], parent=None, can_install: Optional[bool] = None,
                 auto: Callable[[], bool] = auto_enabled):
        super().__init__(parent)
        self.before_install = before_install  # the tray: release the mutex, stop the HTTP server, quit
        self.can_install = updates.can_self_update() if can_install is None else can_install
        self.auto = auto
        self.state, self.info, self.error = IDLE, None, None
        self.installer: Optional[str] = None
        self.clients: list[dict] = []
        self.install_when_ready = False  # set by auto mode or by a manual Update deferred until clients close
        self._ask: Optional[Callable[[list[dict]], Optional[bool]]] = None  # the Update button's question
        self.job: Optional[Job] = None
        self.skills_job: Optional[Job] = None
        self.skills_report: Optional[dict] = None  # the last run_updates() result
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._periodic)
        self.wait_timer = QTimer(self)
        self.wait_timer.timeout.connect(self._try_install)

    def start(self) -> None:
        QTimer.singleShot(FIRST_CHECK_MS, self._periodic)
        self.timer.start(CHECK_EVERY_H * 3600 * 1000)

    def _set(self, state: str, error: Optional[str] = None) -> None:
        self.state, self.error = state, error
        self.changed.emit()

    @property
    def latest(self) -> Optional[str]:
        return (self.info or {}).get("latest")

    @property
    def newer(self) -> bool:
        return bool(self.info and self.info.get("newer"))

    # -- steps
    def _periodic(self) -> None:
        if not self.auto():
            return
        if self.state in (IDLE, LATEST, ERROR, MANUAL):
            self.check(install=True)
        self.update_skills()

    def update_skills(self) -> None:
        """Install the newer shop versions of the shop skills not edited here (shop turned off: nothing)."""
        if self.skills_job is None:
            self.skills_job = start(Job(lambda: core_skills.run_updates(), self._skills_updated))

    def _skills_updated(self, res) -> None:
        self.skills_job = None
        if not isinstance(res, dict):
            log.warning("skill update failed: %s", res)
            return
        self.skills_report = res
        if res.get("edited"):
            log.info("skills edited here, not updated: %s", ", ".join(res["edited"]))
        if res.get("failed") or (res.get("error") and not res.get("skipped")):
            log.warning("skill update: %s", res.get("failed") or res.get("error"))
        if res.get("updated"):
            log.info("skills updated from the shop: %s", ", ".join(res["updated"]))
            self.skills_changed.emit(list(res["updated"]))

    def check(self, install: bool = False) -> None:
        """Ask GitHub for the latest release; install=True goes on to download and install it."""
        if self.job is not None or self.state in (DOWNLOADING, INSTALLING):
            return
        self.install_when_ready = self.install_when_ready or install
        self._set(CHECKING)
        self.job = start(Job(updates.check, self._checked))

    def _checked(self, res) -> None:
        self.job = None
        if not isinstance(res, dict) or res.get("error"):
            self.install_when_ready, self._ask = False, None
            self._set(ERROR, res.get("error") if isinstance(res, dict) else str(res))
            return
        self.info = res
        if not res.get("newer"):
            self.install_when_ready = False
            self._set(LATEST)
        elif not self.can_install:
            self.install_when_ready = False
            self._set(MANUAL)
        elif self.install_when_ready:
            self._download()
        else:
            self._set(IDLE)

    def _download(self) -> None:
        self._set(DOWNLOADING)
        info = self.info
        self.job = start(Job(lambda: updates.download(info), self._downloaded))

    def _downloaded(self, res) -> None:
        self.job = None
        if not isinstance(res, str):
            self.install_when_ready, self._ask = False, None
            log.warning("update download failed: %s", res)
            self._set(ERROR, str(res))
            return
        self.installer = res
        log.info("update %s downloaded and verified: %s", self.latest, res)
        (self._manual_install if self._ask is not None else self._try_install)()

    def _try_install(self) -> None:
        """Install now when no MCP client runs a MCP for Primavera P6 server; else wait and look again."""
        self.clients = updates.client_servers()
        if self.clients:
            if self.state != WAITING:
                log.info("update %s waits: MCP for Primavera P6 in use by %s", self.latest,
                         ", ".join(sorted({c["client"] for c in self.clients})))
            self._set(WAITING)
            if not self.wait_timer.isActive():
                self.wait_timer.start(WAIT_POLL_MS)
            return
        self.wait_timer.stop()
        self._install()

    def _install(self) -> None:
        try:
            setup_log = updates.launch_install(self.installer)
        except Exception as e:
            self._set(ERROR, f"{type(e).__name__}: {e}")
            return
        log.info("installing MCP for Primavera P6 %s over %s (setup log %s)", self.latest, __version__, setup_log)
        self._set(INSTALLING)
        self.before_install(self.latest)

    # -- the Settings window's Update button
    def update_now(self, close_clients: Callable[[list[dict]], Optional[bool]]) -> None:
        """Download if needed and install. When MCP clients use MCP for Primavera P6, close_clients(clients) decides:
        True stops their servers and installs, False installs once they close, None cancels."""
        if self.state in (DOWNLOADING, INSTALLING, CHECKING) or not self.can_install:
            return
        self.install_when_ready = True
        self._ask = close_clients
        if self.installer is None:
            if self.newer:
                self._download()
            else:
                self.check(install=True)
            return
        self._manual_install()

    def _manual_install(self) -> None:
        clients, ask = updates.client_servers(), self._ask
        self._ask = None
        if clients and ask is not None:
            choice = ask(clients)
            if choice is None:
                self.install_when_ready = False
                self._set(IDLE)
                return
            if choice:
                updates.stop_servers([c["pid"] for c in clients])
        self._try_install()
