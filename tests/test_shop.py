"""Skill shop (p6mcp.shop): catalog reads, sharing link, zip files, install on this PC and the p6_shop_* tools;
the core set (p6mcp.core_skills): first start online and offline, Restore core skills, automatic updates.
The network is always faked (an opener serving a catalog from memory)."""

import hashlib
import io
import json
import os
import urllib.parse
import zipfile

import pytest

from p6mcp import __version__, config, core_skills, settings_logic, shop, skills

SKILL = {"name": "float-watch", "title": "Float watch", "description": "Activities losing float. Use when asked "
         "which activities are close to critical.", "args": [{"name": "project", "description": "Project ID",
                                                               "default": None}],
         "template": "Run p6_float_analysis(project=\"{project}\") and read reference/limits.md.",
         "files": [{"path": "reference/limits.md", "text": "Near critical: total float under 10 days."}]}
CORE = core_skills.names()  # the bundled copy of the core set


def builtin_entry(name: str) -> dict:
    """A built-in skill entry of a catalog written for P6 MCP 1.8.2."""
    return {"schema": 1, "kind": shop.KIND, "builtin": True, "name": name, "title": name, "args": [],
            "description": "What-if of delaying one activity. Use when asked.", "template": "{rules}", "files": [],
            "tools": [], "writes": False, "min_app_version": "1.8.2", "version": 1, "author": "nizarhamza",
            "license": shop.LICENSE}


class Catalog:
    """index.json and skills/<name>/p6mcp.json served from memory. core: names flagged core in the index; raw:
    {name: exact p6mcp.json bytes} (the bundled copy as GitHub serves it)."""

    def __init__(self, *items, author="planner1", builtins=(), core=(), raw=None):
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.entries: list[dict] = []
        for n, it in enumerate(items, 1):
            meta = shop.meta(it, version=it.get("version", 1), author=author, license=shop.LICENSE, issue=n)
            self.put(json.dumps(meta, indent=2).encode("utf-8"), core)
        for name in builtins:
            self.put(json.dumps(builtin_entry(name), indent=2).encode("utf-8"), core)
        for body in (raw or {}).values():
            self.put(body, core)

    def put(self, body: bytes, core=()) -> None:
        it = json.loads(body)
        self.files[f"skills/{it['name']}/{shop.META}"] = body
        e = {k: v for k, v in it.items() if k not in ("args", "template", "files")}
        e["sha256"] = hashlib.sha256(body).hexdigest()
        if it["name"] in core:
            e["core"] = True
        self.entries = [x for x in self.entries if x["name"] != it["name"]] + [e]
        self.files[shop.INDEX] = json.dumps({"schema": 1, "skills": self.entries}).encode("utf-8")

    def opener(self, req, timeout):
        assert req.full_url.startswith(shop.RAW_PREFIX) and timeout == shop.TIMEOUT
        path = req.full_url[len(shop.RAW_PREFIX):]
        self.calls.append(path)
        if path not in self.files:
            raise OSError(f"404 {path}")
        return io.BytesIO(self.files[path])

    def entry(self, name):
        return next(e for e in shop.fetch_index(self.opener)["skills"] if e["name"] == name)


def offline(req, timeout):
    raise OSError("offline")


def newer(name: str) -> dict:
    """The bundled core skill as the live shop's next version."""
    it = core_skills.bundled(name)
    it.pop("sha256")
    return dict(it, version=it["version"] + 1, description=it["description"] + " Newer.")


def bundle_bytes(name: str) -> bytes:
    with open(os.path.join(core_skills.BUNDLE_DIR, name + ".json"), "rb") as fh:
        return fh.read()


def by_name(data: dict) -> dict:
    return {c["name"]: c for c in data["custom_skills"]}


def test_check_shared():
    assert shop.check_shared(shop.meta(SKILL)) == []
    assert any("kind" in e for e in shop.check_shared(dict(shop.meta(SKILL), kind="other")))
    assert any("needs P6 MCP 99.0.0" in e for e in shop.check_shared(dict(SKILL, min_app_version="99.0.0")))
    assert any("unknown tools p6_nope" in e for e in shop.check_shared(dict(SKILL, template="Use p6_nope.")))
    assert shop.check_shared(dict(SKILL, name="delay-whatif")) == []  # no built-in name reserved since 1.9
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

    off = shop.fetch_index(offline)
    assert off["skills"] == [] and "offline" in off["error"]
    with pytest.raises(ValueError, match="refusing"):
        shop._get("../other/index.json", cat.opener)
    big = Catalog(SKILL)
    big.files[shop.INDEX] = b" " * (shop.MAX_BYTES + 1)
    assert "larger than" in shop.fetch_index(big.opener)["error"]


