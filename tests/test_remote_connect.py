"""Connect button of the remote access connection page (3.3): the browser key cookie of the starting browser, the
redesigned page (Connect button only for that browser, the code with Copy for everyone, nonce script and CSP), the
side-effect-free status check the page polls, and the tray's loopback listener that fills the code in the approval
window (Origin, Host, code and browser key checked)."""

import json
import re
import sqlite3
import urllib.error
import urllib.request

import pytest
from conftest import free_port

pytest.importorskip("httpx2")
pytest.importorskip("mcp.server.auth.settings")
from p6mcp.remote import pages  # noqa: E402
from p6mcp.remote.pairing import PATH, PairListener  # noqa: E402
from p6mcp.remote.store import Store  # noqa: E402
from tests import test_remote_auth as ra  # noqa: E402
from tests.test_remote_auth import CLAUDE, authorize, get  # noqa: E402

live = ra.live  # the live remote access server fixture

CLIENT = {"client_id": CLAUDE, "client_name": "Claude", "via": "cimd"}


@pytest.fixture(autouse=True)
def _own_localappdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))


def _pending(store, key="browser-key-0123456789"):
    return store.add_pending(CLIENT, "https://claude.ai/api/mcp/auth_callback", "s", "c" * 43, ["p6:read"],
                             "https://h/mcp", key)


# ----------------------------------------------------------------- store

def test_store_browser_key_and_local_hint(tmp_path):
    store = Store(str(tmp_path / "auth.db"))
    req = _pending(store)
    rid, pair = req["id"], req["pair"]
    assert store.browser_ok(rid, "browser-key-0123456789")
    assert not store.browser_ok(rid, "other") and not store.browser_ok(rid, None) and not store.browser_ok("x", "k")
    raw = sqlite3.connect(str(tmp_path / "auth.db")).execute("SELECT browser_key FROM pending").fetchone()[0]
    assert raw != "browser-key-0123456789" and len(raw) == 64  # stored hashed
    assert store.local_hint(rid, pair.lower(), "browser-key-0123456789")["id"] == rid
    assert store.local_hint(rid, "ZZZZZZ", "browser-key-0123456789") is None
    assert store.local_hint(rid, pair, "wrong") is None
    assert store.local_hint("unknown-request-id", pair, "browser-key-0123456789") is None
    no_key = _pending(store, key=None)
    assert store.local_hint(no_key["id"], no_key["pair"], "") is None and not store.browser_ok(no_key["id"], "")
    store.decide(pair, False)
    assert store.local_hint(rid, pair, "browser-key-0123456789") is None  # decided: nothing to fill in


def test_store_adds_browser_key_column_to_a_3_2_store(tmp_path):
    path = str(tmp_path / "auth.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE pending (id TEXT PRIMARY KEY, pair TEXT NOT NULL, client_id TEXT NOT NULL, "
                "client_name TEXT NOT NULL, via TEXT NOT NULL, redirect_uri TEXT NOT NULL, state TEXT, "
                "challenge TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT NOT NULL, created INTEGER NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'pending', decided INTEGER)")
    con.commit()
    con.close()
    store = Store(path)
    assert store.browser_ok(_pending(store)["id"], "browser-key-0123456789")


# ----------------------------------------------------------------- page

def _start(live):
    r = authorize(live)
    assert r.status_code == 302
    wait = r.headers["location"]
    rid = wait.split("id=")[1]
    cookie = r.headers["set-cookie"]
    return wait, rid, cookie


def test_authorize_sets_browser_cookie(live):
    wait, rid, cookie = _start(live)
    assert cookie.startswith(f"p6b_{rid}=")
    low = cookie.lower()
    assert "httponly" in low and "secure" in low and "samesite=lax" in low and "path=/authorize" in low
    key = cookie.split(";")[0].split("=", 1)[1]
    assert live["store"].browser_ok(rid, key)


def test_wait_page_connect_button_only_for_the_starting_browser(live):
    wait, rid, cookie = _start(live)
    key = cookie.split(";")[0].split("=", 1)[1]
    pair = live["store"].pending(rid)["pair"]
    port = live["st"].raw["remote_pair_port"]

    mine = get(live, wait, headers={"cookie": f"p6b_{rid}={key}"})
    assert mine.status_code == 200 and 'id="connect"' in mine.text and pair in mine.text and 'id="copy"' in mine.text
    csp = mine.headers["content-security-policy"]
    nonce = re.search(r"script-src 'nonce-([A-Za-z0-9_-]+)'", csp).group(1)
    assert f'<script nonce="{nonce}">' in mine.text
    assert f"connect-src 'self' http://127.0.0.1:{port};" in csp and "default-src 'none'" in csp
    cfg = json.loads(re.search(r'<script type="application/json" id="cfg">(.*?)</script>', mine.text).group(1))
    assert cfg["key"] == key and cfg["local"] == f"http://127.0.0.1:{port}{PATH}" and cfg["id"] == rid
    assert "<noscript><meta http-equiv=\"refresh\"" in mine.text

    other = get(live, wait)  # a link opened in another browser: no cookie
    assert other.status_code == 200 and 'id="connect"' not in other.text and pair in other.text
    assert "127.0.0.1" not in other.headers["content-security-policy"] and '"key"' not in other.text
    wrong = get(live, wait, headers={"cookie": f"p6b_{rid}=forged"})
    assert 'id="connect"' not in wrong.text


