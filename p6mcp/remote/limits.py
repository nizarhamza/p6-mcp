"""Request limits and the optional IP allowlist of remote access.

Rate limits (per caller IP, as the relay or Cloudflare reports it): token buckets refilled continuously. The login
endpoints are tight (a person connects an app a few times, a client refreshes its token once an hour); /mcp is
generous (an AI app makes a few calls a second at most); /authorize/wait allows the browser page's polling (one
request every 2 s) with room to spare. The file handoff pages (/files/<token>, /upload/<token>) take a page view
and a POST per file: tight, a link guesser gets a few tries a minute against 256-bit tokens.

IP allowlist (setting remote_ip_allowlist, off by default): /mcp and the client endpoints of OAuth (/token,
/register, /revoke) accept only Anthropic's published outbound ranges and OpenAI's connector ranges
(https://openai.com/chatgpt-connectors.json, refreshed daily, the last good copy kept). The browser pages
(/authorize, /authorize/wait, the file handoff pages) and the metadata are not filtered: the user's own browser
opens them. Defence in depth only: the OAuth token is the control.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
import time
import urllib.request
from typing import Callable, Optional

from p6mcp import config

log = logging.getLogger("p6mcp.remote.limits")

# (requests per minute, burst) per caller IP
LIMITS = {"/authorize/wait": (120, 30), "/authorize": (10, 10), "/register": (10, 5), "/token": (30, 10),
          "/revoke": (30, 10), "/mcp": (600, 120),
          "/files": (30, 10), "/upload": (20, 6)}  # file handoff pages: a page view and one POST per file
# https://platform.claude.com/docs/en/api/ip-addresses (outbound, "will not change without notice"), 2026-11-25
ANTHROPIC = ("160.79.104.0/21", "153.61.208.0/20")
OPENAI_URL = "https://openai.com/chatgpt-connectors.json"
OPENAI_REFRESH = 86400
FILTERED = ("/mcp", "/token", "/register", "/revoke")


class RateLimiter:
    def __init__(self, limits: dict = LIMITS, clock: Callable[[], float] = time.monotonic):
        self.limits, self.clock = limits, clock
        self.buckets: dict[tuple[str, str], tuple[float, float]] = {}
        self.lock = threading.Lock()

    def group(self, path: str) -> Optional[str]:
        if path in self.limits:
            return path
        return next((p for p in self.limits if path.startswith(p + "/")), None)

    def allow(self, path: str, ip: Optional[str]) -> tuple[bool, int]:
        """(allowed, seconds to wait)."""
        grp = self.group(path)
        if grp is None:
            return True, 0
        per_min, burst = self.limits[grp]
        rate = per_min / 60.0
        key = (grp, ip or "?")
        now = self.clock()
        with self.lock:
            if len(self.buckets) > 10000:  # many callers: forget idle ones
                self.buckets = {k: v for k, v in self.buckets.items() if now - v[1] < 600}
            tokens, last = self.buckets.get(key, (float(burst), now))
            tokens = min(float(burst), tokens + (now - last) * rate)
            if tokens < 1:
                self.buckets[key] = (tokens, now)
                return False, max(1, int((1 - tokens) / rate) + 1)
            self.buckets[key] = (tokens - 1, now)
        return True, 0


def _networks(prefixes) -> list:
    out = []
    for p in prefixes:
        try:
            out.append(ipaddress.ip_network(p, strict=False))
        except ValueError:
            continue
    return out


def openai_cache() -> str:
    return os.path.join(config.localdata_dir(), "remote", "openai_ranges.json")


def parse_openai(doc: dict) -> list[str]:
    return [p.get("ipv4Prefix") or p.get("ipv6Prefix") for p in doc.get("prefixes", [])
            if isinstance(p, dict) and (p.get("ipv4Prefix") or p.get("ipv6Prefix"))]


class Allowlist:
    """The ranges the allowlist accepts; OpenAI's list is refreshed in the background."""

    def __init__(self, fetch: Optional[Callable[[], dict]] = None, cache: Optional[str] = None):
        self.fetch = fetch or self._fetch
        self.cache = cache or openai_cache()
        self.anthropic = _networks(ANTHROPIC)
        self.openai: list = []
        self.updated = 0.0
        self.lock = threading.Lock()
        try:
            with open(self.cache, encoding="utf-8") as fh:
                self.openai = _networks(parse_openai(json.load(fh)))
        except (OSError, ValueError):
            pass

    @staticmethod
    def _fetch() -> dict:
        req = urllib.request.Request(OPENAI_URL, headers={"user-agent": "MCP for Primavera P6"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read(2_000_000))

    def refresh(self) -> bool:
        try:
            doc = self.fetch()
            nets = _networks(parse_openai(doc))
        except Exception as e:  # keep the last good copy
            log.warning("OpenAI IP ranges not refreshed: %s", e)
            return False
        if not nets:
            return False
        with self.lock:
            self.openai, self.updated = nets, time.time()
        try:
            os.makedirs(os.path.dirname(self.cache), exist_ok=True)
            tmp = self.cache + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            os.replace(tmp, self.cache)
        except OSError:
            pass
        return True

    def refresh_if_old(self) -> None:
        if time.time() - self.updated > OPENAI_REFRESH:
            self.updated = time.time()  # one attempt per period, even when it fails
            threading.Thread(target=self.refresh, daemon=True).start()

    def allows(self, ip: Optional[str]) -> bool:
        if not ip:
            return False
        try:
            addr = ipaddress.ip_address(ip.strip())
        except ValueError:
            return False
        with self.lock:
            nets = self.anthropic + self.openai
        return any(addr in n for n in nets)
