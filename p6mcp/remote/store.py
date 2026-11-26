"""The OAuth state of remote access: %LOCALAPPDATA%\\P6MCP\\remote\\auth.db (SQLite, WAL).

Shared by the remote access server (which issues and checks tokens) and the tray app or the command line (which
approve, deny or revoke). Tokens and authorization codes are stored as SHA-256 hashes only: a copy of the file
gives no usable token.

Tables
  clients   clients registered with DCR (CIMD clients are not stored, their document is cached in cimd)
  cimd      client metadata documents fetched (client_id URL -> JSON, fetched at)
  pending   connection requests waiting for the user's decision on the PC (pairing code; browser_key = hash of the
            key the starting browser keeps in a cookie, for the page's Connect button)
  codes     authorization codes (60 s, single use)
  grants    approved connections: client, scopes, resource, created, last used, revoked
  tokens    access (ACCESS_TTL) and refresh (REFRESH_TTL, rotated, single use) tokens of a grant
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from typing import Optional

from p6mcp import config

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 60
APPROVAL_SECONDS = 300
MAX_OPEN = 3  # connection requests waiting at once (a flood of approval windows is refused at /authorize)
CLIENT_KEEP = 30 * 86400  # DCR clients without a live connection are forgotten after this
CIMD_CACHE = 86400
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I: read on one screen, typed on another
SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, name TEXT NOT NULL, redirect_uris TEXT NOT NULL,
                                    created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cimd (client_id TEXT PRIMARY KEY, doc TEXT NOT NULL, fetched INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, pair TEXT NOT NULL, client_id TEXT NOT NULL,
                                    client_name TEXT NOT NULL, via TEXT NOT NULL, redirect_uri TEXT NOT NULL,
                                    state TEXT, challenge TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT NOT NULL,
                                    created INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                                    decided INTEGER, browser_key TEXT);
CREATE TABLE IF NOT EXISTS codes (hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL, challenge TEXT NOT NULL,
                                  redirect_uri TEXT NOT NULL, client_id TEXT NOT NULL, exp INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, client_name TEXT NOT NULL,
                                   via TEXT NOT NULL, redirect_host TEXT, scopes TEXT NOT NULL, resource TEXT NOT NULL,
                                   created INTEGER NOT NULL, last_used INTEGER, revoked INTEGER);
CREATE TABLE IF NOT EXISTS tokens (hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL, kind TEXT NOT NULL,
                                   exp INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS tokens_grant ON tokens (grant_id);
"""


def default_path() -> str:
    return os.path.join(config.localdata_dir(), "remote", "auth.db")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> int:
    return int(time.time())


