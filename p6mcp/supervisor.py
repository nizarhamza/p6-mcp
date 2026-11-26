"""Hosts a child process for the tray app (no Qt here: tick() is driven by a timer).

By default the local HTTP server: on every tick the wanted state is read from the settings: transport http ->
one child process `p6mcp-server --http --port N` must run; stdio -> none. A port change restarts the child; a
child that exits on its own is restarted with a growing delay, and after MAX_FAILS quick failures the
supervisor stops trying and reports the error until the settings change or restart() is called.

The same class hosts the remote access server and its tunnel connector (p6mcp.remote.supervise): `want` gives
the key (a port, or any value whose change means a restart) or None, `status` names the child's status file
(None: running = alive for READY_AFTER seconds), `env` adds environment variables (the tunnel token: never on a
command line, other processes can read those).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from p6mcp import config, runtime
from p6mcp.clients.base import launch

log = logging.getLogger("p6mcp.supervisor")
MAX_FAILS, FAIL_WINDOW = 5, 300.0
BACKOFF = (1, 2, 5, 10, 30)
STOP_WAIT = 6.0
READY_AFTER = 3.0  # a child without status file counts as running once alive this long

RUNNING, STARTING, STOPPED, ERROR, DISABLED = "running", "starting", "stopped", "error", "disabled"


@dataclass
class State:
    state: str = DISABLED   # disabled = transport stdio (nothing to host)
    port: Any = None
    pid: Optional[int] = None
    error: Optional[str] = None
    restarts: int = 0


def server_command(port: int) -> list[str]:
    ln = launch()
    return [ln.command, *ln.args, "--http", "--port", str(port)]


def _want_http(st: config.Settings) -> Optional[int]:
    return int(st.http_port) if st.transport == "http" else None


class Supervisor:
    def __init__(self, settings: Callable[[], config.Settings] = config.load,
                 command: Callable[[Any], list[str]] = server_command, clock: Callable[[], float] = time.monotonic,
                 want: Callable[[config.Settings], Any] = _want_http, status: Optional[str] = "http",
                 label: str = "HTTP server", out_log: str = "http-server.out.log",
                 env: Optional[Callable[[config.Settings], Optional[dict]]] = None):
        self.settings, self.command, self.clock = settings, command, clock
        self.want, self.status_name, self.label, self.out_log, self.env = want, status, label, out_log, env
        self.proc: Optional[subprocess.Popen] = None
        self.port: Optional[int] = None
        self.fails: list[float] = []
        self.next_try = 0.0
        self.given_up: Optional[str] = None
        self.state = State()

    # -- process control
    def _start(self, port: Any, st: config.Settings) -> None:
        os.makedirs(runtime.logs_dir(), exist_ok=True)
        extra = self.env(st) if self.env else None
        env = dict(os.environ, **extra) if extra else None
        out = open(os.path.join(runtime.logs_dir(), self.out_log), "ab")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(self.command(port), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                     creationflags=flags, close_fds=True, env=env)
        out.close()
        self.port = port
        self.started_at = self.clock()
        log.info("%s started (pid %s, port %s)", self.label, self.proc.pid, port)

    def _ours(self, status: dict) -> bool:
        """The status file was written by our child (or by the interpreter a venv launcher started)."""
        return self.proc is not None and self.proc.pid in (status.get("pid"), status.get("ppid"))

    def _stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            if sys.platform == "win32":  # the whole tree: a launcher may have started the real server
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=STOP_WAIT * 2)
            else:
                self.proc.terminate()
            try:
                self.proc.wait(STOP_WAIT)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(STOP_WAIT)
        log.info("%s stopped (pid %s)", self.label, self.proc.pid)
        self.proc = None

    def stop(self) -> None:
        """Stop the child (tray Quit)."""
        self._stop()
        self.state = State(STOPPED, self.port)

    def restart(self) -> State:
        """Stop, forget past failures and start again on the next tick."""
        self._stop()
        self.fails, self.given_up, self.next_try = [], None, 0.0
        return self.tick()

    # -- one step
    def _status(self) -> dict:
        return runtime.read_status(self.status_name) if self.status_name else {}

    def tick(self) -> State:
        st = self.settings()
        port = self.want(st)
        if port is None:
            self._stop()
            self.fails, self.given_up = [], None
            self.state = State(DISABLED)
            return self.state
        if self.proc is not None and self.port != port:  # new port: restart now
            self._stop()
            self.fails, self.given_up, self.next_try = [], None, 0.0
        if self.proc is not None and self.proc.poll() is not None:  # ended on its own
            code = self.proc.returncode
            self.proc = None
            now = self.clock()
            self.fails = [t for t in self.fails if now - t < FAIL_WINDOW] + [now]
            reason = self._status().get("error") or f"exit code {code}"
            log.warning("%s ended: %s", self.label, reason)
            if len(self.fails) >= MAX_FAILS:
                self.given_up = reason
            self.next_try = now + BACKOFF[min(len(self.fails), len(BACKOFF)) - 1]
            self.state = State(ERROR, port, None, reason, len(self.fails))
        if self.given_up:
            self.state = State(ERROR, port, None, f"{self.given_up} (stopped after {MAX_FAILS} attempts)",
                               len(self.fails))
            return self.state
        if self.proc is None:
            if self.clock() < self.next_try:
                return self.state
            try:
                self._start(port, st)
            except OSError as e:
                self.given_up = f"cannot start the {self.label}: {e}"
                self.state = State(ERROR, port, None, self.given_up)
                return self.state
        if self.status_name:
            status = self._status()
            live = self._ours(status) and status.get("state") == RUNNING
        else:
            live = self.proc.poll() is None and self.clock() - self.started_at >= READY_AFTER
        self.state = State(RUNNING if live else STARTING, port, self.proc.pid,
                           None if live else self.state.error, len(self.fails))
        return self.state
