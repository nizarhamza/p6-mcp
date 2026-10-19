"""The Database, Server and Tools pages of the Settings window.

Each page edits win.data (the config dict) through win.set_data(); nothing is written to disk until
the user clicks Save in the window.
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QPushButton, QRadioButton,
                               QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from p6mcp import config, credentials, dbcheck, policy, settings_logic
from p6mcp.i18n import tr
from p6mcp.settings_app.common import Job, ask, group, note, pill, start


def _mb(n: Optional[int]) -> str:
    if n is None:
        return ""
    v = f"{n / 1048576:,.1f}".replace(",", " ").replace(".", tr("num.decimal"))
    return tr("unit.mb", v=v)


# ----------------------------------------------------------------- Database

class DatabasePage(QWidget):
    COLS = ("db.col.name", "db.col.file", "db.col.status")

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.results: dict[str, dict] = {}  # last test result per database name
        self.job: Optional[Job] = None
        lay = QVBoxLayout(self)
        lay.addWidget(note(tr("db.intro")))

        box, bl = group(tr("db.profiles"))
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels([tr(c) for c in self.COLS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._show_selected)
        bl.addWidget(self.table)
        row = QHBoxLayout()
        self.add_btn = QPushButton(tr("db.add"))
        self.add_menu = QMenu(self)
        self.add_btn.setMenu(self.add_menu)
        self.add_menu.aboutToShow.connect(self._fill_add_menu)
        self.remove_btn = QPushButton(tr("db.remove"))
        self.remove_btn.clicked.connect(self._remove)
        self.active_btn = QPushButton(tr("db.make_active"))
        self.active_btn.clicked.connect(self._make_active)
        for b in (self.add_btn, self.remove_btn, self.active_btn):
            row.addWidget(b)
        row.addStretch(1)
        bl.addLayout(row)
        lay.addWidget(box)

        box, dl = group(tr("db.selected"))
        self.detail = box
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.editingFinished.connect(self._rename)
        form.addRow(tr("db.name"), self.name_edit)
        self.kind = QComboBox()
        self.kind.addItem(tr("db.kind.sqlite"), "sqlite")
        for key in ("db.kind.sqlserver", "db.kind.oracle"):
            self.kind.addItem(tr(key) + " " + tr("db.kind.later"))
            self.kind.model().item(self.kind.count() - 1).setEnabled(False)
        form.addRow(tr("db.kind"), self.kind)
        prow = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText(tr("db.path.placeholder"))
        self.path_edit.editingFinished.connect(self._repoint)
        browse = QPushButton(tr("common.browse"))
        browse.clicked.connect(self._browse_path)
        prow.addWidget(self.path_edit, 1)
        prow.addWidget(browse)
        form.addRow(tr("db.file"), prow)
        dl.addLayout(form)
        trow = QHBoxLayout()
        self.test_btn = QPushButton(tr("db.test"))
        self.test_btn.clicked.connect(self.run_test)
        self.state = QLabel()
        trow.addWidget(self.test_btn)
        trow.addWidget(self.state, 1)
        dl.addLayout(trow)
        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.RichText)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        dl.addWidget(self.result)
        self.trust = QCheckBox(tr("db.trust"))
        self.trust.clicked.connect(self._trust_clicked)
        dl.addWidget(self.trust)
        crow = QHBoxLayout()
        self.use_btn = QPushButton(tr("db.use"))
        self.use_btn.clicked.connect(self._confirm)
        crow.addWidget(self.use_btn)
        crow.addStretch(1)
        dl.addLayout(crow)
        lay.addWidget(box)
        lay.addStretch(1)
        self.refresh()

    # -- table
    def selected(self) -> Optional[str]:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def refresh(self, select: Optional[str] = None) -> None:
        keep = select or self.selected()
        data = self.win.data
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for d in data.get("databases", []):
            r = self.table.rowCount()
            self.table.insertRow(r)
            name = QTableWidgetItem(d["name"])
            name.setData(Qt.ItemDataRole.UserRole, d["name"])
            if d["name"] == data.get("active_database"):
                f = name.font()
                f.setBold(True)
                name.setFont(f)
                status = tr("db.status.active")
            else:
                status = tr("db.status.confirmed") if d.get("confirmed") else tr("db.status.untested")
            self.table.setItem(r, 0, name)
            path = QTableWidgetItem(d.get("path", ""))
            path.setToolTip(d.get("path", ""))
            self.table.setItem(r, 1, path)
            self.table.setItem(r, 2, QTableWidgetItem(status))
            if d["name"] == keep:
                self.table.selectRow(r)
        if self.selected() is None and self.table.rowCount():
            self.table.selectRow(0)
        self.table.blockSignals(False)
        self._show_selected()

    def _entry(self) -> Optional[dict]:
        name = self.selected()
        return settings_logic.find(self.win.data, name) if name else None

    def _show_selected(self) -> None:
        e = self._entry()
        self.detail.setEnabled(e is not None)
        self.remove_btn.setEnabled(e is not None)
        self.active_btn.setEnabled(bool(e and e.get("confirmed") and e["name"] != self.win.data.get("active_database")))
        self.name_edit.setText(e["name"] if e else "")
        self.path_edit.setText(e.get("path", "") if e else "")
        self._show_result()

    # -- adding
    def _fill_add_menu(self) -> None:
        self.add_menu.clear()
        self.add_menu.addAction(tr("db.add.browse"), lambda: self._add(self._pick_file()))
        sugg = dbcheck.suggestions()
        if sugg:
            self.add_menu.addSection(tr("db.add.suggested"))
        known = {os.path.normcase(os.path.abspath(d.get("path", ""))) for d in self.win.data.get("databases", [])}
        for s in sugg:
            size = _mb(s["size_bytes"]) if s["exists"] else tr("db.missing")
            label = f"{s['path']}  ({s['source']}, {size})"
            act = self.add_menu.addAction(label, lambda p=s["path"]: self._add(p))
            act.setEnabled(s["exists"] and os.path.normcase(s["path"]) not in known)

    def _pick_file(self) -> Optional[str]:
        start_dir = os.path.dirname(self.path_edit.text()) or os.path.join(os.path.expanduser("~"), "Documents")
        path, _ = QFileDialog.getOpenFileName(self, tr("db.pick.title"), start_dir, tr("db.pick.filter"))
        return path or None

    def _add(self, path: Optional[str]) -> None:
        if not path:
            return
        data, name = settings_logic.add_database(self.win.data, os.path.normpath(path))
        self.win.set_data(data)
        self.refresh(select=name)

    def _browse_path(self) -> None:
        path = self._pick_file()
        if path:
            self.path_edit.setText(os.path.normpath(path))
            self._repoint()

    # -- editing
    def _rename(self) -> None:
        e = self._entry()
        new = self.name_edit.text().strip()
        if not e or new == e["name"]:
            return
        try:
            data = settings_logic.update_database(self.win.data, e["name"], new_name=new)
        except config.ConfigError as err:
            self.win.error(str(err))
            self.name_edit.setText(e["name"])
            return
        if e["name"] in self.results:
            self.results[new] = self.results.pop(e["name"])
        self.win.set_data(data)
        self.refresh(select=new)

    def _repoint(self) -> None:
        e = self._entry()
        path = self.path_edit.text().strip()
        if not e or not path or os.path.normcase(os.path.abspath(path)) == \
                os.path.normcase(os.path.abspath(e.get("path", ""))):
            return
        self.results.pop(e["name"], None)
        self.win.set_data(settings_logic.update_database(self.win.data, e["name"], path=os.path.normpath(path)))
        self.refresh(select=e["name"])

    def _remove(self) -> None:
        e = self._entry()
        if e and ask(self, tr("db.remove"), tr("db.remove.ask", name=e["name"]), warning=False):
            self.results.pop(e["name"], None)
            self.win.set_data(settings_logic.remove_database(self.win.data, e["name"]))
            self.refresh()

    def _make_active(self) -> None:
        e = self._entry()
        if e:
            self.win.set_data(settings_logic.set_active(self.win.data, e["name"]))
            self.refresh(select=e["name"])

    # -- test and confirmation
    def run_test(self) -> None:
        e = self._entry()
        if not e or self.job is not None:
            return
        self._repoint()
        e = self._entry()
        name, path, trusted = e["name"], e["path"], tuple(self.win.data.get("trusted_schemas", []))
        self.test_btn.setEnabled(False)
        pill(self.state, "info", tr("db.testing"))
        self.job = start(Job(lambda: dbcheck.test_connection(path, trusted), lambda r: self._tested(name, r)))

    def _tested(self, name: str, res) -> None:
        self.job = None
        self.test_btn.setEnabled(True)
        if isinstance(res, Exception):
            res = {"ok": False, "error": f"{type(res).__name__}: {res}", "path": ""}
        self.results[name] = res
        if self.selected() == name:
            self._show_result()
        self.win.page_changed()

    def _show_result(self) -> None:
        e = self._entry()
        res = self.results.get(e["name"]) if e else None
        self.trust.setVisible(False)
        self.use_btn.setEnabled(False)
        if e is None:
            self.state.clear()
            self.state.setStyleSheet("")
            self.result.clear()
            return
        if res is None:
            if e.get("confirmed"):
                pill(self.state, "ok", tr("db.state.confirmed", at=e.get("tested_at") or "?"))
            else:
                pill(self.state, "idle", tr("db.state.untested"))
            self.result.setText(tr("db.hint.test") if not e.get("confirmed") else
                                tr("db.summary.saved", label=e.get("schema_label") or "?",
                                   n=e.get("projects") if e.get("projects") is not None else "?"))
            return
        if not res.get("ok"):
            pill(self.state, "error", tr("db.state.failed"))
            self.result.setText(f"<b>{tr('db.error')}</b> {_esc(res.get('error'))}")
            return
        sch = res["schema"]
        level = "ok" if res["writes_possible"] else "warn"
        pill(self.state, level, tr("db.state.ok"))
        if not sch["known"] and not sch["trusted"]:
            writes = tr("db.writes.unknown_schema")
        elif not res["file_writable"]:
            writes = tr("db.writes.file_read_only")
        else:
            writes = tr("db.writes.possible")
        lines = [
            (tr("db.r.version"), res.get("p6_version") or tr("db.r.unknown")),
            (tr("db.r.schema"), sch["label"] if sch["known"] else tr(
                "db.r.schema_trusted" if sch["trusted"] else "db.r.schema_untested",
                v=res.get("p6_version") or "?", fp=sch["fingerprint"][:12])),
            (tr("db.r.projects"), tr("db.projects", n=res["projects"], eps=res["eps_nodes"], bl=res["baselines"])),
            (tr("db.r.size"), _mb(res.get("size_bytes"))),
            (tr("db.r.writes"), writes),
        ]
        html = "<table cellspacing='2'>" + "".join(
            f"<tr><td><b>{_esc(k)}</b>&nbsp;&nbsp;</td><td>{_esc(v)}</td></tr>" for k, v in lines) + "</table>"
        if res.get("p6_running"):
            html += f"<p>{_esc(tr('db.warn.p6_running'))}</p>"
        self.result.setText(html)
        if not sch["known"]:
            self.trust.setVisible(True)
            self.trust.setChecked(sch["fingerprint"] in self.win.data.get("trusted_schemas", []))
        same = os.path.normcase(res["path"]) == os.path.normcase(os.path.abspath(e["path"]))
        self.use_btn.setEnabled(same and not (e.get("confirmed") and e["name"] == self.win.data.get("active_database")
                                              and e.get("schema_fingerprint") == sch["fingerprint"]))

    def _trust_clicked(self, checked: bool) -> None:
        e = self._entry()
        res = self.results.get(e["name"]) if e else None
        if not res or not res.get("ok"):
            return
        fp = res["schema"]["fingerprint"]
        if checked and not ask(self, tr("db.trust.title"), tr("db.trust.ask")):
            self.trust.setChecked(False)
            return
        self.win.set_data(settings_logic.trust_schema(self.win.data, fp, checked))
        res["schema"]["trusted"] = checked
        res["schema"]["writable"] = res["schema"]["readable"] and (res["schema"]["known"] or checked)
        res["writes_possible"] = res["schema"]["writable"] and res["file_writable"]
        self._show_result()

    def _confirm(self) -> None:
        e = self._entry()
        res = self.results.get(e["name"]) if e else None
        if not res:
            return
        try:
            data = settings_logic.confirm(self.win.data, e["name"], res)
        except config.ConfigError as err:
            self.win.error(str(err))
            return
        self.win.set_data(data)
        self.refresh(select=e["name"])


def _esc(text) -> str:
    return (str(text) if text is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ----------------------------------------------------------------- Server

def server_status(data: dict) -> tuple[str, str]:
    """(level, text) of the server status. The HTTP host is supervised by the tray app (later milestone)."""
    if data.get("transport", "stdio") == "stdio":
        return "info", tr("srv.status.stdio")
    return "idle", tr("srv.status.http_stopped")


class ServerPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.token: Optional[str] = None
        self.token_error: Optional[str] = None
        self.shown = False
        lay = QVBoxLayout(self)

        box, bl = group(tr("srv.transport"))
        self.stdio = QRadioButton(tr("srv.stdio"))
        self.http = QRadioButton(tr("srv.http"))
        self.grp = QButtonGroup(self)
        for b in (self.stdio, self.http):
            self.grp.addButton(b)
            bl.addWidget(b)
        bl.addWidget(note(tr("srv.transport.note")))
        self.grp.buttonClicked.connect(self._transport)
        lay.addWidget(box)

        self.http_box, hl = group(tr("srv.http.box"))
        form = QFormLayout()
        prow = QHBoxLayout()
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.valueChanged.connect(self._port)
        self.port_check = QPushButton(tr("srv.port.check"))
        self.port_check.clicked.connect(self.check_port)
        self.port_state = QLabel()
        prow.addWidget(self.port)
        prow.addWidget(self.port_check)
        prow.addWidget(self.port_state, 1)
        form.addRow(tr("srv.port"), prow)
        trow = QHBoxLayout()
        self.token_edit = QLineEdit()
        self.token_edit.setReadOnly(True)
        self.show_btn = QPushButton(tr("srv.token.show"))
        self.show_btn.setCheckable(True)
        self.show_btn.toggled.connect(self._show_token)
        self.copy_btn = QPushButton(tr("common.copy"))
        self.copy_btn.clicked.connect(self._copy_token)
        self.regen_btn = QPushButton(tr("srv.token.regenerate"))
        self.regen_btn.clicked.connect(self._regenerate)
        trow.addWidget(self.token_edit, 1)
        for b in (self.show_btn, self.copy_btn, self.regen_btn):
            trow.addWidget(b)
        form.addRow(tr("srv.token"), trow)
        hl.addLayout(form)
        self.token_note = note(tr("srv.token.note"))
        hl.addWidget(self.token_note)
        lay.addWidget(self.http_box)

        box, sl = group(tr("srv.status"))
        self.status = QLabel()
        sl.addWidget(self.status)
        lay.addWidget(box)
        lay.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        d = self.win.data
        http = d.get("transport", "stdio") == "http"
        (self.http if http else self.stdio).setChecked(True)
        self.port.blockSignals(True)
        self.port.setValue(int(d.get("http_port", config.DEFAULTS["http_port"])))
        self.port.blockSignals(False)
        self.http_box.setEnabled(http)
        if http and self.token is None and self.token_error is None:
            self._load_token()
        self._render_token()
        level, text = server_status(d)
        pill(self.status, level, text)

    def _transport(self, *_):
        self.win.set_data(dict(self.win.data, transport="http" if self.http.isChecked() else "stdio"))
        self.refresh()

    def _port(self, value: int) -> None:
        self.port_state.clear()
        self.port_state.setStyleSheet("")
        self.win.set_data(dict(self.win.data, http_port=int(value)))

    def check_port(self) -> bool:
        free = settings_logic.port_free(self.port.value())
        pill(self.port_state, "ok" if free else "error", tr("srv.port.free") if free else tr("srv.port.busy"))
        return free

    def _load_token(self) -> None:
        try:
            self.token = credentials.http_token(create=True)
        except credentials.SecretStoreError as e:
            self.token_error = str(e)

    def _render_token(self) -> None:
        ok = self.token is not None
        for b in (self.show_btn, self.copy_btn, self.regen_btn):
            b.setEnabled(ok)
        if self.token_error:
            self.token_edit.setText("")
            self.token_note.setText(tr("srv.token.error", err=self.token_error))
        elif ok:
            self.token_edit.setText(self.token if self.show_btn.isChecked() else credentials.mask(self.token))

    def _show_token(self, _checked: bool) -> None:
        self.show_btn.setText(tr("srv.token.hide") if self.show_btn.isChecked() else tr("srv.token.show"))
        self._render_token()

    def _copy_token(self) -> None:
        if self.token:
            QGuiApplication.clipboard().setText(self.token)
            self.win.flash(tr("srv.token.copied"))

    def _regenerate(self) -> None:
        if not ask(self, tr("srv.token.regenerate"), tr("srv.token.regenerate.ask")):
            return
        try:
            self.token = credentials.regenerate_http_token()
        except credentials.SecretStoreError as e:
            self.win.error(str(e))
            return
        self._render_token()
        self.win.flash(tr("srv.token.regenerated"))


# ----------------------------------------------------------------- Tools and safety

class ToolsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        n_basic, n_all = len(policy.BASIC_TOOLS), len(policy.TOOL_KIND)

        box, bl = group(tr("tools.profile"))
        self.basic = QRadioButton(tr("tools.basic", n=n_basic))
        self.full = QRadioButton(tr("tools.full", n=n_all))
        self.grp = QButtonGroup(self)
        for b in (self.basic, self.full):
            self.grp.addButton(b)
            bl.addWidget(b)
        self.grp.buttonClicked.connect(self._profile)
        lay.addWidget(box)

        box, wl = group(tr("tools.writes"))
        self.allow = QCheckBox(tr("tools.allow"))
        self.allow.clicked.connect(self._allow)
        self.backup = QCheckBox(tr("tools.backup"))
        self.backup.clicked.connect(self._backup)
        self.block = QCheckBox(tr("tools.block"))
        self.block.clicked.connect(self._block)
        for w in (self.allow, self.backup, self.block):
            wl.addWidget(w)
        wl.addWidget(note(tr("tools.writes.note")))
        lay.addWidget(box)

        box, kl = group(tr("tools.backups"))
        form = QFormLayout()
        frow = QHBoxLayout()
        self.folder = QLineEdit()
        self.folder.setPlaceholderText(settings_logic.default_backup_dir())
        self.folder.editingFinished.connect(self._folder)
        browse = QPushButton(tr("common.browse"))
        browse.clicked.connect(self._browse)
        default = QPushButton(tr("tools.backups.default"))
        default.clicked.connect(self._default_folder)
        frow.addWidget(self.folder, 1)
        frow.addWidget(browse)
        frow.addWidget(default)
        form.addRow(tr("tools.backups.folder"), frow)
        self.keep = QSpinBox()
        self.keep.setRange(0, 1000)
        self.keep.setSuffix(" " + tr("tools.backups.unit"))
        self.keep.setSpecialValueText(tr("tools.backups.keep_all"))
        self.keep.valueChanged.connect(self._keep)
        form.addRow(tr("tools.backups.keep"), self.keep)
        kl.addLayout(form)
        kl.addWidget(note(tr("tools.backups.note")))
        lay.addWidget(box)

        box, tl = group(tr("tools.list"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([tr("tools.col.tool"), tr("tools.col.kind"), tr("tools.col.offered")])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2):
            self.table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(self.fontMetrics().height() * 10)
        tl.addWidget(self.table)
        self.count = note()
        tl.addWidget(self.count)
        lay.addWidget(box, 1)
        self.refresh()

    def refresh(self) -> None:
        d = self.win.data
        full = d.get("tool_profile") == "full"
        (self.full if full else self.basic).setChecked(True)
        self.allow.setChecked(bool(d.get("allow_writes")))
        self.allow.setEnabled(full)
        self.allow.setToolTip("" if full else tr("tools.allow.needs_full"))
        self.backup.setChecked(bool(d.get("backup_before_write", True)))
        self.block.setChecked(bool(d.get("block_when_p6_running", True)))
        self.folder.setText(d.get("backup_dir") or "")
        self.keep.blockSignals(True)
        self.keep.setValue(int(d.get("backup_retention", config.DEFAULTS["backup_retention"])))
        self.keep.blockSignals(False)
        rows = settings_logic.tool_rows(d.get("tool_profile", "basic"), bool(d.get("allow_writes")))
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(r["tool"]))
            self.table.setItem(i, 1, QTableWidgetItem(tr("kind." + r["kind"])))
            self.table.setItem(i, 2, QTableWidgetItem(tr("common.yes") if r["available"] else tr("common.no")))
        self.count.setText(tr("tools.count", n=sum(r["available"] for r in rows), total=len(rows)))

    def _set(self, **changes) -> None:
        self.win.set_data(dict(self.win.data, **changes))
        self.refresh()

    def _profile(self, *_):
        self._set(tool_profile="full" if self.full.isChecked() else "basic")

    def _allow(self, checked: bool) -> None:
        if checked and not ask(self, tr("tools.allow"), tr("tools.allow.ask")):
            self.allow.setChecked(False)
            return
        self._set(allow_writes=checked)

    def _backup(self, checked: bool) -> None:
        if not checked and self.win.data.get("allow_writes") and not ask(self, tr("tools.backup"),
                                                                          tr("tools.backup.ask")):
            self.backup.setChecked(True)
            return
        self._set(backup_before_write=checked)

    def _block(self, checked: bool) -> None:
        if not checked and not ask(self, tr("tools.block"), tr("tools.block.ask")):
            self.block.setChecked(True)
            return
        self._set(block_when_p6_running=checked)

    def _folder(self) -> None:
        text = self.folder.text().strip()
        if (text or None) != self.win.data.get("backup_dir"):
            self._set(backup_dir=os.path.normpath(text) if text else None)

    def _browse(self) -> None:
        start_dir = self.folder.text() or settings_logic.default_backup_dir()
        path = QFileDialog.getExistingDirectory(self, tr("tools.backups.folder"), start_dir)
        if path:
            self._set(backup_dir=os.path.normpath(path))

    def _default_folder(self) -> None:
        self._set(backup_dir=None)

    def _keep(self, value: int) -> None:
        self.win.set_data(dict(self.win.data, backup_retention=int(value)))
