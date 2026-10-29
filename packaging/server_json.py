"""server.json for the official MCP Registry, as published for one release: the repository's server.json with
the release version, the URL of the release's MCP bundle and that file's SHA-256 (required for MCPB packages;
MCP clients check it before installing). The Registry workflow runs it on the published release's .mcpb.

    python packaging/server_json.py --mcpb p6mcp-2.0.0.mcpb [--repo https://github.com/OWNER/REPO] [--out FILE]

--repo defaults to the repository named in server.json (the workflow passes the repository it runs in, so a
release made before the repository rename points at the name its assets really have).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER_JSON = os.path.join(ROOT, "server.json")
MCPB = re.compile(r"p6mcp-(.+)\.mcpb")  # packaging/bundle.py names it so, with the version of the tag

sys.path.insert(0, HERE)
from bundle import semver  # noqa: E402


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(base: dict, mcpb_path: str, repo: str | None = None) -> dict:
    """base (the repository's server.json) for the bundle at mcpb_path: the tag version comes from its file
    name (PEP 440, as p6mcp.__version__), the Registry version is its semver form."""
    name = os.path.basename(mcpb_path)
    m = MCPB.fullmatch(name)
    if not m:
        raise ValueError(f"{name}: not an MCP bundle of this project (p6mcp-<version>.mcpb)")
    tag_version, version = m[1], semver(m[1])
    repo = (repo or base["repository"]["url"]).rstrip("/")
    out = json.loads(json.dumps(base))
    out["version"] = version
    out["repository"]["url"] = repo  # the repository the release really lives in (before or after the rename)
    pkgs = [p for p in out["packages"] if p.get("registryType") == "mcpb"]
    if len(pkgs) != 1:
        raise ValueError("server.json must list exactly one mcpb package")
    pkgs[0].update(identifier=f"{repo}/releases/download/v{tag_version}/{name}", version=version,
                   fileSha256=sha256(mcpb_path))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mcpb", required=True)
    ap.add_argument("--repo")
    ap.add_argument("--out", default="-")
    a = ap.parse_args(argv)
    with open(SERVER_JSON, encoding="utf-8") as fh:
        data = build(json.load(fh), a.mcpb, a.repo)
    text = json.dumps(data, indent=2) + "\n"
    if a.out == "-":
        sys.stdout.write(text)
    else:
        with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
