"""Skill shop (p6mcp.shop): catalog reads, sharing link, zip files, install on this PC and the p6_shop_* tools.
The network is always faked (an opener serving a catalog from memory)."""

import hashlib
import io
import json
import os
import urllib.parse
import zipfile

import pytest

from p6mcp import __version__, config, settings_logic, shop, skills

SKILL = {"name": "float-watch", "title": "Float watch", "description": "Activities losing float. Use when asked "
         "which activities are close to critical.", "args": [{"name": "project", "description": "Project ID",
                                                               "default": None}],
         "template": "Run p6_float_analysis(project=\"{project}\") and read reference/limits.md.",
         "files": [{"path": "reference/limits.md", "text": "Near critical: total float under 10 days."}]}


class Catalog:
    """index.json and skills/<name>/p6mcp.json served from memory."""

    def __init__(self, *items, author="planner1"):
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []
        entries = []
        for n, it in enumerate(items, 1):
            body = json.dumps(shop.meta(it, version=it.get("version", 1), author=author, license=shop.LICENSE,
                                        issue=n), indent=2).encode("utf-8")
            self.files[f"skills/{it['name']}/{shop.META}"] = body
            entries.append({k: v for k, v in json.loads(body).items() if k not in ("args", "template", "files")}
                           | {"sha256": hashlib.sha256(body).hexdigest()})
        self.files[shop.INDEX] = json.dumps({"schema": 1, "skills": entries}).encode("utf-8")

    def opener(self, req, timeout):
        assert req.full_url.startswith(shop.RAW_PREFIX) and timeout == shop.TIMEOUT
        path = req.full_url[len(shop.RAW_PREFIX):]
        self.calls.append(path)
        if path not in self.files:
            raise OSError(f"404 {path}")
        return io.BytesIO(self.files[path])

    def entry(self, name):
        return next(e for e in shop.fetch_index(self.opener)["skills"] if e["name"] == name)


def test_check_shared():
    assert shop.check_shared(shop.meta(SKILL)) == []
    assert any("kind" in e for e in shop.check_shared(dict(shop.meta(SKILL), kind="other")))
    assert any("needs P6 MCP 99.0.0" in e for e in shop.check_shared(dict(SKILL, min_app_version="99.0.0")))
    assert any("unknown tools p6_nope" in e for e in shop.check_shared(dict(SKILL, template="Use p6_nope.")))
    assert any("built-in" in e for e in shop.check_shared(dict(SKILL, name="delay-whatif")))
    assert any("description is empty" in e for e in shop.check_shared(dict(SKILL, description="")))
    m = shop.meta(SKILL)
    assert m["tools"] == ["p6_float_analysis"] and m["writes"] is False and m["min_app_version"] == __version__
    assert shop.meta(dict(SKILL, template="p6_update_activity"))["writes"] is True


def test_catalog_reads():
    cat = Catalog(SKILL, dict(SKILL, name="another", title="Another"))
    idx = shop.fetch_index(cat.opener)
    assert idx["error"] is None and [e["name"] for e in idx["skills"]] == ["another", "float-watch"]
    item = shop.fetch_skill(cat.entry("float-watch"), cat.opener)
    assert item["template"] == SKILL["template"] and item["author"] == "planner1" and item["sha256"]
    assert shop.search(idx["skills"], "ANOTHER") == [idx["skills"][0]]
    assert shop.search(idx["skills"], "planner1 watch") == [idx["skills"][1]]

    offline = shop.fetch_index(lambda req, timeout: (_ for _ in ()).throw(OSError("offline")))
    assert offline["skills"] == [] and "offline" in offline["error"]
    with pytest.raises(ValueError, match="refusing"):
        shop._get("../other/index.json", cat.opener)
    big = Catalog(SKILL)
    big.files[shop.INDEX] = b" " * (shop.MAX_BYTES + 1)
    assert "larger than" in shop.fetch_index(big.opener)["error"]


