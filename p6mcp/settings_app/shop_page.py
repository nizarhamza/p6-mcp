"""The Shop page of the Settings window: P6 skills shared by users on GitHub (p6mcp.shop).

The catalog is read only when the page is first shown or on Refresh (nothing when the shop is turned off on
the Tools and safety page), and each skill only when it is selected; reads run on the thread pool. Install,
Update and Remove edit win.data like the Skills page: they take effect after Save (and Install skills on the
Clients page for the clients' skills folders).
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from p6mcp import settings_logic, shop, skills
from p6mcp.i18n import tr
from p6mcp.settings_app.common import Job, ask, group, note, pill, start
from p6mcp.settings_app.guidance_pages import SkillsPage, _preview_box

FILTERS = ("all", "read", "writes", "installed", "updates", "builtin")
STATUS_LEVEL = {shop.INSTALLED: "ok", shop.UPDATE: "warn", shop.CONFLICT: "warn", shop.NEWER_APP: "warn",
                shop.BUILTIN: "ok"}


class ShopPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.index: Optional[dict] = None  # fetch_index() result
        self.items: dict[tuple, dict] = {}  # (name, sha256) -> fetch_skill() result
        self.rows: list[dict] = []
        self.job = None
        lay = QVBoxLayout(self)
        lay.addWidget(note(tr("shop.intro", repo=shop.REPO)))

        box, tl = group(tr("shop.list"))
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("shop.search"))
        self.search.textChanged.connect(lambda *_a: self.refresh())
        self.filter = QComboBox()
        for f in FILTERS:
            self.filter.addItem(tr("shop.filter." + f), f)
        self.filter.currentIndexChanged.connect(lambda *_a: self.refresh())
        self.reload_btn = QPushButton(tr("shop.reload"))
        self.reload_btn.clicked.connect(self.load)
        row.addWidget(self.search, 1)
        row.addWidget(self.filter)
        row.addWidget(self.reload_btn)
        tl.addLayout(row)
        self.state = QLabel()
        self.state.setWordWrap(True)
        tl.addWidget(self.state)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([tr("shop.col.skill"), tr("shop.col.title"), tr("shop.col.author"),
                                              tr("shop.col.version"), tr("shop.col.status")])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in (0, 2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(self.fontMetrics().height() * 12)
        self.table.currentCellChanged.connect(lambda *_a: self._selected())
        tl.addWidget(self.table)
        row = QHBoxLayout()
        self.install_btn = QPushButton(tr("shop.install"))
        self.install_btn.clicked.connect(self.install)
        self.remove_btn = QPushButton(tr("shop.remove"))
        self.remove_btn.clicked.connect(self.remove)
        row.addWidget(self.install_btn)
        row.addWidget(self.remove_btn)
        row.addStretch(1)
        tl.addLayout(row)
        lay.addWidget(box)

        box, pl = group(tr("shop.preview"))
        self.desc = note()
        pl.addWidget(self.desc)
        self.links = QLabel()
        self.links.setOpenExternalLinks(True)
        self.links.setWordWrap(True)
        pl.addWidget(self.links)
        self.tools = note()
        pl.addWidget(self.tools)
        self.preview = _preview_box(12)
        pl.addWidget(self.preview)
        pl.addWidget(note(tr("shop.review_note")))
        lay.addWidget(box, 1)
        self.refresh()

    def enabled(self) -> bool:
        return self.win.data.get("shop_enabled", True) is not False

    def showEvent(self, event) -> None:  # the catalog is read when the page is first shown
        super().showEvent(event)
        if self.index is None and self.job is None and self.enabled():
            self.load()
        else:
            self.refresh()  # the Skills page or the Tools page may have changed win.data

    def _changed(self, data: dict) -> None:
        self.win.set_data(data)
        self.win.page(SkillsPage).refresh()
        self.refresh()

    # -- catalog
    def load(self) -> None:
        if not self.enabled() or self.job is not None:
            return
        self.reload_btn.setEnabled(False)
        pill(self.state, "info", tr("shop.loading"))
        self.job = start(Job(shop.fetch_index, self._loaded))

    def _loaded(self, res) -> None:
        self.job = None
        self.reload_btn.setEnabled(True)
        self.index = res if isinstance(res, dict) else {"skills": [], "error": str(res)}
        self.refresh()

    def entries(self) -> list[dict]:
        rows = shop.search((self.index or {}).get("skills") or [], self.search.text())
        f = self.filter.currentData()
        out = []
        for e in rows:
            st = shop.status(self.win.data, e)
            keep = {"all": True, "read": not e.get("writes"), "writes": bool(e.get("writes")),
                    "installed": st in (shop.INSTALLED, shop.UPDATE), "updates": st == shop.UPDATE,
                    "builtin": st == shop.BUILTIN}[f]
            if not keep:
                continue
            out.append(dict(e, status=st))
        return out

    def refresh(self) -> None:
        cur = self.current()
        if not self.enabled():
            pill(self.state, "info", tr("shop.off"))
        elif self.index is None:
            pill(self.state, "info", tr("shop.not_loaded"))
        elif self.index.get("error"):
            pill(self.state, "warn", tr("shop.error", err=self.index["error"]))
        else:
            pill(self.state, "ok", tr("shop.loaded", n=len(self.index["skills"])))
        self.reload_btn.setEnabled(self.enabled() and self.job is None)
        self.rows = self.entries() if self.enabled() else []
        self.table.setRowCount(len(self.rows))
        for i, e in enumerate(self.rows):
            for c, text in enumerate((skills.PREFIX + e["name"], e.get("title") or "", e.get("author") or "",
                                      str(e.get("version") or ""), tr("shop.status." + e["status"]))):
                self.table.setItem(i, c, QTableWidgetItem(text))
        idx = next((i for i, e in enumerate(self.rows) if cur and e["name"] == cur["name"]), 0 if self.rows else -1)
        if idx >= 0:
            self.table.setCurrentCell(idx, 0)
        self._selected()

    def current(self) -> Optional[dict]:
        r = self.table.currentRow() if hasattr(self, "table") else -1
        return self.rows[r] if 0 <= r < len(self.rows) else None

    def _selected(self) -> None:
        e = self.current()
        st = e["status"] if e else None
        self.install_btn.setEnabled(st in (shop.AVAILABLE, shop.UPDATE, shop.CONFLICT))
        self.install_btn.setText(tr("shop.update") if st == shop.UPDATE else tr("shop.install"))
        self.remove_btn.setEnabled(st in (shop.INSTALLED, shop.UPDATE))
        if e is None:
            for w in (self.desc, self.links, self.tools):
                w.setText("")
            self.preview.setPlainText("")
            return
        self.desc.setText(e.get("description") or "")
        links = [f'<a href="https://github.com/{e["author"]}">{e["author"]}</a>'] if e.get("author") else []
        if e.get("issue"):
            links.append(f'<a href="{shop.REPO_URL}/issues/{e["issue"]}">{tr("shop.discussion")}</a>')
        if st == shop.BUILTIN:
            links.append(tr("shop.builtin_note"))
        self.links.setText(" | ".join(links))
        tools = ", ".join(e.get("tools") or []) or tr("shop.no_tools")
        self.tools.setText(tr("shop.tools.writes" if e.get("writes") else "shop.tools", tools=tools))
        item = self.items.get((e["name"], e.get("sha256")))
        if st == shop.BUILTIN:  # this P6 MCP's own copy: nothing to read from the catalog
            self.preview.setPlainText(skills.skill_md(skills.BY_NAME[e["name"]]))
        elif e.get("builtin"):  # a built-in skill of a newer P6 MCP
            self.preview.setPlainText(tr("shop.status." + shop.NEWER_APP))
        elif item is not None:
            self.preview.setPlainText(skills.skill_md(skills.from_config(shop.to_stored(item))))
        elif self.job is None:
            self.preview.setPlainText(tr("shop.loading"))
            self.job = start(Job(lambda entry=e: shop.fetch_skill(entry), self._fetched))

    def _fetched(self, res) -> None:
        self.job = None
        if isinstance(res, dict):
            self.items[(res["name"], res.get("sha256"))] = res
            self._selected()
        else:
            self.preview.setPlainText(tr("shop.error", err=res))

    # -- install, update, remove
    def ask_name(self, name: str) -> Optional[str]:
        """Another name for a skill whose name is taken here (the tests replace it)."""
        text, ok = QInputDialog.getText(self, tr("shop.install"), tr("shop.rename", name=name), text="shop-" + name)
        return text.strip() if ok and text.strip() else None

    def install(self) -> bool:
        e = self.current()
        item = self.items.get((e["name"], e.get("sha256"))) if e else None
        if item is None or e["status"] not in (shop.AVAILABLE, shop.UPDATE, shop.CONFLICT):
            return False
        as_name = None
        if e["status"] == shop.CONFLICT:
            as_name = self.ask_name(e["name"])
            if not as_name:
                return False
        elif item.get("writes") and not ask(self, tr("shop.install"), tr("shop.install.writes", name=e["name"]),
                                            warning=False):
            return False
        try:
            data = shop.install(self.win.data, item, as_name)
        except ValueError as err:
            self.win.error(str(err))
            return False
        self._changed(data)
        self.win.flash(tr("shop.installed", name=as_name or e["name"]))
        return True

    def remove(self) -> bool:
        e = self.current()
        mine = shop.installed_as(self.win.data, e["name"]) if e else None
        if mine is None or not ask(self, tr("shop.remove"), tr("skill.remove.ask", name=mine["name"]), warning=False):
            return False
        self._changed(settings_logic.remove_skill(self.win.data, mine["name"]))
        return True
