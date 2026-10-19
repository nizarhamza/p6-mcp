"""Minimal-change editing of JSON / JSONC files (comments and trailing commas allowed).

MCP clients keep their settings in JSON files that the user also edits by hand (VS Code allows
comments). Rewriting such a file with json.dump would drop comments and reformat everything, so
edits here change only the text of one member: everything else stays byte for byte.

    text = set_member(text, ["mcpServers"], "p6mcp", {"command": "x.exe"})
    text = delete_member(text, ["mcpServers"], "p6mcp")
    value = loads(text)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence


class JsoncError(ValueError):
    def __init__(self, msg: str, text: str = "", pos: int = 0):
        line = text.count("\n", 0, pos) + 1
        col = pos - (text.rfind("\n", 0, pos) + 1) + 1
        super().__init__(f"{msg} (line {line}, column {col})" if text else msg)


@dataclass
class Member:
    key: str
    key_start: int
    value_start: int
    value_end: int
    comma: Optional[int] = None  # index of the comma after the value, if any
    node: Any = None              # Obj for an object value


@dataclass
class Obj:
    start: int  # index of '{'
    end: int    # index after '}'
    members: list[Member] = field(default_factory=list)

    def get(self, key: str) -> Optional[Member]:
        found = [m for m in self.members if m.key == key]
        return found[-1] if found else None  # JSON parsers keep the last duplicate


_NUM = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")


class _Parser:
    def __init__(self, text: str):
        self.t = text
        self.n = len(text)

    def ws(self, i: int) -> int:
        t, n = self.t, self.n
        while i < n:
            c = t[i]
            if c in " \t\r\n﻿":
                i += 1
            elif t.startswith("//", i):
                j = t.find("\n", i)
                i = n if j < 0 else j + 1
            elif t.startswith("/*", i):
                j = t.find("*/", i + 2)
                if j < 0:
                    raise JsoncError("unterminated comment", t, i)
                i = j + 2
            else:
                break
        return i

    def string(self, i: int) -> tuple[str, int]:
        j, t = i + 1, self.t
        while j < self.n:
            c = t[j]
            if c == "\\":
                j += 2
                continue
            if c == '"':
                try:
                    return json.loads(t[i:j + 1]), j + 1
                except ValueError as e:
                    raise JsoncError(f"bad string: {e}", t, i) from None
            if c == "\n":
                break
            j += 1
        raise JsoncError("unterminated string", t, i)

    def value(self, i: int) -> tuple[Any, int, int, Any]:
        """(python value, start, end, Obj or None)."""
        i = self.ws(i)
        if i >= self.n:
            raise JsoncError("unexpected end of file", self.t, i)
        c = self.t[i]
        if c == "{":
            return self.obj(i)
        if c == "[":
            return self.arr(i)
        if c == '"':
            v, j = self.string(i)
            return v, i, j, None
        for word, v in (("true", True), ("false", False), ("null", None)):
            if self.t.startswith(word, i):
                return v, i, i + len(word), None
        m = _NUM.match(self.t, i)
        if m:
            s = m.group()
            return (float(s) if any(ch in s for ch in ".eE") else int(s)), i, m.end(), None
        raise JsoncError(f"unexpected character {c!r}", self.t, i)

    def obj(self, i: int):
        o, out = Obj(i, i), {}
        j = self.ws(i + 1)
        while True:
            if j >= self.n:
                raise JsoncError("unterminated object", self.t, i)
            if self.t[j] == "}":
                o.end = j + 1
                return out, i, j + 1, o
            if self.t[j] != '"':
                raise JsoncError("expected a quoted key", self.t, j)
            key, k = self.string(j)
            k = self.ws(k)
            if k >= self.n or self.t[k] != ":":
                raise JsoncError("expected ':'", self.t, k)
            v, vs, ve, node = self.value(k + 1)
            out[key] = v
            m = Member(key, j, vs, ve, None, node)
            o.members.append(m)
            j = self.ws(ve)
            if j < self.n and self.t[j] == ",":
                m.comma = j
                j = self.ws(j + 1)
            elif j < self.n and self.t[j] != "}":
                raise JsoncError("expected ',' or '}'", self.t, j)

    def arr(self, i: int):
        out = []
        j = self.ws(i + 1)
        while True:
            if j >= self.n:
                raise JsoncError("unterminated array", self.t, i)
            if self.t[j] == "]":
                return out, i, j + 1, None
            v, _vs, ve, _ = self.value(j)
            out.append(v)
            j = self.ws(ve)
            if j < self.n and self.t[j] == ",":
                j = self.ws(j + 1)
            elif j < self.n and self.t[j] != "]":
                raise JsoncError("expected ',' or ']'", self.t, j)


def parse(text: str) -> tuple[Any, Optional[Obj]]:
    """(value, root Obj when the root is an object). Raises JsoncError."""
    p = _Parser(text)
    v, _s, e, node = p.value(0)
    rest = p.ws(e)
    if rest != len(text):
        raise JsoncError("unexpected content after the JSON value", text, rest)
    return v, node


def loads(text: str) -> Any:
    return parse(text)[0]


def has_comments(text: str) -> bool:
    """True when the file uses JSONC features (comments or trailing commas)."""
    try:
        json.loads(text.lstrip("﻿"))
        return False
    except ValueError:
        return True


# ----------------------------------------------------------------- editing

def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _line_indent(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    m = re.match(r"[ \t]*", text[start:pos])
    return m.group() if m else ""


def _indent_unit(text: str) -> str:
    m = re.search(r"\n([ \t]+)\S", text)
    if not m:
        return "  "
    ind = m.group(1)
    return "\t" if ind.startswith("\t") else " " * min(len(ind), 8)


def _dump(value: Any, indent: str, unit: str, nl: str) -> str:
    s = json.dumps(value, indent=unit if unit != "\t" else "\t", ensure_ascii=False)
    return s.replace("\n", nl + indent)


def _find(root: Obj, path: Sequence[str]) -> Optional[Obj]:
    node = root
    for key in path:
        m = node.get(key)
        if m is None or not isinstance(m.node, Obj):
            return None
        node = m.node
    return node


def _insert(text: str, obj: Obj, key: str, value: Any) -> str:
    nl, unit = _newline(text), _indent_unit(text)
    if obj.members:
        indent = _line_indent(text, obj.members[0].key_start)
        if not indent.strip() and text.rfind("\n", 0, obj.members[0].key_start) < obj.start:
            indent = _line_indent(text, obj.start) + unit  # members on the brace's line: one level deeper
    else:
        indent = _line_indent(text, obj.start) + unit
    member = json.dumps(key, ensure_ascii=False) + ": " + _dump(value, indent, unit, nl)
    if not obj.members:
        inner = text[obj.start + 1:obj.end - 1]
        if inner.strip():  # only comments inside: keep them, add after
            return text[:obj.end - 1].rstrip(" \t") + nl + indent + member + nl + _line_indent(text, obj.start) + \
                text[obj.end - 1:]
        return text[:obj.start + 1] + nl + indent + member + nl + _line_indent(text, obj.start) + text[obj.end - 1:]
    last = obj.members[-1]
    pos = (last.comma + 1) if last.comma is not None else last.value_end
    # keep a comment that ends the last member's line on that line
    m = re.match(r"[ \t]*(//[^\r\n]*)?", text[pos:])
    after = pos + (m.end() if m else 0)
    comma = "" if last.comma is not None else ","
    return text[:last.value_end] + comma + text[last.value_end:after] + nl + indent + member + text[after:]


def set_member(text: str, path: Sequence[str], key: str, value: Any) -> str:
    """Set path..key to value, creating the missing objects of path. Other text is unchanged."""
    if not text.strip():
        text = "{}"
    data, root = parse(text)
    if not isinstance(root, Obj):
        raise JsoncError("the file does not hold a JSON object")
    node, done = root, []
    for p in path:
        m = node.get(p)
        if m is None:
            # create the rest of the path in one insertion
            nested: Any = value
            for k in reversed([*path[len(done) + 1:], key]):
                nested = {k: nested}
            return set_member_checked(text, data, list(path), key, value, _insert(text, node, p, nested))
        if not isinstance(m.node, Obj):
            raise JsoncError(f"{'.'.join(done + [p])} is not an object", text, m.value_start)
        done.append(p)
        node = m.node
    m = node.get(key)
    if m is None:
        new = _insert(text, node, key, value)
    else:
        nl, unit = _newline(text), _indent_unit(text)
        new = text[:m.value_start] + _dump(value, _line_indent(text, m.key_start), unit, nl) + text[m.value_end:]
    return set_member_checked(text, data, list(path), key, value, new)


def set_member_checked(text: str, before: Any, path: list[str], key: str, value: Any, new: str) -> str:
    """The edit must parse and change exactly path..key (a safety net against any editing bug)."""
    expected = json.loads(json.dumps(before))
    node = expected
    for p in path:
        node = node.setdefault(p, {})
    node[key] = value
    try:
        got = loads(new)
    except JsoncError as e:
        raise JsoncError(f"internal edit error, file left unchanged: {e}") from None
    if got != expected:
        raise JsoncError("internal edit error (other entries would change), file left unchanged")
    return new


def delete_member(text: str, path: Sequence[str], key: str) -> str:
    """Remove path..key (every duplicate of it). Unchanged text when it is absent."""
    data, root = parse(text)
    if not isinstance(root, Obj):
        raise JsoncError("the file does not hold a JSON object")
    obj = _find(root, path)
    if obj is None or obj.get(key) is None:
        return text
    idx = max(i for i, m in enumerate(obj.members) if m.key == key)
    m = obj.members[idx]
    line_start = text.rfind("\n", 0, m.key_start) + 1
    start = line_start if not text[line_start:m.key_start].strip() else m.key_start
    if m.comma is not None:
        end = m.comma + 1
        rest = re.match(r"[ \t]*(\r?\n)?", text[end:])
        end += rest.end() if rest and rest.group(1) else 0
        new = text[:start] + text[end:]
    elif idx > 0:  # last member: remove its lines and the comma before it, keep comments in between
        prev = obj.members[idx - 1]
        if start == line_start and line_start > 0:  # also the line break that precedes it
            start = line_start - 1
            if start > 0 and text[start - 1] == "\r":
                start -= 1
        end = m.value_end
        if prev.comma is not None and prev.comma < start:
            new = text[:prev.comma] + text[prev.comma + 1:start] + text[end:]
        else:
            new = text[:start] + text[end:]
    else:
        new = text[:start] + text[m.value_end:]
    _, nroot = parse(new)
    nobj = _find(nroot, path)
    if nobj is not None and not nobj.members and not text_between(new, nobj).strip():
        new = new[:nobj.start] + "{}" + new[nobj.end:]
    if nobj is not None and nobj.get(key) is not None:
        return delete_member(new, path, key)  # a duplicate key
    expected = json.loads(json.dumps(data))
    node = expected
    for p in path:
        node = node[p]
    node.pop(key, None)
    if loads(new) != expected:
        raise JsoncError("internal edit error (other entries would change), file left unchanged")
    return new


def text_between(text: str, obj: Obj) -> str:
    return text[obj.start + 1:obj.end - 1]
