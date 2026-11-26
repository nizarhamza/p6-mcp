"""Token verification for the remote access server.

The verifier answers the SDK's question "is this bearer token valid, and for what?" with an AccessToken whose
claims carry the id of the OAuth connection (grant) it belongs to; the remote tool wrapper binds confirmation
tokens to that grant (p6mcp.confirm.OWNER).

Milestone M1 has no authorization server yet (M2 adds it, with its token store): NoGrants accepts no token, so the
remote access server runs and answers discovery, but no client can call a tool. Tests use their own verifier.
"""

from __future__ import annotations

from typing import Optional

from mcp.server.auth.provider import AccessToken


def grant_of(token: Optional[AccessToken]) -> Optional[str]:
    """The OAuth connection a token belongs to (its client id when the verifier gives no grant)."""
    if token is None:
        return None
    claims = getattr(token, "claims", None) or {}  # mcp 1.x AccessToken has no claims
    return str(claims.get("grant") or token.client_id)


class NoGrants:
    """Accepts no token (until the authorization server of M2)."""

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        return None
