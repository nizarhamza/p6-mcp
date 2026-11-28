"""Accessibility of the Settings window: accessible names, live announcements, access keys, the focus ring,
contrast and Windows high contrast mode.

What Qt 6 on Windows gives assistive technologies (Narrator, NVDA) through UI Automation:
- Name: a widget's accessibleName; else a button's text (access key "&" removed); else the text of a QLabel whose
  buddy it is (a QFormLayout row label is its field's buddy); else the title of the QGroupBox it sits in. A QLabel's
  name is its text (rich text turned into plain text).
- HelpText (UIA FullDescription / HelpText): accessibleDescription, else the tooltip.
- AccessKey: "Alt+X" from the "&X" of a button, check box, radio button or of a label with a buddy.
- Events: a QLabel raises NameChanged when its text changes, but screen readers only read name changes of the
  focused element; a status that changes elsewhere (test results, copy progress, update state) is therefore also
  sent as a QAccessibleAnnouncementEvent (Qt 6.8+), which Qt raises as a UIA Notification event: Narrator and
  NVDA read it at once without moving the focus (announce()).
"""

from __future__ import annotations

import re
import sys
from itertools import pairwise
from typing import Iterable, Optional

from PySide6.QtCore import QEvent, QObject, QRect, Qt
from PySide6.QtGui import (QAccessible, QAccessibleAnnouncementEvent, QColor, QGuiApplication, QKeySequence,
                           QPainter, QPalette, QPen)
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractSpinBox, QApplication, QComboBox,
                               QFormLayout, QLabel, QLayout, QLineEdit, QPlainTextEdit, QStyle, QStyledItemDelegate,
                               QStyleOptionViewItem, QTextEdit, QWidget)

TEXT_MIN = 4.5  # WCAG 2.1 AA, body text
LARGE_MIN = 3.0  # large text (18 pt, 14 pt bold), UI component borders and the focus indicator
FOCUS_WIDTH = 2  # logical pixels of the focus ring
HIGH_CONTRAST: Optional[bool] = None  # tests force the mode; None = ask Windows (through Qt)
ANNOUNCED: list[tuple[str, str]] = []  # (politeness, text) of the last announcements, for the tests
MNEMONIC_ROLE = Qt.ItemDataRole.UserRole + 41  # a list item's text with its access key ("&Database")
INTERACTIVE = (QAbstractButton, QLineEdit, QComboBox, QAbstractSpinBox, QAbstractItemView, QPlainTextEdit, QTextEdit)


# ----------------------------------------------------------------- contrast

