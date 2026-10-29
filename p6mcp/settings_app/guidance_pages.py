"""The Instructions and Skills pages of the Settings window.

Instructions: the text every MCP client receives when it connects (p6mcp.instructions), built-in lines
that can be turned off and the user's own lines. Skills: the P6 skills (p6mcp.skills), the ones installed from
the skill shop (the core set included, Restore core skills reinstalls it: p6mcp.core_skills) and the user's own
ones (with reference files; shared on GitHub through p6mcp.shop, or as a zip file). Both edit win.data through
win.set_data(); the server reads them when it starts, so a change takes effect after Save and a restart of the
MCP client (skills folders: Install skills on the Clients page). The Skills page shows the skills as cards or
list rows (settings_app.cards; the view is window state, not a setting) with a search box and a Source filter;
a card's checkbox, Remove button and "..." menu call the page's actions on that skill (current()), and View
details opens settings_app.skill_details.
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase, QGuiApplication, QIcon
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QSizePolicy, QSpacerItem, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from p6mcp import core_skills, instructions, settings_logic, shop, skills
from p6mcp.i18n import tr
from p6mcp.settings_app.cards import (GRID, LIST, Badge, Card, CardView, Elided, Flow, ViewToggle, bold,
                                      card_button, icon_button, later, mono_font, more_button, retain_hidden)
from p6mcp.settings_app.common import Job, ask, group, muted, note, start
from p6mcp.settings_app.skill_details import Action, SkillDetails

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


SOURCE_LEVEL = {"shop": "info", "shop_edited": "warn", "custom": "idle"}  # badge colours of the Source values
LIST_COLUMNS = (("id", 10), ("offered", 10), ("source", 6))  # list cells (minimum characters), hidden last first


def offered(r: dict) -> tuple[str, str]:
    """(badge level, text) of whether clients get a skill row: the values of the former Offered column."""
    if r["offered"]:
        return "ok", tr("common.yes")
    if not r["enabled"]:
        return "idle", tr("skill.off")
    return "warn", tr("skill.needs", tools=", ".join(r["missing_tools"]))


class SkillCard(Card):
    """A skill of the config: a card (grid) or a row (list); without a row, the list's column titles."""

    def __init__(self, page: "SkillsPage", r: Optional[dict] = None):
        super().__init__(page.view, r["name"] if r else "", header=r is None)
        self.page, self.r = page, r
        key, u = self.key, self.unit
        grid = page.view.mode == GRID
        self.check = QCheckBox()
        self.check.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ident = Elided(skills.PREFIX + key if r else tr("skill.col.skill"))
        self.title = bold(Elided(r["title"] if r else tr("skill.col.title")))

        def remove():
            page.run(key, page.remove)
        self.remove_btn = card_button(tr("skill.remove"), remove) if grid else \
            icon_button(QIcon.ThemeIcon.EditDelete, tr("skill.remove"), remove)
        if r:
            ident, src = skills.PREFIX + key, settings_logic.skill_source(r)
            level, value = offered(r)
            on, why = page.allowed(r)["remove"]
            self.ident.setFont(mono_font(self.font()))
            self.desc = muted(Elided(r["description"], lines=2))
            self.source = Badge(SOURCE_LEVEL[src], tr("skill.kind." + src))
            self.offered = Badge(level, tr("skill.offered.badge", value=value) if grid else value)
            self.check.setChecked(r["enabled"])
            self.check.setToolTip(tr("skill.on.tip", name=ident))
            self.check.setAccessibleName(tr("skill.on.tip", name=ident))
            self.check.toggled.connect(lambda checked: later(lambda: page.set_enabled(key, checked)))
            self.remove_btn.setEnabled(on)
            if why:  # the reason it is off (an icon button keeps its name otherwise)
                self.remove_btn.setToolTip(why)
            self.remove_btn.setAccessibleName(tr("cards.action", action=tr("skill.remove"), name=ident))
            self.open_fn = lambda: page.run(key, page.details)
            self.space_fn = lambda: page.set_enabled(key, not page.row(key)["enabled"])
            self.delete_fn = lambda: page.run(key, page.remove) if page.allowed(page.row(key))["remove"][0] else None
            self.setAccessibleName(tr("cards.name", id=ident, title=r["title"]) if r["title"] else ident)
            self.setAccessibleDescription(" ".join(x for x in (
                r["description"], tr("cards.field", label=tr("skill.col.kind"), value=tr("skill.kind." + src)),
                tr("cards.field", label=tr("skill.col.offered"), value=value), tr("cards.keys.skill")) if x))
        else:  # column titles over the cells of the rows
            self.source = Elided(tr("skill.col.kind"))
            self.offered = Elided(tr("skill.col.offered"))
            for w in (self.ident, self.title, self.source, self.offered):
                muted(bold(w))
            for w in (self.source, self.offered):  # in line with the text of the badges below
                w.setContentsMargins(u, 0, u, 0)
        self.more = more_button(self)
        if not grid:
            lay = QHBoxLayout(self)
            lay.setContentsMargins(u, u // 2, u, u // 2)
            lay.setSpacing(u)
            lay.addWidget(self.check)
            lay.addWidget(self.cell("id", self.ident))
            lay.addWidget(self.title, 1)
            lay.addWidget(self.cell("source", self.source))
            lay.addWidget(self.cell("offered", self.offered))
            lay.addWidget(self.remove_btn)
            lay.addWidget(self.more)
            self.fixed = [self.check, self.remove_btn, self.more]
            if r is None:
                retain_hidden(self.check, self.remove_btn, self.more)
            return
        g = QGridLayout(self)
        g.setContentsMargins(u * 2, u * 2, u * 2, u * 2)
        g.setHorizontalSpacing(u)
        g.setVerticalSpacing(u // 2)
        g.addWidget(self.check, 0, 0)
        g.addWidget(self.ident, 0, 1)
        g.addWidget(self.more, 0, 2, Qt.AlignmentFlag.AlignRight)
        g.addWidget(self.title, 1, 1, 1, 2)
        g.addWidget(self.desc, 2, 1, 1, 2)
        g.addItem(QSpacerItem(0, u, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding), 3, 1)
        badges = Flow()  # the Offered value can be long: the badges wrap instead of being cut
        badges.setSpacing(u)
        badges.addWidget(self.source)
        badges.addWidget(self.offered)
        g.addLayout(badges, 4, 1, 1, 2)
        g.addWidget(self.remove_btn, 5, 1, 1, 2, Qt.AlignmentFlag.AlignRight)
        g.setColumnStretch(1, 1)

    def menu(self) -> Optional[QMenu]:
        return self.page.menu_for(self.key, self) if self.r else None

class SkillsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rows: list[dict] = []  # the skills shown (search and Source filter applied), in config order
        self.job: Optional[Job] = None  # Restore core skills (reads the shop)
        lay = QVBoxLayout(self)
        lay.addWidget(note(tr("skill.intro")))

        bar = QHBoxLayout()
        flow = Flow()
        flow.setSpacing(self.fontMetrics().averageCharWidth())
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("skill.search"))
        self.search.setAccessibleName(tr("skill.search"))
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(self.fontMetrics().averageCharWidth() * 30)
        self.search.setMaximumWidth(self.fontMetrics().averageCharWidth() * 60)
        self.search.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.search.textChanged.connect(lambda *_a: self.refresh())
        self.source = QComboBox()
        self.source.addItem(tr("skill.source.all"), "all")
        for s in settings_logic.SKILL_SOURCES:
            self.source.addItem(tr("skill.kind." + s), s)
        self.source.setAccessibleName(tr("skill.col.kind"))
        self.source.setToolTip(tr("skill.col.kind"))
        self.source.currentIndexChanged.connect(lambda *_a: self.refresh())
        self.add_btn = QPushButton(tr("skill.add"))
        self.add_btn.clicked.connect(self.add)
        self.import_btn = QPushButton(tr("skill.import"))
        self.import_btn.clicked.connect(self.import_skill)
        self.restore_btn = QPushButton(tr("skill.restore"))
        self.restore_btn.clicked.connect(self.restore_core)
        for w in (self.search, self.source, self.add_btn, self.import_btn, self.restore_btn):
            flow.addWidget(w)
        bar.addLayout(flow, 1)
        view = settings_logic.ui_view(settings_logic.load_ui_state(), "skills")
        self.toggle = ViewToggle(view, {GRID: tr("view.grid"), LIST: tr("view.list")})
        self.toggle.changed.connect(self.set_view)
        bar.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(bar)

        self.view = CardView(view)
        lay.addWidget(self.view, 1)
        lay.addWidget(note(tr("skill.restart")))
        self.refresh()

    def refresh(self, select: Optional[str] = None) -> None:
        if select is not None:
            self.view.current = select
        every = settings_logic.skill_rows(self.win.data)
        self.rows = settings_logic.filter_skills(every, self.search.text(), self.source.currentData())
        if select is not None and self.row(select) is None and any(r["name"] == select for r in every):
            for w in (self.search, self.source):  # the skill just added or changed shows, as before the filters
                w.blockSignals(True)
            self.search.clear()
            self.source.setCurrentIndex(0)
            for w in (self.search, self.source):
                w.blockSignals(False)
            self.rows = every
        self.view.set_empty(tr("cards.no_match") if every else tr("skill.empty"))
        header = SkillCard(self) if self.view.mode == LIST else None
        self.view.set_items([SkillCard(self, r) for r in self.rows], header, LIST_COLUMNS)
        full = len(self.win.data.get("custom_skills") or []) >= skills.MAX_CUSTOM
        for b in (self.add_btn, self.import_btn):
            b.setEnabled(not full)
            b.setToolTip(tr("skill.full", n=skills.MAX_CUSTOM) if full else "")

    def set_view(self, view: str) -> None:
        """Grid or List: kept in the window state file (not a setting: no Save needed)."""
        settings_logic.save_ui_state(skills_view=view)
        self.view.mode = view
        self.refresh()

    def current(self) -> Optional[dict]:
        key = self.view.current if hasattr(self, "view") else None
        return self.row(key)

    def row(self, name: Optional[str]) -> Optional[dict]:
        return next((r for r in self.rows if r["name"] == name), None)

    def run(self, name: str, fn) -> object:
        """fn (an action of the page, on the current skill) for the skill name."""
        if self.row(name) is None:
            return None
        self.view.set_current(name)
        return fn()

    def allowed(self, r: dict) -> dict[str, tuple[bool, str]]:
        """{action: (enabled, tooltip)} for a skill row, the rules of the former buttons: a skill of the config can
        be edited and removed, a shop skill is already shared; the tooltip says why an action is off (the Share
        help when it is on)."""
        custom, shared = bool(r["custom"]), bool(r.get("source"))
        locked = "" if custom else tr("skill.locked")
        share = tr("skill.share.note") if custom and not shared else (tr("skill.share.shop") if shared else locked)
        return {"details": (True, ""), "edit": (custom, locked), "copy": (True, ""), "export": (True, ""),
                "share": (custom and not shared, share), "remove": (custom, locked)}

    def menu_for(self, name: str, parent: QWidget) -> QMenu:
        """The "..." menu of a card (also the Menu key and a right click)."""
        menu = QMenu(parent)
        menu.setLayoutDirection(parent.layoutDirection())
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)  # built for each opening
        menu.setToolTipsVisible(True)
        allowed = self.allowed(self.row(name))
        for key, label, fn in (("details", "skill.details", self.details), ("edit", "skill.edit", self.edit),
                               ("copy", "skill.copy", self.copy), ("export", "skill.export", self.export),
                               ("share", "skill.share", self.share)):
            act = menu.addAction(tr(label))
            act.setObjectName(key)
            act.setEnabled(allowed[key][0])
            act.setToolTip(allowed[key][1])
            act.triggered.connect(lambda _c=False, f=fn: later(lambda: self.run(name, f)))
            if key == "details":
                menu.addSeparator()
        return menu

    def set_enabled(self, name: str, on: bool) -> None:
        """The card's checkbox (and Space): the skill on or off, applied on Save like every change of this page."""
        try:
            data = settings_logic.set_skill_enabled(self.win.data, name, on)
        except ValueError as e:
            self.win.error(str(e))
            self.refresh()
            return
        self._apply(data, name)

    # -- details
    def show_dialog(self, dlg: QDialog) -> None:
        """Show a modal dialog, then free it (the tests replace it)."""
        dlg.exec()
        dlg.deleteLater()

    def info(self, r: dict) -> dict:
        """What the details dialog shows of a skill row (author and discussion when the Skill shop page has read
        the catalog)."""
        skill, src = r["skill"], r.get("source") or {}
        entry = self._catalog(src.get("shop"))
        kind, tools = settings_logic.skill_source(r), skill.tools()
        return {"title": r["title"] or skills.PREFIX + r["name"], "name": skills.PREFIX + r["name"],
                "author": entry.get("author"), "version": src.get("version"),
                "issue_url": f"{shop.REPO_URL}/issues/{entry['issue']}" if entry.get("issue") else None,
                "source": (SOURCE_LEVEL[kind], tr("skill.kind." + kind)), "offered": offered(r),
                "description": r["description"], "tools": tools, "writes": shop._writes(tools),
                "md": skills.skill_md(skill), "note": tr("skill.restart")}

    def _catalog(self, shop_name: Optional[str]) -> dict:
        if not shop_name:
            return {}
        from p6mcp.settings_app.shop_page import ShopPage  # imports this module: loaded on use
        index = self.win.page(ShopPage).index or {}
        return next((e for e in index.get("skills") or [] if e.get("name") == shop_name), {})

    def details(self) -> Optional[SkillDetails]:
        """The details dialog of the current skill; Edit... closes it and opens the editor."""
        r = self.current()
        if r is None:
            return None
        on, tip = self.allowed(r)["edit"]
        dlg = SkillDetails(self, self.info(r), [Action("edit", tr("skill.edit"), self.edit, on, tip, close=True)])
        self.show_dialog(dlg)
        if dlg.after is not None:
            dlg.after()
        return dlg

    def _apply(self, data: dict, select: Optional[str] = None) -> None:
        self.win.set_data(data)
        self.refresh(select)

    def edit_skill(self, item: Optional[dict], old_name: Optional[str]) -> Optional[dict]:
        """Open the editor; the new config dict, or None on Cancel (the tests replace it)."""
        dlg = SkillDialog(self, self.win.data, item, old_name)
        return dlg.result_data if dlg.exec() == QDialog.DialogCode.Accepted else None

    def _stored(self, name: str) -> Optional[dict]:
        return next((c for c in self.win.data.get("custom_skills") or [] if c.get("name") == name), None)

    def _item(self, r: dict) -> dict:
        """The selected skill as an editable item."""
        return dict(self._stored(r["name"]) or {})

    def _new_name(self, base: str) -> str:
        names = {c.get("name") for c in self.win.data.get("custom_skills") or []}
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

    # -- the core set (p6mcp.core_skills)
    def restore_core(self) -> bool:
        """Reinstall the core skills (from the shop, else the copy shipped with the app) on the thread pool; the
        result edits win.data like the other actions (Save). Also the Shop page's button."""
        if self.job is not None or not ask(self, tr("skill.restore"), tr("skill.restore.ask"), warning=False):
            return False
        data = self.win.data
        self.restore_btn.setEnabled(False)
        self.win.flash(tr("skill.restore.running"))
        self.job = start(Job(lambda: core_skills.install(data), self._restored))
        return True

    def _restored(self, res) -> None:
        self.job = None
        self.restore_btn.setEnabled(True)
        if not isinstance(res, tuple):
            self.win.error(tr("skill.restore.failed", err=res))
            return
        data, rep = res
        self._apply(data)
        from p6mcp.settings_app.shop_page import ShopPage  # imports this module: loaded on use
        self.win.page(ShopPage).refresh()
        n = len(rep["shop"]) + len(rep["bundle"])
        done = tr("skill.restore.done", n=n, kept=len(rep["kept"])) if not rep["bundle"] else \
            tr("skill.restore.done.offline", n=n, kept=len(rep["kept"]), err=rep["error"] or "")
        problems = rep["conflicts"] or rep["failed"]
        if problems:
            names = ", ".join(rep["conflicts"] + [f["name"] for f in rep["failed"]])
            self.win.error(done + " " + tr("skill.restore.partial", names=names))
        else:
            self.win.flash(done)

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
        QGuiApplication.clipboard().setText(clip)  # pasted by the user: a long link breaks GitHub's sign-in
        self.open_url(url)
        self.win.flash(tr("skill.share.copied"))
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
