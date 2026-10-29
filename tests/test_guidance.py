"""Server instructions and the user's own P6 skills (Instructions and Skills pages of the Settings app)."""

import asyncio
import dataclasses
import re
import subprocess

import pytest

from p6mcp import clients, config, core_skills, instructions, settings_logic, skills
from p6mcp.clients.base import Launch

LN = Launch(r"C:\Program Files\P6 MCP\p6mcp-server.exe", [])
ACT = "When you show an activity, write it as Activity ID - Activity name (A1230 - Excavation)."
MINE = {"name": "float-report", "title": "Float report", "description": "Activities with low float. Use when asked.",
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
    data = settings_logic.put_skill(config.DEFAULTS, MINE)
    cmd = skills.custom_skills(config.validate(data))[0]
    assert cmd.custom and cmd.tools() == ["p6_float_analysis"]
    text = skills.render(cmd, {"project": "DEMO"})
    assert "of DEMO with total float under 5 days" in text and '{braces} and {"json": 1}' in text
    assert text.endswith(skills.RULES)
    md = skills.skill_md(cmd)
    assert "name: p6-float-report\n" in md and "of <project>" in md and skills.MARKER in md

    assert settings_logic.put_skill(config.DEFAULTS, dict(MINE, name="weekly-update-review"))  # no name reserved
    for bad, why in ((dict(MINE, name="Bad Name"), "name"), (dict(MINE, template=" "), "empty"),
                     (dict(MINE, args=[{"name": "rules"}]), "rules"),
                     (dict(MINE, args=[{"name": "a"}, {"name": "a"}]), "twice")):
        with pytest.raises(ValueError, match=why):
            settings_logic.put_skill(config.DEFAULTS, bad)
    with pytest.raises(ValueError, match="twice"):
        settings_logic.put_skill(data, MINE)
    renamed = settings_logic.put_skill(data, dict(MINE, name="low-float"), old_name="float-report")
    assert [c["name"] for c in renamed["custom_skills"]] == ["low-float"]
    assert settings_logic.remove_skill(renamed, "low-float")["custom_skills"] == []
    with pytest.raises(config.ConfigError):
        config.validate({"custom_skills": [dict(MINE, name="x y")]})


def test_offered_with_turned_off_and_custom_commands():
    data = dict(config.DEFAULTS, core_skills_installed=True)  # the core set is in custom_skills like any skill
    data = settings_logic.put_skill(data, MINE)
    data = settings_logic.put_skill(data, dict(MINE, name="free-text", template="Summarise the project."))
    data = settings_logic.put_skill(data, dict(MINE, name="variance", template="Run p6_compare_projects."))
    data = settings_logic.set_skill_enabled(data, "variance", False)
    assert [c.name for c in skills.offered("full", True, data)] == ["float-report", "free-text"]
    # Basic profile: p6_float_analysis is a Basic tool, free-text names none
    assert [c.name for c in skills.offered("basic", False, data)] == ["float-report", "free-text"]
    data = settings_logic.set_skill_enabled(data, "float-report", False)
    assert "float-report" not in [c.name for c in skills.offered("full", True, data)]
    rows = {r["name"]: r for r in settings_logic.skill_rows(dict(data, tool_profile="basic"))}
    assert list(rows) == ["float-report", "free-text", "variance"]  # only the skills of the config
    assert rows["variance"]["missing_tools"] == ["p6_compare_projects"] and not rows["variance"]["offered"]
    assert rows["free-text"]["offered"] and not rows["float-report"]["enabled"] and not rows["free-text"]["edited"]
    with pytest.raises(ValueError, match="No skill"):
        settings_logic.set_skill_enabled(data, "weekly-update-review", False)  # not a built-in skill any more
    assert skills.offered("full", True) == skills.offered("full", True, config.DEFAULTS)


def test_server_registers_custom_prompts_and_instructions(server, monkeypatch):
    core = [s.name for s in core_skills.bundled_skills()]  # offered until the core set is installed
    raw = config.validate(settings_logic.put_instruction(settings_logic.put_skill(
        dict(config.DEFAULTS, tool_profile="full", allow_writes=True, skills_disabled=core[:1]), MINE),
        ACT))
    monkeypatch.setattr(server, "STARTUP", dataclasses.replace(server.STARTUP, raw=raw))
    monkeypatch.setattr(server, "PROMPTS", {})
    monkeypatch.setattr(server, "mcp", server.FastMCP("primavera-p6", instructions=instructions.build(raw)))
    server._register_prompts()
    assert sorted(server.PROMPTS) == sorted(core[1:] + ["float-report"])  # the 1.8 skills_disabled still apply
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
    config.save(settings_logic.put_skill(dict(config.DEFAULTS, tool_profile="full", allow_writes=True), MINE))
    ad = clients.get("cursor")
    res = ad.install_skills("full", True)
    assert "p6-float-report" in res["written"] and ad.skills_state() == clients.SKILLS_INSTALLED
    data = config.read_file()[0]
    config.save(settings_logic.set_skill_enabled(data, "float-report", False))
    assert ad.skills_state() == clients.SKILLS_OUTDATED  # turned off: the folder must go
    assert ad.install_skills("full", True)["removed"] == ["p6-float-report"]


def test_strict_rules_for_new_skills_only(server):
    """A stored skill of an older version keeps loading; a new or edited one gets the strict rules, and an
    unknown tool never breaks the server, the pages or the clients (it is simply not offered)."""
    typo = dict(MINE, name="typo", description="", template="Run p6_not_a_tool and p6_list_projects.")
    data = config.validate({"custom_skills": [typo]})  # stored: loads
    skill = skills.custom_skills(data)[0]
    assert skills.missing_tools(skill, "full", True) == ["p6_not_a_tool"] and not skills.available(skill, "full", True)
    assert [s.name for s in skills.offered("full", True, data)] == [s.name for s in core_skills.bundled_skills()]
    rows = {r["name"]: r for r in settings_logic.skill_rows(dict(data, tool_profile="full", allow_writes=True))}
    assert rows["typo"]["missing_tools"] == ["p6_not_a_tool"] and not rows["typo"]["offered"]
    for bad, why in ((dict(typo, description="Fine."), "unknown tools p6_not_a_tool"),
                     (dict(MINE, description=""), "description is empty"),
                     (dict(MINE, description="x" * 1025), "one line"),
                     (dict(MINE, title="two\nlines"), "title"),
                     (dict(MINE, files=[{"path": "../up.md", "text": "x"}]), "reference file"),
                     (dict(MINE, files=[{"path": "run.ps1", "text": "x"}]), "reference file"),
                     (dict(MINE, files=[{"path": "con.md", "text": "x"}]), "reserved"),
                     (dict(MINE, files=[{"path": "a.md", "text": "x" * (skills.MAX_FILE_TEXT + 1)}]), "longer"),
                     (dict(MINE, files=[{"path": f"f{i}.md", "text": "x"} for i in range(6)]), "at most 5"),
                     (dict(MINE, files=[{"path": "A.md", "text": "x"}, {"path": "a.md", "text": "y"}]), "twice")):
        with pytest.raises(ValueError, match=why):
            settings_logic.put_skill(config.DEFAULTS, bad)
    ok = settings_logic.put_skill(config.DEFAULTS, dict(MINE, files=[{"path": "ref\\Notes.md", "text": "n"}]))
    assert ok["custom_skills"][0]["files"] == [{"path": "ref/Notes.md", "text": "n"}]


def test_edited_shop_skill_keeps_its_source_and_is_marked_edited():
    """1.9: an edited shop skill keeps its source (it was dropped in 1.8): its content_sha tells the automatic
    update to leave it alone, and the Shop page still offers the update."""
    stored = settings_logic.put_skill(config.DEFAULTS, MINE)["custom_skills"][0]
    src = {"shop": "float-report", "version": 2, "sha256": "ab", "content_sha": skills.content_sha(stored)}
    data = settings_logic.put_skill(config.DEFAULTS, dict(MINE, source=src))
    assert data["custom_skills"][0]["source"] == src and not skills.edited(data["custom_skills"][0])
    data = settings_logic.set_skill_enabled(data, "float-report", False)
    assert not skills.edited(data["custom_skills"][0])  # turned off, not edited
    renamed = settings_logic.put_skill(data, dict(data["custom_skills"][0], name="my-floats"), "float-report")
    assert not skills.edited(renamed["custom_skills"][0])  # the name is not content
    edited = settings_logic.put_skill(data, dict(data["custom_skills"][0], template="Other p6_lookahead."),
                                      "float-report")
    assert edited["custom_skills"][0]["source"] == src and skills.edited(edited["custom_skills"][0])
    assert settings_logic.skill_rows(edited)[0]["edited"]
    assert skills.edited(dict(stored, source={"shop": "x"}))  # no content_sha recorded: never replaced
    assert not skills.edited(stored)  # the user's own skill


def test_restart_needed_for_instructions_and_commands():
    data = settings_logic.put_instruction(config.DEFAULTS, ACT)
    assert settings_logic.restart_needed(config.DEFAULTS, data) == ["custom_instructions"]
