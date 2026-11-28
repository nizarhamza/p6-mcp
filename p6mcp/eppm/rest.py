"""REST session with a P6 EPPM server (`/p6ws/restapi`), standard library only (as updates.py and shop.py).

Authentication (measured on P6 EPPM 25.12 on premises, docs/EPPM_PLAN.md section 13):
- login: `POST /restapi/login?DatabaseName=<name>` with the `authToken` header (base64 of user:password); the
  `JSESSIONID` cookie then carries the session (Basic alone is refused), `POST /logout` ends it.
- oauth (Oracle Cloud): `POST /oauth/token` with the `authToken` and `token_exp` headers, then
  `Authorization: Bearer <token>` on every call.
- auto: oauth for hosts ending in oraclecloud.com, login otherwise.

Answers are JSON; numbers arrive as strings ("80.0"). Errors become EppmError (a ValueError) with a message for
the user that never contains the password.

Proxy: the profile's `proxy` setting (eppm/proxy.py: the Windows or environment proxy by default, none, or a proxy URL
of the profile), decided once per session for its one host; a loopback server is never proxied.
"""

from __future__ import annotations

import base64
import html
import http.cookiejar
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from p6mcp.eppm import proxy as _proxy
from p6mcp.messages import _join, _t

DEFAULT_CONTEXT = "p6ws"
TOKEN_LIFETIME_S = 3600
# P6 EPPM locks an account after this many failed sign-ins in a row (USERS.failed_login_attempts_cnt, the default,
# measured on 25.12); a locked account's /login then answers HTTP 500 "Request failed" even with the right password
LOCK_AFTER = 5
LOCKED_HINT = (" P6 EPPM answers this way when the account is locked: it locks an account after {n} failed sign-ins in "
               "a row (the default). Ask your P6 administrator to unlock it, and check the password before you try "
               "again: every failed try counts toward the lockout.")


def lock_finding(failed: Optional[int], user: str) -> str:
    """What USERS.failed_login_attempts_cnt says about a refused sign-in ('' when it could not be read)."""
    if failed is None:
        return ""
    if failed >= LOCK_AFTER:
        return _t(" The SQL account confirms it: P6 counts {failed} failed sign-ins in a row for {user}, so the "
                  "account is locked.", failed=failed, user=user)
    return _t(" The SQL account shows {failed} failed sign-in(s) in a row for {user}, under P6's lockout limit "
              "({limit} by default): the account is probably not locked, and the P6 server log may tell the cause.",
              failed=failed, user=user, limit=LOCK_AFTER)


class EppmError(ValueError):
    """A P6 EPPM call failed; the message is meant for the user."""


def auth_mode(base_url: str, auth: str = "auto") -> str:
    if auth in ("oauth", "login"):
        return auth
    host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    return "oauth" if host.endswith("oraclecloud.com") else "login"


def _ssl_context(ca_bundle: Optional[str]) -> ssl.SSLContext:
    try:
        return ssl.create_default_context(cafile=ca_bundle or None)
    except (OSError, ssl.SSLError) as e:
        raise EppmError(_t("The certificate file cannot be read ({file}): {error}", file=ca_bundle, error=e)) from e


