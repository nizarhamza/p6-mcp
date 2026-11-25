"""P6 EPPM backend (docs/EPPM_PLAN.md): REST session and read-only SQL connection of a server profile.

Milestone 1 holds the connection layer and its test; the pull, the working copy and the push come later.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Callable, Optional

CONNECTION_KEYS = ("base_url", "context", "database", "user", "auth", "ca_bundle", "sql")


def digest(profile: dict) -> str:
    """Fingerprint of the connection settings: a test result confirms only the settings it was run with."""
    keys = {k: profile.get(k) for k in CONNECTION_KEYS}
    return hashlib.sha256(json.dumps(keys, sort_keys=True, default=str).encode()).hexdigest()[:16]


def lock_check(profile: dict, sql_password: Optional[str] = None,
               sql_module: Optional[Callable[[str], Any]] = None) -> Optional[Callable[[], str]]:
    """For rest.Session(lock_check=): when a sign-in is refused as P6 EPPM refuses a locked account, read the user's
    failed sign-ins with the profile's read-only SQL account and say what they show. sql_password None = the stored
    one, read only then; sql_module = driver factory (tests). None when the profile has no SQL account."""
    sqlcfg = profile.get("sql")
    if not sqlcfg:
        return None

    def check() -> str:
        from p6mcp.eppm import rest, sql
        pw = sql_password
        if pw is None:
            from p6mcp import credentials
            try:
                pw = credentials.get(credentials.eppm_sql_key(profile["name"]))
            except Exception:  # noqa: BLE001  (no secret store: nothing to add)
                return ""
        if not pw:
            return ""
        module = sql_module(sqlcfg.get("driver", "")) if sql_module else None
        return rest.lock_finding(sql.failed_sign_ins(sqlcfg, pw, profile.get("user", ""), module),
                                 profile.get("user", ""))
    return check


def test_profile(profile: dict, password: Optional[str], sql_password: Optional[str] = None,
                 rest_opener: Optional[Callable] = None, sql_module: Optional[Callable[[str], Any]] = None) -> dict:
    """Test connection of a server profile: REST sign-in and project count, then the SQL account when the profile
    has one. ok follows REST (the SQL route is optional: its result is reported beside). Never raises."""
    from p6mcp.eppm import rest, sql
    res: dict = {"ok": False, "digest": digest(profile), "tested_at": datetime.now().isoformat(timespec="seconds")}
    if not password:
        res["rest"] = {"ok": False, "error": "No password: enter the P6 password of this server."}
    else:
        res["rest"] = rest.test_connection(profile, password, rest_opener,
                                           lock_check(profile, sql_password or "", sql_module))
    res["ok"] = bool(res["rest"].get("ok"))
    if profile.get("sql"):
        if not sql_password:
            res["sql"] = {"ok": False, "error": "No password for the SQL account."}
        else:
            res["sql"] = sql.test_connection(profile["sql"], sql_password, sql_module)
    else:
        res["sql"] = None
    return res
