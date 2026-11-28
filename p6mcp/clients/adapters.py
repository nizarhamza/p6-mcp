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

Checked on 2026-11-28:
- Devin (Windsurf, renamed Devin Desktop on 2026-06-02): docs.devin.ai/cli/extensibility/mcp/configuration: user
  file %APPDATA%\\devin\\mcp_config.json, {"mcpServers": {name: {command, args, env} or {url, headers}}}, read by
  the Devin CLI and Devin Local (the default agent of Devin Desktop), and the file Cascade's "Open MCP config
  file" opens (docs.devin.ai/windsurf/plugins/cascade/mcp). The pre-rename ~/.codeium/windsurf/mcp_config.json
  is not written (the editor's MCP discovery and the Devin CLI's Windsurf import read it).
- Zed: zed.dev/docs/ai/mcp and docs/configuring-zed: %APPDATA%\\Zed\\settings.json (JSONC), {"context_servers":
  {name: {command, args, env} or {url, headers}}}.
- Continue: docs.continue.dev/customize/deep-dives/mcp: block files in .continue/mcpServers (name, version,
  schema v1, an mcpServers list of {name, command, args, env} or {name, type: streamable-http, url,
  requestOptions {headers}}); the global ~/.continue/mcpServers (CONTINUE_GLOBAL_DIR overrides ~/.continue) is
  loaded too (Continue's core/config/yaml/loadYaml.ts, includeGlobal). The block file p6mcp.yaml is ours alone,
  written as JSON (valid YAML): the user's config.yaml is never edited.
- Gemini CLI: geminicli.com/docs/tools/mcp-server: ~/.gemini/settings.json, {"mcpServers": {name: {command, args,
  env} or {httpUrl, headers}}} (url alone means SSE); read at start.
- Codex (the CLI, the IDE extension and the ChatGPT desktop app share it): learn.chatgpt.com/docs/extend/mcp and
  docs/config-file/config-reference: ~/.codex/config.toml (CODEX_HOME overrides ~/.codex), [mcp_servers.<name>]
  {command, args, env, startup_timeout_sec, tool_timeout_sec} or {url, http_headers}; edited through
  p6mcp.tomledit (only that table changes); restart Codex after a change.

P6 skills folders (p6mcp.skills, Agent Skills: a folder per skill with SKILL.md), checked on 2026-10-28:
Claude Code ~/.claude/skills (code.claude.com/docs/en/skills); Cursor reads ~/.claude/skills besides its own
folders (cursor.com/docs/context/skills) and so does VS Code (code.visualstudio.com/docs/agent-customization/
agent-skills; same-name skills deduplicated), so these three share ~/.claude/skills (writing ~/.cursor/skills
too would show Cursor duplicates); Cline ~/.cline/skills (docs.cline.bot/customization/skills; it reads
.claude/skills only inside a workspace). Every client needs name = folder name and a description of at most
1,024 characters. Claude Desktop has no skills folder (zip upload): it lists the server's MCP prompts itself;
LM Studio has neither. Command folders of P6 MCP 1.4 to 1.7 (legacy_commands_dir, cleaned up): VS Code
prompts folder, ~/.cursor/commands, ~/Documents/Cline/Workflows, ~/.claude/commands.

~/.agents/skills (checked on 2026-11-28) is the user skills folder of Codex (learn.chatgpt.com/docs/build-skills),
Zed (zed.dev/docs/ai/skills), Gemini CLI (geminicli.com/docs/cli/skills: alias of ~/.gemini/skills that wins over
it) and Devin (docs.devin.ai/cli/extensibility/skills/overview): these four share it. Cursor and VS Code read it
as well as ~/.claude/skills. Continue reads ~/.continue/skills, but its loader lists that folder twice
(core/config/markdown/loadMarkdownSkills.ts), so no skills are written for it (it gets the MCP entry only).
"""

from __future__ import annotations

import glob
import json
import os
from typing import Optional

from p6mcp import jsonc, tomledit
from p6mcp.clients.base import Adapter, HttpTarget, Launch

VSCODE_INPUT = "p6mcp-token"


def _env(name: str) -> str:
    return os.environ.get(name) or ""


def _home() -> str:
    return _env("USERPROFILE") or os.path.expanduser("~")


def _claude_skills() -> str:
    """~/.claude/skills: read by Claude Code, Cursor and VS Code."""
    return os.path.join(_home(), ".claude", "skills")


def _agents_skills() -> str:
    """~/.agents/skills: read by Codex, Zed, Gemini CLI and Devin (and by Cursor and VS Code)."""
    return os.path.join(_home(), ".agents", "skills")


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


class Devin(Adapter):
    id, name = "devin", "Devin (Windsurf)"
    process_names = ("devin.exe",)
    docs = "https://docs.devin.ai/cli/extensibility/mcp/configuration"
    note = "cl.note.devin"

    def _folder(self) -> str:  # also the Devin Desktop user data folder (%APPDATA%\Devin: same folder on Windows)
        return os.path.join(_env("APPDATA"), "devin")

    def config_path(self) -> str:
        return os.path.join(self._folder(), "mcp_config.json")

    def skills_dir(self) -> str:
        return _agents_skills()

    def installed(self) -> bool:
        return (os.path.isdir(self._folder())
                or os.path.isfile(os.path.join(_env("LOCALAPPDATA"), "devin", "bin", "devin.exe")))


class Zed(Adapter):
    id, name = "zed", "Zed"
    container = ("context_servers",)
    process_names = ("zed.exe",)
    docs = "https://zed.dev/docs/ai/mcp"

    def config_path(self) -> str:
        return os.path.join(_env("APPDATA"), "Zed", "settings.json")

    def skills_dir(self) -> str:
        return _agents_skills()

    def installed(self) -> bool:
        return (os.path.isdir(os.path.join(_env("APPDATA"), "Zed"))
                or os.path.isfile(os.path.join(_env("LOCALAPPDATA"), "Programs", "Zed", "Zed.exe")))


CONTINUE_BLOCK = {"name": "MCP for Primavera P6", "version": "1.0.0", "schema": "v1"}


class Continue(Adapter):
    """Continue loads every block file of ~/.continue/mcpServers: ours is p6mcp.yaml, written as JSON (valid YAML,
    read back with jsonc), so the user's config.yaml is never touched. Its mcpServers list holds {name: p6mcp,
    ...entry}; servers() maps the list by name."""
    id, name = "continue", "Continue"
    process_names = ()  # an extension of VS Code or JetBrains: no process of its own
    docs = "https://docs.continue.dev/customize/deep-dives/mcp"
    note = "cl.note.continue"

    def _home_dir(self) -> str:
        return _env("CONTINUE_GLOBAL_DIR") or os.path.join(_home(), ".continue")

    def config_path(self) -> str:
        return os.path.join(self._home_dir(), "mcpServers", "p6mcp.yaml")

    def installed(self) -> bool:
        return (os.path.isdir(self._home_dir())
                or bool(glob.glob(os.path.join(_home(), ".vscode", "extensions", "continue.continue-*"))))

    def parse(self, text: str) -> dict:
        try:
            data = super().parse(text)
        except jsonc.JsoncError as e:
            raise jsonc.JsoncError(f"{e}. This block file was not written by MCP for Primavera P6, or was edited as "
                                   "YAML: left unchanged") from None
        if not isinstance(data.get("mcpServers", []), list):
            raise jsonc.JsoncError("mcpServers is not a list")
        return data

    def servers(self, data) -> dict:
        items = data.get("mcpServers") if isinstance(data, dict) else None
        return {i["name"]: {k: v for k, v in i.items() if k != "name"} for i in items or []
                if isinstance(i, dict) and isinstance(i.get("name"), str)}

    @staticmethod
    def _is(key: str):
        return lambda i: isinstance(i, dict) and i.get("name") == key

    def set_entry(self, text: str, key: str, entry: dict) -> str:
        item = {"name": key, **entry}
        if not text.strip():
            return json.dumps({**CONTINUE_BLOCK, "mcpServers": [item]}, indent=2) + "\n"
        if "mcpServers" not in self.parse(text):
            return jsonc.set_member(text, [], "mcpServers", [item])
        return jsonc.append_item(jsonc.remove_items(text, ["mcpServers"], self._is(key)), ["mcpServers"], item)

    def delete_entry(self, text: str, key: str) -> Optional[str]:
        data = self.parse(text)
        if key not in self.servers(data):
            return text
        if len(data.get("mcpServers") or []) == 1:
            return None  # the file only held this server: deleted
        return jsonc.remove_items(text, ["mcpServers"], self._is(key))

    def http_entry(self, http: HttpTarget) -> dict:
        return {"type": "streamable-http", "url": http.url,
                "requestOptions": {"headers": {"Authorization": f"Bearer {http.token}"}}}


class GeminiCli(Adapter):
    id, name = "gemini", "Gemini CLI"
    process_names = ()  # runs in node.exe
    docs = "https://geminicli.com/docs/tools/mcp-server/"

    def config_path(self) -> str:
        return os.path.join(_home(), ".gemini", "settings.json")

    def skills_dir(self) -> str:  # its ~/.agents/skills alias wins over ~/.gemini/skills
        return _agents_skills()

    def installed(self) -> bool:
        return os.path.isdir(os.path.join(_home(), ".gemini"))

    def http_entry(self, http: HttpTarget) -> dict:  # httpUrl = Streamable HTTP (url alone would be SSE)
        return {"httpUrl": http.url, "headers": {"Authorization": f"Bearer {http.token}"}}


# Codex defaults are 10 s to start and 60 s per tool: a cold start of the server exe or a long tool (schedule risk,
# XER import) can pass them
CODEX_TIMEOUTS = {"startup_timeout_sec": 30, "tool_timeout_sec": 600}


class Codex(Adapter):
    id, name = "codex", "Codex"
    container = ("mcp_servers",)
    process_names = ("codex.exe",)
    docs = "https://learn.chatgpt.com/docs/extend/mcp"
    note = "cl.note.codex"

    def _home_dir(self) -> str:
        return _env("CODEX_HOME") or os.path.join(_home(), ".codex")

    def config_path(self) -> str:
        return os.path.join(self._home_dir(), "config.toml")

    def skills_dir(self) -> str:
        return _agents_skills()

    def installed(self) -> bool:
        return os.path.isdir(self._home_dir())

    def parse(self, text: str) -> dict:
        return tomledit.loads(text)

    def set_entry(self, text: str, key: str, entry: dict) -> str:
        return tomledit.set_table(text, [*self.container, key], entry)

    def delete_entry(self, text: str, key: str) -> Optional[str]:
        return tomledit.delete_table(text, [*self.container, key])

    def stdio_entry(self, ln: Launch) -> dict:
        return {"command": ln.command, "args": list(ln.args), **CODEX_TIMEOUTS}

    def http_entry(self, http: HttpTarget) -> dict:
        return {"url": http.url, "http_headers": {"Authorization": f"Bearer {http.token}"}, **CODEX_TIMEOUTS}


ADAPTERS: tuple[Adapter, ...] = (ClaudeDesktop(), ClaudeCode(), Cursor(), VSCode(), VSCodeInsiders(), LMStudio(),
                                 Cline(), Devin(), Zed(), Continue(), GeminiCli(), Codex())
BY_ID = {a.id: a for a in ADAPTERS}
