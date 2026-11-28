"""Small widgets and helpers shared by the Settings pages."""

from __future__ import annotations

import os
import sys
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRect, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QPalette, QPixmap
from PySide6.QtWidgets import (QCheckBox, QFrame, QGroupBox, QLabel, QMessageBox, QRadioButton, QSizePolicy, QSpinBox,
                               QStyle, QStyleOptionButton, QStyleOptionSpinBox, QStylePainter, QVBoxLayout, QWidget)

from p6mcp.i18n import tr
from p6mcp.settings_app import a11y

# Palette of the status pills and banners: readable in light and dark Windows themes (every pair at least 4.5:1,
# tests/test_accessibility.py); not used in Windows high contrast mode (the system colours only)
COLORS = {"ok": ("#1e6b3a", "#e3f4e8"), "warn": ("#7a4b00", "#fff1d6"), "error": ("#8a1c1c", "#fde4e4"),
          "info": ("#1f4e79", "#e3eef9"), "idle": ("#444444", "#ececec")}


def brand_file(rel: str) -> Optional[str]:
    """A file of assets/brand (repository root in a checkout, the bundle folder when frozen); None if absent."""
    here = os.path.abspath(__file__)
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.dirname(here)))
    path = os.path.join(base, "assets", "brand", *rel.split("/"))
    return path if os.path.isfile(path) else None


def brand_pixmap(name: str, height: int, dpr: float = 1.0) -> Optional[QPixmap]:
    """A png/<name>.png brand image scaled to height logical pixels (the @2x file on high DPI screens)."""
    path = (dpr > 1 and brand_file(f"png/{name}@2x.png")) or brand_file(f"png/{name}.png")
    pm = QPixmap(path) if path else QPixmap()
    if pm.isNull():
        return None
    pm = pm.scaledToHeight(round(height * dpr), Qt.TransformationMode.SmoothTransformation)
    pm.setDevicePixelRatio(dpr)
    return pm


def pill(label: QLabel, level: str, text: str) -> None:
    """A status in a coloured pill (its words say the state, never the colour alone). A live label (a11y.live)
    announces a new text to screen readers while it is on screen; an error is announced at once."""
    changed = label.text() != text
    label.setText(text)
    if a11y.high_contrast():  # the system colours, a frame keeps the pill's shape
        label.setStyleSheet("QLabel { border: 1px solid palette(window-text); border-radius: 4px; padding: 4px 8px; }")
    else:
        fg, bg = COLORS[level]
        label.setStyleSheet(f"QLabel {{ color: {fg}; background: {bg}; border-radius: 4px; padding: 4px 8px; }}")
    if changed and label.property("a11y_live") and label.isVisible():
        a11y.announce(label, text, assertive=level == "error")


def muted(lab: QLabel) -> QLabel:
    """Secondary text: the theme's placeholder colour, made darker (or lighter) just enough for 4.5:1 on the window
    and field backgrounds (the Windows 11 placeholder is about 3.9:1); the text colour in high contrast mode."""
    pal = lab.palette()
    pal.setColor(QPalette.ColorRole.WindowText, a11y.muted_color(pal))
    lab.setPalette(pal)
    return lab