class TooManyRequests(RuntimeError):
    """MAX_OPEN connection requests are already waiting for a decision."""


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = path or default_path()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with closing(self._con()) as con:
            con.executescript(SCHEMA)
            if "browser_key" not in {r[1] for r in con.execute("PRAGMA table_info(pending)")}:  # 3.2.0 store
                con.execute("ALTER TABLE pending ADD COLUMN browser_key TEXT")

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=5, isolation_level=None)  # autocommit; BEGIN IMMEDIATE when needed
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        return con

    # ------------------------------------------------------------- clients
    def add_client(self, name: str, redirect_uris: list[str]) -> str:
        cid = "p6c-" + uuid.uuid4().hex
        with closing(self._con()) as con:
            con.execute("DELETE FROM clients WHERE created < ? AND client_id NOT IN "
                        "(SELECT client_id FROM grants WHERE revoked IS NULL)", (_now() - CLIENT_KEEP,))
            con.execute("INSERT INTO clients VALUES (?, ?, ?, ?)", (cid, name, json.dumps(redirect_uris), _now()))
        return cid

    def client(self, client_id: str) -> Optional[dict]:
        with closing(self._con()) as con:
            r = con.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        if r is None:
            return None
        return {"client_id": r["client_id"], "client_name": r["name"], "redirect_uris": json.loads(r["redirect_uris"]),
                "via": "dcr"}

    def cimd_get(self, client_id: str) -> Optional[dict]:
        with closing(self._con()) as con:
            r = con.execute("SELECT doc, fetched FROM cimd WHERE client_id = ?", (client_id,)).fetchone()
        if r is None or r["fetched"] + CIMD_CACHE < _now():
            return None
        return json.loads(r["doc"])

    def cimd_put(self, client_id: str, doc: dict) -> None:
        with closing(self._con()) as con:
            con.execute("INSERT OR REPLACE INTO cimd VALUES (?, ?, ?)", (client_id, json.dumps(doc), _now()))

    # ------------------------------------------------------------- pending requests
    def add_pending(self, client: dict, redirect_uri: str, state: Optional[str], challenge: str, scopes: list[str],
                    resource: str, browser_key: Optional[str] = None) -> dict:
        """A new waiting request. browser_key: the secret the starting browser keeps in a cookie (stored hashed)."""
        rid = secrets.token_urlsafe(18)
        pair = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(6))
        with closing(self._con()) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM pending WHERE created < ?", (_now() - 3600,))
            n = con.execute("SELECT COUNT(*) FROM pending WHERE status = 'pending' AND created >= ?",
                            (_now() - APPROVAL_SECONDS,)).fetchone()[0]
            if n >= MAX_OPEN:
                con.execute("ROLLBACK")
                raise TooManyRequests(f"{n} connection requests are already waiting")
            con.execute("INSERT INTO pending (id, pair, client_id, client_name, via, redirect_uri, state, challenge, "
                        "scopes, resource, created, browser_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (rid, pair, client["client_id"], client["client_name"], client["via"], redirect_uri, state,
                         challenge, json.dumps(scopes), resource, _now(),
                         token_hash(browser_key) if browser_key else None))
            con.execute("COMMIT")
        return self.pending(rid)

    def pending(self, rid: str) -> Optional[dict]:
        with closing(self._con()) as con:
            r = con.execute("SELECT * FROM pending WHERE id = ?", (rid,)).fetchone()
        return _pending_view(r) if r else None

    def pending_list(self) -> list[dict]:
        """Requests still waiting for a decision (oldest first)."""
        with closing(self._con()) as con:
            rows = con.execute("SELECT * FROM pending WHERE status = 'pending' AND created >= ? ORDER BY created",
                               (_now() - APPROVAL_SECONDS,)).fetchall()
        return [_pending_view(r) for r in rows]

    def browser_ok(self, rid: str, browser_key: Optional[str]) -> bool:
        """This browser key is the one of the browser that started the request."""
        if not browser_key:
            return False
        with closing(self._con()) as con:
            r = con.execute("SELECT browser_key FROM pending WHERE id = ?", (rid,)).fetchone()
        return bool(r and r["browser_key"]) and hmac.compare_digest(r["browser_key"], token_hash(browser_key))

    def local_hint(self, rid: str, code: str, browser_key: str) -> Optional[dict]:
        """The Connect button of the browser page, received on this PC's loopback address: the waiting request
        when the code and the starting browser's key match it, else None. It only fills the code in the approval
        window; approving still takes the user's click there."""
        req = self.pending(rid) if isinstance(rid, str) else None
        if req is None or req["status"] != "pending" or req["expires_in"] <= 0:
            return None
        if not isinstance(code, str) or not hmac.compare_digest(code.strip().upper().encode(), req["pair"].encode()):
            return None
        return req if isinstance(browser_key, str) and self.browser_ok(rid, browser_key) else None

    def decide(self, pair: str, approve: bool, edits: bool = False) -> Optional[dict]:
        """Approve (edits: add the p6:write scope) or deny the waiting request showing this pairing code. None when
        no waiting request has it."""
        pair = (pair or "").strip().upper()
        with closing(self._con()) as con:
            con.execute("BEGIN IMMEDIATE")
            r = con.execute("SELECT * FROM pending WHERE pair = ? AND status = 'pending' AND created >= ?",
                            (pair, _now() - APPROVAL_SECONDS)).fetchone()
            if r is None:
                con.execute("ROLLBACK")
                return None
            scopes = json.loads(r["scopes"])
            if approve and edits and "p6:write" not in scopes:
                scopes.append("p6:write")
            if approve and not edits:
                scopes = [s for s in scopes if s != "p6:write"]
            con.execute("UPDATE pending SET status = ?, scopes = ?, decided = ? WHERE id = ?",
                        ("approved" if approve else "denied", json.dumps(scopes), _now(), r["id"]))
            con.execute("COMMIT")
        return self.pending(r["id"])

    def decide_request(self, rid: str, typed_code: str, approve: bool, edits: bool = False) -> Optional[dict]:
        """The approval window's decision, by request id. Approving needs the code the user's own browser shows,
        typed on the PC: a request started by someone else shows its code in their browser, not the user's.
        Returns the request (status approved / denied), {"mismatch": True} for a wrong code, None when the request
        is no longer waiting."""
        req = self.pending(rid)
        if req is None or req["status"] != "pending" or req["expires_in"] <= 0:
            return None
        if approve and (typed_code or "").strip().upper() != req["pair"]:
            return {"mismatch": True}
        return self.decide(req["pair"], approve, edits)

    def finish_pending(self, rid: str) -> Optional[dict]:
        """The browser came back for a decided (or expired) request: an approved one becomes a grant and an
        authorization code. Returns {status, code?, grant?, request} once, None when unknown or already finished."""
        with closing(self._con()) as con:  # the browser polls every 2 s: a plain read first, the write lock after
            r = con.execute("SELECT status, created FROM pending WHERE id = ?", (rid,)).fetchone()
        if r is None:
            return None
        if r["status"] == "pending" and r["created"] >= _now() - APPROVAL_SECONDS:
            return {"status": "pending", "request": self.pending(rid)}
        with closing(self._con()) as con:
            con.execute("BEGIN IMMEDIATE")
            r = con.execute("SELECT * FROM pending WHERE id = ?", (rid,)).fetchone()
            if r is None:
                con.execute("ROLLBACK")
                return None
            req = _pending_view(r)
            expired = r["status"] == "pending" and r["created"] < _now() - APPROVAL_SECONDS
            if r["status"] == "pending" and not expired:
                con.execute("ROLLBACK")
                return {"status": "pending", "request": req}
            con.execute("DELETE FROM pending WHERE id = ?", (rid,))
            out = {"status": "expired" if expired else r["status"], "request": req}
            if r["status"] == "approved":
                gid = uuid.uuid4().hex
                code = secrets.token_urlsafe(32)
                host = req["redirect_uri"].split("/")[2] if "://" in req["redirect_uri"] else None
                con.execute("INSERT INTO grants (id, client_id, client_name, via, redirect_host, scopes, resource, "
                            "created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (gid, req["client_id"], req["client_name"], req["via"], host, json.dumps(req["scopes"]),
                             req["resource"], _now()))
                con.execute("INSERT INTO codes VALUES (?, ?, ?, ?, ?, ?)",
                            (token_hash(code), gid, req["challenge"], req["redirect_uri"], req["client_id"],
                             _now() + CODE_TTL))
                out.update(code=code, grant=gid)
            con.execute("COMMIT")
        return out

    # ------------------------------------------------------------- codes and tokens
    def take_code(self, code: str) -> Optional[dict]:
        """The authorization code's record, removed (single use); None when unknown or expired."""
        with closing(self._con()) as con:
            con.execute("BEGIN IMMEDIATE")
            r = con.execute("SELECT * FROM codes WHERE hash = ?", (token_hash(code),)).fetchone()
            con.execute("DELETE FROM codes WHERE hash = ? OR exp < ?", (token_hash(code), _now()))
            con.execute("COMMIT")
        if r is None or r["exp"] < _now():
            return None
        return dict(r)

    def issue(self, grant_id: str) -> dict:
        """A new access token and refresh token for the grant (the token response)."""
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = _now()
        with closing(self._con()) as con:
            con.execute("DELETE FROM tokens WHERE exp < ?", (now - 86400,))
            con.execute("INSERT INTO tokens VALUES (?, ?, 'access', ?, 0)", (token_hash(access), grant_id,
                                                                             now + ACCESS_TTL))
            con.execute("INSERT INTO tokens VALUES (?, ?, 'refresh', ?, 0)", (token_hash(refresh), grant_id,
                                                                              now + REFRESH_TTL))
            scopes = json.loads(con.execute("SELECT scopes FROM grants WHERE id = ?", (grant_id,)).fetchone()[0])
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL, "refresh_token": refresh,
                "scope": " ".join(scopes)}

    def refresh(self, refresh_token: str, client_id: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
        """(token response, None) for a valid refresh token, else (None, reason). A refresh token used twice
        revokes its whole grant: one of the two holders is not the client."""
        h = token_hash(refresh_token)
        with closing(self._con()) as con:
            con.execute("BEGIN IMMEDIATE")
            r = con.execute("SELECT t.*, g.client_id, g.revoked FROM tokens t JOIN grants g ON g.id = t.grant_id "
                            "WHERE t.hash = ? AND t.kind = 'refresh'", (h,)).fetchone()
            if r is None or r["exp"] < _now() or r["revoked"]:
                con.execute("ROLLBACK")
                return None, "unknown, expired or revoked refresh token"
            if client_id and client_id != r["client_id"]:
                con.execute("ROLLBACK")
                return None, "client_id mismatch"
            if r["used"]:
                con.execute("UPDATE grants SET revoked = ? WHERE id = ?", (_now(), r["grant_id"]))
                con.execute("DELETE FROM tokens WHERE grant_id = ?", (r["grant_id"],))
                con.execute("COMMIT")
                return None, "refresh token reused: the connection was revoked"
            con.execute("UPDATE tokens SET used = 1 WHERE hash = ?", (h,))
            con.execute("COMMIT")
        return self.issue(r["grant_id"]), None

    def check_access(self, token: str) -> Optional[dict]:
        """The grant of a valid access token: {grant, client_id, client_name, scopes, resource, exp}."""
        with closing(self._con()) as con:
            r = con.execute("SELECT t.exp, g.* FROM tokens t JOIN grants g ON g.id = t.grant_id "
                            "WHERE t.hash = ? AND t.kind = 'access'", (token_hash(token),)).fetchone()
            if r is None or r["exp"] < _now() or r["revoked"]:
                return None
            if not r["last_used"] or r["last_used"] < _now() - 60:  # one write a minute at most
                con.execute("UPDATE grants SET last_used = ? WHERE id = ?", (_now(), r["id"]))
        return {"grant": r["id"], "client_id": r["client_id"], "client_name": r["client_name"],
                "scopes": json.loads(r["scopes"]), "resource": r["resource"], "exp": r["exp"]}

    def revoke_token(self, token: str) -> Optional[str]:
        """RFC 7009: the grant of this access or refresh token is revoked. Returns its id (None: unknown token)."""
        with closing(self._con()) as con:
            r = con.execute("SELECT grant_id FROM tokens WHERE hash = ?", (token_hash(token),)).fetchone()
        if r is None:
            return None
        self.revoke(r["grant_id"])
        return r["grant_id"]

    # ------------------------------------------------------------- grants (Settings, Connected clients)
    def grants(self, include_revoked: bool = False) -> list[dict]:
        with closing(self._con()) as con:
            rows = con.execute("SELECT * FROM grants" + ("" if include_revoked else " WHERE revoked IS NULL")
                               + " ORDER BY created").fetchall()
        return [{"id": r["id"], "client_name": r["client_name"], "client_id": r["client_id"], "via": r["via"],
                 "redirect_host": r["redirect_host"], "scopes": json.loads(r["scopes"]), "resource": r["resource"],
                 "created": r["created"], "last_used": r["last_used"], "revoked": r["revoked"]} for r in rows]

    def revoke(self, grant_id: str) -> bool:
        with closing(self._con()) as con:
            n = con.execute("UPDATE grants SET revoked = ? WHERE id = ? AND revoked IS NULL",
                            (_now(), grant_id)).rowcount
            con.execute("DELETE FROM tokens WHERE grant_id = ?", (grant_id,))
        return bool(n)

    def revoke_all(self) -> int:
        with closing(self._con()) as con:
            n = con.execute("UPDATE grants SET revoked = ? WHERE revoked IS NULL", (_now(),)).rowcount
            con.execute("DELETE FROM tokens")
            con.execute("DELETE FROM codes")
            con.execute("DELETE FROM pending")
        return n

    def revoke_other_resources(self, resource: str) -> int:
        """Grants issued for another resource (the public address changed) are revoked."""
        with closing(self._con()) as con:
            ids = [r[0] for r in con.execute("SELECT id FROM grants WHERE revoked IS NULL AND resource != ?",
                                             (resource,))]
        for gid in ids:
            self.revoke(gid)
        return len(ids)


def _pending_view(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "pair": r["pair"], "client_id": r["client_id"], "client_name": r["client_name"],
            "via": r["via"], "redirect_uri": r["redirect_uri"], "state": r["state"], "challenge": r["challenge"],
            "scopes": json.loads(r["scopes"]), "resource": r["resource"], "created": r["created"],
            "status": r["status"], "expires_in": max(0, r["created"] + APPROVAL_SECONDS - _now())}
