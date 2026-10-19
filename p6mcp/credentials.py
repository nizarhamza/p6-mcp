"""Secrets in the Windows Credential Manager (through keyring), never in config.json.

Entries live under the service name "P6MCP" (P6MCP_SECRET_SERVICE overrides it, for tests):
  http_token          bearer token of the localhost HTTP transport
  db:<profile name>   password of a database connection profile (server backends, later)
"""

from __future__ import annotations

import os
import secrets as _rand  # stdlib (this module is p6mcp.credentials)
from typing import Optional

SERVICE = "P6MCP"
HTTP_TOKEN = "http_token"


class SecretStoreError(RuntimeError):
    """The Credential Manager cannot be reached."""


def _service() -> str:
    return os.environ.get("P6MCP_SECRET_SERVICE") or SERVICE


def _keyring():
    try:
        import keyring
        from keyring.errors import KeyringError  # noqa: F401
    except ImportError as e:  # pragma: no cover: keyring is a hard dependency
        raise SecretStoreError("keyring is not installed: secrets cannot be stored.") from e
    return keyring


def get(name: str) -> Optional[str]:
    kr = _keyring()
    try:
        return kr.get_password(_service(), name)
    except Exception as e:  # keyring raises backend-specific errors
        raise SecretStoreError(f"Credential Manager read failed for {name!r}: {e}") from e


def set(name: str, value: str) -> None:  # noqa: A001: mirrors keyring's vocabulary
    if not isinstance(value, str) or not value:
        raise ValueError("A secret must be non-empty text.")
    kr = _keyring()
    try:
        kr.set_password(_service(), name, value)
    except Exception as e:
        raise SecretStoreError(f"Credential Manager write failed for {name!r}: {e}") from e


def delete(name: str) -> bool:
    """True when an entry was removed, False when there was none."""
    kr = _keyring()
    from keyring.errors import PasswordDeleteError
    try:
        kr.delete_password(_service(), name)
        return True
    except PasswordDeleteError:
        return False
    except Exception as e:
        raise SecretStoreError(f"Credential Manager delete failed for {name!r}: {e}") from e


def db_key(profile: str) -> str:
    return f"db:{profile}"


def new_token() -> str:
    """256 bits, URL-safe."""
    return _rand.token_urlsafe(32)


def http_token(create: bool = True) -> Optional[str]:
    """The HTTP bearer token, created and stored on first use when create=True."""
    tok = get(HTTP_TOKEN)
    if tok is None and create:
        tok = new_token()
        set(HTTP_TOKEN, tok)
    return tok


def regenerate_http_token() -> str:
    tok = new_token()
    set(HTTP_TOKEN, tok)
    return tok


def mask(value: Optional[str]) -> str:
    """For display: the last 4 characters only."""
    if not value:
        return ""
    return "•" * 8 + value[-4:]
