"""Remote access for claude.ai and ChatGPT (3.2, docs/REMOTE_ACCESS_PLAN.md).

A separate server process (p6mcp-server --remote, 127.0.0.1 only) offers the P6 tools to remote MCP clients that
reach it through the user's own Cloudflare account (a relay Worker on their free workers.dev address, or a host
name on their own domain) and an outbound tunnel from this PC. Clients authenticate with OAuth tokens that this
PC issues after the user approves the connection here.

    p6mcp.remote.server   the ASGI app, the guard in front of it and the remote tool set
    p6mcp.remote.shaping  what a remote result may carry: local paths redacted, size budget
    p6mcp.remote.auth     token verification (the authorization server comes with it)
"""

PATH = "/mcp"
READ_SCOPE, WRITE_SCOPE = "p6:read", "p6:write"
RELAY_SECRET_KEY = "remote:relay_secret"  # Credential Manager: shared with the relay Worker (relay mode)
RELAY_HEADER = "x-p6mcp-relay"
CLIENT_IP_HEADER = "x-p6mcp-client-ip"


def public_url(host: str) -> str:
    """https://<host>, the issuer and the base of the resource identifier."""
    return f"https://{host}"


def resource_url(host: str) -> str:
    """The resource identifier: the connector URL the user pastes in claude.ai or ChatGPT, exactly."""
    return public_url(host) + PATH
