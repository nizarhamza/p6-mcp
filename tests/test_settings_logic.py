import os
import socket

import pytest

from p6mcp import config, policy
from p6mcp import settings_logic as sl


def base():
    return {"databases": [], "active_database": None}


def test_add_uses_unique_names():
    d, n1 = sl.add_database(base(), r"C:\data\PPMDBSQLite.db")
    d, n2 = sl.add_database(d, r"D:\other\PPMDBSQLite.db")
    assert (n1, n2) == ("PPMDBSQLite", "PPMDBSQLite 2")
    assert all(e["confirmed"] is False for e in d["databases"])


def test_new_path_clears_confirmation_and_activation():
    d, n = sl.add_database(base(), r"C:\a.db")
    d = config.confirm_database(d, n, {"fingerprint": "f"})
    d = sl.update_database(d, n, path=r"C:\a.db")  # same file: nothing changes
    assert d["active_database"] == n and sl.find(d, n)["confirmed"]
    d = sl.update_database(d, n, path=r"C:\b.db")
    e = sl.find(d, n)
    assert e["confirmed"] is False and "schema_fingerprint" not in e and d["active_database"] is None


def test_rename():
    d, n = sl.add_database(base(), r"C:\a.db")
    d, m = sl.add_database(d, r"C:\b.db")
    d = config.confirm_database(d, n, {})
    d = sl.update_database(d, n, new_name="Main")
    assert d["active_database"] == "Main"
    with pytest.raises(config.ConfigError, match="already used"):
        sl.update_database(d, "Main", new_name=m)
    with pytest.raises(config.ConfigError, match="needs a name"):
        sl.update_database(d, "Main", new_name="  ")


def test_remove_and_set_active():
    d, n = sl.add_database(base(), r"C:\a.db")
    with pytest.raises(config.ConfigError, match="Test the connection"):
        sl.set_active(d, n)
    d = config.confirm_database(d, n, {})
    d = sl.remove_database(d, n)
    assert d["databases"] == [] and d["active_database"] is None


def test_confirm_needs_a_successful_test_of_the_same_file(tmp_path):
    d, n = sl.add_database(base(), str(tmp_path / "a.db"))
    with pytest.raises(config.ConfigError, match="successful"):
        sl.confirm(d, n, {"ok": False, "path": str(tmp_path / "a.db")})
    with pytest.raises(config.ConfigError, match="another file"):
        sl.confirm(d, n, {"ok": True, "path": str(tmp_path / "b.db")})
    d = sl.confirm(d, n, {"ok": True, "path": str(tmp_path / "a.db"), "projects": 3,
                          "schema": {"fingerprint": "abc", "label": "P6"}})
    e = sl.find(d, n)
    assert d["active_database"] == n and e["confirmed"] and e["schema_fingerprint"] == "abc" and e["projects"] == 3


def test_trust_schema_toggles():
    d = sl.trust_schema(base(), "fp")
    assert d["trusted_schemas"] == ["fp"]
    assert sl.trust_schema(d, "fp")["trusted_schemas"] == ["fp"]
    assert sl.trust_schema(d, "fp", False)["trusted_schemas"] == []


def test_port_checks():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        busy = s.getsockname()[1]
        assert sl.port_free(busy) is False
    free = sl.free_port(40123)
    assert free is not None and sl.port_free(free)


def test_restart_needed():
    assert sl.restart_needed({}, {"tool_profile": "basic"}) == []
    assert sl.restart_needed({"tool_profile": "basic"}, {"tool_profile": "full", "allow_writes": True}) == \
        ["tool_profile", "allow_writes"]
    assert sl.restart_needed({}, {"backup_retention": 3, "active_database": "x"}) == []


def test_tool_rows_match_policy():
    rows = sl.tool_rows("basic", True)
    assert len(rows) == len(policy.TOOL_KIND)
    assert {r["tool"] for r in rows if r["available"]} == policy.BASIC_TOOLS
    assert [r["kind"] for r in rows] == sorted((r["kind"] for r in rows), key=policy.KINDS.index)


def test_load_for_edit_and_broken_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("P6MCP_CONFIG", str(path))
    data, err = sl.load_for_edit()
    assert err is None and data["tool_profile"] == "basic"
    path.write_text("{oops", encoding="utf-8")
    data, err = sl.load_for_edit()
    assert "unreadable" in err and data == config.DEFAULTS
    moved = sl.set_aside_broken_file()
    assert moved.endswith(".broken") and not path.exists() and os.path.exists(moved)
    path.write_text("{again", encoding="utf-8")
    assert sl.set_aside_broken_file().endswith(".broken-2")


def test_skill_source_and_filter():
    rows = [{"name": "float-watch", "title": "Float watch", "description": "Low float.", "source": None,
             "edited": False},
            {"name": "lookahead-brief", "title": "Look-ahead brief", "description": "Short look-ahead.",
             "source": {"shop": "lookahead-brief"}, "edited": False},
            {"name": "milestones", "title": "Milestone tracker", "description": "Against the baseline.",
             "source": {"shop": "milestones"}, "edited": True}]
    assert [sl.skill_source(r) for r in rows] == ["custom", "shop", "shop_edited"]
    assert sl.SKILL_SOURCES == ("shop", "shop_edited", "custom")

    def names(query="", source="all"):
        return [r["name"] for r in sl.filter_skills(rows, query, source)]
    assert names() == ["float-watch", "lookahead-brief", "milestones"]
    assert names("LOOK-AHEAD") == ["lookahead-brief"]  # case ignored
    assert names("p6-mile") == ["milestones"]  # the ID as the pages show it
    assert names("  float   low ") == ["float-watch"]  # every word, in the title or the description
    assert names("float baseline") == []
    assert names(source="shop") == ["lookahead-brief"] and names(source="shop_edited") == ["milestones"]
    assert names(source="custom") == ["float-watch"] and names("brief", "custom") == []


def test_ui_state_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = sl.ui_state_path()
    assert path == str(tmp_path / "P6MCP" / "settings_ui.json") and path != config.config_path()
    assert sl.load_ui_state() == {} and sl.ui_view({}, "skills") == "grid"
    assert sl.save_ui_state(skills_view="list") and sl.save_ui_state(details_size=[640, 480])
    state = sl.load_ui_state()
    assert state == {"skills_view": "list", "details_size": [640, 480]} and not os.path.exists(path + ".tmp")
    assert sl.ui_view(state, "skills") == "list" and sl.ui_view(state, "shop") == "grid"  # one view per page
    assert sl.ui_view({"shop_view": "cards"}, "shop") == "grid"  # an unknown value
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{broken")
    assert sl.load_ui_state() == {}
    assert sl.save_ui_state(shop_view="list") and sl.load_ui_state() == {"shop_view": "list"}  # replaced

    def fail(*_a):
        raise OSError("locked")
    monkeypatch.setattr(sl.os, "replace", fail)
    assert sl.save_ui_state(shop_view="grid") is False and sl.load_ui_state() == {"shop_view": "list"}
