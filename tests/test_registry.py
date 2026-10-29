"""Official MCP Registry listing (server.json), the README tool tables and the landing site's generated blocks."""

import importlib.util
import json
import os
import subprocess
import sys

import pytest

from p6mcp import __version__, updates

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(rel, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def server_json():
    with open(os.path.join(ROOT, "server.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_server_json_matches_the_release():
    s, bundle = server_json(), load("packaging/bundle.py", "bundle")
    slug = updates.REPO.split("/")[1]
    assert s["name"] == f"io.github.{updates.REPO.split('/')[0]}/{slug}"
    assert s["version"] == bundle.semver(__version__)
    assert s["repository"] == {"url": updates.REPO_URL, "source": "github"}
    assert s["title"] == bundle.manifest(__version__)["display_name"]
    assert 1 <= len(s["description"]) <= 100 and "—" not in s["description"]
    assert s["websiteUrl"] == bundle.SITE and all(i["src"].startswith("https://") for i in s["icons"])
    (pkg,) = s["packages"]
    assert pkg["registryType"] == "mcpb" and pkg["transport"] == {"type": "stdio"}
    assert pkg["identifier"] == f"{updates.REPO_URL}/releases/download/v{__version__}/p6mcp-{__version__}.mcpb"
    assert "mcp" in pkg["identifier"]  # the Registry refuses an MCPB URL without "mcp"
    assert "fileSha256" not in pkg  # added from the real file at publication (packaging/server_json.py)


def test_readme_names_the_server_for_the_registry():
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        first = fh.readline().strip()
    assert first == f"<!-- mcp-name: {server_json()['name']} -->"


def test_published_server_json_carries_the_bundle_hash(tmp_path):
    sj = load("packaging/server_json.py", "server_json")
    mcpb = tmp_path / "p6mcp-2.1.0rc1.mcpb"
    mcpb.write_bytes(b"bundle bytes")
    out = sj.build(server_json(), str(mcpb), "https://github.com/nizarhamza/p6-mcp/")
    (pkg,) = out["packages"]
    assert out["version"] == pkg["version"] == "2.1.0-rc.1"
    assert pkg["identifier"] == "https://github.com/nizarhamza/p6-mcp/releases/download/v2.1.0rc1/p6mcp-2.1.0rc1.mcpb"
    assert pkg["fileSha256"] == sj.sha256(str(mcpb)) and len(pkg["fileSha256"]) == 64
    assert out["repository"] == {"url": "https://github.com/nizarhamza/p6-mcp", "source": "github"}
    assert out["name"] == server_json()["name"]  # the Registry name never follows the repository URL
    with pytest.raises(ValueError, match="not an MCP bundle"):
        sj.build(server_json(), str(tmp_path / "other.zip"))


def test_generated_tool_tables_are_current():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "gen_docs.py"), "--check"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
