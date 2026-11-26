"""Cloudflare setup of remote access, in the user's own Cloudflare account (free plan).

One Cloudflare API token, created by the user with the permissions of TOKEN_PERMISSIONS, is used during setup and
never stored. Two modes:

relay (default, no domain needed, free)
    a remotely managed tunnel `p6mcp-<machine>` (no public hostname), a Workers VPC Service bound to it for
    127.0.0.1:<remote_port>, and the relay Worker `p6mcp-<machine>` (worker.js) on the account's workers.dev
    subdomain, with the VPC Service binding and the relay secret. Public host: p6mcp-<machine>.<sub>.workers.dev.
domain (the user's own domain on Cloudflare)
    the same tunnel with a public hostname p6mcp.<zone> (ingress rule + proxied CNAME to <tunnel>.cfargotunnel.com).

Every step is idempotent: a second run finds the tunnel and VPC Service by name and re-uploads the Worker, so the
same function repairs a partial setup. remove() deletes what setup created (ids kept in config remote_cf).

API shapes were read back from objects wrangler created on 2026-11-26 (M0): VPC Service = POST
/accounts/{a}/connectivity/directory/services {name, type: http, http_port, host: {ipv4, network: {tunnel_id}}};
Worker bindings {type: vpc_service, name, service_id} and {type: secret_text, name, text}.
"""

from __future__ import annotations

import json
import re
import secrets
import socket
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Callable, Optional

API = "https://api.cloudflare.com/client/v4"
WORKER_FILE = "worker.js"
WORKER_COMPAT_DATE = "2026-11-01"
BINDING, SECRET_BINDING = "P6", "RELAY_SECRET"
TOKEN_PERMISSIONS = (  # what the token must allow (account level; Zone DNS Edit too for domain mode)
    "Account Settings: Read", "Cloudflare Tunnel: Edit", "Workers Scripts: Edit", "Connectivity Directory: Admin")
# Prefilled "Create token" page (permission group keys of the dashboard; a key it does not know is simply not
# ticked, and the permission check of setup() says which one is missing)
TOKEN_GROUPS = {"relay": (("account_settings", "read"), ("argotunnel", "edit"), ("workers_scripts", "edit"),
                          ("connectivity_directory", "admin")),
                "domain": (("account_settings", "read"), ("argotunnel", "edit"), ("zone", "read"), ("dns", "edit"))}
TIMEOUT = 30
Transport = Callable[[str, str, dict, Optional[bytes]], tuple[int, bytes]]  # method, url, headers, body


class CloudflareError(RuntimeError):
    """A Cloudflare call failed; the message is for the user (what to fix)."""


class NeedsAccount(CloudflareError):
    """The token reaches several accounts: the caller must choose one (accounts: [(id, name)])."""

    def __init__(self, accounts: list[tuple[str, str]]):
        super().__init__("This token can use several Cloudflare accounts: choose one.")
        self.accounts = accounts


def _urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise CloudflareError(f"Cloudflare cannot be reached from this PC: {e}") from e


def token_url(mode: str = "relay") -> str:
    """The Cloudflare dashboard's Create token page with the permissions of this mode ticked."""
    from urllib.parse import urlencode
    keys = json.dumps([{"key": k, "type": t} for k, t in TOKEN_GROUPS.get(mode, TOKEN_GROUPS["relay"])],
                      separators=(",", ":"))
    return "https://dash.cloudflare.com/profile/api-tokens?" + urlencode(
        {"permissionGroupKeys": keys, "name": "MCP for Primavera P6 remote access", "accountId": "*",
         "zoneId": "all"})


def worker_source() -> str:
    """The relay Worker shipped with this version (its VERSION stamp is checked by the health check)."""
    return resources.files("p6mcp.remote").joinpath(WORKER_FILE).read_text(encoding="utf-8")


def worker_version(source: Optional[str] = None) -> str:
    m = re.search(r'const VERSION = "([^"]+)"', source or worker_source())
    if not m:
        raise CloudflareError("worker.js has no VERSION stamp")
    return m.group(1)


