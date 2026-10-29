"""Cards of the Skills and Skill shop pages.

CardView shows Card widgets as a grid whose column count follows the width (cards at least CARD_MIN_CHARS average
characters wide, about 280 px at 100 %, never a horizontal scroll bar) or as a list of rows whose cells line up in
columns (a column that does not fit is narrowed toward its minimum, then hidden, the least useful first).
Keyboard: the view is one tab stop (its current item); the arrow keys move between items (mirrored in a
right-to-left layout), Home and End go to the first and last one, and each item maps Space, Enter, Delete and the
Menu key (or Shift+F10) to its own actions. The actions run after the key or click event returns (later()), as
they may rebuild the cards or open a modal dialog. Sizes come from the font; colours from the palette (the
Windows light or dark theme and its accent colour) and common.COLORS for the badges.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPen, QPixmap, QTextLayout, \
    QTextOption
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout,
                               QMenu, QPushButton, QScrollArea, QSizePolicy, QSpacerItem, QStyle, QToolButton,
                               QVBoxLayout, QWidget)

from p6mcp.i18n import tr
from p6mcp.settings_app.common import COLORS, muted

GRID, LIST = "grid", "list"
CARD_MIN_CHARS = 46  # minimum card width in average characters: about 280 px with Segoe UI 9 at 100 %
TITLE_MIN_CHARS = 16  # the stretching column of a list row keeps at least this width


def later(fn: Callable[[], object]) -> None:
    """Run fn once the current event has been handled."""
    QTimer.singleShot(0, fn)


def mono_font(base: QFont) -> QFont:
    """A monospace font at the size of base (Skill IDs, SKILL.md)."""
    f = QFont(base)
    f.setFamilies(["Cascadia Mono", "Consolas",
                   QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()])
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


def mix(a: QColor, b: QColor, t: float) -> QColor:
    """a moved toward b by t (0..1): borders and hover tints that follow the palette."""
    return QColor.fromRgbF(a.redF() + (b.redF() - a.redF()) * t, a.greenF() + (b.greenF() - a.greenF()) * t,
                           a.blueF() + (b.blueF() - a.blueF()) * t)


def fit_columns(columns: list[tuple[str, int, int]], budget: int, gap: int = 0) -> dict[str, int]:
    """Widths of list columns (key, natural, minimum), most useful first, that fit in budget pixels (each shown
    column also takes gap, the spacing before it): the natural widths when they fit, else each column narrowed
    toward its minimum in proportion, else the last columns hidden (width 0) one by one."""
    cols = list(columns)
    out = {k: 0 for k, _n, _m in columns}
    while cols:
        nat = sum(n + gap for _k, n, _m in cols)
        low = sum(min(m, n) + gap for _k, n, m in cols)
        if nat <= budget:
            out.update({k: n for k, n, _m in cols})
            return out
        if low <= budget:
            cut, slack = nat - budget, nat - low
            out.update({k: n - math.ceil((n - min(m, n)) * cut / slack) for k, n, m in cols})
            return out
        cols.pop()
    return out


def bold(w: QWidget) -> QWidget:
    f = w.font()
    f.setBold(True)
    w.setFont(f)
    return w


def card_button(text: str, fn: Callable[[], object]) -> QPushButton:
    """A button of a card: the card is the tab stop (its keys reach the same actions) and the action runs once
    the click has been handled (it may rebuild the cards)."""
    b = QPushButton(text)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.clicked.connect(lambda _c=False: later(fn))
    return b


def primary(b: QPushButton) -> QPushButton:
    """The main action of a card or dialog (Install, Update) filled with the Windows accent colour, as Windows 11
    draws a primary button (the windows11 style only outlines a default button). Height kept from the style."""
    pal, u = b.palette(), b.fontMetrics().averageCharWidth()
    accent, base = pal.color(QPalette.ColorRole.Accent), pal.color(QPalette.ColorRole.Base)
    ink = "#000000" if accent.lightnessF() > 0.55 else "#ffffff"
    b.setFixedHeight(b.sizeHint().height())
    b.setStyleSheet(
        f"QPushButton {{ background: {accent.name()}; color: {ink}; border: none; border-radius: {max(3, u // 2)}px;"
        f" padding: 0 {u * 2}px; }}"
        f" QPushButton:hover {{ background: {mix(accent, base, 0.12).name()}; }}"
        f" QPushButton:pressed {{ background: {mix(accent, base, 0.25).name()}; }}")
    return b


def icon_button(icon: QIcon.ThemeIcon, text: str, fn: Callable[[], object]) -> QToolButton:
    """A compact button of a list row: the Windows icon (Segoe Fluent Icons through Qt's theme icons), text as
    tooltip and accessible name; the text itself when the platform has no such icon."""
    b = QToolButton()
    theme = QIcon.fromTheme(icon)
    if theme.isNull():
        b.setText(text)
    else:
        b.setIcon(theme)
    b.setToolTip(text)
    b.setAccessibleName(text)
    b.setAutoRaise(True)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.clicked.connect(lambda _c=False: later(fn))
    return b


def more_button(card: "Card") -> QToolButton:
    """The "..." button of a card: its menu."""
    b = QToolButton()
    b.setText("...")
    b.setToolTip(tr("skill.more"))
    b.setAccessibleName(tr("skill.more"))
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.setAutoRaise(True)

    def show():
        menu = card.menu()
        if menu is not None:
            popup(menu, b)
    b.clicked.connect(show)
    card.menu_anchor = b
    return b


def popup(menu: QMenu, anchor: QWidget) -> None:
    """Open a card's menu under anchor, from its leading edge (its right edge in a right-to-left layout)."""
    menu.setLayoutDirection(anchor.layoutDirection())  # a menu is a window: it does not inherit it
    rtl = anchor.layoutDirection() == Qt.LayoutDirection.RightToLeft
    x = anchor.width() - menu.sizeHint().width() if rtl else 0
    menu.exec(anchor.mapToGlobal(QPoint(x, anchor.height())))


def retain_hidden(*widgets: QWidget) -> None:
    """Hidden widgets that keep their room (the list header lines up with the rows)."""
    for w in widgets:
        sp = w.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        w.setSizePolicy(sp)
        w.hide()


# ----------------------------------------------------------------- small widgets

class Elided(QLabel):
    """A label cut to `lines` lines, the last one ending with an ellipsis. Its text stays the full text (screen
    readers read it) and is also its tooltip."""

    def __init__(self, text: str = "", lines: int = 1, tip: bool = True):
        super().__init__()
        self.lines, self.tip = lines, tip
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setText(text)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setToolTip(text if self.tip else "")
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        fm, m = self.fontMetrics(), self.contentsMargins()
        return QSize(fm.horizontalAdvance(self.text()) + m.left() + m.right() + 1,
                     fm.lineSpacing() * self.lines + m.top() + m.bottom())

    def minimumSizeHint(self) -> QSize:
        m = self.contentsMargins()
        return QSize(self.fontMetrics().averageCharWidth() * 4 + m.left() + m.right(), self.sizeHint().height())

    def shown_lines(self, width: int) -> list[str]:
        """The lines drawn at this width (the last one elided)."""
        text, fm = " ".join(self.text().split()), self.fontMetrics()
        if self.lines <= 1 or not text:
            return [fm.elidedText(text, Qt.TextElideMode.ElideRight, width)]
        layout = QTextLayout(text, self.font())
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        opt.setTextDirection(self.layoutDirection())
        layout.setTextOption(opt)
        out, pos = [], 0
        layout.beginLayout()
        while len(out) < self.lines - 1:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            out.append(text[line.textStart():line.textStart() + line.textLength()].rstrip())
            pos = line.textStart() + line.textLength()
        layout.endLayout()
        rest = text[pos:].strip()
        if rest:
            out.append(fm.elidedText(rest, Qt.TextElideMode.ElideRight, width))
        return out

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        r, step = self.contentsRect(), self.fontMetrics().lineSpacing()
        align = QStyle.visualAlignment(self.layoutDirection(), Qt.AlignmentFlag.AlignLeft
                                       | Qt.AlignmentFlag.AlignVCenter)
        for i, line in enumerate(self.shown_lines(r.width())):
            p.drawText(QRect(r.left(), r.top() + i * step, r.width(), step), align, line)


class Badge(Elided):
    """A status pill in the colours of common.COLORS (readable in light and dark themes), cut with an ellipsis when
    its cell is narrow."""

    def __init__(self, level: str = "idle", text: str = ""):
        self.level = level
        super().__init__(text)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        u = self.fontMetrics().averageCharWidth()
        self.setContentsMargins(u, 1, u, 1)

    def set(self, level: str, text: str) -> None:
        self.level = level
        self.setText(text)

    def paintEvent(self, _event) -> None:
        fg, bg = COLORS[self.level]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(bg))
        radius = self.height() / 2
        p.drawRoundedRect(QRectF(self.rect()), radius, radius)
        p.setPen(QColor(fg))
        r = self.contentsRect()
        p.drawText(r, Qt.AlignmentFlag.AlignCenter,
                   self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, r.width()))


