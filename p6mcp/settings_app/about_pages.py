"""Logs and updates page, About page."""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

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
        self.auto.clicked.connect(lambda c: self.win.set_data(dict(self.win.data, update_check=bool(c))))
        ul.addWidget(self.auto)
        row = QHBoxLayout()
        self.check_btn = QPushButton(tr("upd.check"))
        self.check_btn.clicked.connect(self.check)
        self.open_btn = QPushButton(tr("upd.open"))
        self.open_btn.clicked.connect(self._open_release)
        row.addWidget(self.check_btn)
        row.addWidget(self.open_btn)
        row.addStretch(1)
        ul.addLayout(row)
        self.state = QLabel()
        self.state.setWordWrap(True)
        ul.addWidget(self.state)
        ul.addWidget(note(tr("upd.note")))
        lay.addWidget(box)
        lay.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        d = self.win.data
        self.level.blockSignals(True)
        self.level.setCurrentIndex(max(0, self.level.findData(d.get("log_level", "INFO"))))
        self.level.blockSignals(False)
        self.auto.setChecked(bool(d.get("update_check", True)))
        self._render()

    def _level(self, _i: int) -> None:
        self.win.set_data(dict(self.win.data, log_level=self.level.currentData()))

    def check(self) -> None:
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

    def _render(self) -> None:
        r = self.result
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

    def _open_release(self) -> None:
        if self.result and self.result.get("url"):
            QDesktopServices.openUrl(QUrl(self.result["url"]))


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
