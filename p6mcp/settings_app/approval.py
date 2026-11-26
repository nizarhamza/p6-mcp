"""The approval window of remote access: a connection request from claude.ai or ChatGPT, decided on this PC.

The browser page of the request shows a pairing code; this window shows the same code, the app and its return
host, and asks Approve or Deny (with "Allow edits" when edits from remote clients are on). It closes on its own
when the request is decided elsewhere (command line) or expires after 5 minutes.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from p6mcp.i18n import tr
from p6mcp.settings_app.common import note


class ApprovalDialog(QDialog):
    def __init__(self, request: dict, edits_allowed: bool, decide: Callable[[str, bool, bool], object]):
        super().__init__()
        self.request, self.decide_fn = request, decide
        self.result_text = None  # "approved" / "denied" once decided here
        self.setWindowTitle(tr("remote.approve.title"))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        lay = QVBoxLayout(self)
        host = request["redirect_uri"].split("/")[2] if "://" in request["redirect_uri"] else "?"
        intro = QLabel(tr("remote.approve.text", client=request["client_name"], host=host))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        lay.addWidget(QLabel(tr("remote.approve.code")))
        self.code = QLabel(request["pair"])
        f = QFont(self.code.font())
        f.setPointSizeF(f.pointSizeF() * 2.2)
        f.setBold(True)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, f.pointSizeF() * 0.3)
        self.code.setFont(f)
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.code)
        lay.addWidget(note(tr("remote.approve.only_yours")))
        self.edits = QCheckBox(tr("remote.approve.edits"))
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
        self.deny_btn.setDefault(True)  # Enter denies: approving takes a deliberate click
        row.addWidget(self.deny_btn)
        row.addWidget(self.approve_btn)
        lay.addLayout(row)

    def _decide(self, approve: bool) -> None:
        res = self.decide_fn(self.request["pair"], approve, approve and self.edits.isChecked())
        self.result_text = (res or {}).get("status") or "expired"
        self.accept()
