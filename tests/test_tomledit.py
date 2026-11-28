"""Minimal TOML table edits (p6mcp.tomledit, used for the Codex config.toml)."""

import tomllib

import pytest

from p6mcp import tomledit

P = ["mcp_servers", "p6mcp"]
ENTRY = {"command": r"C:\Program Files\P6 MCP\p6mcp-server.exe", "args": ["-m", "p6mcp"], "startup_timeout_sec": 30}

TRICKY = '''# lines that look like tables but are not
title = """
[mcp_servers.p6mcp]
"""
quoted = """two quotes at the end"""""
literal = \'\'\'
[mcp_servers.p6mcp.env]
\'\'\'
nested = [
  ["[mcp_servers.p6mcp]"],
  { a = "]" },  # ]
]
path = 'C:\\x\\[y]'

[ "mcp_servers" . "github" ]  # spaced and quoted
command = "npx"

[[mcp_servers.github.extra]]
name = "one"

[mcp_servers.'p6mcp-old']
command = "old.exe"
'''


def test_headers_skip_strings_and_arrays():
    keys = [(h.keys, h.array) for h in tomledit.headers(TRICKY)]
    assert keys == [(("mcp_servers", "github"), False), (("mcp_servers", "github", "extra"), True),
                    (("mcp_servers", "p6mcp-old"), False)]


@pytest.mark.parametrize("text", ["", "\n", "x = 1\n", "x = 1\n\n# end\n", TRICKY, TRICKY.replace("\n", "\r\n")])
def test_add_then_delete_gives_the_original_back(text):
    new = tomledit.set_table(text, P, ENTRY)
    assert new.startswith(text) and tomllib.loads(new)["mcp_servers"]["p6mcp"] == ENTRY
    if "\r\n" in text:
        assert "\n" not in new.replace("\r\n", "")
    assert tomledit.delete_table(new, P) == text
    assert tomledit.delete_table(text, P) == text  # nothing to remove


def test_file_without_final_newline():
    new = tomledit.set_table("x = 1", P, ENTRY)
    assert new.startswith("x = 1\n\n[mcp_servers.p6mcp]\n") and tomledit.delete_table(new, P) == "x = 1\n"


def test_replaced_in_place_with_its_subtables():
    text = ('a = 1\n\n[mcp_servers."p6mcp"]  # mine\ncommand = "old"\nargs = [\n  "x",\n]\n\n# next one\n'
            '[mcp_servers.github]\ncommand = "npx"\n\n[mcp_servers.p6mcp.env]\nK = "v"\n')
    new = tomledit.set_table(text, P, ENTRY)
    assert new == ("a = 1\n\n" + tomledit.render(P, ENTRY) + '\n# next one\n[mcp_servers.github]\ncommand = "npx"\n')
    data = tomllib.loads(new)
    assert data["mcp_servers"]["p6mcp"] == ENTRY and data["mcp_servers"]["github"] == {"command": "npx"}


@pytest.mark.parametrize("value", ["it's", 'say "hi"', "back\\slash", "two\nlines", "tab\there", "caf\u00e9 \u2713",
                                   "bell\x07", "del\x7f", ""])
def test_strings_and_keys_round_trip(value):
    table = {"command": value, "args": [value, "plain"], "http_headers": {"Authorization": value, "X Key's": value},
             "enabled": True, "n": 2, "f": 1.5, "empty": {}}
    new = tomledit.set_table("", P, table)
    assert tomllib.loads(new)["mcp_servers"]["p6mcp"] == table


def test_windows_paths_stay_readable():
    assert "command = 'C:\\Program Files\\P6 MCP\\p6mcp-server.exe'" in tomledit.render(P, ENTRY)


@pytest.mark.parametrize("text", [
    'mcp_servers = { p6mcp = { command = "x" } }\n',  # inline table
    '[mcp_servers]\np6mcp.command = "x"\n',            # dotted keys
    "mcp_servers.p6mcp.command = 'x'\n",
])
def test_entries_without_their_own_table_are_refused(text):
    with pytest.raises(tomledit.TomlError, match="edit it by hand"):
        tomledit.set_table(text, P, ENTRY)
    with pytest.raises(tomledit.TomlError, match="edit it by hand"):
        tomledit.delete_table(text, P)


def test_an_edit_that_would_break_the_file_is_refused():
    with pytest.raises(tomledit.TomlError, match="file left unchanged"):  # cannot extend an inline table
        tomledit.set_table('mcp_servers = { github = { command = "x" } }\n', P, ENTRY)
    with pytest.raises(tomledit.TomlError, match="not valid TOML"):
        tomledit.set_table("a = [\n", P, ENTRY)


def test_without_tomllib(monkeypatch):
    monkeypatch.setattr(tomledit, "tomllib", None)
    with pytest.raises(tomledit.TomlError, match="Python 3.11"):
        tomledit.loads("")
