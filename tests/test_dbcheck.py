import os
import sqlite3

from conftest import fingerprint, sha

from p6mcp import dbcheck

BOOT = """<?xml version="1.0" encoding="utf-8"?>
<BootStrap>
  <DataBaseAliases DefaultPMAlias="PMDB">
    <DataBaseAlias>
      <Name>{alias}</Name>
      <Connection>
        <DriverName>SQLite</DriverName>
        <DataBase>{path}</DataBase>
      </Connection>
    </DataBaseAlias>
    <DataBaseAlias>
      <Name>SERVER</Name>
      <Connection><DriverName>MSSQL</DriverName><DataBase>pmdb</DataBase></Connection>
    </DataBaseAlias>
  </DataBaseAliases>
</BootStrap>
"""


def test_connection_on_a_p6_database(golden):
    before = sha(golden)
    res = dbcheck.test_connection(golden)
    assert res["ok"] and res["error"] is None
    assert res["p6_version"] == "25.12" and res["projects"] == 1 and res["eps_nodes"] == 1 and res["baselines"] == 0
    assert res["schema"]["known"] is False and "schema_unknown" in res["warnings"]
    assert res["writes_possible"] is False and res["file_writable"] is True
    assert sha(golden) == before  # the test only reads


def test_trusted_schema_allows_writes(golden):
    res = dbcheck.test_connection(golden, trusted=[fingerprint(golden)])
    assert res["schema"]["trusted"] and res["writes_possible"] and "schema_unknown" not in res["warnings"]


def test_connection_errors(tmp_path):
    assert "No file" in dbcheck.test_connection("")["error"]
    assert "not found" in dbcheck.test_connection(str(tmp_path / "x.db"))["error"]
    text = tmp_path / "notes.db"
    text.write_text("hello, this is not a database" * 50, encoding="utf-8")
    assert "not a SQLite database" in dbcheck.test_connection(str(text))["error"]
    other = tmp_path / "other.db"
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE t (a)")
    con.close()
    res = dbcheck.test_connection(str(other))
    assert not res["ok"] and "not a P6 Professional database" in res["error"]


def test_suggestions_from_p6_connections(tmp_path, monkeypatch, golden):
    appdata, local, home = tmp_path / "roaming", tmp_path / "local", tmp_path / "home"
    for ver, alias, path in (("23.12.1", "OLD", golden), ("25.12.0", "PMDB", str(tmp_path / "gone.db"))):
        d = appdata / "Oracle" / "Primavera P6" / "P6 Professional" / ver
        d.mkdir(parents=True)
        (d / "prmbootstrapV2.xml").write_text(BOOT.format(alias=alias, path=path), encoding="utf-8")
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("USERPROFILE", str(home))
    s = dbcheck.suggestions()
    assert [x["source"] for x in s] == ["P6 Professional 25.12.0, connection PMDB",
                                        "P6 Professional 23.12.1, connection OLD", "default location"]
    assert s[0]["exists"] is False and s[1]["exists"] is True and s[1]["size_bytes"] == os.path.getsize(golden)
    assert s[2]["path"] == os.path.join(str(home), "Documents", "PPMDBSQLite.db")


def test_bootstrap_unreadable(tmp_path):
    bad = tmp_path / "PrmBootStrapV2.xml"
    bad.write_text("<not xml", encoding="utf-8")
    assert dbcheck.bootstrap_sqlite_paths(str(bad)) == []
