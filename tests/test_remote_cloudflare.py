"""Remote access, milestone M3: the Cloudflare setup against a fake Cloudflare API (response shapes read back from
a real account on 2026-11-26), the settings and secrets it leaves, the supervised server and tunnel, keep-awake,
the relay Worker (run with Node when installed) and the cloudflared pin. No network."""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from urllib.parse import parse_qs, urlparse

import pytest

from p6mcp import config, supervisor
from p6mcp.remote import cloudflare as cf
from p6mcp.remote import manage, supervise
from p6mcp.supervisor import DISABLED, ERROR, RUNNING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCT = "a" * 32
API = "https://api.cloudflare.com/client/v4"


class FakeCloudflare:
    """The Cloudflare API calls the setup makes, in memory."""

    def __init__(self, accounts=((ACCT, "Someone's Account"),), missing=(), subdomain="someone", zones=()):
        self.accounts = [{"id": a, "name": n} for a, n in accounts]
        self.missing = set(missing)  # permission names the token lacks
        self.subdomain = subdomain
        self.tunnels, self.services, self.scripts, self.script_sub = {}, {}, {}, {}
        self.tunnel_config = {}
        self.zones = {z: uuid.uuid4().hex for z in zones}
        self.dns = {}
        self.calls = []

    def ok(self, result, status=200):
        return status, json.dumps({"success": True, "errors": [], "result": result}).encode()

    def err(self, status, code, msg):
        doc = {"success": False, "errors": [{"code": code, "message": msg}], "result": None}
        return status, json.dumps(doc).encode()

    def __call__(self, method, url, headers, body):
        assert url.startswith(API) and headers["Authorization"] == "Bearer tok"
        u = urlparse(url)
        path, q = u.path[len("/client/v4"):], {k: v[0] for k, v in parse_qs(u.query).items()}
        self.calls.append((method, path))
        need = {"/cfd_tunnel": "Cloudflare Tunnel: Edit", "/workers/": "Workers Scripts: Edit",
                "/connectivity/": "Connectivity Directory: Admin", "/zones": "Zone"}
        for frag, perm in need.items():
            if frag in path and perm in self.missing:
                return self.err(403, 10000, "Authentication error")
        if path == "/accounts":
            return self.ok(self.accounts)
        if path.startswith("/zones"):
            return self.zone_call(method, path, q, body)
        m = re.match(r"^/accounts/([0-9a-f]{32})(/.*)$", path)
        assert m and m.group(1) in {a["id"] for a in self.accounts}, path
        sub = m.group(2)
        if sub == "/cfd_tunnel" and method == "GET":
            found = [t for t in self.tunnels.values() if t["name"] == q.get("name")] if "name" in q else []
            return self.ok(found if "name" in q else list(self.tunnels.values())[:1])
        if sub == "/cfd_tunnel" and method == "POST":
            b = json.loads(body)
            tid = str(uuid.uuid4())
            self.tunnels[tid] = {"id": tid, "name": b["name"], "config_src": b["config_src"], "tun_type": "cfd_tunnel"}
            return self.ok(self.tunnels[tid])
        m2 = re.match(r"^/cfd_tunnel/([0-9a-f-]{36})(/token|/configurations|/connections)?$", sub)
        if m2:
            tid, tail = m2.group(1), m2.group(2)
            if tid not in self.tunnels:
                return self.err(404, 1003, "tunnel not found")
            if tail == "/token":
                return self.ok("eyJ" + tid.replace("-", ""))
            if tail == "/configurations":
                self.tunnel_config[tid] = json.loads(body)
                return self.ok({"tunnel_id": tid})
            if tail == "/connections":
                return self.ok(None)
            if method == "DELETE":
                del self.tunnels[tid]
                return self.ok(None)
        if sub.startswith("/connectivity/directory/services"):
            rest = sub[len("/connectivity/directory/services"):]
            if method == "GET" and not rest:
                return self.ok(list(self.services.values()))
            if method == "POST":
                b = json.loads(body)
                assert b["type"] == "http" and b["host"]["ipv4"] == "127.0.0.1"
                assert b["host"]["network"]["tunnel_id"] in self.tunnels
                sid = str(uuid.uuid4())
                self.services[sid] = dict(b, service_id=sid)
                return self.ok(self.services[sid])
            sid = rest.strip("/")
            if sid not in self.services:
                return self.err(404, 1004, "service not found")
            if method == "PUT":
                self.services[sid] = dict(json.loads(body), service_id=sid)
                return self.ok(self.services[sid])
            if method == "DELETE":
                del self.services[sid]
                return self.ok(None)
        if sub == "/workers/scripts" and method == "GET":
            return self.ok([{"id": n} for n in self.scripts])
        if sub == "/workers/subdomain":
            if method == "GET":
                return self.ok({"subdomain": self.subdomain}) if self.subdomain else self.err(404, 10007, "none")
            self.subdomain = json.loads(body)["subdomain"]
            return self.ok({"subdomain": self.subdomain})
        m3 = re.match(r"^/workers/scripts/([a-z0-9-]+)(/subdomain)?$", sub)
        if m3:
            name, tail = m3.groups()
            if tail:
                self.script_sub[name] = json.loads(body)
                return self.ok(self.script_sub[name])
            if method == "PUT":
                self.scripts[name] = self.parse_multipart(headers["Content-Type"], body)
                return self.ok({"id": name})
            if method == "DELETE":
                self.scripts.pop(name, None)
                self.script_sub.pop(name, None)
                return self.ok(None)
        raise AssertionError(f"unexpected call {method} {path}")

    def zone_call(self, method, path, q, body):
        if path == "/zones":
            return self.ok([{"id": self.zones[q["name"]], "name": q["name"]}] if q.get("name") in self.zones else [])
        m = re.match(r"^/zones/([0-9a-f]{32})/dns_records(?:/([0-9a-f]+))?$", path)
        zid, rid = m.groups()
        if method == "GET":
            return self.ok([r for r in self.dns.values() if r["zone"] == zid and r["name"] == q.get("name")])
        if method == "POST":
            b = json.loads(body)
            rid = uuid.uuid4().hex
            self.dns[rid] = dict(b, id=rid, zone=zid)
            return self.ok(self.dns[rid])
        if method == "DELETE":
            self.dns.pop(rid, None)
            return self.ok(None)
        raise AssertionError(path)

    @staticmethod
    def parse_multipart(ctype, body):
        boundary = ctype.split("boundary=")[1].encode()
        parts = {}
        for chunk in body.split(b"--" + boundary)[1:-1]:
            head, data = chunk.split(b"\r\n\r\n", 1)
            name = re.search(rb'name="([^"]+)"', head).group(1).decode()
            parts[name] = data[:-2]
        meta = json.loads(parts["metadata"])
        return {"meta": meta, "module": parts[meta["main_module"]].decode()}


