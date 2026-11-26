"""Remote access authorization server (3.2, milestone M2): metadata, DCR, CIMD, the approval on the PC, the code
and token exchange (PKCE S256), refresh rotation with reuse detection, revocation, and a full login with the SDK's
own OAuth client, on a real uvicorn server behind the relay guard. No network: the client metadata documents of
claude.ai and ChatGPT are served by a fake fetcher, and https://<public host> is routed to 127.0.0.1."""

import asyncio
import base64
import hashlib
import io
import json
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest

from p6mcp import settings_logic

httpx = pytest.importorskip("httpx2")
pytest.importorskip("mcp.server.auth.settings")
from p6mcp.remote import RELAY_HEADER, auth, cli, resource_url  # noqa: E402
from p6mcp.remote import server as remote  # noqa: E402
from p6mcp.remote import store as store_mod  # noqa: E402
from p6mcp.remote.store import Store  # noqa: E402

HOST = "p6mcp-relay.example.workers.dev"
ISSUER, RESOURCE = f"https://{HOST}", resource_url(HOST)
SECRET = "relay-secret-0123456789"
CLAUDE = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
GPT = "https://chatgpt.com/oauth/client.json"
GPT_CB = "https://chatgpt.com/connector_platform_oauth_redirect"
DOCS = {CLAUDE: {"client_id": CLAUDE, "client_name": "Claude", "redirect_uris": [CLAUDE_CB]},
        GPT: {"client_id": GPT, "client_name": "ChatGPT", "redirect_uris": [GPT_CB]},
        "https://claude.ai/oauth/wrong-id": {"client_id": "https://claude.ai/oauth/other",
                                             "redirect_uris": [CLAUDE_CB]}}
FETCHED = []


async def fake_fetch(url):
    FETCHED.append(url)
    if url in DOCS:
        return 200, json.dumps(DOCS[url]).encode()
    return 404, b""


def pkce():
    verifier = base64.urlsafe_b64encode(b"v" * 40).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@pytest.fixture
def live(p6, tmp_path):
    p6.set(remote_public_host=HOST, remote_allow_writes=True)
    st = p6.config.load()
    store = Store(str(tmp_path / "auth.db"))
    app = remote.build_app(st, relay_secret=SECRET, store=store, fetch=fake_fetch)
    port = settings_logic.free_port(41900)
    srv = remote.make_server(app, port)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.monotonic() + 20
    while not srv.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    FETCHED.clear()
    yield {"base": f"http://127.0.0.1:{port}", "port": port, "store": store, "p6": p6, "st": st}
    srv.should_exit = True
    th.join(10)


def get(live, path, **kw):
    return httpx.get(live["base"] + path, headers={RELAY_HEADER: SECRET, **kw.pop("headers", {})}, timeout=10,
                     follow_redirects=False, **kw)


def post(live, path, **kw):
    return httpx.post(live["base"] + path, headers={RELAY_HEADER: SECRET, **kw.pop("headers", {})}, timeout=10, **kw)


def authorize(live, client=CLAUDE, redirect=CLAUDE_CB, challenge=None, **extra):
    params = {"response_type": "code", "client_id": client, "redirect_uri": redirect, "state": "st-1",
              "code_challenge": challenge or pkce()[1], "code_challenge_method": "S256", "resource": RESOURCE,
              "scope": "p6:read", **extra}
    return get(live, "/authorize", params=params)


def approved_code(live, edits=False, client=CLAUDE, redirect=CLAUDE_CB):
    verifier, challenge = pkce()
    r = authorize(live, client, redirect, challenge)
    assert r.status_code == 302 and r.headers["location"].startswith("/authorize/wait?id=")
    wait = r.headers["location"]
    page = get(live, wait)
    pair = live["store"].pending_list()[-1]["pair"]
    assert page.status_code == 200 and pair in page.text and "content-security-policy" in page.headers
    assert live["store"].decide(pair, True, edits)["status"] == "approved"
    back = get(live, wait)
    assert back.status_code == 302
    loc = urlparse(back.headers["location"])
    q = parse_qs(loc.query)
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == redirect and q["state"] == ["st-1"] and q["iss"] == [ISSUER]
    return q["code"][0], verifier


