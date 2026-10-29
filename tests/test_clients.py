"""Client adapters on copies of sample config files in a fake user profile (never the real ones)."""

import json
import os

import pytest

from p6mcp import clients, jsonc
from p6mcp.clients import files
from p6mcp.clients.__main__ import main as cli
from p6mcp.clients.base import Launch

LN = Launch(r"C:\Program Files\P6 MCP\p6mcp-server.exe", [])
V2 = {"command": "python", "args": [r"C:\p6-mcp\p6_mcp_server.py"], "env": {"P6_DB_PATH": r"C:\x\demo.db"}}

CLAUDE = ('{\n  "globalShortcut": "Ctrl+Space",\n  "mcpServers": {\n    "primavera-p6": %s,\n'
          '    "filesystem": {\n      "command": "npx",\n      "args": ["-y", "server-filesystem"]\n    }\n'
          '  },\n  "preferences": {"menuBarEnabled": false}\n}\n') % json.dumps(V2)
VSCODE = ('{\r\n\t// my MCP servers\r\n\t"servers": {\r\n\t\t"github": {"type": "http", "url": "https://x/mcp"},'
          ' // remote\r\n\t},\r\n\t"inputs": [\r\n\t\t{"type": "promptString", "id": "tok", "password": true}\r\n'
          '\t]\r\n}\r\n')


@pytest.fixture
def home(tmp_path, monkeypatch):
    roaming, local, profile = tmp_path / "Roaming", tmp_path / "Local", tmp_path / "User"
    for d in (roaming, local, profile):
        d.mkdir()
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.delenv("CLINE_MCP_SETTINGS_PATH", raising=False)
    monkeypatch.setattr(clients, "launch", lambda: LN)
    monkeypatch.setattr("p6mcp.clients.base.launch", lambda: LN)
    monkeypatch.setattr("p6mcp.clients._running_names", lambda: set())
    return {"roaming": roaming, "local": local, "profile": profile}