def _view_pixmap(kind: str, color: QColor, size: int, dpr: float) -> QPixmap:
    pm = QPixmap(round(size * dpr), round(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    s, g = float(size), size * 0.14
    if kind == GRID:  # four tiles
        c = (s - 3 * g) / 2
        for x in (g, 2 * g + c):
            for y in (g, 2 * g + c):
                p.drawRoundedRect(QRectF(x, y, c, c), c * 0.2, c * 0.2)
    else:  # three rows: a dot and a line each
        h = (s - 4 * g) / 3
        for i in range(3):
            y = g + i * (h + g)
            p.drawRoundedRect(QRectF(g, y, h, h), h * 0.3, h * 0.3)
            p.drawRoundedRect(QRectF(2 * g + h, y, s - 3 * g - h, h), h * 0.3, h * 0.3)
    p.end()
    return pm


def view_icon(kind: str, color: QColor, size: int) -> QIcon:
    """The Grid or List icon drawn in color (the theme's button text) at the usual display scales."""
    icon = QIcon()
    for dpr in (1.0, 1.25, 1.5, 2.0):
        icon.addPixmap(_view_pixmap(kind, color, size, dpr))
    return icon


class ViewToggle(QWidget):
    """Two icon buttons, Grid and List; changed(view) when the user picks the other one."""

    changed = Signal(str)

    def __init__(self, view: str, names: dict[str, str]):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QToolButton] = {}
        size = self.icon_px = self.fontMetrics().height()
        for kind in (GRID, LIST):
            b = QToolButton()
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.setIconSize(QSize(size, size))
            b.setToolTip(names[kind])
            b.setAccessibleName(names[kind])
            self.group.addButton(b)
            lay.addWidget(b)
            self.buttons[kind] = b
        self.buttons[view if view in self.buttons else GRID].setChecked(True)
        self.group.buttonToggled.connect(lambda _b, on: on and self.changed.emit(self.view()))
        self._paint_icons()

    def view(self) -> str:
        return next(k for k, b in self.buttons.items() if b.isChecked())

    def _paint_icons(self) -> None:
        color = self.palette().color(QPalette.ColorRole.ButtonText)
        for kind, b in self.buttons.items():
            b.setIcon(view_icon(kind, color, self.icon_px))

    def changeEvent(self, event) -> None:  # Windows switched between light and dark: redraw in the new colour
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange,
                            QEvent.Type.StyleChange) and hasattr(self, "buttons"):
            self._paint_icons()


