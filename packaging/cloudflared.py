"""Fetch the cloudflared.exe bundled for remote access (3.2), pinned by version and SHA-256.

    python packaging/cloudflared.py         -> packaging/build/cloudflared/cloudflared.exe (checked, cached)

cloudflared (Apache-2.0, https://github.com/cloudflare/cloudflared) is Cloudflare's tunnel connector, signed by
Cloudflare; it is shipped unchanged in the P6MCP folder (cloudflared\\cloudflared.exe) and not re-signed. The pin
is the checksum Cloudflare publishes with the release (release notes and the asset digest of the GitHub release).
To update: change VERSION and SHA256 to a newer release's values, build, and run the remote access checks.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request

VERSION = "2026.11.2"
SHA256 = "555b46c85582461d189cac30d14ad05480d612613801be9f110b8ce08c5b2113"  # cloudflared-windows-amd64.exe
URL = f"https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/cloudflared-windows-amd64.exe"
DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build", "cloudflared", "cloudflared.exe")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(dest: str = DEST) -> str:
    if os.path.isfile(dest) and sha256(dest) == SHA256:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".part")
    os.close(fd)
    try:
        with urllib.request.urlopen(URL, timeout=120) as r, open(tmp, "wb") as out:
            shutil.copyfileobj(r, out)
        got = sha256(tmp)
        if got != SHA256:
            raise SystemExit(f"cloudflared {VERSION}: checksum mismatch (got {got}, expected {SHA256})")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return dest


if __name__ == "__main__":
    print(fetch())
    sys.exit(0)