# ----------------------------------------------------------------- setup, repair, remove

def test_relay_setup_creates_everything():
    fake = FakeCloudflare()
    seen = []
    r = cf.setup("tok", "relay", 39218, machine="ENG-PC 07", transport=fake, progress=seen.append)
    assert r.public_host == "p6mcp-eng-pc-07.someone.workers.dev" and r.mode == "relay"
    assert r.tunnel_token.startswith("eyJ") and r.relay_secret and len(r.relay_secret) >= 40
    t = fake.tunnels[r.tunnel_id]
    assert t["name"] == "p6mcp-eng-pc-07" and t["config_src"] == "cloudflare"
    svc = fake.services[r.service_id]
    assert svc["http_port"] == 39218 and svc["host"]["network"]["tunnel_id"] == r.tunnel_id
    w = fake.scripts["p6mcp-eng-pc-07"]
    binds = {b["name"]: b for b in w["meta"]["bindings"]}
    assert binds["P6"] == {"type": "vpc_service", "name": "P6", "service_id": r.service_id}
    assert binds["RELAY_SECRET"]["text"] == r.relay_secret and binds["P6_PORT"]["text"] == "39218"
    assert w["module"] == cf.worker_source() and fake.script_sub["p6mcp-eng-pc-07"]["enabled"] is True
    assert "Token checked" in seen and any("workers.dev" in s for s in seen)
    ids = r.ids()
    assert set(ids) == {"account_id", "tunnel_id", "service_id", "script", "zone_id", "dns_record_id"}
    assert "token" not in json.dumps(ids).lower().replace("tunnel_id", "")  # ids only


