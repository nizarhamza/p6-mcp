"""Clients page: MCP clients found on this PC and the p6mcp entry in their config files.

Registering writes the client's own file at once (backup first, minimal edit); only the list of
clients whose offer was declined ('Do not offer again') waits for Save, like every other setting.
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from p6mcp import clients, credentials
from p6mcp.clients import files
from p6mcp.i18n import tr
from p6mcp.jsonc import JsoncError
from p6mcp.settings_app.common import Job, ask, group, note, pill, start

STATE_LEVEL = {"registered": "ok", "outdated": "warn", "error": "error", "not_registered": "info"}


class ClientsPage(QWidget):
    KINDS = (("stdio", "cl.snippet.stdio"), ("http", "cl.snippet.http"), ("vscode", "cl.snippet.vscode"))

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.statuses: list = []
        self.job: Optional[Job] = None
        lay = QVBoxLayout(self)
        lay.addWidget(note(tr("cl.intro")))

        box, bl = group(tr("cl.detected"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([tr("cl.col.client"), tr("cl.col.status"), tr("cl.col.file")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(self.fontMetrics().height() * 10)
        self.table.itemSelectionChanged.connect(self._show_selected)
        bl.addWidget(self.table)
        row = QHBoxLayout()
        self.reg_btn = QPushButton(tr("cl.register"))
        self.reg_btn.clicked.connect(self.register)
        self.unreg_btn = QPushButton(tr("cl.unregister"))
        self.unreg_btn.clicked.connect(self.unregister)
        self.ignore_btn = QPushButton(tr("cl.ignore"))
        self.ignore_btn.clicked.connect(self.toggle_ignore)
        self.open_btn = QPushButton(tr("cl.open_folder"))
        self.open_btn.clicked.connect(self._open_folder)
        self.scan_btn = QPushButton(tr("cl.rescan"))
        self.scan_btn.clicked.connect(self.rescan)
        for b in (self.reg_btn, self.unreg_btn, self.ignore_btn, self.open_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.scan_btn)
        bl.addLayout(row)
        self.state = QLabel()
        self.state.setWordWrap(True)
        bl.addWidget(self.state)
        lrow = QHBoxLayout()
        self.legacy = QLabel()
        self.legacy.setWordWrap(True)
        self.legacy_btn = QPushButton()
        self.legacy_btn.clicked.connect(self.remove_legacy)
        lrow.addWidget(self.legacy, 1)
        lrow.addWidget(self.legacy_btn)
        bl.addLayout(lrow)
        bl.addWidget(note(tr("cl.restart_note")))
        lay.addWidget(box)

        box, sl = group(tr("cl.snippet"))
        srow = QHBoxLayout()
        self.kind = QComboBox()
        for code, key in self.KINDS:
            self.kind.addItem(tr(key), code)
        self.kind.currentIndexChanged.connect(lambda _i: self.show_snippet())
        self.copy_btn = QPushButton(tr("common.copy"))
        self.copy_btn.clicked.connect(self.copy_snippet)
        srow.addWidget(self.kind, 1)
        srow.addWidget(self.copy_btn)
        sl.addLayout(srow)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.text.setMinimumHeight(self.fontMetrics().height() * 9)
        sl.addWidget(self.text)
        sl.addWidget(note(tr("cl.snippet.note")))
        lay.addWidget(box)
        lay.addStretch(1)
        self.show_snippet()
        self.rescan()

    # -- scanning
    def rescan(self) -> None:
        if self.job is not None:
            return
        self.scan_btn.setEnabled(False)
        data = dict(self.win.data)
        self.job = start(Job(lambda: clients.scan(http=self.target(data)), self._scanned))

    @staticmethod
    def target(data: dict):
        """HTTP target of these settings; without a reachable Credential Manager, stdio entries."""
        try:
            return clients.target(data)
        except credentials.SecretStoreError:
            return None

    def _scanned(self, res) -> None:
        self.job = None
        self.scan_btn.setEnabled(True)
        if isinstance(res, Exception):
            self.win.error(tr("cl.scan_failed", err=res))
            return
        self.statuses = res
        self.win.client_statuses = res
        self.refresh()
        self.win.page_changed()

    def refresh(self) -> None:
        keep = self.selected()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for st in self.statuses:
            r = self.table.rowCount()
            self.table.insertRow(r)
            name = QTableWidgetItem(st.name)
            name.setData(Qt.ItemDataRole.UserRole, st.client)
            self.table.setItem(r, 0, name)
            text = tr("cl.state." + st.state) + (" " + tr("cl.running") if st.running else "")
            self.table.setItem(r, 1, QTableWidgetItem(text))
            path = QTableWidgetItem(st.path or "")
            path.setToolTip(st.path or "")
            self.table.setItem(r, 2, path)
            if st.client == keep:
                self.table.selectRow(r)
        if self.selected() is None:
            first = next((r for r, st in enumerate(self.statuses) if st.state != clients.NOT_INSTALLED), None)
            if first is not None:
                self.table.selectRow(first)
        self.table.blockSignals(False)
        self._show_selected()

    def selected(self) -> Optional[str]:
        rows = self.table.selectionModel().selectedRows()
        item = self.table.item(rows[0].row(), 0) if rows else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def select(self, client_id: str) -> None:
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) == client_id:
                self.table.selectRow(r)

    def status(self):
        cid = self.selected()
        return next((s for s in self.statuses if s.client == cid), None)

    def _show_selected(self) -> None:
        st = self.status()
        installed = bool(st and st.state != clients.NOT_INSTALLED)
        self.reg_btn.setEnabled(bool(st and st.state in (clients.NOT_REGISTERED, clients.OUTDATED)))
        self.reg_btn.setText(tr("cl.repair") if st and st.state == clients.OUTDATED else tr("cl.register"))
        self.unreg_btn.setEnabled(bool(st and st.state in (clients.REGISTERED, clients.OUTDATED)))
        self.ignore_btn.setEnabled(bool(st and st.state == clients.NOT_REGISTERED))
        ignored = bool(st and st.client in self.win.data.get("clients_ignored", []))
        self.ignore_btn.setText(tr("cl.unignore") if ignored else tr("cl.ignore"))
        self.open_btn.setEnabled(installed)
        if st is None:
            self.state.clear()
            self.state.setStyleSheet("")
        else:
            text = tr("cl.detail." + st.state, name=st.name)
            if st.message:
                text += " " + st.message
            if st.running and installed:
                text += " " + tr("cl.detail.running", name=st.name)
            if installed and self.win.data.get("transport") == "http":
                ad = clients.get(st.client)
                if not ad.supports_http:
                    text += " " + tr("cl.detail.stdio_only", name=st.name)
                elif ad.token_in_file:
                    text += " " + tr("cl.detail.token_in_file", name=st.name)
                else:
                    text += " " + tr("cl.detail.token_prompt", name=st.name)
            pill(self.state, STATE_LEVEL.get(st.state, "idle"), text)
        others = st.others if st else []
        self.legacy.setVisible(bool(others))
        self.legacy_btn.setVisible(bool(others))
        if others:
            pill(self.legacy, "warn", tr("cl.legacy", name=st.name, keys=", ".join(others)))
            self.legacy_btn.setText(tr("cl.legacy.remove", key=others[0]))

    # -- actions on the client's file
    def _act(self, fn, done_key: str) -> bool:
        st = self.status()
        if st is None:
            return False
        try:
            res = fn(clients.get(st.client))
        except (ValueError, files.ClientFileError, JsoncError, OSError) as e:
            self.win.error(tr("cl.failed", name=st.name, err=e))
            return False
        msg = tr(done_key, name=st.name)
        if res.get("backup"):
            msg += " " + tr("cl.backup", path=res["backup"])
        self.win.flash(msg)
        self.rescan()
        return True

    def register(self) -> bool:
        http = self.target(self.win.data)
        return self._act(lambda ad: ad.register(http=http), "cl.done.register")

    def unregister(self) -> bool:
        st = self.status()
        if st and ask(self, tr("cl.unregister"), tr("cl.unregister.ask", name=st.name), warning=False):
            return self._act(lambda ad: ad.unregister(), "cl.done.unregister")
        return False

    def remove_legacy(self) -> bool:
        st = self.status()
        if not st or not st.others:
            return False
        key = st.others[0]
        if ask(self, tr("cl.legacy.title"), tr("cl.legacy.ask", key=key, name=st.name)):
            return self._act(lambda ad: ad.unregister(key), "cl.done.legacy")
        return False

    def toggle_ignore(self) -> None:
        st = self.status()
        if not st:
            return
        current = self.win.data.get("clients_ignored", [])
        ignored = [c for c in current if c != st.client] + ([] if st.client in current else [st.client])
        self.win.set_data(dict(self.win.data, clients_ignored=ignored))
        self._show_selected()

    def _open_folder(self) -> None:
        st = self.status()
        if st and st.path:
            folder = os.path.dirname(st.path)
            while folder and not os.path.isdir(folder):
                folder = os.path.dirname(folder)
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    # -- snippets for other clients
    def show_snippet(self) -> None:
        port = int(self.win.data.get("http_port", 39217))
        self.text.setPlainText(clients.snippet(self.kind.currentData(), port=port))

    def copy_snippet(self) -> str:
        text = self.text.toPlainText()
        if self.kind.currentData() == "http":
            try:
                text = clients.snippet("http", port=int(self.win.data.get("http_port", 39217)),
                                       token=credentials.http_token(create=True))
            except credentials.SecretStoreError as e:
                self.win.error(tr("srv.token.error", err=e))
        QGuiApplication.clipboard().setText(text)
        self.win.flash(tr("cl.snippet.copied"))
        return text
