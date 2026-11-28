"""The approval window of remote access: a connection request from claude.ai or ChatGPT, decided on this PC.

The browser page of the request shows a pairing code; approving needs that code typed here. A request that someone
else started (with the connector address) shows its code in their browser, not in the user's, so it cannot be
approved by a click on a window that looks like the user's own. Since 3.3 the window does not open on its own: the
browser page's Connect button opens it with the code filled in (p6mcp.remote.pairing), and the tray notification or
menu opens it to type the code. The window closes on its own when the request is
decided elsewhere (command line) or expires after 5 minutes; three wrong codes deny it.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from p6mcp.i18n import tr
from p6mcp.settings_app import a11y
from p6mcp.settings_app.common import Check, note, pill

MAX_TRIES = 3


class ApprovalDialog(QDialog):
    def __init__(self, request: dict, edits_allowed: bool, decide: Callable[[str, str, bool, bool], object]):
        """decide(request id, typed code, approve, edits) -> the store's answer (Store.decide_request)."""
        super().__init__()
        self.request, self.decide_fn = request, decide
        self.result_text = None  # "approved" / "denied" / "expired" once decided here
        self.tries = 0
        self.setWindowTitle(tr("remote.approve.title"))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        lay = QVBoxLayout(self)
        host = request["redirect_uri"].split("/")[2] if "://" in request["redirect_uri"] else "?"
        intro = QLabel(tr("remote.approve.text", client=request["client_name"], host=host))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        if request.get("via") == "dcr":
            lay.addWidget(note(tr("remote.approve.unverified")))
        code_label = QLabel(tr("remote.approve.code"))
        lay.addWidget(code_label)
        self.code = QLineEdit()
        code_label.setBuddy(self.code)
        self.code.setMaxLength(6)
        self.code.setPlaceholderText("ABC234")
        f = QFont(self.code.font())
        f.setPointSizeF(f.pointSizeF() * 1.8)
        f.setBold(True)
        self.code.setFont(f)
        self.code.textChanged.connect(self._typed)
        lay.addWidget(self.code)
        self.error = a11y.live(QLabel())
        self.error.setVisible(False)
        lay.addWidget(self.error)
        lay.addWidget(note(tr("remote.approve.only_yours")))
        self.edits = Check(tr("remote.approve.edits"))
        self.edits.setEnabled(edits_allowed)
        lay.addWidget(self.edits)
        if not edits_allowed:
            lay.addWidget(note(tr("remote.approve.edits_off")))
        row = QHBoxLayout()
        row.addStretch(1)
        self.deny_btn = QPushButton(tr("remote.approve.deny"))
        self.deny_btn.clicked.connect(lambda: self._decide(False))
        self.approve_btn = QPushButton(tr("remote.approve.approve"))
        self.approve_btn.clicked.connect(lambda: self._decide(True))
        self.approve_btn.setEnabled(False)
        self.deny_btn.setDefault(True)  # Enter denies: approving takes the code and a deliberate click
        row.addWidget(self.deny_btn)
        row.addWidget(self.approve_btn)
        lay.addLayout(row)

    def prefill(self, code: str) -> None:
        """The Connect button of the browser page on this PC sent the code (p6mcp.remote.pairing): fill it in. The
        user still decides with a click (Deny stays the default button)."""
        self.code.setText(code)
        self.error.setVisible(True)  # shown first: a live label is announced only on screen
        pill(self.error, "ok", tr("remote.approve.prefilled"))

    def _typed(self, text: str) -> None:
        self.approve_btn.setEnabled(len(text.strip()) == 6)

    def _decide(self, approve: bool) -> None:
        res = self.decide_fn(self.request["id"], self.code.text(), approve, approve and self.edits.isChecked())
        if isinstance(res, dict) and res.get("mismatch"):
            self.tries += 1
            if self.tries >= MAX_TRIES:
                self.decide_fn(self.request["id"], "", False, False)
                self.result_text = "denied"
                self.accept()
                return
            self.error.setVisible(True)
            pill(self.error, "error", tr("remote.approve.mismatch", left=MAX_TRIES - self.tries))
            self.code.clear()
            return
        self.result_text = (res or {}).get("status") or "expired"
        self.accept()