class Flow(QLayout):
    """Widgets in a row that wraps onto more rows when the width is short (the page toolbars). Widgets whose
    horizontal size policy expands share the room left on their row."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._place(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._place(rect, apply=True)

    def sizeHint(self) -> QSize:
        items = [it for it in self._items if not it.isEmpty()]
        sp = max(self.spacing(), 0)
        return QSize(sum(it.sizeHint().width() for it in items) + sp * max(len(items) - 1, 0),
                     max((it.sizeHint().height() for it in items), default=0))

    def minimumSize(self) -> QSize:
        size = QSize()
        for it in self._items:
            if not it.isEmpty():
                size = size.expandedTo(it.minimumSize())
        return size

    def _place(self, rect: QRect, apply: bool) -> int:
        sp = max(self.spacing(), 0)
        lines, line, x = [], [], 0
        for it in (i for i in self._items if not i.isEmpty()):
            w = min(it.sizeHint().width(), rect.width())
            if line and x + w > rect.width():
                lines.append(line)
                line, x = [], 0
            line.append((it, w))
            x += w + sp
        if line:
            lines.append(line)
        y = rect.y()
        owner = self.parentWidget()
        rtl = owner is not None and owner.layoutDirection() == Qt.LayoutDirection.RightToLeft
        for line in lines:
            h = max(it.sizeHint().height() for it, _w in line)
            grow = [it for it, _w in line if it.widget() is not None and it.widget().sizePolicy().horizontalPolicy()
                    in (QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)]
            extra = rect.width() - sum(w for _it, w in line) - sp * (len(line) - 1)
            x = rect.x()
            for it, w in line:
                if it in grow and extra > 0:  # never past its maximum width: the rest stays at the line's end
                    w = min(w + extra // len(grow), it.maximumSize().width())
                ih = it.sizeHint().height()
                if apply:
                    r = QRect(x, y + (h - ih) // 2, w, ih)
                    if rtl:  # mirrored: the first item on the right
                        r.moveLeft(rect.x() + rect.right() - r.right())
                    it.setGeometry(r)
                x += w + sp
            y += h + sp
        return y - sp - rect.y() if lines else 0


# ----------------------------------------------------------------- cards

class Card(QFrame):
    """One item of a CardView, drawn as a card (grid) or a row (list). Subclasses lay out their widgets for
    view.mode, put the list cells whose widths the view sets in self.cells (key -> widget) and the other
    fixed-width widgets of a row in self.fixed, and set open_fn (Enter, double click), space_fn (Space),
    delete_fn (Delete) and menu() (the Menu key, Shift+F10, a right click). header=True: the list's column
    titles (no focus, no frame)."""

    open_fn: Optional[Callable[[], object]] = None
    space_fn: Optional[Callable[[], object]] = None
    delete_fn: Optional[Callable[[], object]] = None

    def __init__(self, view: "CardView", key: str, header: bool = False):
        super().__init__()
        self.view, self.key, self.header = view, key, header
        self.cells: dict[str, QWidget] = {}
        self.fixed: list[QWidget] = []
        self.menu_anchor: Optional[QWidget] = None  # where the keyboard opens the menu
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus if header else Qt.FocusPolicy.ClickFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, not header)
        self.unit = self.fontMetrics().averageCharWidth()

    def menu(self) -> Optional[QMenu]:
        return None

    def cell(self, key: str, widget: QWidget) -> QWidget:
        """A list cell holding widget at its natural width (a badge keeps its pill shape), width set by the view."""
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(widget)
        if widget.sizePolicy().horizontalPolicy() != QSizePolicy.Policy.Ignored:
            lay.addStretch(1)
        self.cells[key] = box
        return box

    def set_widths(self, widths: dict[str, int]) -> None:
        for key, box in self.cells.items():
            w = widths.get(key, 0)
            box.setVisible(w > 0)
            if w > 0:
                box.setFixedWidth(w)

    def natural(self, key: str) -> int:
        box = self.cells[key]
        return box.layout().itemAt(0).widget().sizeHint().width()

    def fixed_width(self) -> int:
        """Width of a row without its column cells (margins, buttons, the spacing between them and around the
        stretching cell); each shown column adds its width and one spacing (fit_columns gap)."""
        lay = self.layout()
        m = lay.contentsMargins()
        cells = set(self.cells.values())
        n = sum(1 for i in range(lay.count()) if lay.itemAt(i).widget() not in (None, *cells))
        fixed = sum(w.maximumWidth() if w.minimumWidth() == w.maximumWidth() else w.sizeHint().width()
                    for w in self.fixed)
        return m.left() + m.right() + max(lay.spacing(), 0) * (n - 1) + fixed

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        if self.view.mode == GRID:  # every card the same width: the columns share the row equally
            return QSize(self.unit * CARD_MIN_CHARS, hint.height())
        return hint

    def paintEvent(self, _event) -> None:
        if self.header:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        base, text = pal.color(QPalette.ColorRole.Base), pal.color(QPalette.ColorRole.Text)
        fill = mix(base, text, 0.06) if self.underMouse() else base
        focus = self.hasFocus()
        pen = QPen(pal.color(QPalette.ColorRole.Accent) if focus else mix(base, text, 0.16), 2 if focus else 1)
        p.setPen(pen)
        p.setBrush(fill)
        inset = pen.widthF() / 2
        radius = self.unit if self.view.mode == GRID else self.unit * 0.75
        p.drawRoundedRect(QRectF(self.rect()).adjusted(inset, inset, -inset, -inset), radius, radius)

    def keyPressEvent(self, event) -> None:
        k, mods = event.key(), event.modifiers()
        rtl = self.layoutDirection() == Qt.LayoutDirection.RightToLeft
        plain = mods in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier)
        if self.header or not plain:
            super().keyPressEvent(event)
            return
        side = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1}
        if k in side:
            self.view.move(self, dx=-side[k] if rtl else side[k])
        elif k in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.view.move(self, dy=-1 if k == Qt.Key.Key_Up else 1)
        elif k in (Qt.Key.Key_Home, Qt.Key.Key_End):
            self.view.focus_index(0 if k == Qt.Key.Key_Home else -1)
        else:
            fn = {Qt.Key.Key_Return: self.open_fn, Qt.Key.Key_Enter: self.open_fn, Qt.Key.Key_Space: self.space_fn,
                  Qt.Key.Key_Delete: self.delete_fn}.get(k)
            if fn is None:
                super().keyPressEvent(event)
            elif not event.isAutoRepeat():
                later(fn)
            return

    def contextMenuEvent(self, event) -> None:
        if self.header:
            return
        menu = self.menu()
        if menu is None:
            return
        self.setFocus(Qt.FocusReason.PopupFocusReason)
        if event.reason() == event.Reason.Mouse:
            menu.setLayoutDirection(self.layoutDirection())
            menu.exec(event.globalPos())
        else:
            popup(menu, self.menu_anchor if self.menu_anchor is not None and self.menu_anchor.isVisible() else self)

    def mouseDoubleClickEvent(self, event) -> None:
        if self.open_fn is not None and not self.header and event.button() == Qt.MouseButton.LeftButton:
            later(self.open_fn)
        else:
            super().mouseDoubleClickEvent(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.view.set_current(self.key)
        self.view.ensureWidgetVisible(self, 0, self.unit)
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()


class CardView(QScrollArea):
    """The cards of a page, in GRID or LIST mode; the page gives new Card widgets on every refresh (set_items)."""

    def __init__(self, mode: str = GRID):
        super().__init__()
        self.mode = mode
        self.items: list[Card] = []
        self.header: Optional[Card] = None
        self.columns: list[tuple[str, int]] = []  # list mode: (cell key, minimum characters), most useful first
        self.current: Optional[str] = None  # key of the item the tab key reaches
        self._cols = 0
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # the items take the focus
        fm = self.fontMetrics()
        self.setMinimumHeight(fm.height() * 12)
        self.inner = QWidget()
        self.grid = QGridLayout(self.inner)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(fm.averageCharWidth())
        self.empty = QWidget(self.inner)
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(0, fm.height() * 2, 0, 0)
        self.empty_text = muted(QLabel())
        self.empty_text.setWordWrap(True)
        self.empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.empty_btn = QPushButton()
        self.empty_btn.setVisible(False)
        self._empty_action: Optional[Callable[[], object]] = None
        self.empty_btn.clicked.connect(lambda: self._empty_action is not None and self._empty_action())
        el.addWidget(self.empty_text)
        el.addWidget(self.empty_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        self.setWidget(self.inner)

    # -- content
    def set_empty(self, text: str, button: Optional[str] = None,
                  action: Optional[Callable[[], object]] = None) -> None:
        """What the view says when it has no item (and an optional button, such as Retry)."""
        self.empty_text.setText(text)
        self._empty_action = action
        self.empty_btn.setVisible(button is not None)
        if button is not None:
            self.empty_btn.setText(button)

    def set_items(self, items: list[Card], header: Optional[Card] = None,
                  columns: tuple[tuple[str, int], ...] = ()) -> None:
        focus = QApplication.focusWidget()
        had_focus = focus is not None and (focus in self.items or self.empty.isAncestorOf(focus))
        if had_focus:
            focus.clearFocus()
        for w in self.items + ([self.header] if self.header is not None else []):
            w.hide()
            w.deleteLater()  # one of them may be handling the event that led here
        self.items, self.header, self.columns = list(items), header, list(columns)
        for w in self.items + ([header] if header is not None else []):
            w.setParent(self.inner)
        keys = [it.key for it in self.items]
        self._cols = 0
        self._layout()
        prev = self.empty_btn  # created with the view: new cards take its place in the tab order, not the end
        for it in self.items:
            QWidget.setTabOrder(prev, it)
            prev = it
        self.set_current(self.current if self.current in keys else (keys[0] if keys else None))
        if had_focus and self.current is not None:
            self.item(self.current).setFocus(Qt.FocusReason.OtherFocusReason)

    def item(self, key: Optional[str]) -> Optional[Card]:
        return next((it for it in self.items if it.key == key), None)

    # -- layout
    def avail(self) -> int:
        """Width for the items: the scroll bar's width is always kept free, so its coming and going never changes
        the column count."""
        return max(0, self.width() - 2 * self.frameWidth() - self.verticalScrollBar().sizeHint().width())

    def column_count(self) -> int:
        if self.mode == LIST:
            return 1
        gap = self.grid.horizontalSpacing()
        card = max([self.fontMetrics().averageCharWidth() * CARD_MIN_CHARS]  # or a wider card (long translations)
                   + [it.minimumSizeHint().width() for it in self.items])
        return max(1, (self.avail() + gap) // (card + gap))

    def _layout(self) -> None:
        g = self.grid
        while g.count():
            g.takeAt(0)
        for c in range(g.columnCount()):
            g.setColumnStretch(c, 0)
        for r in range(g.rowCount()):
            g.setRowStretch(r, 0)
        n = self._cols = self.column_count()
        row = 0
        if self.header is not None:
            self.header.setVisible(bool(self.items))
            if self.items:
                g.addWidget(self.header, 0, 0)
                row = 1
        self.empty.setVisible(not self.items)
        if not self.items:
            g.addWidget(self.empty, 0, 0, 1, n)
            row = 1
        for i, it in enumerate(self.items):
            g.addWidget(it, row + i // n, i % n)
            it.show()
        for c in range(n):
            g.setColumnStretch(c, 1)
        last = row + (len(self.items) + n - 1) // n
        g.addItem(QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding), last, 0)
        g.setRowStretch(last, 1)  # the room left goes below the items, never into the cards
        self._fit_columns()

    def _fit_columns(self) -> None:
        rows = ([self.header] if self.header is not None else []) + self.items
        if self.mode != LIST or not rows or not self.columns:
            return
        u = self.fontMetrics().averageCharWidth()
        head = self.header.natural if self.header is not None else (lambda _k: 0)
        cols = [(k, max(r.natural(k) for r in rows) + u, max(u * chars, head(k))) for k, chars in self.columns]
        widths = fit_columns(cols, self.avail() - rows[0].fixed_width() - u * TITLE_MIN_CHARS,
                             max(rows[0].layout().spacing(), 0))
        for r in rows:
            r.set_widths(widths)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.mode == GRID and self.items and self.column_count() != self._cols:
            self._layout()
        else:
            self._fit_columns()

    # -- keyboard
    def set_current(self, key: Optional[str]) -> None:
        """The item the tab key reaches (one tab stop for the whole view)."""
        self.current = key
        for it in self.items:
            it.setFocusPolicy(Qt.FocusPolicy.StrongFocus if it.key == key else Qt.FocusPolicy.ClickFocus)

    def move(self, card: Card, dx: int = 0, dy: int = 0) -> None:
        if card not in self.items:
            return
        i, n, last = self.items.index(card), max(self._cols, 1), len(self.items) - 1
        j = i + dx + dy * n
        if dy > 0 and j > last:
            j = last if last // n > i // n else i  # the shorter last row: its last card
        if 0 <= j <= last and j != i:
            self.items[j].setFocus(Qt.FocusReason.OtherFocusReason)

    def focus_index(self, index: int) -> None:
        if self.items:
            self.items[index].setFocus(Qt.FocusReason.OtherFocusReason)
