"""Entry point of p6mcp-server: python -m p6mcp

    p6mcp-server                    stdio transport (started by an MCP client)
    p6mcp-server --http [--port N]  local Streamable HTTP transport (started by the tray app)
    p6mcp-server --remote [--port N]  remote access server for claude.ai and ChatGPT (started by the tray app)
    p6mcp-server clients ...        client registration (see p6mcp.clients.__main__)
    p6mcp-server setup ...          installer helpers (see p6mcp.setup_cli)
    p6mcp-server remote ...         remote access connections: approve, deny, revoke (see p6mcp.remote.cli)
"""


def main() -> None:
    import multiprocessing
    import sys
    # A worker process of p6_schedule_risk (spawn) starts this exe again when frozen: freeze_support runs the
    # worker and exits there, so a worker never starts a second server. A no-op when not frozen.
    multiprocessing.freeze_support()
    args = sys.argv[1:]
    if args[:1] == ["clients"]:  # registration commands (installer)
        from p6mcp.clients.__main__ import main as clients_main
        sys.exit(clients_main(args[1:]))
    if args[:1] == ["remote"]:
        from p6mcp.remote.cli import main as remote_cli
        sys.exit(remote_cli(args[1:]))
    if args[:1] == ["setup"]:
        from p6mcp.setup_cli import main as setup_main
        sys.exit(setup_main(args[1:]))
    from p6mcp import runtime
    if runtime.setup_running():  # the clients and setup commands above run inside setup itself
        print("MCP for Primavera P6 is being updated: restart the MCP client (or the server) in a minute.",
              file=sys.stderr)
        sys.exit(1)
    if "--remote" in args:
        from p6mcp.remote.server import main as remote_main
        sys.exit(remote_main(args))
    if "--http" in args:
        from p6mcp.http_server import main as http_main
        sys.exit(http_main(args))
    runtime.setup_logging("stdio-server")  # never log to stdout: it carries the protocol
    from p6mcp.server import mcp
    mcp.run()


if __name__ == "__main__":
    main()
