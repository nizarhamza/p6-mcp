"""Write p6mcp/data/core_skills from a checkout of the skill shop catalog (nizarhamza/p6-mcp-shop).

    python tools/bundle_core_skills.py D:\\HAMZA\\Documents\\p6-mcp-shop            # entries flagged core
    python tools/bundle_core_skills.py <catalog> --names list,lookahead-brief       # an explicit set

The core set is the index.json entries flagged "core": true (or --names). Each skills/<name>/p6mcp.json is
copied byte for byte (the bytes GitHub serves: the checkout must match the index sha256, so a CRLF checkout
or a file edited after the build is refused) after the checks of this app (shop.check_shared). core.json
records the catalog, its commit and each skill's version and sha256 (Shop page updates). No timestamp: an
unchanged catalog gives unchanged files. Copies of skills no longer in the set are removed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from p6mcp import core_skills, shop  # noqa: E402


def _commit(catalog: str):
    try:
        out = subprocess.run(["git", "-C", catalog, "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        return out.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def build(catalog: str, names=None, dest: str = core_skills.BUNDLE_DIR) -> dict:
    """Copy the core skills of the catalog checkout into dest; returns the core.json written."""
    with open(os.path.join(catalog, shop.INDEX), encoding="utf-8") as fh:
        index = json.load(fh)
    entries = {e["name"]: e for e in index.get("skills") or [] if isinstance(e, dict) and "name" in e}
    names = list(names) if names else [n for n, e in entries.items() if e.get("core")]
    if not names:
        raise SystemExit("no core skill: the index flags none (use --names)")
    rows, files, errs = [], {}, []
    for name in names:
        e = entries.get(name)
        if e is None or e.get("builtin"):
            errs.append(f"{name}: not an installable skill of the index")
            continue
        path = os.path.join(catalog, "skills", name, shop.META)
        with open(path, "rb") as fh:
            raw = fh.read()
        sha = hashlib.sha256(raw).hexdigest()
        if sha != e.get("sha256"):
            errs.append(f"{name}: {path} does not match the index sha256 (checkout with LF line endings, built)")
            continue
        item = json.loads(raw.decode("utf-8"))
        bad = shop.check_shared(item) + ([f"holds {item.get('name')!r}"] if item.get("name") != name else [])
        if bad:
            errs.append(f"{name}: " + "; ".join(bad))
            continue
        files[name] = raw
        rows.append({"name": name, "version": int(item.get("version") or 1), "sha256": sha})
    if errs:
        raise SystemExit("\n".join(errs))
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(dest):
        if f.endswith(".json") and f != core_skills.BUNDLE_INDEX and f[:-5] not in files:
            os.remove(os.path.join(dest, f))
    for name, raw in files.items():
        with open(os.path.join(dest, name + ".json"), "wb") as fh:
            fh.write(raw)
    head = {"schema": core_skills.BUNDLE_SCHEMA, "catalog": shop.REPO, "commit": _commit(catalog), "skills": rows}
    with open(os.path.join(dest, core_skills.BUNDLE_INDEX), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(head, indent=2) + "\n")
    return head


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("catalog", help="folder of a p6-mcp-shop checkout")
    ap.add_argument("--names", help="comma separated skill names instead of the entries flagged core")
    a = ap.parse_args(argv)
    head = build(a.catalog, [n.strip() for n in a.names.split(",") if n.strip()] if a.names else None)
    print(f"{len(head['skills'])} core skills from {head['catalog']} {head['commit'] or ''}: "
          + ", ".join(f"{r['name']} v{r['version']}" for r in head["skills"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
