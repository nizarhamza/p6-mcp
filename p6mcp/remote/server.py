"""The remote access server: p6mcp-server --remote [--port N]

A process of its own (started by the tray app's supervisor), so that remote clients get their own tool list,
their own confirmation tokens and can be stopped without touching local clients. Bound to 127.0.0.1 only: the
tunnel connector on this PC is the only way in.

Every request goes through RemoteGuard:
- relay mode (the default): the request must carry the relay secret header that only the user's relay Worker
  adds (a local process or a browser page reaching 127.0.0.1 has no secret);
- domain mode: the Host header must name the configured public host;
- request bodies are capped (BODY_CAP).
Then the SDK app checks the OAuth bearer token (401 with resource_metadata when it is missing) and the tools
check the remote policy (p6mcp.policy.remote_refusal) on every call, with the grant's scopes.

The public host comes from the settings, never from a request header: it names the issuer and the resource
identifier the tokens are bound to.
"""

from __future__ import annotations

import argparse
import functools
import hmac
import logging
import sys
from typing import Any, Callable, Optional

from p6mcp import config, credentials, policy, runtime
from p6mcp import confirm as _confirm
from p6mcp.remote import (CLIENT_IP_HEADER, PATH, READ_SCOPE, RELAY_HEADER, RELAY_SECRET_KEY, public_url,
                          resource_url, shaping)
from p6mcp.remote import audit
from p6mcp.remote import auth as _auth
from p6mcp.remote.auth import grant_of
from p6mcp.remote.limits import FILTERED, Allowlist, RateLimiter
from p6mcp.remote.store import Store

HOST = "127.0.0.1"
BODY_CAP = 1_048_576  # bytes of one request body (an MCP call is a few KB)
STATUS = "remote"
REMOTE_LINE = ("Remote session (claude.ai or ChatGPT): file import and export tools are not available here; edits, "
               "when allowed, need a connection approved with edits on the PC.")
CLIENT_IP = audit.CLIENT_IP
log = logging.getLogger("p6mcp.remote")


class SetupError(RuntimeError):
    """Remote access is not set up enough to start (the message says what is missing)."""


# ----------------------------------------------------------------- tools

def _remote_tool(name: str, gated: Callable, tool_error: Optional[type],
                 listed: Optional[list[str]] = None) -> Callable:
    """The callable a remote client reaches: remote policy with the grant's scopes, the confirmation token
    bound to the grant, then the result redacted and fitted to the size budget. listed: the remote tool list of
    this server (p6_diagnose reports it instead of the local one)."""
    from mcp.server.auth.middleware.auth_context import get_access_token
    from p6mcp import server as S

    def fail(msg: str):
        raise (tool_error or ValueError)(msg)

    writes = policy.remote_kind(name) == "write"

    def roots():
        return _local_roots(S)

    @functools.wraps(gated)
    def call(*a, **k):
        tok = get_access_token()
        claims = getattr(tok, "claims", None) or {}
        grant, app = grant_of(tok), claims.get("client_name") or (tok.client_id if tok else None)
        timer, state = audit.Timer(), {"result": "error", "chars": None}
        try:
            st = S._refresh()
            why = policy.remote_refusal(name, st.tool_profile, st.allow_writes, st.remote_allow_writes,
                                        tok.scopes if tok else None, S._shop_on(st), args=k)
            if why:
                state["result"] = "refused"
                fail(why)
            owner = _confirm.OWNER.set(grant)
            client = _confirm.CLIENT.set({"transport": "remote", "grant": grant, "app": app})
            try:
                res = gated(*a, **k)
            except Exception as e:
                if tool_error is not None and isinstance(e, tool_error):
                    raise
                fail(shaping.redact(str(e) if isinstance(e, (ValueError, RuntimeError))
                                    else f"{type(e).__name__}: {e}", roots()))
            finally:
                _confirm.CLIENT.reset(client)
                _confirm.OWNER.reset(owner)
            if name == "p6_diagnose" and isinstance(res, dict):
                res = _remote_diagnose(res, st, tok, listed)
            try:
                out = shaping.shape(res, roots=roots())
            except shaping.TooLarge as e:
                state["result"] = "too_large"
                fail(str(e))
            state.update(result="ok", chars=shaping.size(out))
            return out
        finally:
            audit.write("tool", tool=name, grant=grant, app=app, ip=CLIENT_IP.get(), args=audit.digest(k),
                        dry_run=not (k.get("confirm") or k.get("dry_run") is False) if writes else None,
                        result=state["result"], ms=timer.ms(), chars=state["chars"])
    return call


