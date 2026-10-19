"""The supported MCP clients (Windows). Paths and formats checked on 2026-10-19 against:

- Claude Desktop: modelcontextprotocol.io "Connect to local MCP servers": %APPDATA%\\Claude\\
  claude_desktop_config.json, {"mcpServers": {name: {command, args, env}}}. A Microsoft Store install may
  keep a virtualised copy under %LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude: used when
  it exists.
- Cursor: cursor.com/docs/mcp: ~/.cursor/mcp.json, {"mcpServers": {name: {command, args, env} or {url, headers}}}.
- VS Code: code.visualstudio.com/docs/copilot/reference/mcp-configuration: user profile mcp.json
  (default profile: %APPDATA%\\Code\\User\\mcp.json), {"servers": {name: {"type": "stdio", command, args}}}.
- LM Studio: lmstudio.ai/blog/mcp (Cursor notation) and its bug tracker: %USERPROFILE%\\.lmstudio\\mcp.json.
- Cline: docs.cline.bot/mcp/configuring-mcp-servers: ~/.cline/data/settings/cline_mcp_settings.json
  (CLINE_MCP_SETTINGS_PATH overrides), {"mcpServers": {name: {command, args, disabled, autoApprove}}}; the
  VS Code extension's older globalStorage file is used only when the shared folder does not exist.
"""

from __future__ import annotations

import glob
import os

from p6mcp.clients.base import Adapter, Launch


def _env(name: str) -> str:
    return os.environ.get(name) or ""


def _home() -> str:
    return _env("USERPROFILE") or os.path.expanduser("~")


class ClaudeDesktop(Adapter):
    id, name = "claude_desktop", "Claude Desktop"
    process_names = ("claude.exe",)
    docs = "https://modelcontextprotocol.io/docs/develop/connect-local-servers"

    def _packages(self) -> list[str]:
        return glob.glob(os.path.join(_env("LOCALAPPDATA"), "Packages", "Claude_*"))

    def config_path(self) -> str:
        for pkg in self._packages():
            p = os.path.join(pkg, "LocalCache", "Roaming", "Claude", "claude_desktop_config.json")
            if os.path.isfile(p):
                return p
        return os.path.join(_env("APPDATA"), "Claude", "claude_desktop_config.json")

    def installed(self) -> bool:
        return (os.path.isdir(os.path.join(_env("APPDATA"), "Claude")) or bool(self._packages())
                or os.path.isdir(os.path.join(_env("LOCALAPPDATA"), "AnthropicClaude")))


class Cursor(Adapter):
    id, name = "cursor", "Cursor"
    process_names = ("cursor.exe",)
    docs = "https://cursor.com/docs/mcp"

    def config_path(self) -> str:
        return os.path.join(_home(), ".cursor", "mcp.json")

    def installed(self) -> bool:
        return (os.path.isdir(os.path.join(_home(), ".cursor"))
                or os.path.isfile(os.path.join(_env("LOCALAPPDATA"), "Programs", "cursor", "Cursor.exe")))


class VSCode(Adapter):
    id, name = "vscode", "VS Code"
    container = ("servers",)
    process_names = ("code.exe",)
    folder = "Code"
    exe_dirs = ("Microsoft VS Code",)
    docs = "https://code.visualstudio.com/docs/copilot/reference/mcp-configuration"

    def config_path(self) -> str:
        return os.path.join(_env("APPDATA"), self.folder, "User", "mcp.json")

    def installed(self) -> bool:
        if os.path.isdir(os.path.join(_env("APPDATA"), self.folder, "User")):
            return True
        roots = [os.path.join(_env("LOCALAPPDATA"), "Programs"), _env("ProgramFiles")]
        return any(os.path.isdir(os.path.join(r, d)) for r in roots if r for d in self.exe_dirs)

    def stdio_entry(self, ln: Launch) -> dict:
        return {"type": "stdio", "command": ln.command, "args": list(ln.args)}


class VSCodeInsiders(VSCode):
    id, name = "vscode_insiders", "VS Code Insiders"
    process_names = ("code - insiders.exe",)
    folder = "Code - Insiders"
    exe_dirs = ("Microsoft VS Code Insiders",)


class LMStudio(Adapter):
    id, name = "lmstudio", "LM Studio"
    process_names = ("lm studio.exe",)
    docs = "https://lmstudio.ai/blog/mcp"

    def config_path(self) -> str:
        return os.path.join(_home(), ".lmstudio", "mcp.json")

    def installed(self) -> bool:
        return (os.path.isdir(os.path.join(_home(), ".lmstudio"))
                or os.path.isdir(os.path.join(_env("LOCALAPPDATA"), "Programs", "LM Studio")))


class Cline(Adapter):
    id, name = "cline", "Cline"
    process_names = ("code.exe",)
    docs = "https://docs.cline.bot/mcp/configuring-mcp-servers"

    def _legacy(self) -> str:
        return os.path.join(_env("APPDATA"), "Code", "User", "globalStorage", "saoudrizwan.claude-dev", "settings",
                            "cline_mcp_settings.json")

    def config_path(self) -> str:
        if _env("CLINE_MCP_SETTINGS_PATH"):
            return _env("CLINE_MCP_SETTINGS_PATH")
        shared = os.path.join(_home(), ".cline", "data", "settings", "cline_mcp_settings.json")
        if os.path.isdir(os.path.join(_home(), ".cline")):
            return shared
        # only the VS Code extension: its own file (Cline 4 migrates it to the shared one on first launch)
        if os.path.isdir(os.path.dirname(os.path.dirname(self._legacy()))) or self._extension():
            return self._legacy()
        return shared

    def _extension(self) -> bool:
        return bool(glob.glob(os.path.join(_home(), ".vscode", "extensions", "saoudrizwan.claude-dev-*")))

    def installed(self) -> bool:
        return (bool(_env("CLINE_MCP_SETTINGS_PATH")) or os.path.isdir(os.path.join(_home(), ".cline"))
                or self._extension() or os.path.isdir(os.path.dirname(os.path.dirname(self._legacy()))))

    def stdio_entry(self, ln: Launch) -> dict:
        return {"command": ln.command, "args": list(ln.args), "disabled": False, "autoApprove": []}


ADAPTERS: tuple[Adapter, ...] = (ClaudeDesktop(), Cursor(), VSCode(), VSCodeInsiders(), LMStudio(), Cline())
BY_ID = {a.id: a for a in ADAPTERS}
