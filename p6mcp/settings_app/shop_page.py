"""The Shop page of the Settings window: P6 skills shared by users on GitHub (p6mcp.shop).

The catalog is read only when the page is first shown or on Refresh (nothing when the shop is turned off on
the Tools and safety page), and each skill only when it is needed (its details, or Install before the skill was
read); reads run on the thread pool, one at a time. The skills show as cards or list rows (settings_app.cards; the
view is window state, not a setting). Install, Update and Remove edit win.data like the Skills page: they take
effect after Save (and Install skills on the Clients page for the clients' skills folders). Restore core skills is
the Skills page's action. Built-in entries of a 1.8.2 catalog show as needing a newer MCP for Primavera P6 and are
never read.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QComboBox, QDialog, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu,
                               QProgressBar, QPushButton, QSizePolicy, QSpacerItem, QVBoxLayout, QWidget)

from p6mcp import settings_logic, shop, skills
from p6mcp.i18n import tr
from p6mcp.settings_app.cards import (GRID, LIST, Badge, Card, CardView, Elided, Flow, ViewToggle, bold,
                                      card_button, icon_button, later, mono_font, primary, retain_hidden)
from p6mcp.settings_app.common import Job, ask, muted, note, pill, start
from p6mcp.settings_app.guidance_pages import SkillsPage
from p6mcp.settings_app.skill_details import Action, SkillDetails

FILTERS = ("all", "read", "writes", "installed", "updates")
STATUS_LEVEL = {shop.INSTALLED: "ok", shop.UPDATE: "warn", shop.CONFLICT: "warn", shop.NEWER_APP: "warn"}
INSTALLABLE = (shop.AVAILABLE, shop.UPDATE, shop.CONFLICT)  # Install (Update for UPDATE) is offered
REMOVABLE = (shop.INSTALLED, shop.UPDATE)
LIST_COLUMNS = (("id", 10), ("state", 8), ("author", 6), ("version", 4))  # list cells, hidden last first


def state(e: dict) -> tuple[str, str]:
    """(badge level, text) of a catalog row: the values of the former Here column."""
    return STATUS_LEVEL.get(e["status"], "idle"), \
        tr("shop.status." + e["status"]) + (tr("shop.edited") if e["edited"] else "")


class ShopCard(Card):
    """A skill of the catalog: a card (grid) or a row (list); without an entry, the list's column titles."""

    def __init__(self, page: "ShopPage", e: Optional[dict] = None):
        super().__init__(page.view, e["name"] if e else "", header=e is None)
        self.page, self.e = page, e
        key, u = self.key, self.unit
        grid = page.view.mode == GRID
        if e is None:
            self.ident, self.title = Elided(tr("shop.col.skill")), Elided(tr("shop.col.title"))
            self.author, self.version = Elided(tr("shop.col.author")), Elided(tr("shop.col.version"))
            self.badge = Elided(tr("shop.col.status"))
            for w in (self.ident, self.title, self.author, self.version, self.badge):
                muted(bold(w))
            self.badge.setContentsMargins(u, 0, u, 0)  # in line with the text of the badges below
            self.install_btn, self.remove_btn = QPushButton(), QPushButton()
            self.details_btn = icon_button(QIcon.ThemeIcon.DialogInformation, tr("skill.details"), lambda: None)
            self._row(page.actions_width)
            retain_hidden(self.actions, self.details_btn)
            return
        st, ident = e["status"], skills.PREFIX + key
        reading = key in page.reading
        self.ident = Elided(ident)
        self.ident.setFont(mono_font(self.font()))
        self.title = bold(Elided(e.get("title") or ""))
        author, version = e.get("author") or "", str(e.get("version") or "")
        self.author = muted(Elided(tr("shop.by", author=author) if grid and author else author))
        self.version = muted(Elided(tr("shop.version", version=version) if grid and version else version))
        if grid:  # side by side at their own width
            for w in (self.author, self.version):
                w.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.badge = Badge(*state(e))
        self.desc = muted(Elided(e.get("description") or "", lines=2))
        self.install_btn = card_button(tr("shop.reading") if reading else
                                       tr("shop.update") if st == shop.UPDATE else tr("shop.install"),
                                       lambda: page.request_install(key))
        if st not in INSTALLABLE and st != shop.NEWER_APP:
            self.install_btn.hide()  # hide() only: a widget shown before it has a parent opens as a window
        self.install_btn.setEnabled(st in INSTALLABLE and not reading)
        if st in INSTALLABLE and not reading:
            primary(self.install_btn)  # the main action of the card (accent colour)
        self.install_btn.setToolTip(tr("shop.status." + st) if st in (shop.NEWER_APP, shop.CONFLICT) else "")
        self.install_btn.setAccessibleName(tr("cards.action", action=self.install_btn.text(), name=ident))
        self.remove_btn = card_button(tr("shop.remove"), lambda: page.run(key, page.remove))
        self.remove_btn.setAccessibleName(tr("cards.action", action=tr("shop.remove"), name=ident))
        # a list row has room for one action: Update or Install, else Remove (the menu and the details have both)
        if not (st in REMOVABLE if grid else st == shop.INSTALLED):
            self.remove_btn.hide()

        def details():
            page.run(key, page.details)
        self.details_btn = card_button(tr("skill.details"), details) if grid else \
            icon_button(QIcon.ThemeIcon.DialogInformation, tr("skill.details"), details)
        self.details_btn.setAccessibleName(tr("cards.action", action=tr("skill.details"), name=ident))
        self.open_fn = lambda: page.run(key, page.details)
        self.delete_fn = lambda: page.run(key, page.remove) if (page.row(key) or {}).get("status") in REMOVABLE \
            else None
        self.setAccessibleName(tr("cards.name", id=ident, title=e["title"]) if e.get("title") else ident)
        self.setAccessibleDescription(" ".join(x for x in (
            e.get("description") or "",
            tr("cards.field", label=tr("shop.col.author"), value=author) if author else "",
            tr("cards.field", label=tr("shop.col.version"), value=version) if version else "",
            tr("cards.field", label=tr("shop.col.status"), value=state(e)[1]), tr("cards.keys.shop")) if x))
        if not grid:
            self._row(page.actions_width)
            return
        g = QGridLayout(self)
        g.setContentsMargins(u * 2, u * 2, u * 2, u * 2)
        g.setHorizontalSpacing(u)
        g.setVerticalSpacing(u // 2)
        g.addWidget(self.ident, 0, 0)
        g.addWidget(self.badge, 0, 1, Qt.AlignmentFlag.AlignRight)
        g.addWidget(self.title, 1, 0, 1, 2)
        by = QHBoxLayout()
        by.setSpacing(u * 2)
        by.addWidget(self.author)
        by.addWidget(self.version)
        by.addStretch(1)
        g.addLayout(by, 2, 0, 1, 2)
        g.addWidget(self.desc, 3, 0, 1, 2)
        g.addItem(QSpacerItem(0, u, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding), 4, 0)
        buttons = QHBoxLayout()
        buttons.setSpacing(u)
        buttons.addWidget(self.install_btn)
        buttons.addWidget(self.remove_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.details_btn)
        g.addLayout(buttons, 5, 0, 1, 2)
        g.setColumnStretch(0, 1)

    def _row(self, actions_width: int) -> None:
        u = self.unit
        self.actions = QWidget()
        al = QHBoxLayout(self.actions)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(u)
        al.addWidget(self.install_btn)
        al.addWidget(self.remove_btn)
        al.addStretch(1)
        self.actions.setFixedWidth(actions_width)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(u, u // 2, u, u // 2)
        lay.setSpacing(u)
        lay.addWidget(self.cell("id", self.ident))
        lay.addWidget(self.title, 1)
        lay.addWidget(self.cell("author", self.author))
        lay.addWidget(self.cell("version", self.version))
        lay.addWidget(self.cell("state", self.badge))
        lay.addWidget(self.actions)
        lay.addWidget(self.details_btn)
        self.fixed = [self.actions, self.details_btn]

    def menu(self) -> Optional[QMenu]:
        return self.page.menu_for(self.key, self) if self.e else None


class ShopPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.index: Optional[dict] = None  # fetch_index() result
        self.items: dict[tuple, dict] = {}  # (name, sha256) -> fetch_skill() result
        self.rows: list[dict] = []
        self.job = None
        self.loading = False  # the catalog is being read (busy bar)
        self.waiting: list[tuple] = []  # (entry, then, failed): skills to read, one at a time
        self.reading: set[str] = set()  # skills whose Install waits for their read
        self.dialog: Optional[SkillDetails] = None
        self.stale = False  # a skill read was newer than the index: read the index again when idle
        lay = QVBoxLayout(self)
        lay.addWidget(note(tr("shop.intro", repo=shop.REPO)))

        bar = QHBoxLayout()
        flow = Flow()
        flow.setSpacing(self.fontMetrics().averageCharWidth())
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("shop.search"))
        self.search.setAccessibleName(tr("shop.search"))
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(self.fontMetrics().averageCharWidth() * 30)
        self.search.setMaximumWidth(self.fontMetrics().averageCharWidth() * 60)
        self.search.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.search.textChanged.connect(lambda *_a: self.refresh())
        self.filter = QComboBox()
        for f in FILTERS:
            self.filter.addItem(tr("shop.filter." + f), f)
        self.filter.setAccessibleName(tr("shop.filter"))
        self.filter.setToolTip(tr("shop.filter"))
        self.filter.currentIndexChanged.connect(lambda *_a: self.refresh())
        self.reload_btn = QPushButton(tr("shop.reload"))
        self.reload_btn.clicked.connect(self.load)
        self.restore_btn = QPushButton(tr("skill.restore"))
        self.restore_btn.clicked.connect(lambda: self.win.page(SkillsPage).restore_core())
        for w in (self.search, self.filter, self.reload_btn, self.restore_btn):
            flow.addWidget(w)
        bar.addLayout(flow, 1)
        view = settings_logic.ui_view(settings_logic.load_ui_state(), "shop")
        self.toggle = ViewToggle(view, {GRID: tr("view.grid"), LIST: tr("view.list")})
        self.toggle.changed.connect(self.set_view)
        bar.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(bar)
        self.state = QLabel()
        self.state.setWordWrap(True)
        lay.addWidget(self.state)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)  # busy, no percentage: the catalog is one small file
        self.busy.setTextVisible(False)
        self.busy.setMaximumHeight(max(4, self.fontMetrics().height() // 3))
        self.busy.setAccessibleName(tr("shop.loading"))
        self.busy.setVisible(False)
        lay.addWidget(self.busy)
        self.view = CardView(view)
        lay.addWidget(self.view, 1)
        lay.addWidget(note(tr("shop.review_note")))
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

    def set_view(self, view: str) -> None:
        """Grid or List: kept in the window state file (not a setting: no Save needed)."""
        settings_logic.save_ui_state(shop_view=view)
        self.view.mode = view
        self.refresh()

    # -- catalog
    def load(self) -> None:
        if not self.enabled() or self.job is not None:
            return
        self.loading = True
        self.reload_btn.setEnabled(False)
        self.busy.setVisible(True)
        pill(self.state, "info", tr("shop.loading"))
        self.job = start(Job(shop.fetch_index, self._loaded))
        self.refresh()  # the busy state replaces an error and its Retry button

    def _loaded(self, res) -> None:
        self.job = None
        self.loading = False
        self.index = res if isinstance(res, dict) else {"skills": [], "error": str(res)}
        self.refresh()
        self._next()

    def _entry(self, e: dict) -> dict:
        mine = shop.installed_as(self.win.data, e["name"])
        return dict(e, status=shop.status(self.win.data, e), edited=bool(mine and skills.edited(mine)))

    def entries(self) -> list[dict]:
        rows = shop.search((self.index or {}).get("skills") or [], self.search.text())
        f = self.filter.currentData()
        out = []
        for e in rows:
            e = self._entry(e)
            st = e["status"]
            keep = {"all": True, "read": not e.get("writes"), "writes": bool(e.get("writes")),
                    "installed": st in (shop.INSTALLED, shop.UPDATE), "updates": st == shop.UPDATE}[f]
            if keep:
                out.append(e)
        return out

    def refresh(self) -> None:
        catalog = (self.index or {}).get("skills") or []
        if not self.enabled():
            pill(self.state, "info", tr("shop.off"))
            self.view.set_empty("")
        elif self.loading:
            pill(self.state, "info", tr("shop.loading"))
            self.view.set_empty("")
        elif self.index is None:
            pill(self.state, "info", tr("shop.not_loaded"))
            self.view.set_empty("")
        elif self.index.get("error"):
            pill(self.state, "warn", tr("shop.error", err=self.index["error"]))
            self.view.set_empty(tr("shop.unreachable"), tr("shop.retry"), self.load)
        else:
            pill(self.state, "ok", tr("shop.loaded", n=len(self.index["skills"])))
            self.view.set_empty(tr("cards.no_match") if catalog else tr("shop.empty"))
        self.reload_btn.setEnabled(self.enabled() and self.job is None)
        self.busy.setVisible(self.loading)
        self.rows = self.entries() if self.enabled() else []
        self.actions_width = self._actions_width()
        header = ShopCard(self) if self.view.mode == LIST else None
        self.view.set_items([ShopCard(self, e) for e in self.rows], header, LIST_COLUMNS)
        if self.dialog is not None:
            self._fill_dialog()

    def _actions_width(self) -> int:
        """Width of the action cell of a list row: its widest button."""
        b = QPushButton()
        width = 0
        for key in ("shop.install", "shop.update", "shop.remove", "shop.reading"):
            b.setText(tr(key))
            width = max(width, b.sizeHint().width())
        return max([width] + [primary(QPushButton(tr(k))).sizeHint().width() for k in ("shop.install", "shop.update")])

    def current(self) -> Optional[dict]:
        key = self.view.current if hasattr(self, "view") else None
        return self.row(key)

    def row(self, name: Optional[str]) -> Optional[dict]:
        return next((e for e in self.rows if e["name"] == name), None)

    def entry(self, name: Optional[str]) -> Optional[dict]:
        """A skill of the catalog with its state here: its row, else from the whole catalog (the details dialog and
        an Install waiting for its read act on a skill the search or the filter may hide meanwhile)."""
        if name is None or not self.enabled():
            return None
        return self.row(name) or next((self._entry(x) for x in (self.index or {}).get("skills") or []
                                       if x["name"] == name), None)

    def run(self, name: str, fn) -> object:
        """fn (an action of the page, on the current skill) for the skill name."""
        if self.row(name) is None:
            return None
        self.view.set_current(name)
        return fn()

    def menu_for(self, name: str, parent: QWidget) -> QMenu:
        """The menu of a card (the Menu key, Shift+F10, a right click)."""
        e = self.row(name)
        menu = QMenu(parent)
        menu.setLayoutDirection(parent.layoutDirection())
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)  # built for each opening
        menu.setToolTipsVisible(True)
        st = e["status"]
        for key, label, fn, on in (
                ("install", "shop.update" if st == shop.UPDATE else "shop.install",
                 lambda: self.request_install(name), st in INSTALLABLE and name not in self.reading),
                ("remove", "shop.remove", lambda: self.run(name, self.remove), st in REMOVABLE),
                ("details", "skill.details", lambda: self.run(name, self.details), True)):
            if key == "details":
                menu.addSeparator()
            act = menu.addAction(tr(label))
            act.setObjectName(key)
            act.setEnabled(on)
            act.triggered.connect(lambda _c=False, f=fn: later(f))
        return menu

    # -- one skill of the catalog
    def read(self, e: dict, then: Callable[[dict], object], failed: Optional[Callable[[object], object]] = None
             ) -> None:
        """Read one skill (shop.fetch_skill checks its sha256 against the index) on the thread pool, then call
        then(item); a skill read before is not read again."""
        item = self.items.get((e["name"], e.get("sha256")))
        if item is not None:
            then(item)
            return
        self.waiting.append((e, then, failed))
        self._next()

    def _next(self) -> None:
        while self.job is None and self.waiting:
            entry, then, _failed = self.waiting[0]
            item = self.items.get((entry["name"], entry.get("sha256")))
            if item is None:
                self.reload_btn.setEnabled(False)
                self.job = start(Job(lambda e=entry: shop.fetch_skill(e), self._fetched))
                return
            self.waiting.pop(0)  # asked twice (a card and the details): read once
            then(item)
        if self.job is None and self.stale:  # a skill was newer than the index: read the index again
            self.stale = False
            self.load()

    def _fetched(self, res) -> None:
        self.job = None
        entry, then, failed = self.waiting.pop(0)
        key = (entry["name"], entry.get("sha256"))
        calls = [(then, failed)]
        if isinstance(res, dict):
            # also under the key asked for: fetch_skill gives the newer version when the catalog changed since
            # its index was read (sha256 of the fresh index), so the Install asked for still runs
            self.items[key] = self.items[(res["name"], res.get("sha256"))] = res
            self.stale = self.stale or res.get("sha256") != entry.get("sha256")
        else:  # the other waits for the same skill share this error instead of reading it again
            calls += [(t, f) for e, t, f in self.waiting if (e["name"], e.get("sha256")) == key]
            self.waiting = [w for w in self.waiting if (w[0]["name"], w[0].get("sha256")) != key]
        self._next()  # the next read starts before a callback opens a question
        self.reload_btn.setEnabled(self.enabled() and self.job is None)
        if isinstance(res, dict):
            then(res)
            return
        for _then, fail in calls:
            if fail is not None:
                fail(res)
        if any(fail is None for _then, fail in calls):
            self.win.error(tr("shop.error", err=res))

    @staticmethod
    def _md(item: dict) -> str:
        return skills.skill_md(skills.from_config(shop.to_stored(item)))

    # -- details
    def show_dialog(self, dlg: QDialog) -> None:
        """Show a modal dialog, then free it (the tests replace it)."""
        dlg.exec()
        dlg.deleteLater()

    def info(self, e: dict) -> dict:
        return {"key": e["name"], "title": e.get("title") or skills.PREFIX + e["name"],
                "name": skills.PREFIX + e["name"], "author": e.get("author"), "version": e.get("version"),
                "state": state(e), "issue_url": f"{shop.REPO_URL}/issues/{e['issue']}" if e.get("issue") else None,
                "description": e.get("description") or "", "tools": e.get("tools") or [],
                "writes": bool(e.get("writes")), "md": "", "note": tr("shop.review_note")}

    def dialog_actions(self, e: dict) -> list[Action]:
        st, name = e["status"], e["name"]
        out = []
        if st in INSTALLABLE or st == shop.NEWER_APP:
            out.append(Action("install", tr("shop.update") if st == shop.UPDATE else tr("shop.install"),
                              lambda: self.request_install(name), st in INSTALLABLE and name not in self.reading,
                              tr("shop.status." + st) if st in (shop.NEWER_APP, shop.CONFLICT) else "",
                              primary=True))
        if st in REMOVABLE:
            out.append(Action("remove", tr("shop.remove"), lambda: self.remove(name)))
        return out

    def details(self) -> Optional[SkillDetails]:
        """The details dialog of the current skill: SKILL.md is read when the skill was not read yet; Install,
        Update and Remove act from it and it follows their result."""
        e = self.current()
        if e is None:
            return None
        dlg = self.dialog = SkillDetails(self, self.info(e), self.dialog_actions(e))
        dlg.finished.connect(lambda _r: setattr(self, "dialog", None) if self.dialog is dlg else None)
        self._show_md(e)
        self.show_dialog(dlg)
        return dlg

    def _fill_dialog(self) -> None:
        """The open dialog after an install or a removal (also when the filter no longer shows the skill)."""
        e = self.entry(self.dialog.info.get("key"))
        if e is None:
            return
        text = self.dialog.md.toPlainText()
        self.dialog.fill(self.info(e), self.dialog_actions(e))
        if e.get("builtin") or (e["name"], e.get("sha256")) in self.items:
            self._show_md(e)
        else:  # a read under way fills it, a failed one keeps its error: never a new read from here
            self.dialog.set_md(text)

    def _show_md(self, e: dict) -> None:
        dlg = self.dialog
        if e.get("builtin"):  # a built-in skill of P6 MCP 1.8.2 in an older catalog: nothing to read
            dlg.set_md(tr("shop.status." + shop.NEWER_APP))
            return
        item = self.items.get((e["name"], e.get("sha256")))
        if item is not None:
            dlg.set_md(self._md(item))
            return
        dlg.set_md(tr("shop.loading"))

        def show(it):
            if self.dialog is dlg and dlg.info.get("key") == it["name"]:
                dlg.set_md(self._md(it))

        def failed(err):
            if self.dialog is dlg:
                dlg.set_md(tr("shop.error", err=err))
        self.read(e, show, failed)

    # -- install, update, remove
    def request_install(self, name: str) -> None:
        """Install or Update from a card or the details: the skill is read first when it was not (sha256 checked),
        then install() asks its questions as before."""
        e = self.entry(name)
        if e is None or e["status"] not in INSTALLABLE or name in self.reading:
            return
        if self.view.item(name) is not None:
            self.view.set_current(name)
        if (e["name"], e.get("sha256")) in self.items:
            self.install(name)
            return
        self.reading.add(name)
        self.refresh()  # the card says Reading...

        def done(_item):
            self.reading.discard(name)
            self.refresh()
            self.install(name)  # also when the search or the filter hid the card meanwhile

        def failed(err):
            self.reading.discard(name)
            self.refresh()
            self.win.error(tr("shop.error", err=err))
        self.read(e, done, failed)

    def ask_name(self, name: str) -> Optional[str]:
        """Another name for a skill whose name is taken here (the tests replace it)."""
        text, ok = QInputDialog.getText(self, tr("shop.install"), tr("shop.rename", name=name), text="shop-" + name)
        return text.strip() if ok and text.strip() else None

    def install(self, name: Optional[str] = None) -> bool:
        e = self.entry(name) if name else self.current()
        item = self.items.get((e["name"], e.get("sha256"))) if e else None
        if item is None or e["status"] not in INSTALLABLE:
            return False
        as_name = None
        mine = shop.installed_as(self.win.data, e["name"])
        if e["status"] == shop.CONFLICT:
            as_name = self.ask_name(e["name"])
            if not as_name:
                return False
        elif mine is not None and skills.edited(mine) and not ask(
                self, tr("shop.update"), tr("shop.update.edited", name=mine["name"]), warning=True):
            return False  # the automatic update leaves an edited skill alone: replacing it is the user's choice
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

    def remove(self, name: Optional[str] = None) -> bool:
        e = self.entry(name) if name else self.current()
        mine = shop.installed_as(self.win.data, e["name"]) if e else None
        if mine is None or not ask(self, tr("shop.remove"), tr("skill.remove.ask", name=mine["name"]), warning=False):
            return False
        self._changed(settings_logic.remove_skill(self.win.data, mine["name"]))
        return True