def _local_roots(S) -> list[tuple[str, str]]:
    """The folders of this PC that results may name: database, backups, edit log, app data, user profile."""
    import os
    st = S.SETTINGS
    folders = {os.path.dirname(S.DB_PATH) if S.DB_PATH else "": None, st.backup_dir: None, st.edit_log_dir: None,
               config.localdata_dir(): None, config.appdata_dir(): None,
               os.environ.get("USERPROFILE", ""): "~"}
    return shaping.known_roots(folders)


def _remote_diagnose(res: dict, st: config.Settings, tok: Any, listed: Optional[list[str]]) -> dict:
    """p6_diagnose as a remote client sees it: the remote tool list and restart need, this connection, and the
    remote edit switch next to the local one."""
    res = dict(res)
    now = remote_tools(st)
    if listed is not None:
        res["tools_listed"] = len(listed)
        res["restart_required"] = set(now) != set(listed)
        res.pop("restart_note", None)
        if res["restart_required"]:
            res["restart_note"] = ("Settings changed since the remote access server started: restart remote access "
                                   "in MCP for Primavera P6 on the PC, then reconnect.")
    claims = getattr(tok, "claims", None) or {}
    res["remote"] = {"public_url": resource_url(st.remote_public_host) if st.remote_public_host else None,
                     "mode": st.remote_mode, "allow_edits_from_remote": st.remote_allow_writes,
                     "connection": {"client": claims.get("client_name") or (tok.client_id if tok else None),
                                    "scopes": list(tok.scopes) if tok else []},
                     "note": "Edits from a remote client need 'Allow write operations' and 'Allow edits from remote "
                             "clients' on the PC, and a connection approved with edits."}
    return res


def remote_tools(st: config.Settings) -> list[str]:
    """Names of the tools offered to remote clients under these settings, in registration order."""
    from p6mcp import server as S
    shop = S._shop_on(st)
    return [n for n in S.REGISTERED
            if policy.remote_listed(n, st.tool_profile, st.allow_writes, st.remote_allow_writes, shop)]


def instructions() -> str:
    from p6mcp import server as S
    return S.INSTRUCTIONS + "\n- " + REMOTE_LINE


def build_mcp(st: config.Settings, verifier: Any, store: Optional[Store] = None, fetch: Any = None):
    """A FastMCP/MCPServer of its own for remote clients: OAuth resource server for https://<host>/mcp, the
    remote tool set (no prompts in this version) and, with a store, the authorization server's routes."""
    from mcp.server.auth.provider import AccessToken
    from mcp.server.auth.settings import AuthSettings
    from p6mcp import server as S
    if "validate_token_resource" not in AuthSettings.model_fields or "claims" not in AccessToken.model_fields:
        raise SetupError("Remote access needs the mcp package 2.x (token resource check and connection ids): "
                         "update MCP for Primavera P6.")
    if not st.remote_public_host:
        raise SetupError("Remote access is not set up: no public address. Run the Cloudflare setup in MCP for "
                         "Primavera P6 Settings (Remote access).")
    host = st.remote_public_host
    auth_kw = dict(issuer_url=public_url(host), resource_server_url=resource_url(host), required_scopes=[READ_SCOPE])
    auth_kw["validate_token_resource"] = True  # refuse tokens issued for another resource
    srv = S.FastMCP("primavera-p6", instructions=instructions(), token_verifier=verifier,
                    auth=AuthSettings(**auth_kw), **S._version_kw)
    if store is not None:
        extra = tuple(st.raw.get("remote_extra_redirect_uris") or ())
        _auth.add_routes(srv, public_url(host), resource_url(host), store, extra, fetch, st.raw.get("language"))
    listed = remote_tools(st)
    for name in listed:
        fn = _remote_tool(name, S.REGISTERED[name], S.ToolError, listed)
        if S.ToolAnnotations is None:
            srv.tool()(fn)
        else:
            srv.tool(annotations=S.ToolAnnotations.model_validate(S.TOOL_HINTS[name]))(fn)
    return srv