def test_repair_reuses_objects_and_keeps_the_secret():
    fake = FakeCloudflare()
    first = cf.setup("tok", "relay", 39218, machine="pc", transport=fake)
    again = cf.setup("tok", "relay", 39218, machine="pc", relay_secret=first.relay_secret, transport=fake)
    assert again.tunnel_id == first.tunnel_id and again.service_id == first.service_id
    assert len(fake.tunnels) == 1 and len(fake.services) == 1 and again.relay_secret == first.relay_secret
    moved = cf.setup("tok", "relay", 40000, machine="pc", relay_secret=first.relay_secret, transport=fake)
    assert moved.service_id == first.service_id and fake.services[first.service_id]["http_port"] == 40000


def test_missing_permission_creates_nothing():
    fake = FakeCloudflare(missing={"Connectivity Directory: Admin"})
    with pytest.raises(cf.CloudflareError, match="Connectivity Directory: Admin"):
        cf.setup("tok", "relay", machine="pc", transport=fake)
    assert not fake.tunnels and not fake.scripts
    with pytest.raises(cf.CloudflareError, match="Cloudflare Tunnel: Edit"):
        cf.setup("tok", "relay", machine="pc", transport=FakeCloudflare(missing={"Cloudflare Tunnel: Edit"}))


def test_account_choice_and_new_subdomain():
    two = FakeCloudflare(accounts=((ACCT, "A"), ("b" * 32, "B")))
    with pytest.raises(cf.NeedsAccount) as e:
        cf.setup("tok", transport=two)
    assert [n for _, n in e.value.accounts] == ["A", "B"]
    assert cf.setup("tok", account_id="b" * 32, machine="pc", transport=two).account_id == "b" * 32
    fresh = FakeCloudflare(subdomain=None)
    r = cf.setup("tok", machine="pc", transport=fresh)
    assert fresh.subdomain == "pc" and r.public_host == "p6mcp-pc.pc.workers.dev"
    with pytest.raises(cf.CloudflareError, match="Paste"):
        cf.setup("  ", transport=fresh)


def test_domain_mode_and_dns_conflict():
    fake = FakeCloudflare(zones=("example.com",))
    r = cf.setup("tok", "domain", 39218, domain_host="P6.Example.com", machine="pc", transport=fake)
    assert r.public_host == "p6.example.com" and r.relay_secret is None and r.zone_id and r.dns_record_id
    assert fake.dns[r.dns_record_id]["content"] == f"{r.tunnel_id}.cfargotunnel.com"
    assert fake.dns[r.dns_record_id]["proxied"] is True
    ingress = fake.tunnel_config[r.tunnel_id]["config"]["ingress"]
    assert ingress[0] == {"hostname": "p6.example.com", "service": "http://127.0.0.1:39218"}
    assert cf.setup("tok", "domain", domain_host="p6.example.com", machine="pc", transport=fake).dns_record_id \
        == r.dns_record_id  # repair finds its own record
    fake.dns["x" * 32] = {"id": "x" * 32, "zone": r.zone_id, "name": "app.example.com", "type": "A",
                          "content": "1.2.3.4"}
    with pytest.raises(cf.CloudflareError, match="already has a DNS record"):
        cf.setup("tok", "domain", domain_host="app.example.com", machine="pc", transport=fake)
    with pytest.raises(cf.CloudflareError, match="No zone"):
        cf.setup("tok", "domain", domain_host="p6.other.org", machine="pc", transport=fake)