def test_fetch_checks_the_index_hash():
    cat = Catalog(SKILL)
    entry = cat.entry("float-watch")
    newer = Catalog(dict(SKILL, version=2, description="Newer text. Use when asked."))
    cat.files[f"skills/float-watch/{shop.META}"] = newer.files[f"skills/float-watch/{shop.META}"]
    with pytest.raises(ValueError, match="does not match the catalog index"):  # the index was not updated yet
        shop.fetch_skill(entry, cat.opener)
    cat.files[shop.INDEX] = newer.files[shop.INDEX]  # now it is: one new read of the index fixes it
    assert shop.fetch_skill(entry, cat.opener)["version"] == 2
    bad = Catalog(dict(SKILL, template="Use p6_nope."))
    with pytest.raises(ValueError, match="unknown tools"):
        shop.fetch_skill(bad.entry("float-watch"), bad.opener)


def test_share_url():
    url, clip = shop.share_url(SKILL)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith(f"{shop.REPO_URL}/issues/new?") and clip is None
    assert q["template"] == [shop.ISSUE_TEMPLATE] and q["title"] == ["Share: float-watch"]
    assert json.loads(q[shop.ISSUE_FIELD][0]) == shop.meta(SKILL)
    long = dict(SKILL, template="Use p6_lookahead. " + "x" * 7500)
    url, clip = shop.share_url(long)
    assert shop.ISSUE_FIELD + "=" not in url and len(url) < 200 and json.loads(clip)["template"] == long["template"]
    with pytest.raises(ValueError, match="description"):
        shop.share_url(dict(SKILL, description=""))


def test_export_and_import(tmp_path):
    path = shop.export_zip(dict(SKILL, enabled=False, source={"shop": "x"}), str(tmp_path / "p6-float-watch.zip"))
    with zipfile.ZipFile(path) as z:
        names = sorted(z.namelist())
        md = z.read("p6-float-watch/SKILL.md").decode("utf-8")
    assert names == ["p6-float-watch/SKILL.md", "p6-float-watch/p6mcp.json", "p6-float-watch/reference/limits.md"]
    assert md.startswith("---\nname: p6-float-watch\n")  # also a skill Claude Desktop can upload
    item = shop.import_file(path)
    assert item == dict(skills.shared_item(SKILL), enabled=True)  # no source: an imported skill is the user's own

    def zipped(entries: dict) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n, t in entries.items():
                z.writestr(n, t)
        return buf.getvalue()

    plain = zipped({"pdf-notes/SKILL.md": "---\nname: pdf-notes\ndescription: 'Notes. Use when asked.'\n---\n"
                                          "# Notes\nRead REFERENCE.md first.", "pdf-notes/REFERENCE.md": "ref"})
    item = shop.import_bytes(plain)
    assert item["name"] == "pdf-notes" and item["description"] == "Notes. Use when asked." and item["args"] == []
    assert item["template"].startswith("# Notes") and item["files"] == [{"path": "REFERENCE.md", "text": "ref"}]
    with pytest.raises(ValueError, match="only text reference files"):
        shop.import_bytes(zipped({"s/SKILL.md": "---\nname: s\ndescription: d\n---\nx", "s/run.py": "print(1)"}))
    with pytest.raises(ValueError, match="unsafe path"):
        shop.import_bytes(zipped({"../SKILL.md": "x"}))
    with pytest.raises(ValueError, match="no SKILL.md"):
        shop.import_bytes(zipped({"notes.txt": "x"}))
    with pytest.raises(ValueError, match="not a zip"):
        shop.import_bytes(b"plain text")
    assert shop.import_bytes(json.dumps(shop.meta(SKILL)).encode(), "skill.json")["name"] == "float-watch"


def test_install_update_and_conflicts():
    cat = Catalog(SKILL)
    entry = cat.entry("float-watch")
    item = shop.fetch_skill(entry, cat.opener)
    data = config.validate({})
    assert shop.status(data, entry) == shop.AVAILABLE
    data = shop.install(data, item)
    stored = data["custom_skills"][0]
    assert stored["source"] == {"shop": "float-watch", "version": 1, "sha256": entry["sha256"]}
    assert shop.status(data, entry) == shop.INSTALLED and config.validate(data)["custom_skills"] == [stored]
    data = settings_logic.set_skill_enabled(data, "float-watch", False)

    newer = Catalog(dict(SKILL, version=2, description="Newer. Use when asked."))
    e2 = newer.entry("float-watch")
    assert shop.status(data, e2) == shop.UPDATE
    data = shop.install(data, shop.fetch_skill(e2, newer.opener))
    assert len(data["custom_skills"]) == 1 and data["custom_skills"][0]["description"] == "Newer. Use when asked."
    assert data["custom_skills"][0]["enabled"] is False and data["custom_skills"][0]["source"]["version"] == 2

    mine = settings_logic.put_skill(config.validate({}), dict(SKILL, description="Mine. Use when asked."))
    assert shop.status(mine, entry) == shop.CONFLICT
    with pytest.raises(ValueError, match="already have a skill named"):
        shop.install(mine, item)
    both = shop.install(mine, item, as_name="shop-float-watch")
    assert [c["name"] for c in both["custom_skills"]] == ["float-watch", "shop-float-watch"]
    assert shop.installed_as(both, "float-watch")["name"] == "shop-float-watch"
    assert shop.status(both, entry) == shop.INSTALLED
    assert shop.status(config.validate({}), dict(entry, min_app_version="99.0.0")) == shop.NEWER_APP