# ----------------------------------------------------------------- guard and app

class RemoteGuard:
    """ASGI middleware in front of the SDK app: relay secret (relay mode) or Host (domain mode), the optional IP
    allowlist, rate limits, body cap, the caller's IP in CLIENT_IP and one audit line per request."""

    def __init__(self, app, mode: str, public_host: str, relay_secret: Optional[str],
                 limiter: Optional[RateLimiter] = None, allowlist: Optional[Allowlist] = None):
        if mode == "relay" and not relay_secret:
            raise SetupError("Remote access is not set up: the relay secret is missing. Run the Cloudflare setup "
                             "again (Repair) in MCP for Primavera P6 Settings (Remote access).")
        self.app = app
        self.mode = mode
        self.host = public_host.lower()
        self.secret = (relay_secret or "").encode("utf-8")
        self.limiter = limiter or RateLimiter()
        self.allowlist = allowlist

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # lifespan: the SDK's session manager starts and stops here
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        path = scope.get("path", "")
        if self.mode == "relay":
            if not hmac.compare_digest(headers.get(RELAY_HEADER, "").encode("utf-8"), self.secret):
                await _reply(send, 403, "Forbidden")
                return
            ip = headers.get(CLIENT_IP_HEADER) or None
        elif headers.get("host", "").lower() != self.host:
            await _reply(send, 421, "Invalid Host header")
            return
        else:
            ip = headers.get("cf-connecting-ip") or None  # set by Cloudflare's edge in front of the tunnel
        timer = audit.Timer()
        if self.allowlist is not None and path in FILTERED:
            self.allowlist.refresh_if_old()
            if not self.allowlist.allows(ip):
                audit.write("http", method=scope["method"], path=path, status=403, ip=ip, reason="allowlist")
                await _reply(send, 403, "Not from an allowed AI service (the IP allowlist is on)")
                return
        ok, wait = self.limiter.allow(path, ip)
        if not ok:
            audit.write("http", method=scope["method"], path=path, status=429, ip=ip, reason="rate limit")
            await _reply(send, 429, "Too many requests", {"retry-after": str(wait)})
            return
        try:
            length = int(headers.get("content-length") or 0)
        except ValueError:
            length = BODY_CAP + 1
        if length > BODY_CAP:
            await _reply(send, 413, "Request body too large")
            return
        received = 0

        async def capped_receive():  # a body sent without content-length (chunked) is capped as it arrives
            nonlocal received
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > BODY_CAP:
                    raise ValueError("request body too large")
            return msg

        info = {"status": None, "bytes": 0}

        async def counted_send(msg):
            if msg["type"] == "http.response.start":
                info["status"] = msg["status"]
            elif msg["type"] == "http.response.body":
                info["bytes"] += len(msg.get("body", b""))
            await send(msg)

        tok = CLIENT_IP.set(ip)
        try:
            await self.app(scope, capped_receive, counted_send)
        finally:
            CLIENT_IP.reset(tok)
            audit.write("http", method=scope["method"], path=path, status=info["status"], bytes=info["bytes"],
                        ms=timer.ms(), ip=ip)


