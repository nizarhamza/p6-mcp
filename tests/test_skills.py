"""P6 skills (p6mcp.skills): MCP prompts of the server and skill folders in the clients' skills folders
(a fake user profile: the real client folders are never touched)."""

import asyncio
import inspect
import json
import os
import re

import pytest

from p6mcp import clients, policy, skills
from p6mcp.clients.__main__ import main as cli
from p6mcp.clients.base import Launch

LN = Launch(r"C:\Program Files\P6 MCP\p6mcp-server.exe", [])
NAMES = [s.name for s in skills.BUILTIN]
FOLDERS = [skills.PREFIX + n for n in NAMES]
SHARED = ("User", ".claude", "skills")  # Claude Code, Cursor and VS Code read ~/.claude/skills


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


def frontmatter(text: str) -> dict:
    head = text.split("---\n")[1]
    return {k: json.loads(v) if v.startswith('"') else v
            for k, v in (line.split(": ", 1) for line in head.strip().splitlines())}


def test_texts_name_existing_tools_and_parameters(server):
    for s in skills.BUILTIN:
        text = skills.render(s, {a.name: "SAMPLE" for a in s.args})
        assert text.isascii() and "--" not in text, s.name
        assert s.tools() and all(t in policy.TOOL_KIND for t in s.tools()), s.name
        for tool, args in re.findall(r"\b(p6_\w+)\(([^()]*)\)", text):
            params = inspect.signature(server.REGISTERED[tool]).parameters
            assert all(a in params for a in re.findall(r"(\w+)=", args)), (s.name, tool, args)
        assert "{" not in skills.render(s, {}).replace('{"op"', ""), s.name  # every field filled
        assert "Use when" in s.description and len(s.description) <= skills.MAX_DESCRIPTION, s.name
        assert s.description.isascii() and skills._one_line(s.description), s.name


def test_offered_follows_the_tool_settings():
    assert skills.offered("basic", True) == []
    assert [s.name for s in skills.offered("full", False)] == [n for n in NAMES if n != "update-import-safely"]
    assert [s.name for s in skills.offered("full", True)] == NAMES


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


def test_skill_md_format():
    s = skills.BY_NAME["weekly-update-review"]
    md = skills.skill_md(s)
    head = frontmatter(md)
    assert head == {"name": "p6-weekly-update-review", "description": s.description}  # portable fields only
    assert skills.folder_name(s) == head["name"] and skills.NAME_RE.match(s.name)
    assert md.isascii() and skills.MARKER in md and "# Weekly update review" in md
    assert "- weeks: Look-ahead length in weeks (default 3)" in md and "project <project>" in md
    assert md.rstrip().endswith(skills.RULES.splitlines()[-1]) and skills.skill_files(s) == {"SKILL.md": md}
    assert skills.written_files(md) == ["SKILL.md"] and skills.written_files("no marker") == []

    mine = skills.from_config({"name": "with-refs", "title": "Refs", "description": 'Says "hi" \\ back',
                               "args": [], "template": "Read the checklist.", "enabled": True,
                               "files": [{"path": "reference/checklist.md", "text": "- item p6_lookahead"},
                                         {"path": "notes.txt", "text": "n"}]})
    md = skills.skill_md(mine)
    assert frontmatter(md)["description"] == 'Says "hi" \\ back'  # escaped as a JSON / YAML string
    assert "- reference/checklist.md\n- notes.txt" in md and mine.tools() == ["p6_lookahead"]
    assert skills.written_files(md) == ["SKILL.md", "reference/checklist.md", "notes.txt"]
    assert set(skills.skill_files(mine)) == {"SKILL.md", "reference/checklist.md", "notes.txt"}
    prompt = skills.prompt_text(mine, {})
    assert prompt.index("Reference file reference/checklist.md:\n\n- item p6_lookahead") < prompt.index(skills.RULES)