def machine_slug(name: Optional[str] = None) -> str:
    """p6mcp-<computer name> in lower case letters, digits and hyphens (a Worker name is also a host label)."""
    raw = (name or socket.gethostname() or "pc").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:40].strip("-") or "pc"
    return f"p6mcp-{slug}"


@dataclass
class SetupResult:
    account_id: str
    mode: str
    public_host: str
    tunnel_id: str
    tunnel_token: str
    relay_secret: Optional[str]
    service_id: Optional[str] = None
    script: Optional[str] = None
    zone_id: Optional[str] = None
    dns_record_id: Optional[str] = None
    steps: list[str] = field(default_factory=list)

    def ids(self) -> dict:
        """What the config keeps (remote_cf): ids only, never a token or secret."""
        return {"account_id": self.account_id, "tunnel_id": self.tunnel_id, "service_id": self.service_id,
                "script": self.script, "zone_id": self.zone_id, "dns_record_id": self.dns_record_id}


class Cloudflare:
    def __init__(self, token: str, transport: Optional[Transport] = None):
        if not token or not token.strip():
            raise CloudflareError("Paste the Cloudflare API token first.")
        self.token = token.strip()
        self.transport = transport or _urllib_transport

    # ------------------------------------------------------------- HTTP
    def call(self, method: str, path: str, body: Any = None, raw: Optional[bytes] = None,
             content_type: Optional[str] = None, ok404: bool = False) -> Any:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        data = raw
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        status, payload = self.transport(method, API + path, headers, data)
        try:
            doc = json.loads(payload or b"{}")
        except ValueError:
            doc = {}
        if status == 404 and ok404:
            return None
        if status >= 400 or doc.get("success") is False:
            raise CloudflareError(_explain(status, doc, method, path))
        return doc.get("result")

    # ------------------------------------------------------------- account and token
    def account(self, account_id: Optional[str] = None) -> str:
        accounts = self.call("GET", "/accounts?per_page=50") or []
        pairs = [(a["id"], a.get("name") or a["id"]) for a in accounts]
        if account_id:
            if account_id not in {a for a, _ in pairs}:
                raise CloudflareError("This token cannot use the Cloudflare account that remote access was set up in.")
            return account_id
        if not pairs:
            raise CloudflareError("This token cannot read any Cloudflare account: give it 'Account Settings: Read'.")
        if len(pairs) > 1:
            raise NeedsAccount(pairs)
        return pairs[0][0]

    def check_permissions(self, account_id: str, mode: str, zone: Optional[str] = None) -> None:
        """Read calls that fail with a clear message when the token lacks a permission (before anything is
        created, so a token missing a right never leaves a half setup)."""
        checks = [("Cloudflare Tunnel: Edit", f"/accounts/{account_id}/cfd_tunnel?per_page=1"),
                  ("Workers Scripts: Edit", f"/accounts/{account_id}/workers/scripts")]
        if mode == "relay":
            checks.append(("Connectivity Directory: Admin",
                           f"/accounts/{account_id}/connectivity/directory/services?per_page=1"))
        for perm, path in checks:
            try:
                self.call("GET", path)
            except CloudflareError as e:
                raise CloudflareError(f"The token is missing the permission '{perm}' ({e}).") from e
        if mode == "domain":
            self.zone_id(zone or "")

    # ------------------------------------------------------------- objects
    def find_tunnel(self, account_id: str, name: str) -> Optional[str]:
        found = self.call("GET", f"/accounts/{account_id}/cfd_tunnel?name={name}&is_deleted=false") or []
        return found[0]["id"] if found else None

    def tunnel(self, account_id: str, name: str) -> str:
        tid = self.find_tunnel(account_id, name)
        if tid:
            return tid
        return self.call("POST", f"/accounts/{account_id}/cfd_tunnel",
                         {"name": name, "config_src": "cloudflare"})["id"]

    def tunnel_token(self, account_id: str, tunnel_id: str) -> str:
        tok = self.call("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token")
        if not isinstance(tok, str) or not tok:
            raise CloudflareError("Cloudflare returned no tunnel token.")
        return tok

    def vpc_service(self, account_id: str, name: str, tunnel_id: str, port: int) -> str:
        body = {"name": name, "type": "http", "http_port": port,
                "host": {"ipv4": "127.0.0.1", "network": {"tunnel_id": tunnel_id}}}
        for svc in self.call("GET", f"/accounts/{account_id}/connectivity/directory/services?per_page=100") or []:
            if svc.get("name") == name:
                net = (svc.get("host") or {}).get("network") or {}
                if net.get("tunnel_id") == tunnel_id and svc.get("http_port") == port:
                    return svc["service_id"]
                self.call("PUT", f"/accounts/{account_id}/connectivity/directory/services/{svc['service_id']}", body)
                return svc["service_id"]
        return self.call("POST", f"/accounts/{account_id}/connectivity/directory/services", body)["service_id"]

    def workers_subdomain(self, account_id: str, slug: str) -> str:
        """The account's workers.dev subdomain, registered when the account has none yet."""
        got = self.call("GET", f"/accounts/{account_id}/workers/subdomain", ok404=True)
        if got and got.get("subdomain"):
            return got["subdomain"]
        for attempt in range(5):
            name = f"{slug.removeprefix('p6mcp-')}-{uuid.uuid4().hex[:6]}" if attempt else slug.removeprefix("p6mcp-")
            try:
                return self.call("PUT", f"/accounts/{account_id}/workers/subdomain", {"subdomain": name})["subdomain"]
            except CloudflareError:
                continue
        raise CloudflareError("No workers.dev subdomain could be registered for this account.")

    def upload_worker(self, account_id: str, script: str, service_id: str, relay_secret: str, port: int) -> None:
        meta = {"main_module": WORKER_FILE, "compatibility_date": WORKER_COMPAT_DATE,
                "bindings": [{"type": "vpc_service", "name": BINDING, "service_id": service_id},
                             {"type": "secret_text", "name": SECRET_BINDING, "text": relay_secret},
                             {"type": "plain_text", "name": "P6_PORT", "text": str(port)}],
                "observability": {"enabled": False}}
        boundary = "p6mcp" + uuid.uuid4().hex
        parts = [("metadata", "blob", "application/json", json.dumps(meta).encode("utf-8")),
                 (WORKER_FILE, WORKER_FILE, "application/javascript+module", worker_source().encode("utf-8"))]
        body = b""
        for name, filename, ctype, data in parts:
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                     f"Content-Type: {ctype}\r\n\r\n").encode("utf-8") + data + b"\r\n"
        body += f"--{boundary}--\r\n".encode("utf-8")
        self.call("PUT", f"/accounts/{account_id}/workers/scripts/{script}", raw=body,
                  content_type=f"multipart/form-data; boundary={boundary}")
        self.call("POST", f"/accounts/{account_id}/workers/scripts/{script}/subdomain",
                  {"enabled": True, "previews_enabled": False})

    def zone_id(self, host: str) -> str:
        """The zone of a host name the user owns on Cloudflare (p6mcp.example.com -> example.com)."""
        labels = host.lower().strip(".").split(".")
        for i in range(len(labels) - 1):
            zone = ".".join(labels[i:])
            found = self.call("GET", f"/zones?name={zone}") or []
            if found:
                return found[0]["id"]
        raise CloudflareError(f"No zone of {host!r} in this Cloudflare account (or the token lacks 'Zone: Read' "
                              "and 'DNS: Edit' on it).")

    def route_hostname(self, account_id: str, tunnel_id: str, host: str, port: int) -> tuple[str, str]:
        """Domain mode: ingress rule of the tunnel and a proxied CNAME. Returns (zone id, DNS record id)."""
        self.call("PUT", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
                  {"config": {"ingress": [{"hostname": host, "service": f"http://127.0.0.1:{port}"},
                                          {"service": "http_status:404"}]}})
        zone = self.zone_id(host)
        target = f"{tunnel_id}.cfargotunnel.com"
        for rec in self.call("GET", f"/zones/{zone}/dns_records?name={host}") or []:
            if rec.get("type") == "CNAME" and rec.get("content") == target:
                return zone, rec["id"]
            raise CloudflareError(f"{host} already has a DNS record ({rec.get('type')} {rec.get('content')}): "
                                  "choose another name or remove that record.")
        rec = self.call("POST", f"/zones/{zone}/dns_records",
                        {"type": "CNAME", "name": host, "content": target, "proxied": True,
                         "comment": "MCP for Primavera P6 remote access"})
        return zone, rec["id"]


