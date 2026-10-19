"""Entry point of p6mcp-server: python -m p6mcp (stdio transport)."""


def main() -> None:
    from p6mcp.server import mcp
    mcp.run()


if __name__ == "__main__":
    main()
