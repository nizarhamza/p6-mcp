import ast
import inspect
import json
import os
import re
import subprocess
import sys

import pytest

from p6mcp import config, policy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_every_tool_is_classified(server):
    tools = {n for n in dir(server) if n.startswith("p6_") and callable(getattr(server, n))}
    assert tools == set(server.TOOL_HINTS) == set(policy.TOOL_KIND) == set(server.REGISTERED)


def test_hints_agree_with_kinds(server):
    for name, kind in policy.TOOL_KIND.items():
        h = server.TOOL_HINTS[name]
        if kind == policy.READ:
            assert h["readOnlyHint"], name
        else:
            assert not h["readOnlyHint"], name
        if kind == policy.DESTRUCTIVE:
            assert h["destructiveHint"], name


def test_hints_are_literal_in_the_source(server):
    """Static scanners (M8ven) read the hints from the decorator line: a dict literal of the four booleans."""
    with open(server.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    found = {}
    for node in tree.body:
        for d in getattr(node, "decorator_list", ()):
            if isinstance(d, ast.Call) and getattr(d.func, "id", None) == "_tool":
                assert not d.args and [k.arg for k in d.keywords] == ["annotations"], node.name
                assert isinstance(d.keywords[0].value, ast.Dict), node.name
                found[node.name] = ast.literal_eval(d.keywords[0].value)
    assert found == server.TOOL_HINTS
    for name, hints in found.items():
        assert tuple(hints) == server.HINT_KEYS, name
        assert all(type(v) is bool for v in hints.values()), name


def test_tool_refuses_other_annotations(server):
    def p6_probe():
        return None
    with pytest.raises(ValueError, match="in this order"):
        server._tool(annotations={"readOnlyHint": True})(p6_probe)
    with pytest.raises(ValueError, match="in this order"):
        server._tool(annotations={"readOnlyHint": 1, "destructiveHint": False, "idempotentHint": True,
                                  "openWorldHint": False})(p6_probe)
    with pytest.raises(ValueError, match="cannot be destructive"):
        server._tool(annotations={"readOnlyHint": True, "destructiveHint": True, "idempotentHint": True,
                                  "openWorldHint": False})(p6_probe)
    assert "p6_probe" not in server.TOOL_HINTS and "p6_probe" not in server.REGISTERED


def test_every_tool_is_named_in_a_test(server):
    """Each tool has at least one test calling it by name (the names are read here, never written)."""
    folder = os.path.join(ROOT, "tests")
    text = ""
    for name in sorted(os.listdir(folder)):
        if name.endswith(".py"):
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                text += fh.read()
    assert [t for t in server.TOOL_HINTS if not re.search(rf"\b{t}\b", text)] == []


def test_write_tools_expose_the_two_step_parameters(server):
    for name, kind in policy.TOOL_KIND.items():
        params = inspect.signature(server.REGISTERED[name]).parameters
        if kind in (policy.WRITE, policy.DESTRUCTIVE, policy.CONFIG):
            assert params["dry_run"].default is True, name
            assert "confirmation_token" in params, name
            assert ("confirmation_text" in params) == (kind == policy.DESTRUCTIVE), name
            assert "Two steps" in server.REGISTERED[name].__doc__
        else:
            assert "dry_run" not in params and "confirmation_token" not in params, name


def test_basic_profile_is_a_small_read_only_set():
    assert 8 <= len(policy.BASIC_TOOLS) <= 12
    assert all(policy.TOOL_KIND[t] == policy.READ for t in policy.BASIC_TOOLS)
    assert "p6_sql_select" not in policy.BASIC_TOOLS


@pytest.mark.parametrize("profile,writes", [("basic", False), ("basic", True), ("full", False), ("full", True)])
def test_listing_rules(profile, writes):
    listed = {t for t in policy.TOOL_KIND if policy.listed(t, profile, writes)}
    if profile == "basic":
        assert listed == policy.BASIC_TOOLS
    elif writes:
        assert listed == set(policy.TOOL_KIND)
    else:
        assert listed == {t for t, k in policy.TOOL_KIND.items() if k in (policy.READ, policy.FILE, policy.CONFIG)}
    assert not policy.listed("p6_shop_search", profile, writes, shop=False)
    assert policy.refusal("p6_shop_install", "full", writes, shop=False) == policy.SHOP_OFF
    assert policy.refusal("p6_shop_install", "full", False) is None  # settings only: writes not needed


def _listed_by_a_fresh_server(tmp_path, cfg: dict) -> set:
    path = tmp_path / "config.json"
    config.save(cfg, str(path))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("P6_", "P6MCP_"))}
    env.update(P6MCP_CONFIG=str(path), PYTHONPATH=ROOT)
    code = ("import asyncio, json; from p6mcp.server import mcp; "
            "print(json.dumps(sorted(t.name for t in asyncio.run(mcp.list_tools()))))")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


def test_clients_see_only_the_basic_tools(tmp_path):
    assert _listed_by_a_fresh_server(tmp_path, {"tool_profile": "basic"}) == policy.BASIC_TOOLS


def test_clients_see_no_write_tool_when_writes_are_off(tmp_path):
    names = _listed_by_a_fresh_server(tmp_path, {"tool_profile": "full", "allow_writes": False})
    assert names and not any(policy.is_write(n) for n in names)
    assert "p6_sql_select" in names and "p6_export_xer" in names