# ----------------------------------------------------------------- p6_shop_* tools

@pytest.fixture
def shop_server(server, p6, tmp_path, monkeypatch):
    for var, sub in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local"), ("USERPROFILE", "User")):
        (tmp_path / sub).mkdir(exist_ok=True)
        monkeypatch.setenv(var, str(tmp_path / sub))  # the clients' skills folders of a fake profile
    cat = Catalog(SKILL, dict(SKILL, name="bulk-update", title="Bulk update", template="Use p6_update_activity.",
                              description="Updates activities. Use when asked to update many."))
    index, fetch = shop.fetch_index, shop.fetch_skill  # server._shop is this module: keep the real functions
    monkeypatch.setattr(server._shop, "fetch_index", lambda opener=None: index(cat.opener))
    monkeypatch.setattr(server._shop, "fetch_skill", lambda entry, opener=None: fetch(entry, cat.opener))
    return p6


def test_shop_tools(shop_server, server, tmp_path):
    p6 = shop_server
    res = p6.call("p6_shop_search", query="update")
    assert res["total"] == 1 and res["rows"][0]["name"] == "bulk-update" and res["rows"][0]["writes"] is True
    assert res["rows"][0]["status"] == "available" and res["catalog"] == shop.REPO_URL
    got = p6.call("p6_shop_get", name="float-watch")
    assert got["template"] == SKILL["template"] and got["files"] == SKILL["files"] and got["missing_tools"] == []
    assert got["issue_url"].endswith("/issues/1") and got["status"] == "available"
    with pytest.raises(ValueError, match="close names: float-watch"):
        p6.call("p6_shop_get", name="float-watc")

    skills_dir = tmp_path / "User" / ".claude" / "skills"
    from p6mcp import clients
    clients.get("claude_code").install_skills("full", True)  # a client that already has P6 skills
    dry = p6.call("p6_shop_install", name="float-watch")
    assert dry["dry_run"] and dry["confirmation_token"] and "P6 must be closed" not in dry["next"]
    assert str(skills_dir) in dry["would_do"][2] and dry["skill"]["template"] == SKILL["template"]
    assert "float-watch" not in json.dumps(config.read_file()[0]["custom_skills"])  # nothing written yet
    with pytest.raises(ValueError):
        p6.call("p6_shop_install", name="float-watch", dry_run=False, confirmation_token="wrong")
    res = p6.call("p6_shop_install", name="float-watch", dry_run=False, confirmation_token=dry["confirmation_token"])
    assert res["applied"] and res["skills_folders_refreshed"] == [str(skills_dir)]
    saved = config.read_file()[0]["custom_skills"]
    assert saved[0]["name"] == "float-watch" and saved[0]["source"]["shop"] == "float-watch"
    assert (skills_dir / "p6-float-watch" / "reference" / "limits.md").exists()
    assert p6.call("p6_shop_search")["rows"][1]["status"] == "installed"

    p6.set(shop_enabled=False)
    with pytest.raises(ValueError, match="skill shop is turned off"):
        p6.call("p6_shop_search")
    assert server.p6_diagnose()["shop"]["enabled"] is False
    p6.set(shop_enabled=True, allow_writes=False)  # settings only: no database write permission needed
    assert p6.call("p6_shop_install", name="bulk-update")["dry_run"]
    hints = server.TOOL_HINTS
    assert hints["p6_shop_search"]["openWorldHint"] and hints["p6_shop_install"]["openWorldHint"]
    assert not hints["p6_list_projects"]["openWorldHint"] and os.path.exists(os.environ["P6MCP_CONFIG"])
