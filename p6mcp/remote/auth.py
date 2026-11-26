"""OAuth 2.1 authorization server of remote access, and the token verifier of the remote MCP endpoint.

This PC is the authorization server of its own remote access: no outside identity provider is involved. A
client (claude.ai, ChatGPT) discovers it through the protected resource metadata, identifies itself with a client
metadata document (CIMD, both measured in M0) or registers with DCR (fallback), and sends the user's browser to
/authorize. The browser page shows a pairing code and waits; the user approves the request showing that code on
the PC (tray app, or `p6mcp-server remote approve CODE`), with or without edits. The browser then goes back to the
client with an authorization code, which the client exchanges at /token (PKCE S256) for an access token (1 hour)
and a refresh token (30 days, rotated; a refresh token used twice revokes the connection).

Endpoints (all public, behind RemoteGuard): /.well-known/oauth-authorization-server (and the same document at
/.well-known/openid-configuration, which ChatGPT asks for), /.well-known/oauth-protected-resource (root; the SDK
serves the /mcp one), /register, /authorize, /authorize/wait, /token, /revoke.

Measured client behaviour (M0, 2026-11-26): claude.ai client_id https://claude.ai/oauth/mcp-oauth-client-metadata,
callback https://claude.ai/api/mcp/auth_callback; ChatGPT client_id https://chatgpt.com/oauth/client.json, callback
https://chatgpt.com/connector_platform_oauth_redirect (the RFC 9207 iss variant); both ask for scope p6:read and
send resource = the connector URL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import re
import socket
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlencode, urlparse

import anyio
from mcp.server.auth.provider import AccessToken

from p6mcp import i18n
from p6mcp.remote import READ_SCOPE, WRITE_SCOPE, audit
from p6mcp.remote.store import APPROVAL_SECONDS, Store, TooManyRequests

log = logging.getLogger("p6mcp.remote.auth")
SCOPES = [READ_SCOPE, WRITE_SCOPE]
REDIRECT_EXACT = frozenset({
    "https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect"})
REDIRECT_PREFIX = ("https://chatgpt.com/connector/oauth/",)
CIMD_HOSTS = frozenset({"claude.ai", "claude.com", "chatgpt.com", "openai.com"})
CIMD_MAX_BYTES = 65536
CIMD_TIMEOUT = 5.0
_PKCE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_RID = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
Fetcher = Callable[[str], Awaitable[tuple[int, bytes]]]  # url -> (status, body): replaced in tests


def grant_of(token: Optional[AccessToken]) -> Optional[str]:
    """The OAuth connection a token belongs to (its client id when the verifier gives no grant)."""
    if token is None:
        return None
    claims = getattr(token, "claims", None) or {}  # mcp 1.x AccessToken has no claims
    return str(claims.get("grant") or token.client_id)


def redirect_ok(uri: str, extra: tuple[str, ...] = ()) -> bool:
    return uri in REDIRECT_EXACT or uri in extra or any(uri.startswith(p) and "?" not in uri and "#" not in uri
                                                         for p in REDIRECT_PREFIX)


class StoreVerifier:
    """Access tokens of the store, for the SDK's bearer middleware (it also checks the resource)."""

    def __init__(self, store: Store):
        self.store = store

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        g = await anyio.to_thread.run_sync(self.store.check_access, token)  # SQLite off the event loop
        if g is None:
            return None
        return AccessToken(token=token, client_id=g["client_id"], scopes=g["scopes"], expires_at=g["exp"],
                           resource=g["resource"], claims={"grant": g["grant"], "client_name": g["client_name"]})


# ----------------------------------------------------------------- client metadata documents (CIMD)

