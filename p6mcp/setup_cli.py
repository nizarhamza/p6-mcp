"""Commands the installer runs (plain ASCII output, exit code 0 on success):

    p6mcp-server setup detected-clients       ids of the MCP clients installed on this PC, one per line
    p6mcp-server setup startup on|off [--minimized|--normal]
                                              run at Windows startup, in config.json and the registry
"""

from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="p6mcp-server setup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detected-clients")
    st = sub.add_parser("startup")
    st.add_argument("state", choices=("on", "off"))
    st.add_argument("--normal", action="store_true", help="open the window instead of starting in the tray")
    a = ap.parse_args(argv)

    if a.cmd == "detected-clients":
        from p6mcp import clients
        for s in clients.scan():
            if s.state != clients.NOT_INSTALLED:
                print(s.client)
        return 0

    from p6mcp import config, startup
    try:
        data = config.read_file()[0] or dict(config.DEFAULTS)
    except config.ConfigError as e:  # never overwrite a file the user may want to repair
        print(f"config.json is unreadable, left unchanged: {e}", file=sys.stderr)
        return 1
    data = dict(data, run_at_startup=a.state == "on", start_minimized=not a.normal)
    config.save(data)
    cmd = startup.sync(data)
    print(f"startup: {cmd or 'off'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
