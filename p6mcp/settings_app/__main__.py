"""Entry point of p6mcp-settings: the Settings window and the notification area (tray) app.

    p6mcp-settings              open the Settings window (and the tray icon)
    p6mcp-settings --minimized  tray icon only (Windows startup); the window opens from the icon

A second start of the app only asks the running one to show its window.
"""

from __future__ import annotations

import sys


def main(argv=None) -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    args = list(sys.argv if argv is None else argv)
    minimized = "--minimized" in args
    # Exact fractional scaling (110 %, 125 %...) instead of rounding to whole factors; Qt 6 is
    # per-monitor DPI aware, so moving the window between monitors rescales it.
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication([a for a in args if a != "--minimized"])
    app.setApplicationName("P6 MCP Settings")
    app.setOrganizationName("P6MCP")

    from p6mcp import config, i18n, runtime
    from p6mcp.settings_app.tray import TrayApp, signal_running_instance
    if signal_running_instance():
        return 0
    runtime.setup_logging("tray")
    i18n.set_language((config.read_file()[0] or {}).get("language", "auto") if _readable() else "auto")
    tray = TrayApp(app)
    if not minimized or tray.tray is None:
        tray.show_window()
    return app.exec()


def _readable() -> bool:
    from p6mcp import config
    try:
        config.read_file()
        return True
    except config.ConfigError:
        return False


if __name__ == "__main__":
    sys.exit(main())
