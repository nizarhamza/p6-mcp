"""p6_use_database (plan item P2): the data source of one server process, switched over config.json without saving it.

Synthetic data only: two copies of the synthetic database, the XER set exported from it, and P6 EPPM and SQL Server /
Oracle profiles whose local copies are written by the test (the store layouts of p6mcp.eppm.store and p6mcp.pmdb).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3

import pytest

import fake_pmdb
from conftest import sha

from p6mcp import confirm, pmdb, policy
from p6mcp.eppm import store as eppm_store

# p6mcp.core is imported through the server fixture only: core.state reads the settings when it is imported, and a
# module level import here would run at collection, before the session's config file is set.


@pytest.fixture
def env(p6, tmp_path, monkeypatch):
    """The session's demo database plus a second confirmed one ('other', DEMO-A renamed OTHER-A)."""
    from p6mcp.core import state
    p6.state = state
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    other = str(tmp_path / "other.db")
    shutil.copyfile(p6.db, other)
    con = sqlite3.connect(other)
    con.execute("UPDATE PROJECT SET proj_short_name = 'OTHER-A' WHERE proj_short_name = 'DEMO-A'")
    con.commit()
    con.close()
    p6.set(databases=p6.cfg["databases"] + [{"name": "other", "kind": "sqlite", "path": other, "confirmed": True}])
    p6.other = other
    yield p6
    state.OVERRIDE = None
    p6.s._refresh()


def use(p6, **kw):
    """Dry run, then the switch with its token."""
    return p6.apply("p6_use_database", **kw)


def projects(p6) -> set:
    return {r["proj_short_name"] for r in p6.call("p6_list_projects")["rows"]}


