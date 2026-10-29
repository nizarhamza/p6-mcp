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
- Claude Code: code.claude.com/docs/en/mcp: user scope servers in ~/.claude.json, top-level
  {"mcpServers": {name: {"type": "stdio", command, args} or {"type": "http", url, headers}}}.

P6 skills folders (p6mcp.skills, Agent Skills: a folder per skill with SKILL.md), checked on 2026-10-28:
Claude Code ~/.claude/skills (code.claude.com/docs/en/skills); Cursor reads ~/.claude/skills besides its own
folders (cursor.com/docs/context/skills) and so does VS Code (code.visualstudio.com/docs/agent-customization/
agent-skills; same-name skills deduplicated), so these three share ~/.claude/skills (writing ~/.cursor/skills
too would show Cursor duplicates); Cline ~/.cline/skills (docs.cline.bot/customization/skills; it reads
.claude/skills only inside a workspace). Every client needs name = folder name and a description of at most
1,024 characters. Claude Desktop has no skills folder (zip upload): it lists the server's MCP prompts itself;
LM Studio has neither. Command folders of P6 MCP 1.4 to 1.7 (legacy_commands_dir, cleaned up): VS Code
prompts folder, ~/.cursor/commands, ~/Documents/Cline/Workflows, ~/.claude/commands.
"""

from __future__ import annotations

import glob
import os

from p6mcp import jsonc
from p6mcp.clients.base import Adapter, HttpTarget, Launch

VSCODE_INPUT = "p6mcp-token"


def _env(name: str) -> str:
    return os.environ.get(name) or ""


def _home() -> str:
    return _env("USERPROFILE") or os.path.expanduser("~")


def _claude_skills() -> str:
    """~/.claude/skills: read by Claude Code, Cursor and VS Code."""
    return os.path.join(_home(), ".claude", "skills")


class ClaudeDesktop(Adapter):
    id, name = "claude_desktop", "Claude Desktop"
    process_names = ("claude.exe",)
    docs = "https://modelcontextprotocol.io/docs/develop/connect-local-servers"
    supports_http = False  # its config file only starts local (stdio) servers
    prompts_native = True  # the + menu lists the server's prompts

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

    def skills_dir(self) -> str:  # Cursor also reads ~/.claude/skills: shared with Claude Code and VS Code
        return _claude_skills()

    def legacy_commands_dir(self) -> str:
        return os.path.join(_home(), ".cursor", "commands")

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
    prompts_native = True  # also as /mcp.p6mcp.<prompt> in the chat

    def config_path(self) -> str:
        return os.path.join(_env("APPDATA"), self.folder, "User", "mcp.json")

    def skills_dir(self) -> str:  # VS Code also reads ~/.claude/skills (chat.useAgentSkills)
        return _claude_skills()

    def legacy_commands_dir(self) -> str:
        return os.path.join(_env("APPDATA"), self.folder, "User", "prompts")

    def installed(self) -> bool:
        if os.path.isdir(os.path.join(_env("APPDATA"), self.folder, "User")):
            return True
        roots = [os.path.join(_env("LOCALAPPDATA"), "Programs"), _env("ProgramFiles")]
        return any(os.path.isdir(os.path.join(r, d)) for r in roots if r for d in self.exe_dirs)

    token_in_file = False  # VS Code asks for the token once and keeps it in its secret storage

    def stdio_entry(self, ln: Launch) -> dict:
        return {"type": "stdio", "command": ln.command, "args": list(ln.args)}

    def http_entry(self, http: HttpTarget) -> dict:
        return {"type": "http", "url": http.url, "headers": {"Authorization": "Bearer ${input:" + VSCODE_INPUT + "}"}}

    def extra_register(self, text: str, entry: dict) -> str:
        inputs = (jsonc.loads(text) or {}).get("inputs") or []
        has = any(isinstance(i, dict) and i.get("id") == VSCODE_INPUT for i in inputs)
        if entry.get("type") == "http" and not has:
            return jsonc.append_item(text, ["inputs"], {
                "type": "promptString", "id": VSCODE_INPUT, "password": True,
                "description": "MCP for Primavera P6 access token (MCP for Primavera P6 Settings, Server page, Copy)"})
        if entry.get("type") != "http" and has:
            return self.extra_unregister(text)
        return text

    def extra_unregister(self, text: str) -> str:
        return jsonc.remove_items(text, ["inputs"], lambda i: isinstance(i, dict) and i.get("id") == VSCODE_INPUT)


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

    def skills_dir(self) -> str:  # global skills (Cline 3.48 and later)
        return os.path.join(_home(), ".cline", "skills")

    def legacy_commands_dir(self) -> str:  # global workflows: ~/Documents/Cline/Workflows (home, not the shell folder)
        return os.path.join(_home(), "Documents", "Cline", "Workflows")

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

    def http_entry(self, http: HttpTarget) -> dict:  # without type Cline falls back to legacy SSE
        return {"type": "streamableHttp", "url": http.url, "headers": {"Authorization": f"Bearer {http.token}"},
                "disabled": False, "autoApprove": []}


class ClaudeCode(Adapter):
    id, name = "claude_code", "Claude Code"
    process_names = ()  # claude.exe is also Claude Desktop's process name: no running check
    docs = "https://code.claude.com/docs/en/mcp"
    prompts_native = True  # also as /mcp__p6mcp__<prompt>

    def config_path(self) -> str:
        return os.path.join(_home(), ".claude.json")

    def skills_dir(self) -> str:
        return _claude_skills()

    def legacy_commands_dir(self) -> str:
        return os.path.join(_home(), ".claude", "commands")

    def installed(self) -> bool:
        return os.path.isfile(self.config_path()) or os.path.isdir(os.path.join(_home(), ".claude"))

    def stdio_entry(self, ln: Launch) -> dict:  # without type an entry with a url is a config error
        return {"type": "stdio", "command": ln.command, "args": list(ln.args)}

    def http_entry(self, http: HttpTarget) -> dict:
        return {"type": "http", "url": http.url, "headers": {"Authorization": f"Bearer {http.token}"}}


ADAPTERS: tuple[Adapter, ...] = (ClaudeDesktop(), ClaudeCode(), Cursor(), VSCode(), VSCodeInsiders(), LMStudio(),
                                 Cline())
BY_ID = {a.id: a for a in ADAPTERS}
