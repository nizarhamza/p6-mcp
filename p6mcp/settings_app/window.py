"""MCP for Primavera P6 Settings: main window (navigation, Save / Revert, language, banners)."""

from __future__ import annotations

import copy
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QListWidget, QMainWindow, QMessageBox, QPushButton,
                               QScrollArea, QStackedWidget, QVBoxLayout, QWidget)

from p6mcp import __version__, config, core_skills, i18n, settings_logic, startup
from p6mcp.i18n import tr
from p6mcp.settings_app import a11y
from p6mcp.settings_app.common import brand_pixmap, muted, pill
from p6mcp.settings_app.about_pages import AboutPage, LogsPage
from p6mcp.settings_app.clients_page import ClientsPage
from p6mcp.settings_app.guidance_pages import InstructionsPage, SkillsPage
from p6mcp.settings_app.pages import DatabasePage, ServerPage, StartupPage, ToolsPage
from p6mcp.settings_app.remote_page import RemotePage
from p6mcp.settings_app.shop_page import ShopPage
from p6mcp.settings_app.tray import app_icon

PAGES = (("nav.database", DatabasePage), ("nav.startup", StartupPage), ("nav.server", ServerPage),
         ("nav.remote", RemotePage), ("nav.clients", ClientsPage), ("nav.tools", ToolsPage),
         ("nav.instructions", InstructionsPage),
         ("nav.skills", SkillsPage), ("nav.shop", ShopPage), ("nav.logs", LogsPage), ("nav.about", AboutPage))
CLIENTS_PAGE = [cls for _k, cls in PAGES].index(ClientsPage)
HEADER_HEIGHT = 48  # logical pixels of the logo header


