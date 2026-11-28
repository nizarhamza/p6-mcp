"""Templates of the tool messages (p6mcp.messages._t) and their catalogs in p6mcp/locales/tools.

    python tools/tool_messages.py --check             every template translated, no stale entry, same placeholders
    python tools/tool_messages.py --missing fr [file] the templates fr.json lacks, as JSON (to translate)
    python tools/tool_messages.py --prune fr          drop the entries no template uses any more

A template is the literal first argument of _t(...), or a module-level str constant (NAME = "...") passed as
_t(NAME, ...). tests/test_tool_messages.py runs the same checks.
"""

from __future__ import annotations

import ast
import glob
import json
import os
import re
import string
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, "p6mcp")
CATALOG_DIR = os.path.join(PACKAGE, "locales", "tools")
LANGUAGES = ("fr",)


def sources() -> list[str]:
    return sorted(glob.glob(os.path.join(PACKAGE, "**", "*.py"), recursive=True))


def _constants(tree: ast.Module) -> dict[str, str]:
    out = {}
    for st in tree.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name) \
                and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
            out[st.targets[0].id] = st.value.value
    return out


def extract(paths=None) -> tuple[dict[str, list[str]], list[str]]:
    """{template: [file:line, ...]} of every _t call, and the calls whose template is not a literal."""
    paths = paths or sources()
    trees = {p: ast.parse(open(p, encoding="utf-8").read(), p) for p in paths}
    consts: dict[str, str] = {}
    for t in trees.values():
        consts.update(_constants(t))
    found: dict[str, list[str]] = {}
    bad: list[str] = []
    for p, tree in trees.items():
        rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
        local = _constants(tree)
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_t"):
                continue
            a = n.args[0] if n.args else None
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                tmpl = a.value
            elif isinstance(a, ast.Name) and (a.id in local or a.id in consts):
                tmpl = local.get(a.id, consts.get(a.id))
            elif isinstance(a, ast.Attribute) and a.attr in consts:  # module.CONSTANT (policy.REMOTE_PATH_ARG)
                tmpl = consts[a.attr]
            else:
                if rel != "p6mcp/messages.py":
                    bad.append(f"{rel}:{n.lineno}")
                continue
            found.setdefault(tmpl, []).append(f"{rel}:{n.lineno}")
    return found, bad


def fields(template: str) -> set[tuple[str, str, str]]:
    """The replacement fields of a format template: (name, conversion, format spec)."""
    out = set()
    for _lit, name, spec, conv in string.Formatter().parse(template):
        if name is not None:
            out.add((name, conv or "", spec or ""))
    return out


def load(lang: str) -> dict[str, str]:
    path = os.path.join(CATALOG_DIR, f"{lang}.json")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return {k: v for k, v in json.load(fh).items() if not k.startswith("_")}


def problems(lang: str, found=None) -> list[str]:
    found = found if found is not None else extract()[0]
    cat = load(lang)
    out = [f"missing {lang}: {t!r} ({found[t][0]})" for t in found if not cat.get(t)]
    out += [f"stale {lang}: {t!r}" for t in cat if t not in found]
    for t, v in cat.items():
        if t in found and v:
            try:
                if fields(t) != fields(v):
                    out.append(f"placeholders {lang}: {t!r}")
            except ValueError as e:
                out.append(f"bad template {lang}: {t!r}: {e}")
            if re.search("[–—«»“”]", v) or "--" in v:
                out.append(f"house style {lang}: {v!r}")
    return out


def _write(lang: str, data: dict) -> None:
    os.makedirs(CATALOG_DIR, exist_ok=True)
    with open(os.path.join(CATALOG_DIR, f"{lang}.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")


def main(argv: list[str]) -> int:
    found, bad = extract()
    if "--missing" in argv:
        lang = argv[argv.index("--missing") + 1]
        only = [os.path.relpath(os.path.abspath(a), ROOT).replace(os.sep, "/")
                for a in argv[argv.index("--missing") + 2:]]
        cat = load(lang)
        miss = {t: "" for t, where in found.items() if not cat.get(t)
                and (not only or any(w.split(":")[0] in only for w in where))}
        print(json.dumps(miss, ensure_ascii=False, indent=1))
        return 0
    if "--prune" in argv:
        lang = argv[argv.index("--prune") + 1]
        _write(lang, {t: v for t, v in load(lang).items() if t in found})
        return 0
    out = [f"template not a literal: {b}" for b in bad]
    for lang in LANGUAGES:
        out += problems(lang, found)
    for line in out:
        print(line)
    print(f"{len(found)} templates, {len(out)} problems")
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