def note(text: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return muted(lab)


def group(title: str) -> tuple[QGroupBox, QVBoxLayout]:
    box = QGroupBox(title)
    lay = QVBoxLayout(box)
    return box, lay


class _Wrapping:
    """A check box or radio button whose text wraps onto more lines when the page is narrow (large text, high
    display scale, a narrow window) instead of widening the page past the window (a horizontal scroll bar). The
    indicator is the style's, aligned with the first line; a click on the text toggles it, as on one line."""

    _INDICATOR = {QStyle.PrimitiveElement.PE_IndicatorRadioButton: (QStyle.SubElement.SE_RadioButtonIndicator,
                                                                    QStyle.SubElement.SE_RadioButtonContents),
                  QStyle.PrimitiveElement.PE_IndicatorCheckBox: (QStyle.SubElement.SE_CheckBoxIndicator,
                                                                 QStyle.SubElement.SE_CheckBoxContents)}
    PRIMITIVE: QStyle.PrimitiveElement
    MIN_CHARS = 24  # the text column never gets narrower than this many average characters

    def _setup(self) -> None:
        sp = self.sizePolicy()
        sp.setHeightForWidth(True)
        sp.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
        self.setSizePolicy(sp)

    def _line(self, width: int) -> QStyleOptionButton:
        """The style option of a one-line button of this width."""
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        opt.rect = QRect(0, 0, width, super().sizeHint().height())
        return opt

    def _flags(self, opt) -> int:
        show = self.style().styleHint(QStyle.StyleHint.SH_UnderlineShortcut, opt, self)
        align = QStyle.visualAlignment(self.layoutDirection(), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        return int(align) | int(Qt.TextFlag.TextWordWrap) | int(
            Qt.TextFlag.TextShowMnemonic if show else Qt.TextFlag.TextHideMnemonic)

    def _text_rect(self, width: int, height: int) -> QRect:
        opt = self._line(width)
        contents = self.style().subElementRect(self._INDICATOR[self.PRIMITIVE][1], opt, self)
        top = contents.top() + max(0, (contents.height() - self.fontMetrics().height()) // 2)
        left, right = max(contents.left(), 0), min(contents.right(), width - 1)  # the style's rect may overhang
        return QRect(left, top, max(1, right - left + 1), max(1, height - top))

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        r = self._text_rect(width, 100000)
        text = self.fontMetrics().boundingRect(r, self._flags(self._line(width)), self.text())
        return max(super().sizeHint().height(), r.top() + text.height() + (super().sizeHint().height()
                                                                           - self.fontMetrics().height()) // 2)

    def sizeHint(self) -> QSize:
        return super().sizeHint()  # one line when there is room

    def minimumSizeHint(self) -> QSize:
        one = super().sizeHint()
        fm = self.fontMetrics()
        width = min(one.width(), one.width() - fm.horizontalAdvance(self.text().replace("&", ""))
                    + fm.averageCharWidth() * self.MIN_CHARS)
        return QSize(width, self.heightForWidth(width))

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:
        p = QStylePainter(self)
        opt = self._line(self.width())
        ind_opt = QStyleOptionButton(opt)
        ind_opt.rect = self.style().subElementRect(self._INDICATOR[self.PRIMITIVE][0], opt, self)
        p.drawPrimitive(self.PRIMITIVE, ind_opt)
        role = self.foregroundRole()
        self.style().drawItemText(p, self._text_rect(self.width(), self.height()), self._flags(opt), self.palette(),
                                  self.isEnabled(), self.text(), role)


class Radio(_Wrapping, QRadioButton):
    PRIMITIVE = QStyle.PrimitiveElement.PE_IndicatorRadioButton

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self._setup()


class Check(_Wrapping, QCheckBox):
    PRIMITIVE = QStyle.PrimitiveElement.PE_IndicatorCheckBox

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self._setup()


def fit_spin(spin: QSpinBox) -> QSpinBox:
    """Wide enough for its longest value: the Windows 11 style sizes the arrows from the font but keeps the field
    narrow, which cuts the number with large Windows text. Call after setRange / setSuffix."""
    fm = spin.fontMetrics()
    longest = max((spin.prefix() + str(v) + spin.suffix() for v in (spin.minimum(), spin.maximum())),
                  key=fm.horizontalAdvance)
    need = max(fm.horizontalAdvance(longest), fm.horizontalAdvance(spin.specialValueText())) + fm.averageCharWidth() * 2
    spin.resize(spin.sizeHint())
    opt = QStyleOptionSpinBox()
    spin.initStyleOption(opt)
    field = spin.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, opt, QStyle.SubControl.SC_SpinBoxEditField,
                                        spin)
    if need > field.width():
        spin.setMinimumWidth(spin.sizeHint().width() + need - field.width())
    return spin


def hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f


def question_box(parent: Optional[QWidget], title: str, text: str, warning: bool = True) -> QMessageBox:
    """The Yes / No box of ask(): No is the default button and Esc answers No."""
    box = QMessageBox(QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Question, title, text,
                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    box.setEscapeButton(QMessageBox.StandardButton.No)
    box.button(QMessageBox.StandardButton.Yes).setText(tr("common.yes"))
    box.button(QMessageBox.StandardButton.No).setText(tr("common.no"))
    return box


def ask(parent: QWidget, title: str, text: str, warning: bool = True) -> bool:
    """Yes / No question, No by default (the tests replace it)."""
    return question_box(parent, title, text, warning).exec() == QMessageBox.StandardButton.Yes


class _Signals(QObject):
    done = Signal(object)


class Job(QRunnable):
    """Run fn() on the thread pool and deliver its result (or the exception) on the GUI thread."""

    def __init__(self, fn: Callable[[], object], on_done: Callable[[object], None]):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()
        self.signals.done.connect(on_done, Qt.ConnectionType.QueuedConnection)

    def run(self) -> None:
        try:
            out = self.fn()
        except Exception as e:  # reported to the page, never lost in the worker thread
            out = e
        self.signals.done.emit(out)


def start(job: Job) -> Job:
    QThreadPool.globalInstance().start(job)
    return job
