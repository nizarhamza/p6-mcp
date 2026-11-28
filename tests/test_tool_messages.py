"""Tool messages in English and French (p6mcp.messages): catalogs complete, no raw English text left at the places
a person reads, French replies with unchanged machine values, English kept where a program reads the text."""

from __future__ import annotations

import ast
import glob
import importlib.util
import json
import os
import pickle
import re

import pytest

from p6mcp import messages, policy
from p6mcp.messages import Msg, _join, _t

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_tool_messages():
    spec = importlib.util.spec_from_file_location("tool_messages", os.path.join(ROOT, "tools", "tool_messages.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TM = _load_tool_messages()

# The tool layer: every module whose texts can reach a tool reply or a tool error.
TOOL_LAYER = (["p6mcp/registry.py", "p6mcp/policy.py", "p6mcp/schema.py", "p6mcp/config.py", "p6mcp/xerstore.py",
               "p6mcp/pmdb.py", "p6mcp/shop.py", "p6mcp/remote/server.py", "p6mcp/remote/files.py",
               "p6mcp/remote/shaping.py"]
              + sorted(os.path.relpath(p, ROOT).replace(os.sep, "/") for d in ("core", "tools", "eppm")
                       for p in glob.glob(os.path.join(ROOT, "p6mcp", d, "*.py"))))
EXCEPTIONS = {"ValueError", "RuntimeError", "PermissionError", "FileNotFoundError", "ToolError", "LookupError",
              "KeyError", "TimeoutError", "OSError", "ConnectionError", "FileExistsError", "TypeError"}
TEXT_KEYS = {"note", "notes", "next", "caveat", "error", "reason", "warning", "warnings", "message", "advice",
             "hint", "explanation", "definition", "critical_definition", "iterations_note", "remote_note", "detail",
             "skipped", "would_do", "done", "refuse_reasons"}
TEXT_LISTS = {"plan", "notes", "warnings", "reasons", "advice"}
REFUSAL_FUNCS = {"write_refusal", "refusal", "remote_refusal", "_write_refusal", "_guard_write", "_eppm_refusal",
                 "_plan_gate"}
# Deliberate English, by (file, function): developer errors of the tool declarations and errors of the startup and
# HTTP layers that never reach a tool reply.
ALLOWED = {
    ("p6mcp/registry.py", "deco"),          # @_tool declaration errors
    ("p6mcp/policy.py", "kind"),            # a tool without a TOOL_KIND entry
    ("p6mcp/config.py", "confirm_database"),  # Settings window only
    ("p6mcp/core/calendar.py", "add_work_hours"),  # an internal assertion that cannot happen
    ("p6mcp/remote/server.py", "build_mcp"),  # remote server startup (status file)
    ("p6mcp/remote/server.py", "__init__"),
    ("p6mcp/remote/server.py", "capped_receive"),  # HTTP body cap
}


def _sentence(node) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr):
        text = "".join(v.value if isinstance(v, ast.Constant) else " " for v in node.values)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _sentence(node.left) or _sentence(node.right)
    elif isinstance(node, ast.IfExp):
        return _sentence(node.body) or _sentence(node.orelse)
    else:
        return False
    return len(re.findall(r"[A-Za-z]{2,}", text)) >= 2


def raw_texts(rel: str, root: str = ROOT) -> list[str]:
    """Raw English sentences at the places a person reads: raised errors, message keys of results, plan / notes /
    warnings lists, refusal returns."""
    tree = ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}

    def func(n):
        while n in parents:
            n = parents[n]
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return n.name
        return None

    hits = []

    def check(node, where):
        if node is not None and _sentence(node) and (rel, func(where)) not in ALLOWED:
            hits.append(f"{rel}:{node.lineno}")

    for n in ast.walk(tree):
        if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call) and n.exc.args:
            f = n.exc.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if name in EXCEPTIONS or name.endswith("Error"):
                check(n.exc.args[0], n)
        elif isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values, strict=True):
                if isinstance(k, ast.Constant) and k.value in TEXT_KEYS:
                    for e in (v.elts if isinstance(v, (ast.List, ast.Tuple)) else [v]):
                        check(e, n)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "append" \
                and isinstance(n.func.value, ast.Name) and n.func.value.id in TEXT_LISTS and n.args:
            check(n.args[0], n)
        elif isinstance(n, ast.Return) and func(n) in REFUSAL_FUNCS:
            check(n.value, n)
    return hits


def test_no_raw_english_text_in_the_tool_layer():
    hits = [h for rel in TOOL_LAYER for h in raw_texts(rel)]
    assert not hits, "wrap these texts in _t(...) (p6mcp.messages) or allow-list them: " + ", ".join(hits)


