"""Server instructions and the user's own P6 commands (Instructions and Commands pages of the Settings app)."""

import asyncio
import dataclasses
import re
import subprocess

import pytest

from p6mcp import clients, config, instructions, prompts, settings_logic
from p6mcp.clients.base import Launch

LN = Launch(r"C:\Program Files\P6 MCP\p6mcp-server.exe", [])
ACT = "When you show an activity, write it as Activity ID - Activity name (A1230 - Excavation)."
MINE = {"name": "float-report", "title": "Float report", "description": "Activities with low float.",
        "args": [{"name": "project", "description": "Project ID", "default": None},
                 {"name": "days", "description": "Float limit in days", "default": "5"}],
        "template": "List the activities of {project} with total float under {days} days:\n"
                    "p6_float_analysis(project=\"{project}\"). Keep {braces} and {\"json\": 1} as written.",
        "enabled": True}


def _head_instructions() -> str:
    """INSTRUCTIONS of server.py before they moved to p6mcp.instructions (commit fba4211)."""
    src = subprocess.run(["git", "show", "fba4211:p6mcp/server.py"], capture_output=True, text=True,
                         encoding="utf-8", cwd=config.__file__.rsplit("p6mcp", 1)[0]).stdout
    m = re.search(r'INSTRUCTIONS = """(.*?)"""', src, re.S)
    if not m:
        pytest.skip("git history not available")
    return m.group(1).replace("\r\n", "\n")


# ----------------------------------------------------------------- instructions

def test_default_instructions_are_unchanged():
    assert instructions.build(None) == instructions.build(config.DEFAULTS) == _head_instructions()


def test_instructions_follow_the_settings():
    data = settings_logic.set_instruction_enabled(config.DEFAULTS, "sql", False)
    data = settings_logic.put_instruction(data, "  " + ACT + "\n ")
    data = settings_logic.put_instruction(data, "Answer in French.")
    data = settings_logic.set_custom_instruction_enabled(data, 1, False)
    text = instructions.build(config.validate(data))
    assert "p6_sql_select" not in text and "Writes take two calls" in text
    assert text.endswith(instructions.CUSTOM_HEADER + "\n- " + ACT) and "French" not in text
    data = settings_logic.move_instruction(data, 1, -1)
    assert [i["text"] for i in data["custom_instructions"]] == ["Answer in French.", ACT]
    assert settings_logic.move_instruction(data, 0, -1) == data
    assert settings_logic.remove_instruction(data, 0)["custom_instructions"] == [{"text": ACT, "enabled": True}]
    with pytest.raises(ValueError):
        settings_logic.set_instruction_enabled(data, "writes", False)  # the two-step write rule stays on
    with pytest.raises(ValueError):
        settings_logic.put_instruction(data, "   ")
    with pytest.raises(config.ConfigError):
        config.validate({"custom_instructions": [{"text": ""}]})
    with pytest.raises(config.ConfigError):
        config.validate({"instructions_disabled": "sql"})


# ----------------------------------------------------------------- commands

def test_custom_command_render_and_checks():
    data = settings_logic.put_command(config.DEFAULTS, MINE)
    cmd = prompts.custom_commands(config.validate(data))[0]
    assert cmd.custom and cmd.tools() == ["p6_float_analysis"]
    text = prompts.render(cmd, {"project": "DEMO"})
    assert "of DEMO with total float under 5 days" in text and '{braces} and {"json": 1}' in text
    assert text.endswith(prompts.RULES)
    cc = prompts.command_file(cmd, "claude_code")
    assert "arguments: project days" in cc and "of $project" in cc and prompts.MARKER in cc

    for bad, why in ((dict(MINE, name="weekly-update-review"), "built-in"), (dict(MINE, name="Bad Name"), "name"),
                     (dict(MINE, template=" "), "empty"),
                     (dict(MINE, args=[{"name": "rules"}]), "rules"),
                     (dict(MINE, args=[{"name": "a"}, {"name": "a"}]), "twice")):
        with pytest.raises(ValueError, match=why):
            settings_logic.put_command(config.DEFAULTS, bad)
    with pytest.raises(ValueError, match="twice"):
        settings_logic.put_command(data, MINE)
    renamed = settings_logic.put_command(data, dict(MINE, name="low-float"), old_name="float-report")
    assert [c["name"] for c in renamed["custom_commands"]] == ["low-float"]
    assert settings_logic.remove_command(renamed, "low-float")["custom_commands"] == []
    with pytest.raises(config.ConfigError):
        config.validate({"custom_commands": [dict(MINE, name="x y")]})


