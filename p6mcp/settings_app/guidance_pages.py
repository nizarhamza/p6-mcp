"""The Instructions and Commands pages of the Settings window.

Instructions: the text every MCP client receives when it connects (p6mcp.instructions), built-in lines
that can be turned off and the user's own lines. Commands: the P6 commands (p6mcp.prompts), built-in ones
that can be turned off and the user's own ones. Both edit win.data through win.set_data(); the server reads
them when it starts, so a change takes effect after Save and a restart of the MCP client.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QHeaderView, QInputDialog, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from p6mcp import instructions, prompts, settings_logic
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


# ----------------------------------------------------------------- Commands

class CommandDialog(QDialog):
    """Edit one of the user's own commands. On OK, self.result_data holds the new config dict."""

    def __init__(self, parent, data: dict, item: Optional[dict] = None, old_name: Optional[str] = None):
        super().__init__(parent)
        self.data, self.old_name, self.result_data = data, old_name, None
        item = item or {"name": "", "title": "", "description": "", "args": [], "template": "", "enabled": True}
        self.enabled = bool(item.get("enabled", True))
        self.setWindowTitle(tr("cmd.edit.title") if old_name else tr("cmd.add.title"))
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(item.get("name", ""))
        self.name.setPlaceholderText("my-report")
        form.addRow(tr("cmd.f.name"), self.name)
        self.title = QLineEdit(item.get("title", ""))
        form.addRow(tr("cmd.f.title"), self.title)
        self.desc = QLineEdit(item.get("description", ""))
        form.addRow(tr("cmd.f.description"), self.desc)
        lay.addLayout(form)

        box, al = group(tr("cmd.f.args"))
        self.args = QTableWidget(0, 4)
        self.args.setHorizontalHeaderLabels([tr("cmd.arg.name"), tr("cmd.arg.description"), tr("cmd.arg.default"),
                                             tr("cmd.arg.required")])
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
        add = QPushButton(tr("cmd.arg.add"))
        add.clicked.connect(lambda: self._add_arg({"name": "", "description": "", "default": None}))
        rem = QPushButton(tr("cmd.arg.remove"))
        rem.clicked.connect(lambda: self.args.currentRow() >= 0 and self.args.removeRow(self.args.currentRow()))
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        al.addLayout(row)
        lay.addWidget(box)

        box, tl = group(tr("cmd.f.text"))
        self.text = QPlainTextEdit(item.get("template", ""))
        self.text.setMinimumHeight(self.fontMetrics().height() * 12)
        tl.addWidget(self.text)
        tl.addWidget(note(tr("cmd.f.text.note")))
        lay.addWidget(box, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("common.cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        fm = self.fontMetrics()
        self.resize(fm.averageCharWidth() * 100, fm.height() * 40)

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

    def item(self) -> dict:
        args = []
        for r in range(self.args.rowCount()):
            cell = lambda c: (self.args.item(r, c).text() if self.args.item(r, c) else "")  # noqa: E731
            required = self.args.item(r, 3) is not None and self.args.item(r, 3).checkState() == Qt.CheckState.Checked
            args.append({"name": cell(0), "description": cell(1), "default": None if required else cell(2)})
        return {"name": self.name.text(), "title": self.title.text(), "description": self.desc.text(),
                "args": args, "template": self.text.toPlainText(), "enabled": self.enabled}

    def accept(self) -> None:
        try:
            self.result_data = settings_logic.put_command(self.data, self.item(), self.old_name)
        except ValueError as e:
            QMessageBox.warning(self, self.windowTitle(), str(e))
            return
        super().accept()


class CommandsPage(QWidget):
    COLS = ("cmd.col.on", "cmd.col.command", "cmd.col.title", "cmd.col.kind", "cmd.col.offered")

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rows: list[dict] = []
        self._loading = False
        lay = QVBoxLayout(self)
        lay.addWidget(note(tr("cmd.intro")))

        box, tl = group(tr("cmd.list"))
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
        self.table.setMinimumHeight(self.fontMetrics().height() * 14)  # the 5 built-in commands and a few more
        self.table.itemChanged.connect(self._toggled)
        self.table.currentCellChanged.connect(lambda *_a: self._selected())
        self.table.cellDoubleClicked.connect(lambda *_a: self.edit())
        tl.addWidget(self.table)
        row = QHBoxLayout()
        self.add_btn = QPushButton(tr("cmd.add"))
        self.add_btn.clicked.connect(self.add)
        self.copy_btn = QPushButton(tr("cmd.copy"))
        self.copy_btn.clicked.connect(self.copy)
        self.edit_btn = QPushButton(tr("cmd.edit"))
        self.edit_btn.clicked.connect(self.edit)
        self.remove_btn = QPushButton(tr("cmd.remove"))
        self.remove_btn.clicked.connect(self.remove)
        for b in (self.add_btn, self.copy_btn, self.edit_btn, self.remove_btn):
            row.addWidget(b)
        row.addStretch(1)
        tl.addLayout(row)
        lay.addWidget(box)

        box, pl = group(tr("cmd.preview"))
        self.desc = note()
        pl.addWidget(self.desc)
        self.preview = _preview_box(12)
        pl.addWidget(self.preview)
        pl.addWidget(note(tr("cmd.restart")))
        lay.addWidget(box, 1)
        self.refresh()

    def refresh(self, select: Optional[str] = None) -> None:
        cur = self.current()
        select = select or (cur["name"] if cur else None)
        self.rows = settings_logic.command_rows(self.win.data)
        self._loading = True
        self.table.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            on = QTableWidgetItem()
            on.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            on.setCheckState(CHECKED[r["enabled"]])
            self.table.setItem(i, 0, on)
            self.table.setItem(i, 1, QTableWidgetItem("/" + prompts.PREFIX + r["name"]))
            self.table.setItem(i, 2, QTableWidgetItem(r["title"]))
            self.table.setItem(i, 3, QTableWidgetItem(tr("cmd.kind.custom" if r["custom"] else "cmd.kind.builtin")))
            if r["offered"]:
                offered = tr("common.yes")
            elif not r["enabled"]:
                offered = tr("cmd.off")
            else:
                offered = tr("cmd.needs", tools=", ".join(r["missing_tools"]))
            cell = QTableWidgetItem(offered)
            cell.setToolTip(offered)
            self.table.setItem(i, 4, cell)
        self._loading = False
        idx = next((i for i, r in enumerate(self.rows) if r["name"] == select), 0 if self.rows else -1)
        if idx >= 0:
            self.table.setCurrentCell(idx, 1)
        self.add_btn.setEnabled(len(self.win.data.get("custom_commands") or []) < prompts.MAX_CUSTOM)
        self._selected()

    def current(self) -> Optional[dict]:
        r = self.table.currentRow() if hasattr(self, "table") else -1
        return self.rows[r] if 0 <= r < len(self.rows) else None

    def _selected(self) -> None:
        r = self.current()
        custom = bool(r and r["custom"])
        self.edit_btn.setEnabled(custom)
        self.remove_btn.setEnabled(custom)
        self.copy_btn.setEnabled(r is not None)
        if r is None:
            self.desc.setText("")
            self.preview.setPlainText("")
            return
        cmd = r["command"]
        self.desc.setText(cmd.description)
        self.preview.setPlainText(prompts.render(cmd, {a.name: "<" + a.name + ">" for a in cmd.args}))

    def _apply(self, data: dict, select: Optional[str] = None) -> None:
        self.win.set_data(data)
        self.refresh(select)

    def _toggled(self, it: QTableWidgetItem) -> None:
        if self._loading or it.column() != 0:
            return
        name = self.rows[it.row()]["name"]
        try:
            data = settings_logic.set_command_enabled(self.win.data, name, it.checkState() == Qt.CheckState.Checked)
        except ValueError as e:
            self.win.error(str(e))
            self.refresh()
            return
        self._apply(data, name)

    def edit_command(self, item: Optional[dict], old_name: Optional[str]) -> Optional[dict]:
        """Open the editor; the new config dict, or None on Cancel (the tests replace it)."""
        dlg = CommandDialog(self, self.win.data, item, old_name)
        return dlg.result_data if dlg.exec() == QDialog.DialogCode.Accepted else None

    def _stored(self, name: str) -> Optional[dict]:
        return next((c for c in self.win.data.get("custom_commands") or [] if c.get("name") == name), None)

    def add(self) -> bool:
        data = self.edit_command(None, None)
        if data is None:
            return False
        self._apply(data, data["custom_commands"][-1]["name"])
        return True

    def copy(self) -> bool:
        """A new command of the user's starting from the selected one (a built-in text becomes editable)."""
        r = self.current()
        if r is None:
            return False
        cmd = r["command"]
        stored = self._stored(cmd.name)
        template = stored["template"] if stored else cmd.template.replace("{rules}", "").rstrip()
        if not stored:  # built-in texts double their literal braces for str.format
            template = template.replace("{{", "{").replace("}}", "}")
        names = {c.name for c in prompts.COMMANDS} | {c.get("name") for c in self.win.data.get("custom_commands") or []}
        name, n = "my-" + cmd.name, 2
        while name in names:
            name, n = f"my-{cmd.name}-{n}", n + 1
        item = {"name": name[:prompts.MAX_NAME], "title": cmd.title, "description": cmd.description,
                "args": [{"name": a.name, "description": a.description, "default": a.default} for a in cmd.args],
                "template": template, "enabled": True}
        data = self.edit_command(item, None)
        if data is None:
            return False
        self._apply(data, data["custom_commands"][-1]["name"])
        return True

    def edit(self) -> bool:
        r = self.current()
        if not r or not r["custom"]:
            return False
        data = self.edit_command(self._stored(r["name"]), r["name"])
        if data is None:
            return False
        new = next((c["name"] for c in data["custom_commands"] if c["name"] not in
                    {x.get("name") for x in self.win.data.get("custom_commands") or []}), r["name"])
        self._apply(data, new)
        return True

    def remove(self) -> bool:
        r = self.current()
        if not r or not r["custom"] or not ask(self, tr("cmd.remove"), tr("cmd.remove.ask", name=r["name"]),
                                               warning=False):
            return False
        self._apply(settings_logic.remove_command(self.win.data, r["name"]))
        return True
