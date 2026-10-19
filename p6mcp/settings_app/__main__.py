"""Entry point of p6mcp-settings."""

from __future__ import annotations

import sys


def main(argv=None) -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    # Exact fractional scaling (110 %, 125 %...) instead of rounding to whole factors; Qt 6 is
    # per-monitor DPI aware, so moving the window between monitors rescales it.
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName("P6 MCP Settings")
    app.setOrganizationName("P6MCP")
    from p6mcp.settings_app.window import SettingsWindow
    win = SettingsWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
