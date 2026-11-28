"""Proxy of the REST calls to a P6 EPPM server (owner's choice for 3.4.0: the Windows proxy plus an override).

A server profile chooses with its `proxy` setting:
- system (the default, also when the key is absent): the proxy Windows uses (Internet Settings, read by
  urllib.request.getproxies) or the HTTP_PROXY / HTTPS_PROXY environment variables, which win when one is set, with
  their bypass list (ProxyOverride, or NO_PROXY);
- none: a direct connection;
- custom: `proxy_url` (http://host:port), for this server only, whatever Windows says.
A loopback host (localhost, 127.0.0.1, ::1) is never sent through a proxy. Basic proxy sign-in: `proxy_user` in the
profile and its password in the Windows Credential Manager (credentials.eppm_proxy_key); a proxy URL that carries a
user or a password is refused by the configuration. A proxy setup script (PAC, "Use setup script" in Windows) is not
run: `resolve` reports its address so the user can enter the proxy in the profile.

The SQL route has no HTTP part (SQL Server and Oracle protocols over their own ports): it is never proxied.
"""

from __future__ import annotations

import base64
import ipaddress
import urllib.request
from typing import Callable, Optional
from urllib.parse import urlsplit

from p6mcp.messages import _t

MODES = ("system", "none", "custom")
KEYS = ("proxy", "proxy_url", "proxy_user")  # profile settings (the password is in the Credential Manager)


def check_url(url: object) -> Optional[str]:
    """Why a proxy URL is refused, None when it is accepted (http://host:port)."""
    if not isinstance(url, str) or not url.strip():
        return _t("the proxy address must be text such as http://proxy.example.com:8080")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return _t("the proxy address {url!r} is not a valid address (http://host:port)", url=url)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return _t("the proxy address must not hold a user name or a password: enter the proxy user in its own "
                  "field; its password goes to the Windows Credential Manager")
    if parts.scheme.lower() != "http":
        return _t("the proxy address must start with http:// (the connection to the P6 EPPM server stays encrypted "
                  "through the proxy)")
    if not parts.hostname:
        return _t("the proxy address has no host name (http://host:port)")
    if port is None:
        return _t("the proxy address needs a port (http://host:port, often 8080 or 3128)")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return _t("the proxy address must be http://host:port, with nothing after the port")
    return None


def is_loopback(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _registry() -> tuple[dict, Callable[[str], bool]]:
    """Windows Internet Settings proxies and bypass test ({} elsewhere). Tests replace it."""
    get = getattr(urllib.request, "getproxies_registry", None)
    bypass = getattr(urllib.request, "proxy_bypass_registry", None)
    if get is None or bypass is None:
        return {}, lambda _h: False
    try:
        return get() or {}, bypass
    except OSError:
        return {}, lambda _h: False


def _pac() -> Optional[str]:
    """The proxy setup script (PAC) address Windows is set to use, None when there is none. Tests replace it."""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            return str(winreg.QueryValueEx(k, "AutoConfigURL")[0] or "") or None
    except OSError:
        return None


def _system(scheme: str, host: str) -> tuple[Optional[str], str, bool]:
    """(proxy URL or None, source, bypassed) of the Windows / environment settings for one host."""
    env = urllib.request.getproxies_environment()
    if any(k != "no" for k in env):
        url = env.get(scheme)
        return url, "environment", bool(url) and bool(urllib.request.proxy_bypass_environment(host, env))
    proxies, bypass = _registry()
    url = proxies.get(scheme)
    if url and "://" not in url:
        url = "http://" + url
    return url, "windows", bool(url) and bool(bypass(host))


def resolve(base_url: str, profile: Optional[dict] = None) -> dict:
    """The proxy the calls to base_url use under the profile's settings:
    {mode, url (None = direct), source (profile, environment, windows or None), bypassed, user, pac, note}.
    Never holds a password."""
    profile = profile or {}
    mode = profile.get("proxy") or "system"
    parts = urlsplit(base_url or "")
    scheme, host = (parts.scheme or "https").lower(), parts.hostname or ""
    out: dict = {"mode": mode, "url": None, "source": None, "bypassed": False,
                 "user": (profile.get("proxy_user") or None)}
    if mode == "none":
        out["note"] = _t("Direct connection (the profile says no proxy).")
    elif mode == "custom":
        out.update(url=(profile.get("proxy_url") or "").strip() or None, source="profile")
    else:
        url, source, bypassed = _system(scheme, host)
        out.update(url=url, source=source if url else None, bypassed=bypassed)
        if bypassed:
            out.update(url=None, note=_t("Direct connection: {host} is in the {source} proxy bypass list.", host=host,
                                         source=source))
        elif not url:
            pac = _pac()
            out["note"] = _t("Direct connection: Windows sets no proxy for this address.")
            if pac:
                out["pac"] = pac
                out["note"] = _t("Windows uses a proxy setup script ({pac}), which this app does not run: the calls "
                                 "go direct. If the server can only be reached through a proxy, enter its address in "
                                 "the server settings (Proxy: This proxy).", pac=pac)
    if out["url"] and is_loopback(host):
        out.update(url=None, bypassed=True, note=_t("Direct connection: the server is on this PC (never proxied)."))
    if out["url"]:
        out["note"] = _t("Through the proxy {url} ({source}).", url=out["url"], source=out["source"])
    return out


class Via(urllib.request.ProxyHandler):
    """Sends every request of a session through one proxy, decided once by resolve() for the session's one host
    (no bypass check here). Basic credentials go in a Proxy-Authorization header (urllib moves it to the CONNECT
    request of an https tunnel), never in a URL."""

    def __init__(self, url: str, user: Optional[str] = None, password: Optional[str] = None):
        super().__init__({})  # never urllib's own system proxies: resolve() decided
        self.hostport = urlsplit(url).netloc
        self.auth = ("Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()) if user and password \
            else None

    def _via(self, req):
        if self.auth:
            req.add_header("Proxy-Authorization", self.auth)
        req.set_proxy(self.hostport, "http")
        return None  # the HTTP or HTTPS handler then connects to the proxy

    def http_open(self, req):
        return self._via(req)

    def https_open(self, req):
        return self._via(req)


def handler(choice: dict, password: Optional[str] = None) -> urllib.request.ProxyHandler:
    """The opener's proxy handler for a resolve() result: Via, or a handler that never proxies."""
    if choice.get("url"):
        return Via(choice["url"], choice.get("user"), password)
    return urllib.request.ProxyHandler({})
