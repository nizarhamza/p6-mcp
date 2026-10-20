"""Logs and updates page, About page."""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QVBoxLayout, QWidget)

from p6mcp import __version__, config, runtime, updates
from p6mcp.i18n import tr
from p6mcp.settings_app.common import Job, group, note, pill, start


def _open_folder(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))


class LogsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.job: Optional[Job] = None
        self.result: Optional[dict] = None
        lay = QVBoxLayout(self)

        box, bl = group(tr("logs.box"))
        form = QFormLayout()
        self.level = QComboBox()
        for lv in config.LOG_LEVELS:
            self.level.addItem(tr("logs.level." + lv.lower()), lv)
        self.level.currentIndexChanged.connect(self._level)
        form.addRow(tr("logs.level"), self.level)
        bl.addLayout(form)
        row = QHBoxLayout()
        for key, path in (("logs.open", runtime.logs_dir()), ("logs.open_data", config.localdata_dir()),
                          ("logs.open_config", os.path.dirname(config.config_path()))):
            b = QPushButton(tr(key))
            b.clicked.connect(lambda _c=False, p=path: _open_folder(p))
            row.addWidget(b)
        row.addStretch(1)
        bl.addLayout(row)
        bl.addWidget(note(tr("logs.note")))
        lay.addWidget(box)

        box, ul = group(tr("upd.box"))
        self.auto = QCheckBox(tr("upd.auto"))
        self.auto.clicked.connect(lambda c: self.win.set_data(dict(self.win.data, auto_update=bool(c))))
        ul.addWidget(self.auto)
        row = QHBoxLayout()
        self.check_btn = QPushButton(tr("upd.check"))
        self.check_btn.clicked.connect(self.check)
        self.update_btn = QPushButton(tr("upd.now"))
        self.update_btn.clicked.connect(self.update_now)
        self.open_btn = QPushButton(tr("upd.open"))
        self.open_btn.clicked.connect(self._open_release)
        row.addWidget(self.check_btn)
        row.addWidget(self.update_btn)
        row.addWidget(self.open_btn)
        row.addStretch(1)
        ul.addLayout(row)
        self.state = QLabel()
        self.state.setWordWrap(True)
        ul.addWidget(self.state)
        ul.addWidget(note(tr("upd.note")))
        lay.addWidget(box)
        lay.addStretch(1)
        self.updater = getattr(win, "updater", None)  # the tray's Updater (None: check only)
        if self.updater is not None:
            self.updater.changed.connect(self._updater_changed)
        self.refresh()

    def _updater_changed(self) -> None:
        self._render()
        self.win.page_changed()

    def refresh(self) -> None:
        d = self.win.data
        self.level.blockSignals(True)
        self.level.setCurrentIndex(max(0, self.level.findData(d.get("log_level", "INFO"))))
        self.level.blockSignals(False)
        self.auto.setChecked(bool(d.get("auto_update", True)))
        self._render()

    def _level(self, _i: int) -> None:
        self.win.set_data(dict(self.win.data, log_level=self.level.currentData()))

    def check(self) -> None:
        if self.updater is not None:
            self.updater.check()
            return
        if self.job is not None:
            return
        self.check_btn.setEnabled(False)
        pill(self.state, "info", tr("upd.checking"))
        self.job = start(Job(updates.check, self._checked))

    def _checked(self, res) -> None:
        self.job = None
        self.check_btn.setEnabled(True)
        self.result = res if isinstance(res, dict) else {"error": str(res), "newer": False}
        self._render()
        self.win.page_changed()

    def update_now(self) -> None:
        if self.updater is not None:
            self.updater.update_now(self._ask_clients)

    def _ask_clients(self, clients: list[dict]) -> Optional[bool]:
        """MCP clients run P6 MCP: True = close them and update now, False = update when they close."""
        names = ", ".join(sorted({os.path.splitext(c["client"])[0] for c in clients}))
        box = QMessageBox(QMessageBox.Icon.Question, tr("upd.box"), tr("upd.in_use", names=names), parent=self)
        now = box.addButton(tr("upd.close_now"), QMessageBox.ButtonRole.AcceptRole)
        later = box.addButton(tr("upd.when_closed"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(tr("common.cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(later)
        box.exec()
        clicked = box.clickedButton()
        return True if clicked is now else False if clicked is later else None

    def _render(self) -> None:
        if self.updater is not None:
            self._render_updater()
            return
        r = self.result
        self.update_btn.setVisible(False)
        self.open_btn.setVisible(bool(r and r.get("newer")))
        if r is None:
            self.state.clear()
            self.state.setStyleSheet("")
        elif r.get("newer"):
            pill(self.state, "info", tr("upd.newer", latest=r["latest"], current=__version__))
        elif r.get("error"):
            pill(self.state, "warn", tr("upd.failed", err=r["error"]))
        else:
            pill(self.state, "ok", tr("upd.latest", current=__version__))

    def _render_updater(self) -> None:
        from p6mcp.settings_app import updater as U
        u = self.updater
        busy = u.state in (U.CHECKING, U.DOWNLOADING, U.INSTALLING)
        self.check_btn.setEnabled(not busy)
        self.update_btn.setVisible(u.newer and u.can_install and u.state != U.INSTALLING)
        self.update_btn.setEnabled(not busy)
        self.open_btn.setVisible(u.newer and not u.can_install)
        latest = u.latest or "?"
        if u.state == U.CHECKING:
            pill(self.state, "info", tr("upd.checking"))
        elif u.state == U.DOWNLOADING:
            pill(self.state, "info", tr("upd.downloading", latest=latest))
        elif u.state == U.WAITING:
            names = ", ".join(sorted({os.path.splitext(c["client"])[0] for c in u.clients}))
            pill(self.state, "warn", tr("upd.waiting", latest=latest, names=names))
        elif u.state == U.INSTALLING:
            pill(self.state, "info", tr("upd.installing", latest=latest))
        elif u.state == U.ERROR:
            pill(self.state, "warn", tr("upd.failed", err=u.error or "?"))
        elif u.state == U.LATEST:
            pill(self.state, "ok", tr("upd.latest", current=__version__))
        elif u.newer:
            pill(self.state, "info", tr("upd.newer", latest=latest, current=__version__)
                 + ("" if u.can_install else " " + tr("upd.manual")))
        else:
            self.state.clear()
            self.state.setStyleSheet("")

    def newer_release(self) -> Optional[dict]:
        """The newer release found (by the updater or by this page's own check), else None."""
        if self.updater is not None:
            return self.updater.info if self.updater.newer else None
        r = self.result
        return r if r and r.get("newer") else None

    def _open_release(self) -> None:
        r = self.updater.info if self.updater is not None else self.result
        if r and r.get("url"):
            QDesktopServices.openUrl(QUrl(r["url"]))


class AboutPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        title = QLabel(f"<h2>P6 MCP {__version__}</h2>")
        lay.addWidget(title)
        lay.addWidget(note(tr("about.tagline")))
        box, bl = group(tr("about.privacy"))
        priv = QLabel(tr("about.privacy.text"))
        priv.setWordWrap(True)
        bl.addWidget(priv)
        lay.addWidget(box)
        box, ll = group(tr("about.project"))
        link = QLabel(f'<a href="{updates.REPO_URL}">{updates.REPO_URL}</a>')
        link.setOpenExternalLinks(True)
        link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        ll.addWidget(link)
        lic = QLabel(tr("about.license"))
        lic.setWordWrap(True)
        ll.addWidget(lic)
        ll.addWidget(note(tr("about.trademark")))
        lay.addWidget(box)
        lay.addStretch(1)

    def refresh(self) -> None:
        pass