def test_the_scan_finds_a_raw_message(tmp_path):
    (tmp_path / "p6mcp").mkdir()
    (tmp_path / "p6mcp" / "x.py").write_text(
        'def f(p, plan):\n    raise ValueError(f"Project {p} not found")\n'
        'def g(p, plan):\n    plan.append(_t("Delete {p} now", p=p))\n'
        '    raise ValueError(_t("Project {p} gone", p=p))\n'
        'def h(plan):\n    plan.append("Delete everything now")\n    return {"note": "Read only here."}\n',
        encoding="utf-8")
    assert raw_texts("p6mcp/x.py", str(tmp_path)) == ["p6mcp/x.py:2", "p6mcp/x.py:7", "p6mcp/x.py:8"]


def test_catalogs_are_complete_with_the_same_placeholders():
    found, bad = TM.extract()
    assert not bad, f"_t needs a literal template (or a module constant): {bad}"
    assert len(found) > 1000
    assert TM.problems("fr", found) == []


def test_french_catalog_house_style():
    fr = TM.load("fr")
    for en, text in fr.items():
        assert not re.search("[–—«»“”]", text) and "--" not in text, en
        assert not re.search(r"[^\W\d_]:\s", text), f"French puts a space before ':' in {text!r}"
    assert fr["Project not found: {project}"].startswith("Projet introuvable")


def test_msg_is_its_english_text():
    m = _t("Project not found: {project}", project="P")
    assert m == "Project not found: P" and isinstance(m, str)
    assert isinstance(str(ValueError(m)), Msg)  # str(e) keeps the template, so a caught error stays translatable
    assert pickle.loads(pickle.dumps(m)).render("fr") == m.render("fr") == "Projet introuvable : P"
    assert _t("{{literal}} {n}", n=1) == "{literal} 1"
    j = _join("; ", [m, "plain"])
    assert j == "Project not found: P; plain" and j.render("fr") == "Projet introuvable : P; plain"
    assert messages.localize({"a": [j], "b": (m,), "k": 1}, "fr") == {
        "a": ["Projet introuvable : P; plain"], "b": ("Projet introuvable : P",), "k": 1}
    assert messages.localize({"a": m}, "en")["a"] is m


def test_missing_or_broken_translation_falls_back_to_english(monkeypatch):
    monkeypatch.setitem(messages.catalog("fr"), "Project not found: {project}", "Projet {inconnu}")
    assert _t("Project not found: {project}", project="P").render("fr") == "Project not found: P"
    assert _t("A text no catalog has").render("fr") == "A text no catalog has"


def test_language_resolution(monkeypatch):
    monkeypatch.setattr(messages, "_system_language", lambda: "fr")
    assert messages.resolve("auto") == "fr" and messages.resolve(None) == "fr"
    assert messages.resolve("en") == "en" and messages.resolve("FR") == "fr"
    assert messages.resolve("ar") == "en"  # no tool catalog: English
    monkeypatch.setattr(messages, "_system_language", lambda: "de")
    assert messages.resolve("auto") == "en"


# ----------------------------------------------------------------- replies in French

@pytest.fixture
def fr(p6, monkeypatch):
    """The test database with the Language setting on French (the English pin of the tests removed)."""
    monkeypatch.delenv(messages.ENV_LANGUAGE, raising=False)
    p6.set(language="fr")
    return p6


