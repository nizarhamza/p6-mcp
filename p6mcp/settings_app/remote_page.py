"""Remote access page (3.2): claude.ai and ChatGPT in the browser, through the user's own Cloudflare account.

Sections: what it does and what leaves the PC, the Cloudflare setup (token used once, Set up / Repair, Remove),
the connector address to paste in the AI app, the switches (on / off, edits from remote clients, file downloads and
uploads, keep awake), the
connected clients with Revoke, and the status of the remote access server and tunnel.

The Cloudflare setup changes things outside this window (the Cloudflare account, the Credential Manager), so its
result is saved at once (commit()), not left as an unsaved change Revert could undo half of.
"""

from __future__ import annotations

import copy
import time
from typing import Optional

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QFormLayout, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QPushButton, QRadioButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from p6mcp import config, runtime, settings_logic
from p6mcp.i18n import tr
from p6mcp.remote import cloudflare, manage, resource_url
from p6mcp.settings_app.common import Job, ask, group, note, pill, start

SIGNUP_URL = "https://dash.cloudflare.com/sign-up"


def _when(ts: Optional[int]) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else tr("remote.clients.never")


def remote_status(data: dict, st=None) -> tuple[str, str]:
    """(level, text): the tray's RemoteHost state when given, else the remote server's status file."""
    if not data.get("remote_public_host"):
        return "idle", tr("remote.status.not_setup")
    if not data.get("remote_enabled"):
        return "idle", tr("remote.status.off")
    if st is None:
        f = runtime.read_status("remote")
        if f.get("state") == "running":
            return "ok", tr("remote.status.running", url=resource_url(data["remote_public_host"]))
        return "idle", tr("remote.status.no_tray")
    if st.state == "running":
        return "ok", tr("remote.status.running", url=resource_url(data["remote_public_host"]))
    if st.state == "error":
        return "error", tr("remote.status.error", err=st.error or "?")
    if st.state == "disabled":
        return "warn", tr("remote.status.save_first")
    return "info", tr("remote.status.starting")