def test_fetch_checks_the_index_hash():
    cat = Catalog(SKILL)
    entry = cat.entry("float-watch")
    newer_cat = Catalog(dict(SKILL, version=2, description="Newer text. Use when asked."))
    cat.files[f"skills/float-watch/{shop.META}"] = newer_cat.files[f"skills/float-watch/{shop.META}"]
    with pytest.raises(ValueError, match="does not match the catalog index"):  # the index was not updated yet
        shop.fetch_skill(entry, cat.opener)
    cat.files[shop.INDEX] = newer_cat.files[shop.INDEX]  # now it is: one new read of the index fixes it
    assert shop.fetch_skill(entry, cat.opener)["version"] == 2
    bad = Catalog(dict(SKILL, template="Use p6_nope."))
    with pytest.raises(ValueError, match="unknown tools"):
        shop.fetch_skill(bad.entry("float-watch"), bad.opener)


def test_share_url():
    url, clip = shop.share_url(SKILL)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith(f"{shop.REPO_URL}/issues/new?") and json.loads(clip) == shop.meta(SKILL)
    assert q == {"template": [shop.ISSUE_TEMPLATE], "title": ["Share: float-watch"]}
    long = dict(SKILL, template="Use p6_lookahead. " + "x" * 7500)
    url, clip = shop.share_url(long)
    # never the skill in the link: GitHub's sign-in with Google fails on a long return URL
    assert len(url) < 150 and json.loads(clip)["template"] == long["template"]
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
    assert stored["source"] == {"shop": "float-watch", "version": 1, "sha256": entry["sha256"],
                                "content_sha": skills.content_sha(stored)}
    assert shop.status(data, entry) == shop.INSTALLED and config.validate(data)["custom_skills"] == [stored]
    data = settings_logic.set_skill_enabled(data, "float-watch", False)

    newer_cat = Catalog(dict(SKILL, version=2, description="Newer. Use when asked."))
    e2 = newer_cat.entry("float-watch")
    assert shop.status(data, e2) == shop.UPDATE
    data = shop.install(data, shop.fetch_skill(e2, newer_cat.opener))
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


def test_builtin_entries_of_a_1_8_2_catalog():
    """1.8.2 catalogs list the built-in skills of that version: 1.9 has none, so such an entry needs a newer P6
    MCP, is never fetched or installed, and the core install takes the bundled copy instead."""
    cat = Catalog(SKILL, builtins=("update-import-safely",))
    entry = cat.entry("update-import-safely")
    assert entry["builtin"] is True and "template" not in entry
    data = config.validate({})
    assert shop.status(data, entry) == shop.NEWER_APP
    with pytest.raises(ValueError, match="never installed from the shop"):
        shop.fetch_skill(entry, cat.opener)
    with pytest.raises(ValueError, match="not installed from the shop"):
        shop.install(data, builtin_entry("update-import-safely"))
    live = Catalog(builtins=CORE[:1], core=CORE[:1])
    _new, rep = core_skills.install(data, live.opener)
    assert rep["bundle"] == CORE and live.calls == [shop.INDEX]


# ----------------------------------------------------------------- the core set (p6mcp.core_skills)

GONE = "retired-check"  # a skill turned off in 1.8 that the core set does not hold (ignored by the install)


def test_core_set_bundle_and_pending_offer():
    assert len(CORE) >= 2 and [s.name for s in core_skills.bundled_skills()] == CORE and GONE not in CORE
    data = config.validate(config.migrate({"schema_version": 2, "skills_disabled": [CORE[0], GONE]})[0])
    assert skills.pending_core(data) and [s.name for s in skills.all_skills(data)] == CORE[1:]
    assert skills.pending_core(None) and not skills.pending_core({"core_skills_installed": True})