class Session:
    """One signed-in session. Use as a context manager, or call close()."""

    def __init__(self, base_url: str, user: str, password: str, database: str = "", auth: str = "auto",
                 context: str = DEFAULT_CONTEXT, ca_bundle: Optional[str] = None, timeout_s: int = 120,
                 opener: Optional[urllib.request.OpenerDirector] = None,
                 lock_check: Optional[Callable[[], str]] = None, proxy: Optional[dict] = None,
                 proxy_password: Optional[str] = None):
        """lock_check: called when a sign-in looks refused for a locked account; returns a sentence to add (the
        read-only SQL account's count of failed sign-ins, eppm.lock_check) or ''. It must never raise.
        proxy: the profile's proxy settings (proxy, proxy_url, proxy_user: eppm/proxy.py), proxy_password the
        proxy user's password. An opener given (tests) is used as it is."""
        if not (user or "").strip():
            raise EppmError(_t("No user name: enter the P6 user of this server."))
        if not password:
            raise EppmError(_t("No password: enter the P6 password of this server."))
        root = base_url.rstrip("/") + "/" + (context or DEFAULT_CONTEXT).strip("/")
        self.rest, self.token_url = root + "/restapi", root + "/oauth/token"
        self.user, self.database, self.timeout = user, database or "", timeout_s
        self.mode = auth_mode(base_url, auth)
        self._pw, self._proxy_pw = password, proxy_password or ""
        self._b64 = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.jar = http.cookiejar.CookieJar()
        self.proxy = _proxy.resolve(base_url, proxy)
        if self.proxy["mode"] == "custom" and not self.proxy["url"] and not self.proxy["bypassed"]:
            raise EppmError(_t("The proxy is set to 'This proxy' without an address: enter the proxy address "
                               "(http://host:port) in the server settings, or choose another proxy setting."))
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_ssl_context(ca_bundle)), urllib.request.HTTPCookieProcessor(self.jar),
            _proxy.handler(self.proxy, proxy_password))
        self.bearer: Optional[str] = None
        self.bearer_until = 0.0
        self.signed_in = False
        self.lock_check = lock_check
        self.calls: list[dict] = []  # name, method, status, ms (no URL query, no body)

    # -- transport
    def _send(self, method: str, url: str, headers: Optional[dict] = None, body: Any = None,
              name: str = "") -> tuple[int, bytes]:
        h = {"Accept": "application/json"}
        if self.bearer and not (headers or {}).get("authToken"):
            h["Authorization"] = f"Bearer {self.bearer}"
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        t0 = time.perf_counter()
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, (e.read() or b"")
        except ssl.SSLError as e:
            raise EppmError(_t("Secure connection to the server failed ({reason}). If your company uses its own "
                               "certificates, set the certificate file of this server.", reason=e.reason or e)) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = self._clean(str(getattr(e, "reason", e)))
            if self.proxy.get("url"):
                if "407" in reason:  # an https tunnel the proxy refused
                    raise EppmError(self._proxy_refused()) from e
                raise EppmError(_t("The server cannot be reached through the proxy {url} ({source}): {reason}. Check "
                                   "the proxy setting of this server.",
                                   url=self.proxy["url"], source=self.proxy["source"], reason=reason)) from e
            raise EppmError(_t("The server cannot be reached: {reason}", reason=reason)) from e
        if status == 407:  # plain http through the proxy: the proxy itself answers
            raise EppmError(self._proxy_refused())
        self.calls.append({"name": name or method, "method": method, "status": status,
                           "ms": round((time.perf_counter() - t0) * 1000)})
        return status, raw

    def _proxy_refused(self) -> str:
        if self.proxy.get("user"):
            return _t("The proxy {url} refused the call (HTTP 407, proxy sign-in required): check the proxy user and "
                      "password of this server. Only Basic proxy sign-in is supported (not NTLM or Kerberos).",
                      url=self.proxy.get("url"))
        return _t("The proxy {url} refused the call (HTTP 407, proxy sign-in required): enter the proxy user and "
                  "password in the server settings. Only Basic proxy sign-in is supported (not NTLM or Kerberos).",
                  url=self.proxy.get("url"))

    def _clean(self, text: str) -> str:
        for s in (self._pw, self._b64, self._proxy_pw):
            if s:
                text = text.replace(s, "***")
        return text[:300]

    def _detail(self, raw: bytes) -> str:
        """The server's error as one line of plain text: JSON message, else the text of an HTML page (P6 answers
        some errors with a page of headings and a Java stack trace)."""
        text = raw.decode("utf-8", "replace").strip()
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                text = str(obj.get("message") or obj.get("error") or obj.get("detail") or text)
        except ValueError:
            pass
        if "<" in text and ">" in text:
            text = html.unescape(re.sub(r"<[^>]*>", " ", text))
            text = re.split(r"(?i)\bstack\s*trace\b", text)[0]  # the Java stack is not for the user
        return self._clean(" ".join(text.split()))

    # -- sign in and out
    def login(self) -> "Session":
        if self.mode == "oauth":
            st, raw = self._send("POST", self.token_url, {"authToken": self._b64,
                                                           "token_exp": str(TOKEN_LIFETIME_S)}, name="oauth_token")
            tok = None
            if st == 200:
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except ValueError:
                    obj = raw.decode("utf-8", "replace").strip().strip('"')
                if isinstance(obj, dict):
                    tok = next((v for k, v in obj.items() if "token" in k.lower() and isinstance(v, str)), None)
                elif isinstance(obj, str) and obj and " " not in obj:
                    tok = obj
            if not tok:
                raise self._refused(st, raw)
            self.bearer, self.bearer_until = tok, time.time() + TOKEN_LIFETIME_S - 60
        else:
            q = "?" + urllib.parse.urlencode({"DatabaseName": self.database}) if self.database else ""
            st, raw = self._send("POST", self.rest + "/login" + q, {"authToken": self._b64}, name="login")
            if st != 200:
                raise self._refused(st, raw)
        self.signed_in = True
        return self

    def _refused(self, status: int, raw: bytes) -> EppmError:
        if status in (401, 403):
            if self.mode == "oauth":
                return EppmError(_t("Sign-in refused by the server (HTTP {status}): check the user name, the "
                                    "password.", status=status))
            return EppmError(_t("Sign-in refused by the server (HTTP {status}): check the user name, the password and "
                                "the database name.", status=status))
        if status == 404:
            return EppmError(_t("The server answered 404: check the server address and the context (usually p6ws), "
                                "and that P6 EPPM Web Services is deployed."))
        if self.mode == "oauth" and status >= 500:  # measured on an on-premises 25.12: IDCS not configured
            return EppmError(_t("OAuth sign-in is not set up on this server (it is for P6 EPPM on Oracle Cloud). For "
                                "a server on premises, set Sign-in to Automatic or Session sign-in. The server said: "
                                "{detail}", detail=self._detail(raw)))
        detail = self._detail(raw)
        parts = [_t("Sign-in failed (HTTP {status}): {detail}", status=status, detail=detail)]
        if self.mode == "login" and status == 500 and "request failed" in detail.lower():
            parts.append(_t(LOCKED_HINT, n=LOCK_AFTER))
            if self.lock_check:
                try:
                    parts.append(self.lock_check() or "")
                except Exception:  # noqa: BLE001  (the check is a bonus: the sign-in error stays the message)
                    pass
        return EppmError(_join("", parts))

    def close(self) -> None:
        if self.signed_in and self.mode == "login":
            try:
                self._send("POST", self.rest + "/logout", name="logout")
            except EppmError:
                pass
        self.signed_in, self.bearer = False, None

    def __enter__(self) -> "Session":
        return self.login()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- calls
    def _renew(self) -> None:
        if self.mode == "oauth" and self.signed_in and time.time() > self.bearer_until:
            self.login()

    def call(self, method: str, path: str, body: Any = None, name: str = "") -> Any:
        """JSON answer of one call; EppmError on any status other than 2xx."""
        self._renew()
        st, raw = self._send(method, self.rest + path, body=body, name=name)
        if not 200 <= st < 300:
            if st in (401, 403):
                raise EppmError(_t("The server refused the call (HTTP {status}): the session ended or this user lacks "
                                   "the rights for it.", status=st))
            raise EppmError(_t("The server answered HTTP {status} to {call}: {detail}",
                               status=st, call=name or path.split("?")[0], detail=self._detail(raw)))
        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise EppmError(_t("The server sent an answer that is not JSON to {call}.",
                               call=name or path.split("?")[0])) from e

    def read(self, entity: str, fields: list[str], filter: Optional[str] = None) -> list[dict]:
        """GET /<entity>?Fields=..&Filter=.. as a list of objects (no paging in the API)."""
        q = "Fields=" + urllib.parse.quote(",".join(fields), safe=",")
        if filter:
            q += "&Filter=" + urllib.parse.quote(filter)
        data = self.call("GET", f"/{entity}?{q}", name=f"read {entity}")
        if isinstance(data, dict):  # some releases wrap the list
            data = next((v for v in data.values() if isinstance(v, list)), [data])
        return data if isinstance(data, list) else []