def make_copy(p6, path: str, manifest_dir: str, manifest_file: str, manifest: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    shutil.copyfile(p6.db, path)
    with open(os.path.join(manifest_dir, manifest_file), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)


# ----------------------------------------------------------------- listing

def test_listing_shows_every_profile_and_changes_nothing(env):
    before = sha(os.environ["P6MCP_CONFIG"])
    for kw in ({}, {"dry_run": False}):  # without source it lists, whatever dry_run says; never a token
        res = env.call("p6_use_database", **kw)
        assert "confirmation_token" not in res and "dry_run" not in res
        rows = {r["profile"]: r for r in res["profiles"]}
        assert set(rows) == {"demo", "other"}
        assert rows["demo"]["active"] and rows["demo"]["saved"] and not rows["other"]["active"]
        assert rows["other"]["switchable"] and rows["other"]["points_to"] == os.path.abspath(env.other)
        assert rows["other"]["confirmed"] and not rows["other"]["read_only"]
        assert res["active"]["source"] == "database" and res["active"]["profile"] == "demo"
        assert res["saved"] == {"source": "database", "profile": "demo"} and res["switched"] is False
    assert sha(os.environ["P6MCP_CONFIG"]) == before


# ----------------------------------------------------------------- switch and back

def test_switch_reads_the_other_database_for_this_process_only(env):
    cfg_before, listed = sha(os.environ["P6MCP_CONFIG"]), set(env.s.LISTED)
    dry = env.call("p6_use_database", source="database", profile="other")
    assert dry["dry_run"] and dry["confirmation_token"] and "P6 must be closed" not in dry["next"]
    assert any(env.other in line for line in dry["would_do"])
    assert "DEMO-A" in projects(env)  # a dry run switches nothing
    with pytest.raises(confirm.TokenError):
        env.call("p6_use_database", source="database", profile="other", dry_run=False, confirmation_token="p6c_bad")
    res = env.call("p6_use_database", source="database", profile="other", dry_run=False,
                   confirmation_token=dry["confirmation_token"])
    assert res["applied"] and res["switched"] and res["since"]
    assert res["active"] == {"source": "database", "profile": "other", "db_path": os.path.abspath(env.other)}
    assert res["saved"] == {"source": "database", "profile": "demo"} and res["writes"] == "allowed"
    assert "OTHER-A" in projects(env) and "DEMO-A" not in projects(env)
    with pytest.raises(confirm.TokenError):  # single use
        env.call("p6_use_database", source="database", profile="other", dry_run=False,
                 confirmation_token=dry["confirmation_token"])
    assert sha(os.environ["P6MCP_CONFIG"]) == cfg_before  # never saved
    assert set(env.s.LISTED) == listed  # the tool list stays the startup one

    env.set(p6_user="PLANNER")  # config.json edited meanwhile: every _refresh keeps the switch
    assert "OTHER-A" in projects(env)
    diag = env.call("p6_diagnose")
    ds = diag["data_source"]
    assert ds["switched"] and ds["active"]["profile"] == "other" and ds["saved"]["profile"] == "demo"
    assert ds["since"] == res["since"] and "note" in ds and diag["restart_required"] is False
    assert diag["db_path"] == os.path.abspath(env.other)
    assert env.call("p6_use_database", source="database", profile="other")["already_active"]

    back = use(env, source="saved")
    assert back["applied"] and not back["switched"] and back["active"]["profile"] == "demo"
    assert env.state.OVERRIDE is None and "DEMO-A" in projects(env)
    assert env.call("p6_use_database", source="saved")["already_active"]


def test_switching_to_the_saved_profile_clears_the_switch(env):
    use(env, source="database", profile="other")
    res = use(env, source="database", profile="DEMO")  # case ignored; it is the saved choice
    assert res["applied"] and not res["switched"] and env.state.OVERRIDE is None


def test_engine_cache_is_cleared(env):
    env.call("p6_cpm_validate", project="DEMO-A")
    assert len(env.state._engine_cache) > 0
    clears = env.state._engine_stats["clears"]
    use(env, source="database", profile="other")
    assert len(env.state._engine_cache) == 0 and env.state._engine_stats["clears"] == clears + 1


def test_edit_log_and_backups_follow_the_new_database(env):
    demo_hash = env.hash()
    use(env, source="database", profile="other")
    res = env.apply("p6_update_activity", project="OTHER-A", task_code="A1010", task_name="Renamed here")
    assert res.get("applied", True) is not False, res
    assert env.hash() == demo_hash  # the saved database is untouched
    con = sqlite3.connect(env.other)
    assert con.execute("SELECT task_name FROM TASK WHERE task_code = 'A1010'").fetchone()[0] == "Renamed here"
    con.close()
    backups = os.listdir(env.backups)
    assert backups and all(b.startswith("other_") for b in backups)
    entries = [json.loads(line) for f in os.listdir(env.logs)
               for line in open(os.path.join(env.logs, f), encoding="utf-8")]
    assert entries and {e["db"] for e in entries} == {"other.db"}
    assert env.call("p6_edit_log")["total"] >= 1
    use(env, source="saved")
    assert env.call("p6_edit_log")["total"] == 0  # the demo database's own log is empty


# ----------------------------------------------------------------- refusals

def test_unconfirmed_and_missing_profiles_are_refused(env, tmp_path):
    gone = str(tmp_path / "gone.db")
    env.set(databases=env.cfg["databases"] + [
        {"name": "raw", "kind": "sqlite", "path": env.other, "confirmed": False},
        {"name": "gone", "kind": "sqlite", "path": gone, "confirmed": True}])
    rows = {r["profile"]: r for r in env.call("p6_use_database")["profiles"]}
    assert not rows["raw"]["switchable"] and "not tested and confirmed" in rows["raw"]["why_not"]
    assert not rows["gone"]["switchable"] and "not found" in rows["gone"]["why_not"]
    with pytest.raises(ValueError, match="not tested and confirmed"):
        env.call("p6_use_database", source="database", profile="raw")
    with pytest.raises(ValueError, match="not found"):
        env.call("p6_use_database", source="database", profile="gone")
    with pytest.raises(ValueError, match="one of"):
        env.call("p6_use_database", source="database")  # several set up: the name is needed
    with pytest.raises(ValueError, match="No P6 Professional SQLite database named"):
        env.call("p6_use_database", source="database", profile="nope")
    with pytest.raises(ValueError, match="No XER files"):
        env.call("p6_use_database", source="xer")
    assert env.state.OVERRIDE is None


def test_file_removed_after_the_dry_run_switches_nothing(env):
    dry = env.call("p6_use_database", source="database", profile="other")
    os.remove(env.other)
    with pytest.raises(ValueError, match="Nothing was switched"):
        env.call("p6_use_database", source="database", profile="other", dry_run=False,
                 confirmation_token=dry["confirmation_token"])
    assert env.state.OVERRIDE is None and "DEMO-A" in projects(env)


def test_settings_changed_after_the_dry_run(env, tmp_path):
    dry = env.call("p6_use_database", source="database", profile="other")
    moved = str(tmp_path / "moved.db")
    shutil.copyfile(env.other, moved)
    env.set(databases=[env.cfg["databases"][0], {"name": "other", "kind": "sqlite", "path": moved, "confirmed": True}])
    res = env.call("p6_use_database", source="database", profile="other", dry_run=False,
                   confirmation_token=dry["confirmation_token"])
    assert res["applied"] is False and "settings changed" in res["error"] and env.state.OVERRIDE is None


def test_environment_wins(env, monkeypatch):
    monkeypatch.setenv("P6_DB_PATH", env.db)
    env.s._refresh()
    res = env.call("p6_use_database")
    assert res["environment"] and res["active"]["source"] == "environment"
    assert not any(r["switchable"] for r in res["profiles"])
    with pytest.raises(ValueError, match="environment wins"):
        env.call("p6_use_database", source="database", profile="other")


def test_profile_removed_while_switched(env):
    use(env, source="database", profile="other")
    env.set(databases=env.cfg["databases"][:1])
    with pytest.raises(RuntimeError, match="p6_use_database"):
        env.call("p6_list_projects")
    assert use(env, source="saved")["applied"] and "DEMO-A" in projects(env)


def test_basic_profile_and_remote_clients(env):
    assert policy.kind("p6_use_database") == policy.CONFIG
    assert "p6_use_database" in policy.NO_DB_TOOLS
    assert policy.remote_kind("p6_use_database") == "never"
    assert not policy.remote_listed("p6_use_database", "full", True, True)
    assert policy.remote_refusal("p6_use_database", "full", True, True, {"p6:read", "p6:write"}) == \
        policy.REMOTE_OFF.format(tool="p6_use_database")
    env.set(tool_profile="basic")
    with pytest.raises(ValueError, match="Basic tool profile"):
        env.call("p6_use_database")


def test_http_transport_note(env, monkeypatch):
    monkeypatch.setattr(env.state, "TRANSPORT", "http")
    res = use(env, source="database", profile="other")
    assert any("every client of the HTTP server" in n for n in res["notes"])
    assert env.call("p6_diagnose")["data_source"]["transport"] == "http"


# ----------------------------------------------------------------- other data sources

def test_xer_set_is_read_only(env, tmp_path):
    xer = str(tmp_path / "demo.xer")
    env.call("p6_export_xer", project="DEMO-A", path=xer)
    env.set(xer_files=[xer], xer_confirmed=False)
    with pytest.raises(ValueError, match="not loaded and checked"):
        env.call("p6_use_database", source="xer")
    env.set(xer_confirmed=True)
    res = use(env, source="xer")
    assert res["active"]["source"] == "xer" and res["writes"] == env.s.XER_READ_ONLY
    assert projects(env) == {"DEMO-A"}
    assert env.call("p6_diagnose")["xer_mode"]["files"][0]["file"] == "demo.xer"
    with pytest.raises(ValueError, match="XER-only mode is read only"):
        env.call("p6_update_activity", project="DEMO-A", task_code="A1010", task_name="x")
    use(env, source="saved")
    assert env.call("p6_update_activity", project="DEMO-A", task_code="A1010", task_name="x")["dry_run"]


def test_eppm_profile_needs_a_working_copy(env):
    prof = {"name": "eppm1", "base_url": "https://reader:secretpw@eppm.test/p6ws", "database": "PMDB",
            "user": "reader", "confirmed": True}
    env.set(eppm_profiles=[prof])
    rows = {r["profile"]: r for r in env.call("p6_use_database")["profiles"]}
    assert "secretpw" not in json.dumps(rows) and rows["eppm1"]["points_to"].startswith("https://eppm.test/p6ws")
    assert rows["eppm1"]["copy"] is None and not rows["eppm1"]["switchable"]
    with pytest.raises(ValueError, match="no working copy"):
        env.call("p6_use_database", source="eppm", profile="eppm1")
    make_copy(env, eppm_store.store_path("eppm1"), eppm_store.store_dir("eppm1"), eppm_store.MANIFEST_FILE,
              {"store_version": eppm_store.STORE_VERSION, "server": "https://eppm.test/p6ws", "profile": "eppm1",
               "route": "sql", "pulled_at": "2026-11-28T09:00:00", "pulled_ts": "2026-11-28 09:00:00.000",
               "projects": [{"project_id": "DEMO-A"}], "warnings": []})
    res = use(env, source="eppm")  # the only P6 EPPM profile: no name needed
    assert res["active"]["source"] == "eppm" and res["active"]["profile"] == "eppm1"
    assert res["active"]["db_path"] == eppm_store.store_path("eppm1") and res["writes"] == "allowed"
    assert env.call("p6_diagnose")["eppm_mode"]["server"] == "eppm1"
    assert "DEMO-A" in projects(env)


def test_pmdb_copy_is_read_only(env):
    env.set(pmdb_profiles=[fake_pmdb.profile("mssql", confirmed=False)])
    with pytest.raises(ValueError, match="not tested and confirmed"):
        env.call("p6_use_database", source="pmdb", profile="plant")
    env.set(pmdb_profiles=[fake_pmdb.profile("mssql", confirmed=True)])
    with pytest.raises(ValueError, match="no local copy"):
        env.call("p6_use_database", source="pmdb", profile="plant")
    make_copy(env, pmdb.store_path("plant"), pmdb.store_dir("plant"), pmdb.MANIFEST_FILE,
              {"store_version": pmdb.STORE_VERSION, "profile": "plant", "copied_at": "2026-11-28T09:00:00",
               "projects": [{"project_id": "DEMO-A"}], "baselines": [], "warnings": [], "rows": {}})
    row = next(r for r in env.call("p6_use_database")["profiles"] if r["source"] == "pmdb")
    assert row["switchable"] and row["read_only"] and row["copy"]["copied_at"] == "2026-11-28T09:00:00"
    res = use(env, source="pmdb", profile="plant")
    assert res["active"]["source"] == "pmdb" and res["writes"] == pmdb.READ_ONLY
    with pytest.raises(ValueError, match="read only"):
        env.call("p6_update_activity", project="DEMO-A", task_code="A1010", task_name="x")
    assert env.call("p6_diagnose")["pmdb_mode"]["database"] == "plant"
