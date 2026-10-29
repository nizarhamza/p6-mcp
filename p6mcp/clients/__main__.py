"""Command line for client registration (used by the installer):

    p6mcp-server clients list [--json]
    p6mcp-server clients register cursor vscode ...   (or --detected: every installed client)
    p6mcp-server clients unregister cursor ...
    p6mcp-server clients snippet [stdio|http|vscode]
    p6mcp-server clients skills install|remove cursor ...   (or --detected: every client with a skills folder;
                                                              "commands", the name before 1.8, still works)

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
    for name in ("skills", "commands"):  # commands: P6 MCP 1.4 to 1.7 (installer of an older version)
        cm = sub.add_parser(name, help="P6 skills in the clients' skills folders")
        cm.add_argument("action", choices=("install", "remove"))
        cm.add_argument("ids", nargs="*")
        cm.add_argument("--detected", action="store_true", help="every installed client with a skills folder")
    a = ap.parse_args(argv)

    data = config.read_file()[0] or config.DEFAULTS
    if a.cmd in ("skills", "commands"):
        return _skills(ap, a, data)
    http = clients.target(data)
    if a.cmd == "list":
        sts = clients.scan(http=http)
        if a.json:
            print(json.dumps([s.as_dict() for s in sts], indent=2))
        else:
            for s in sts:
                print(f"{s.client:16} {s.state:15} {s.path}" + (f"  ({s.message})" if s.message else ""))
        return 0
    if a.cmd == "snippet":
        print(clients.snippet(a.kind, port=data.get("http_port", 39217)))
        return 0

    ids = list(a.ids)
    if a.detected:
        ids += [s.client for s in clients.scan(http=http) if s.state != clients.NOT_INSTALLED and s.client not in ids]
    if not ids:
        ap.error("name at least one client, or --detected")
    ok = True
    for cid in ids:
        try:
            ad = clients.get(cid)
            res = ad.register(http=http) if a.cmd == "register" else ad.unregister()
            print(f"{cid}: {'updated' if res['changed'] else 'unchanged'} {res['path']}"
                  + (f" (backup {res['backup']})" if res["backup"] else ""))
        except (ValueError, files.ClientFileError, JsoncError, OSError) as e:
            ok = False
            print(f"{cid}: FAILED {e}", file=sys.stderr)
    if ok and a.cmd == "register":
        print("Restart each client so it loads MCP for Primavera P6.")
    return 0 if ok else 1


def _skills(ap, a, data) -> int:
    ids = list(a.ids)
    if a.detected:
        ids += [ad.id for ad in clients.ADAPTERS
                if ad.skills_dir() and ad.installed() and ad.id not in ids]
    if not ids:
        ap.error("name at least one client, or --detected")
    ok = True
    for cid in ids:
        try:
            ad = clients.get(cid)
            if a.action == "install":
                res = ad.install_skills(data.get("tool_profile", "basic"), bool(data.get("allow_writes")))
                print(f"{cid}: {len(res['written'])} written, {len(res['removed'])} removed in {res['folder']}"
                      + (f" (kept your own skills: {', '.join(res['skipped'])})" if res["skipped"] else ""))
            else:
                res = ad.remove_skills()
                print(f"{cid}: {len(res['removed'])} removed" + (f" from {res['folder']}" if res["folder"] else ""))
        except (ValueError, files.ClientFileError, OSError) as e:
            ok = False
            print(f"{cid}: FAILED {e}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
