"""P6 commands (p6mcp.prompts): MCP prompts of the server and command files in the clients' folders
(a fake user profile: the real client folders are never touched)."""

import asyncio
import inspect
import json
import os
import re

import pytest

from p6mcp import clients, policy, prompts
from p6mcp.clients.__main__ import main as cli
from p6mcp.clients.base import Launch

LN = Launch(r"C:\Program Files\P6 MCP\p6mcp-server.exe", [])
NAMES = [c.name for c in prompts.COMMANDS]


@pytest.fixture
def home(tmp_path, monkeypatch):
    for var, sub in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local"), ("USERPROFILE", "User")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.delenv("CLINE_MCP_SETTINGS_PATH", raising=False)
    monkeypatch.setenv("P6MCP_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(clients, "launch", lambda: LN)
    monkeypatch.setattr("p6mcp.clients.base.launch", lambda: LN)
    monkeypatch.setattr("p6mcp.clients._running_names", lambda: set())
    from p6mcp import config
    config.save({"tool_profile": "full", "allow_writes": True})
    return tmp_path


def test_texts_name_existing_tools_and_parameters(server):
    for cmd in prompts.COMMANDS:
        text = prompts.render(cmd, {a.name: "SAMPLE" for a in cmd.args})
        assert text.isascii() and "--" not in text, cmd.name
        assert cmd.tools() and all(t in policy.TOOL_KIND for t in cmd.tools()), cmd.name
        for tool, args in re.findall(r"\b(p6_\w+)\(([^()]*)\)", text):
            params = inspect.signature(server.REGISTERED[tool]).parameters
            assert all(a in params for a in re.findall(r"(\w+)=", args)), (cmd.name, tool, args)
        assert "{" not in prompts.render(cmd, {}).replace('{"op"', ""), cmd.name  # every field filled


def test_offered_follows_the_tool_settings():
    assert prompts.offered("basic", True) == []
    assert [c.name for c in prompts.offered("full", False)] == [n for n in NAMES if n != "update-import-safely"]
    assert [c.name for c in prompts.offered("full", True)] == NAMES


def test_server_offers_them_as_mcp_prompts(server):
    assert sorted(server.PROMPTS) == sorted(NAMES)  # the server fixture: full profile, writes allowed
    if not hasattr(server.mcp, "list_prompts"):
        pytest.skip("this SDK has no prompt registry")
    listed = asyncio.run(server.mcp.list_prompts())
    assert sorted(p.name for p in listed) == sorted(NAMES)
    wk = next(p for p in listed if p.name == "weekly-update-review")
    assert [(a.name, a.required) for a in wk.arguments] == [("project", True), ("weeks", False)]
    res = asyncio.run(server.mcp.get_prompt("weekly-update-review", {"project": "DEMO"}))
    text = res.messages[0].content.text
    assert res.messages[0].role == "user" and "project DEMO" in text and "weeks=3" in text


def test_command_files_per_style():
    cmd = prompts.BY_NAME["weekly-update-review"]
    vs = prompts.command_file(cmd, "vscode")
    assert vs.startswith("---\ndescription: ") and "agent: agent" in vs and "${input:project:" in vs
    cc = prompts.command_file(cmd, "claude_code")
    assert "arguments: project weeks" in cc and 'argument-hint: "<project> [weeks]"' in cc and '"$project"' in cc
    pl = prompts.command_file(cmd, "plain")
    assert pl.startswith("# Weekly update review") and '"<project>"' in pl and "weeks (default 3)" in pl
    for text in (vs, cc, pl):
        assert prompts.MARKER in text and text.isascii()
    assert prompts.file_name(cmd, "vscode") == "p6-weekly-update-review.prompt.md"
    assert prompts.file_name(cmd, "plain") == "p6-weekly-update-review.md"
    with pytest.raises(ValueError):
        prompts.command_file(cmd, "other")


@pytest.mark.parametrize("cid,folder,suffix", [
    ("cursor", ("User", ".cursor", "commands"), ".md"),
    ("vscode", ("Roaming", "Code", "User", "prompts"), ".prompt.md"),
    ("cline", ("User", "Documents", "Cline", "Workflows"), ".md"),
    ("claude_code", ("User", ".claude", "commands"), ".md"),
])
def test_install_update_and_remove_commands(home, cid, folder, suffix):
    ad = clients.get(cid)
    d = home.joinpath(*folder)
    assert ad.commands_dir() == str(d) and ad.commands_state() == clients.CMD_NONE
    d.mkdir(parents=True)
    mine = d / ("p6-baseline-variance" + suffix)
    mine.write_text("my own command", encoding="utf-8")  # a file of the user's with one of our names
    other = d / ("notes" + suffix)
    other.write_text("unrelated", encoding="utf-8")

    res = ad.install_commands("full", True)
    assert res["skipped"] == [mine.name] and len(res["written"]) == 4 and res["folder"] == str(d)
    assert mine.read_text(encoding="utf-8") == "my own command" and other.exists()
    mine.unlink()
    res = ad.install_commands("full", True)
    assert res["written"] == [mine.name] and ad.commands_state("full", True) == clients.CMD_INSTALLED
    assert ad.install_commands("full", True)["changed"] is False

    imp = d / ("p6-update-import-safely" + suffix)
    imp.write_text(imp.read_text(encoding="utf-8") + "edited", encoding="utf-8")
    assert ad.commands_state("full", True) == clients.CMD_OUTDATED
    res = ad.install_commands("full", False)  # writes off: the import command is no longer offered
    assert res["removed"] == [imp.name] and not imp.exists()
    assert ad.commands_state("full", False) == clients.CMD_INSTALLED
    assert ad.commands_state("full", True) == clients.CMD_OUTDATED

    res = ad.remove_commands()
    assert len(res["removed"]) == 4 and sorted(os.listdir(d)) == [other.name]


@pytest.mark.parametrize("cid", ["claude_desktop", "lmstudio"])
def test_clients_without_command_folder(home, cid):
    ad = clients.get(cid)
    assert ad.commands_state() == clients.CMD_UNSUPPORTED
    with pytest.raises(ValueError):
        ad.install_commands("full", True)
    assert ad.prompts_native is (cid == "claude_desktop")


def test_scan_reports_command_state(home):
    (home / "User" / ".cursor").mkdir()
    st = {s.client: s for s in clients.scan()}
    assert st["cursor"].commands == clients.CMD_NONE and st["claude_desktop"].commands == clients.CMD_UNSUPPORTED
    clients.get("cursor").install_commands("full", True)
    assert {s.client: s for s in clients.scan()}["cursor"].commands == clients.CMD_INSTALLED


def test_claude_code_registration_keeps_the_rest(home):
    path = home / "User" / ".claude.json"
    before = {"numStartups": 12, "mcpServers": {"other": {"command": "npx", "args": ["x"]}},
              "projects": {"D:/w": {"mcpServers": {}}}}
    path.write_text(json.dumps(before, indent=2), encoding="utf-8")
    ad = clients.get("claude_code")
    assert ad.installed() and ad.status().state == clients.NOT_REGISTERED
    ad.register()
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["mcpServers"]["p6mcp"] == {"type": "stdio", "command": LN.command, "args": []}
    assert after["mcpServers"]["other"] == before["mcpServers"]["other"] and after["projects"] == before["projects"]
    assert ad.status().state == clients.REGISTERED
    http = clients.HttpTarget("http://127.0.0.1:39217/mcp", "tok")
    assert ad.expected(http=http) == {"type": "http", "url": "http://127.0.0.1:39217/mcp",
                                      "headers": {"Authorization": "Bearer tok"}}
    ad.unregister()
    assert "p6mcp" not in json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_cli_commands(home, capsys):
    (home / "User" / ".cursor").mkdir()
    assert cli(["commands", "install", "--detected"]) == 0
    assert "cursor: 5 written" in capsys.readouterr().out
    assert cli(["commands", "install", "claude_desktop"]) == 1
    assert cli(["commands", "remove", "cursor"]) == 0
    assert "cursor: 5 removed" in capsys.readouterr().out
