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
# P6 command files in the client's own command folder (p6mcp.prompts)
CMD_UNSUPPORTED, CMD_NONE, CMD_INSTALLED, CMD_OUTDATED = "unsupported", "none", "installed", "outdated"


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
class HttpTarget:
    """The local HTTP server a client should connect to (transport http)."""
    url: str
    token: str


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
    transport: str = "stdio"          # of the entry this installation expects for this client
    commands: str = CMD_UNSUPPORTED   # P6 command files: unsupported, none, installed, outdated

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
    supports_http = True      # False: the client only starts stdio servers (gets a stdio entry in http mode)
    token_in_file = True      # an HTTP entry holds the access token in the client's file
    command_style: Optional[str] = None  # p6mcp.prompts.STYLES; None: no command folder (MCP prompts only)
    prompts_native = False    # the client lists the server's MCP prompts by itself

    # -- to define per client
    def commands_dir(self) -> Optional[str]:
        """Folder the client reads its user commands from (None: none)."""
        return None

    def config_path(self) -> str:
        raise NotImplementedError

    def installed(self) -> bool:
        raise NotImplementedError

    def stdio_entry(self, ln: Launch) -> dict:
        return {"command": ln.command, "args": list(ln.args)}

    def http_entry(self, http: HttpTarget) -> dict:
        return {"url": http.url, "headers": {"Authorization": f"Bearer {http.token}"}}

    def extra_register(self, text: str, entry: dict) -> str:
        """Further edits a registration needs besides the entry (VS Code: its token prompt)."""
        return text

    def extra_unregister(self, text: str) -> str:
        return text

    # -- shared
    def uses_http(self, http: Optional[HttpTarget]) -> bool:
        return http is not None and self.supports_http

    def expected(self, ln: Optional[Launch] = None, http: Optional[HttpTarget] = None) -> dict:
        return self.http_entry(http) if self.uses_http(http) else self.stdio_entry(ln or launch())

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

    def status(self, ln: Optional[Launch] = None, running: Optional[set] = None,
               http: Optional[HttpTarget] = None) -> Status:
        st = Status(self.id, self.name, NOT_INSTALLED, self.config_path(),
                    transport="http" if self.uses_http(http) else "stdio")
        if running is not None:
            st.running = any(p.lower() in running for p in self.process_names)
        if not self.installed():
            return st
        try:
            _text, _bom, data = self._load()
        except (files.ClientFileError, jsonc.JsoncError) as e:
            st.state, st.message = ERROR, str(e)
            return st
        try:
            st.commands = self.commands_state()
        except (OSError, files.ClientFileError):
            st.commands = CMD_OUTDATED
        servers = self.servers(data)
        st.others = sorted(k for k, v in servers.items() if k != SERVER_KEY and _is_p6(v))
        if SERVER_KEY not in servers:
            st.state = NOT_REGISTERED
            return st
        st.entry = servers[SERVER_KEY]
        st.state = REGISTERED if _norm(st.entry) == _norm(self.expected(ln, http)) else OUTDATED
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

    def register(self, ln: Optional[Launch] = None, http: Optional[HttpTarget] = None) -> dict:
        """Add or repair the p6mcp entry (HTTP when http is given and the client supports it)."""
        entry = self.expected(ln, http)
        return self._edit(lambda t: self.extra_register(
            jsonc.set_member(t, list(self.container), SERVER_KEY, entry), entry))

    def unregister(self, key: str = SERVER_KEY) -> dict:
        """Remove the p6mcp entry (or, with key, another P6 MCP entry such as a v2 install)."""
        if key != SERVER_KEY:
            _t, _b, data = self._load()
            if key not in self.servers(data) or not _is_p6(self.servers(data)[key]):
                raise ValueError(f"{key} is not a P6 MCP entry of {self.name}: left unchanged")
        if key != SERVER_KEY:
            return self._edit(lambda t: jsonc.delete_member(t, list(self.container), key) if t.strip() else t)
        return self._edit(lambda t: self.extra_unregister(jsonc.delete_member(t, list(self.container), key))
                          if t.strip() else t)


    # -- P6 command files (p6mcp.prompts) in the client's command folder
    def _command_files(self, profile: str, allow_writes: bool) -> dict[str, str]:
        """file name -> content of the commands offered under these settings."""
        from p6mcp import prompts
        return {prompts.file_name(c, self.command_style): prompts.command_file(c, self.command_style)
                for c in prompts.offered(profile, allow_writes)}

    def _ours(self) -> dict[str, str]:
        """Command files P6 MCP wrote in the folder (they carry the marker): name -> content."""
        from p6mcp import prompts
        folder = self.commands_dir()
        out: dict[str, str] = {}
        if not folder or not os.path.isdir(folder):
            return out
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if not (name.startswith(prompts.PREFIX) and name.endswith(".md") and os.path.isfile(path)):
                continue
            text, _bom = files.read(path)
            if text is not None and prompts.MARKER in text:
                out[name] = text
        return out

    def commands_state(self, profile: Optional[str] = None, allow_writes: Optional[bool] = None) -> str:
        """unsupported (no command folder), none, installed (every offered command, current text) or
        outdated (some missing, changed or no longer offered). Settings default to the saved ones."""
        if not self.command_style or not self.commands_dir():
            return CMD_UNSUPPORTED
        ours = self._ours()
        if not ours:
            return CMD_NONE
        if profile is None or allow_writes is None:
            from p6mcp import config
            try:
                data = config.read_file()[0] or config.DEFAULTS
            except config.ConfigError:  # unreadable settings: the defaults, as the server
                data = config.DEFAULTS
            profile, allow_writes = data.get("tool_profile", "basic"), bool(data.get("allow_writes"))
        return CMD_INSTALLED if ours == self._command_files(profile, allow_writes) else CMD_OUTDATED

    def install_commands(self, profile: str, allow_writes: bool) -> dict:
        """Write the commands offered under these settings into the client's command folder, and remove
        the ones P6 MCP wrote earlier that are no longer offered. Files without the marker (the user's own)
        are never touched. Returns {changed, folder, written, removed, skipped}."""
        folder = self.commands_dir()
        if not self.command_style or not folder:
            raise ValueError(f"{self.name} has no command folder: it lists the server's commands itself"
                             if self.prompts_native else f"{self.name} has no command folder")
        want = self._command_files(profile, allow_writes)
        ours = self._ours()
        written, removed, skipped = [], [], []
        for name, text in want.items():
            path = os.path.join(folder, name)
            if ours.get(name) == text:
                continue
            if name not in ours and os.path.exists(path):
                skipped.append(name)  # a file of the user's with the same name
                continue
            files.write_atomic(path, text)
            written.append(name)
        for name in ours:
            if name not in want:
                os.remove(os.path.join(folder, name))
                removed.append(name)
        return {"changed": bool(written or removed), "folder": folder, "written": written, "removed": removed,
                "skipped": skipped}

    def remove_commands(self) -> dict:
        """Delete the command files P6 MCP wrote (marker); the user's own files are kept."""
        folder = self.commands_dir()
        removed = []
        for name in self._ours():
            os.remove(os.path.join(folder, name))
            removed.append(name)
        return {"changed": bool(removed), "folder": folder, "removed": removed}


def _is_p6(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    words = [str(entry.get("command", ""))] + [str(a) for a in entry.get("args", []) or []]
    words.append(str(entry.get("url", "")))
    text = " ".join(words).lower().replace("\\", "/")
    return any(m in text for m in P6_MARKERS) or text.endswith("-m p6mcp")
