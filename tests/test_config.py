import json
import os

import pytest

from p6mcp import config


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for k in ("P6_DB_PATH", "P6_USER", "P6_BACKUP_DIR", "P6_LOG_DIR", "P6_SKIP_PROCESS_CHECK", "P6MCP_TOOL_PROFILE",
              "P6MCP_ALLOW_WRITES"):
        monkeypatch.delenv(k, raising=False)
    path = tmp_path / "config.json"
    monkeypatch.setenv("P6MCP_CONFIG", str(path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    return path


def write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_no_file_means_no_database_and_safe_defaults(clean_env):
    st = config.load()
    assert st.source == "none" and st.db_path == ""
    assert st.db_problem == config.NO_DB_MESSAGE
    assert st.tool_profile == "basic" and st.allow_writes is False and st.backup_before_write is True
    assert st.block_when_p6_running is True


def test_unconfirmed_database_is_not_used(clean_env, golden):
    write(clean_env, {"schema_version": 1, "databases": [{"name": "a", "path": golden}], "active_database": "a"})
    st = config.load()
    assert st.db_path == "" and "not tested and confirmed" in st.db_problem


def test_confirmed_database_is_used(clean_env, golden):
    data = {"schema_version": 1, "databases": [{"name": "a", "path": golden}]}
    data = config.confirm_database(data, "a", {"fingerprint": "f", "label": "x", "projects": 1})
    config.save(data)
    st = config.load()
    assert st.db_path == os.path.abspath(golden) and st.db_name == "a" and st.source == "config"
    assert st.backup_dir.startswith(str(clean_env.parent / "local")) and st.backup_scope == "every_write"
    assert config.read_file()[0]["databases"][0]["tested_at"]


def test_missing_database_file(clean_env, tmp_path):
    write(clean_env, {"schema_version": 1, "databases": [{"name": "a", "path": str(tmp_path / "gone.db"),
                                                          "confirmed": True}], "active_database": "a"})
    st = config.load()
    assert st.db_path == "" and "not found" in st.db_problem


def test_migration_from_version_0(clean_env, golden):
    write(clean_env, {"db_path": golden, "allow_writes": True})
    data, steps = config.read_file()
    assert steps == ["0->1", "1->2", "2->3"] and data["schema_version"] == 3
    assert data["databases"] == [{"name": "default", "kind": "sqlite", "path": golden, "confirmed": False}]
    st = config.load()
    assert st.migrated == ("0->1", "1->2", "2->3") and st.db_path == ""  # migrated but not confirmed


def test_migration_of_commands_to_skills(clean_env):
    """1.8.0: P6 commands became skills; a 1.7 file keeps its own commands and the ones turned off."""
    cmd = {"name": "float-report", "title": "", "description": "Low float.", "args": [], "template": "Use "
           "p6_float_analysis.", "enabled": False}
    write(clean_env, {"schema_version": 1, "custom_commands": [cmd], "commands_disabled": ["delay-whatif"]})
    data, steps = config.read_file()
    assert steps == ["1->2", "2->3"] and data["custom_skills"] == [dict(cmd, files=[])]
    assert data["skills_disabled"] == ["delay-whatif"] and "custom_commands" not in data
    assert data["shop_enabled"] is True


def test_migration_to_shop_skills(clean_env):
    """1.9.0: no built-in skills. In memory only the flag changes (the core set needs the network: the Settings
    app installs it, p6mcp.core_skills); skills_disabled stays for that install."""
    write(clean_env, {"schema_version": 2, "skills_disabled": ["delay-whatif"], "custom_skills": []})
    data, steps = config.read_file()
    assert steps == ["2->3"] and data["schema_version"] == 3 and data["core_skills_installed"] is False
    assert data["skills_disabled"] == ["delay-whatif"]
    assert config.validate({})["core_skills_installed"] is False  # no file: not installed either
    with pytest.raises(config.ConfigError, match="core_skills_installed"):
        config.validate({"core_skills_installed": "yes"})


def test_newer_schema_version_is_reported_not_raised(clean_env):
    write(clean_env, {"schema_version": 99})
    st = config.load()
    assert st.db_path == "" and "newer P6 MCP" in st.config_error


def test_malformed_file_is_reported(clean_env):
    clean_env.write_text("{not json", encoding="utf-8")
    st = config.load()
    assert st.db_path == "" and "unreadable" in st.config_error and "Settings" in st.db_problem


def test_validation_errors(clean_env):
    with pytest.raises(config.ConfigError, match="tool_profile"):
        config.save({"tool_profile": "admin"})
    with pytest.raises(config.ConfigError, match="http_port"):
        config.save({"http_port": 80})
    with pytest.raises(config.ConfigError, match="not in databases"):
        config.save({"active_database": "x"})
    assert not clean_env.exists()  # nothing written by a refused save


def test_secrets_are_refused(clean_env):
    with pytest.raises(config.ConfigError, match="Credential Manager"):
        config.save({"databases": [{"name": "a", "path": "x.db", "password": "hunter2"}]})


def test_atomic_save_keeps_previous_copy(clean_env):
    config.save({"log_level": "INFO"})
    config.save({"log_level": "DEBUG"})
    assert json.loads(clean_env.read_text(encoding="utf-8"))["log_level"] == "DEBUG"
    assert json.loads((clean_env.parent / "config.json.bak").read_text(encoding="utf-8"))["log_level"] == "INFO"
    assert not [n for n in os.listdir(clean_env.parent) if n.endswith(".tmp")]


def test_unknown_keys_are_kept(clean_env):
    write(clean_env, {"schema_version": 1, "future_option": 3})
    assert config.read_file()[0]["future_option"] == 3


def test_environment_selection_keeps_v2_layout(clean_env, monkeypatch, golden):
    monkeypatch.setenv("P6_DB_PATH", golden)
    st = config.load()
    db_dir = os.path.dirname(os.path.abspath(golden))
    assert st.source == "env" and st.db_path == os.path.abspath(golden)
    assert st.backup_dir == os.path.join(db_dir, "p6_mcp_backups")
    assert st.edit_log_dir == os.path.join(db_dir, "p6_mcp_logs")
    assert st.backup_scope == "session" and st.backup_retention == 0
    assert st.tool_profile == "basic" and st.allow_writes is False  # still opt-in


def test_environment_overrides(clean_env, monkeypatch, golden):
    config.save({"tool_profile": "basic", "allow_writes": False})
    monkeypatch.setenv("P6MCP_TOOL_PROFILE", "full")
    monkeypatch.setenv("P6MCP_ALLOW_WRITES", "1")
    monkeypatch.setenv("P6_SKIP_PROCESS_CHECK", "1")
    st = config.load()
    assert st.tool_profile == "full" and st.allow_writes is True and st.block_when_p6_running is False


def test_cache_follows_file_changes(clean_env):
    config.save({"log_level": "INFO"})
    assert config.load().log_level == "INFO"
    config.save({"log_level": "ERROR"})
    assert config.load().log_level == "ERROR"