def test_first_start_online_installs_the_core_set_from_the_shop():
    cat = Catalog(*[newer(n) for n in CORE], SKILL, core=CORE + ["float-watch"])
    data = config.validate(config.migrate({"schema_version": 2, "skills_disabled": [CORE[0], GONE]})[0])
    new, rep = core_skills.first_start(data, cat.opener)
    assert rep["shop"] == CORE + ["float-watch"] and rep["bundle"] == rep["failed"] == [] and rep["error"] is None
    assert new["core_skills_installed"] is True and new["skills_disabled"] == [] and rep["turned_off"] == [CORE[0]]
    stored = by_name(new)
    assert list(stored) == CORE + ["float-watch"]  # a core skill only the live index has is installed too
    assert stored[CORE[0]]["enabled"] is False and all(stored[n]["enabled"] for n in CORE[1:])  # 1.8 off: off
    for n in stored:
        assert shop.status(new, cat.entry(n)) == shop.INSTALLED and not skills.edited(stored[n])
    assert all(stored[n]["description"].endswith("Newer.") for n in CORE)  # the shop's version, not the copy
    assert config.validate(new) == new
    assert [s.name for s in skills.all_skills(new)] == CORE[1:] + ["float-watch"]  # no bundled copy any more


def test_first_start_offline_uses_the_bundle_with_shop_sources():
    data = config.validate({"skills_disabled": [CORE[0]]})
    new, rep = core_skills.first_start(data, offline)
    assert rep["bundle"] == CORE and rep["shop"] == [] and "offline" in rep["error"] and new["core_skills_installed"]
    stored = by_name(new)
    for row in core_skills.bundle()["skills"]:
        it = stored[row["name"]]
        assert it["source"] == {"shop": row["name"], "version": row["version"], "sha256": row["sha256"],
                                "content_sha": skills.content_sha(it)}
    assert stored[CORE[0]]["enabled"] is False
    # the Shop page compares with the live catalog later: the same bytes are installed, a newer version is an update
    same = Catalog(raw={n: bundle_bytes(n) for n in CORE}, core=CORE)
    assert all(shop.status(new, same.entry(n)) == shop.INSTALLED for n in CORE)
    later = Catalog(*[newer(n) for n in CORE], core=CORE)
    assert all(shop.status(new, later.entry(n)) == shop.UPDATE for n in CORE)
    upd, urep = core_skills.updates(new, later.opener)  # and the automatic update brings them to it
    assert urep["updated"] == CORE and all(shop.status(upd, later.entry(n)) == shop.INSTALLED for n in CORE)
    assert by_name(upd)[CORE[0]]["enabled"] is False


def test_first_start_after_a_shop_error_conflicts_and_newer_skills():
    cat = Catalog(*[newer(n) for n in CORE], core=CORE)
    del cat.files[f"skills/{CORE[0]}/{shop.META}"]  # listed, not readable: the rest comes from the copy at once
    _new, rep = core_skills.install(config.validate({}), cat.opener)
    assert rep["bundle"] == CORE and rep["shop"] == [] and CORE[0] in rep["error"]
    assert cat.calls == [shop.INDEX, f"skills/{CORE[0]}/{shop.META}"]

    mine = settings_logic.put_skill(config.validate({}), dict(SKILL, name=CORE[0]))  # the user's own, a core name
    cat = Catalog(*[dict(newer(n), min_app_version="99.0.0") if n == CORE[-1] else newer(n) for n in CORE], core=CORE)
    new, rep = core_skills.first_start(mine, cat.opener)
    assert rep["conflicts"] == [CORE[0]] and rep["shop"] == CORE[1:-1] and rep["bundle"] == [CORE[-1]]
    assert new["core_skills_installed"] and by_name(new)[CORE[0]] == mine["custom_skills"][0]  # left alone

    def never(req, timeout):
        raise AssertionError("the skill shop is turned off: nothing is read")
    new, rep = core_skills.first_start(config.validate({"shop_enabled": False}), never)
    assert rep["bundle"] == CORE and "turned off" in rep["error"] and new["core_skills_installed"]


def test_first_start_retries_when_a_core_skill_is_missing_and_keeps_1_8_shop_skills():
    cat = Catalog(SKILL, core=["float-watch"])
    del cat.files[f"skills/float-watch/{shop.META}"]  # a core skill of the shop that cannot be read, no copy
    new, rep = core_skills.first_start(config.validate({"skills_disabled": ["delay-whatif"]}), cat.opener)
    assert rep["bundle"] == CORE and [f["name"] for f in rep["failed"]] == ["float-watch"]
    assert new["core_skills_installed"] is False and new["skills_disabled"] == ["delay-whatif"]  # next start retries

    cat = Catalog(*[newer(n) for n in CORE], core=CORE)
    old = shop.install(config.validate({}), shop.fetch_skill(cat.entry(CORE[0]), cat.opener))
    del old["custom_skills"][0]["source"]["content_sha"]  # installed by 1.8 (an edit dropped the source then)
    assert skills.edited(old["custom_skills"][0])
    new, rep = core_skills.first_start(old, cat.opener)
    assert rep["kept"] == [CORE[0]] and rep["shop"] == CORE[1:] and not skills.edited(by_name(new)[CORE[0]])