def _channel(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def over(fg: QColor, bg: QColor) -> QColor:
    """fg drawn over the opaque bg (palette colours such as the placeholder text are translucent)."""
    a = fg.alphaF()
    return QColor.fromRgbF(fg.redF() * a + bg.redF() * (1 - a), fg.greenF() * a + bg.greenF() * (1 - a),
                           fg.blueF() * a + bg.blueF() * (1 - a))


def luminance(color) -> float:
    c = QColor(color)
    return 0.2126 * _channel(c.redF()) + 0.7152 * _channel(c.greenF()) + 0.0722 * _channel(c.blueF())


def contrast(fg, bg) -> float:
    """WCAG 2.1 contrast ratio of fg (composited over bg when translucent) against bg, 1 to 21."""
    bg = QColor(bg)
    bg.setAlphaF(1.0)
    hi, lo = sorted((luminance(over(QColor(fg), bg)), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def readable(fg, bg, minimum: float = TEXT_MIN) -> QColor:
    """fg, or the closest colour toward black or white (whichever side contrasts more with bg) that reaches
    minimum against bg: the hue is kept as long as possible."""
    bg = QColor(bg)
    base = over(QColor(fg), bg)
    if contrast(base, bg) >= minimum:
        return base
    target = QColor("#000000") if contrast("#000000", bg) >= contrast("#ffffff", bg) else QColor("#ffffff")
    for step in range(1, 21):
        t = step / 20
        c = QColor.fromRgbF(base.redF() + (target.redF() - base.redF()) * t,
                            base.greenF() + (target.greenF() - base.greenF()) * t,
                            base.blueF() + (target.blueF() - base.blueF()) * t)
        if contrast(c, bg) >= minimum:
            return c
    return target


def ink(bg) -> QColor:
    """Black or white, whichever reads better on bg."""
    return QColor("#000000") if contrast("#000000", bg) >= contrast("#ffffff", bg) else QColor("#ffffff")


def high_contrast() -> bool:
    """Windows high contrast (a contrast theme) is on: the window then keeps the system colours only."""
    if HIGH_CONTRAST is not None:
        return HIGH_CONTRAST
    try:
        hints = QGuiApplication.styleHints().accessibility()
        if hints.contrastPreference() == Qt.ContrastPreference.HighContrast:
            return True
    except (AttributeError, RuntimeError):  # Qt before 6.10
        pass
    return _windows_high_contrast()


def _windows_high_contrast() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class HIGHCONTRAST(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD), ("lpszDefaultScheme", wintypes.LPWSTR)]

    hc = HIGHCONTRAST()
    hc.cbSize = ctypes.sizeof(HIGHCONTRAST)
    try:
        if not ctypes.windll.user32.SystemParametersInfoW(0x0042, hc.cbSize, ctypes.byref(hc), 0):
            return False
    except (AttributeError, OSError):
        return False
    return bool(hc.dwFlags & 0x1)  # HCF_HIGHCONTRASTON


_NORMAL_STYLE: Optional[str] = None  # the style the application started with (windows11)
HC_STYLE = "windows"  # the classic style draws everything from the palette: the contrast theme's colours


def system_palette() -> QPalette:
    """The Windows system colours (GetSysColor: the contrast theme's colours while high contrast is on); the
    application palette elsewhere."""
    pal = QPalette(QGuiApplication.palette())
    if sys.platform != "win32":
        return pal
    import ctypes
    get = ctypes.windll.user32.GetSysColor

    def sys_color(index: int) -> QColor:
        v = get(index)
        return QColor(v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)

    window, text, face, btn_text = sys_color(5), sys_color(8), sys_color(15), sys_color(18)
    highlight, hl_text, gray, link = sys_color(13), sys_color(14), sys_color(17), sys_color(26)
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        disabled = group == QPalette.ColorGroup.Disabled
        for role, c in ((QPalette.ColorRole.Window, window), (QPalette.ColorRole.Base, window),
                        (QPalette.ColorRole.AlternateBase, window), (QPalette.ColorRole.Button, face),
                        (QPalette.ColorRole.WindowText, gray if disabled else text),
                        (QPalette.ColorRole.Text, gray if disabled else text),
                        (QPalette.ColorRole.ButtonText, gray if disabled else btn_text),
                        (QPalette.ColorRole.PlaceholderText, gray if disabled else text),
                        (QPalette.ColorRole.Highlight, highlight), (QPalette.ColorRole.HighlightedText, hl_text),
                        (QPalette.ColorRole.Accent, highlight), (QPalette.ColorRole.Link, link),
                        (QPalette.ColorRole.ToolTipBase, window), (QPalette.ColorRole.ToolTipText, text),
                        (QPalette.ColorRole.Light, text), (QPalette.ColorRole.Midlight, text),
                        (QPalette.ColorRole.Mid, text), (QPalette.ColorRole.Dark, text),
                        (QPalette.ColorRole.Shadow, text)):
            pal.setColor(group, role, c)
    return pal


def apply_contrast_mode(app: Optional[QApplication] = None) -> bool:
    """High contrast on: the classic Windows style and the system colours, nothing of ours (the Windows 11 style
    keeps its own greys and fills, unreadable in a contrast theme); off again: the style the app started with and
    the default palette. True when high contrast is on."""
    global _NORMAL_STYLE
    app = app or QApplication.instance()
    if app is None:
        return False
    if _NORMAL_STYLE is None:
        _NORMAL_STYLE = app.style().name()
    on = high_contrast()
    if on:
        if app.style().name() != HC_STYLE:
            app.setStyle(HC_STYLE)
        app.setPalette(system_palette())
    elif app.style().name() != _NORMAL_STYLE:
        app.setStyle(_NORMAL_STYLE)
        app.setPalette(QPalette())
    return on


def focus_color(pal: QPalette) -> QColor:
    """The focus indicator: the Windows accent colour when it reaches 3:1 against both the window and the field
    backgrounds, else the highlight colour, else the text colour (always the system colours in high contrast)."""
    window, base = pal.color(QPalette.ColorRole.Window), pal.color(QPalette.ColorRole.Base)
    roles = (QPalette.ColorRole.Highlight, QPalette.ColorRole.WindowText) if high_contrast() else (
        QPalette.ColorRole.Accent, QPalette.ColorRole.Highlight, QPalette.ColorRole.WindowText)
    for role in roles:
        c = pal.color(role)
        if contrast(c, window) >= LARGE_MIN and contrast(c, base) >= LARGE_MIN:
            return over(c, window)
    return readable(pal.color(QPalette.ColorRole.WindowText), window, LARGE_MIN)


def muted_color(pal: QPalette) -> QColor:
    """Secondary text: the theme's placeholder colour (translucent in the Windows 11 style: about 3.9:1 on the
    light window background), darkened or lightened just enough for 4.5:1 on both the window and the field (card)
    backgrounds; the text colour in high contrast mode."""
    if high_contrast():
        return pal.color(QPalette.ColorRole.WindowText)
    window, base = pal.color(QPalette.ColorRole.Window), pal.color(QPalette.ColorRole.Base)
    c = readable(pal.color(QPalette.ColorRole.PlaceholderText), window, TEXT_MIN)
    return c if contrast(c, base) >= TEXT_MIN else readable(c, base, TEXT_MIN)


# ----------------------------------------------------------------- names

def name(widget: QWidget, text: str, description: Optional[str] = None) -> QWidget:
    """Accessible name (and description) of a widget whose visible text is missing or not enough."""
    widget.setAccessibleName(strip_amp(text))
    if description is not None:
        widget.setAccessibleDescription(description)
    return widget


def strip_amp(text: str) -> str:
    return re.sub(r"&(.)", r"\1", text or "")


def exposed_name(widget: QWidget) -> str:
    """The name UI Automation reads for widget (Qt's accessible interface)."""
    iface = QAccessible.queryAccessibleInterface(widget)
    return (iface.text(QAccessible.Text.Name) or "").strip() if iface is not None and iface.isValid() else ""


def _internal(w: QWidget) -> bool:
    """A part of another widget (the line edit of a combo box or spin box, the clear button of a line edit, the
    headers and corner button of a table): its owner carries the name."""
    p = w.parentWidget()
    while p is not None:
        if isinstance(p, (QComboBox, QAbstractSpinBox, QLineEdit, QAbstractItemView)):
            return True
        p = p.parentWidget()
    return False


def _labelled(w: QWidget) -> bool:
    """Named on purpose: an accessibleName, the text of a button, or a label whose buddy it is."""
    if w.accessibleName().strip():
        return True
    if isinstance(w, QAbstractButton) and strip_amp(w.text()).strip():
        return True
    parent = w.parentWidget()
    return parent is not None and any(isinstance(o, QLabel) and o.buddy() is w and o.text().strip()
                                      for o in parent.children())


def interactive(root: QWidget, focusable_only: bool = False) -> Iterable[QWidget]:
    """The widgets of root a user acts on (buttons, fields, lists, cards that take the focus)."""
    from p6mcp.settings_app.cards import Card
    for w in [root] + root.findChildren(QWidget):
        if _internal(w):
            continue
        if isinstance(w, Card):
            if w.header or w.focusPolicy() == Qt.FocusPolicy.NoFocus:
                continue
        elif not isinstance(w, INTERACTIVE):
            continue
        if focusable_only and w.focusPolicy() == Qt.FocusPolicy.NoFocus:
            continue
        yield w


def problems(root: QWidget) -> list[str]:
    """Interactive widgets of root without a name given on purpose (accessibleName, button text, buddy label);
    the group box title Qt would fall back on is not enough. Empty when everything is named."""
    out = []
    for w in interactive(root):
        if w.isHidden() and isinstance(w, QAbstractButton) and not w.text():
            continue  # a button given its text when it is shown (banner, Retry, remove a v2 entry)
        if not _labelled(w):
            hint = w.toolTip() or getattr(w, "placeholderText", lambda: "")() or w.objectName()
            out.append(f"{type(w).__name__} in {type(w.parentWidget()).__name__} ({hint!r}) exposed as "
                       f"{exposed_name(w)!r}")
    return out


def tab_chain(root: QWidget) -> list[QWidget]:
    """The widgets of root the Tab key reaches (shown, enabled, taking the focus by Tab), in Tab order."""
    out, w = [], root.nextInFocusChain()
    window = root.window()
    for _ in range(5000):
        if w is root or w is None:
            break
        if (root.isAncestorOf(w) and w.isVisibleTo(window) and w.isEnabled() and not _internal(w)
                and w.focusPolicy().value & Qt.FocusPolicy.TabFocus.value):
            out.append(w)
        w = w.nextInFocusChain()
    return out


def tab_order_problems(root: QWidget) -> list[str]:
    """Pairs of widgets of root whose Tab order goes against the reading order: the next one sits wholly above the
    previous one, or on the same line wholly before it (left of it, right of it in a right-to-left layout). A widget
    marked a11y_bar_end (the Grid / List switch at the top end of a toolbar that wraps) comes after the toolbar."""
    rtl = root.layoutDirection() == Qt.LayoutDirection.RightToLeft
    chain = tab_chain(root)
    rects = [QRect(w.mapTo(root, w.rect().topLeft()), w.size()) for w in chain]
    out = []
    for (a, ra), (b, rb) in pairwise(zip(chain, rects, strict=True)):
        above = rb.bottom() < ra.top()
        same_line = rb.top() <= ra.bottom() and ra.top() <= rb.bottom()
        before = same_line and (rb.left() > ra.right() if rtl else rb.right() < ra.left())
        if (above or before) and not b.property("a11y_bar_end"):
            out.append(f"{_describe(a)} -> {_describe(b)}")
    return out


def _describe(w: QWidget) -> str:
    text = getattr(w, "text", None)
    return f"{type(w).__name__}({strip_amp(text()) if callable(text) else w.accessibleName()!r})"


def tie_forms(root: QWidget) -> None:
    """Each QFormLayout row label becomes the buddy of its field when the field is a layout (QFormLayout only
    does it for a widget field): screen readers read the label as the field's name and its access key works."""
    for form in root.findChildren(QFormLayout):
        for row in range(form.rowCount()):
            label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            field = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            lab = label.widget() if label is not None else None
            if not isinstance(lab, QLabel) or lab.buddy() is not None or field is None:
                continue
            target = field.widget() if field.widget() is not None else _first_field(field.layout())
            if target is not None:
                lab.setBuddy(target)


def _first_field(layout: Optional[QLayout]) -> Optional[QWidget]:
    if layout is None:
        return None
    for i in range(layout.count()):
        item = layout.itemAt(i)
        w = item.widget()
        if w is not None and isinstance(w, INTERACTIVE) and w.focusPolicy() != Qt.FocusPolicy.NoFocus:
            return w
        found = _first_field(item.layout())
        if found is not None:
            return found
    return None


# ----------------------------------------------------------------- live status

def live(label: QLabel, description: Optional[str] = None) -> QLabel:
    """A status label whose new text is announced (common.pill, set_status)."""
    label.setProperty("a11y_live", True)
    if description is not None:
        label.setAccessibleDescription(description)
    return label


def announce(widget: QWidget, text: str, assertive: bool = False) -> None:
    """Have the screen reader read text now (UIA Notification event), without moving the focus."""
    text = (text or "").strip()
    if not text:
        return
    ANNOUNCED.append(("assertive" if assertive else "polite", text))
    del ANNOUNCED[:-50]
    if not QAccessible.isActive():  # no assistive technology listens: nothing to raise
        return
    ev = QAccessibleAnnouncementEvent(widget, text)
    ev.setPoliteness(QAccessible.AnnouncementPoliteness.Assertive if assertive
                     else QAccessible.AnnouncementPoliteness.Polite)
    QAccessible.updateAccessibility(ev)


def set_status(label: QLabel, text: str, assertive: bool = False) -> None:
    """Set a plain status text and announce it when it changed (the label must be live())."""
    changed = label.text() != text
    label.setText(text)
    if changed and label.property("a11y_live"):
        announce(label, text, assertive)


# ----------------------------------------------------------------- access keys

def mnemonic_of(text: str) -> Optional[str]:
    """The access key letter of a text ("&Save" -> "s"), None without one."""
    m = re.search(r"&([^&\s])", (text or "").replace("&&", ""))
    return m.group(1).lower() if m else None


def with_key(text: str, taken: set) -> str:
    """text with an access key ("&") on a letter not in taken (lower case ASCII letters): the first
    letter of a word first, then any other letter; text unchanged when every letter is taken. The letter is added
    to taken."""
    plain = strip_amp(text)
    words = [m.start() for m in re.finditer(r"\b\w", plain)]
    order = words + [i for i in range(len(plain)) if i not in words]
    for i in order:
        ch = plain[i].lower()
        if ch.isascii() and ch.isalpha() and ch not in taken:
            taken.add(ch)
            return plain[:i] + "&" + plain[i:]
    return plain


def give_keys(widgets: Iterable[QWidget], taken: set) -> None:
    """Access keys for buttons (and labels with a buddy) in that order of importance, avoiding taken."""
    for w in widgets:
        if w is None:
            continue
        if isinstance(w, (QAbstractButton, QLabel)):
            w.setText(with_key(w.text(), taken))


def retext(button: QAbstractButton, text: str) -> None:
    """A new text for a button that has an access key: the same letter when the new text has it (the letter is
    the button's for good, so it stays unique), else no access key."""
    letter = button.property("a11y_key") or mnemonic_of(button.text())
    if not letter:
        button.setText(text)
        return
    button.setProperty("a11y_key", letter)
    button.setText(with_key(text, set("abcdefghijklmnopqrstuvwxyz") - {letter}))


def page_keys(root: QWidget) -> list[tuple[str, QWidget]]:
    """(letter, widget) of every access key of root: buttons, check boxes, radios, labels with a buddy."""
    out = []
    for w in [root] + root.findChildren(QWidget):
        if isinstance(w, QAbstractButton) or (isinstance(w, QLabel) and w.buddy() is not None):
            if isinstance(w, QLabel) and w.textFormat() == Qt.TextFormat.RichText:
                continue
            k = mnemonic_of(w.text())
            if k:
                out.append((k, w))
    return out


def shortcut_text(key: str) -> str:
    """'Alt+D' in the platform's words."""
    return QKeySequence(f"Alt+{key.upper()}").toString(QKeySequence.SequenceFormat.NativeText)


class MnemonicDelegate(QStyledItemDelegate):
    """A list item drawn by the style, its access key letter (MNEMONIC_ROLE) underlined as Windows underlines
    the letter of a button: when the style says so (always, or once Alt is pressed)."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        super().paint(painter, option, index)
        marked = index.data(MNEMONIC_ROLE)
        widget = option.widget
        style = widget.style() if widget is not None else QApplication.style()
        if not marked or not style.styleHint(QStyle.StyleHint.SH_UnderlineShortcut, option, widget):
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        plain, pos = strip_amp(marked), marked.replace("&&", "__").find("&")
        if pos < 0 or plain != opt.text:
            return
        text_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        margin = style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, None, widget) + 1
        fm = opt.fontMetrics
        width = fm.horizontalAdvance(plain)
        rtl = opt.direction == Qt.LayoutDirection.RightToLeft
        left = text_rect.right() - margin - width + 1 if rtl else text_rect.left() + margin
        x = left + fm.horizontalAdvance(plain[:pos])
        y = text_rect.center().y() - fm.height() // 2 + fm.ascent() + max(1, fm.underlinePos())
        selected = bool(opt.state & QStyle.StateFlag.State_Selected) and style.name() != "windows11"
        pen = QPen(opt.palette.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text))
        pen.setWidth(max(1, fm.lineWidth()))
        painter.save()
        painter.setPen(pen)
        painter.drawLine(x, y, x + fm.horizontalAdvance(plain[pos]) - 1, y)
        painter.restore()


# ----------------------------------------------------------------- focus ring

class FocusRing(QWidget):
    """A ring drawn around the widget that has the keyboard focus, on top of its window: every control shows its
    focus the same way, in focus_color() (3:1 against the backgrounds), whatever the style draws itself.
    Transparent for the mouse. One per window; FocusRing.install() follows the application's focus."""

    _tracker: Optional["_Tracker"] = None

    def __init__(self, window: QWidget):
        super().__init__(window)
        self.setObjectName("a11y_focus_ring")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.target: Optional[QWidget] = None
        self.hide()

    @staticmethod
    def install(app: Optional[QApplication] = None) -> None:
        app = app or QApplication.instance()
        if app is None or FocusRing._tracker is not None:
            return
        FocusRing._tracker = _Tracker(app)

    @staticmethod
    def for_window(window: QWidget) -> "FocusRing":
        ring = window.findChild(FocusRing, "a11y_focus_ring", Qt.FindChildOption.FindDirectChildrenOnly)
        return ring if ring is not None else FocusRing(window)

    def follow(self, target: Optional[QWidget]) -> None:
        self.target = target
        self.place()

    def place(self) -> None:
        t = self.target
        if t is None or not t.isVisible() or t.window() is not self.parentWidget() or t.property("a11y_own_focus"):
            self.hide()
            return
        visible = t.visibleRegion().boundingRect()
        if visible.isEmpty():
            self.hide()
            return
        w = FOCUS_WIDTH
        top_left = t.mapTo(self.parentWidget(), visible.topLeft())
        r = QRect(top_left, visible.size()).adjusted(-w - 1, -w - 1, w + 1, w + 1)
        self.setGeometry(r.intersected(self.parentWidget().rect()))
        self.raise_()
        self.show()
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(focus_color(self.palette()), FOCUS_WIDTH)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        half = FOCUS_WIDTH / 2
        r = self.rect().toRectF().adjusted(half, half, -half, -half)
        p.drawRoundedRect(r, FOCUS_WIDTH * 2, FOCUS_WIDTH * 2)


class _Tracker(QObject):
    """Moves the focus ring of the focused widget's window when the focus moves, the widget or one of its parents
    moves or resizes (scrolling), shows or hides."""

    WATCH = (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.LayoutRequest)

    def __init__(self, app: QApplication):
        super().__init__(app)
        self.watched: list[QWidget] = []
        self.ring: Optional[FocusRing] = None
        app.focusChanged.connect(self._focus)

    def _unwatch(self) -> None:
        for w in self.watched:
            try:
                w.removeEventFilter(self)
            except RuntimeError:  # deleted
                pass
        self.watched = []

    def _focus(self, _old, new) -> None:
        self._unwatch()
        if self.ring is not None:
            try:
                self.ring.follow(None)
            except RuntimeError:
                pass
            self.ring = None
        if new is None or new.window() is None or not _ring_wanted(new.window()):
            return
        self.ring = FocusRing.for_window(new.window())
        w = new
        while w is not None:
            w.installEventFilter(self)
            self.watched.append(w)
            if w.isWindow():
                break
            w = w.parentWidget()
        self.ring.follow(new)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in self.WATCH and self.ring is not None:
            try:
                self.ring.place()
            except RuntimeError:
                self.ring = None
        return False


def _ring_wanted(window: QWidget) -> bool:
    """Our windows and dialogs only (menus and tooltips are popups and draw their own selection)."""
    return window.windowType() not in (Qt.WindowType.Popup, Qt.WindowType.ToolTip)