def test_status_check_has_no_side_effect(live):
    wait, rid, _cookie = _start(live)
    store = live["store"]
    r = get(live, wait + "&check=1")
    assert r.status_code == 200 and r.json()["status"] == "pending" and r.json()["left"] > 0
    store.decide(store.pending(rid)["pair"], True)
    assert get(live, wait + "&check=1").json()["status"] == "done"
    assert get(live, wait + "&check=1").json()["status"] == "done"  # checking twice does not consume the code
    back = get(live, wait)
    assert back.status_code == 302 and "code=" in back.headers["location"]
    assert get(live, "/authorize/wait?id=unknown-request-x&check=1").status_code == 404


def test_page_text_is_escaped():
    body = pages.wait_body(lambda k, **kw: "<b>" + k + "</b>" + json.dumps(kw), "<script>", "h", "ABC234", 100,
                           300, True)
    assert "<script>" not in body and "&lt;script&gt;" in body
    html = pages.page("en", "t", body, "/authorize/wait?id=x", "n0nce", {"x": "</script><b>"})
    assert "</script><b>" not in html


# ----------------------------------------------------------------- loopback listener

ORIGIN = "https://p6mcp-relay.example.workers.dev"


@pytest.fixture
def listener(tmp_path):
    store = Store(str(tmp_path / "auth.db"))
    got = []
    port = free_port()
    pl = PairListener(port, lambda: ORIGIN, store.local_hint, lambda req, code: got.append((req["id"], code)))
    assert pl.start()
    yield {"pl": pl, "store": store, "got": got, "port": port}
    pl.stop()


def _call(port, body, origin=ORIGIN, host=None, method="POST"):
    data = json.dumps(body).encode() if body is not None else None
    rq = urllib.request.Request(f"http://127.0.0.1:{port}{PATH}", data=data, method=method)
    if origin:
        rq.add_header("Origin", origin)
    if host:
        rq.add_header("Host", host)
    rq.add_header("Content-Type", "text/plain")
    try:
        with urllib.request.urlopen(rq, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_listener_fills_the_code(listener):
    req = _pending(listener["store"])
    status, headers, body = _call(listener["port"], {"id": req["id"], "code": req["pair"],
                                                     "key": "browser-key-0123456789"})
    assert status == 200 and json.loads(body) == {"ok": True}
    assert headers["Access-Control-Allow-Origin"] == ORIGIN
    assert listener["got"] == [(req["id"], req["pair"])]


def test_listener_refuses(listener):
    req = _pending(listener["store"])
    good = {"id": req["id"], "code": req["pair"], "key": "browser-key-0123456789"}
    port = listener["port"]
    assert _call(port, good, origin="https://evil.example")[0] == 403
    assert _call(port, good, origin=None)[0] == 403
    assert _call(port, good, host=f"rebind.example:{port}")[0] == 403
    assert _call(port, dict(good, code="ZZZZZZ"))[0] == 404
    assert _call(port, dict(good, key="forged"))[0] == 404
    assert _call(port, {"id": req["id"]})[0] == 400
    assert _call(port, {"id": "x" * 2000, "code": "c", "key": "k"})[0] == 400  # body too large
    assert listener["got"] == []


def test_listener_reads_a_refused_body_before_answering(listener):
    """A refusal used to close the socket with the body unread: the reset then often beat the 403 or 400 to the
    client (ConnectionAbortedError on Windows, about one request in seven)."""
    port = listener["port"]
    for _ in range(20):
        assert _call(port, {"id": "a", "code": "b", "key": "c"}, host=f"rebind.example:{port}")[0] == 403
        assert _call(port, {"id": "x" * 2000, "code": "c", "key": "k"})[0] == 400


def test_listener_preflight(listener):
    status, headers, _ = _call(listener["port"], None, method="OPTIONS")
    assert status == 204 and headers["Access-Control-Allow-Origin"] == ORIGIN
    assert headers["Access-Control-Allow-Private-Network"] == "true"
    assert _call(listener["port"], None, origin="https://evil.example", method="OPTIONS")[0] == 403


def test_listener_port_in_use_is_reported(listener):
    other = PairListener(listener["port"], lambda: ORIGIN, lambda *a: None, lambda *a: None)
    assert not other.start() and other.error and not other.running