def test_offered_with_turned_off_and_custom_commands():
    data = settings_logic.put_command(config.DEFAULTS, MINE)
    data = settings_logic.put_command(data, dict(MINE, name="free-text", template="Summarise the project."))
    data = settings_logic.set_command_enabled(data, "baseline-variance", False)
    names = [c.name for c in prompts.offered("full", True, data)]
    assert "baseline-variance" not in names and names[-2:] == ["float-report", "free-text"]
    # Basic profile: the built-in ones need Full tools, p6_float_analysis is a Basic tool, free-text names none
    assert [c.name for c in prompts.offered("basic", False, data)] == ["float-report", "free-text"]
    data = settings_logic.set_command_enabled(data, "float-report", False)
    assert "float-report" not in [c.name for c in prompts.offered("full", True, data)]
    rows = {r["name"]: r for r in settings_logic.command_rows(dict(data, tool_profile="basic"))}
    assert rows["weekly-update-review"]["missing_tools"] and not rows["weekly-update-review"]["offered"]
    assert rows["free-text"]["offered"] and not rows["float-report"]["enabled"]
    assert settings_logic.set_command_enabled(data, "baseline-variance", True)["commands_disabled"] == []
    assert prompts.offered("full", True) == prompts.offered("full", True, config.DEFAULTS)


def test_server_registers_custom_prompts_and_instructions(server, monkeypatch):
    raw = config.validate(settings_logic.put_instruction(settings_logic.put_command(
        dict(config.DEFAULTS, tool_profile="full", allow_writes=True, commands_disabled=["delay-whatif"]), MINE),
        ACT))
    monkeypatch.setattr(server, "STARTUP", dataclasses.replace(server.STARTUP, raw=raw))
    monkeypatch.setattr(server, "PROMPTS", {})
    monkeypatch.setattr(server, "mcp", server.FastMCP("primavera-p6", instructions=instructions.build(raw)))
    server._register_prompts()
    assert "float-report" in server.PROMPTS and "delay-whatif" not in server.PROMPTS
    assert server.mcp.instructions.endswith(ACT)
    if not hasattr(server.mcp, "list_prompts"):
        pytest.skip("this SDK has no prompt registry")
    listed = {p.name: p for p in asyncio.run(server.mcp.list_prompts())}
    assert [(a.name, a.required) for a in listed["float-report"].arguments] == [("project", True), ("days", False)]
    res = asyncio.run(server.mcp.get_prompt("float-report", {"project": "DEMO"}))
    assert "of DEMO with total float under 5 days" in res.messages[0].content.text


def test_command_files_include_custom_commands(tmp_path, monkeypatch):
    for var, sub in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local"), ("USERPROFILE", "User")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setenv("P6MCP_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr("p6mcp.clients.base.launch", lambda: LN)
    config.save(settings_logic.put_command(dict(config.DEFAULTS, tool_profile="full", allow_writes=True), MINE))
    ad = clients.get("cursor")
    res = ad.install_commands("full", True)
    assert "p6-float-report.md" in res["written"] and ad.commands_state() == clients.CMD_INSTALLED
    data = config.read_file()[0]
    config.save(settings_logic.set_command_enabled(data, "float-report", False))
    assert ad.commands_state() == clients.CMD_OUTDATED  # turned off: the file must go
    assert ad.install_commands("full", True)["removed"] == ["p6-float-report.md"]


def test_restart_needed_for_instructions_and_commands():
    data = settings_logic.put_instruction(config.DEFAULTS, ACT)
    assert settings_logic.restart_needed(config.DEFAULTS, data) == ["custom_instructions"]
