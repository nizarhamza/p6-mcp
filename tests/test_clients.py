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


# -- Devin (Windsurf), Zed, Gemini CLI: JSON files; Continue: a block file of its own; Codex: TOML

JSON_CLIENTS = {  # id: (folder that marks the install, config file, servers key)
    "devin": (("roaming", "devin"), ("roaming", "devin", "mcp_config.json"), "mcpServers"),
    "zed": (("roaming", "Zed"), ("roaming", "Zed", "settings.json"), "context_servers"),
    "gemini": (("profile", ".gemini"), ("profile", ".gemini", "settings.json"), "mcpServers"),
}
OTHERS = ('{\n  // my settings\n  "theme": "One Dark",\n  "%s": {\n'
          '    "github": {"command": "npx", "args": ["-y", "gh"]} // remote\n  },\n  "telemetry": false\n}\n')


def at(home, parts):
    return os.path.join(home[parts[0]], *parts[1:])


@pytest.mark.parametrize("cid", JSON_CLIENTS)
def test_json_clients_register_update_unregister(home, cid):
    marker, cfg, key = JSON_CLIENTS[cid]
    ad, path = clients.get(cid), at(home, cfg)
    assert ad.status(LN).state == clients.NOT_INSTALLED and ad.config_path() == path
    os.makedirs(at(home, marker))
    assert ad.installed() and ad.status(LN).state == clients.NOT_REGISTERED
    res = ad.register(LN)  # missing file: created
    assert res["changed"] and res["backup"] is None
    assert json.loads(raw(path)) == {key: {"p6mcp": {"command": LN.command, "args": []}}}
    assert ad.status(LN).state == clients.REGISTERED
    assert ad.unregister()["changed"] and json.loads(raw(path)) == {key: {}}

    original = OTHERS % key  # other servers and comments: kept byte for byte outside our entry
    put(path, original)
    res = ad.register(LN)
    assert raw(res["backup"]) == original
    text = raw(path)
    assert text.startswith(original[:original.index("} // remote") + 1])
    assert text.endswith('\n  },\n  "telemetry": false\n}\n') and "// my settings" in text and "// remote" in text
    data = jsonc.loads(text)
    assert data[key]["github"] == {"command": "npx", "args": ["-y", "gh"]} and data["theme"] == "One Dark"
    assert ad.status(LN).state == clients.REGISTERED
    assert ad.register(LN)["changed"] is False

    new = Launch(r"D:\P6 MCP 2\p6mcp-server.exe", [])  # moved installation: outdated, then repaired
    assert ad.status(new).state == clients.OUTDATED
    ad.register(new)
    assert ad.status(new).state == clients.REGISTERED and jsonc.loads(raw(path))[key]["p6mcp"]["command"] == new.command
    assert ad.unregister()["changed"] and raw(path) == original
    assert len(backups(cid)) >= 3

    bad = '{"%s": {"a": {"command": "x"' % key
    put(path, bad)
    before = backups(cid)
    assert ad.status(LN).state == clients.ERROR
    with pytest.raises(jsonc.JsoncError):
        ad.register(LN)
    assert raw(path) == bad and backups(cid) == before


def test_new_clients_http_entries(home):
    for parts in (("roaming", "devin"), ("roaming", "Zed"), ("profile", ".gemini")):
        os.makedirs(at(home, parts))
    bearer = {"Authorization": "Bearer tok-123"}
    expected = {"devin": {"url": HTTP.url, "headers": bearer}, "zed": {"url": HTTP.url, "headers": bearer},
                "gemini": {"httpUrl": HTTP.url, "headers": bearer}}  # Gemini CLI: url alone would mean SSE
    for cid, entry in expected.items():
        ad = clients.get(cid)
        ad.register(LN, HTTP)
        assert jsonc.loads(raw(ad.config_path()))[ad.container[0]]["p6mcp"] == entry
        assert ad.status(LN, http=HTTP).state == clients.REGISTERED and ad.token_in_file


def test_devin_detection(home):
    ad = clients.get("devin")
    assert not ad.installed()
    put(os.path.join(home["local"], "devin", "bin", "devin.exe"), "")  # the Devin CLI alone
    assert ad.installed() and ad.config_path() == os.path.join(home["roaming"], "devin", "mcp_config.json")


