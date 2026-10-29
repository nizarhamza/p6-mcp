"""Release artifacts besides the installer, from the PyInstaller output in packaging/dist:

- P6MCP-<version>-portable.zip   the P6MCP folder (Settings app and server), no installation
- p6mcp-<version>.mcpb           MCP bundle for one-click install in Claude Desktop: the server alone
                                 (manifest 0.3, type binary, Windows); the database and the write
                                 permission are chosen in Claude's extension settings (user_config)
- SHA256SUMS.txt                 checksums of every artifact in packaging/dist

    python packaging/bundle.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
BUILD = os.path.join(HERE, "build")
ROOT = os.path.dirname(HERE)
LEGAL = ("LICENSE", "NOTICE")
REPO = os.environ.get("P6MCP_REPO_URL", "https://github.com/nizarhamza/p6-mcp")
SITE = "https://mcp-for-primavera-p6.pages.dev/"


def semver(v: str) -> str:
    """PEP 440 -> semver: 3.0.0.dev1 -> 3.0.0-dev.1, 3.0.0rc2 -> 3.0.0-rc.2."""
    m = re.fullmatch(r"(\d+\.\d+\.\d+)(?:\.?(dev|rc|a|b)(\d+))?", v)
    if not m:
        raise ValueError(f"version {v!r} is not X.Y.Z[.devN|rcN]")
    return m[1] + (f"-{m[2]}.{m[3]}" if m[2] else "")


def zip_dir(folder: str, dest: str, prefix: str = "") -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, _dirs, files in os.walk(folder):
            for f in sorted(files):
                full = os.path.join(root, f)
                z.write(full, os.path.join(prefix, os.path.relpath(full, folder)))


def manifest(version: str) -> dict:
    exe = "server/p6mcp-server.exe"
    return {
        "manifest_version": "0.3",
        "name": "p6mcp",
        "display_name": "MCP for Primavera P6",
        "version": semver(version),
        "description": "Analyse and edit Primavera P6 Professional schedules (SQLite database) on this PC: critical "
                       "path, driving path, float, DCMA 14-point check, look-ahead, comparisons, CPM what-if. Not "
                       "affiliated with Oracle.",
        "long_description": "Runs locally. Writes are off by default; when you allow them (full profile), each one "
                            "is previewed, applied after your confirmation, backed up first and logged. The data the "
                            "tools return goes to the model Claude uses. For the full product (Settings window, "
                            "XER-only mode, other MCP clients) use the installer.",
        "author": {"name": "Nizar Hamza", "url": REPO},
        "repository": {"type": "git", "url": REPO},
        "homepage": SITE,
        "license": "Apache-2.0",
        "icon": "icon.png",
        "server": {
            "type": "binary",
            "entry_point": exe,
            "mcp_config": {
                "command": "${__dirname}/" + exe,
                "args": [],
                "env": {"P6_DB_PATH": "${user_config.database}",
                        "P6MCP_TOOL_PROFILE": "${user_config.tool_profile}",
                        "P6MCP_ALLOW_WRITES": "${user_config.allow_writes}"},
            },
        },
        "user_config": {
            "database": {"type": "file", "title": "P6 Professional database (.db)",
                         "description": "The SQLite database of P6 Professional standalone. Nothing is read until "
                                        "you choose one.", "required": False},
            "tool_profile": {"type": "string", "title": "Tool profile (basic or full)",
                             "description": "basic: a small read-only set; full: every tool.", "default": "basic",
                             "required": False},
            "allow_writes": {"type": "boolean", "title": "Allow write operations",
                             "description": "Off by default. Writes need the full profile, a preview and your "
                                            "confirmation; a backup is taken first.", "default": False,
                             "required": False},
        },
        "compatibility": {"platforms": ["win32"]},
        "keywords": ["primavera", "primavera p6", "p6", "schedule", "planning", "critical path", "dcma", "xer",
                     "construction"],
    }


def main() -> None:
    v = open(os.path.join(BUILD, "version.txt"), encoding="utf-8").read().strip()
    full, server = os.path.join(DIST, "P6MCP"), os.path.join(DIST, "P6MCP-server")
    for need in (full, server, os.path.join(BUILD, "icon.png")):
        if not os.path.exists(need):
            sys.exit(f"missing {need}: run make_assets.py and pyinstaller first")
    portable = os.path.join(DIST, f"P6MCP-{v}-portable.zip")
    zip_dir(full, portable, "P6MCP")
    with zipfile.ZipFile(portable, "a", zipfile.ZIP_DEFLATED) as z:
        for f in LEGAL:
            z.write(os.path.join(ROOT, f), f"P6MCP/{f}.txt")
    mcpb = os.path.join(DIST, f"p6mcp-{v}.mcpb")
    with zipfile.ZipFile(mcpb, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("manifest.json", json.dumps(manifest(v), indent=2))
        z.write(os.path.join(BUILD, "icon.png"), "icon.png")
        for f in LEGAL:
            z.write(os.path.join(ROOT, f), f)
        for root, _dirs, files in os.walk(server):
            for f in sorted(files):
                full_path = os.path.join(root, f)
                z.write(full_path, os.path.join("server", os.path.relpath(full_path, server)))
    arts = sorted(f for f in os.listdir(DIST) if f.endswith((".exe", ".zip", ".mcpb")))
    with open(os.path.join(DIST, "SHA256SUMS.txt"), "w", encoding="utf-8", newline="\n") as fh:
        for f in arts:
            h = hashlib.sha256()
            with open(os.path.join(DIST, f), "rb") as src:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    h.update(chunk)
            fh.write(f"{h.hexdigest()}  {f}\n")
    print("\n".join(arts + ["SHA256SUMS.txt"]))


if __name__ == "__main__":
    main()