def _shape(obj):
    if isinstance(obj, dict):
        return {k: _shape(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shape(v) for v in obj]
    return "str" if isinstance(obj, str) else type(obj).__name__


VOLATILE = {"snapshot", "seconds", "ms", "engine_cache"}  # differ between two calls in any language


def _codes(obj):
    """Every short code-like value (status, type, check and cause codes, IDs, dates), None for texts."""
    if isinstance(obj, dict):
        return {k: _codes(v) for k, v in obj.items() if k not in VOLATILE}
    if isinstance(obj, list):
        return [_codes(v) for v in obj]
    if isinstance(obj, str):
        return obj if re.fullmatch(r"[A-Za-z0-9_.:\-]+", obj) else None
    return obj


def test_errors_and_refusals_follow_the_language_setting(fr, monkeypatch):
    with pytest.raises(ValueError, match="introuvable"):
        fr.call("p6_get_activity", project="DEMO-A", task_code="NOPE")
    fr.set(language="en")  # switched in Settings: the next call answers in English, without a restart
    with pytest.raises(ValueError, match="not found"):
        fr.call("p6_get_activity", project="DEMO-A", task_code="NOPE")
    fr.set(language="fr", allow_writes=False)
    with pytest.raises(ValueError, match="Les opérations d'écriture sont désactivées"):
        fr.call("p6_update_activity", project="DEMO-A", task_code="A1020", physical_pct=10)
    fr.set(tool_profile="basic")
    with pytest.raises(ValueError, match="profil d'outils"):
        fr.call("p6_sql_select", sql="select 1")
    fr.set(databases=[], active_database=None, tool_profile="full")
    with pytest.raises(RuntimeError, match="Aucune base de données"):
        fr.call("p6_list_projects")
    monkeypatch.setenv(messages.ENV_LANGUAGE, "en")  # the environment wins over the setting
    with pytest.raises(RuntimeError, match="No database configured"):
        fr.call("p6_list_projects")


def test_two_step_write_in_french_keeps_the_edit_log_english(fr):
    kw = {"project": "DEMO-A", "task_code": "A1020", "physical_pct": 10}
    dry = fr.call("p6_update_activity", **kw)
    assert dry["dry_run"] is True and dry["confirmation_token"] in dry["next"] and "Montrez" in dry["next"]
    assert all(type(line) is str for line in dry["would_do"])
    res = fr.call("p6_update_activity", **kw, dry_run=False, confirmation_token=dry["confirmation_token"])
    assert res["applied"] is True and "F9" in res["next"]
    assert res["done"][:len(dry["would_do"])] == dry["would_do"]  # the plan in French, both times
    with open(res["edit_log"]["file"], encoding="utf-8") as fh:
        entry = json.loads(fh.read().splitlines()[-1])
    assert entry["summary"] and not re.search("[éèàùç]", json.dumps(entry, ensure_ascii=False))


def test_confirmation_text_stays_english(fr):
    dry = fr.call("p6_delete_activity", project="DEMO-A", task_code="A1060")
    assert dry["confirmation_text_required"] == "DELETE ACTIVITY DEMO-A A1060"
    assert 'confirmation_text="DELETE ACTIVITY DEMO-A A1060"' in dry["next"] and "Montrez" in dry["next"]
    with pytest.raises(ValueError, match='confirmation_text="DELETE ACTIVITY DEMO-A A1060"'):
        fr.call("p6_delete_activity", project="DEMO-A", task_code="A1060", dry_run=False,
                confirmation_token=dry["confirmation_token"], confirmation_text="SUPPRIMER")


def test_a_language_change_between_dry_run_and_apply_keeps_the_plan(fr):
    kw = {"project": "DEMO-A", "task_code": "A1020", "physical_pct": 20}
    dry = fr.call("p6_update_activity", **kw)
    fr.set(language="en")
    res = fr.call("p6_update_activity", **kw, dry_run=False, confirmation_token=dry["confirmation_token"])
    assert res["applied"] is True  # the plan digest is taken on the English text


@pytest.mark.parametrize("tool,kw", [
    ("p6_project_summary", {"project": "DEMO-A"}),
    ("p6_health_check", {"project": "DEMO-A"}),
    ("p6_critical_path", {"project": "DEMO-A"}),
    ("p6_float_analysis", {"project": "DEMO-A"}),
    ("p6_float_origin", {"project": "DEMO-A", "task_code": "A1020"}),
    ("p6_cpm_validate", {"project": "DEMO-A"}),
    ("p6_schedule_options", {"project": "DEMO-A"}),
    ("p6_lookahead", {"project": "DEMO-A"}),
    ("p6_diagnose", {}),
])
def test_french_reply_keeps_keys_and_codes(fr, monkeypatch, tool, kw):
    res_fr = fr.call(tool, **kw)
    monkeypatch.setenv(messages.ENV_LANGUAGE, "en")
    res_en = fr.call(tool, **kw)
    assert _shape(res_fr) == _shape(res_en)  # same keys and value types
    assert _codes(res_fr) == _codes(res_en)  # same codes: only the texts differ
    assert all(not isinstance(v, Msg) for v in json.loads(json.dumps(res_fr)).values())


def test_notes_and_definitions_are_french(fr):
    cp = fr.call("p6_critical_path", project="DEMO-A")
    assert "marge totale" in json.dumps(cp, ensure_ascii=False).lower()
    s = json.dumps(fr.call("p6_project_summary", project="DEMO-A"), ensure_ascii=False)
    assert "dernier F9" in s


def test_remote_refusals_render_in_french():
    why = policy.remote_refusal("p6_delete_project", "full", True, False, ["p6:read"])
    assert why.render("fr").startswith("p6_delete_project n'est pas disponible pour les clients distants")
    assert policy.remote_refusal("p6_list_projects", "full", True, False, None).render("fr").startswith(
        "Cette connexion n'a pas accès")