# ----------------------------------------------------------------- setup, repair, remove

def setup(token: str, mode: str = "relay", port: int = 39218, account_id: Optional[str] = None,
          domain_host: Optional[str] = None, machine: Optional[str] = None, relay_secret: Optional[str] = None,
          transport: Optional[Transport] = None, progress: Callable[[str], None] = lambda s: None) -> SetupResult:
    """Create (or repair) remote access in the token's account. relay_secret: keep this one (repair), else a new
    one. Raises CloudflareError / NeedsAccount; nothing is written locally here (the caller stores the result)."""
    if mode not in ("relay", "domain"):
        raise CloudflareError(f"unknown mode {mode!r}")
    if mode == "domain" and not domain_host:
        raise CloudflareError("Domain mode needs the host name to use, for example p6mcp.example.com.")
    cf = Cloudflare(token, transport)
    steps: list[str] = []

    def step(text: str) -> None:
        steps.append(text)
        progress(text)

    acct = cf.account(account_id)
    step("Token checked")
    cf.check_permissions(acct, mode, domain_host)
    step("Permissions checked")
    slug = machine_slug(machine)
    tid = cf.tunnel(acct, slug)
    step(f"Tunnel {slug} ready")
    ttoken = cf.tunnel_token(acct, tid)
    if mode == "relay":
        sid = cf.vpc_service(acct, slug, tid, port)
        step("Private link from Cloudflare to this PC ready")
        sub = cf.workers_subdomain(acct, slug)
        secret = relay_secret or secrets.token_urlsafe(32)
        cf.upload_worker(acct, slug, sid, secret, port)
        host = f"{slug}.{sub}.workers.dev"
        step(f"Relay published at https://{host}")
        return SetupResult(acct, mode, host, tid, ttoken, secret, service_id=sid, script=slug, steps=steps)
    zone, rec = cf.route_hostname(acct, tid, domain_host.lower(), port)
    step(f"https://{domain_host.lower()} routed to this PC")
    return SetupResult(acct, mode, domain_host.lower(), tid, ttoken, None, zone_id=zone, dns_record_id=rec,
                       steps=steps)


