"""Update check: the only network call P6 MCP makes, on demand or at Settings launch when the user
keeps it on (config update_check). It reads the latest published GitHub release; it never downloads
or installs anything: the Settings window only opens the release page."""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Optional

from p6mcp import __version__

REPO = "nizarhamza/p6-mcp"
REPO_URL = f"https://github.com/{REPO}"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
TIMEOUT = 8


def parse(v: str) -> tuple:
    """'v1.2.3', '1.2.3', '1.2.3-rc.1', '1.2.3rc1', '1.2.3.dev1' -> sortable tuple (a pre-release sorts first)."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)(?:[.-]?(dev|a|alpha|b|beta|rc)\.?(\d+))?", v.strip())
    if not m:
        raise ValueError(f"not a version: {v!r}")
    rank = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "rc": 3}.get(m[4] or "", 4)
    return int(m[1]), int(m[2]), int(m[3]), rank, int(m[5] or 0)


def check(current: str = __version__, opener=urllib.request.urlopen) -> dict:
    """{current, latest, newer, url, error}. Never raises."""
    out = {"current": current, "latest": None, "newer": False, "url": f"{REPO_URL}/releases", "error": None}
    try:
        req = urllib.request.Request(LATEST_API, headers={"Accept": "application/vnd.github+json",
                                                          "User-Agent": f"p6mcp/{current}"})
        with opener(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag: Optional[str] = data.get("tag_name")
        out["latest"] = tag.lstrip("v") if tag else None
        out["url"] = data.get("html_url") or out["url"]
        out["newer"] = bool(tag) and parse(tag) > parse(current)
    except Exception as e:  # offline, rate limited, no release yet (404)...
        out["error"] = f"{type(e).__name__}: {e}"
    return out