async def _reply(send, status: int, text: str, extra: Optional[dict] = None) -> None:
    body = text.encode("utf-8")
    hdrs = [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]
    hdrs += [(k.encode(), v.encode()) for k, v in (extra or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": hdrs})
    await send({"type": "http.response.body", "body": body})


def build_app(st: config.Settings, verifier: Any = None, relay_secret: Optional[str] = None,
              port: Optional[int] = None, store: Optional[Store] = None, fetch: Any = None):
    """The guarded ASGI app. Without a verifier: the authorization server on store (default: auth.db) and its
    tokens. With a verifier and no store (tests): that verifier, no authorization server."""
    from mcp.server.transport_security import TransportSecuritySettings
    if verifier is None:
        store = store or Store()
        verifier = _auth.StoreVerifier(store)
    if store is not None and st.remote_public_host:  # a new public address: the old connections are void
        store.revoke_other_resources(resource_url(st.remote_public_host))
    srv = build_mcp(st, verifier, store, fetch)
    port = port or st.remote_port
    host = st.remote_public_host
    # The SDK's own Host/Origin checks stay on as a second layer. Any local port is accepted: the tunnel connector
    # names 127.0.0.1 with the port it was given, and RemoteGuard has checked the relay secret or the Host already.
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted({host, f"{HOST}:{port}", f"{HOST}:*", "localhost:*"}),
        allowed_origins=sorted({public_url(host)}))
    app = srv.streamable_http_app(streamable_http_path=PATH, transport_security=security, host=HOST)
    allowlist = Allowlist() if st.raw.get("remote_ip_allowlist") else None
    if allowlist is not None:
        allowlist.refresh_if_old()
    return RemoteGuard(app, st.remote_mode, host, relay_secret, allowlist=allowlist)


# ----------------------------------------------------------------- process

def make_server(app, port: int):
    import uvicorn
    cfg = uvicorn.Config(app, host=HOST, port=port, log_config=None, access_log=False, lifespan="on",
                         timeout_graceful_shutdown=3)
    return uvicorn.Server(cfg)


def serve(port: Optional[int] = None) -> int:
    runtime.setup_logging("remote-server")
    st = config.load()
    port = int(port or st.remote_port)
    try:
        secret = credentials.get(RELAY_SECRET_KEY) if st.remote_mode == "relay" else None
        app = build_app(st, relay_secret=secret, port=port)
    except (SetupError, credentials.SecretStoreError) as e:
        runtime.write_status("error", port, str(e), name=STATUS)
        log.error("%s", e)
        return 2
    from p6mcp import settings_logic
    if not settings_logic.port_free(port):
        msg = (f"Port {port} is already in use: choose another remote access port in MCP for Primavera P6 Settings "
               "(Remote access).")
        runtime.write_status("error", port, msg, name=STATUS)
        log.error(msg)
        return 3
    server = make_server(app, port)
    url = resource_url(st.remote_public_host)
    runtime.write_status("starting", port, name=STATUS)
    log.info("remote access server on http://%s:%s%s for %s", HOST, port, PATH, url)
    orig_startup = server.startup

    async def startup(*a, **k):
        await orig_startup(*a, **k)
        if server.started:
            runtime.write_status("running", port, url=url, name=STATUS)
    server.startup = startup
    try:
        server.run()
    except Exception as e:  # reported to the tray app through the status file
        runtime.write_status("error", port, f"{type(e).__name__}: {e}", name=STATUS)
        log.exception("remote access server failed")
        return 1
    runtime.write_status("stopped", port, name=STATUS)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="p6mcp-server --remote")
    ap.add_argument("--remote", action="store_true")
    ap.add_argument("--port", type=int)
    a = ap.parse_args(argv)
    return serve(a.port)


if __name__ == "__main__":
    sys.exit(main())