class SettingsWindow(QMainWindow):
    def __init__(self, supervisor=None, on_close=None, auto_update_check: bool = False, updater=None, remote=None):
        """supervisor: the tray app's HTTP server supervisor (None: status read from the status file).
        remote: the tray app's RemoteHost (remote access server and tunnel; None: status from its status file).
        on_close: called when a close is accepted; returning True hides the window instead (tray).
        updater: the tray app's Updater (automatic updates and the Update button)."""
        super().__init__()
        self.supervisor, self.on_close, self.updater, self.remote = supervisor, on_close, updater, remote
        self.data, self.load_error = settings_logic.load_for_edit()
        self.saved: Optional[dict] = None if self.load_error else copy.deepcopy(self.data)
        self.disk = settings_logic.file_stamp()  # the config file as loaded or last saved here
        self.restart_keys: list[str] = []
        self.pages: list[QWidget] = []
        self.client_statuses: list = []  # filled by the Clients page scan (at every launch)
        i18n.set_language(self.data.get("language", "auto"))
        self.nav_shortcuts: list[QShortcut] = []
        # Ctrl+S saves (when there is something to save); created once, the window is rebuilt on a language change
        self.save_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Save), self)
        self.save_shortcut.activated.connect(self._save_key)
        a11y.FocusRing.install()
        self._watch_contrast()
        self.build()
        # the only network call, can be off; with the tray's updater the tray also checks on its own schedule
        if self.data.get("auto_update", True) and auto_update_check and (updater is None or updater.info is None):
            self.page(LogsPage).check()

    # -- layout (rebuilt when the language changes)
    def build(self, page: int = 0) -> None:
        self.setWindowTitle(tr("app.title"))
        self.setWindowIcon(app_icon())
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft if i18n.is_rtl() else Qt.LayoutDirection.LeftToRight)
        root = QWidget()
        lay = QVBoxLayout(root)
        self.header = QLabel()
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        logo = brand_pixmap("p6mcp-lockup-on-dark" if dark else "p6mcp-lockup", HEADER_HEIGHT,
                            self.devicePixelRatioF())
        if logo is not None:
            self.header.setPixmap(logo)
        self.header.setVisible(logo is not None)
        lay.addWidget(self.header, 0, Qt.AlignmentFlag.AlignLeft)
        self.banner = QLabel()  # read by _banner() when it appears or changes
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
        a11y.name(self.nav, tr("app.nav"))
        self.nav.setItemDelegate(a11y.MnemonicDelegate(self.nav))
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
        self.lang_label = QLabel(tr("app.language"))
        bottom.addWidget(self.lang_label)
        self.lang = QComboBox()
        self.lang_label.setBuddy(self.lang)
        self.lang.addItem(tr("app.language.auto"), "auto")
        for code in i18n.available():
            self.lang.addItem(i18n.NAMES.get(code, code), code)
        idx = self.lang.findData(self.data.get("language", "auto"))
        self.lang.setCurrentIndex(max(idx, 0))
        self.lang.currentIndexChanged.connect(self._language)
        bottom.addWidget(self.lang)
        bottom.addStretch(1)
        self.status = a11y.live(QLabel())
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
        self._access_keys()
        a11y.tie_forms(root)
        self.nav.setCurrentRow(page)
        fm = self.fontMetrics()
        # wide enough for the widest page without a horizontal scroll bar (sizes follow the font: large Windows
        # text), within the screen
        bar = self.style().pixelMetric(self.style().PixelMetric.PM_ScrollBarExtent, None, self)
        widest = max(p.minimumSizeHint().width() for p in self.pages) + bar + self.nav.width() + fm.height() * 3
        width = max(self.width(), fm.averageCharWidth() * 110, widest)
        height = max(self.height(), fm.height() * 40)
        screen = self.screen().availableGeometry() if self.screen() is not None else None
        if screen is not None:
            width, height = min(width, screen.width()), min(height, screen.height())
        self.resize(width, height)
        self.page_changed()

    # -- keyboard and screen readers
    def _access_keys(self) -> None:
        """Access keys (Alt+letter), chosen for the language shown: Save, Revert, the page list and Language first
        (they work on every page), then each page's main buttons in its order of importance (Page.access_keys():
        groups of widgets, the first one always shown, each later one shown alone, such as the data source panels
        of the Database page; the panels choose first, the first group takes the letters left). A letter is never
        used twice among the widgets shown together."""
        for sc in self.nav_shortcuts:
            sc.setEnabled(False)
            sc.setParent(None)
            sc.deleteLater()
        self.nav_shortcuts = []
        taken: set = set()
        a11y.give_keys([self.save_btn, self.revert_btn], taken)
        for row, (key, _cls) in enumerate(PAGES):
            text = a11y.with_key(tr(key), taken)
            item = self.nav.item(row)
            item.setData(a11y.MNEMONIC_ROLE, text)
            letter = a11y.mnemonic_of(text)
            if letter:
                item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, a11y.shortcut_text(letter))
                sc = QShortcut(QKeySequence(f"Alt+{letter.upper()}"), self)
                sc.activated.connect(lambda r=row: self.go_to(r))
                self.nav_shortcuts.append(sc)
        a11y.give_keys([self.lang_label], taken)
        self.global_keys = frozenset(taken)
        for page in self.pages:
            groups = page.access_keys() if hasattr(page, "access_keys") else []
            used = set(taken)
            for g in groups[1:]:
                mine = set(taken)
                a11y.give_keys(g, mine)
                used |= mine
            if groups:
                a11y.give_keys(groups[0], used)

    def go_to(self, row: int) -> None:
        """Show page row and put the keyboard on the page list (Alt + the page's letter)."""
        self.nav.setCurrentRow(row)
        self.nav.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _save_key(self) -> None:
        if self.save_btn.isEnabled():
            self.save()

    def _watch_contrast(self) -> None:
        """Windows high contrast turned on or off while the window is open: rebuilt with or without our colours."""
        try:
            from PySide6.QtGui import QGuiApplication
            self._a11y_hints = QGuiApplication.styleHints().accessibility()
            self._a11y_hints.contrastPreferenceChanged.connect(self._contrast_changed)
        except (AttributeError, RuntimeError):  # Qt before 6.10: read at the next start
            self._a11y_hints = None

    def _contrast_changed(self, *_args) -> None:
        a11y.apply_contrast_mode()
        self.build(self.nav.currentRow())

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

    def _update(self) -> Optional[dict]:
        return self.page(LogsPage).newer_release() if self.pages else None

    def _open_update(self) -> None:
        page = self.page(LogsPage)
        if self.updater is not None and self.updater.can_install:
            page.update_now()
        else:
            page._open_release()

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
        elif self.data.get("data_source") == "xer" and not (self.data.get("xer_files")
                                                             and self.data.get("xer_confirmed")):
            pill(self.banner, "warn", tr("app.no_xer"))
        elif self.data.get("data_source") == "eppm" and not (
                (settings_logic.find_eppm(self.data, self.data.get("eppm_active") or "") or {}).get("confirmed")):
            pill(self.banner, "warn", tr("app.no_eppm"))
        elif self.data.get("data_source") == "pmdb" and not (
                (settings_logic.find_pmdb(self.data, self.data.get("pmdb_active") or "") or {}).get("confirmed")):
            pill(self.banner, "warn", tr("app.no_pmdb"))
        elif self.data.get("data_source") not in ("xer", "eppm", "pmdb") and not (
                self.data.get("active_database") and settings_logic.find(self.data, self.data["active_database"])):
            pill(self.banner, "warn", tr("app.no_database"))
        elif self._update() is not None:
            self._set_banner_button("upd.now" if self.updater is not None and self.updater.can_install else "upd.open",
                                    self._open_update)
            pill(self.banner, "info", tr("upd.newer", latest=self._update()["latest"], current=__version__))
        elif bad:
            self._set_banner_button("app.clients.open", self.open_clients)
            pill(self.banner, "warn", tr("app.clients.repair", names=", ".join(s.name for s in bad)))
        elif new:
            self._set_banner_button("app.clients.open", self.open_clients)
            pill(self.banner, "info", tr("app.clients.new", names=", ".join(s.name for s in new)))
        else:
            self.banner.setVisible(False)
            self._banner_said = None
            return
        self.banner.setVisible(True)
        if self.isVisible() and self.banner.text() != getattr(self, "_banner_said", None):
            self._banner_said = self.banner.text()  # a new banner is read once
            a11y.announce(self.banner, self.banner.text())

    def page(self, cls):
        return next(p for p in self.pages if isinstance(p, cls))

    def server_state(self, st) -> None:
        """New HTTP server state from the tray app's supervisor."""
        self.page(ServerPage).set_state(st)

    def remote_state(self, st) -> None:
        """New remote access state from the tray app's RemoteHost."""
        self.page(RemotePage).set_state(st)

    def refresh_pages(self) -> None:
        for p in self.pages:
            p.refresh()
        self.page_changed()

    # -- actions
    def save(self) -> bool:
        if self.load_error:
            QMessageBox.warning(self, tr("app.title"), tr("app.broken.save_first"))
            return False
        if settings_logic.file_stamp() != self.disk:  # changed by someone else (a skill installed by Claude)
            answer = self.ask_changed()
            if answer == "reload":
                self.reload()
                return False
            if answer != "overwrite":
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
        # skills turned on or off, installed from the shop, edited, or the tools changed: the clients that read a
        # skills folder get the skills the server now offers (only the files that differ are written)
        refreshed = core_skills.refresh_clients(self.data)
        failed = [r for r in refreshed if r.get("error")]
        if any(r.get("error") or r["written"] or r["removed"] for r in refreshed):
            self.page(ClientsPage).rescan()  # its skills state was read before the folders changed
        if failed:
            self.error(tr("app.skills_refresh_failed", err="; ".join(f"{r['client']}: {r['error']}" for r in failed)))
        before = self.saved or {}
        self.restart_keys = sorted(set(self.restart_keys) | set(settings_logic.restart_needed(before, self.data)),
                                   key=settings_logic.RESTART_KEYS.index)
        self.saved = copy.deepcopy(self.data)
        self.disk = settings_logic.file_stamp()
        self.page(StartupPage).refresh()
        self.page_changed()
        self.flash(tr("app.saved"))
        return True

    def ask_changed(self) -> str:
        """'overwrite', 'reload' or 'cancel' when the file changed since this window read it (tests answer it)."""
        box = QMessageBox(QMessageBox.Icon.Warning, tr("app.title"), tr("app.changed.ask"),
                          QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Reset
                          | QMessageBox.StandardButton.Cancel, self)
        box.button(QMessageBox.StandardButton.Save).setText(tr("app.changed.overwrite"))
        box.button(QMessageBox.StandardButton.Reset).setText(tr("app.changed.reload"))
        box.button(QMessageBox.StandardButton.Cancel).setText(tr("common.cancel"))
        answer = box.exec()
        return {QMessageBox.StandardButton.Save: "overwrite",
                QMessageBox.StandardButton.Reset: "reload"}.get(answer, "cancel")

    def reload(self) -> None:
        """Read the settings file again (unsaved changes of this window are dropped)."""
        self.data, self.load_error = settings_logic.load_for_edit()
        self.saved = None if self.load_error else copy.deepcopy(self.data)
        self.disk = settings_logic.file_stamp()
        self.refresh_pages()
        self.flash(tr("app.changed.reloaded"))

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
        self.disk = settings_logic.file_stamp()
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
        """A short confirmation in the status bar, also read by screen readers."""
        self.statusBar().showMessage(text, 4000)
        a11y.announce(self.statusBar(), text)

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
