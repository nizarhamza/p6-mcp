"""Remote access setup as the Settings app applies it (no Qt here).

apply_setup(): the result of cloudflare.setup() becomes settings (mode, public host, ids in remote_cf) and two
Credential Manager entries (tunnel token, relay secret). clear(): the reverse, after cloudflare.remove() or when the
user gives up the setup. Both return a new config dict; the caller saves it (config.save).
"""

from __future__ import annotations

import copy
from typing import Optional

from p6mcp import credentials
from p6mcp.remote import RELAY_SECRET_KEY
from p6mcp.remote.cloudflare import SetupResult
from p6mcp.remote.supervise import TUNNEL_TOKEN_KEY


def apply_setup(data: dict, result: SetupResult) -> dict:
    """Store the secrets, return the config with the new public address (a new address voids old connections)."""
    credentials.set(TUNNEL_TOKEN_KEY, result.tunnel_token)
    if result.relay_secret:
        credentials.set(RELAY_SECRET_KEY, result.relay_secret)
    else:
        credentials.delete(RELAY_SECRET_KEY)
    out = copy.deepcopy(data)
    out.update(remote_mode=result.mode, remote_public_host=result.public_host, remote_cf=result.ids())
    return out


def relay_secret() -> Optional[str]:
    """The current relay secret (a repair keeps it, so the running relay and server stay in step)."""
    try:
        return credentials.get(RELAY_SECRET_KEY)
    except credentials.SecretStoreError:
        return None


def clear(data: dict) -> dict:
    """Forget the Cloudflare setup on this PC: secrets removed, remote access off, address and ids cleared."""
    for key in (TUNNEL_TOKEN_KEY, RELAY_SECRET_KEY):
        try:
            credentials.delete(key)
        except credentials.SecretStoreError:
            pass
    out = copy.deepcopy(data)
    out.update(remote_enabled=False, remote_public_host=None, remote_cf=None)
    return out
