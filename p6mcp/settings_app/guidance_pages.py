"""The Instructions and Skills pages of the Settings window.

Instructions: the text every MCP client receives when it connects (p6mcp.instructions), built-in lines
that can be turned off and the user's own lines. Skills: the P6 skills (p6mcp.skills), built-in ones that can
be turned off and the user's own ones (with reference files; shared on GitHub through p6mcp.shop, or as a zip
file). Both edit win.data through win.set_data(); the server reads them when it starts, so a change takes
effect after Save and a restart of the MCP client (skills folders: Install skills on the Clients page).
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QHeaderView, QInputDialog, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from p6mcp import instructions, settings_logic, shop, skills
from p6mcp.i18n import tr
from p6mcp.settings_app.common import ask, group, note

CHECKED = {True: Qt.CheckState.Checked, False: Qt.CheckState.Unchecked}


def _preview_box(lines: int) -> QPlainTextEdit:
    box = QPlainTextEdit()
    box.setReadOnly(True)
    box.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    box.setMinimumHeight(box.fontMetrics().height() * lines)
    return box


# ----------------------------------------------------------------- Instructions

class InstructionsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._loading = False
        lay = QVBoxLayout(self)
        lay.addWidget(note(tr("ins.intro")))

        box, bl = group(tr("ins.builtin"))
        self.builtin = QListWidget()
        self.builtin.setWordWrap(True)
        self.builtin.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.builtin.itemChanged.connect(self._builtin_changed)
        bl.addWidget(self.builtin)
        lay.addWidget(box)

        box, cl = group(tr("ins.custom"))
        self.custom = QListWidget()
        self.custom.setWordWrap(True)
        self.custom.itemChanged.connect(self._custom_changed)
        self.custom.itemDoubleClicked.connect(lambda _it: self.edit())
        self.custom.currentRowChanged.connect(lambda _r: self._buttons())
        cl.addWidget(self.custom)
        row = QHBoxLayout()
        self.add_btn = QPushButton(tr("ins.add"))
        self.add_btn.clicked.connect(self.add)
        self.edit_btn = QPushButton(tr("ins.edit"))
        self.edit_btn.clicked.connect(self.edit)
        self.remove_btn = QPushButton(tr("ins.remove"))
        self.remove_btn.clicked.connect(self.remove)
        self.up_btn = QPushButton(tr("ins.up"))
        self.up_btn.clicked.connect(lambda: self.move(-1))
        self.down_btn = QPushButton(tr("ins.down"))
        self.down_btn.clicked.connect(lambda: self.move(1))
        for b in (self.add_btn, self.edit_btn, self.remove_btn, self.up_btn, self.down_btn):
            row.addWidget(b)
        row.addStretch(1)
        cl.addLayout(row)
        cl.addWidget(note(tr("ins.custom.note")))
        lay.addWidget(box)

        box, pl = group(tr("ins.preview"))
        self.preview = _preview_box(10)
        pl.addWidget(self.preview)
        pl.addWidget(note(tr("ins.restart")))
        lay.addWidget(box, 1)
        self.refresh()

    def refresh(self) -> None:
        d = self.win.data
        self._loading = True
        self.builtin.clear()
        for ln in instructions.BUILTIN:
            it = QListWidgetItem(ln.text + (" " + tr("ins.locked") if ln.locked else ""))
            it.setData(Qt.ItemDataRole.UserRole, ln.id)
            if ln.locked:
                it.setFlags(Qt.ItemFlag.ItemIsEnabled)
                it.setToolTip(tr("ins.locked.tip"))
            else:
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(CHECKED[instructions.enabled(ln, d)])
            self.builtin.addItem(it)
        row = self.custom.currentRow()
        self.custom.clear()
        for item in d.get("custom_instructions") or []:
            it = QListWidgetItem(item.get("text", ""))
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(CHECKED[bool(item.get("enabled", True))])
            self.custom.addItem(it)
        if self.custom.count():
            self.custom.setCurrentRow(min(max(row, 0), self.custom.count() - 1))
        self._loading = False
        self.preview.setPlainText(instructions.build(d))
        self._buttons()

    def _buttons(self) -> None:
        row, n = self.custom.currentRow(), self.custom.count()
        self.edit_btn.setEnabled(row >= 0)
        self.remove_btn.setEnabled(row >= 0)
        self.up_btn.setEnabled(row > 0)
        self.down_btn.setEnabled(0 <= row < n - 1)
        self.add_btn.setEnabled(n < instructions.MAX_CUSTOM)

    def _apply(self, fn) -> bool:
        try:
            data = fn(self.win.data)
        except (ValueError, IndexError) as e:
            self.win.error(str(e))
            self.refresh()
            return False
        self.win.set_data(data)
        self.refresh()
        return True

    def _builtin_changed(self, it: QListWidgetItem) -> None:
        if not self._loading:
            on = it.checkState() == Qt.CheckState.Checked
            self._apply(lambda d: settings_logic.set_instruction_enabled(d, it.data(Qt.ItemDataRole.UserRole), on))

    def _custom_changed(self, it: QListWidgetItem) -> None:
        if not self._loading:
            idx, on = self.custom.row(it), it.checkState() == Qt.CheckState.Checked
            self._apply(lambda d: settings_logic.set_custom_instruction_enabled(d, idx, on))

    def ask_text(self, title: str, text: str = "") -> Optional[str]:
        """The instruction text typed by the user, None on Cancel (the tests replace it)."""
        value, ok = QInputDialog.getMultiLineText(self, title, tr("ins.text"), text)
        return value if ok else None

    def add(self) -> bool:
        text = self.ask_text(tr("ins.add"))
        if text is None:
            return False
        if self._apply(lambda d: settings_logic.put_instruction(d, text)):
            self.custom.setCurrentRow(self.custom.count() - 1)
            return True
        return False

    def edit(self) -> bool:
        row = self.custom.currentRow()
        if row < 0:
            return False
        text = self.ask_text(tr("ins.edit"), self.win.data["custom_instructions"][row].get("text", ""))
        return text is not None and self._apply(lambda d: settings_logic.put_instruction(d, text, row))

    def remove(self) -> bool:
        row = self.custom.currentRow()
        if row < 0 or not ask(self, tr("ins.remove"), tr("ins.remove.ask"), warning=False):
            return False
        return self._apply(lambda d: settings_logic.remove_instruction(d, row))

    def move(self, step: int) -> None:
        row = self.custom.currentRow()
        if row >= 0 and self._apply(lambda d: settings_logic.move_instruction(d, row, step)):
            self.custom.setCurrentRow(max(0, min(row + step, self.custom.count() - 1)))


# ----------------------------------------------------------------- Skills

class FileDialog(QDialog):
    """Edit one reference file of a skill (path and text)."""

    def __init__(self, parent, f: dict):
        super().__init__(parent)
        self.setWindowTitle(tr("skill.file.title"))
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.path = QLineEdit(f.get("path", ""))
        self.path.setPlaceholderText("reference/checklist.md")
        form.addRow(tr("skill.file.path"), self.path)
        lay.addLayout(form)
        self.text = QPlainTextEdit(f.get("text", ""))
        lay.addWidget(self.text, 1)
        lay.addWidget(note(tr("skill.file.note", n=skills.MAX_FILE_TEXT)))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("common.cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        fm = self.fontMetrics()
        self.resize(fm.averageCharWidth() * 90, fm.height() * 30)

    def value(self) -> dict:
        return {"path": self.path.text().strip(), "text": self.text.toPlainText()}


class SkillDialog(QDialog):
    """Edit one of the user's own skills (also the preview of an imported one). On OK, self.result_data holds
    the new config dict."""

    def __init__(self, parent, data: dict, item: Optional[dict] = None, old_name: Optional[str] = None):
        super().__init__(parent)
        self.data, self.old_name, self.result_data = data, old_name, None
        item = item or {"name": "", "title": "", "description": "", "args": [], "template": "", "files": [],
                        "enabled": True}
        self.enabled, self.source = bool(item.get("enabled", True)), item.get("source")
        self.files: list[dict] = [dict(f) for f in item.get("files") or []]
        self.setWindowTitle(tr("skill.edit.title") if old_name else tr("skill.add.title"))
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(item.get("name", ""))
        self.name.setPlaceholderText("my-report")
        form.addRow(tr("skill.f.name"), self.name)
        self.title = QLineEdit(item.get("title", ""))
        form.addRow(tr("skill.f.title"), self.title)
        self.desc = QLineEdit(item.get("description", ""))
        self.desc.setPlaceholderText(tr("skill.f.description.hint"))
        form.addRow(tr("skill.f.description"), self.desc)
        lay.addLayout(form)

        box, al = group(tr("skill.f.args"))
        self.args = QTableWidget(0, 4)
        self.args.setHorizontalHeaderLabels([tr("skill.arg.name"), tr("skill.arg.description"),
                                             tr("skill.arg.default"), tr("skill.arg.required")])
        self.args.verticalHeader().setVisible(False)
        hh = self.args.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in (0, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.args.setMinimumHeight(self.fontMetrics().height() * 6)
        for a in item.get("args") or []:
            self._add_arg(a)
        al.addWidget(self.args)
        row = QHBoxLayout()
        add = QPushButton(tr("skill.arg.add"))
        add.clicked.connect(lambda: self._add_arg({"name": "", "description": "", "default": None}))
        rem = QPushButton(tr("skill.arg.remove"))
        rem.clicked.connect(lambda: self.args.currentRow() >= 0 and self.args.removeRow(self.args.currentRow()))
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        al.addLayout(row)
        lay.addWidget(box)

        box, tl = group(tr("skill.f.text"))
        self.text = QPlainTextEdit(item.get("template", ""))
        self.text.setMinimumHeight(self.fontMetrics().height() * 12)
        tl.addWidget(self.text)
        tl.addWidget(note(tr("skill.f.text.note")))
        lay.addWidget(box, 1)

        box, fl = group(tr("skill.f.files"))
        self.file_list = QListWidget()
        self.file_list.setMinimumHeight(self.fontMetrics().height() * 4)
        self.file_list.itemDoubleClicked.connect(lambda *_a: self.edit_file_row())
        fl.addWidget(self.file_list)
        row = QHBoxLayout()
        for key, fn in (("skill.file.add", self.add_file), ("skill.file.load", self.load_file),
                        ("skill.file.edit", self.edit_file_row), ("skill.file.remove", self.remove_file)):
            b = QPushButton(tr(key))
            b.clicked.connect(lambda _c=False, f=fn: f())
            row.addWidget(b)
        row.addStretch(1)
        fl.addLayout(row)
        fl.addWidget(note(tr("skill.f.files.note", n=skills.MAX_FILES, size=skills.MAX_FILE_TEXT)))
        lay.addWidget(box)
        self._show_files()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("common.cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        fm = self.fontMetrics()
        self.resize(fm.averageCharWidth() * 100, fm.height() * 46)

    def _add_arg(self, a: dict) -> None:
        r = self.args.rowCount()
        self.args.insertRow(r)
        self.args.setItem(r, 0, QTableWidgetItem(a.get("name") or ""))
        self.args.setItem(r, 1, QTableWidgetItem(a.get("description") or ""))
        self.args.setItem(r, 2, QTableWidgetItem(a.get("default") or ""))
        req = QTableWidgetItem()
        req.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        req.setCheckState(CHECKED[a.get("default") is None])
        self.args.setItem(r, 3, req)

    # -- reference files
    def _show_files(self) -> None:
        self.file_list.clear()
        for f in self.files:
            self.file_list.addItem(tr("skill.file.row", path=f["path"], n=len(f["text"])))

    def edit_file(self, f: dict) -> Optional[dict]:
        """The file after the editor, None on Cancel (the tests replace it)."""
        dlg = FileDialog(self, f)
        return dlg.value() if dlg.exec() == QDialog.DialogCode.Accepted else None

    def pick_file(self) -> Optional[str]:
        """A text file to load as a reference file (the tests replace it)."""
        path, _f = QFileDialog.getOpenFileName(self, tr("skill.file.load"), "", tr("skill.file.filter"))
        return path or None

    def add_file(self) -> None:
        f = self.edit_file({"path": "", "text": ""})
        if f:
            self.files.append(f)
            self._show_files()

    def load_file(self) -> None:
        path = self.pick_file()
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as e:
            QMessageBox.warning(self, self.windowTitle(), str(e))
            return
        self.files.append({"path": os.path.basename(path), "text": text})
        self._show_files()

    def edit_file_row(self) -> None:
        r = self.file_list.currentRow()
        if 0 <= r < len(self.files):
            f = self.edit_file(self.files[r])
            if f:
                self.files[r] = f
                self._show_files()

    def remove_file(self) -> None:
        r = self.file_list.currentRow()
        if 0 <= r < len(self.files):
            del self.files[r]
            self._show_files()

    def item(self) -> dict:
        def cell(r: int, c: int) -> str:
            return self.args.item(r, c).text() if self.args.item(r, c) else ""

        args = []
        for r in range(self.args.rowCount()):
            required = self.args.item(r, 3) is not None and self.args.item(r, 3).checkState() == Qt.CheckState.Checked
            args.append({"name": cell(r, 0), "description": cell(r, 1), "default": None if required else cell(r, 2)})
        out = {"name": self.name.text(), "title": self.title.text(), "description": self.desc.text(),
               "args": args, "template": self.text.toPlainText(), "files": self.files, "enabled": self.enabled}
        if self.source:
            out["source"] = self.source
        return out

    def accept(self) -> None:
        try:
            self.result_data = settings_logic.put_skill(self.data, self.item(), self.old_name)
        except ValueError as e:
            QMessageBox.warning(self, self.windowTitle(), str(e))
            return
        super().accept()


class SkillsPage(QWidget):
    COLS = ("skill.col.on", "skill.col.skill", "skill.col.title", "skill.col.kind", "skill.col.offered")

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rows: list[dict] = []
        self._loading = False
        lay = QVBoxLayout(self)
        lay.addWidget(note(tr("skill.intro")))

        box, tl = group(tr("skill.list"))
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels([tr(k) for k in self.COLS])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for c in (0, 1, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(self.fontMetrics().height() * 14)  # the built-in skills and a few more
        self.table.itemChanged.connect(self._toggled)
        self.table.currentCellChanged.connect(lambda *_a: self._selected())
        self.table.cellDoubleClicked.connect(lambda *_a: self.edit())
        tl.addWidget(self.table)
        row = QHBoxLayout()
        self.add_btn = QPushButton(tr("skill.add"))
        self.add_btn.clicked.connect(self.add)
        self.copy_btn = QPushButton(tr("skill.copy"))
        self.copy_btn.clicked.connect(self.copy)
        self.edit_btn = QPushButton(tr("skill.edit"))
        self.edit_btn.clicked.connect(self.edit)
        self.remove_btn = QPushButton(tr("skill.remove"))
        self.remove_btn.clicked.connect(self.remove)
        for b in (self.add_btn, self.copy_btn, self.edit_btn, self.remove_btn):
            row.addWidget(b)
        row.addStretch(1)
        tl.addLayout(row)
        row = QHBoxLayout()
        self.share_btn = QPushButton(tr("skill.share"))
        self.share_btn.clicked.connect(self.share)
        self.export_btn = QPushButton(tr("skill.export"))
        self.export_btn.clicked.connect(self.export)
        self.import_btn = QPushButton(tr("skill.import"))
        self.import_btn.clicked.connect(self.import_skill)
        for b in (self.share_btn, self.export_btn, self.import_btn):
            row.addWidget(b)
        row.addStretch(1)
        tl.addLayout(row)
        tl.addWidget(note(tr("skill.share.note")))
        lay.addWidget(box)

        box, pl = group(tr("skill.preview"))
        self.desc = note()
        pl.addWidget(self.desc)
        self.preview = _preview_box(12)
        pl.addWidget(self.preview)
        pl.addWidget(note(tr("skill.restart")))
        lay.addWidget(box, 1)
        self.refresh()

    def refresh(self, select: Optional[str] = None) -> None:
        cur = self.current()
        select = select or (cur["name"] if cur else None)
        self.rows = settings_logic.skill_rows(self.win.data)
        self._loading = True
        self.table.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            on = QTableWidgetItem()
            on.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            on.setCheckState(CHECKED[r["enabled"]])
            self.table.setItem(i, 0, on)
            self.table.setItem(i, 1, QTableWidgetItem(skills.PREFIX + r["name"]))
            self.table.setItem(i, 2, QTableWidgetItem(r["title"]))
            kind = "skill.kind.shop" if r["source"] else "skill.kind.custom" if r["custom"] else "skill.kind.builtin"
            self.table.setItem(i, 3, QTableWidgetItem(tr(kind)))
            if r["offered"]:
                offered = tr("common.yes")
            elif not r["enabled"]:
                offered = tr("skill.off")
            else:
                offered = tr("skill.needs", tools=", ".join(r["missing_tools"]))
            cell = QTableWidgetItem(offered)
            cell.setToolTip(offered)
            self.table.setItem(i, 4, cell)
        self._loading = False
        idx = next((i for i, r in enumerate(self.rows) if r["name"] == select), 0 if self.rows else -1)
        if idx >= 0:
            self.table.setCurrentCell(idx, 1)
        self.add_btn.setEnabled(len(self.win.data.get("custom_skills") or []) < skills.MAX_CUSTOM)
        self.import_btn.setEnabled(len(self.win.data.get("custom_skills") or []) < skills.MAX_CUSTOM)
        self._selected()

    def current(self) -> Optional[dict]:
        r = self.table.currentRow() if hasattr(self, "table") else -1
        return self.rows[r] if 0 <= r < len(self.rows) else None

    def _selected(self) -> None:
        r = self.current()
        custom = bool(r and r["custom"])
        self.edit_btn.setEnabled(custom)
        self.remove_btn.setEnabled(custom)
        self.share_btn.setEnabled(custom and not (r or {}).get("source"))  # a shop skill is already shared
        self.copy_btn.setEnabled(r is not None)
        self.export_btn.setEnabled(r is not None)
        if r is None:
            self.desc.setText("")
            self.preview.setPlainText("")
            return
        skill = r["skill"]
        self.desc.setText(skill.description)
        self.preview.setPlainText(skills.skill_md(skill))

    def _apply(self, data: dict, select: Optional[str] = None) -> None:
        self.win.set_data(data)
        self.refresh(select)

    def _toggled(self, it: QTableWidgetItem) -> None:
        if self._loading or it.column() != 0:
            return
        name = self.rows[it.row()]["name"]
        try:
            data = settings_logic.set_skill_enabled(self.win.data, name, it.checkState() == Qt.CheckState.Checked)
        except ValueError as e:
            self.win.error(str(e))
            self.refresh()
            return
        self._apply(data, name)

    def edit_skill(self, item: Optional[dict], old_name: Optional[str]) -> Optional[dict]:
        """Open the editor; the new config dict, or None on Cancel (the tests replace it)."""
        dlg = SkillDialog(self, self.win.data, item, old_name)
        return dlg.result_data if dlg.exec() == QDialog.DialogCode.Accepted else None

    def _stored(self, name: str) -> Optional[dict]:
        return next((c for c in self.win.data.get("custom_skills") or [] if c.get("name") == name), None)

    def _item(self, r: dict) -> dict:
        """The selected skill as an editable item (a built-in text becomes editable)."""
        stored = self._stored(r["name"]) if r["custom"] else None
        if stored:
            return dict(stored)
        skill = r["skill"]
        template = skill.template.replace("{rules}", "").rstrip()
        template = template.replace("{{", "{").replace("}}", "}")  # built-in texts double them for str.format
        return {"name": skill.name, "title": skill.title, "description": skill.description,
                "args": [{"name": a.name, "description": a.description, "default": a.default} for a in skill.args],
                "template": template, "files": [{"path": p, "text": t} for p, t in skill.files], "enabled": True}

    def _new_name(self, base: str) -> str:
        names = {s.name for s in skills.BUILTIN} | {c.get("name") for c in self.win.data.get("custom_skills") or []}
        name, n = base, 2
        while name in names:
            name, n = f"{base}-{n}", n + 1
        return name[:skills.MAX_NAME]

    def add(self) -> bool:
        data = self.edit_skill(None, None)
        if data is None:
            return False
        self._apply(data, data["custom_skills"][-1]["name"])
        return True

    def copy(self) -> bool:
        """A new skill of the user's starting from the selected one."""
        r = self.current()
        if r is None:
            return False
        item = self._item(r)
        item.pop("source", None)
        item["name"] = self._new_name("my-" + r["name"])
        data = self.edit_skill(item, None)
        if data is None:
            return False
        self._apply(data, data["custom_skills"][-1]["name"])
        return True

    def edit(self) -> bool:
        r = self.current()
        if not r or not r["custom"]:
            return False
        data = self.edit_skill(self._stored(r["name"]), r["name"])
        if data is None:
            return False
        new = next((c["name"] for c in data["custom_skills"] if c["name"] not in
                    {x.get("name") for x in self.win.data.get("custom_skills") or []}), r["name"])
        self._apply(data, new)
        return True

    def remove(self) -> bool:
        r = self.current()
        if not r or not r["custom"] or not ask(self, tr("skill.remove"), tr("skill.remove.ask", name=r["name"]),
                                               warning=False):
            return False
        self._apply(settings_logic.remove_skill(self.win.data, r["name"]))
        return True

    # -- sharing (GitHub) and files (no account needed)
    def open_url(self, url: str) -> None:
        """Open a page in the browser (the tests replace it)."""
        QDesktopServices.openUrl(QUrl(url))

    def save_path(self, name: str) -> Optional[str]:
        """Where to export (the tests replace it)."""
        path, _f = QFileDialog.getSaveFileName(self, tr("skill.export"), name + ".zip", tr("skill.export.filter"))
        return path or None

    def open_path(self) -> Optional[str]:
        """A skill file to import (the tests replace it)."""
        path, _f = QFileDialog.getOpenFileName(self, tr("skill.import"), "", tr("skill.import.filter"))
        return path or None

    def share(self) -> bool:
        r = self.current()
        if not r or not r["custom"] or r.get("source"):
            return False
        try:
            url, clip = shop.share_url(self._item(r))
        except ValueError as e:
            self.win.error(tr("skill.share.invalid", err=e))
            return False
        if not ask(self, tr("skill.share"), tr("skill.share.ask", name=r["name"], repo=shop.REPO), warning=False):
            return False
        if clip:
            QGuiApplication.clipboard().setText(clip)
            self.win.error(tr("skill.share.clipboard"))
        self.open_url(url)
        return True

    def export(self) -> Optional[str]:
        r = self.current()
        if not r:
            return None
        path = self.save_path(skills.PREFIX + r["name"])
        if not path:
            return None
        try:
            shop.export_zip(self._item(r), path)
        except (OSError, ValueError) as e:
            self.win.error(str(e))
            return None
        self.win.flash(tr("skill.export.done", path=path))
        return path

    def import_skill(self) -> bool:
        path = self.open_path()
        if not path:
            return False
        try:
            item = shop.import_file(path)
        except (OSError, ValueError) as e:
            self.win.error(tr("skill.import.failed", err=e))
            return False
        item["name"] = self._new_name(item["name"])  # a name taken here gets a -2 suffix (editable below)
        data = self.edit_skill(item, None)  # the user reviews the skill before it is added
        if data is None:
            return False
        self._apply(data, data["custom_skills"][-1]["name"])
        return True
