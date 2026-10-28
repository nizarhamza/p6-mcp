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
# P6 skills in the client's own skills folder (p6mcp.skills)
SKILLS_UNSUPPORTED, SKILLS_NONE, SKILLS_INSTALLED, SKILLS_OUTDATED = "unsupported", "none", "installed", "outdated"


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
    skills: str = SKILLS_UNSUPPORTED  # P6 skill folders: unsupported, none, installed, outdated

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
    prompts_native = False    # the client lists the server's MCP prompts by itself

    # -- to define per client
    def skills_dir(self) -> Optional[str]:
        """Folder the client reads the user's skills from (None: none; MCP prompts only)."""
        return None

    def legacy_commands_dir(self) -> Optional[str]:
        """Folder where P6 MCP 1.4 to 1.7 wrote the P6 command files (replaced by the skills)."""
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
            st.skills = self.skills_state()
        except (OSError, files.ClientFileError):
            st.skills = SKILLS_OUTDATED
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


    # -- P6 skills (p6mcp.skills): a folder p6-<name> per skill in the client's skills folder
    def _skill_folders(self, profile: str, allow_writes: bool) -> dict[str, dict[str, str]]:
        """folder name -> {relative path: content} of the skills offered under these settings (the skills of
        the saved settings, or the bundled core set before it is installed: skills.all_skills)."""
        from p6mcp import skills
        return {skills.folder_name(s): skills.skill_files(s)
                for s in skills.offered(profile, allow_writes, skills.saved_config())}

    def _ours(self) -> dict[str, dict[str, str]]:
        """Skill folders P6 MCP wrote (their SKILL.md carries the marker): name -> {relative path: content}
        of the files it wrote there."""
        from p6mcp import skills
        root = self.skills_dir()
        out: dict[str, dict[str, str]] = {}
        if not root or not os.path.isdir(root):
            return out
        for name in os.listdir(root):
            md = os.path.join(root, name, "SKILL.md")
            if not (name.startswith(skills.PREFIX) and os.path.isfile(md)):
                continue
            try:
                listed = skills.written_files(files.read(md)[0] or "")
            except files.ClientFileError:  # not UTF-8: not a file P6 MCP wrote
                continue
            content = {}
            for rel in listed:
                path = os.path.join(root, name, *rel.split("/"))
                text = files.read(path)[0] if os.path.isfile(path) else None
                if text is not None:
                    content[rel] = text
            if listed:
                out[name] = content
        return out

    def skills_state(self, profile: Optional[str] = None, allow_writes: Optional[bool] = None) -> str:
        """unsupported (no skills folder), none, installed (every offered skill, current files) or outdated
        (some missing, changed or no longer offered). Settings default to the saved ones."""
        if not self.skills_dir():
            return SKILLS_UNSUPPORTED
        ours = self._ours()
        if not ours:
            return SKILLS_NONE
        if profile is None or allow_writes is None:
            from p6mcp import skills
            data = skills.saved_config()  # unreadable settings: the defaults, as the server
            profile, allow_writes = data.get("tool_profile", "basic"), bool(data.get("allow_writes"))
        return SKILLS_INSTALLED if ours == self._skill_folders(profile, allow_writes) else SKILLS_OUTDATED

    @staticmethod
    def _remove_folder(root: str, name: str, content: dict[str, str]) -> None:
        """Delete the files P6 MCP wrote in a skill folder, then the folders left empty (a file of the user's
        keeps its folder)."""
        folder = os.path.join(root, name)
        for rel in content:
            path = os.path.join(folder, *rel.split("/"))
            if os.path.isfile(path):
                os.remove(path)
        for dirpath, _dirs, _files in sorted(os.walk(folder), key=lambda w: -len(w[0])):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    def install_skills(self, profile: str, allow_writes: bool) -> dict:
        """Write the skills offered under these settings into the client's skills folder, remove the ones
        P6 MCP wrote earlier that are no longer offered and the P6 command files of older versions. A folder
        without the marker (the user's own skill) is never touched. Returns {changed, folder, written, removed,
        skipped, legacy_removed}."""
        root = self.skills_dir()
        if not root:
            raise ValueError(f"{self.name} has no skills folder: it lists the server's skills (MCP prompts) itself"
                             if self.prompts_native else f"{self.name} has no skills folder")
        want = self._skill_folders(profile, allow_writes)
        ours = self._ours()
        written, removed, skipped = [], [], []
        for name, content in want.items():
            if ours.get(name) == content:
                continue
            folder = os.path.join(root, name)
            if name not in ours and os.path.exists(folder):
                skipped.append(name)  # a skill of the user's with the same name
                continue
            for rel, text in content.items():
                files.write_atomic(os.path.join(folder, *rel.split("/")), text)
            self._remove_folder(root, name, {r: t for r, t in ours.get(name, {}).items() if r not in content})
            written.append(name)
        for name, content in ours.items():
            if name not in want:
                self._remove_folder(root, name, content)
                removed.append(name)
        legacy = self.remove_legacy_commands()["removed"]
        return {"changed": bool(written or removed or legacy), "folder": root, "written": written,
                "removed": removed, "skipped": skipped, "legacy_removed": legacy}

    def remove_skills(self) -> dict:
        """Delete the skill folders P6 MCP wrote (marker) and the command files of older versions; the user's
        own skills are kept."""
        root = self.skills_dir()
        removed = []
        for name, content in self._ours().items():
            self._remove_folder(root, name, content)
            removed.append(name)
        legacy = self.remove_legacy_commands()["removed"]
        return {"changed": bool(removed or legacy), "folder": root, "removed": removed, "legacy_removed": legacy}

    def legacy_commands(self) -> list[str]:
        """P6 command files that P6 MCP 1.4 to 1.7 wrote in the client's command folder (old marker)."""
        from p6mcp import skills
        folder = self.legacy_commands_dir()
        if not folder or not os.path.isdir(folder):
            return []
        out = []
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if name.startswith(skills.PREFIX) and name.endswith(".md") and os.path.isfile(path):
                text, _bom = files.read(path)
                if text is not None and skills.COMMAND_MARKER in text:
                    out.append(name)
        return out

    def remove_legacy_commands(self) -> dict:
        folder = self.legacy_commands_dir()
        removed = []
        for name in self.legacy_commands():
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
