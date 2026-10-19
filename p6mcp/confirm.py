"""Confirmation tokens of the two-step write flow.

1. A write tool called with dry_run=true (the default) runs its dry run; the server issues a token
   bound to the tool, its arguments (canonical JSON), the database file and the dry-run plan.
2. The call that applies (dry_run=false) must carry that token and the same arguments. While it runs,
   the tool rebuilds its plan inside the write transaction; a plan that differs from the previewed
   one (the database changed in between) is refused before anything is committed.

Tokens live in the server process (a stdio server keeps the same process for both calls), expire
after TTL_SECONDS and are single use: consumed once the change is applied or the plan is refused.
A refusal that writes nothing (P6 still open, for example) keeps the token for a retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

TTL_SECONDS = 15 * 60
MAX_TOKENS = 200  # oldest dropped beyond this (a client looping on dry runs cannot grow memory)
PLAN_SKIP = ("Backup: ", "Safety backup: ")  # plan lines that only exist when applying


def canonical(params: dict) -> str:
    return json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plan_digest(plan: Iterable[Any]) -> str:
    return digest("\n".join(str(p) for p in plan if not str(p).startswith(PLAN_SKIP)))


def db_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)) if path else ""


@dataclass
class Grant:
    token: str
    tool: str
    params: str       # digest of the canonical arguments
    db: str
    plan: Optional[str]
    expires: float


class TokenError(ValueError):
    """The apply call is refused: missing, unknown, expired or mismatched token."""


class TokenStore:
    def __init__(self, ttl: float = TTL_SECONDS):
        self.ttl = ttl
        self._grants: dict[str, Grant] = {}
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        for k in [k for k, g in self._grants.items() if g.expires <= now]:
            del self._grants[k]
        while len(self._grants) >= MAX_TOKENS:
            del self._grants[min(self._grants, key=lambda k: self._grants[k].expires)]

    def issue(self, tool: str, params: dict, db_path: str, plan: Optional[str]) -> Grant:
        now = time.monotonic()
        g = Grant("p6c_" + secrets.token_urlsafe(18), tool, digest(canonical(params)), db_key(db_path), plan,
                  now + self.ttl)
        with self._lock:
            self._purge(now)
            self._grants[g.token] = g
        return g

    def check(self, tool: str, params: dict, db_path: str, token: Optional[str]) -> Grant:
        """The grant matching this apply call, else TokenError (the grant is left as it is)."""
        if not token:
            raise TokenError(f"confirmation_token is required to apply. Call {tool} with dry_run=true first, show "
                             "the plan to the user, then call again with dry_run=false and the token it returned.")
        with self._lock:
            g = self._grants.get(token)
            if g is not None and g.expires <= time.monotonic():
                del self._grants[token]
                raise TokenError("This confirmation_token has expired (valid 15 minutes). Run the dry run again.")
        if g is None:
            raise TokenError("Unknown or already used confirmation_token (tokens are single use and are lost when "
                             "the server restarts). Run the dry run again.")
        if g.tool != tool:
            raise TokenError(f"This confirmation_token was issued for {g.tool}, not {tool}.")
        if g.db != db_key(db_path):
            raise TokenError("This confirmation_token was issued for another database. Run the dry run again.")
        if g.params != digest(canonical(params)):
            raise TokenError("The arguments differ from the dry run this token was issued for. Call the tool with "
                             "exactly the arguments of the dry run, or run a new dry run.")
        return g

    def consume(self, token: str) -> None:
        with self._lock:
            self._grants.pop(token, None)

    def __len__(self) -> int:
        return len(self._grants)
