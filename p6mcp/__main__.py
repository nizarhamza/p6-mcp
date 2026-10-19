"""Entry point of p6mcp-server: python -m p6mcp (stdio transport)."""


def main() -> None:
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "clients":  # registration commands (installer)
        from p6mcp.clients.__main__ import main as clients_main
        sys.exit(clients_main(sys.argv[2:]))
    from p6mcp.server import mcp
    mcp.run()


if __name__ == "__main__":
    main()