def _public_host(host: str) -> bool:
    """Every address the host resolves to is a public one (no loopback, private or link-local target)."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)


async def http_fetch(url: str) -> tuple[int, bytes]:
    try:
        import httpx2 as httpx
    except ImportError:  # mcp 1.x
        import httpx
    async with httpx.AsyncClient(timeout=CIMD_TIMEOUT, follow_redirects=False) as c:
        async with c.stream("GET", url, headers={"accept": "application/json"}) as r:
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > CIMD_MAX_BYTES:
                    break
            return r.status_code, body


async def resolve_client(client_id: str, store: Store, fetch: Optional[Fetcher] = None) -> Optional[dict]:
    """A DCR client of the store, or the client metadata document at client_id (https, allowed host, public
    addresses, at most CIMD_MAX_BYTES, no redirect; cached a day). None when neither."""
    c = store.client(client_id)
    if c is not None:
        return c
    u = urlparse(client_id)
    try:
        port = u.port
    except ValueError:  # https://claude.ai:abc/x
        return None
    if u.scheme != "https" or u.hostname not in CIMD_HOSTS or port not in (None, 443) or not u.path.strip("/"):
        return None
    doc = store.cimd_get(client_id)
    if doc is None:
        if fetch is None and not _public_host(u.hostname):
            log.warning("CIMD host %s does not resolve to public addresses", u.hostname)
            return None
        try:
            status, body = await (fetch or http_fetch)(client_id)
        except Exception as e:  # network errors: the client is unknown for now
            log.warning("CIMD fetch failed for %s: %s", client_id, e)
            return None
        if status != 200 or len(body) > CIMD_MAX_BYTES:
            log.warning("CIMD refused for %s: HTTP %s, %d bytes", client_id, status, len(body))
            return None
        try:
            doc = json.loads(body)
        except ValueError:
            return None
        if not isinstance(doc, dict) or doc.get("client_id") != client_id:
            log.warning("CIMD refused for %s: client_id mismatch", client_id)
            return None
        store.cimd_put(client_id, doc)
    uris = [x for x in doc.get("redirect_uris") or [] if isinstance(x, str)]
    return {"client_id": client_id, "client_name": str(doc.get("client_name") or u.hostname)[:80],
            "redirect_uris": uris, "via": "cimd"}


# ----------------------------------------------------------------- metadata

def as_metadata(issuer: str) -> dict:
    return {
        "issuer": issuer,
        "authorization_endpoint": issuer + "/authorize",
        "token_endpoint": issuer + "/token",
        "registration_endpoint": issuer + "/register",
        "revocation_endpoint": issuer + "/revoke",
        "scopes_supported": SCOPES,
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    }


def pkce_ok(verifier: str, challenge: str) -> bool:
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii", "replace")).digest()).rstrip(b"=")
    return hmac.compare_digest(digest.decode(), challenge)


# ----------------------------------------------------------------- routes

PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="no-referrer">
{refresh}<title>{title}</title>
<style>body{{font-family:"Segoe UI",Arial,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem;color:#0a1a33;
background:#fff;line-height:1.5}}h1{{font-size:1.3rem}}.code{{font-size:2.4rem;letter-spacing:.35rem;font-weight:700;
background:#eef2f8;padding:.5rem 1rem;display:inline-block;border-radius:6px}}.muted{{color:#4a5a73}}</style>
</head><body><h1>MCP for Primavera P6</h1>{body}</body></html>"""
NO_STORE = {"cache-control": "no-store", "pragma": "no-cache"}
CSP = ("default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'none'")
PAGE_HEADERS = dict(NO_STORE, **{"content-security-policy": CSP, "x-frame-options": "DENY",
                                 "x-content-type-options": "nosniff", "referrer-policy": "no-referrer"})


def _page(title: str, body: str, refresh: Optional[str] = None, status: int = 200):
    from starlette.responses import HTMLResponse
    meta = f'<meta http-equiv="refresh" content="2;url={html.escape(refresh)}">' if refresh else ""
    return HTMLResponse(PAGE.format(lang=html.escape(i18n.language()), title=html.escape(title), body=body,
                                    refresh=meta), status_code=status, headers=PAGE_HEADERS)


def _message(key: str, status: int = 400):
    return _page(i18n.tr("remote.page.title_error"), f"<p>{html.escape(i18n.tr(key))}</p>", status=status)


def _event(what: str, **fields) -> None:
    audit.write("oauth", what=what, ip=audit.CLIENT_IP.get(), **fields)


def _oauth_error(status: int, error: str, desc: str = ""):
    from starlette.responses import JSONResponse
    log.info("oauth error %s: %s", error, desc)
    _event("error", error=error, detail=desc[:200])
    return JSONResponse({"error": error, "error_description": desc}, status_code=status, headers=NO_STORE)


