"""Small widgets and helpers shared by the Settings pages."""

from __future__ import annotations

import os
import sys
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QPalette, QPixmap
from PySide6.QtWidgets import QFrame, QGroupBox, QLabel, QMessageBox, QVBoxLayout, QWidget

from p6mcp.i18n import tr

# Palette of the status pills and banners: readable in light and dark Windows themes
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
    fg, bg = COLORS[level]
    label.setText(text)
    label.setStyleSheet(f"QLabel {{ color: {fg}; background: {bg}; border-radius: 4px; padding: 4px 8px; }}")


def muted(lab: QLabel) -> QLabel:
    """Secondary text: the theme's placeholder colour (readable in light and dark themes)."""
    pal = lab.palette()
    pal.setColor(QPalette.ColorRole.WindowText, pal.color(QPalette.ColorRole.PlaceholderText))
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


def hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f


def ask(parent: QWidget, title: str, text: str, warning: bool = True) -> bool:
    """Yes / No question, No by default (the tests replace it)."""
    box = QMessageBox(QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Question, title, text,
                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    box.button(QMessageBox.StandardButton.Yes).setText(tr("common.yes"))
    box.button(QMessageBox.StandardButton.No).setText(tr("common.no"))
    return box.exec() == QMessageBox.StandardButton.Yes


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
