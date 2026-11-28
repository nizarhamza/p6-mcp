"""Minimal-change editing of one table of a TOML file (the Codex config.toml).

tomllib (Python 3.11 and later) only reads TOML, and the user edits config.toml by hand: rewriting the whole file
would drop its comments and reformat everything. Edits here replace or remove the text of one table and of its
subtables ([mcp_servers.p6mcp], [mcp_servers.p6mcp.env]) and leave every other byte as it was. A file that does not
parse is refused, the table must be written with table headers (an inline table or dotted keys defining it are
refused: edit those by hand), and every edit is parsed again and compared with the expected data before it is
returned (TomlError otherwise).

    text = set_table(text, ["mcp_servers", "p6mcp"], {"command": "x.exe", "args": []})
    text = delete_table(text, ["mcp_servers", "p6mcp"])
    data = loads(text)
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Any, Sequence

from p6mcp.jsonc import JsoncError

try:
    import tomllib
except ImportError:  # Python 3.10: no TOML reader in the standard library
    tomllib = None

BARE = re.compile(r"[A-Za-z0-9_-]+")


class TomlError(JsoncError):
    """A TOML file that cannot be read, or an edit that would not be safe (the file is left unchanged)."""


def loads(text: str) -> dict:
    if tomllib is None:
        raise TomlError("reading a TOML file needs Python 3.11 or later")
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise TomlError(f"not valid TOML: {e}") from None


@dataclass
class Header:
    start: int        # first character of the header line
    end: int          # after the header line's newline (the table's body starts here)
    keys: tuple       # decoded dotted key
    array: bool       # [[array of tables]]


def _key_part(text: str, i: int) -> tuple[str, int]:
    """One key of a dotted key at i: (decoded key, index after it)."""
    c = text[i]
    if c == '"':
        j = i + 1
        while text[j] != '"':
            j += 2 if text[j] == "\\" else 1
        return loads("k = " + text[i:j + 1])["k"], j + 1
    if c == "'":
        j = text.index("'", i + 1)
        return text[i + 1:j], j + 1
    m = BARE.match(text, i)
    if not m:
        raise TomlError("unexpected character in a table header")
    return m.group(), m.end()


def _header(text: str, i: int) -> tuple[tuple, bool, int]:
    """The table header starting at text[i] == '[': (keys, array, index after the closing bracket)."""
    array = text.startswith("[[", i)
    i += 2 if array else 1
    keys = []
    while True:
        while text[i] in " \t":
            i += 1
        key, i = _key_part(text, i)
        keys.append(key)
        while text[i] in " \t":
            i += 1
        if text[i] == ".":
            i += 1
            continue
        close = "]]" if array else "]"
        if not text.startswith(close, i):
            raise TomlError("unexpected character in a table header")
        return tuple(keys), array, i + len(close)


def _end_of_line(text: str, i: int) -> int:
    j = text.find("\n", i)
    return len(text) if j < 0 else j + 1


def _string_end(text: str, i: int) -> int:
    """Index after the string starting at i (basic, literal or multi-line)."""
    q = text[i]
    if text.startswith(q * 3, i):
        j = i + 3
        while True:
            if q == '"' and text[j] == "\\":
                j += 2
                continue
            if text.startswith(q * 3, j):
                j += 3
                extra = 0
                while extra < 2 and j < len(text) and text[j] == q:  # up to two quotes end the content
                    j += 1
                    extra += 1
                return j
            j += 1
    j = i + 1
    while text[j] != q:
        j += 2 if (q == '"' and text[j] == "\\") else 1
    return j + 1


def headers(text: str) -> list[Header]:
    """Every table header of a valid TOML text, in order (lines inside strings or multi-line arrays skipped)."""
    out: list[Header] = []
    i, n, depth, line_start = 0, len(text), 0, True
    while i < n:
        c = text[i]
        if line_start and depth == 0:
            j = i
            while j < n and text[j] in " \t":
                j += 1
            if j < n and text[j] == "[":
                keys, array, k = _header(text, j)
                end = _end_of_line(text, k)
                out.append(Header(i, end, keys, array))
                i, line_start = end, True
                continue
            i, line_start = j, False
            continue
        if c == "\n":
            line_start = True
            i += 1
        elif c == "#":
            i = _end_of_line(text, i)
            line_start = True
        elif c in "\"'":
            i = _string_end(text, i)
        else:
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
            i += 1
    return out


def _is_filler(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith("#")


def _blocks(text: str, path: Sequence[str]) -> list[tuple[int, int]]:
    """(start, end) of every table whose key starts with path: its header and body up to its last content line
    (blank and comment lines before the next table belong to that table)."""
    heads, path = headers(text), tuple(path)
    out = []
    for n, h in enumerate(heads):
        if h.keys[:len(path)] != path:
            continue
        stop = heads[n + 1].start if n + 1 < len(heads) else len(text)
        lines = text[h.end:stop].splitlines(keepends=True)
        while lines and _is_filler(lines[-1]):
            lines.pop()
        out.append((h.start, h.end + sum(len(x) for x in lines)))
    return out


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _cut(text: str, start: int, end: int, nl: str) -> str:
    """Remove text[start:end] and the blank line left doubled by it."""
    before, after = text[:start], text[end:]
    if after.startswith(nl) or not after:
        if before.endswith(nl + nl):
            before = before[:-len(nl)]
        elif not before and after.startswith(nl):
            after = after[len(nl):]
    return before + after


ESCAPES = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\t": "\\t", "\r": "\\r"}


def _basic(s: str) -> str:
    out = []
    for ch in s:
        if ch in ESCAPES:
            out.append(ESCAPES[ch])
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _quote(s: str) -> str:
    """A literal string (a Windows path stays readable: no escapes) when possible, else a basic string."""
    if "'" not in s and not any(ord(ch) < 32 or ord(ch) == 127 for ch in s):
        return "'" + s + "'"
    return _basic(s)


def _key(k: str) -> str:
    return k if BARE.fullmatch(k) else _basic(k)


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise TomlError("only finite numbers are written")
        return repr(v)
    if isinstance(v, str):
        return _quote(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + " }" if v else "{}"
    raise TomlError(f"cannot write a {type(v).__name__} value")


def render(path: Sequence[str], table: dict, nl: str = "\n") -> str:
    """The text of a table: its header, then one line per key (nested tables as inline tables)."""
    lines = ["[" + ".".join(_key(k) for k in path) + "]"]
    lines += [f"{_key(k)} = {_value(v)}" for k, v in table.items()]
    return nl.join(lines) + nl


def _without(data: dict, path: Sequence[str]) -> dict:
    """data without path, and without the tables of path left empty by that (they only held it)."""
    data = copy.deepcopy(data)
    chain = [data]
    for k in path[:-1]:
        node = chain[-1].get(k)
        if not isinstance(node, dict):
            return data
        chain.append(node)
    chain[-1].pop(path[-1], None)
    for depth in range(len(path) - 1, 0, -1):
        if not chain[depth]:
            chain[depth - 1].pop(path[depth - 1], None)
    return data


def _get(data: dict, path: Sequence[str]) -> Any:
    node: Any = data
    for k in path:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def _checked(new: str, expected: dict, path: Sequence[str]) -> str:
    """The edit must parse and give exactly the expected data (a safety net against any editing bug)."""
    try:
        got = loads(new)
    except TomlError as e:
        raise TomlError(f"the edit would not be valid TOML, file left unchanged: {e}") from None
    if _without(got, path) != _without(expected, path) or _get(got, path) != _get(expected, path):
        raise TomlError("internal edit error (other settings would change), file left unchanged")
    return new


def _refuse_inline(text: str, data: dict, path: Sequence[str]) -> list[tuple[int, int]]:
    """The text blocks of path; refused when path holds data written without a table header of its own."""
    blocks = _blocks(text, path)
    if _get(data, path) is not None and not blocks:
        raise TomlError(f"[{'.'.join(path)}] is written as an inline table or with dotted keys: edit it by hand")
    return blocks


def set_table(text: str, path: Sequence[str], table: dict) -> str:
    """Write path as a table holding exactly table: its current text (and its subtables) replaced in place, or
    a new table at the end of the file. Other text is unchanged."""
    data = loads(text)
    blocks = _refuse_inline(text, data, path)
    nl = _newline(text)
    block = render(path, table, nl)
    expected = _without(data, path)
    node = expected
    for k in path[:-1]:
        node = node.setdefault(k, {})
    node[path[-1]] = copy.deepcopy(table)
    if blocks:
        new = text
        for start, end in reversed(blocks[1:]):
            new = _cut(new, start, end, nl)
        start, end = blocks[0]
        if not new[start:end].endswith("\n"):  # the table ends the file without a newline: keep it so
            block = block[:-len(nl)]
        new = new[:start] + block + new[end:]
    else:
        new = text
        if new and not new.endswith("\n"):
            new += nl
        if new.strip() and not new.endswith(nl + nl):
            new += nl
        new += block
    return _checked(new, expected, path)


def delete_table(text: str, path: Sequence[str]) -> str:
    """Remove path (its table and subtables); the text is unchanged when it is not there."""
    data = loads(text)
    if _get(data, path) is None:
        return text
    blocks = _refuse_inline(text, data, path)
    nl, new = _newline(text), text
    for start, end in reversed(blocks):
        new = _cut(new, start, end, nl)
    return _checked(new, _without(data, path), path)
