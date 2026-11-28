"""Entry point of p6mcp-settings: the Settings window and the notification area (tray) app.

    p6mcp-settings              open the Settings window (and the tray icon)
    p6mcp-settings --minimized  tray icon only (Windows startup); the window opens from the icon
    --after-setup               started by setup itself once its files are copied: wait for setup to end

A second start of the app only asks the running one to show its window. While MCP for Primavera P6 setup runs the app
exits at once (see runtime.setup_running), except when setup started it (--after-setup): setup's [Run] entries run
while it still holds its mutex, so that start waits for setup to end (an automatic update used to leave no tray app,
hence no remote access, until the next sign-in).
"""

from __future__ import annotations

import sys


def main(argv=None) -> int:
    import multiprocessing
    multiprocessing.freeze_support()  # a spawned worker process of this exe runs there and exits (no-op otherwise)
    args = list(sys.argv if argv is None else argv)
    minimized = "--minimized" in args
    after_setup = "--after-setup" in args
    from p6mcp import runtime
    if runtime.setup_running():  # before Qt is loaded: exit before locking the files setup replaces
        if not (after_setup and runtime.wait_for_setup_end()):
            if not minimized:
                runtime.request_window_after_setup()
            return 0

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    # Exact fractional scaling (110 %, 125 %...) instead of rounding to whole factors; Qt 6 is
    # per-monitor DPI aware, so moving the window between monitors rescales it.
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication([a for a in args if a not in ("--minimized", "--after-setup")])
    app.setApplicationName("MCP for Primavera P6 Settings")
    app.setOrganizationName("P6MCP")

    from p6mcp import config, i18n
    from p6mcp.settings_app.tray import TrayApp, app_icon, signal_running_instance
    app.setWindowIcon(app_icon())
    if signal_running_instance():
        return 0
    runtime.setup_logging("tray")
    _heal_startup()
    _install_core_skills()
    _convert_commands()
    _refresh_skills()
    i18n.set_language((config.read_file()[0] or {}).get("language", "auto") if _readable() else "auto")
    tray = TrayApp(app)
    asked_during_setup = runtime.take_window_request()
    if not minimized or tray.tray is None or asked_during_setup:
        tray.show_window()
    return app.exec()


def _heal_startup() -> None:
    """Put the Windows startup entry back when the settings ask for one and it is missing or stale (removed by
    another program, a moved install): without it the tray app, and remote access with it, waits for a manual start.
    Installed app only: a run from source never registers its Python."""
    import logging
    from p6mcp import config, startup
    if not getattr(sys, "frozen", False):
        return
    log = logging.getLogger("p6mcp.settings")
    try:
        done = startup.heal(config.read_file()[0] or {})
        if done:
            log.info("Windows startup entry restored: %s", done)
    except Exception:  # never a reason not to start
        log.exception("restoring the Windows startup entry failed")


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


def _refresh_skills() -> None:
    """Rewrite the P6 skills of the clients that have some when they no longer match the saved settings."""
    import logging
    from p6mcp import core_skills
    log = logging.getLogger("p6mcp.settings")
    try:
        for done in core_skills.run_refresh():
            log.info("clients' P6 skills refreshed: %s", done)
    except Exception:  # never a reason not to start
        log.exception("refreshing the clients' P6 skills failed")


def _readable() -> bool:
    from p6mcp import config
    try:
        config.read_file()
        return True
    except config.ConfigError:
        return False


if __name__ == "__main__":
    sys.exit(main())