def test_restore_reinstalls_missing_and_edited_core_skills():
    cat = Catalog(*[newer(n) for n in CORE], core=CORE)
    data, _rep = core_skills.first_start(config.validate({}), cat.opener)
    data = settings_logic.remove_skill(data, CORE[0])
    last = CORE[-1]
    data = settings_logic.put_skill(data, dict(by_name(data)[last], template="Edited p6_lookahead."), last)
    data = settings_logic.set_skill_enabled(data, last, False)
    data = settings_logic.put_skill(data, dict(SKILL, name="mine"))
    new, rep = core_skills.install(data, cat.opener)
    assert sorted(rep["shop"]) == sorted([CORE[0], last]) and rep["kept"] == CORE[1:-1]
    restored = by_name(new)
    assert not skills.edited(restored[last]) and restored[last]["enabled"] is False  # its on/off state kept
    assert "mine" in restored and set(CORE) <= set(restored)
    again, rep = core_skills.install(new, offline)  # offline: never an older copy over a current shop install
    assert rep["kept"] == CORE and rep["bundle"] == [] and again == new


def test_updates_leave_an_edited_skill_alone():
    v1 = Catalog(SKILL, dict(SKILL, name="other", title="Other"))
    data = config.validate({"core_skills_installed": True})
    for n in ("float-watch", "other"):
        data = shop.install(data, shop.fetch_skill(v1.entry(n), v1.opener))
    data = settings_logic.put_skill(data, dict(by_name(data)["other"], template="My own p6_lookahead text."), "other")
    v2 = Catalog(dict(SKILL, version=2, description="Newer. Use when asked."),
                 dict(SKILL, name="other", title="Other", version=2, description="Newer. Use when asked."))
    assert shop.status(data, v2.entry("other")) == shop.UPDATE  # shown as an update on the Shop page
    new, rep = core_skills.updates(data, v2.opener)
    assert rep["updated"] == ["float-watch"] and rep["edited"] == ["other"] and rep["failed"] == []
    assert by_name(new)["other"]["template"] == "My own p6_lookahead text."
    assert shop.status(new, v2.entry("float-watch")) == shop.INSTALLED
    assert shop.status(new, v2.entry("other")) == shop.UPDATE
    forced = shop.install(new, shop.fetch_skill(v2.entry("other"), v2.opener))  # Update on the Shop page (asked)
    assert not skills.edited(by_name(forced)["other"]) and shop.status(forced, v2.entry("other")) == shop.INSTALLED
    v3 = Catalog(dict(SKILL, version=3, min_app_version="99.0.0"))
    assert core_skills.updates(forced, v3.opener)[1]["updated"] == []  # needs a newer P6 MCP: not installed
    assert "offline" in core_skills.updates(forced, offline)[1]["error"]


