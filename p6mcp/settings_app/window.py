"""P6 MCP Settings: main window (navigation, Save / Revert, language, banners)."""

from __future__ import annotations

import copy
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QListWidget, QMainWindow, QMessageBox, QPushButton,
                               QScrollArea, QStackedWidget, QVBoxLayout, QWidget)

from p6mcp import __version__, config, i18n, settings_logic, startup
from p6mcp.i18n import tr
from p6mcp.settings_app.common import muted, pill
from p6mcp.settings_app.clients_page import ClientsPage
from p6mcp.settings_app.pages import DatabasePage, ServerPage, StartupPage, ToolsPage

PAGES = (("nav.database", DatabasePage), ("nav.startup", StartupPage), ("nav.server", ServerPage),
         ("nav.clients", ClientsPage), ("nav.tools", ToolsPage))
CLIENTS_PAGE = [cls for _k, cls in PAGES].index(ClientsPage)


class SettingsWindow(QMainWindow):
    def __init__(self, supervisor=None, on_close=None):
        """supervisor: the tray app's HTTP server supervisor (None: status read from the status file).
        on_close: called when a close is accepted; returning True hides the window instead (tray)."""
        super().__init__()
        self.supervisor, self.on_close = supervisor, on_close
        self.data, self.load_error = settings_logic.load_for_edit()
        self.saved: Optional[dict] = None if self.load_error else copy.deepcopy(self.data)
        self.restart_keys: list[str] = []
        self.pages: list[QWidget] = []
        self.client_statuses: list = []  # filled by the Clients page scan (at every launch)
        i18n.set_language(self.data.get("language", "auto"))
        self.build()

    # -- layout (rebuilt when the language changes)
    def build(self, page: int = 0) -> None:
        self.setWindowTitle(tr("app.title"))
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft if i18n.is_rtl() else Qt.LayoutDirection.LeftToRight)
        root = QWidget()
        lay = QVBoxLayout(root)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        self.banner_btn = QPushButton()
        self.banner_btn.clicked.connect(lambda: self._banner_action and self._banner_action())
        self._banner_action = None
        brow = QHBoxLayout()
        brow.addWidget(self.banner, 1)
        brow.addWidget(self.banner_btn)
        lay.addLayout(brow)

        body = QHBoxLayout()
        self.nav = QListWidget()
        self.stack = QStackedWidget()
        self.pages = []
        for key, cls in PAGES:
            self.nav.addItem(tr(key))
            page_w = cls(self)
            self.pages.append(page_w)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(page_w)
            self.stack.addWidget(scroll)
        fm = self.nav.fontMetrics()
        self.nav.setFixedWidth(max(fm.horizontalAdvance(tr(k)) for k, _ in PAGES) + fm.height() * 3)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        lay.addLayout(body, 1)

        bottom = QHBoxLayout()
        bottom.addWidget(QLabel(tr("app.language")))
        self.lang = QComboBox()
        self.lang.addItem(tr("app.language.auto"), "auto")
        for code in i18n.available():
            self.lang.addItem(i18n.NAMES.get(code, code), code)
        idx = self.lang.findData(self.data.get("language", "auto"))
        self.lang.setCurrentIndex(max(idx, 0))
        self.lang.currentIndexChanged.connect(self._language)
        bottom.addWidget(self.lang)
        bottom.addStretch(1)
        self.status = QLabel()
        bottom.addWidget(self.status)
        self.revert_btn = QPushButton(tr("app.revert"))
        self.revert_btn.clicked.connect(self.revert)
        self.save_btn = QPushButton(tr("app.save"))
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.save)
        bottom.addWidget(self.revert_btn)
        bottom.addWidget(self.save_btn)
        lay.addLayout(bottom)
        lay.addWidget(muted(QLabel(tr("app.footer", version=__version__))))

        self.setCentralWidget(root)
        self.nav.setCurrentRow(page)
        fm = self.fontMetrics()
        self.resize(max(self.width(), fm.averageCharWidth() * 110), max(self.height(), fm.height() * 40))
        self.page_changed()

    # -- state
    def dirty(self) -> bool:
        return self.saved is None or self.data != self.saved

    def set_data(self, data: dict) -> None:
        self.data = data
        self.page_changed()

    def page_changed(self) -> None:
        dirty = self.dirty()
        self.save_btn.setEnabled(dirty)
        self.revert_btn.setEnabled(dirty and self.saved is not None)
        if dirty:
            pill(self.status, "warn", tr("app.unsaved"))
        else:
            self.status.setText(tr("app.saved"))
            self.status.setStyleSheet("")
        self._banner()

    def client_offers(self) -> tuple[list, list]:
        """(clients installed but not registered and not declined, clients whose entry needs repair)."""
        ignored = set(self.data.get("clients_ignored", []))
        new = [s for s in self.client_statuses if s.state == "not_registered" and s.client not in ignored]
        bad = [s for s in self.client_statuses if s.state in ("outdated", "error")]
        return new, bad

    def _set_banner_button(self, key: Optional[str], action=None) -> None:
        self._banner_action = action
        self.banner_btn.setVisible(key is not None)
        if key:
            self.banner_btn.setText(tr(key))

    def open_clients(self) -> None:
        self.nav.setCurrentRow(CLIENTS_PAGE)

    def _banner(self) -> None:
        self._set_banner_button(None)
        new, bad = self.client_offers()
        if self.load_error:
            self._set_banner_button("app.reset_broken", self.reset_broken)
            pill(self.banner, "error", tr("app.broken", err=self.load_error))
        elif self.restart_keys:
            names = ", ".join(tr("restart." + k) for k in self.restart_keys)
            pill(self.banner, "info", tr("app.restart", what=names))
        elif not (self.data.get("active_database") and settings_logic.find(self.data, self.data["active_database"])):
            pill(self.banner, "warn", tr("app.no_database"))
        elif bad:
            self._set_banner_button("app.clients.open", self.open_clients)
            pill(self.banner, "warn", tr("app.clients.repair", names=", ".join(s.name for s in bad)))
        elif new:
            self._set_banner_button("app.clients.open", self.open_clients)
            pill(self.banner, "info", tr("app.clients.new", names=", ".join(s.name for s in new)))
        else:
            self.banner.setVisible(False)
            return
        self.banner.setVisible(True)

    def page(self, cls):
        return next(p for p in self.pages if isinstance(p, cls))

    def server_state(self, st) -> None:
        """New HTTP server state from the tray app's supervisor."""
        self.page(ServerPage).set_state(st)

    def refresh_pages(self) -> None:
        for p in self.pages:
            p.refresh()
        self.page_changed()

    # -- actions
    def save(self) -> bool:
        if self.load_error:
            QMessageBox.warning(self, tr("app.title"), tr("app.broken.save_first"))
            return False
        try:
            config.save(self.data)
        except (config.ConfigError, OSError) as e:
            self.error(tr("app.save_failed", err=e))
            return False
        try:
            startup.sync(self.data)
        except OSError as e:  # the settings are saved; only the startup entry failed
            self.error(tr("st.sync_failed", err=e))
        before = self.saved or {}
        self.restart_keys = sorted(set(self.restart_keys) | set(settings_logic.restart_needed(before, self.data)),
                                   key=settings_logic.RESTART_KEYS.index)
        self.saved = copy.deepcopy(self.data)
        self.page(StartupPage).refresh()
        self.page_changed()
        self.flash(tr("app.saved"))
        return True

    def revert(self) -> None:
        if self.saved is not None:
            self.data = copy.deepcopy(self.saved)
            self.refresh_pages()

    def reset_broken(self) -> None:
        if not self.load_error:
            return
        try:
            moved = settings_logic.set_aside_broken_file()
        except OSError as e:
            self.error(str(e))
            return
        self.load_error = None
        self.saved = None
        self.data = copy.deepcopy(config.DEFAULTS)
        self.flash(tr("app.broken.moved", path=moved or ""))
        self.refresh_pages()

    def _language(self, _index: int) -> None:
        code = self.lang.currentData()
        self.data = dict(self.data, language=code)
        i18n.set_language(code)
        self.build(self.nav.currentRow())

    def error(self, text: str) -> None:
        QMessageBox.warning(self, tr("app.title"), text)

    def flash(self, text: str) -> None:
        self.statusBar().showMessage(text, 4000)

    def close_for_quit(self) -> bool:
        """Before the app quits: False when the user cancels (unsaved changes)."""
        if not self.dirty() or self.load_error:
            return True
        answer = self.ask_close()
        return (answer == "save" and self.save()) or answer == "discard"

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.close_for_quit():
            event.ignore()
            return
        if self.on_close is not None and self.on_close():
            if self.dirty() and self.saved is not None:  # discarded: start from the saved settings next time
                self.data = copy.deepcopy(self.saved)
                self.refresh_pages()
            event.ignore()
            self.hide()
            return
        event.accept()

    def ask_close(self) -> str:
        """'save', 'discard' or 'cancel' (a separate method so tests can answer it)."""
        box = QMessageBox(QMessageBox.Icon.Question, tr("app.title"), tr("app.close.ask"),
                          QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                          | QMessageBox.StandardButton.Cancel, self)
        box.button(QMessageBox.StandardButton.Save).setText(tr("app.save"))
        box.button(QMessageBox.StandardButton.Discard).setText(tr("app.discard"))
        box.button(QMessageBox.StandardButton.Cancel).setText(tr("common.cancel"))
        answer = box.exec()
        return {QMessageBox.StandardButton.Save: "save",
                QMessageBox.StandardButton.Discard: "discard"}.get(answer, "cancel")
