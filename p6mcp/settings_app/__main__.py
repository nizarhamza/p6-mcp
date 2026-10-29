"""Entry point of p6mcp-settings: the Settings window and the notification area (tray) app.

    p6mcp-settings              open the Settings window (and the tray icon)
    p6mcp-settings --minimized  tray icon only (Windows startup); the window opens from the icon

A second start of the app only asks the running one to show its window. While MCP for Primavera P6 setup runs the app
exits at once (see runtime.setup_running).
"""

from __future__ import annotations

import sys


def main(argv=None) -> int:
    args = list(sys.argv if argv is None else argv)
    minimized = "--minimized" in args
    from p6mcp import runtime
    if runtime.setup_running():  # before Qt is loaded: exit before locking the files setup replaces
        if not minimized:
            runtime.request_window_after_setup()
        return 0

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    # Exact fractional scaling (110 %, 125 %...) instead of rounding to whole factors; Qt 6 is
    # per-monitor DPI aware, so moving the window between monitors rescales it.
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication([a for a in args if a != "--minimized"])
    app.setApplicationName("MCP for Primavera P6 Settings")
    app.setOrganizationName("P6MCP")

    from p6mcp import config, i18n
    from p6mcp.settings_app.tray import TrayApp, app_icon, signal_running_instance
    app.setWindowIcon(app_icon())
    if signal_running_instance():
        return 0
    runtime.setup_logging("tray")
    _install_core_skills()
    _convert_commands()
    i18n.set_language((config.read_file()[0] or {}).get("language", "auto") if _readable() else "auto")
    tray = TrayApp(app)
    asked_during_setup = runtime.take_window_request()
    if not minimized or tray.tray is None or asked_during_setup:
        tray.show_window()
    return app.exec()


def _install_core_skills() -> None:
    """P6 MCP 1.9 has no built-in skills: once, install the core set from the skill shop (else from the copy
    bundled with the app), carry the skills turned off, save the settings and rewrite the clients' P6 skills."""
    import logging
    from p6mcp import core_skills
    log = logging.getLogger("p6mcp.settings")
    try:
        done = core_skills.run_first_start()
        if done is not None:
            log.info("core skills installed: %s", done)
    except Exception:  # never a reason not to start: the bundled copy stays offered and the next start retries
        log.exception("installing the core skills failed")


def _convert_commands() -> None:
    """P6 MCP 1.8 turned the P6 commands into skills: convert the command files an older version wrote in
    the clients' folders (nothing to do once converted)."""
    import logging
    from p6mcp import clients
    log = logging.getLogger("p6mcp.settings")
    try:
        for done in clients.convert_legacy_commands():
            log.info("P6 commands converted to skills: %s", done)
    except Exception:  # never a reason not to start
        log.exception("converting the P6 commands to skills failed")


def _readable() -> bool:
    from p6mcp import config
    try:
        config.read_file()
        return True
    except config.ConfigError:
        return False


if __name__ == "__main__":
    sys.exit(main())