@pytest.fixture
def profile(tmp_path, monkeypatch):
    for var, sub in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local"), ("USERPROFILE", "User")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))  # the clients' skills folders of a fake profile
    monkeypatch.delenv("CLINE_MCP_SETTINGS_PATH", raising=False)
    monkeypatch.setenv("P6MCP_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr("p6mcp.clients._running_names", lambda: set())
    return tmp_path


def test_first_start_saves_and_rewrites_the_clients(profile):
    from p6mcp import clients
    (profile / "config.json").write_text(json.dumps({"schema_version": 2, "tool_profile": "full",
                                                     "skills_disabled": [CORE[0]]}), encoding="utf-8")
    ad = clients.get("claude_code")
    ad.install_skills("full", False)  # what the clients got before the first start: the bundled copy
    folder = profile / "User" / ".claude" / "skills"
    before = sorted(os.listdir(folder))
    assert before == sorted(skills.PREFIX + s.name for s in skills.offered("full", False, config.read_file()[0]))
    cat = Catalog(*[newer(n) for n in CORE], core=CORE)
    rep = core_skills.run_first_start(cat.opener)
    saved = config.read_file()[0]
    assert saved["core_skills_installed"] and saved["schema_version"] == config.CONFIG_VERSION
    assert rep["shop"] == CORE and [c["client"] for c in rep["clients"]] == ["claude_code"]  # one shared folder
    assert sorted(os.listdir(folder)) == before  # same p6-<name> folders, now the shop's version
    for s in skills.offered("full", False, saved):
        assert (folder / skills.folder_name(s) / "SKILL.md").read_text(encoding="utf-8") == skills.skill_md(s)
        assert s.description.endswith("Newer.")
    assert ad.skills_state() == clients.SKILLS_INSTALLED and not (folder / (skills.PREFIX + CORE[0])).exists()
    assert core_skills.run_first_start(cat.opener) is None  # once

    (profile / "config.json").write_text("{broken", encoding="utf-8")
    assert core_skills.run_first_start(cat.opener) is None  # unreadable: the window reports it


def test_first_start_of_a_fresh_install(profile):
    rep = core_skills.run_first_start(offline)
    saved = config.read_file()[0]  # a fresh install gets its config.json here
    assert rep["bundle"] == CORE and rep["clients"] == [] and saved["core_skills_installed"]
    assert [c["name"] for c in saved["custom_skills"]] == CORE


def test_run_updates_saves_and_respects_the_switches(profile):
    v1 = Catalog(SKILL)
    data = shop.install(config.validate({"core_skills_installed": True}),
                        shop.fetch_skill(v1.entry("float-watch"), v1.opener))
    v2 = Catalog(dict(SKILL, version=2, description="Newer. Use when asked."))
    for off in ({"auto_update": False}, {"shop_enabled": False}):
        config.save(dict(data, **off))
        assert core_skills.run_updates(v2.opener)["skipped"] == "off"
    config.save(dict(data, core_skills_installed=False))
    assert "not installed" in core_skills.run_updates(v2.opener)["skipped"]
    config.save(data)
    rep = core_skills.run_updates(v2.opener)
    assert rep["updated"] == ["float-watch"] and rep["clients"] == []
    assert config.read_file()[0]["custom_skills"][0]["source"]["version"] == 2
    stamp = os.stat(os.environ["P6MCP_CONFIG"]).st_mtime_ns
    assert core_skills.run_updates(v2.opener)["updated"] == []
    assert os.stat(os.environ["P6MCP_CONFIG"]).st_mtime_ns == stamp  # nothing new: the file is not rewritten


# ----------------------------------------------------------------- p6_shop_* tools

@pytest.fixture
def shop_server(server, p6, tmp_path, monkeypatch):
    for var, sub in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local"), ("USERPROFILE", "User")):
        (tmp_path / sub).mkdir(exist_ok=True)
        monkeypatch.setenv(var, str(tmp_path / sub))  # the clients' skills folders of a fake profile
    cat = Catalog(SKILL, dict(SKILL, name="bulk-update", title="Bulk update", template="Use p6_update_activity.",
                              description="Updates activities. Use when asked to update many."),
                  builtins=("delay-whatif",))
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
    row = p6.call("p6_shop_search", query="delaying")["rows"]  # a built-in entry of a 1.8.2 catalog
    assert [(r["name"], r["status"], r["author"]) for r in row] == [("delay-whatif", "newer_app", "nizarhamza")]
    with pytest.raises(ValueError, match="built-in skill of P6 MCP 1.8 in an older catalog"):
        p6.call("p6_shop_get", name="delay-whatif")
    with pytest.raises(ValueError, match="built-in skill of P6 MCP 1.8 in an older catalog"):
        p6.call("p6_shop_install", name="delay-whatif")

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
    assert saved[0]["source"]["content_sha"] == skills.content_sha(saved[0])  # an edit made later is seen
    assert (skills_dir / "p6-float-watch" / "reference" / "limits.md").exists()
    assert {r["name"]: r["status"] for r in p6.call("p6_shop_search")["rows"]}["float-watch"] == "installed"

    p6.set(shop_enabled=False)
    with pytest.raises(ValueError, match="skill shop is turned off"):
        p6.call("p6_shop_search")
    assert server.p6_diagnose()["shop"]["enabled"] is False
    p6.set(shop_enabled=True, allow_writes=False)  # settings only: no database write permission needed
    assert p6.call("p6_shop_install", name="bulk-update")["dry_run"]
    hints = server.TOOL_HINTS
    assert hints["p6_shop_search"]["openWorldHint"] and hints["p6_shop_install"]["openWorldHint"]
    assert not hints["p6_list_projects"]["openWorldHint"] and os.path.exists(os.environ["P6MCP_CONFIG"])
