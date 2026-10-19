"""XER-only mode in the Database page: the XER files to analyse and their load check."""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QWidget

from p6mcp import xerstore
from p6mcp.i18n import tr
from p6mcp.settings_app.common import Job, group, note, pill, start


class XerPanel(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.job: Optional[Job] = None
        self.result: Optional[dict] = None  # last load: manifest, or {"error": ...}
        from PySide6.QtWidgets import QVBoxLayout
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        box, bl = group(tr("xer.box"))
        bl.addWidget(note(tr("xer.intro")))
        self.list = QListWidget()
        self.list.setMinimumHeight(self.fontMetrics().height() * 6)
        self.list.itemSelectionChanged.connect(self._buttons)
        bl.addWidget(self.list)
        row = QHBoxLayout()
        self.add_btn = QPushButton(tr("xer.add"))
        self.add_btn.clicked.connect(self._add)
        self.remove_btn = QPushButton(tr("xer.remove"))
        self.remove_btn.clicked.connect(self._remove)
        self.up_btn = QPushButton(tr("xer.up"))
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.load_btn = QPushButton(tr("xer.load"))
        self.load_btn.clicked.connect(self.load)
        for b in (self.add_btn, self.remove_btn, self.up_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.load_btn)
        bl.addLayout(row)
        self.state = QLabel()
        self.state.setWordWrap(True)
        bl.addWidget(self.state)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bl.addWidget(self.details)
        lay.addWidget(box)
        self.refresh()

    def files(self) -> list[str]:
        return list(self.win.data.get("xer_files", []))

    def _set_files(self, files: list[str]) -> None:
        self.result = None
        self.win.set_data(dict(self.win.data, xer_files=files, xer_confirmed=False))
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        for f in self.files():
            text = f"{os.path.basename(f)}    {os.path.dirname(f)}"
            item = QListWidgetItem(text if os.path.isfile(f) else f"{text}  ({tr('db.missing')})")
            item.setToolTip(f)
            self.list.addItem(item)
        self._buttons()
        self._show_state()

    def _buttons(self) -> None:
        row = self.list.currentRow()
        sel = bool(self.list.selectedItems())
        self.remove_btn.setEnabled(sel)
        self.up_btn.setEnabled(sel and row > 0)
        self.load_btn.setEnabled(bool(self.files()) and self.job is None)

    def _add(self) -> None:
        start_dir = os.path.dirname(self.files()[-1]) if self.files() else os.path.join(os.path.expanduser("~"),
                                                                                        "Documents")
        paths, _ = QFileDialog.getOpenFileNames(self, tr("xer.pick"), start_dir, tr("xer.filter"))
        self.add_files(paths)

    def add_files(self, paths) -> None:
        known = {os.path.normcase(f) for f in self.files()}
        new = []
        for p in paths:
            p = os.path.normpath(p)
            if os.path.normcase(p) not in known:
                known.add(os.path.normcase(p))
                new.append(p)
        if new:
            self._set_files(self.files() + new)

    def _remove(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            files = self.files()
            del files[row]
            self._set_files(files)

    def _move(self, step: int) -> None:
        row = self.list.currentRow()
        files = self.files()
        if 0 <= row + step < len(files):
            files[row], files[row + step] = files[row + step], files[row]
            self._set_files(files)
            self.list.setCurrentRow(row + step)

    # -- load check
    def load(self) -> None:
        files = self.files()
        if not files or self.job is not None:
            return
        self.load_btn.setEnabled(False)
        pill(self.state, "info", tr("xer.loading"))
        self.job = start(Job(lambda: xerstore.ensure(files)[1], lambda r: self._loaded(files, r)))

    def _loaded(self, files: list[str], res) -> None:
        self.job = None
        if isinstance(res, Exception):
            self.result = {"error": str(res)}
        else:
            self.result = res
            if files == self.files():
                self.win.set_data(dict(self.win.data, xer_confirmed=True))
        self._buttons()
        self._show_state()
        self.win.page_changed()

    def _show_state(self) -> None:
        d = self.win.data
        if self.result and "error" in self.result:
            pill(self.state, "error", tr("xer.failed", err=self.result["error"]))
            self.details.clear()
            return
        if not self.files():
            pill(self.state, "idle", tr("xer.empty"))
            self.details.clear()
            return
        if not d.get("xer_confirmed"):
            pill(self.state, "warn", tr("xer.not_loaded"))
            self.details.clear()
            return
        pill(self.state, "ok", tr("xer.loaded", n=len(self.files())))
        if self.result:
            lines = []
            for f in self.result["files"]:
                projs = ", ".join(p["project_id"] + ("" if p["project_id"] == p["xer"] else
                                                     " " + tr("xer.renamed", name=p["xer"])) for p in f["projects"])
                tasks = f["rows"].get("TASK", 0)
                lines.append(f"<b>{_esc(os.path.basename(f['path']))}</b>: {_esc(projs)} "
                             f"({_esc(tr('xer.counts', tasks=tasks, links=f['rows'].get('TASKPRED', 0)))})")
                lines += [f"&nbsp;&nbsp;{_esc(w)}" for w in f["warnings"][:3]]
            self.details.setText("<br>".join(lines))


def _esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