def test_zed_settings_keep_comments_and_crlf(home):
    path = os.path.join(home["roaming"], "Zed", "settings.json")
    original = ('// Zed settings\r\n//\r\n// For information on how to configure Zed, see the docs\r\n{\r\n'
                '  "ui_font_size": 16, // bigger\r\n  "buffer_font_size": 15,\r\n}\r\n')
    put(path, original)
    ad = clients.get("zed")
    ad.register(LN)
    text = raw(path)
    assert text.startswith('// Zed settings\r\n//\r\n') and "// bigger" in text and "\n" not in text.replace("\r\n", "")
    assert jsonc.loads(text)["context_servers"] == {"p6mcp": {"command": LN.command, "args": []}}
    ad.unregister()
    assert jsonc.loads(raw(path)) == {"ui_font_size": 16, "buffer_font_size": 15, "context_servers": {}}


CONTINUE_YAML = "name: Mine\nversion: 1.0.0\nschema: v1\nmcpServers:\n  - name: p6mcp\n    command: x.exe\n"


def test_continue_block_file(home, monkeypatch, tmp_path):
    ad = clients.get("continue")
    folder = os.path.join(home["profile"], ".continue")
    path = os.path.join(folder, "mcpServers", "p6mcp.yaml")
    assert not ad.installed() and ad.config_path() == path
    put(os.path.join(folder, "config.yaml"), "name: Local Config\nversion: 1.0.0\nschema: v1\n")
    assert ad.status(LN).state == clients.NOT_REGISTERED
    res = ad.register(LN)
    assert res["backup"] is None
    data = json.loads(raw(path))  # JSON is valid YAML: Continue reads it as a block
    assert data == {"name": "MCP for Primavera P6", "version": "1.0.0", "schema": "v1",
                    "mcpServers": [{"name": "p6mcp", "command": LN.command, "args": []}]}
    assert ad.status(LN).state == clients.REGISTERED and ad.register(LN)["changed"] is False
    assert raw(os.path.join(folder, "config.yaml")).startswith("name: Local Config")  # the user's config untouched

    new = Launch(r"D:\P6 MCP 2\p6mcp-server.exe", [])
    assert ad.status(new).state == clients.OUTDATED
    res = ad.register(new)
    assert res["backup"] and json.loads(raw(path))["mcpServers"] == [{"name": "p6mcp", "command": new.command,
                                                                      "args": []}]
    ad.register(LN, HTTP)
    assert json.loads(raw(path))["mcpServers"][0] == {
        "name": "p6mcp", "type": "streamable-http", "url": HTTP.url,
        "requestOptions": {"headers": {"Authorization": "Bearer tok-123"}}}
    assert ad.status(LN, http=HTTP).state == clients.REGISTERED

    res = ad.unregister()  # the file only held our server: deleted, after a backup
    assert res["changed"] and res["backup"] and not os.path.exists(path)
    assert ad.unregister()["changed"] is False

    ad.register(LN)  # a server the user added to our file is kept
    text = raw(path)
    mine = {"name": "other", "command": "npx", "args": ["other"]}
    put(path, jsonc.append_item(text, ["mcpServers"], mine))
    ad.unregister()
    assert json.loads(raw(path))["mcpServers"] == [mine]
    ad.register(LN)
    assert [s["name"] for s in json.loads(raw(path))["mcpServers"]] == ["other", "p6mcp"]

    put(path, CONTINUE_YAML)  # rewritten as YAML: refused, never touched
    before = backups("continue")
    st = ad.status(LN)
    assert st.state == clients.ERROR and "not written by MCP for Primavera P6" in st.message
    with pytest.raises(jsonc.JsoncError):
        ad.register(LN)
    with pytest.raises(jsonc.JsoncError):
        ad.unregister()
    assert raw(path) == CONTINUE_YAML and backups("continue") == before

    monkeypatch.setenv("CONTINUE_GLOBAL_DIR", str(tmp_path / "cont"))
    assert ad.config_path() == str(tmp_path / "cont" / "mcpServers" / "p6mcp.yaml") and not ad.installed()


def test_continue_detected_from_the_extension(home):
    os.makedirs(os.path.join(home["profile"], ".vscode", "extensions", "continue.continue-1.2.3-win32-x64"))
    assert clients.get("continue").installed()


CODEX = '''# Codex settings
model = "gpt-5" # my model
approval_policy = "on-request"

[mcp_servers.github]
command = "npx"   # GitHub
args = ["-y", "@modelcontextprotocol/server-github"]

[mcp_servers.github.env]
GITHUB_TOKEN = "abc"

# my profiles
[profiles.fast]
model = "o4-mini"
'''


