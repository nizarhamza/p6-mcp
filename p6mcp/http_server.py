"""Streamable HTTP transport, for clients that need HTTP or to share one server between clients.

    p6mcp-server --http [--port N]

Security: bound to 127.0.0.1 only; every request must carry "Authorization: Bearer <token>" (the
token lives in the Windows Credential Manager, see p6mcp.credentials); the Host header must name
127.0.0.1 or localhost on this port and an Origin header, when a browser sends one, must be that
same local origin (DNS rebinding protection). The MCP endpoint is /mcp.
"""

from __future__ import annotations

import argparse
import hmac
import logging
import sys
from typing import Optional

from p6mcp import config, credentials, runtime

HOST = "127.0.0.1"
PATH = "/mcp"
log = logging.getLogger("p6mcp.http")


class Guard:
    """ASGI middleware in front of the SDK app: Host, Origin and bearer token on every HTTP request."""

    def __init__(self, app, port: int, token: str):
        if not token:
            raise ValueError("an access token is required")
        self.app = app
        self.token = token.encode("utf-8")
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # lifespan: the SDK's session manager starts and stops here
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if headers.get("host", "").lower() not in self.hosts:
            await _reply(send, 421, "Invalid Host header")
            return
        origin = headers.get("origin")
        if origin is not None and origin.lower() not in self.origins:
            await _reply(send, 403, "Invalid Origin header")
            return
        auth = headers.get("authorization", "")
        scheme, _, given = auth.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip().encode("utf-8"), self.token):
            await _reply(send, 401, "Missing or invalid access token", {"www-authenticate": 'Bearer realm="p6mcp"'})
            return
        await self.app(scope, receive, send)


async def _reply(send, status: int, text: str, extra: Optional[dict] = None) -> None:
    body = text.encode("utf-8")
    headers = [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]
    headers += [(k.encode(), v.encode()) for k, v in (extra or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def build_app(port: int, token: str):
    """The guarded ASGI app (the SDK's own Host/Origin protection stays on as a second layer)."""
    from mcp.server.transport_security import TransportSecuritySettings

    from p6mcp.server import mcp
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=sorted({f"127.0.0.1:{port}", f"localhost:{port}"}),
                                         allowed_origins=sorted({f"http://127.0.0.1:{port}",
                                                                 f"http://localhost:{port}"}))
    app = mcp.streamable_http_app(streamable_http_path=PATH, transport_security=security, host=HOST)
    return Guard(app, port, token)


def make_server(port: int, token: str):
    import uvicorn
    cfg = uvicorn.Config(build_app(port, token), host=HOST, port=port, log_config=None, access_log=False,
                         lifespan="on", timeout_graceful_shutdown=3)
    return uvicorn.Server(cfg)


def serve(port: Optional[int] = None) -> int:
    runtime.setup_logging("http-server")
    st = config.load()
    port = int(port or st.http_port)
    try:
        token = credentials.http_token(create=True)
    except credentials.SecretStoreError as e:
        runtime.write_status("error", port, str(e))
        log.error("no access token: %s", e)
        return 2
    from p6mcp import settings_logic
    if not settings_logic.port_free(port):
        msg = f"Port {port} is already in use: choose another port in P6 MCP Settings (Server)."
        runtime.write_status("error", port, msg)
        log.error(msg)
        return 3
    server = make_server(port, token)
    runtime.write_status("starting", port)
    log.info("HTTP server on http://%s:%s%s", HOST, port, PATH)

    orig_startup = server.startup

    async def startup(*a, **k):
        await orig_startup(*a, **k)
        if server.started:
            runtime.write_status("running", port, url=f"http://{HOST}:{port}{PATH}")
    server.startup = startup
    try:
        server.run()
    except Exception as e:  # reported to the tray app through the status file
        runtime.write_status("error", port, f"{type(e).__name__}: {e}")
        log.exception("HTTP server failed")
        return 1
    runtime.write_status("stopped", port)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="p6mcp-server --http")
    ap.add_argument("--http", action="store_true")
    ap.add_argument("--port", type=int)
    a = ap.parse_args(argv)
    return serve(a.port)


if __name__ == "__main__":
    sys.exit(main())
