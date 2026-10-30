"""Local preview of site/ as Cloudflare Pages serves it: the response headers of site/_headers (Content-Security-Policy
included) and the 404 page for unknown paths. Nothing is cached, so a rebuild shows on reload.

    python tools/serve_site.py [port]    # http://127.0.0.1:8642 by default
"""

from __future__ import annotations

import fnmatch
import http.server
import os
import sys

SITE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site")


def header_rules():
    """[(path pattern, [(name, value)])] of site/_headers."""
    rules, cur = [], None
    with open(os.path.join(SITE, "_headers"), encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line[0].isspace():
                cur = (line.strip(), [])
                rules.append(cur)
            elif cur:
                name, _, value = line.strip().partition(":")
                cur[1].append((name.strip(), value.strip()))
    return rules


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".webp": "image/webp",
                      ".woff2": "font/woff2", ".json": "application/json", ".js": "text/javascript"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SITE, **kwargs)

    def end_headers(self):
        path = self.path.split("?")[0]
        for pattern, headers in header_rules():
            if fnmatch.fnmatch(path, pattern):
                for name, value in headers:
                    if name.lower() != "cache-control":
                        self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_error(self, code, message=None, explain=None):
        if code != 404:
            return super().send_error(code, message, explain)
        with open(os.path.join(SITE, "404.html"), "rb") as f:
            body = f.read()
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8642
    print(f"http://127.0.0.1:{port}")
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
