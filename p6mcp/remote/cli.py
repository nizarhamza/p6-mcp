"""p6mcp-server remote ...: decide connection requests and manage connections from the command line.

    p6mcp-server remote pending                 requests waiting for a decision (code, app, time left)
    p6mcp-server remote approve CODE [--edits]  approve the request showing CODE (--edits: allow edits too)
    p6mcp-server remote deny CODE               refuse it
    p6mcp-server remote connections             approved connections
    p6mcp-server remote revoke ID               end one connection (ID from connections)
    p6mcp-server remote revoke-all              end every connection

The Settings app does the same from its Remote access page and the tray; this is for setup and support.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

from p6mcp.remote.store import Store


def _when(ts: Optional[int]) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"


def main(argv=None, store: Optional[Store] = None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="p6mcp-server remote")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pending")
    a = sub.add_parser("approve")
    a.add_argument("code")
    a.add_argument("--edits", action="store_true")
    sub.add_parser("deny").add_argument("code")
    sub.add_parser("connections")
    sub.add_parser("revoke").add_argument("id")
    sub.add_parser("revoke-all")
    args = ap.parse_args(argv)
    st = store or Store()
    if args.cmd == "pending":
        rows = st.pending_list()
        for r in rows:
            print(f"{r['pair']}  {r['client_name']} ({r['redirect_uri'].split('/')[2]})  {r['expires_in']} s left",
                  file=out)
        if not rows:
            print("No connection request is waiting.", file=out)
        return 0
    if args.cmd in ("approve", "deny"):
        r = st.decide(args.code, approve=args.cmd == "approve", edits=getattr(args, "edits", False))
        if r is None:
            print("No waiting request shows that code (it may have expired after 5 minutes).", file=out)
            return 1
        print(f"{r['status']}: {r['client_name']}, access {' '.join(r['scopes'])}", file=out)
        return 0
    if args.cmd == "connections":
        rows = st.grants()
        for g in rows:
            print(f"{g['id']}  {g['client_name']} ({g['redirect_host']})  {' '.join(g['scopes'])}  approved "
                  f"{_when(g['created'])}, last used {_when(g['last_used'])}", file=out)
        if not rows:
            print("No connection.", file=out)
        return 0
    if args.cmd == "revoke":
        ok = st.revoke(args.id)
        print("Connection ended." if ok else "No active connection with that id.", file=out)
        return 0 if ok else 1
    n = st.revoke_all()
    print(f"{n} connection(s) ended.", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