class RemotePage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.job: Optional[Job] = None
        self.remote_state = None
        lay = QVBoxLayout(self)

        box, bl = group(tr("remote.about"))
        bl.addWidget(note(tr("remote.about.what")))
        bl.addWidget(note(tr("remote.about.privacy")))
        lay.addWidget(box)

        box, sl = group(tr("remote.setup"))
        self.relay = QRadioButton(tr("remote.mode.relay"))
        self.domain = QRadioButton(tr("remote.mode.domain"))
        self.mode = QButtonGroup(self)
        for b in (self.relay, self.domain):
            self.mode.addButton(b)
            sl.addWidget(b)
        self.relay.setChecked(True)
        self.mode.buttonClicked.connect(lambda *_: self._render())
        form = QFormLayout()
        self.domain_host = QLineEdit()
        self.domain_host.setPlaceholderText("p6mcp.example.com")
        form.addRow(tr("remote.domain.host"), self.domain_host)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText(tr("remote.token.placeholder"))
        form.addRow(tr("remote.token"), self.token)
        sl.addLayout(form)
        links = QHBoxLayout()
        self.signup_btn = QPushButton(tr("remote.link.signup"))
        self.signup_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(SIGNUP_URL)))
        self.token_btn = QPushButton(tr("remote.link.token"))
        self.token_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(cloudflare.token_url(self._mode()))))
        links.addWidget(self.signup_btn)
        links.addWidget(self.token_btn)
        links.addStretch(1)
        sl.addLayout(links)
        sl.addWidget(note(tr("remote.token.note", perms=", ".join(cloudflare.TOKEN_PERMISSIONS))))
        brow = QHBoxLayout()
        self.setup_btn = QPushButton(tr("remote.setup.run"))
        self.setup_btn.clicked.connect(self.run_setup)
        self.remove_btn = QPushButton(tr("remote.setup.remove"))
        self.remove_btn.clicked.connect(self.run_remove)
        brow.addWidget(self.setup_btn)
        brow.addWidget(self.remove_btn)
        brow.addStretch(1)
        sl.addLayout(brow)
        self.progress = note()
        sl.addWidget(self.progress)
        lay.addWidget(box)

        box, al = group(tr("remote.address"))
        arow = QHBoxLayout()
        self.address = QLineEdit()
        self.address.setReadOnly(True)
        self.copy_btn = QPushButton(tr("common.copy"))
        self.copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.address.text()))
        arow.addWidget(self.address, 1)
        arow.addWidget(self.copy_btn)
        al.addLayout(arow)
        al.addWidget(note(tr("remote.howto.claude")))
        al.addWidget(note(tr("remote.howto.chatgpt")))
        lay.addWidget(box)

        box, wl = group(tr("remote.access"))
        self.enabled = QCheckBox(tr("remote.enabled"))
        self.enabled.toggled.connect(self._enabled)
        self.edits = QCheckBox(tr("remote.edits"))
        self.edits.toggled.connect(self._edits)
        self.files = QCheckBox(tr("remote.files"))
        self.files.toggled.connect(self._files)
        self.awake = QCheckBox(tr("remote.awake"))
        self.awake.toggled.connect(lambda on: self._set(remote_keep_awake=bool(on)))
        for w in (self.enabled, self.edits, self.files, self.awake):
            wl.addWidget(w)
        wl.addWidget(note(tr("remote.edits.note")))
        wl.addWidget(note(tr("remote.files.note")))
        lay.addWidget(box)

        box, cl = group(tr("remote.clients"))
        self.clients = QTableWidget(0, 4)
        self.clients.setHorizontalHeaderLabels([tr("remote.clients.app"), tr("remote.clients.approved"),
                                                tr("remote.clients.used"), tr("remote.clients.access")])
        self.clients.verticalHeader().setVisible(False)
        self.clients.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.clients.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.clients.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.clients.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.clients.horizontalHeader().setStretchLastSection(True)
        self.clients.itemSelectionChanged.connect(self._render_buttons)
        cl.addWidget(self.clients)
        crow = QHBoxLayout()
        self.revoke_btn = QPushButton(tr("remote.clients.revoke"))
        self.revoke_btn.clicked.connect(self.revoke_selected)
        self.revoke_all_btn = QPushButton(tr("remote.clients.revoke_all"))
        self.revoke_all_btn.clicked.connect(self.revoke_all)
        self.reload_btn = QPushButton(tr("remote.clients.reload"))
        self.reload_btn.clicked.connect(self.load_clients)
        for b in (self.revoke_btn, self.revoke_all_btn, self.reload_btn):
            crow.addWidget(b)
        crow.addStretch(1)
        cl.addLayout(crow)
        cl.addWidget(note(tr("remote.clients.note")))
        lay.addWidget(box)

        box, tl = group(tr("remote.status"))
        self.status = QLabel()
        self.status.setWordWrap(True)
        tl.addWidget(self.status)
        lay.addWidget(box)
        lay.addStretch(1)
        self.grants: list[dict] = []
        self.refresh()

    # ------------------------------------------------------------- state
    def _mode(self) -> str:
        return "domain" if self.domain.isChecked() else "relay"

    def _set(self, **changes) -> None:
        if any(self.win.data.get(k) != v for k, v in changes.items()):
            self.win.set_data(dict(self.win.data, **changes))
        self._render()

    def commit(self, data: dict, keys) -> None:
        """Save these keys of data at once, keeping the window's other unsaved edits as they are."""
        saved = copy.deepcopy(self.win.saved if self.win.saved is not None else self.win.data)
        saved.update({k: data.get(k) for k in keys})
        config.save(saved)
        self.win.saved = saved
        self.win.data = dict(self.win.data, **{k: data.get(k) for k in keys})
        self.win.disk = settings_logic.file_stamp()
        self.win.page_changed()
        self.refresh()

    def refresh(self) -> None:
        d = self.win.data
        (self.domain if d.get("remote_mode") == "domain" else self.relay).setChecked(True)
        if d.get("remote_mode") == "domain" and d.get("remote_public_host") and not self.domain_host.text():
            self.domain_host.setText(d["remote_public_host"])
        for box, key, default in ((self.enabled, "remote_enabled", False), (self.edits, "remote_allow_writes", False),
                                  (self.files, "remote_allow_files", False), (self.awake, "remote_keep_awake", True)):
            box.blockSignals(True)
            box.setChecked(bool(d.get(key, default)))
            box.blockSignals(False)
        self.load_clients()
        self._render()

    def _render(self) -> None:
        d = self.win.data
        set_up = bool(d.get("remote_public_host"))
        busy = self.job is not None
        self.domain_host.setEnabled(self.domain.isChecked() and not busy)
        self.setup_btn.setText(tr("remote.setup.repair") if set_up else tr("remote.setup.run"))
        self.setup_btn.setEnabled(not busy)
        self.remove_btn.setEnabled(set_up and not busy)
        self.address.setText(resource_url(d["remote_public_host"]) if set_up else "")
        self.copy_btn.setEnabled(set_up)
        self.enabled.setEnabled(set_up)
        self.edits.setEnabled(set_up)
        self.files.setEnabled(set_up)
        self.awake.setEnabled(set_up)
        level, text = remote_status(d, self.remote_state if getattr(self.win, "remote", None) is not None else None)
        pill(self.status, level, text)
        self._render_buttons()

    def _render_buttons(self) -> None:
        self.revoke_btn.setEnabled(bool(self.clients.selectedItems()))
        self.revoke_all_btn.setEnabled(bool(self.grants))

    def set_state(self, st) -> None:
        """New state from the tray app's RemoteHost."""
        self.remote_state = st
        self._render()

    # ------------------------------------------------------------- switches
    def _enabled(self, on: bool) -> None:
        if on and not self.win.data.get("remote_privacy_ack"):
            if not ask(self, tr("remote.privacy.title"), tr("remote.privacy.text"), warning=False):
                self.enabled.blockSignals(True)
                self.enabled.setChecked(False)
                self.enabled.blockSignals(False)
                return
            self._set(remote_privacy_ack=True)
        self._set(remote_enabled=bool(on))

    def _edits(self, on: bool) -> None:
        if on and not ask(self, tr("remote.edits.title"), tr("remote.edits.warn")):
            self.edits.blockSignals(True)
            self.edits.setChecked(False)
            self.edits.blockSignals(False)
            return
        self._set(remote_allow_writes=bool(on))

    def _files(self, on: bool) -> None:
        """File downloads and uploads (off by default): turning them on asks first."""
        if on and not ask(self, tr("remote.files.title"), tr("remote.files.warn")):
            self.files.blockSignals(True)
            self.files.setChecked(False)
            self.files.blockSignals(False)
            return
        self._set(remote_allow_files=bool(on))

    # ------------------------------------------------------------- Cloudflare
    def _say(self, lines: list[str], level: Optional[str] = None) -> None:
        text = "\n".join(lines)
        if level:
            pill(self.progress, level, text)
        else:
            self.progress.setStyleSheet("")
            self.progress.setText(text)

    def run_setup(self, account_id: Optional[str] = None) -> None:
        token = self.token.text().strip()
        if not token:
            self._say([tr("remote.token.missing")], "warn")
            return
        mode = self._mode()
        host = self.domain_host.text().strip().lower() or None
        if mode == "domain" and not (host and config.remote_host_ok(host)):
            self._say([tr("remote.domain.bad")], "warn")
            return
        d = self.win.data
        ids = d.get("remote_cf") or {}
        port = int(d.get("remote_port", config.DEFAULTS["remote_port"]))
        keep = manage.relay_secret() if d.get("remote_public_host") else None
        lines: list[str] = []
        self._say([tr("remote.setup.working")])

        def work():
            return cloudflare.setup(token, mode, port, account_id or ids.get("account_id"), host,
                                    relay_secret=keep, progress=lines.append)
        self.job = start(Job(work, lambda res: self._setup_done(res, lines)))
        self._render()

    def _setup_done(self, res, lines: list[str]) -> None:
        self.job = None
        if isinstance(res, cloudflare.NeedsAccount):
            names = [f"{n} ({a[:8]}...)" for a, n in res.accounts]
            choice, ok = QInputDialog.getItem(self, tr("remote.account.title"), tr("remote.account.text"), names,
                                              0, False)
            self._render()
            if ok:
                self.run_setup(res.accounts[names.index(choice)][0])
            return
        if isinstance(res, Exception):
            self._say(lines + [tr("remote.setup.failed", err=res)], "error")
            self._render()
            return
        try:
            data = manage.apply_setup(self.win.data, res)
        except Exception as e:  # Credential Manager refused: nothing saved, the Cloudflare objects can be repaired
            self._say(lines + [tr("remote.setup.failed", err=e)], "error")
            self._render()
            return
        self.token.clear()  # used once, never kept
        self.commit(data, ("remote_mode", "remote_public_host", "remote_cf"))
        self._say(lines + [tr("remote.setup.done", url=resource_url(res.public_host))], "ok")
        if getattr(self.win, "remote", None) is not None:
            self.win.remote.restart()

    def run_remove(self) -> None:
        d = self.win.data
        if not d.get("remote_public_host"):
            return
        if not ask(self, tr("remote.remove.title"), tr("remote.remove.text")):
            return
        if getattr(self.win, "remote", None) is not None:
            self.win.remote.stop()  # the tunnel must be down before Cloudflare deletes it
        token = self.token.text().strip()
        if not token:
            if ask(self, tr("remote.remove.title"), tr("remote.remove.local_only")):
                self._forget()
            else:
                self._say([tr("remote.token.missing")], "warn")
            return
        ids = d.get("remote_cf") or {}
        lines: list[str] = []
        self._say([tr("remote.remove.working")])
        self.job = start(Job(lambda: cloudflare.remove(token, ids, progress=lines.append),
                             lambda res: self._remove_done(res, lines)))
        self._render()

    def _remove_done(self, res, lines: list[str]) -> None:
        self.job = None
        if isinstance(res, Exception):
            self._say(lines + [tr("remote.remove.failed", err=res)], "error")
            self._render()
            return
        self.token.clear()
        self._forget()
        self._say(lines + [tr("remote.remove.done")], "ok")

    def _forget(self) -> None:
        data = manage.clear(self.win.data)
        self._revoke(lambda s: s.revoke_all())
        self.commit(data, ("remote_enabled", "remote_public_host", "remote_cf"))

    # ------------------------------------------------------------- connected clients
    def _store(self):
        from p6mcp.remote.store import Store
        return Store()

    def load_clients(self) -> None:
        try:
            self.grants = self._store().grants() if self.win.data.get("remote_public_host") else []
        except Exception as e:  # auth.db unreadable: shown, never fatal
            self.grants = []
            self._say([tr("remote.clients.error", err=e)], "error")
        self.clients.setRowCount(len(self.grants))
        for r, g in enumerate(self.grants):
            access = tr("remote.clients.edit") if "p6:write" in g["scopes"] else tr("remote.clients.read")
            name = f"{g['client_name']} ({g['redirect_host']})" if g.get("redirect_host") else g["client_name"]
            for c, text in enumerate((name, _when(g["created"]), _when(g["last_used"]), access)):
                self.clients.setItem(r, c, QTableWidgetItem(text))
        self._render_buttons()

    def _revoke(self, fn) -> None:
        try:
            fn(self._store())
        except Exception as e:
            self._say([tr("remote.clients.error", err=e)], "error")
        self.load_clients()

    def revoke_selected(self) -> None:
        rows = {i.row() for i in self.clients.selectedItems()}
        if not rows:
            return
        g = self.grants[min(rows)]
        if ask(self, tr("remote.clients.revoke"), tr("remote.clients.revoke_ask", name=g["client_name"])):
            self._revoke(lambda s: s.revoke(g["id"]))

    def revoke_all(self) -> None:
        if self.grants and ask(self, tr("remote.clients.revoke_all"), tr("remote.clients.revoke_all_ask")):
            self._revoke(lambda s: s.revoke_all())
