"""Loopback listener of the browser page's Connect button (tray app, 127.0.0.1:remote_pair_port).

The connection page (/authorize/wait) shows its pairing code and, in the browser that started the request (the one
holding its browser key cookie), a Connect button. The button posts {id, code, key} from the page to this listener
on the same PC; the tray then fills the code in the approval window, where the user still clicks Approve.

Why it keeps the protection of the typed code: the post must come from a page of the user's own public address
(Origin checked; browsers set it, page scripts cannot), the page only offers the button to the browser that started
the request, and a browser can only reach 127.0.0.1 of its own PC. A request started by someone else runs in their
browser, on their PC: its Connect button reaches their loopback address, never this one. A link sent to the user
opens the page without the browser key cookie: no button there. The Host header is checked too (DNS rebinding).
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

log = logging.getLogger("p6mcp.remote.pairing")
PATH = "/p6mcp/pair"
MAX_BODY = 1024
DRAIN_MAX = 64 * 1024  # a refused body up to this size is still read before the answer
POLL_S = 0.05  # serve_forever poll: stop() returns within it (the 0.5 s default held the tray's quit)


class _ExclusiveServer(ThreadingHTTPServer):
    """No address reuse: on Windows SO_REUSEADDR lets a second program bind the same port and take the requests."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class PairListener:
    def __init__(self, port: int, origin: Callable[[], Optional[str]], hint: Callable[[str, str, str], Optional[dict]],
                 on_pair: Callable[[dict, str], None], host: str = "127.0.0.1"):
        """origin() -> the allowed Origin (https://<public host>) or None; hint(id, code, key) -> the waiting request
        or None (Store.local_hint); on_pair(request, code) runs on the listener's thread."""
        self.port, self.origin, self.hint, self.on_pair, self.host = port, origin, hint, on_pair, host
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self.httpd is not None

    def start(self) -> bool:
        if self.httpd is not None:
            return True
        try:
            self.httpd = _ExclusiveServer((self.host, self.port), _handler(self))
        except OSError as e:  # port taken: the page falls back to the typed code
            self.error = f"{type(e).__name__}: {e}"
            log.warning("Connect button listener not started on %s:%s: %s", self.host, self.port, e)
            return False
        self.error = None
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(POLL_S,), name="p6mcp-pair", daemon=True)
        self.thread.start()
        log.info("Connect button listener on %s:%s", self.host, self.port)
        return True

    def stop(self) -> None:
        httpd, self.httpd = self.httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        self.thread = None


def _handler(owner: PairListener):
    class Handler(BaseHTTPRequestHandler):
        server_version = "p6mcp-pair"
        sys_version = ""
        timeout = 10  # socket timeout: a client that never sends its declared body does not hold a thread

        def log_message(self, fmt, *args):  # no request log on stderr
            pass

        def _allowed_origin(self) -> Optional[str]:
            want = owner.origin()
            got = self.headers.get("Origin")
            return want if want and got == want else None

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in (f"127.0.0.1:{owner.port}", f"localhost:{owner.port}")

        def _send(self, status: int, body: Optional[dict], origin: Optional[str], preflight: bool = False) -> None:
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                if preflight:
                    self.send_header("Access-Control-Allow-Methods", "POST")
                    self.send_header("Access-Control-Allow-Headers", "content-type")
                    self.send_header("Access-Control-Allow-Private-Network", "true")
                    self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Cache-Control", "no-store")
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def do_OPTIONS(self):
            origin = self._allowed_origin()
            if self.path != PATH or not origin or not self._host_ok():
                self._send(403, None, None)
                return
            self._send(204, None, origin, preflight=True)

        def _body(self) -> Optional[bytes]:
            """The request body, read before any answer, refusals included: closing a socket that still holds
            unread data sends a reset, which can reach the client before the answer (a connection abort instead
            of the 403 or 400). None = no usable body (missing, bad length, above MAX_BODY: still read and dropped
            up to DRAIN_MAX; a larger one is left unread, the reset is then the answer)."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if not 0 < n <= DRAIN_MAX:
                return None
            try:
                data = self.rfile.read(n)
            except OSError:  # timeout or reset: nothing more to read
                self.close_connection = True
                return None
            return data if n <= MAX_BODY and len(data) == n else None

        def do_POST(self):
            body = self._body()
            origin = self._allowed_origin()
            if self.path != PATH or not origin or not self._host_ok():
                self._send(403, {"ok": False, "error": "forbidden"}, None)
                return
            if body is None:
                self._send(400, {"ok": False, "error": "bad request"}, origin)
                return
            try:
                msg = json.loads(body)
            except ValueError:
                msg = None
            if not isinstance(msg, dict) or not all(isinstance(msg.get(k), str) for k in ("id", "code", "key")):
                self._send(400, {"ok": False, "error": "bad request"}, origin)
                return
            try:
                req = owner.hint(msg["id"], msg["code"], msg["key"])
            except Exception:
                log.exception("Connect button check failed")
                req = None
            if req is None:
                self._send(404, {"ok": False, "error": "no matching request"}, origin)
                return
            try:
                owner.on_pair(req, req["pair"])
            except Exception:
                log.exception("Connect button handler failed")
                self._send(500, {"ok": False, "error": "app error"}, origin)
                return
            self._send(200, {"ok": True}, origin)

        def do_GET(self):
            self._send(404, None, None)

    return Handler


__all__ = ["PATH", "PairListener"]