@pytest.mark.parametrize("cid,folder,legacy", [
    ("claude_code", SHARED, ("User", ".claude", "commands")),
    ("cursor", SHARED, ("User", ".cursor", "commands")),
    ("vscode", SHARED, ("Roaming", "Code", "User", "prompts")),
    ("cline", ("User", ".cline", "skills"), ("User", "Documents", "Cline", "Workflows")),
])
def test_install_update_and_remove_skills(home, cid, folder, legacy):
    ad = clients.get(cid)
    d = home.joinpath(*folder)
    assert ad.skills_dir() == str(d) and ad.skills_state() == clients.SKILLS_NONE
    mine = d / "p6-baseline-variance"  # a skill of the user's with one of our names
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: p6-baseline-variance\n---\nmy own", encoding="utf-8")
    other = d / "notes"
    other.mkdir()
    old = home.joinpath(*legacy)  # command files of P6 MCP 1.7
    old.mkdir(parents=True)
    (old / "p6-delay-whatif.md").write_text(skills.COMMAND_MARKER + "\nold", encoding="utf-8")
    (old / "p6-mine.md").write_text("my command", encoding="utf-8")
    assert ad.legacy_commands() == ["p6-delay-whatif.md"]

    res = ad.install_skills("full", True)
    assert res["skipped"] == [mine.name] and len(res["written"]) == len(NAMES) - 1 and res["folder"] == str(d)
    assert res["legacy_removed"] == ["p6-delay-whatif.md"] and os.listdir(old) == ["p6-mine.md"]
    assert (mine / "SKILL.md").read_text(encoding="utf-8").endswith("my own") and other.exists()
    md = d / "p6-weekly-update-review" / "SKILL.md"
    assert md.read_text(encoding="utf-8") == skills.skill_md(skills.BY_NAME["weekly-update-review"])
    (mine / "SKILL.md").unlink()
    mine.rmdir()
    res = ad.install_skills("full", True)
    assert res["written"] == [mine.name] and ad.skills_state("full", True) == clients.SKILLS_INSTALLED
    assert ad.install_skills("full", True)["changed"] is False

    imp = d / "p6-update-import-safely" / "SKILL.md"
    imp.write_text(imp.read_text(encoding="utf-8") + "edited", encoding="utf-8")
    assert ad.skills_state("full", True) == clients.SKILLS_OUTDATED
    res = ad.install_skills("full", False)  # writes off: the import skill is no longer offered
    assert res["removed"] == ["p6-update-import-safely"] and not imp.parent.exists()
    assert ad.skills_state("full", False) == clients.SKILLS_INSTALLED
    assert ad.skills_state("full", True) == clients.SKILLS_OUTDATED

    (d / "p6-delay-whatif" / "my-notes.md").write_text("kept", encoding="utf-8")  # a file of the user's
    res = ad.remove_skills()
    assert len(res["removed"]) == len(NAMES) - 1
    assert sorted(os.listdir(d)) == ["notes", "p6-delay-whatif"]
    assert os.listdir(d / "p6-delay-whatif") == ["my-notes.md"]


def test_reference_files_follow_the_skill(home):
    from p6mcp import config, settings_logic
    item = {"name": "refs", "title": "Refs", "description": "Reads two files. Use when testing.", "args": [],
            "template": "Read them.", "files": [{"path": "ref/a.md", "text": "A"}, {"path": "b.txt", "text": "B"}]}
    data = settings_logic.put_skill(config.read_file()[0], item)
    config.save(data)
    ad = clients.get("claude_code")
    ad.install_skills("full", True)
    folder = home / "User" / ".claude" / "skills" / "p6-refs"
    assert (folder / "ref" / "a.md").read_text(encoding="utf-8") == "A" and (folder / "b.txt").exists()
    config.save(settings_logic.put_skill(data, dict(item, files=[{"path": "b.txt", "text": "B2"}]), "refs"))
    assert ad.skills_state() == clients.SKILLS_OUTDATED
    ad.install_skills("full", True)
    assert not (folder / "ref").exists() and (folder / "b.txt").read_text(encoding="utf-8") == "B2"
    assert ad.skills_state() == clients.SKILLS_INSTALLED


@pytest.mark.parametrize("cid", ["claude_desktop", "lmstudio"])
def test_clients_without_skills_folder(home, cid):
    ad = clients.get(cid)
    assert ad.skills_state() == clients.SKILLS_UNSUPPORTED and ad.legacy_commands() == []
    with pytest.raises(ValueError):
        ad.install_skills("full", True)
    assert ad.prompts_native is (cid == "claude_desktop")


def test_scan_reports_skill_state(home):
    (home / "User" / ".cursor").mkdir()
    st = {s.client: s for s in clients.scan()}
    assert st["cursor"].skills == clients.SKILLS_NONE and st["claude_desktop"].skills == clients.SKILLS_UNSUPPORTED
    clients.get("cursor").install_skills("full", True)
    st = {s.client: s for s in clients.scan()}
    assert st["cursor"].skills == clients.SKILLS_INSTALLED == st["claude_code"].skills  # one shared folder


def test_convert_legacy_commands(home):
    old = home / "User" / ".claude" / "commands"
    old.mkdir(parents=True)
    (old / "p6-weekly-update-review.md").write_text(skills.COMMAND_MARKER, encoding="utf-8")
    cline_old = home / "User" / "Documents" / "Cline" / "Workflows"
    cline_old.mkdir(parents=True)
    (cline_old / "p6-delay-whatif.md").write_text(skills.COMMAND_MARKER, encoding="utf-8")
    done = {d["client"]: d for d in clients.convert_legacy_commands()}
    assert set(done) == {"claude_code", "cline"} and sorted(done["claude_code"]["written"]) == sorted(FOLDERS)
    assert sorted(os.listdir(home / "User" / ".cline" / "skills")) == sorted(FOLDERS)
    assert os.listdir(old) == [] and os.listdir(cline_old) == []
    assert clients.convert_legacy_commands() == []  # once converted, nothing to do


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


def test_cli_skills(home, capsys):
    (home / "User" / ".cursor").mkdir()
    assert cli(["skills", "install", "--detected"]) == 0
    assert f"cursor: {len(NAMES)} written" in capsys.readouterr().out
    assert cli(["skills", "install", "claude_desktop"]) == 1
    assert cli(["commands", "remove", "cursor"]) == 0  # the name of P6 MCP 1.4 to 1.7 still works
    assert f"cursor: {len(NAMES)} removed" in capsys.readouterr().out