def exchange(live, code, verifier, client=CLAUDE, redirect=CLAUDE_CB):
    return post(live, "/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                                      "redirect_uri": redirect, "client_id": client, "resource": RESOURCE})


# ----------------------------------------------------------------- metadata and registration

def test_metadata(live):
    m = get(live, "/.well-known/oauth-authorization-server").json()
    assert m["issuer"] == ISSUER and m["token_endpoint"] == ISSUER + "/token"
    assert m["code_challenge_methods_supported"] == ["S256"] and m["token_endpoint_auth_methods_supported"] == ["none"]
    assert m["client_id_metadata_document_supported"] is True
    assert m["authorization_response_iss_parameter_supported"] is True
    assert get(live, "/.well-known/openid-configuration").json() == m
    prm = get(live, "/.well-known/oauth-protected-resource").json()
    assert prm["resource"] == RESOURCE and prm["authorization_servers"] == [ISSUER]
    sdk = get(live, "/.well-known/oauth-protected-resource/mcp").json()
    assert sdk["resource"] == RESOURCE and sdk["authorization_servers"] == [ISSUER]
    assert get(live, "/.well-known/oauth-authorization-server", headers={RELAY_HEADER: "x"}).status_code == 403


def test_dcr(live):
    r = post(live, "/register", json={"client_name": "Claude", "redirect_uris": [CLAUDE_CB]})
    assert r.status_code == 201 and r.json()["token_endpoint_auth_method"] == "none"
    cid = r.json()["client_id"]
    assert live["store"].client(cid)["redirect_uris"] == [CLAUDE_CB]
    for uris in (["https://evil.example/cb"], [], ["https://chatgpt.com/connector/oauth/x?y=1"]):
        assert post(live, "/register", json={"redirect_uris": uris}).status_code == 400
    gpt_cb = "https://chatgpt.com/connector/oauth/abc"
    assert post(live, "/register", json={"redirect_uris": [gpt_cb]}).status_code == 201
    code, verifier = approved_code(live, client=cid)
    assert exchange(live, code, verifier, client=cid).status_code == 200


def test_cimd_rules(live):
    store = live["store"]
    assert asyncio.run(auth.resolve_client(CLAUDE, store, fake_fetch))["client_name"] == "Claude"
    FETCHED.clear()
    asyncio.run(auth.resolve_client(CLAUDE, store, fake_fetch))
    assert FETCHED == []  # cached
    for bad in ("http://claude.ai/x", "https://claude.ai.evil.example/x", "https://evil.example/client.json",
                "https://claude.ai/", "https://claude.ai:8443/x", "https://claude.ai/oauth/wrong-id",
                "https://claude.ai/oauth/missing"):
        assert asyncio.run(auth.resolve_client(bad, store, fake_fetch)) is None, bad
    assert authorize(live, client="https://evil.example/c.json").status_code == 400


# ----------------------------------------------------------------- authorize

def test_authorize_checks(live):
    assert authorize(live, redirect="https://evil.example/cb").status_code == 400  # not the client's: no redirect
    for extra, error in (({"code_challenge_method": "plain"}, "invalid_request"),
                         ({"code_challenge": "short"}, "invalid_request"),
                         ({"resource": "https://other.example/mcp"}, "invalid_target"),
                         ({"scope": "admin"}, "invalid_scope"),
                         ({"response_type": "token"}, "unsupported_response_type")):
        r = authorize(live, **extra)
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert r.status_code == 302 and q["error"] == [error] and q["iss"] == [ISSUER], extra
    assert live["store"].pending_list() == []


def test_deny_and_expiry(live):
    r = authorize(live)
    wait = r.headers["location"]
    pair = live["store"].pending_list()[0]["pair"]
    assert live["store"].decide(pair, False)["status"] == "denied"
    q = parse_qs(urlparse(get(live, wait).headers["location"]).query)
    assert q["error"] == ["access_denied"] and q["state"] == ["st-1"]
    assert get(live, wait).status_code == 404  # finished
    r = authorize(live, client=GPT, redirect=GPT_CB)
    wait = r.headers["location"]
    con = sqlite3.connect(live["store"].path)
    con.execute("UPDATE pending SET created = created - 400")
    con.commit()
    con.close()
    assert live["store"].pending_list() == []
    assert live["store"].decide("ANYXYZ", True) is None
    q = parse_qs(urlparse(get(live, wait).headers["location"]).query)
    assert q["error"] == ["access_denied"]


def test_page_language(p6, tmp_path):
    p6.set(remote_public_host=HOST, language="fr")
    from p6mcp import i18n
    try:
        st = p6.config.load()
        remote.build_app(st, relay_secret=SECRET, store=Store(str(tmp_path / "a.db")), fetch=fake_fetch)
        assert i18n.tr("remote.page.title") == "Approuvez sur votre PC"
    finally:
        i18n.set_language("en")


# ----------------------------------------------------------------- tokens

def test_code_exchange_and_tool_call(live):
    code, verifier = approved_code(live)
    r = exchange(live, code, verifier)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    tok = r.json()
    assert tok["token_type"] == "Bearer" and tok["scope"] == "p6:read" and tok["expires_in"] == 3600
    assert exchange(live, code, verifier).json()["error"] == "invalid_grant"  # single use
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
            "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    r = post(live, "/mcp", json=init, headers={"authorization": f"Bearer {tok['access_token']}",
                                                "accept": "application/json, text/event-stream"})
    assert r.status_code == 200 and "serverInfo" in r.text
    g = live["store"].grants()[0]
    assert g["client_name"] == "Claude" and g["via"] == "cimd" and g["redirect_host"] == "claude.ai"
    assert g["last_used"] is not None