def test_remove_deletes_what_setup_created():
    fake = FakeCloudflare()
    r = cf.setup("tok", machine="pc", transport=fake)
    done = cf.remove("tok", r.ids(), transport=fake)
    assert not fake.tunnels and not fake.services and not fake.scripts
    assert done == ["Relay Worker deleted", "Private link deleted", "Tunnel deleted"]
    assert cf.remove("tok", r.ids(), transport=fake)  # a second removal skips what is gone


def test_errors_do_not_show_the_account_id():
    fake = FakeCloudflare(missing={"Workers Scripts: Edit"})
    with pytest.raises(cf.CloudflareError) as e:
        cf.Cloudflare("tok", fake).call("GET", f"/accounts/{ACCT}/workers/scripts")
    assert ACCT not in str(e.value) and "<account>" in str(e.value)


def test_worker_and_slug():
    assert re.fullmatch(r"\d+\.\d+\.\d+-\d+", cf.worker_version())
    assert cf.machine_slug("ÉQUIPE_Planning PC!") == "p6mcp-quipe-planning-pc"
    assert cf.machine_slug("x" * 80).startswith("p6mcp-") and len(cf.machine_slug("x" * 80)) <= 46


# ----------------------------------------------------------------- settings and secrets

@pytest.fixture
def vault(monkeypatch):
    box = {}
    monkeypatch.setattr(manage.credentials, "set", lambda k, v: box.__setitem__(k, v))
    monkeypatch.setattr(manage.credentials, "get", lambda k: box.get(k))
    monkeypatch.setattr(manage.credentials, "delete", lambda k: box.pop(k, None) is not None)
    return box


def test_apply_setup_and_clear(vault):
    r = cf.setup("tok", machine="pc", transport=FakeCloudflare())
    data = manage.apply_setup({"remote_enabled": True}, r)
    assert vault == {"remote:tunnel_token": r.tunnel_token, "remote:relay_secret": r.relay_secret}
    assert data["remote_public_host"] == r.public_host and data["remote_cf"]["tunnel_id"] == r.tunnel_id
    out = config.validate(data)  # secrets are not in the file, and the ids validate
    assert out["remote_cf"]["script"] == "p6mcp-pc"
    assert manage.relay_secret() == r.relay_secret
    cleared = manage.clear(data)
    assert vault == {} and cleared["remote_enabled"] is False and cleared["remote_cf"] is None


def test_remote_cf_validation():
    with pytest.raises(config.ConfigError, match="remote_cf"):
        config.validate({"remote_cf": {"tunnel_id": 3}})
    with pytest.raises(config.ConfigError, match="remote_cf"):
        config.validate({"remote_cf": {"other": "x"}})