def add_routes(srv: Any, issuer: str, resource: str, store: Store, extra_redirects: tuple[str, ...] = (),
               fetch: Optional[Fetcher] = None, language: Optional[str] = None) -> None:
    """Register the authorization server's routes on the remote MCP server (custom routes: no bearer token)."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse, RedirectResponse, Response
    i18n.set_language(language)

    @srv.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
    async def as_meta(request: Request) -> Response:
        return JSONResponse(as_metadata(issuer))

    @srv.custom_route("/.well-known/openid-configuration", methods=["GET"])
    async def oidc_meta(request: Request) -> Response:
        return JSONResponse(as_metadata(issuer))

    @srv.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
    async def prm_root(request: Request) -> Response:
        return JSONResponse({"resource": resource, "authorization_servers": [issuer], "scopes_supported": SCOPES,
                             "bearer_methods_supported": ["header"], "resource_name": "MCP for Primavera P6"})

    @srv.custom_route("/register", methods=["POST"])
    async def register(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return _oauth_error(400, "invalid_client_metadata", "JSON body required")
        if not isinstance(body, dict):
            return _oauth_error(400, "invalid_client_metadata", "JSON object required")
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
            return _oauth_error(400, "invalid_redirect_uri", "redirect_uris required")
        bad = [u for u in uris if not redirect_ok(u, extra_redirects)]
        if bad:
            return _oauth_error(400, "invalid_redirect_uri", f"redirect URI not allowed: {bad[0]}")
        name = str(body.get("client_name") or urlparse(uris[0]).hostname or "client")[:80]
        cid = await anyio.to_thread.run_sync(store.add_client, name, uris)
        log.info("client registered (DCR): %s", name)
        _event("registered", app=name, hosts=sorted({urlparse(u).hostname for u in uris}))
        return JSONResponse({"client_id": cid, "client_name": name, "redirect_uris": uris,
                             "token_endpoint_auth_method": "none", "response_types": ["code"],
                             "grant_types": ["authorization_code", "refresh_token"]}, status_code=201, headers=NO_STORE)

    @srv.custom_route("/authorize", methods=["GET"])
    async def authorize(request: Request) -> Response:
        q = request.query_params
        cid, redirect, state = q.get("client_id", ""), q.get("redirect_uri", ""), q.get("state")
        client = await resolve_client(cid, store, fetch) if cid else None
        if client is None:
            return _message("remote.page.bad_client")
        if not redirect or redirect not in client["redirect_uris"] or not redirect_ok(redirect, extra_redirects):
            return _message("remote.page.bad_redirect")

        def back(**params) -> Response:  # errors go back to the client once its redirect URI is trusted
            params = {k: v for k, v in params.items() if v is not None}
            params["iss"] = issuer
            return RedirectResponse(redirect + ("&" if "?" in redirect else "?") + urlencode(params), status_code=302)

        if q.get("response_type") != "code":
            return back(error="unsupported_response_type", state=state)
        challenge = q.get("code_challenge", "")
        if q.get("code_challenge_method") != "S256" or not _PKCE.match(challenge):
            return back(error="invalid_request", error_description="PKCE S256 code_challenge required", state=state)
        if (q.get("resource") or resource).rstrip("/") != resource:
            return back(error="invalid_target", error_description=f"resource must be {resource}", state=state)
        asked = (q.get("scope") or READ_SCOPE).split()
        if any(s not in SCOPES for s in asked):
            return back(error="invalid_scope", error_description=f"scopes: {' '.join(SCOPES)}", state=state)
        scopes = [READ_SCOPE] + [s for s in asked if s == WRITE_SCOPE]
        try:
            req = await anyio.to_thread.run_sync(store.add_pending, client, redirect, state, challenge, scopes,
                                                 resource)
        except TooManyRequests:
            _event("busy", app=client["client_name"])
            return _message("remote.page.busy", 429)
        log.info("connection request from %s (%s)", client["client_name"], client["via"])
        _event("request", app=client["client_name"], via=client["via"], host=urlparse(redirect).hostname)
        return RedirectResponse(f"/authorize/wait?id={req['id']}", status_code=302, headers=NO_STORE)

    @srv.custom_route("/authorize/wait", methods=["GET"])
    async def authorize_wait(request: Request) -> Response:
        rid = request.query_params.get("id", "")
        out = await anyio.to_thread.run_sync(store.finish_pending, rid) if _RID.match(rid) else None
        if out is None:
            return _message("remote.page.unknown", 404)
        req = out["request"]
        redirect = req["redirect_uri"]
        sep = "&" if "?" in redirect else "?"
        if out["status"] == "approved":
            log.info("connection approved: %s, grant %s", req["client_name"], out["grant"])
            _event("approved", app=req["client_name"], grant=out["grant"], scopes=req["scopes"])
            params = {"code": out["code"], "iss": issuer}
            if req["state"] is not None:
                params["state"] = req["state"]
            return RedirectResponse(redirect + sep + urlencode(params), status_code=302, headers=NO_STORE)
        if out["status"] in ("denied", "expired"):
            log.info("connection %s: %s", out["status"], req["client_name"])
            _event(out["status"], app=req["client_name"])
            params = {"error": "access_denied", "iss": issuer}
            if req["state"] is not None:
                params["state"] = req["state"]
            return RedirectResponse(redirect + sep + urlencode(params), status_code=302, headers=NO_STORE)
        host = urlparse(redirect).hostname or ""
        body = (f"<p>{html.escape(i18n.tr('remote.page.asks', client=req['client_name'], host=host))}</p>"
                f"<p>{html.escape(i18n.tr('remote.page.approve_on_pc'))}</p>"
                f"<p class=\"code\">{html.escape(req['pair'])}</p>"
                f"<p class=\"muted\">{html.escape(i18n.tr('remote.page.waiting', left=req['expires_in']))}</p>")
        return _page(i18n.tr("remote.page.title"), body, refresh=f"/authorize/wait?id={rid}")

    @srv.custom_route("/token", methods=["POST"])
    async def token(request: Request) -> Response:
        try:
            form = await request.form()
        except Exception:
            return _oauth_error(400, "invalid_request", "form body required")
        gt = form.get("grant_type")
        cid = form.get("client_id")
        res = form.get("resource")
        if res and str(res).rstrip("/") != resource:
            return _oauth_error(400, "invalid_target", "resource mismatch")
        if gt == "authorization_code":
            rec = await anyio.to_thread.run_sync(store.take_code, str(form.get("code", "")))
            if rec is None:
                return _oauth_error(400, "invalid_grant", "unknown or expired code")
            if form.get("redirect_uri") != rec["redirect_uri"]:
                return _oauth_error(400, "invalid_grant", "redirect_uri mismatch")
            if cid and cid != rec["client_id"]:
                return _oauth_error(400, "invalid_grant", "client_id mismatch")
            if not pkce_ok(str(form.get("code_verifier", "")), rec["challenge"]):
                store.revoke(rec["grant_id"])
                return _oauth_error(400, "invalid_grant", "PKCE verification failed")
            _event("token", grant=rec["grant_id"])
            return JSONResponse(await anyio.to_thread.run_sync(store.issue, rec["grant_id"]), headers=NO_STORE)
        if gt == "refresh_token":
            out, why = await anyio.to_thread.run_sync(store.refresh, str(form.get("refresh_token", "")),
                                                      str(cid) if cid else None)
            if out is None:
                if why and "reused" in why:
                    _event("reuse", app=str(cid) if cid else None)
                return _oauth_error(400, "invalid_grant", why or "invalid refresh token")
            _event("refresh")
            return JSONResponse(out, headers=NO_STORE)
        return _oauth_error(400, "unsupported_grant_type", str(gt))

    @srv.custom_route("/revoke", methods=["POST"])
    async def revoke(request: Request) -> Response:
        try:
            form = await request.form()
        except Exception:
            return _oauth_error(400, "invalid_request", "form body required")
        gid = await anyio.to_thread.run_sync(store.revoke_token, str(form.get("token", "")))
        if gid:
            log.info("connection revoked by its client: grant %s", gid)
            _event("revoked", grant=gid)
        return Response(status_code=200, headers=NO_STORE)


__all__ = ["APPROVAL_SECONDS", "StoreVerifier", "add_routes", "as_metadata", "grant_of", "pkce_ok", "redirect_ok",
           "resolve_client"]
