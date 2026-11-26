"""The tray app's side of remote access: the remote access server and the tunnel connector as supervised children,
the PC kept awake while they run, and Stop now.

Both run only while remote access is on (remote_enabled), the privacy notice was accepted and the Cloudflare setup
gave a public host. The tunnel connector is the bundled cloudflared.exe, started with its token in the
environment (TUNNEL_TOKEN), never on its command line.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from p6mcp import config, credentials
from p6mcp.clients.base import launch
from p6mcp.supervisor import DISABLED, ERROR, RUNNING, State, Supervisor

TUNNEL_TOKEN_KEY = "remote:tunnel_token"  # Credential Manager
CLOUDFLARED_ENV = "P6MCP_CLOUDFLARED"     # development: path of a cloudflared.exe to use
log = logging.getLogger("p6mcp.remote.supervise")


def cloudflared_path() -> Optional[str]:
    """The bundled cloudflared.exe (installed beside the app), else P6MCP_CLOUDFLARED, else one on PATH."""
    if getattr(sys, "frozen", False):
        p = os.path.join(os.path.dirname(sys.executable), "cloudflared", "cloudflared.exe")
        if os.path.isfile(p):
            return p
    p = os.environ.get(CLOUDFLARED_ENV)
    if p and os.path.isfile(p):
        return p
    return shutil.which("cloudflared")


def remote_wanted(st: config.Settings) -> bool:
    return bool(st.remote_enabled and st.raw.get("remote_privacy_ack") and st.remote_public_host)


def server_command(port: int) -> list[str]:
    ln = launch()
    return [ln.command, *ln.args, "--remote", "--port", str(port)]


def tunnel_command(_key) -> list[str]:
    exe = cloudflared_path()
    if not exe:
        raise OSError("cloudflared.exe was not found: reinstall MCP for Primavera P6")
    return [exe, "tunnel", "--no-autoupdate", "--loglevel", "info", "run"]


def _tunnel_token() -> Optional[str]:
    try:
        return credentials.get(TUNNEL_TOKEN_KEY)
    except credentials.SecretStoreError:
        return None


@dataclass
class RemoteState:
    server: State
    tunnel: State
    problem: Optional[str] = None  # setup incomplete (shown instead of a state)

    @property
    def state(self) -> str:
        if self.problem:
            return ERROR
        if self.server.state == DISABLED:
            return DISABLED
        if ERROR in (self.server.state, self.tunnel.state):
            return ERROR
        return RUNNING if self.server.state == RUNNING and self.tunnel.state == RUNNING else "starting"

    @property
    def error(self) -> Optional[str]:
        return self.problem or self.server.error or self.tunnel.error


class RemoteHost:
    """Two supervisors (remote access server, tunnel) and the keep-awake request, ticked by the tray's timer."""

    def __init__(self, settings: Callable[[], config.Settings] = config.load,
                 token: Callable[[], Optional[str]] = _tunnel_token,
                 server_cmd: Callable[[int], list[str]] = server_command,
                 tunnel_cmd: Callable[[object], list[str]] = tunnel_command,
                 awake: Optional[Callable[[bool], None]] = None, **kw):
        self.settings, self.token = settings, token
        self.awake = awake or _keep_awake
        self.awake_on = False
        self.server = Supervisor(settings=settings, command=server_cmd, want=self._want_server, status="remote",
                                 label="remote access server", out_log="remote-server.out.log", **kw)
        self.tunnel = Supervisor(settings=settings, command=tunnel_cmd, want=self._want_tunnel, status=None,
                                 label="tunnel connector", out_log="tunnel.out.log",
                                 env=lambda st: {"TUNNEL_TOKEN": self._token or ""}, **kw)
        self._token: Optional[str] = None
        self.state = RemoteState(State(DISABLED), State(DISABLED))

    def _want_server(self, st: config.Settings):
        return int(st.remote_port) if remote_wanted(st) else None

    def _want_tunnel(self, st: config.Settings):
        if not remote_wanted(st) or not self._token:
            return None
        # a new token (repair) restarts the connector; the key is logged, so a digest, never the token
        return f"{st.remote_public_host} {hashlib.sha256(self._token.encode()).hexdigest()[:8]}"

    def tick(self) -> RemoteState:
        st = self.settings()
        self._token = self.token() if remote_wanted(st) else None
        problem = None
        if remote_wanted(st) and not self._token:
            problem = ("The tunnel token is missing: run the Cloudflare setup again (Repair) in MCP for Primavera P6 "
                       "Settings (Remote access).")
        self.state = RemoteState(self.server.tick(), self.tunnel.tick(), problem)
        want_awake = bool(st.raw.get("remote_keep_awake", True)) and self.state.server.state != DISABLED
        if want_awake != self.awake_on:
            self.awake(want_awake)
            self.awake_on = want_awake
        return self.state

    def stop(self) -> None:
        """Stop now (tray menu, Quit): tunnel first, so no request reaches a stopping server."""
        self.tunnel.stop()
        self.server.stop()
        if self.awake_on:
            self.awake(False)
            self.awake_on = False

    def restart(self) -> RemoteState:
        self.tunnel.restart()
        self.server.restart()
        return self.tick()


ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


def _keep_awake(on: bool) -> None:
    """Ask Windows not to sleep while remote access runs (the display may still turn off). Called from the tray's
    thread, which keeps the request alive until it is cleared."""
    if sys.platform != "win32":
        return
    import ctypes
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if on else ES_CONTINUOUS
    if not ctypes.windll.kernel32.SetThreadExecutionState(flags):
        log.warning("SetThreadExecutionState failed")
