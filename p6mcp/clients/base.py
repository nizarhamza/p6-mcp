"""Base adapter: one MCP client, its config file and the p6mcp entry in it.

An adapter only declares where the client keeps its config, how it detects the client and the
shape of a server entry. Reading, status, registration and removal are shared, and every write
goes through: parse (refuse a malformed file), timestamped backup, minimal JSONC edit (other
entries and formatting untouched, checked), atomic replace.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

from p6mcp import jsonc
from p6mcp.clients import files

SERVER_KEY = "p6mcp"
# Entries of other P6 MCP installs (v2 single-file server, a second copy of this one)
P6_MARKERS = ("p6_mcp_server", "p6mcp-server", "p6mcp.server")

NOT_INSTALLED, NOT_REGISTERED, REGISTERED, OUTDATED, ERROR = (
    "not_installed", "not_registered", "registered", "outdated", "error")


@dataclass
class Launch:
    """How a client starts the stdio server."""
    command: str
    args: list[str] = field(default_factory=list)


def launch() -> Launch:
    """The installed p6mcp-server.exe next to the running executable, else this Python with -m p6mcp."""
    if getattr(sys, "frozen", False):
        return Launch(os.path.join(os.path.dirname(sys.executable), "p6mcp-server.exe"))
    exe = sys.executable
    head, tail = os.path.split(exe)
    if tail.lower() == "pythonw.exe":  # the Settings app may run windowless; stdio needs the console one
        exe = os.path.join(head, "python.exe")
    return Launch(exe, ["-m", "p6mcp"])


@dataclass
class Status:
    client: str
    name: str
    state: str
    path: Optional[str] = None
    entry: Any = None                                    # our current entry, if any
    others: list[str] = field(default_factory=list)      # other P6 MCP entries (legacy installs)
    running: bool = False
    message: Optional[str] = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _norm(v: Any) -> Any:
    """Entries compared with paths case- and separator-insensitively."""
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, str) and ("\\" in v or "/" in v) and len(v) > 2 and v[1] == ":":
        return os.path.normcase(os.path.normpath(v))
    return v


def _running_names() -> set[str]:
    if sys.platform != "win32":
        return set()
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line.startswith('"')}


class Adapter:
    id = ""
    name = ""
    container: tuple[str, ...] = ("mcpServers",)
    process_names: tuple[str, ...] = ()
    docs = ""  # where the path and format were checked

    # -- to define per client
    def config_path(self) -> str:
        raise NotImplementedError

    def installed(self) -> bool:
        raise NotImplementedError

    def stdio_entry(self, ln: Launch) -> dict:
        return {"command": ln.command, "args": list(ln.args)}

    # -- shared
    def expected(self, ln: Optional[Launch] = None) -> dict:
        return self.stdio_entry(ln or launch())

    def _load(self) -> tuple[Optional[str], bool, Any]:
        path = self.config_path()
        text, bom = files.read(path)
        if text is None or not text.strip():
            return text, bom, {}
        data = jsonc.loads(text)
        if not isinstance(data, dict):
            raise jsonc.JsoncError("the file does not hold a JSON object")
        return text, bom, data

    def servers(self, data: Any) -> dict:
        node = data
        for k in self.container:
            node = node.get(k) if isinstance(node, dict) else None
        return node if isinstance(node, dict) else {}

    def status(self, ln: Optional[Launch] = None, running: Optional[set] = None) -> Status:
        st = Status(self.id, self.name, NOT_INSTALLED, self.config_path())
        if running is not None:
            st.running = any(p.lower() in running for p in self.process_names)
        if not self.installed():
            return st
        try:
            _text, _bom, data = self._load()
        except (files.ClientFileError, jsonc.JsoncError) as e:
            st.state, st.message = ERROR, str(e)
            return st
        servers = self.servers(data)
        st.others = sorted(k for k, v in servers.items() if k != SERVER_KEY and _is_p6(v))
        if SERVER_KEY not in servers:
            st.state = NOT_REGISTERED
            return st
        st.entry = servers[SERVER_KEY]
        st.state = REGISTERED if _norm(st.entry) == _norm(self.expected(ln)) else OUTDATED
        return st

    def _edit(self, change) -> dict:
        path = self.config_path()
        text, bom, _data = self._load()  # refuses a malformed file before anything is touched
        new = change(text or "")
        if new == (text or ""):
            return {"changed": False, "path": path, "backup": None}
        saved = files.backup(self.id, path)
        files.write_atomic(path, new, bom)
        return {"changed": True, "path": path, "backup": saved}

    def register(self, ln: Optional[Launch] = None) -> dict:
        """Add or repair the p6mcp entry."""
        entry = self.expected(ln)
        return self._edit(lambda t: jsonc.set_member(t, list(self.container), SERVER_KEY, entry))

    def unregister(self, key: str = SERVER_KEY) -> dict:
        """Remove the p6mcp entry (or, with key, another P6 MCP entry such as a v2 install)."""
        if key != SERVER_KEY:
            _t, _b, data = self._load()
            if key not in self.servers(data) or not _is_p6(self.servers(data)[key]):
                raise ValueError(f"{key} is not a P6 MCP entry of {self.name}: left unchanged")
        return self._edit(lambda t: jsonc.delete_member(t, list(self.container), key) if t.strip() else t)


def _is_p6(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    words = [str(entry.get("command", ""))] + [str(a) for a in entry.get("args", []) or []]
    words.append(str(entry.get("url", "")))
    text = " ".join(words).lower().replace("\\", "/")
    return any(m in text for m in P6_MARKERS) or text.endswith("-m p6mcp")