def test_pkce_and_redirect_must_match(live):
    code, verifier = approved_code(live)
    assert exchange(live, code, "x" * 43).json()["error"] == "invalid_grant"
    assert live["store"].grants() == []  # a failed PKCE revokes the connection
    code, verifier = approved_code(live)
    assert exchange(live, code, verifier, redirect=GPT_CB).json()["error"] == "invalid_grant"
    code, verifier = approved_code(live)
    r = post(live, "/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                                   "redirect_uri": CLAUDE_CB, "resource": "https://other.example/mcp"})
    assert r.json()["error"] == "invalid_target"
    assert post(live, "/token", data={"grant_type": "password"}).json()["error"] == "unsupported_grant_type"


def test_refresh_rotation_and_reuse(live):
    code, verifier = approved_code(live, edits=True)
    tok = exchange(live, code, verifier).json()
    assert tok["scope"] == "p6:read p6:write"
    r1 = post(live, "/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                    "client_id": CLAUDE})
    assert r1.status_code == 200 and r1.json()["refresh_token"] != tok["refresh_token"]
    assert post(live, "/token", data={"grant_type": "refresh_token", "refresh_token": r1.json()["refresh_token"],
                                      "client_id": GPT}).json()["error"] == "invalid_grant"  # another client
    reuse = post(live, "/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    assert reuse.json()["error"] == "invalid_grant" and "revoked" in reuse.json()["error_description"]
    assert live["store"].grants() == []
    assert live["store"].check_access(r1.json()["access_token"]) is None  # the whole connection is gone


def test_revoke_endpoint(live):
    code, verifier = approved_code(live)
    tok = exchange(live, code, verifier).json()
    assert post(live, "/revoke", data={"token": tok["refresh_token"]}).status_code == 200
    assert live["store"].check_access(tok["access_token"]) is None
    assert post(live, "/revoke", data={"token": "unknown"}).status_code == 200


def test_new_public_address_voids_old_connections(p6, tmp_path, live):
    code, verifier = approved_code(live)
    tok = exchange(live, code, verifier).json()
    p6.set(remote_public_host="p6.example.com")
    remote.build_app(p6.config.load(), relay_secret=SECRET, store=live["store"], fetch=fake_fetch)
    assert live["store"].check_access(tok["access_token"]) is None


# ----------------------------------------------------------------- SDK client, end to end

class _Route(httpx.AsyncBaseTransport):
    """https://<public host>/... goes to the local server, as the relay would send it (with the relay secret)."""

    def __init__(self, port):
        self.inner = httpx.AsyncHTTPTransport()
        self.port = port

    async def handle_async_request(self, request):
        if request.url.host == HOST:
            request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
            request.headers["host"] = f"127.0.0.1:{self.port}"
            request.headers[RELAY_HEADER] = SECRET
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


class _Mem:
    def __init__(self):
        self.tokens = self.info = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, t):
        self.tokens = t

    async def get_client_info(self):
        return self.info

    async def set_client_info(self, i):
        self.info = i


def test_sdk_client_login_tool_call_and_refresh(live, monkeypatch):
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

    monkeypatch.setattr(store_mod, "ACCESS_TTL", 2)
    done = {}

    async def redirect_handler(url):  # the user's browser: open the page, approve on the PC, follow it back
        async with httpx.AsyncClient(transport=_Route(live["port"]), follow_redirects=False) as c:
            r = await c.get(url)
            wait = ISSUER + r.headers["location"]
            pair = live["store"].pending_list()[-1]["pair"]
            assert pair in (await c.get(wait)).text
            live["store"].decide(pair, True)
            q = parse_qs(urlparse((await c.get(wait)).headers["location"]).query)
            done["cb"] = AuthorizationCodeResult(code=q["code"][0], state=q["state"][0], iss=q["iss"][0])

    async def callback_handler():
        return done["cb"]

    async def go():
        oauth = OAuthClientProvider(
            server_url=RESOURCE, storage=_Mem(), redirect_handler=redirect_handler, callback_handler=callback_handler,
            client_metadata=OAuthClientMetadata(client_name="Claude", redirect_uris=[CLAUDE_CB],
                                                grant_types=["authorization_code", "refresh_token"],
                                                response_types=["code"], token_endpoint_auth_method="none"),
            client_metadata_url=CLAUDE)
        async with httpx.AsyncClient(auth=oauth, transport=_Route(live["port"]), timeout=60) as hc, \
                streamable_http_client(RESOURCE, http_client=hc) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                first = (await s.call_tool("p6_list_projects", {})).is_error
                await asyncio.sleep(3)  # the access token has expired: the client refreshes it
                second = await s.call_tool("p6_list_projects", {})
                return names, first, second
    names, first_error, second = asyncio.run(go())
    assert "p6_list_projects" in names and not first_error and not second.is_error
    assert "DEMO-A" in second.content[0].text
    g = live["store"].grants()
    assert len(g) == 1 and g[0]["via"] == "cimd" and g[0]["client_name"] == "Claude"
    con = sqlite3.connect(live["store"].path)
    used = con.execute("SELECT COUNT(*) FROM tokens WHERE kind = 'refresh' AND used = 1").fetchone()[0]
    con.close()
    assert used == 1 and CLAUDE in FETCHED


# ----------------------------------------------------------------- command line

def test_cli(tmp_path):
    store = Store(str(tmp_path / "auth.db"))
    out = io.StringIO()
    assert cli.main(["pending"], store, out) == 0 and "No connection request" in out.getvalue()
    client = {"client_id": GPT, "client_name": "ChatGPT", "via": "cimd"}
    req = store.add_pending(client, GPT_CB, "s", pkce()[1], ["p6:read"], RESOURCE)
    out = io.StringIO()
    cli.main(["pending"], store, out)
    assert req["pair"] in out.getvalue() and "ChatGPT (chatgpt.com)" in out.getvalue()
    assert cli.main(["approve", "nope00"], store, io.StringIO()) == 1
    out = io.StringIO()
    assert cli.main(["approve", req["pair"].lower(), "--edits"], store, out) == 0
    assert "approved: ChatGPT, access p6:read p6:write" in out.getvalue()
    assert store.finish_pending(req["id"])["status"] == "approved"
    gid = store.grants()[0]["id"]
    out = io.StringIO()
    cli.main(["connections"], store, out)
    assert gid in out.getvalue()
    assert cli.main(["revoke", gid], store, io.StringIO()) == 0 and store.grants() == []
    assert cli.main(["revoke", gid], store, io.StringIO()) == 1
    assert cli.main(["revoke-all"], store, io.StringIO()) == 0
