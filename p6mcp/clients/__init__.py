"""MCP client registration: one adapter per client (p6mcp.clients.adapters), shared logic in base.

    from p6mcp import clients
    for st in clients.scan(): print(st.name, st.state)
    clients.get("cursor").register()

MCP has no local discovery: a client only reads its own config file, so "detection" means adding
the p6mcp entry to that file. New clients: subclass base.Adapter and add it to adapters.ADAPTERS.
"""

from __future__ import annotations

import json
from typing import Iterable, Optional

from p6mcp.clients.adapters import ADAPTERS, BY_ID
from p6mcp.clients.base import (ERROR, NOT_INSTALLED, NOT_REGISTERED, OUTDATED, REGISTERED, SERVER_KEY, Adapter,
                                Launch, Status, launch)
from p6mcp.clients.base import _running_names

__all__ = ["ADAPTERS", "BY_ID", "Adapter", "Launch", "Status", "launch", "scan", "get", "snippet", "http_url",
           "SERVER_KEY", "NOT_INSTALLED", "NOT_REGISTERED", "REGISTERED", "OUTDATED", "ERROR"]


def get(client_id: str) -> Adapter:
    try:
        return BY_ID[client_id]
    except KeyError:
        raise ValueError(f"Unknown client {client_id!r}; known: {', '.join(BY_ID)}") from None


def scan(ids: Optional[Iterable[str]] = None, ln: Optional[Launch] = None) -> list[Status]:
    """Status of every client (or of ids), with whether each one is running."""
    running = _running_names()
    ln = ln or launch()
    return [get(i).status(ln, running) for i in (ids or BY_ID)]


def http_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp"


def snippet(kind: str = "stdio", port: int = 39217, token: Optional[str] = None,
            ln: Optional[Launch] = None) -> str:
    """Config text for a client P6 MCP has no adapter for. kind: stdio, http (mcpServers notation,
    used by most clients) or vscode (VS Code servers notation, stdio)."""
    ln = ln or launch()
    if kind == "stdio":
        body = {"mcpServers": {SERVER_KEY: {"command": ln.command, "args": ln.args}}}
    elif kind == "http":
        body = {"mcpServers": {SERVER_KEY: {"url": http_url(port),
                                            "headers": {"Authorization": f"Bearer {token or '<access token>'}"}}}}
    elif kind == "vscode":
        body = {"servers": {SERVER_KEY: {"type": "stdio", "command": ln.command, "args": ln.args}}}
    else:
        raise ValueError("kind must be stdio, http or vscode")
    return json.dumps(body, indent=2)