def stored_proxy_password(profile: dict) -> Optional[str]:
    """The proxy user's password from the Credential Manager (None without a proxy user or a stored one)."""
    if not profile.get("proxy_user"):
        return None
    from p6mcp import credentials
    try:
        return credentials.get(credentials.eppm_proxy_key(profile["name"]))
    except credentials.SecretStoreError as e:
        raise EppmError(str(e)) from e


def from_profile(profile: dict, password: str, opener: Optional[urllib.request.OpenerDirector] = None,
                 lock_check: Optional[Callable[[], str]] = None, proxy_password: Optional[str] = None) -> Session:
    """A Session with every setting of a server profile. proxy_password None = the stored one (read only when the
    profile has a proxy user)."""
    if proxy_password is None:
        proxy_password = stored_proxy_password(profile)
    return Session(profile["base_url"], profile.get("user", ""), password, profile.get("database") or "",
                   profile.get("auth", "auto"), profile.get("context") or DEFAULT_CONTEXT,
                   profile.get("ca_bundle") or None, int(profile.get("timeout_s", 120)), opener=opener,
                   lock_check=lock_check, proxy={k: profile.get(k) for k in _proxy.KEYS},
                   proxy_password=proxy_password)


def test_connection(profile: dict, password: str,
                    opener_factory: Optional[Callable[[], urllib.request.OpenerDirector]] = None,
                    lock_check: Optional[Callable[[], str]] = None, proxy_password: Optional[str] = None) -> dict:
    """Sign in, count the projects the user can read, sign out. Never raises: {ok, ...} or {ok: False, error}.
    proxy = how the calls go (eppm/proxy.py resolve: no password)."""
    t0 = time.perf_counter()
    res: dict = {"ok": False, "auth": auth_mode(profile.get("base_url", ""), profile.get("auth", "auto"))}
    try:
        res["proxy"] = _proxy.resolve(profile.get("base_url", ""), profile)
        s = from_profile(dict(profile, user=profile["user"]), password,
                         opener=opener_factory() if opener_factory else None, lock_check=lock_check,
                         proxy_password=proxy_password)
        with s:
            rows = s.read("project", ["ObjectId", "Id"])
            res.update(ok=True, projects=len(rows))
    except EppmError as e:
        res["error"] = str(e)
    except (KeyError, TypeError, ValueError) as e:
        res["error"] = _t("Incomplete server settings: {error}", error=e)
    res["seconds"] = round(time.perf_counter() - t0, 2)
    return res
