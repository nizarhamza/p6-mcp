"""Settings gate: no database, unconfirmed database, profile, write permission, schema."""

import pytest

from p6mcp import config, policy

READ_DB_TOOLS = {"p6_list_projects": {}, "p6_list_eps": {}, "p6_query_activities": {"project": "DEMO-A"},
                 "p6_project_summary": {"project": "DEMO-A"}, "p6_sql_select": {"sql": "select 1"},
                 "p6_list_backups": {}, "p6_edit_log": {}}


def test_no_database_configured(p6):
    p6.set(databases=[], active_database=None)
    for tool, kw in READ_DB_TOOLS.items():
        with pytest.raises(RuntimeError, match="No database configured. Open MCP for Primavera P6 Settings to "
                                               "select one."):
            p6.call(tool, **kw)
    with pytest.raises(RuntimeError, match="No database configured"):
        p6.call("p6_update_activity", project="DEMO-A", task_code="A1020", physical_pct=10)
    d = p6.call("p6_diagnose")
    assert d["database_problem"] == config.NO_DB_MESSAGE and d["settings"]["db_path"] is None


def test_unconfirmed_database_is_refused(p6):
    p6.set(databases=[dict(p6.cfg["databases"][0], confirmed=False)])
    with pytest.raises(RuntimeError, match="not tested and confirmed"):
        p6.call("p6_list_projects")


def test_basic_profile_refuses_other_tools(p6):
    p6.set(tool_profile="basic")
    assert p6.call("p6_list_projects")["total"] == 1
    with pytest.raises(ValueError, match="Basic tool profile"):
        p6.call("p6_sql_select", sql="select 1")
    with pytest.raises(ValueError, match="Basic tool profile"):
        p6.call("p6_update_activity", project="DEMO-A", task_code="A1020", physical_pct=10)


def test_writes_off_refuses_even_the_dry_run(p6):
    p6.set(allow_writes=False)
    before = p6.hash()
    with pytest.raises(ValueError, match="Write operations are disabled"):
        p6.call("p6_update_activity", project="DEMO-A", task_code="A1020", physical_pct=10)
    with pytest.raises(ValueError, match="Write operations are disabled"):
        p6.call("p6_backup_database")
    assert p6.call("p6_list_projects")["total"] == 1  # reads still work
    assert p6.hash() == before


def test_writes_off_also_blocks_direct_calls(p6):
    """The gate sits in the write path itself, not only in the client-facing wrapper."""
    p6.set(allow_writes=False)
    with pytest.raises(RuntimeError, match="Write operations are disabled"):
        p6.s.p6_update_activity("DEMO-A", "A1020", physical_pct=10, confirm=True)
    with pytest.raises(RuntimeError, match="Write operations are disabled"):
        p6.s.p6_restore_backup("anything.db", confirm=True)


def test_turning_writes_off_takes_effect_without_restart(p6):
    dry = p6.call("p6_update_activity", project="DEMO-A", task_code="A1020", physical_pct=10)
    p6.set(allow_writes=False)
    with pytest.raises(ValueError, match="disabled"):
        p6.call("p6_update_activity", project="DEMO-A", task_code="A1020", physical_pct=10, dry_run=False,
                confirmation_token=dry["confirmation_token"])


def test_unknown_schema_is_read_only(p6):
    p6.set(trusted_schemas=[])
    assert p6.call("p6_query_activities", project="DEMO-A")["total"] == 7
    with pytest.raises(RuntimeError, match="Unknown P6 schema"):
        p6.call("p6_update_activity", project="DEMO-A", task_code="A1020", physical_pct=10)
    d = p6.call("p6_diagnose")
    assert d["schema"]["writable"] is False and d["schema"]["p6_version"]["version"] == "25.12"
    assert "Unknown P6 schema" in d["writes"]


def test_known_schema_needs_no_trust(p6, monkeypatch):
    fp = p6.cfg["trusted_schemas"][0]
    monkeypatch.setitem(p6.s._schema.KNOWN_SCHEMAS, fp, "synthetic")
    p6.set(trusted_schemas=[])
    assert p6.call("p6_update_activity", project="DEMO-A", task_code="A1020", physical_pct=10)["dry_run"]


def test_diagnose_reports_restart_needed(p6):
    assert p6.s.LISTED == set(policy.TOOL_KIND)  # the session server started with everything listed
    p6.set(tool_profile="basic")
    d = p6.call("p6_diagnose")
    assert d["restart_required"] is True and "restart" in d["restart_note"]


def test_switching_database_clears_caches(p6, tmp_path, golden):
    import shutil
    other = str(tmp_path / "other.db")
    shutil.copyfile(golden, other)
    p6.call("p6_list_projects")
    assert p6.s._cols_cache
    p6.set(databases=[{"name": "other", "kind": "sqlite", "path": other, "confirmed": True}], active_database="other")
    assert p6.s.DB_PATH.endswith("other.db") and not p6.s._cols_cache
