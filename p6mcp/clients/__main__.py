"""Command line for client registration (used by the installer):

    p6mcp-server clients list [--json]
    p6mcp-server clients register cursor vscode ...   (or --detected: every installed client)
    p6mcp-server clients unregister cursor ...
    p6mcp-server clients snippet [stdio|http|vscode]

Exit code 0 when every requested action succeeded, 1 otherwise. Output is plain ASCII.
"""

from __future__ import annotations

import argparse
import json
import sys

from p6mcp import clients, config
from p6mcp.clients import files
from p6mcp.jsonc import JsoncError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="p6mcp-server clients")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--json", action="store_true")
    for name in ("register", "unregister"):
        p = sub.add_parser(name)
        p.add_argument("ids", nargs="*")
        p.add_argument("--detected", action="store_true", help="every installed client")
    sn = sub.add_parser("snippet")
    sn.add_argument("kind", nargs="?", default="stdio", choices=("stdio", "http", "vscode"))
    a = ap.parse_args(argv)

    if a.cmd == "list":
        sts = clients.scan()
        if a.json:
            print(json.dumps([s.as_dict() for s in sts], indent=2))
        else:
            for s in sts:
                print(f"{s.client:16} {s.state:15} {s.path}" + (f"  ({s.message})" if s.message else ""))
        return 0
    if a.cmd == "snippet":
        data = config.read_file()[0] or config.DEFAULTS
        print(clients.snippet(a.kind, port=data.get("http_port", 39217)))
        return 0

    ids = list(a.ids)
    if a.detected:
        ids += [s.client for s in clients.scan() if s.state != clients.NOT_INSTALLED and s.client not in ids]
    if not ids:
        ap.error("name at least one client, or --detected")
    ok = True
    for cid in ids:
        try:
            ad = clients.get(cid)
            res = ad.register() if a.cmd == "register" else ad.unregister()
            print(f"{cid}: {'updated' if res['changed'] else 'unchanged'} {res['path']}"
                  + (f" (backup {res['backup']})" if res["backup"] else ""))
        except (ValueError, files.ClientFileError, JsoncError, OSError) as e:
            ok = False
            print(f"{cid}: FAILED {e}", file=sys.stderr)
    if ok and a.cmd == "register":
        print("Restart each client so it loads P6 MCP.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