def remove(token: str, ids: dict, transport: Optional[Transport] = None,
           progress: Callable[[str], None] = lambda s: None) -> list[str]:
    """Delete what setup created (the tunnel connector must be stopped first). Missing objects are skipped."""
    cf = Cloudflare(token, transport)
    acct = cf.account(ids.get("account_id"))
    done = []

    def gone(text: str) -> None:
        done.append(text)
        progress(text)

    if ids.get("script"):
        cf.call("DELETE", f"/accounts/{acct}/workers/scripts/{ids['script']}?force=true", ok404=True)
        gone("Relay Worker deleted")
    if ids.get("service_id"):
        cf.call("DELETE", f"/accounts/{acct}/connectivity/directory/services/{ids['service_id']}", ok404=True)
        gone("Private link deleted")
    if ids.get("zone_id") and ids.get("dns_record_id"):
        cf.call("DELETE", f"/zones/{ids['zone_id']}/dns_records/{ids['dns_record_id']}", ok404=True)
        gone("DNS record deleted")
    if ids.get("tunnel_id"):
        cf.call("DELETE", f"/accounts/{acct}/cfd_tunnel/{ids['tunnel_id']}/connections", ok404=True)
        cf.call("DELETE", f"/accounts/{acct}/cfd_tunnel/{ids['tunnel_id']}", ok404=True)
        gone("Tunnel deleted")
    return done


def _explain(status: int, doc: dict, method: str, path: str) -> str:
    errs = doc.get("errors") or []
    msg = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errs if isinstance(e, dict)) or f"HTTP {status}"
    where = re.sub(r"/accounts/[0-9a-f]{32}", "/accounts/<account>", path.split("?")[0])
    if status in (401, 403) or any(e.get("code") in (9109, 10000, 10001) for e in errs if isinstance(e, dict)):
        return f"Cloudflare refused {method} {where}: {msg}. Check the token and its permissions."
    return f"Cloudflare error on {method} {where}: {msg}"
