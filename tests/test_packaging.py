"""Release helpers: bundle manifest, icon file, installer commands (no build needed)."""

import importlib.util
import os
import struct
import sys

import pytest

from p6mcp import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "packaging", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_semver_and_manifest():
    b = load("bundle")
    assert b.semver("3.0.0") == "3.0.0" and b.semver("3.0.0.dev1") == "3.0.0-dev.1" and b.semver("3.1.0rc2") == \
        "3.1.0-rc.2"
    with pytest.raises(ValueError):
        b.semver("3.0")
    m = b.manifest("3.0.0")
    assert m["manifest_version"] == "0.3" and m["server"]["type"] == "binary"
    assert m["server"]["mcp_config"]["command"] == "${__dirname}/server/p6mcp-server.exe"
    assert m["compatibility"]["platforms"] == ["win32"] and "Oracle" in m["description"]
    assert set(m["user_config"]) == {"database", "tool_profile", "allow_writes"}


def test_core_skills_are_packaged_and_built_from_a_catalog(tmp_path):
    """The core set shipped for offline first starts: package data, PyInstaller datas (both exes), and
    tools/bundle_core_skills.py, which copies the catalog's bytes and refuses a file the index does not match."""
    import hashlib
    import json
    from p6mcp import core_skills, shop
    spec = open(os.path.join(ROOT, "packaging", "p6mcp.spec"), encoding="utf-8").read()
    assert '"p6mcp", "data", "core_skills", "*.json"' in spec
    assert '"data/core_skills/*.json"' in open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
    assert "p6mcp/data/core_skills/*.json -text" in open(os.path.join(ROOT, ".gitattributes"), encoding="utf-8").read()

    cat, entries = tmp_path / "catalog", []
    for name in core_skills.names():  # a catalog checkout holding the bundled skills, one flagged core
        raw = open(os.path.join(core_skills.BUNDLE_DIR, name + ".json"), "rb").read()
        (cat / "skills" / name).mkdir(parents=True)
        (cat / "skills" / name / shop.META).write_bytes(raw)
        entries.append({"name": name, "sha256": hashlib.sha256(raw).hexdigest(),
                        "core": name == core_skills.names()[0]})
    (cat / shop.INDEX).write_text(json.dumps({"schema": 1, "skills": entries}), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("bundle_core_skills", os.path.join(ROOT, "tools",
                                                                                      "bundle_core_skills.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    out = tmp_path / "out"
    out.mkdir()
    (out / "stale.json").write_text("{}", encoding="utf-8")
    head = tool.build(str(cat), dest=str(out))  # the entries flagged core
    assert [r["name"] for r in head["skills"]] == core_skills.names()[:1] and not (out / "stale.json").exists()
    head = tool.build(str(cat), core_skills.names(), dest=str(out))
    assert head["skills"] == core_skills.bundle()["skills"] and head["catalog"] == shop.REPO
    for name in core_skills.names():
        assert (out / f"{name}.json").read_bytes() == (cat / "skills" / name / shop.META).read_bytes()
    first = cat / "skills" / core_skills.names()[0] / shop.META
    first.write_bytes(first.read_bytes().replace(b"\n", b"\r\n"))  # a CRLF checkout: not the bytes GitHub serves
    with pytest.raises(SystemExit, match="does not match the index sha256"):
        tool.build(str(cat), core_skills.names(), dest=str(out))


def test_unresolved_bundle_options_mean_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("P6MCP_CONFIG", str(tmp_path / "none.json"))
    monkeypatch.setenv("P6_DB_PATH", "${user_config.database}")
    monkeypatch.setenv("P6MCP_TOOL_PROFILE", "${user_config.tool_profile}")
    monkeypatch.setenv("P6MCP_ALLOW_WRITES", "${user_config.allow_writes}")
    st = config.load()
    assert st.db_path == "" and st.db_problem == config.NO_DB_MESSAGE and st.tool_profile == "basic"
    assert st.allow_writes is False


def test_brand_icon_file():
    data = open(os.path.join(ROOT, "assets", "brand", "ico", "p6mcp.ico"), "rb").read()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    widths = {data[6 + 16 * i] for i in range(count)}
    assert (reserved, kind) == (0, 1) and {16, 32, 48, 0} <= widths  # 0 means 256 in an ICO directory


def test_setup_cli(monkeypatch, tmp_path, capsys):
    from p6mcp import setup_cli, startup
    for var, sub in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local"), ("USERPROFILE", "User")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setenv("P6MCP_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("CLINE_MCP_SETTINGS_PATH", raising=False)
    monkeypatch.setattr("p6mcp.clients._running_names", lambda: set())
    (tmp_path / "User" / ".cursor").mkdir()
    assert setup_cli.main(["detected-clients"]) == 0
    assert capsys.readouterr().out.split() == ["cursor"]
    calls = []
    monkeypatch.setattr(startup, "sync", lambda data: calls.append(data["run_at_startup"]) or "cmd")
    assert setup_cli.main(["startup", "on"]) == 0 and config.read_file()[0]["run_at_startup"] is True
    assert setup_cli.main(["startup", "off"]) == 0 and calls == [True, False]
    (tmp_path / "config.json").write_text("{broken", encoding="utf-8")
    assert setup_cli.main(["startup", "on"]) == 1 and (tmp_path / "config.json").read_text() == "{broken"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows")
def test_installer_script_is_consistent():
    text = open(os.path.join(ROOT, "packaging", "installer.iss"), encoding="utf-8").read()
    from p6mcp.clients import BY_ID
    for cid in BY_ID:  # every adapter is offered by the installer
        assert f"clients register {cid}" in text and f"ClientDetected('{cid}')" in text
    from p6mcp.settings_app.tray import MUTEX
    assert f"AppMutex={MUTEX}" in text and "PrivilegesRequired=lowest" in text
    from p6mcp.runtime import SETUP_MUTEX
    assert f"SetupMutex={SETUP_MUTEX}" in text