def put(path, text, bom=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write((("\ufeff" if bom else "") + text).encode("utf-8"))


def raw(path):
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8")


def backups(cid):
    d = files.backup_dir(cid)
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def test_nothing_installed(home):
    assert {s.client: s.state for s in clients.scan()} == {a.id: clients.NOT_INSTALLED for a in clients.ADAPTERS}


def test_claude_desktop_register_keeps_everything_else(home):
    ad = clients.get("claude_desktop")
    path = os.path.join(home["roaming"], "Claude", "claude_desktop_config.json")
    put(path, CLAUDE)
    st = ad.status(LN)
    assert st.state == clients.NOT_REGISTERED and st.others == ["primavera-p6"] and st.path == path
    res = ad.register(LN)
    assert res["changed"] and res["backup"] and raw(res["backup"]) == CLAUDE
    data = json.loads(raw(path))
    assert data["mcpServers"]["p6mcp"] == {"command": LN.command, "args": []}
    assert data["mcpServers"]["primavera-p6"] == V2 and data["preferences"] == {"menuBarEnabled": False}
    assert raw(path).startswith(CLAUDE[:CLAUDE.index('"filesystem"')])  # text before our entry untouched
    assert ad.status(LN).state == clients.REGISTERED
    assert ad.register(LN) == {"changed": False, "path": path, "backup": None}  # idempotent, no new backup
    assert ad.unregister()["changed"] and raw(path) == CLAUDE  # back to the original bytes


def test_outdated_entry_is_repaired(home):
    ad = clients.get("cursor")
    path = os.path.join(home["profile"], ".cursor", "mcp.json")
    put(path, json.dumps({"mcpServers": {"p6mcp": {"command": r"D:\old\p6mcp-server.exe", "args": []}}}, indent=2))
    assert ad.status(LN).state == clients.OUTDATED
    ad.register(LN)
    assert ad.status(LN).state == clients.REGISTERED and len(backups("cursor")) == 1


def test_path_comparison_ignores_case_and_separators(home):
    ad = clients.get("cursor")
    path = os.path.join(home["profile"], ".cursor", "mcp.json")
    put(path, json.dumps({"mcpServers": {"p6mcp": {"command": LN.command.upper().replace("\\", "/"), "args": []}}}))
    assert ad.status(LN).state == clients.REGISTERED


def test_vscode_jsonc_comments_crlf_and_bom_kept(home):
    ad = clients.get("vscode")
    path = os.path.join(home["roaming"], "Code", "User", "mcp.json")
    put(path, VSCODE, bom=True)
    ad.register(LN)
    text = raw(path)
    assert text.startswith("\ufeff") and "// my MCP servers" in text and "// remote" in text
    assert "\n" not in text.replace("\r\n", "")
    data = jsonc.loads(text[1:])
    assert data["servers"]["p6mcp"] == {"type": "stdio", "command": LN.command, "args": []}
    assert data["inputs"][0]["id"] == "tok"
    ad.unregister()
    assert jsonc.loads(raw(path)[1:]) == jsonc.loads(VSCODE)


def test_new_file_is_created_for_an_installed_client(home):
    os.makedirs(os.path.join(home["profile"], ".lmstudio"))
    ad = clients.get("lmstudio")
    assert ad.status(LN).state == clients.NOT_REGISTERED
    res = ad.register(LN)
    assert res["backup"] is None and json.loads(raw(res["path"])) == {"mcpServers": {"p6mcp": ad.expected(LN)}}


def test_malformed_file_is_never_touched(home):
    path = os.path.join(home["profile"], ".cursor", "mcp.json")
    bad = '{"mcpServers": {"a": {"command": "x",}}'  # unterminated
    put(path, bad)
    ad = clients.get("cursor")
    st = ad.status(LN)
    assert st.state == clients.ERROR and "unterminated" in st.message
    with pytest.raises(jsonc.JsoncError):
        ad.register(LN)
    assert raw(path) == bad and backups("cursor") == []


def test_not_utf8_is_refused(home):
    path = os.path.join(home["profile"], ".cursor", "mcp.json")
    os.makedirs(os.path.dirname(path))
    with open(path, "wb") as fh:
        fh.write(b'{"mcpServers": {"caf\xe9": {}}}')
    assert clients.get("cursor").status(LN).state == clients.ERROR


def test_failed_write_leaves_the_file_and_no_temp(home, monkeypatch):
    path = os.path.join(home["profile"], ".cursor", "mcp.json")
    original = '{"mcpServers": {}}'
    put(path, original)

    def deny(*_a):
        raise PermissionError("locked")
    monkeypatch.setattr(files.os, "replace", deny)
    monkeypatch.setattr(files.time, "sleep", lambda _s: None)
    with pytest.raises(files.ClientFileError):
        clients.get("cursor").register(LN)
    assert raw(path) == original
    assert [n for n in os.listdir(os.path.dirname(path)) if n.endswith(".tmp")] == []


def test_legacy_entry_removal(home):
    ad = clients.get("claude_desktop")
    path = os.path.join(home["roaming"], "Claude", "claude_desktop_config.json")
    put(path, CLAUDE)
    with pytest.raises(ValueError, match="not a MCP for Primavera P6 entry"):
        ad.unregister("filesystem")
    ad.unregister("primavera-p6")
    data = json.loads(raw(path))
    assert "primavera-p6" not in data["mcpServers"] and "filesystem" in data["mcpServers"]


def test_store_install_uses_the_virtualised_file(home):
    pkg = os.path.join(home["local"], "Packages", "Claude_abc123", "LocalCache", "Roaming", "Claude")
    put(os.path.join(pkg, "claude_desktop_config.json"), "{}")
    ad = clients.get("claude_desktop")
    assert ad.installed() and ad.config_path().startswith(pkg)


def test_cline_paths(home, monkeypatch, tmp_path):
    ad = clients.get("cline")
    legacy = os.path.join(home["roaming"], "Code", "User", "globalStorage", "saoudrizwan.claude-dev", "settings",
                          "cline_mcp_settings.json")
    assert not ad.installed()
    put(legacy, '{"mcpServers": {}}')
    assert ad.installed() and ad.config_path() == legacy
    os.makedirs(os.path.join(home["profile"], ".cline"))
    assert ad.config_path() == os.path.join(home["profile"], ".cline", "data", "settings", "cline_mcp_settings.json")
    custom = str(tmp_path / "cline.json")
    monkeypatch.setenv("CLINE_MCP_SETTINGS_PATH", custom)
    assert ad.config_path() == custom
    res = ad.register(LN)
    assert json.loads(raw(res["path"]))["mcpServers"]["p6mcp"] == {"command": LN.command, "args": [],
                                                                    "disabled": False, "autoApprove": []}


def test_backup_retention(home, monkeypatch):
    monkeypatch.setattr(files, "KEEP_BACKUPS", 2)
    path = os.path.join(home["profile"], ".cursor", "mcp.json")
    put(path, "{}")
    ad = clients.get("cursor")
    for i in range(4):
        ad.register(Launch(f"C:\\v{i}\\p6mcp-server.exe"))
    assert len(backups("cursor")) == 2


def test_cli(home, capsys):
    os.makedirs(os.path.join(home["profile"], ".cursor"))
    os.makedirs(os.path.join(home["profile"], ".lmstudio"))
    assert cli(["register", "--detected"]) == 0
    out = capsys.readouterr().out
    assert "cursor: updated" in out and "lmstudio: updated" in out and "Restart" in out
    assert cli(["list", "--json"]) == 0
    states = {s["client"]: s["state"] for s in json.loads(capsys.readouterr().out)}
    assert states["cursor"] == states["lmstudio"] == "registered" and states["vscode"] == "not_installed"
    assert cli(["unregister", "cursor"]) == 0
    assert cli(["register", "nosuch"]) == 1
    assert cli(["snippet", "vscode"]) == 0 and '"servers"' in capsys.readouterr().out


def test_snippets(home):
    s = json.loads(clients.snippet("stdio", ln=LN))
    assert s == {"mcpServers": {"p6mcp": {"command": LN.command, "args": []}}}
    h = json.loads(clients.snippet("http", port=40000, token="abc"))["mcpServers"]["p6mcp"]
    assert h == {"url": "http://127.0.0.1:40000/mcp", "headers": {"Authorization": "Bearer abc"}}
    assert "<access token>" in clients.snippet("http")
    with pytest.raises(ValueError):
        clients.snippet("ftp")


def test_launch_uses_console_python(monkeypatch):
    from p6mcp.clients import base
    monkeypatch.setattr(base.sys, "executable", r"C:\py\pythonw.exe")
    assert base.launch().command == r"C:\py\python.exe" and base.launch().args == ["-m", "p6mcp"]
    monkeypatch.setattr(base.sys, "frozen", True, raising=False)
    monkeypatch.setattr(base.sys, "executable", r"C:\Program Files\P6 MCP\p6mcp-settings.exe")
    assert base.launch().command == r"C:\Program Files\P6 MCP\p6mcp-server.exe"


HTTP = clients.HttpTarget("http://127.0.0.1:40000/mcp", "tok-123")


def test_http_entries(home):
    os.makedirs(os.path.join(home["profile"], ".cursor"))
    put(os.path.join(home["roaming"], "Claude", "claude_desktop_config.json"), "{}")
    cursor, claude = clients.get("cursor"), clients.get("claude_desktop")
    cursor.register(LN, HTTP)
    claude.register(LN, HTTP)
    c = json.loads(raw(cursor.config_path()))["mcpServers"]["p6mcp"]
    assert c == {"url": HTTP.url, "headers": {"Authorization": "Bearer tok-123"}}
    assert json.loads(raw(claude.config_path()))["mcpServers"]["p6mcp"] == {"command": LN.command, "args": []}
    assert cursor.status(LN, http=HTTP).state == clients.REGISTERED
    assert claude.status(LN, http=HTTP).transport == "stdio"
    new_token = clients.HttpTarget(HTTP.url, "tok-456")
    assert cursor.status(LN, http=new_token).state == clients.OUTDATED  # regenerated token: repair
    assert cursor.status(LN).state == clients.OUTDATED  # back to stdio: repair


def test_vscode_http_uses_a_password_prompt(home):
    path = os.path.join(home["roaming"], "Code", "User", "mcp.json")
    put(path, VSCODE)
    ad = clients.get("vscode")
    ad.register(LN, HTTP)
    data = jsonc.loads(raw(path))
    assert data["servers"]["p6mcp"] == {"type": "http", "url": HTTP.url,
                                        "headers": {"Authorization": "Bearer ${input:p6mcp-token}"}}
    assert [i["id"] for i in data["inputs"]] == ["tok", "p6mcp-token"] and "tok-123" not in raw(path)
    ad.register(LN, HTTP)  # idempotent: one prompt only
    assert len(jsonc.loads(raw(path))["inputs"]) == 2
    ad.register(LN)  # back to stdio: the prompt goes
    assert [i["id"] for i in jsonc.loads(raw(path))["inputs"]] == ["tok"]
    ad.register(LN, HTTP)
    ad.unregister()
    assert jsonc.loads(raw(path)) == jsonc.loads(VSCODE) and "// my MCP servers" in raw(path)


def test_cline_http_entry(home, monkeypatch, tmp_path):
    monkeypatch.setenv("CLINE_MCP_SETTINGS_PATH", str(tmp_path / "cline.json"))
    ad = clients.get("cline")
    ad.register(LN, HTTP)
    e = json.loads(raw(ad.config_path()))["mcpServers"]["p6mcp"]
    assert e["type"] == "streamableHttp" and e["url"] == HTTP.url and e["disabled"] is False