def test_cli_forget(vault, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from p6mcp.remote import cli
    from p6mcp.remote.store import Store, default_path
    Store()  # auth.db exists
    from p6mcp.remote.files import root
    os.makedirs(os.path.join(root(), "a" * 32, "f1"))  # a file waiting for a remote client
    vault.update({"remote:tunnel_token": "t", "remote:relay_secret": "s"})
    monkeypatch.setattr("p6mcp.credentials.delete", lambda k: vault.pop(k, None) is not None)
    out = io.StringIO()
    assert cli.main(["forget"], out=out) == 0 and vault == {} and not os.path.exists(default_path())
    assert not os.path.exists(root())


# ----------------------------------------------------------------- supervised server and tunnel

def _settings(tmp_path, **remote):
    cfg = dict({"remote_enabled": True, "remote_privacy_ack": True, "remote_public_host": "p6mcp-pc.x.workers.dev",
                "remote_port": 41990}, **remote)
    path = tmp_path / "config.json"
    config.save(cfg, str(path))
    return lambda: config.load()


@pytest.fixture
def child_env(tmp_path, monkeypatch):
    monkeypatch.setenv("P6MCP_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    return tmp_path


def sleeper(tmp_path, name):
    """A child that writes its TUNNEL_TOKEN (if any) to a file and sleeps."""
    out = tmp_path / f"{name}.env"
    code = (f"import os,time; open(r'{out}','w').write(os.environ.get('TUNNEL_TOKEN','')); time.sleep(60)")
    return lambda key: [sys.executable, "-c", code], out


def running_server(tmp_path):
    """A child that reports itself running in the remote status file (as the remote access server does)."""
    import p6mcp
    root = os.path.dirname(os.path.dirname(p6mcp.__file__))
    code = (f"import sys,time; sys.path.insert(0, r'{root}'); from p6mcp import runtime; "
            "runtime.write_status('running', 41990, name='remote'); time.sleep(60)")
    return lambda port: [sys.executable, "-c", code]


def test_tunnel_waits_for_our_server(child_env):
    tmp = child_env
    tun_cmd, tun_env = sleeper(tmp, "tunnel-early")
    srv_cmd, _ = sleeper(tmp, "silent-server")  # alive, but never reports running (another program has the port)
    host = supervise.RemoteHost(settings=_settings(tmp), token=lambda: "tok", server_cmd=srv_cmd,
                                tunnel_cmd=tun_cmd, awake=lambda on: None)
    try:
        for _ in range(3):
            st = host.tick()
            time.sleep(0.3)
        assert host.tunnel.proc is None and st.tunnel.state == "starting" and not tun_env.exists()
    finally:
        host.stop()


def test_remote_host_runs_both_and_keeps_awake(child_env, monkeypatch):
    tmp = child_env
    settings = _settings(tmp)
    srv_cmd = running_server(tmp)
    tun_cmd, tun_env = sleeper(tmp, "tunnel")
    awake = []
    host = supervise.RemoteHost(settings=settings, token=lambda: "tunnel-token-123", server_cmd=srv_cmd,
                                tunnel_cmd=tun_cmd, awake=awake.append)
    try:
        st = host.tick()
        assert st.server.state != DISABLED and awake == [True]
        deadline = time.monotonic() + 20
        while host.tick().server.state != RUNNING:  # the tunnel starts once our server reports running
            assert time.monotonic() < deadline
            time.sleep(0.2)
        host.tick()
        deadline = time.monotonic() + 15
        while not tun_env.exists() or not tun_env.read_text():
            assert time.monotonic() < deadline
            time.sleep(0.1)
        assert tun_env.read_text() == "tunnel-token-123"  # through the environment
        assert "tunnel-token-123" not in " ".join(map(str, host.tunnel.proc.args))  # never on the command line
        monkeypatch.setattr(supervisor, "READY_AFTER", 0.2)  # 3 s in the product: not waited for here
        time.sleep(0.25)
        assert host.tick().tunnel.state == RUNNING  # no status file: alive for READY_AFTER seconds
    finally:
        host.stop()
    assert awake == [True, False] and host.server.proc is None and host.tunnel.proc is None


def test_remote_host_off_or_incomplete(child_env):
    tmp = child_env
    host = supervise.RemoteHost(settings=_settings(tmp, remote_enabled=False), token=lambda: "t",
                                server_cmd=lambda p: ["x"], tunnel_cmd=lambda k: ["x"], awake=lambda on: None)
    assert host.tick().state == DISABLED
    host = supervise.RemoteHost(settings=_settings(tmp, remote_privacy_ack=False), token=lambda: "t",
                                server_cmd=lambda p: ["x"], tunnel_cmd=lambda k: ["x"], awake=lambda on: None)
    assert host.tick().state == DISABLED
    srv_cmd, _ = sleeper(tmp, "server2")
    host = supervise.RemoteHost(settings=_settings(tmp), token=lambda: None, server_cmd=srv_cmd,
                                tunnel_cmd=lambda k: ["x"], awake=lambda on: None)
    try:
        st = host.tick()
        assert st.state == ERROR and "tunnel token is missing" in st.error and st.tunnel.state == DISABLED
    finally:
        host.stop()


def test_tunnel_key_never_holds_the_token(child_env):
    host = supervise.RemoteHost(settings=_settings(child_env), token=lambda: "secret-tunnel-token",
                                server_cmd=lambda p: ["x"], tunnel_cmd=lambda k: ["x"], awake=lambda on: None)
    host._token = "secret-tunnel-token"
    host._server_up = True
    key = host._want_tunnel(config.load())
    assert "secret" not in key and key.startswith("p6mcp-pc.x.workers.dev ")


def test_cloudflared_path_installed(monkeypatch, tmp_path):
    """The installed layout of PyInstaller 6 (checked in the 3.2.0 draft's portable zip): P6MCP\\p6mcp-settings.exe
    and P6MCP\\_internal\\cloudflared\\cloudflared.exe; never a cloudflared from PATH or the environment."""
    app = tmp_path / "P6MCP"
    (app / "_internal" / "cloudflared").mkdir(parents=True)
    exe = app / "_internal" / "cloudflared" / "cloudflared.exe"
    exe.write_bytes(b"MZ")
    other = tmp_path / "other.exe"
    other.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "p6mcp-settings.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(app / "_internal"), raising=False)
    monkeypatch.setenv(supervise.CLOUDFLARED_ENV, str(other))
    assert supervise.cloudflared_path() == str(exe)
    exe.unlink()
    assert supervise.cloudflared_path() is None  # missing: no fallback to another cloudflared


def test_cloudflared_path(monkeypatch, tmp_path):
    exe = tmp_path / "cloudflared.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setenv(supervise.CLOUDFLARED_ENV, str(exe))
    assert supervise.cloudflared_path() == str(exe)
    assert supervise.tunnel_command("k")[1:] == ["tunnel", "--no-autoupdate", "--loglevel", "info", "run"]


# ----------------------------------------------------------------- relay Worker and the bundled connector

@pytest.mark.slow
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_worker_with_node(tmp_path):
    src = os.path.join(ROOT, "p6mcp", "remote", "worker.js")
    shutil.copyfile(src, tmp_path / "worker.mjs")
    (tmp_path / "run.mjs").write_text("""
import worker from "./worker.mjs";
const seen = [];
const env = { RELAY_SECRET: "s3", P6_PORT: "41234", P6: { fetch: async (url, init) => {
  seen.push({ url, relay: init.headers.get("X-P6MCP-Relay"), ip: init.headers.get("X-P6MCP-Client-IP"),
              method: init.method, redirect: init.redirect });
  return new Response("ok", { status: 201, headers: { "x-up": "1" } }); } } };
const req = new Request("https://p6mcp-pc.x.workers.dev/mcp?a=1", { method: "POST", body: "{}",
  headers: { "CF-Connecting-IP": "160.79.106.1", "X-P6MCP-Relay": "forged" } });
const r = await worker.fetch(req, env);
const off = { RELAY_SECRET: "s3", P6: { fetch: async () => { throw new Error("no tunnel"); } } };
const down = await worker.fetch(new Request("https://h/x"), off);
const rpc = await worker.fetch(new Request("https://h/mcp", { method: "POST", body: JSON.stringify(
  { jsonrpc: "2.0", id: 7, method: "tools/call" }) }), off);
const rpcBody = await rpc.json();
const noPort = { RELAY_SECRET: "s3", P6: { fetch: async () => new Response("bad gateway", { status: 502 }) } };
const gw = await worker.fetch(new Request("https://h/mcp", { method: "POST", body: '{"jsonrpc":"2.0","id":"a"}' }),
  noPort);
const gwBody = await gw.json();
const note = await worker.fetch(new Request("https://h/mcp", { method: "POST", body: '{"jsonrpc":"2.0"}' }), off);
// file handoff pages: path and body forwarded unchanged, the answer's headers kept (download, upload)
const files = [];
const fileEnv = { RELAY_SECRET: "s3", P6_PORT: "41234", P6: { fetch: async (url, init) => {
  files.push({ url, method: init.method, relay: init.headers.get("X-P6MCP-Relay"),
               body: init.body ? new Uint8Array(init.body).length : 0, type: init.headers.get("content-type") });
  return new Response("ERMHDR", { status: 200, headers: { "content-disposition": 'attachment; filename="a.xer"' } });
} } };
const dl = await worker.fetch(new Request("https://h/files/tok_1", { method: "POST" }), fileEnv);
const form = new FormData();
form.append("file", new Blob([new Uint8Array(300000)]), "big.xer");
const upl = await worker.fetch(new Request("https://h/upload/tok_2", { method: "POST", body: form }), fileEnv);
const offFile = await worker.fetch(new Request("https://h/files/tok_3", { method: "POST" }), off);
console.log(JSON.stringify({ seen, status: r.status, up: r.headers.get("x-up"), ver: r.headers.get("x-p6mcp-relay"),
  down: down.status, downVer: down.headers.get("x-p6mcp-relay"), rpcStatus: rpc.status, rpcBody,
  gwBody, noteStatus: note.status, files, dlDisp: dl.headers.get("content-disposition"),
  dlBody: await dl.text(), uplStatus: upl.status, offFile: offFile.status }));
""", encoding="utf-8")
    # The script makes no network call and has no wait: it runs in well under a second. A busy CI runner once took
    # more than 60 s just to start node (the job ran at half speed), so the limit only guards against a hang.
    try:
        out = subprocess.run(["node", str(tmp_path / "run.mjs")], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        pytest.fail("node did not finish in 300 s: a hung process or an overloaded machine, not a Worker result")
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["seen"] == [{"url": "http://127.0.0.1:41234/mcp?a=1", "relay": "s3", "ip": "160.79.106.1",
                            "method": "POST", "redirect": "manual"}]  # a forged relay header is replaced
    assert res["status"] == 201 and res["up"] == "1" and res["ver"] == cf.worker_version()
    assert res["down"] == 503 and res["downVer"] == cf.worker_version()
    # an MCP request while the PC is off: a JSON-RPC error the AI app shows, with the reason
    assert res["rpcStatus"] == 200 and res["rpcBody"]["id"] == 7 and "PC is not reachable" in \
        res["rpcBody"]["error"]["message"]
    assert res["gwBody"]["id"] == "a" and res["gwBody"]["error"]["code"] == -32000  # tunnel up, nothing listening
    assert res["noteStatus"] == 503  # a notification has no id to answer
    # file handoff: /files/<token> and /upload/<token> reach the PC unchanged (a 300 KB multipart upload included)
    dl, up = res["files"]
    assert dl == {"url": "http://127.0.0.1:41234/files/tok_1", "method": "POST", "relay": "s3", "body": 0,
                  "type": None}
    assert up["url"] == "http://127.0.0.1:41234/upload/tok_2" and up["body"] > 300000
    assert up["type"].startswith("multipart/form-data; boundary=")
    assert res["dlDisp"].startswith("attachment") and res["dlBody"] == "ERMHDR" and res["uplStatus"] == 200
    assert res["offFile"] == 503  # PC off: a plain 503 with the reason, not a JSON-RPC answer


def test_cloudflared_pin_and_build_wiring():
    sys.path.insert(0, os.path.join(ROOT, "packaging"))
    try:
        import cloudflared as pin
    finally:
        sys.path.pop(0)
    assert re.fullmatch(r"\d{4}\.\d{1,2}\.\d+", pin.VERSION) and re.fullmatch(r"[0-9a-f]{64}", pin.SHA256)
    assert pin.URL.startswith("https://github.com/cloudflare/cloudflared/releases/download/")
    spec = open(os.path.join(ROOT, "packaging", "p6mcp.spec"), encoding="utf-8").read()
    assert "cloudflared.exe" in spec and "worker.js" in spec
    assert "packaging\\cloudflared.py" in open(os.path.join(ROOT, "packaging", "build.ps1"), encoding="utf-8").read()
    rel = open(os.path.join(ROOT, ".github", "workflows", "release.yml"), encoding="utf-8").read()
    assert "python packaging/cloudflared.py" in rel
    iss = open(os.path.join(ROOT, "packaging", "installer.iss"), encoding="utf-8").read()
    assert 'Parameters: "remote forget"' in iss
    assert "cloudflared" in open(os.path.join(ROOT, "NOTICE"), encoding="utf-8").read()
