"""P6 Professional on SQL Server or Oracle in the Database page: the database servers, the login password, the
connection test, the projects to copy and the local read-only copy (p6mcp.pmdb).

Profiles live in win.data (saved with Save). The password goes to the Windows Credential Manager only after a test
or a copy shows it works; an empty password field means "use the stored one". Copy now fills the local copy at once
(the server reads it once the profile is saved and in use); the server never writes to the database.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from p6mcp import config, credentials, pmdb, settings_logic
from p6mcp.i18n import tr
from p6mcp.settings_app import a11y
from p6mcp.settings_app.common import Check, Job, ask, fit_spin, group, note, pill, start


def _stored(name: str) -> Optional[str]:
    try:
        return credentials.get(credentials.pmdb_key(name))
    except credentials.SecretStoreError:
        return None


class PmdbPanel(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.job: Optional[Job] = None
        self.results: dict[str, dict] = {}  # last test result per profile name
        self.copies: dict[str, object] = {}  # last copy result (manifest or exception) per profile name
        self.module_factory = None  # tests: the fake driver
        self._filling = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        box, bl = group(tr("pmdb.box"))
        bl.addWidget(note(tr("pmdb.intro")))
        self.list = QListWidget()
        self.list.setMinimumHeight(self.fontMetrics().height() * 4)
        self.list.itemSelectionChanged.connect(self._fill)
        a11y.name(self.list, tr("pmdb.box"))
        bl.addWidget(self.list)
        row = QHBoxLayout()
        self.add_btn = QPushButton(tr("pmdb.add"))
        self.add_btn.clicked.connect(self._add)
        self.remove_btn = QPushButton(tr("pmdb.remove"))
        self.remove_btn.clicked.connect(self._remove)
        row.addWidget(self.add_btn)
        row.addWidget(self.remove_btn)
        row.addStretch(1)
        bl.addLayout(row)
        lay.addWidget(box)

        box, dl = group(tr("pmdb.selected"))
        self.detail = box
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.editingFinished.connect(self._rename)
        form.addRow(tr("pmdb.name"), self.name_edit)
        self.driver = QComboBox()
        for key in config.EPPM_SQL_DRIVERS:
            self.driver.addItem(tr(f"pmdb.driver.{key}"), key)
        self.driver.currentIndexChanged.connect(self._driver_changed)
        form.addRow(tr("pmdb.driver"), self.driver)
        self.host_edit = self._line(form, "pmdb.host", "host")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        fit_spin(self.port)
        self.port.editingFinished.connect(lambda: self._set(port=self.port.value()))
        form.addRow(tr("pmdb.port"), self.port)
        self.service_edit = self._line(form, "pmdb.service", "service", tr("pmdb.service.placeholder"))
        self.schema_edit = self._line(form, "pmdb.schema", "schema", tr("pmdb.schema.placeholder"))
        self.user_edit = self._line(form, "pmdb.user", "user")
        self.pw_edit = QLineEdit()
        self.pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(tr("pmdb.password"), self.pw_edit)
        self.tls = Check(tr("pmdb.tls"))
        self.tls.clicked.connect(lambda: self._set(tls=self.tls.isChecked()))
        form.addRow("", self.tls)
        self.projects_edit = QLineEdit()
        self.projects_edit.setPlaceholderText(tr("pmdb.projects.placeholder"))
        self.projects_edit.editingFinished.connect(
            lambda: self._set(projects=pmdb.split_ids(self.projects_edit.text())))
        form.addRow(tr("pmdb.projects"), self.projects_edit)
        self.eps_edit = QLineEdit()
        self.eps_edit.setPlaceholderText(tr("pmdb.eps.placeholder"))
        self.eps_edit.editingFinished.connect(lambda: self._set(eps=pmdb.split_ids(self.eps_edit.text())))
        form.addRow(tr("pmdb.eps"), self.eps_edit)
        self.baselines = QComboBox()
        for key in pmdb.BASELINES:
            self.baselines.addItem(tr(f"pmdb.baselines.{key}"), key)
        self.baselines.currentIndexChanged.connect(lambda _i: self._set(baselines=self.baselines.currentData()))
        form.addRow(tr("pmdb.baselines"), self.baselines)
        dl.addLayout(form)
        dl.addWidget(note(tr("pmdb.readonly")))

        trow = QHBoxLayout()
        self.test_btn = QPushButton(tr("pmdb.test"))
        self.test_btn.clicked.connect(self.run_test)
        self.state = a11y.live(QLabel())
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
        self.use_btn = QPushButton(tr("pmdb.use"))
        self.use_btn.clicked.connect(self._confirm)
        urow.addWidget(self.use_btn)
        self.install_btn = QPushButton(tr("pmdb.install"))
        self.install_btn.clicked.connect(self.install_driver)
        self.install_btn.hide()
        urow.addWidget(self.install_btn)
        self.copy_btn = QPushButton(tr("pmdb.copy.now"))
        self.copy_btn.clicked.connect(self.run_copy)
        urow.addWidget(self.copy_btn)
        urow.addStretch(1)
        dl.addLayout(urow)
        self.copy_state = a11y.live(QLabel())
        self.copy_state.setWordWrap(True)
        self.copy_state.setTextFormat(Qt.TextFormat.PlainText)
        dl.addWidget(self.copy_state)
        lay.addWidget(box)
        self.refresh()

    def access_keys(self) -> list:
        return [self.add_btn, self.test_btn, self.use_btn, self.copy_btn, self.remove_btn, self.install_btn]

    # -- helpers
    def _line(self, form: QFormLayout, label: str, key: str, placeholder: str = "") -> QLineEdit:
        edit = QLineEdit()
        if placeholder:
            edit.setPlaceholderText(placeholder)
        edit.editingFinished.connect(lambda: self._set(**{key: edit.text().strip()}))
        form.addRow(tr(label), edit)
        return edit

    def profiles(self) -> list[dict]:
        return list(self.win.data.get("pmdb_profiles", []))

    def selected(self) -> Optional[str]:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def current(self) -> Optional[dict]:
        name = self.selected()
        return settings_logic.find_pmdb(self.win.data, name) if name else None

    def _apply(self, data: dict, select: Optional[str] = None) -> None:
        self.win.set_data(data)
        self.refresh(select)
        self.win.page_changed()

    def _set(self, **changes) -> None:
        p = self.current()
        if self._filling or p is None or all(p.get(k) == v for k, v in changes.items()):
            return
        try:
            self._apply(settings_logic.update_pmdb_profile(self.win.data, p["name"], **changes), p["name"])
        except config.ConfigError as e:
            pill(self.state, "error", str(e))

    # -- list
    def refresh(self, select: Optional[str] = None) -> None:
        keep = select or self.selected()
        active = self.win.data.get("pmdb_active")
        self.list.blockSignals(True)
        self.list.clear()
        for p in self.profiles():
            status = tr("pmdb.status.active") if p["name"] == active else (
                tr("pmdb.status.confirmed") if p.get("confirmed") else tr("pmdb.status.untested"))
            item = QListWidgetItem(f"{p['name']}    {tr('pmdb.driver.' + p.get('driver', 'mssql'))}    "
                                   f"{p.get('host') or ''}    ({status})")
            item.setData(Qt.ItemDataRole.UserRole, p["name"])
            self.list.addItem(item)
            if p["name"] == keep:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        self._fill()

    def _add(self) -> None:
        name, ok = QInputDialog.getText(self, tr("pmdb.add.title"), tr("pmdb.add.prompt"), text=tr("pmdb.add.default"))
        if ok:
            self.add_database(name)

    def add_database(self, name: str = "", driver: str = "mssql") -> str:
        data, used = settings_logic.add_pmdb_profile(self.win.data, driver, name.strip() or None)
        self._apply(data, used)
        return used

    def _remove(self) -> None:
        p = self.current()
        if p is None or not ask(self, tr("pmdb.remove.title"), tr("pmdb.remove.ask", name=p["name"])):
            return
        try:
            settings_logic.forget_pmdb(p["name"])
        except credentials.SecretStoreError as e:
            pill(self.state, "error", str(e))
        self.results.pop(p["name"], None)
        self.copies.pop(p["name"], None)
        self._apply(settings_logic.remove_pmdb_profile(self.win.data, p["name"]))

    def _rename(self) -> None:
        p = self.current()
        new = self.name_edit.text().strip()
        if self._filling or p is None or new == p["name"]:
            return
        try:
            data = settings_logic.update_pmdb_profile(self.win.data, p["name"], new_name=new)
        except config.ConfigError as e:
            pill(self.state, "error", str(e))
            self.name_edit.setText(p["name"])
            return
        try:  # the password follows the name (the local copy is made again under the new name)
            old = _stored(p["name"])
            if old:
                credentials.set(credentials.pmdb_key(new), old)
                credentials.delete(credentials.pmdb_key(p["name"]))
        except credentials.SecretStoreError as e:
            pill(self.state, "error", str(e))
        for d in (self.results, self.copies):
            if p["name"] in d:
                d[new] = d.pop(p["name"])
        self._apply(data, new)

    def _driver_changed(self, _i: int) -> None:
        p = self.current()
        if self._filling or p is None:
            return
        new, old = self.driver.currentData(), p.get("driver")
        changes = {"driver": new}
        if p.get("port") == pmdb.DEFAULT_PORTS.get(old):
            changes["port"] = pmdb.DEFAULT_PORTS[new]
        if (p.get("schema") or "") == pmdb.DEFAULT_SCHEMA.get(old):
            changes["schema"] = pmdb.DEFAULT_SCHEMA[new]
        self._set(**changes)

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
            self.driver.setCurrentIndex(max(0, self.driver.findData(p.get("driver"))))
            self.host_edit.setText(p.get("host") or "")
            self.port.setValue(int(p.get("port") or 1))
            self.service_edit.setText(p.get("service") or "")
            self.schema_edit.setText(p.get("schema") or "")
            self.user_edit.setText(p.get("user") or "")
            self.tls.setChecked(bool(p.get("tls")))
            self.projects_edit.setText(", ".join(p.get("projects") or []))
            self.eps_edit.setText(", ".join(p.get("eps") or []))
            self.baselines.setCurrentIndex(max(0, self.baselines.findData(p.get("baselines") or "current")))
            self.pw_edit.clear()
            self.pw_edit.setPlaceholderText(tr("pmdb.password.stored") if _stored(p["name"]) else "")
        finally:
            self._filling = False
        self._show_result()

    def _show_result(self) -> None:
        p = self.current()
        if p is None:
            return
        res = self.results.get(p["name"])
        idle = self.job is None
        self.test_btn.setEnabled(idle)
        self.copy_btn.setEnabled(idle)
        self.install_btn.setVisible(bool((res or {}).get("driver_missing")))
        self.install_btn.setEnabled(idle)
        fresh = bool(res and res.get("ok") and res.get("digest") == pmdb.digest(p))
        self.use_btn.setEnabled(fresh and not (p.get("confirmed") and self.win.data.get("pmdb_active") == p["name"]))
        self._show_copy(p)
        if res is None:
            if p.get("confirmed"):
                pill(self.state, "ok", tr("pmdb.state.confirmed", when=p.get("tested_at", "")))
            else:
                pill(self.state, "warn", tr("pmdb.state.untested"))
            self.result.clear()
            return
        if res.get("ok"):
            sel = res.get("selection") or {}
            lines = [tr("pmdb.result.ok", version=(res.get("p6_version") or {}).get("version") or "?",
                        n=res.get("projects", 0), s=res.get("seconds", 0))]
            if sel.get("projects"):
                lines.append(tr("pmdb.result.selection", ids=", ".join(sel["projects"]), bl=sel.get("baselines", 0)))
            else:
                lines.append(tr("pmdb.result.none"))
            missing = (sel.get("missing") or {}).get("projects", []) + (sel.get("missing") or {}).get("eps", [])
            if missing:
                lines.append(tr("pmdb.result.missing", ids=", ".join(missing)))
        elif res.get("driver_missing"):
            lines = [tr("pmdb.result.driver", driver=res["driver_missing"])]
        else:
            lines = [tr("pmdb.result.failed", err=res.get("error", ""))]
        self.result.setText("\n".join(lines))
        if not res.get("ok"):
            pill(self.state, "error", tr("pmdb.state.failed"))
        elif not fresh:
            pill(self.state, "warn", tr("pmdb.state.changed"))
        else:
            pill(self.state, "ok", tr("pmdb.state.ok"))

    def _show_copy(self, p: dict) -> None:
        last = self.copies.get(p["name"])
        if isinstance(last, Exception):
            pill(self.copy_state, "error", tr("pmdb.copy.failed", err=str(last)))
            return
        m = pmdb.read_manifest(p["name"])
        if not m:
            a11y.set_status(self.copy_state, tr("pmdb.copy.none"))
            self.copy_state.setStyleSheet("")
            return
        pill(self.copy_state, "ok", tr("pmdb.copy.state", when=m.get("copied_at", "").replace("T", " "),
                                       n=len(m.get("projects", [])), acts=(m.get("rows") or {}).get("TASK", 0)))

    # -- test, copy and confirm
    def _password(self, p: dict) -> tuple[str, Optional[str]]:
        typed = self.pw_edit.text()
        return typed, typed or _stored(p["name"])

    def run_test(self) -> None:
        p = self.current()
        if p is None or self.job is not None:
            return
        name, (typed, pw) = p["name"], self._password(p)
        self.test_btn.setEnabled(False)
        self.copy_btn.setEnabled(False)
        pill(self.state, "info", tr("pmdb.testing"))
        snapshot, factory = dict(p), self.module_factory
        self.job = start(Job(lambda: pmdb.test_profile(snapshot, pw, factory), lambda r: self._tested(name, r, typed)))

    def _keep(self, name: str, typed: str) -> Optional[str]:
        """Store a typed password once it has worked; the error text otherwise."""
        if not typed:
            return None
        try:
            credentials.set(credentials.pmdb_key(name), typed)
        except credentials.SecretStoreError as e:
            return str(e)
        return None

    def _tested(self, name: str, res, typed: str) -> None:
        self.job = None
        if isinstance(res, Exception):
            res = {"ok": False, "error": str(res)}
        self.results[name] = res
        if res.get("ok"):
            err = self._keep(name, typed)
            if err:
                res["ok"], res["error"] = False, err
        if self.selected() == name:
            self._fill()
        self.win.page_changed()

    def run_copy(self) -> None:
        p = self.current()
        if p is None or self.job is not None:
            return
        name, (typed, pw) = p["name"], self._password(p)
        self.test_btn.setEnabled(False)
        self.copy_btn.setEnabled(False)
        pill(self.copy_state, "info", tr("pmdb.copying"))
        snapshot, factory = dict(p), self.module_factory
        self.job = start(Job(lambda: pmdb.copy(snapshot, pw or "", factory), lambda r: self._copied(name, r, typed)))

    def _copied(self, name: str, res, typed: str) -> None:
        self.job = None
        self.copies[name] = res
        if not isinstance(res, Exception):
            self._keep(name, typed)
        if self.selected() == name:
            self._fill()
        self.win.page_changed()

    def install_driver(self) -> None:
        """Download and install the driver the last test missed (asks first: it reaches PyPI)."""
        from p6mcp.eppm import drivers, sql
        p = self.current()
        if p is None or self.job is not None:
            return
        drv = p["driver"]
        if not ask(self, tr("pmdb.install"), tr("pmdb.install.ask", driver=sql.DRIVERS[drv]), warning=False):
            return
        self.install_btn.setEnabled(False)
        self.test_btn.setEnabled(False)
        pill(self.state, "info", tr("pmdb.installing", driver=sql.DRIVERS[drv]))
        self.job = start(Job(lambda: drivers.install(drv), self._installed))

    def _installed(self, res) -> None:
        self.job = None
        if isinstance(res, Exception):
            pill(self.state, "error", tr("pmdb.install.failed", err=str(res)))
            self.install_btn.setEnabled(True)
            self.test_btn.setEnabled(True)
            return
        self.run_test()  # the test now finds the driver

    def _confirm(self) -> None:
        p = self.current()
        if p is None:
            return
        try:
            data = settings_logic.confirm_pmdb(self.win.data, p["name"], self.results.get(p["name"], {}))
        except config.ConfigError as e:
            pill(self.state, "error", str(e))
            return
        self._apply(dict(data, data_source="pmdb"), p["name"])