def codex_entry(ln=LN):
    return {"command": ln.command, "args": list(ln.args), "startup_timeout_sec": 30, "tool_timeout_sec": 600}


def test_codex_toml(home, monkeypatch, tmp_path):
    import tomllib
    ad = clients.get("codex")
    folder = os.path.join(home["profile"], ".codex")
    path = os.path.join(folder, "config.toml")
    assert not ad.installed() and ad.config_path() == path
    os.makedirs(folder)
    assert ad.status(LN).state == clients.NOT_REGISTERED
    res = ad.register(LN)  # missing file
    assert res["backup"] is None
    assert raw(path) == ("[mcp_servers.p6mcp]\ncommand = 'C:\\Program Files\\P6 MCP\\p6mcp-server.exe'\nargs = []\n"
                         "startup_timeout_sec = 30\ntool_timeout_sec = 600\n")
    assert ad.status(LN).state == clients.REGISTERED
    assert ad.unregister()["changed"] and raw(path) == ""

    put(path, CODEX)  # comments and other tables kept byte for byte
    res = ad.register(LN)
    assert raw(res["backup"]) == CODEX
    text = raw(path)
    assert text.startswith(CODEX + "\n[mcp_servers.p6mcp]\n")
    data = tomllib.loads(text)
    assert data["mcp_servers"]["p6mcp"] == codex_entry() and data["mcp_servers"]["github"]["env"] == {
        "GITHUB_TOKEN": "abc"} and data["profiles"] == {"fast": {"model": "o4-mini"}}
    assert ad.status(LN).state == clients.REGISTERED and ad.register(LN)["changed"] is False
    assert ad.unregister()["changed"] and raw(path) == CODEX

    # an existing entry (written by hand, with an env subtable elsewhere) is replaced where it stands
    by_hand = CODEX.replace("# my profiles\n", "[mcp_servers.p6mcp]\ncommand = 'D:\\old\\p6mcp-server.exe' # old\n"
                            "args = []\n\n# my profiles\n") + "\n[mcp_servers.p6mcp.env]\nP6_USER = \"me\"\n"
    put(path, by_hand)
    assert ad.status(LN).state == clients.OUTDATED
    ad.register(LN)
    text = raw(path)
    head = by_hand[:by_hand.index("[mcp_servers.p6mcp]")]
    assert text.startswith(head + "[mcp_servers.p6mcp]\ncommand = 'C:\\Program Files")
    assert text.endswith("tool_timeout_sec = 600\n\n# my profiles\n[profiles.fast]\nmodel = \"o4-mini\"\n")
    assert tomllib.loads(text)["mcp_servers"]["p6mcp"] == codex_entry() and ad.status(LN).state == clients.REGISTERED

    ad.register(LN, HTTP)
    assert tomllib.loads(raw(path))["mcp_servers"]["p6mcp"] == {
        "url": HTTP.url, "http_headers": {"Authorization": "Bearer tok-123"}, "startup_timeout_sec": 30,
        "tool_timeout_sec": 600}
    assert ad.status(LN, http=HTTP).state == clients.REGISTERED

    # a broken file, then an entry written as an inline table: refused, never touched
    for bad, state in (('model = "gpt-5\n', clients.ERROR), ('[mcp_servers]\np6mcp = { command = "x" }\n',
                                                            clients.OUTDATED)):
        put(path, bad)
        before = backups("codex")
        assert ad.status(LN).state == state
        with pytest.raises(jsonc.JsoncError):
            ad.register(LN)
        with pytest.raises(jsonc.JsoncError):
            ad.unregister()
        assert raw(path) == bad and backups("codex") == before

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    assert ad.config_path() == str(tmp_path / "codex-home" / "config.toml") and not ad.installed()


def test_codex_cli_and_v2_entry(home, capsys):
    folder = os.path.join(home["profile"], ".codex")
    put(os.path.join(folder, "config.toml"), '[mcp_servers.primavera-p6]\ncommand = "python"\n'
                                             'args = ["C:\\\\p6-mcp\\\\p6_mcp_server.py"]\n')
    ad = clients.get("codex")
    assert ad.status(LN).others == ["primavera-p6"]
    assert cli(["register", "codex"]) == 0 and "codex: updated" in capsys.readouterr().out
    ad.unregister("primavera-p6")
    assert raw(ad.config_path()).startswith("[mcp_servers.p6mcp]\n")
    assert cli(["unregister", "codex"]) == 0 and raw(ad.config_path()) == ""
