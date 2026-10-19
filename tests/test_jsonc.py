import json

import pytest

from p6mcp import jsonc

ENTRY = {"command": "C:\\P6\\p6mcp-server.exe", "args": []}

SAMPLES = {
    "plain": '{\n  "globalShortcut": "",\n  "mcpServers": {\n    "other": {\n      "command": "npx"\n    }\n  }\n}\n',
    "empty_servers": '{"mcpServers": {}}',
    "inline": '{"mcpServers": {"a": {"command": "x"}}}',
    "jsonc_crlf_tabs": ('{\r\n\t// my servers\r\n\t"servers": {\r\n\t\t"a": {"type": "stdio", "command": "x"}'
                        ' // keep me\r\n\t},\r\n\t/* block */ "inputs": [],\r\n}\r\n'),
    "trailing_comma": '{\n  "mcpServers": {\n    "a": {"command": "x"},\n  },\n}\n',
    "four_spaces": ('{\n    "mcpServers": {\n        "a": {\n            "command": "x"\n        }\n    },\n'
                    '    "z": 1\n}'),
}


@pytest.mark.parametrize("name", SAMPLES)
def test_add_update_remove_round_trip(name):
    text = SAMPLES[name]
    path = ["servers"] if "servers\"" in text and "mcpServers" not in text else ["mcpServers"]
    before = jsonc.loads(text)
    a = jsonc.set_member(text, path, "p6mcp", ENTRY)
    got = jsonc.loads(a)
    assert got[path[0]]["p6mcp"] == ENTRY
    assert {k: v for k, v in got[path[0]].items() if k != "p6mcp"} == before[path[0]]
    b = jsonc.set_member(a, path, "p6mcp", dict(ENTRY, args=["-x"]))
    assert jsonc.loads(b)[path[0]]["p6mcp"]["args"] == ["-x"]
    c = jsonc.delete_member(b, path, "p6mcp")
    assert jsonc.loads(c) == before
    if name != "trailing_comma":  # the only change allowed: a trailing comma before our entry
        assert c == text
    if "\r\n" in text:
        assert "\n" not in a.replace("\r\n", "")
    for kept in ("// my servers", "// keep me", "/* block */"):
        if kept in text:
            assert kept in a and kept in c


def test_creates_file_and_container():
    t = jsonc.set_member("", ["mcpServers"], "p6mcp", ENTRY)
    assert json.loads(t) == {"mcpServers": {"p6mcp": ENTRY}}
    t = jsonc.set_member('{\n  "theme": "dark"\n}', ["mcpServers"], "p6mcp", ENTRY)
    assert json.loads(t) == {"theme": "dark", "mcpServers": {"p6mcp": ENTRY}}


def test_middle_member_removed_cleanly():
    t = '{\n  "mcpServers": {\n    "a": 1,\n    "p6mcp": {"x": 1},\n    "b": 2\n  }\n}'
    assert jsonc.delete_member(t, ["mcpServers"], "p6mcp") == '{\n  "mcpServers": {\n    "a": 1,\n    "b": 2\n  }\n}'


def test_absent_member_is_a_no_op():
    t = '{"mcpServers": {"a": 1}}'
    assert jsonc.delete_member(t, ["mcpServers"], "p6mcp") == t
    assert jsonc.delete_member('{"x": 1}', ["mcpServers"], "p6mcp") == '{"x": 1}'


def test_duplicate_keys_are_all_removed():
    t = '{"mcpServers": {"p6mcp": 1, "a": 2, "p6mcp": 3}}'
    assert jsonc.loads(jsonc.delete_member(t, ["mcpServers"], "p6mcp")) == {"mcpServers": {"a": 2}}


@pytest.mark.parametrize("bad", ['{"a": }', '{"a": 1', '[1, 2]', '{"a": 1} x', '{"a": "line\nbreak"}',
                                 '{"mcpServers": 3}', '{/* open'])
def test_malformed_input_is_refused(bad):
    with pytest.raises(jsonc.JsoncError):
        jsonc.set_member(bad, ["mcpServers"], "p6mcp", ENTRY)


def test_error_position():
    with pytest.raises(jsonc.JsoncError, match="line 2, column 8"):
        jsonc.loads('{\n  "a": ?\n}')


def test_has_comments():
    assert jsonc.has_comments(SAMPLES["jsonc_crlf_tabs"]) and not jsonc.has_comments(SAMPLES["plain"])
