"""P6 EPPM mode in the Database page: the server profiles, their passwords and the connection test.

Profiles live in win.data (saved with Save). Passwords go to the Windows Credential Manager only after a test
shows they work (REST password after a successful sign-in, SQL password after a successful SQL test); an empty
password field means "use the stored one".
"""

from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlsplit

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from p6mcp import config, credentials, eppm, settings_logic
from p6mcp.i18n import tr
from p6mcp.settings_app.common import Job, ask, group, note, pill, start

SQL_DEFAULTS = {"mssql": (1433, "dbo"), "oracle": (1521, "ADMUSER")}


def _stored(key: str) -> Optional[str]:
    try:
        return credentials.get(key)
    except credentials.SecretStoreError:
        return None


class EppmPanel(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.job: Optional[Job] = None
        self.results: dict[str, dict] = {}  # last test result per profile name
        self._filling = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        box, bl = group(tr("eppm.box"))
        bl.addWidget(note(tr("eppm.intro")))
        self.list = QListWidget()
        self.list.setMinimumHeight(self.fontMetrics().height() * 4)
        self.list.itemSelectionChanged.connect(self._fill)
        bl.addWidget(self.list)
        row = QHBoxLayout()
        self.add_btn = QPushButton(tr("eppm.add"))
        self.add_btn.clicked.connect(self._add)
        self.remove_btn = QPushButton(tr("eppm.remove"))
        self.remove_btn.clicked.connect(self._remove)
        row.addWidget(self.add_btn)
        row.addWidget(self.remove_btn)
        row.addStretch(1)
        bl.addLayout(row)
        lay.addWidget(box)

        box, dl = group(tr("eppm.selected"))
        self.detail = box
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.editingFinished.connect(self._rename)
        form.addRow(tr("eppm.name"), self.name_edit)
        self.url_edit = self._line(form, "eppm.url", "base_url", tr("eppm.url.placeholder"))
        self.db_edit = self._line(form, "eppm.database", "database", tr("eppm.database.placeholder"))
        self.user_edit = self._line(form, "eppm.user", "user")
        self.pw_edit = QLineEdit()
        self.pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(tr("eppm.password"), self.pw_edit)
        self.auth = QComboBox()
        for key in config.EPPM_AUTH:
            self.auth.addItem(tr(f"eppm.auth.{key}"), key)
        self.auth.currentIndexChanged.connect(lambda _i: self._set(auth=self.auth.currentData()))
        form.addRow(tr("eppm.auth"), self.auth)
        crow = QHBoxLayout()
        self.ca_edit = QLineEdit()
        self.ca_edit.setPlaceholderText(tr("eppm.ca.placeholder"))
        self.ca_edit.editingFinished.connect(lambda: self._set(ca_bundle=self.ca_edit.text().strip() or None))
        ca_btn = QPushButton(tr("common.browse"))
        ca_btn.clicked.connect(self._browse_ca)
        crow.addWidget(self.ca_edit, 1)
        crow.addWidget(ca_btn)
        form.addRow(tr("eppm.ca"), crow)
        dl.addLayout(form)

        self.sql_check = QCheckBox(tr("eppm.sql"))
        self.sql_check.clicked.connect(self._sql_toggled)
        dl.addWidget(self.sql_check)
        self.sql_box = QWidget()
        sform = QFormLayout(self.sql_box)
        sform.setContentsMargins(0, 0, 0, 0)
        self.driver = QComboBox()
        for key in config.EPPM_SQL_DRIVERS:
            self.driver.addItem(tr(f"eppm.sql.driver.{key}"), key)
        self.driver.currentIndexChanged.connect(self._driver_changed)
        sform.addRow(tr("eppm.sql.driver"), self.driver)
        self.host_edit = self._line(sform, "eppm.sql.host", "host", sql=True)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.editingFinished.connect(lambda: self._set_sql(port=self.port.value()))
        sform.addRow(tr("eppm.sql.port"), self.port)
        self.service_edit = self._line(sform, "eppm.sql.service", "service", sql=True)
        self.schema_edit = self._line(sform, "eppm.sql.schema", "schema", sql=True)
        self.prefix_edit = self._line(sform, "eppm.sql.prefix", "table_prefix", sql=True)
        self.suser_edit = self._line(sform, "eppm.sql.user", "user", sql=True)
        self.spw_edit = QLineEdit()
        self.spw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        sform.addRow(tr("eppm.sql.password"), self.spw_edit)
        dl.addWidget(self.sql_box)

        trow = QHBoxLayout()
        self.test_btn = QPushButton(tr("eppm.test"))
        self.test_btn.clicked.connect(self.run_test)
        self.state = QLabel()
        self.state.setWordWrap(True)
        self.state.setTextFormat(Qt.TextFormat.PlainText)
        trow.addWidget(self.test_btn)
        trow.addWidget(self.state, 1)
        dl.addLayout(trow)
        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.PlainText)  # server messages are never rendered as HTML
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        dl.addWidget(self.result)
        urow = QHBoxLayout()
        self.use_btn = QPushButton(tr("eppm.use"))
        self.use_btn.clicked.connect(self._confirm)
        urow.addWidget(self.use_btn)
        self.install_btn = QPushButton(tr("eppm.install"))
        self.install_btn.clicked.connect(self.install_driver)
        self.install_btn.hide()
        urow.addWidget(self.install_btn)
        urow.addStretch(1)
        dl.addLayout(urow)
        lay.addWidget(box)
        self.refresh()

    # -- helpers
    def _line(self, form: QFormLayout, label: str, key: str, placeholder: str = "", sql: bool = False) -> QLineEdit:
        edit = QLineEdit()
        if placeholder:
            edit.setPlaceholderText(placeholder)
        if sql:
            edit.editingFinished.connect(lambda: self._set_sql(**{key: edit.text().strip()}))
        else:
            edit.editingFinished.connect(lambda: self._set(**{key: edit.text().strip()}))
        form.addRow(tr(label), edit)
        return edit

    def profiles(self) -> list[dict]:
        return list(self.win.data.get("eppm_profiles", []))

    def selected(self) -> Optional[str]:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def current(self) -> Optional[dict]:
        name = self.selected()
        return settings_logic.find_eppm(self.win.data, name) if name else None

    def _apply(self, data: dict, select: Optional[str] = None) -> None:
        self.win.set_data(data)
        self.refresh(select)
        self.win.page_changed()

    def _set(self, **changes) -> None:
        p = self.current()
        if self._filling or p is None or all(p.get(k) == v for k, v in changes.items()):
            return
        if "base_url" in changes and not str(changes["base_url"]).lower().startswith(("https://", "http://")):
            pill(self.state, "error", tr("eppm.url.bad"))
            self.url_edit.setText(p.get("base_url", ""))
            return
        try:
            self._apply(settings_logic.update_eppm_profile(self.win.data, p["name"], **changes), p["name"])
        except config.ConfigError as e:
            pill(self.state, "error", str(e))

    def _set_sql(self, **changes) -> None:
        p = self.current()
        if self._filling or p is None or not p.get("sql"):
            return
        self._set(sql=dict(p["sql"], **changes))

    # -- list
    def refresh(self, select: Optional[str] = None) -> None:
        keep = select or self.selected()
        active = self.win.data.get("eppm_active")
        self.list.blockSignals(True)
        self.list.clear()
        for p in self.profiles():
            status = tr("eppm.status.active") if p["name"] == active else (
                tr("eppm.status.confirmed") if p.get("confirmed") else tr("eppm.status.untested"))
            item = QListWidgetItem(f"{p['name']}    {p.get('base_url', '')}    ({status})")
            item.setData(Qt.ItemDataRole.UserRole, p["name"])
            self.list.addItem(item)
            if p["name"] == keep:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        self._fill()

    def _add(self) -> None:
        url, ok = QInputDialog.getText(self, tr("eppm.add.title"), tr("eppm.add.prompt"), text="https://")
        self.add_server(url if ok else "")

    def add_server(self, url: str) -> Optional[str]:
        url = (url or "").strip()
        if not url.lower().startswith(("https://", "http://")) or not urlsplit(url).hostname:
            if url:
                pill(self.state, "error", tr("eppm.url.bad"))
            return None
        data, name = settings_logic.add_eppm_profile(self.win.data, url)
        self._apply(data, name)
        return name

    def _remove(self) -> None:
        p = self.current()
        if p is None or not ask(self, tr("eppm.remove.title"), tr("eppm.remove.ask", name=p["name"])):
            return
        try:
            settings_logic.forget_eppm_passwords(p["name"])
        except credentials.SecretStoreError as e:
            pill(self.state, "error", str(e))
        self.results.pop(p["name"], None)
        self._apply(settings_logic.remove_eppm_profile(self.win.data, p["name"]))

    def _rename(self) -> None:
        p = self.current()
        new = self.name_edit.text().strip()
        if self._filling or p is None or new == p["name"]:
            return
        try:
            data = settings_logic.update_eppm_profile(self.win.data, p["name"], new_name=new)
        except config.ConfigError as e:
            pill(self.state, "error", str(e))
            self.name_edit.setText(p["name"])
            return
        try:  # the passwords follow the name
            for key in (credentials.eppm_key, credentials.eppm_sql_key):
                old = _stored(key(p["name"]))
                if old:
                    credentials.set(key(new), old)
                    credentials.delete(key(p["name"]))
        except credentials.SecretStoreError as e:
            pill(self.state, "error", str(e))
        if p["name"] in self.results:
            self.results[new] = self.results.pop(p["name"])
        self._apply(data, new)

    def _browse_ca(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("eppm.ca"), os.path.expanduser("~"), tr("eppm.ca.filter"))
        if path:
            self.ca_edit.setText(os.path.normpath(path))
            self._set(ca_bundle=os.path.normpath(path))

    # -- SQL block
    def _sql_toggled(self) -> None:
        p = self.current()
        if p is None:
            return
        if self.sql_check.isChecked():
            port, schema = SQL_DEFAULTS["mssql"]
            self._set(sql={"driver": "mssql", "host": urlsplit(p.get("base_url", "")).hostname or "localhost",
                           "port": port, "service": p.get("database") or "PMDB", "user": "", "schema": schema,
                           "table_prefix": ""})
        else:
            self._set(sql=None)

    def _driver_changed(self, _i: int) -> None:
        p = self.current()
        if self._filling or p is None or not p.get("sql"):
            return
        new = self.driver.currentData()
        old = p["sql"].get("driver")
        changes = {"driver": new}
        if p["sql"].get("port") == SQL_DEFAULTS.get(old, (None,))[0]:
            changes["port"] = SQL_DEFAULTS[new][0]
        if p["sql"].get("schema") == SQL_DEFAULTS.get(old, (None, None))[1]:
            changes["schema"] = SQL_DEFAULTS[new][1]
        self._set_sql(**changes)

    # -- detail
    def _fill(self) -> None:
        p = self.current()
        self.detail.setVisible(p is not None)
        self.remove_btn.setEnabled(p is not None)
        if p is None:
            return
        self._filling = True
        try:
            self.name_edit.setText(p["name"])
            self.url_edit.setText(p.get("base_url", ""))
            self.db_edit.setText(p.get("database") or "")
            self.user_edit.setText(p.get("user") or "")
            self.auth.setCurrentIndex(max(0, self.auth.findData(p.get("auth", "auto"))))
            self.ca_edit.setText(p.get("ca_bundle") or "")
            self.pw_edit.clear()
            self.pw_edit.setPlaceholderText(tr("eppm.password.stored") if _stored(credentials.eppm_key(p["name"]))
                                            else "")
            sql = p.get("sql")
            self.sql_check.setChecked(bool(sql))
            self.sql_box.setVisible(bool(sql))
            self.spw_edit.clear()
            if sql:
                self.driver.setCurrentIndex(max(0, self.driver.findData(sql.get("driver"))))
                self.host_edit.setText(sql.get("host", ""))
                self.port.setValue(int(sql.get("port") or 1))
                self.service_edit.setText(sql.get("service", ""))
                self.schema_edit.setText(sql.get("schema") or "")
                self.prefix_edit.setText(sql.get("table_prefix") or "")
                self.suser_edit.setText(sql.get("user") or "")
                self.spw_edit.setPlaceholderText(
                    tr("eppm.password.stored") if _stored(credentials.eppm_sql_key(p["name"])) else "")
        finally:
            self._filling = False
        self._show_result()

    def _show_result(self) -> None:
        p = self.current()
        if p is None:
            return
        res = self.results.get(p["name"])
        self.test_btn.setEnabled(self.job is None)
        missing = ((res or {}).get("sql") or {}).get("driver_missing")
        self.install_btn.setVisible(bool(missing))
        self.install_btn.setEnabled(self.job is None)
        fresh = bool(res and res.get("ok") and res.get("digest") == eppm.digest(p))
        self.use_btn.setEnabled(fresh and not (p.get("confirmed") and self.win.data.get("eppm_active") == p["name"]))
        if res is None:
            if p.get("confirmed"):
                pill(self.state, "ok", tr("eppm.state.confirmed", when=p.get("tested_at", "")))
            else:
                pill(self.state, "warn", tr("eppm.state.untested"))
            self.result.clear()
            return
        r, s = res.get("rest") or {}, res.get("sql")
        lines = [tr("eppm.result.rest_ok", n=r.get("projects", 0), auth=tr(f"eppm.auth.{r.get('auth', 'login')}"),
                    s=r.get("seconds", 0)) if r.get("ok") else tr("eppm.result.rest_failed", err=r.get("error", ""))]
        if s is not None:
            if s.get("ok"):
                lines.append(tr("eppm.result.sql_ok", n=s.get("project_rows", 0), cols=s.get("task_columns", 0)))
            elif s.get("driver_missing"):
                lines.append(tr("eppm.result.sql_driver", driver=s["driver_missing"]))
            else:
                lines.append(tr("eppm.result.sql_failed", err=s.get("error", "")))
        self.result.setText("\n".join(lines))
        if not res.get("ok"):
            pill(self.state, "error", tr("eppm.state.failed"))
        elif not fresh:
            pill(self.state, "warn", tr("eppm.state.changed"))
        elif s is not None and not s.get("ok"):
            pill(self.state, "warn", tr("eppm.state.rest_only"))
        else:
            pill(self.state, "ok", tr("eppm.state.ok"))

    # -- test and confirm
    def run_test(self) -> None:
        p = self.current()
        if p is None or self.job is not None:
            return
        name = p["name"]
        typed, styped = self.pw_edit.text(), self.spw_edit.text()
        pw = typed or _stored(credentials.eppm_key(name))
        spw = styped or (_stored(credentials.eppm_sql_key(name)) if p.get("sql") else None)
        self.test_btn.setEnabled(False)
        pill(self.state, "info", tr("eppm.testing"))
        snapshot = dict(p)
        self.job = start(Job(lambda: eppm.test_profile(snapshot, pw, spw),
                             lambda r: self._tested(name, r, typed, styped)))

    def _tested(self, name: str, res, typed: str, styped: str) -> None:
        self.job = None
        if isinstance(res, Exception):
            res = {"ok": False, "rest": {"ok": False, "error": str(res)}, "sql": None}
        self.results[name] = res
        try:  # a password is kept only once it has worked
            if typed and (res.get("rest") or {}).get("ok"):
                credentials.set(credentials.eppm_key(name), typed)
            if styped and (res.get("sql") or {}).get("ok"):
                credentials.set(credentials.eppm_sql_key(name), styped)
        except credentials.SecretStoreError as e:
            res.setdefault("rest", {})["error"] = str(e)
        if self.selected() == name:
            self._fill()
        self.win.page_changed()

    def install_driver(self) -> None:
        """Download and install the SQL driver the last test missed (asks first: it reaches PyPI)."""
        from p6mcp.eppm import drivers, sql
        p = self.current()
        if p is None or not p.get("sql") or self.job is not None:
            return
        drv = p["sql"]["driver"]
        if not ask(self, tr("eppm.install"), tr("eppm.install.ask", driver=sql.DRIVERS[drv]), warning=False):
            return
        self.install_btn.setEnabled(False)
        self.test_btn.setEnabled(False)
        pill(self.state, "info", tr("eppm.installing", driver=sql.DRIVERS[drv]))
        self.job = start(Job(lambda: drivers.install(drv), self._installed))

    def _installed(self, res) -> None:
        self.job = None
        if isinstance(res, Exception):
            pill(self.state, "error", tr("eppm.install.failed", err=str(res)))
            self.install_btn.setEnabled(True)
            self.test_btn.setEnabled(True)
            return
        self.run_test()  # the test now finds the driver

    def _confirm(self) -> None:
        p = self.current()
        if p is None:
            return
        try:
            data = settings_logic.confirm_eppm(self.win.data, p["name"], self.results.get(p["name"], {}))
        except config.ConfigError as e:
            pill(self.state, "error", str(e))
            return
        self._apply(dict(data, data_source="eppm"), p["name"])
