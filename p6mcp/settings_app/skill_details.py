"""The skill details dialog of the Skills and Skill shop pages: title and ID, author (link to the GitHub
profile), version, source or state, description, the tools it calls (with the write tools warning) and the whole
SKILL.md. The page gives the content (fill) and the buttons: an action that changes the skill refills the dialog,
one marked close (Edit...) closes it first and runs after. Resizable; its size is kept in the window state file
(settings_logic.save_ui_state), never in the settings.
"""

from __future__ import annotations

import html
from typing import Callable, NamedTuple, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

from p6mcp import settings_logic
from p6mcp.i18n import tr
from p6mcp.settings_app.cards import Badge, later, mono_font, primary
from p6mcp.settings_app.common import note, pill


class Action(NamedTuple):
    key: str  # install, update, remove, edit
    text: str
    fn: Callable[[], object]
    enabled: bool = True
    tip: str = ""
    primary: bool = False
    close: bool = False  # close the dialog, then run fn


class SkillDetails(QDialog):
    """info: {title, name (the ID, p6-...), description, md (SKILL.md), tools (list), writes (bool), and when known
    author, version, source / state / offered ((level, text) badges), issue_url, note}."""

    def __init__(self, parent: QWidget, info: dict, actions: list[Action]):
        super().__init__(parent)
        self.setLayoutDirection(parent.layoutDirection())  # a dialog is a window: it does not inherit it
        self.after: Optional[Callable[[], object]] = None  # an Action with close=True, run by the page
        self.buttons: dict[str, QPushButton] = {}
        self.setWindowTitle(tr("details.title"))
        self.setSizeGripEnabled(True)
        lay = QVBoxLayout(self)
        self.head = QLabel()
        self.head.setWordWrap(True)
        self.head.setTextFormat(Qt.TextFormat.PlainText)
        self.head.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        f = self.head.font()
        f.setPointSizeF(f.pointSizeF() * 1.4)
        f.setBold(True)
        self.head.setFont(f)
        lay.addWidget(self.head)
        self.ident = QLabel()
        self.ident.setFont(mono_font(self.font()))
        self.ident.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.ident)

        self.form = QFormLayout()
        self.author = QLabel()
        self.author.setOpenExternalLinks(True)
        self.author.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.version = QLabel()
        self.source, self.state, self.offered = Badge(), Badge(), Badge()
        self.issue = QLabel()
        self.issue.setOpenExternalLinks(True)
        self.issue.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.rows: dict[str, QWidget] = {}
        for key, label, w in (("author", "details.author", self.author),
                              ("version", "details.version", self.version),
                              ("source", "skill.col.kind", self.source), ("state", "shop.col.status", self.state),
                              ("offered", "skill.col.offered", self.offered),
                              ("issue", "details.discussion", self.issue)):
            self.form.addRow(tr(label), w if not isinstance(w, Badge) else _left(w))
            self.rows[key] = w
        lay.addLayout(self.form)

        self.desc = QLabel()
        self.desc.setWordWrap(True)
        self.desc.setTextFormat(Qt.TextFormat.PlainText)
        self.desc.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.desc)
        self.tools = QLabel()
        self.tools.setWordWrap(True)
        self.tools.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.tools)
        self.md = QPlainTextEdit()
        self.md.setReadOnly(True)
        self.md.setFont(mono_font(self.font()))
        self.md.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.md.setMinimumHeight(self.fontMetrics().height() * 8)
        self.md.setAccessibleName("SKILL.md")
        lay.addWidget(self.md, 1)
        self.note = note()
        lay.addWidget(self.note)
        self.box = QDialogButtonBox()
        self.box.rejected.connect(self.reject)  # Close (and Esc)
        lay.addWidget(self.box)
        self.fill(info, actions)
        self._restore_size()

    def fill(self, info: dict, actions: list[Action]) -> None:
        self.info = info
        self.head.setText(info.get("title") or info.get("name") or "")
        self.ident.setText(info.get("name") or "")
        author = info.get("author")
        self.author.setText(f'<a href="https://github.com/{html.escape(author)}">{html.escape(author)}</a>'
                            if author else "")
        self.version.setText(str(info["version"]) if info.get("version") is not None else "")
        for key in ("source", "state", "offered"):
            if info.get(key):
                self.rows[key].set(*info[key])
        issue = info.get("issue_url")
        self.issue.setText(f'<a href="{html.escape(issue)}">{html.escape(tr("shop.discussion"))}</a>'
                           if issue else "")
        shown = {"author": bool(author), "version": info.get("version") is not None, "issue": bool(issue),
                 **{k: bool(info.get(k)) for k in ("source", "state", "offered")}}
        for key, w in self.rows.items():
            self.form.setRowVisible(w if not isinstance(w, Badge) else w.parentWidget(), shown[key])
        self.desc.setText(info.get("description") or "")
        tools = ", ".join(info.get("tools") or []) or tr("shop.no_tools")
        if info.get("writes"):  # a skill using write tools can change the schedule (after the user's yes)
            pill(self.tools, "warn", tr("shop.tools.writes", tools=tools))
        else:
            self.tools.setText(tr("shop.tools", tools=tools))
            self.tools.setStyleSheet("")
        self.set_md(info.get("md") or "")
        self.note.setText(info.get("note") or "")
        self.note.setVisible(bool(info.get("note")))
        focused = next((k for k, b in self.buttons.items() if b.hasFocus()), None)
        self.box.clear()
        self.buttons = {}
        for a in actions:
            b = self.box.addButton(a.text, QDialogButtonBox.ButtonRole.ActionRole)
            b.setEnabled(a.enabled)
            b.setToolTip(a.tip)
            b.setAutoDefault(False)
            if a.primary and a.enabled:
                primary(b)
            b.clicked.connect(lambda _c=False, act=a: self._run(act))
            self.buttons[a.key] = b
        close = self.box.addButton(tr("details.close"), QDialogButtonBox.ButtonRole.RejectRole)
        close.setAutoDefault(False)
        self.buttons["close"] = close
        if focused is not None:  # the keyboard stays on the buttons: the same action, its opposite, else Close
            twin = {"install": "remove", "remove": "install"}.get(focused)
            target = next((self.buttons[k] for k in (focused, twin, "close")
                           if k in self.buttons and self.buttons[k].isEnabled()), close)
            target.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_md(self, text: str) -> None:
        self.md.setPlainText(text)

    def _run(self, act: Action) -> None:
        if act.close:
            self.after = act.fn
            self.accept()
        else:  # after the click: the action refills the dialog, which replaces the clicked button
            later(act.fn)

    # -- size kept between openings (window state file)
    def _restore_size(self) -> None:
        fm = self.fontMetrics()
        w, h = fm.averageCharWidth() * 100, fm.height() * 40
        saved = settings_logic.load_ui_state().get("details_size")
        if isinstance(saved, list) and len(saved) == 2 and all(isinstance(v, int) and v > 0 for v in saved):
            w, h = saved
        screen = (self.parentWidget() or self).screen().availableGeometry()
        self.resize(max(min(w, screen.width()), self.minimumSizeHint().width()),
                    max(min(h, screen.height()), self.minimumSizeHint().height()))

    def done(self, result: int) -> None:
        settings_logic.save_ui_state(details_size=[self.width(), self.height()])
        super().done(result)


def _left(w: QWidget) -> QWidget:
    """w at its natural width on the leading side of a form row."""
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(w)
    lay.addStretch(1)
    return box
