# ruff: noqa: E501  (HTML templates)
"""The landing site (site/) generated from site-src/pages: one shared shell (top bar, sidebar, "On this page",
footer, search and image dialogs) around the content of each page.

    python tools/build_site.py           # write the pages and site/assets/search.json
    python tools/build_site.py --check   # exit 1 when site/ differs from what the sources give (tests/test_site.py)

A page source is HTML: a JSON front matter in an HTML comment, optional head extras (Open Graph, JSON-LD), then
the content. Icons are written [[i:name]] (or [[i:name|class]]), the brand mark [[mark]] or [[mark-s]], the check
mark [[tick]]. The home page also takes [[toolmap]] (the README tool tables), [[skills]] (the bundled core skills)
and [[gallery]] (the Settings captures). The images in site/assets/img are captures of the app and of its output on
the synthetic demo project; this script does not make them. site.css, site.js, theme.js and search.json are loaded
with ?v=<content hash>, so a new version reaches visitors at once.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "site")
PAGES = os.path.join(ROOT, "site-src", "pages")
CORE = os.path.join(ROOT, "p6mcp", "data", "core_skills")
REL = "https://github.com/nizarhamza/p6-mcp/releases/latest"
GH = "https://github.com/nizarhamza/p6-mcp"
FORBIDDEN = ("—", "–", " -- ")  # writing rule of the site: no em dash, en dash or double hyphen


def read_text(path):
    with open(path, encoding="utf-8") as f:  # universal newlines: CRLF checkouts read as LF
        return f.read()


def ver(rel):
    """Short content hash of a site asset (line endings ignored), for ?v= cache busting."""
    with open(os.path.join(SITE, rel), "rb") as f:
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()[:10]


# ---------------------------------------------------------------- icons (Lucide style, 24 x 24, stroked)
ICONS = {
    "search": '<circle cx="11" cy="11" r="7.5"/><path d="m20.5 20.5-4.2-4.2"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
    "moon": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "download": '<path d="M12 4v11"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><path d="M5 20h14"/>',
    "arrow-right": '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
    "external": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    "copy": '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "list": '<path d="M3 6h18M3 12h12M3 18h16"/>',
    "play": '<path d="M7 4.5v15l12-7.5z"/>',
    "pause": '<path d="M8 5v14M16 5v14"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "bot": '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 8V4M9 4h6"/><path d="M9 13v2M15 13v2"/>',
    "database": '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    "file": '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/><path d="M8 13h8M8 17h5"/>',
    "file-code": '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/><path d="m10 13-2 2 2 2M14 13l2 2-2 2"/>',
    "shield": '<path d="M12 22s8-3.5 8-10V5l-8-3-8 3v7c0 6.5 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
    "cpu": '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
    "activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "git-branch": '<path d="M6 3v12"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
    "package": '<path d="m7.5 4.27 9 5.15"/><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
    "globe": '<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
    "terminal": '<path d="m4 17 6-6-6-6"/><path d="M12 19h8"/>',
    "code": '<path d="m16 18 6-6-6-6"/><path d="m8 6-6 6 6 6"/>',
    "folder": '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/>',
    "sparkles": '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 3v4M17 5h4M5 17v4M3 19h4"/>',
    "book": '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/>',
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    "git-compare": '<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M13 6h3a2 2 0 0 1 2 2v7"/><path d="M11 18H8a2 2 0 0 1-2-2V9"/>',
    "key": '<circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6M15.5 7.5l3 3L22 7l-3-3"/>',
    "archive": '<rect x="2" y="3" width="20" height="5" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>',
    "history": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
    "refresh": '<path d="M21 12a9 9 0 0 1-15.5 6.3L3 16"/><path d="M3 21v-5h5"/><path d="M3 12a9 9 0 0 1 15.5-6.3L21 8"/><path d="M21 3v5h-5"/>',
    "store": '<path d="M3 9l1.5-5h15L21 9"/><path d="M3 9h18v1.5a3 3 0 0 1-6 0 3 3 0 0 1-6 0 3 3 0 0 1-6 0z"/><path d="M5 13.5V21h14v-7.5"/>',
    "check-circle": '<circle cx="12" cy="12" r="10"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
    "x-circle": '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6M9 9l6 6"/>',
    "message": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "layers": '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
    "route": '<circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"/><circle cx="18" cy="5" r="3"/>',
    "dice": '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.1"/><circle cx="15.5" cy="8.5" r="1.1"/><circle cx="12" cy="12" r="1.1"/><circle cx="8.5" cy="15.5" r="1.1"/><circle cx="15.5" cy="15.5" r="1.1"/>',
    "pencil": '<path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>',
    "power": '<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/>',
    "eye": '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "share": '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.6 13.5 6.8 4M15.4 6.5l-6.8 4"/>',
    "github": '<path d="M15 22v-4a4.8 4.8 0 0 0-1-3.5c3 0 6-2 6-5.5.08-1.25-.27-2.48-1-3.5.28-1.15.28-2.35 0-3.5 0 0-1 0-3 1.5-2.64-.5-5.36-.5-8 0C6 2 5 2 5 2c-.3 1.15-.3 2.35 0 3.5A5.4 5.4 0 0 0 4 9c0 3.5 3 5.5 6 5.5-.39.49-.68 1.05-.85 1.65-.17.6-.22 1.23-.15 1.85v4"/><path d="M9 18c-4.51 2-5-2-7-2"/>',
    "monitor": '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
    "clipboard-check": '<rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="m9 14 2 2 4-4"/>',
    "zap": '<path d="M13 2 3 14h9l-1 8 10-12h-9z"/>',
    "play-circle": '<circle cx="12" cy="12" r="10"/><path d="m10 8 6 4-6 4z"/>',
    "badge-check": '<path d="M3.85 8.62a4 4 0 0 1 4.78-4.77 4 4 0 0 1 6.74 0 4 4 0 0 1 4.78 4.78 4 4 0 0 1 0 6.74 4 4 0 0 1-4.77 4.78 4 4 0 0 1-6.75 0 4 4 0 0 1-4.78-4.77 4 4 0 0 1 0-6.76Z"/><path d="m9 12 2 2 4-4"/>',
    "test-tube": '<path d="M14.5 2v17.5c0 1.4-1.1 2.5-2.5 2.5s-2.5-1.1-2.5-2.5V2"/><path d="M8.5 2h7M14.5 16h-5"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
    "settings": '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    "sliders": '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    "plug": '<path d="M12 22v-5"/><path d="M9 8V2M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8z"/>',
}
GROUP_ICONS = {
    "Projects and structure": "layers", "Activities and relationships": "share", "Critical path and float": "route",
    "Schedule quality": "clipboard-check", "Progress and reporting": "activity",
    "Comparison, what-if and risk": "git-compare", "XER files": "file", "Edits (off by default)": "pencil",
    "Backups, log and diagnostics": "archive", "Skill shop": "store",
}


def icon(name, cls=""):
    if name not in ICONS:
        raise KeyError(f"unknown icon {name!r}")
    c = "icon" + (" " + cls if cls else "")
    return f'<svg class="{c}" aria-hidden="true"><use href="#i-{name}"/></svg>'


def sprite():
    syms = "".join(f'<symbol id="i-{k}" viewBox="0 0 24 24">{v}</symbol>' for k, v in ICONS.items())
    return f'<svg class="sprite" aria-hidden="true" focusable="false">{syms}</svg>'


MARK_G = ('<g transform="translate(6 0)"><rect class="m-body" x="4" y="56" width="16" height="8" rx="4"/>'
          '<path class="m-body" d="M36 28H48Q52 28 52 32V88Q52 92 48 92H36A20 20 0 0 1 16 72V48A20 20 0 0 1 36 28Z"/>'
          '<rect class="m-bar" x="58" y="42" width="46" height="12" rx="6"/><rect class="m-bar" x="58" y="66" width="26" height="12" rx="6"/>'
          '<path class="m-bar" d="M96 64L104 72L96 80L88 72Z"/></g>')


def mark(cls="brand-mark"):
    return f'<svg class="{cls}" viewBox="0 0 120 120" aria-hidden="true">{MARK_G}</svg>'


TICK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>'


def macros(s):
    s = s.replace("[[mark]]", mark()).replace("[[mark-s]]", mark("mark-i")).replace("[[tick]]", TICK)
    return re.sub(r"\[\[i:([a-z-]+)(?:\|([a-z- ]+))?\]\]", lambda m: icon(m.group(1), m.group(2) or ""), s)


# ---------------------------------------------------------------- data: tools (README) and core skills (bundle)
def read_tools():
    block = read_text(os.path.join(ROOT, "README.md")).split("<!-- tools:start -->")[1].split("<!-- tools:end -->")[0]
    groups, cur = [], None
    for line in block.splitlines():
        m = re.match(r"^\*\*(.+)\*\*$", line.strip())
        if m:
            cur = {"name": m.group(1), "tools": []}
            groups.append(cur)
            continue
        m = re.match(r"^\| `(p6_[a-z_]+)` \| (.+?) \| (.+?) \| (yes)? ?\|$", line.strip())
        if m and cur is not None:
            acc_label = m.group(3).strip()
            acc = {"Read": "read", "Write": "write", "Delete or replace": "write", "Writes a file": "file",
                   "App settings": "settings"}[acc_label]
            cur["tools"].append({"name": m.group(1), "desc": m.group(2).strip(), "acc": acc, "acc_label": acc_label,
                                 "basic": bool(m.group(4))})
    return groups


# Short descriptions for the site, in the order of the catalog's core set
SKILL_TEXT = {
    "weekly-update-review": "Progress, finish, critical path, slippage, changes since the last update, look-ahead and actions.",
    "schedule-quality-audit": "DCMA 14 score and grade, engine check, trend, dangling ends, redundant links; fixes proposed only.",
    "update-import-safely": "Inspect an update XER, dry run the import, report what it would change, apply on your yes.",
    "delay-whatif": "Delay one activity in memory: what drives it, then the new finish, milestones and critical counts.",
    "baseline-variance": "Finish variance, slip causes, the activities that moved most, logic changes and earned value.",
    "export-dashboard": "The Excel dashboard, then a short summary of its cards, failed DCMA checks and warnings.",
    "list": "The projects of the database grouped by EPS, with status, data date and finish.",
    "lookahead-brief": "What runs, starts and finishes in the next weeks, critical work first, week by week.",
    "progress-earned-value": "Percent complete by every method, earned value and SPI, the S-curve against the baseline.",
    "float-near-critical": "Float statistics and ranges, the float paths of the last F9, near-critical activities.",
    "milestone-tracker": "Milestones against the baseline, the most late ones and what drives the first of them.",
    "resource-wbs-summary": "Units and costs by resource, then activities and units by WBS and by status.",
    "update-integrity-audit": "Changes in an update that a reviewer should question, against the previous update.",
    "schedule-risk": "Monte Carlo on the CPM engine: P50 and P80 finish, chance to finish on time, criticality.",
    "logic-date-integrity": "Do the stored dates follow from the logic? The CPM engine against the last F9.",
}


def read_skills():
    core = json.loads(read_text(os.path.join(CORE, "core.json")))
    names = [s["name"] for s in core["skills"]]
    if sorted(names) != sorted(SKILL_TEXT):
        raise ValueError(f"the core skills changed: give each one a short text in SKILL_TEXT ({sorted(names)})")
    out = []
    for name in SKILL_TEXT:
        sk = json.loads(read_text(os.path.join(CORE, name + ".json")))
        spec = next(json.loads(f["text"]) for f in sk["files"] if f["path"].endswith("spec.json"))
        chain = []
        for st in spec.get("steps", []):
            if st.get("tool") and st["tool"] not in chain:
                chain.append(st["tool"])
        out.append({"name": name, "title": sk["title"], "text": SKILL_TEXT[name], "chain": chain, "writes": bool(sk["writes"])})
    return out


def toolmap_html(tools):
    c = {"read": 0, "write": 0, "file": 0, "settings": 0, "basic": 0}
    for g in tools:
        for t in g["tools"]:
            c[t["acc"]] += 1
            c["basic"] += t["basic"]
    n_all = sum(len(g["tools"]) for g in tools)
    seg = [("all", "All", n_all), ("read", "Read only", c["read"]), ("basic", "Basic profile", c["basic"]),
           ("write", "Write", c["write"]), ("file", "Writes a file", c["file"]), ("settings", "App settings", c["settings"])]
    out = ['<div class="toolmap" data-toolmap>', '<div class="tm-bar">',
           '<div class="seg" role="group" aria-label="Filter the tools">']
    for key, label, n in seg:
        out.append(f'<button type="button" data-filter="{key}" aria-pressed="{"true" if key == "all" else "false"}">{label} <span class="n">{n}</span></button>')
    out.append(f'</div><p class="tm-count" aria-live="polite" aria-atomic="true"><b>{n_all}</b> tools shown</p></div>')
    out.append('<div class="tm-grid reveal-chips" data-anim>')
    k = 0
    for g in tools:
        out.append(f'<div class="tm-cat"><h3>{icon(GROUP_ICONS[g["name"]])}{html.escape(g["name"])}<span class="n">{len(g["tools"])}</span></h3><ul class="tm-tools">')
        for t in g["tools"]:
            basic = " data-basic" if t["basic"] else ""
            hidden = ", " + t["acc_label"].lower() + (", Basic profile" if t["basic"] else "")
            out.append(f'<li><button type="button" class="tm-tool" id="t-{t["name"]}" data-name="{t["name"]}" data-access="{t["acc"]}" data-acc="{t["acc_label"]}"{basic} '
                       f'data-desc="{html.escape(t["desc"])}" aria-pressed="false" style="--k:{k}">{t["name"]}<span class="visually-hidden">{hidden}</span></button></li>')
            k += 1
        out.append("</ul></div>")
    out.append("</div>")
    out.append('<div class="tm-detail" aria-live="polite"><p class="hint">Select a tool to see what it does, what it may change and whether the Basic profile offers it.</p></div>')
    out.append('<ul class="tm-legend" aria-label="Legend"><li><i></i>Read only</li><li><i class="w"></i>Write (off by default)</li>'
               '<li><i class="f"></i>Writes a file</li><li><i class="s"></i>App settings</li><li><i class="b">B</i>In the Basic profile</li></ul>')
    out.append(f'<p class="tm-more">The <a href="{GH}#tools">tool tables of the README</a> list the same tools, generated from the code.</p>')
    out.append("</div>")
    return "\n".join(out)


def skills_html(skills):
    out = ['<ul class="skills-grid">']
    for i, s in enumerate(skills):
        badge = '<span class="badge-w">Uses write tools</span>' if s["writes"] else ""
        chain = '<span class="visually-hidden">Tools it calls: </span>' + '<span class="visually-hidden">, </span>'.join(f'<span class="t">{t}</span>' for t in s["chain"])
        out.append(f'<li class="skill" data-reveal style="--i:{i % 3}"><h4>{icon("sparkles")}{html.escape(s["title"])}</h4>'
                   f'<p>{html.escape(s["text"])}</p>{badge}<p class="chain">{chain}</p></li>')
    out.append("</ul>")
    return "\n".join(out)


# Settings window captures: site/assets/img/settings-<key>[-dark]-<1180|2360>.webp
GALLERY = [
    ("database", "Database", "database", "Pick the P6 database (suggestions come from P6's own connection list). Test connection reads it read only: P6 version, schema, projects. Or switch to XER files only."),
    ("clients", "MCP clients", "plug", "Finds Claude Desktop, Claude Code, Cursor, VS Code, LM Studio and Cline, and adds, repairs or removes only the p6mcp entry, after a backup of the file. Install skills writes the P6 skills to the client's skills folder."),
    ("tools", "Tools and safety", "shield", "Basic (11 read-only tools) or Full (58). Writes stay off until you allow them; a backup before every write, writes refused while P6 runs, the skill shop on or off."),
    ("instructions", "Instructions", "message", "What every MCP client receives when it connects. Turn built-in lines off or add your own rules, such as how to write activities."),
    ("skills", "Skills", "sparkles", "The installed skills, whether the current tool settings offer them and where they come from. Edit one, or write your own."),
    ("shop", "Skill shop", "store", "The public catalog on GitHub: read a skill in full before installing it. Share yours from the Skills page."),
]


def gallery_html():
    tabs, panels = [], []
    for i, (key, title, ic, cap) in enumerate(GALLERY, 1):
        sel = i == 1
        tabindex = "" if sel else ' tabindex="-1"'
        tabs.append(f'<button class="tab" type="button" role="tab" id="st-t{i}" aria-controls="st-p{i}" aria-selected="{"true" if sel else "false"}"{tabindex}>{icon(ic)}{title}</button>')
        alt = f"Settings window, {title} page"
        imgs = "".join(
            f'<img class="only-{mode}" src="/assets/img/settings-{key}{sfx}-1180.webp" srcset="/assets/img/settings-{key}{sfx}-1180.webp 1180w, /assets/img/settings-{key}{sfx}-2360.webp 2360w" '
            f'sizes="(min-width: 64rem) 48rem, 100vw" width="1180" height="900" alt="{alt}{", dark mode" if sfx else ""}" loading="lazy" decoding="async">'
            for mode, sfx in (("light", ""), ("dark", "-dark")))
        panels.append(
            f'<div class="tab-panel" role="tabpanel" id="st-p{i}" aria-labelledby="st-t{i}"{"" if sel else " hidden"}><figure>'
            f'<div class="window"><div class="window-bar">{mark("app-ico")}<span class="ttl">MCP for Primavera P6 Settings</span><span class="win-btns" aria-hidden="true"><i class="min"></i><i class="max"></i><i class="cls"></i></span></div>'
            f'<button class="zoom shot" type="button" data-zoom="/assets/img/settings-{key}-2360.webp" data-zoom-dark="/assets/img/settings-{key}-dark-2360.webp">{imgs}<span class="visually-hidden">, open at full size</span></button></div>'
            f'<figcaption><strong>{title}.</strong> {html.escape(cap)}</figcaption></figure></div>')
    return ('<div class="gallery" data-tabs data-reveal><div class="tabs-list" role="tablist" aria-label="Settings pages">' + "".join(tabs)
            + '</div><div class="tab-panels">' + "".join(panels) + "</div></div>")


# ---------------------------------------------------------------- shell
SIDEBAR = [
    ("Get started", [("Introduction", "#top", None), ("What it answers", "#answers", None), ("Install", "#install", None)]),
    ("How it works", [("Anatomy of a call", "#architecture", None), ("Tools", "#tools", None), ("Skills", "#skills", None),
                      ("Safe writes", "#writes", None), ("Repository", "#repository", None)]),
    ("See it", [("Reports", "#reports", None), ("Settings app", "#app", None)]),
    ("Guides", [("All guides", "/guides/", None), ("DCMA 14-point check", "/guides/dcma-14-point-check-ai/", None),
                ("Driving path", "/guides/driving-path-p6-claude/", None), ("Comparing two updates", "/guides/compare-p6-updates-ai/", None),
                ("XER before submission", "/guides/review-xer-before-submission/", None)]),
    ("Help", [("Compared with XER servers", "#compare", None), ("FAQ", "#faq", None),
              ("README on GitHub", GH + "#readme", "ext"), ("Skill shop catalog", "https://github.com/nizarhamza/p6-mcp-shop", "ext"),
              ("Releases", "https://github.com/nizarhamza/p6-mcp/releases", "ext")]),
]
# Guide pages in sidebar order (also the order of the search index)
GUIDES = [href for _label, href, _kind in SIDEBAR[3][1] if href != "/guides/"]


def sidebar(page_url):
    home = page_url == "/"
    out = ['<nav class="sidebar" id="sidebar" aria-label="Documentation">']
    for title, items in SIDEBAR:
        out.append(f'<div class="side-group"><p class="side-title">{title}</p><ul class="side-list">')
        for label, href, kind in items:
            if href.startswith("#") and not home:
                href = "/" + href
            cur = ' aria-current="page"' if href == page_url else ""
            spy = ' data-spy' if home and href.startswith("#") else ""
            ext = icon("external", "ext") if kind == "ext" else ""
            out.append(f'<li><a href="{href}"{cur}{spy}>{label}{ext}</a></li>')
        out.append("</ul></div>")
    out.append("</nav>")
    return "".join(out)


def topbar(page_url):
    guides_cur = ' aria-current="page"' if page_url == "/guides/" else (' class="is-section"' if page_url.startswith("/guides/") else "")
    return f'''<a class="skip-link" href="#main">Skip to content</a>
<header class="topbar">
  <div class="topbar-inner">
    <button class="icon-btn menu-btn" type="button" aria-expanded="false" aria-controls="sidebar" aria-label="Open the navigation">{icon("menu")}</button>
    <a class="brand" href="/" aria-label="MCP for Primavera P6, home">{mark()}<span class="brand-word"><span class="brand-name">MCP</span><span class="brand-tag">for Primavera P6</span></span></a>
    <button class="topbar-search" type="button" data-search-open aria-haspopup="dialog">{icon("search")}<span>Search the docs</span><kbd data-kbd>Ctrl K</kbd></button>
    <button class="icon-btn search-btn-s" type="button" data-search-open aria-haspopup="dialog" aria-label="Search the docs">{icon("search")}</button>
    <nav class="topnav" aria-label="Main">
      <a href="/guides/" aria-label="Guides"{guides_cur}>{icon("book")}<span class="nav-text">Guides</span></a>
      <a href="{GH}" aria-label="GitHub">{icon("github")}<span class="nav-text">GitHub</span></a>
      <button class="icon-btn theme-toggle" type="button" data-theme-toggle aria-label="Switch to the dark theme">{icon("moon", "i-moon")}{icon("sun", "i-sun")}</button>
      <a class="btn btn-primary btn-s btn-download" href="{REL}">{icon("download")}Download</a>
    </nav>
  </div>
</header>'''


def toc(items, cta):
    lis = "".join(f'<li><a href="#{i}">{html.escape(t)}</a></li>' for i, t in items)
    return (f'<nav class="toc" aria-labelledby="toc-h"><p class="toc-title" id="toc-h">{icon("list")}On this page</p>'
            f'<ol>{lis}</ol>{cta}</nav>')


CTA = {
    "home": (f'<div class="toc-cta"><strong>Free Windows installer</strong><p>Registers the server with the MCP clients it finds.</p>'
             f'<a class="btn btn-primary btn-s" href="{REL}">{icon("download")}Download 2.0.0</a></div>'),
    "guide": (f'<div class="toc-cta"><strong>Try it on your own schedule</strong><p>Free Windows installer. Works on your P6 database or on an XER file.</p>'
              f'<a class="btn btn-primary btn-s" href="{REL}">{icon("download")}Download 2.0.0</a></div>'),
}

FOOTER = f'''<footer class="site-foot">
  <div class="foot-inner">
    <div class="foot-grid">
      <div class="foot-brand">
        <a class="brand" href="/" aria-label="MCP for Primavera P6, home">{mark()}<span class="brand-word"><span class="brand-name">MCP</span><span class="brand-tag">for Primavera P6</span></span></a>
        <p>Local MCP server for Primavera P6 Professional schedules. Free and open source under the Apache License 2.0.</p>
      </div>
      <nav aria-label="Documentation, footer"><p class="foot-title">Docs</p><ul>
        <li><a href="/">Introduction</a></li><li><a href="/#tools">Tools</a></li><li><a href="/#skills">Skills</a></li>
        <li><a href="/#install">Install</a></li><li><a href="/#faq">FAQ</a></li></ul></nav>
      <nav aria-label="Guides, footer"><p class="foot-title">Guides</p><ul>
        <li><a href="/guides/dcma-14-point-check-ai/">DCMA 14-point check with AI</a></li>
        <li><a href="/guides/driving-path-p6-claude/">Driving path in P6 with Claude</a></li>
        <li><a href="/guides/compare-p6-updates-ai/">Comparing two P6 updates</a></li>
        <li><a href="/guides/review-xer-before-submission/">Reviewing an XER before submission</a></li></ul></nav>
      <nav aria-label="Project, footer"><p class="foot-title">Project</p><ul>
        <li><a href="{REL}">Download</a></li><li><a href="{GH}">Source and documentation</a></li>
        <li><a href="https://github.com/nizarhamza/p6-mcp-shop">Skill shop catalog</a></li>
        <li><a href="{GH}/blob/main/SECURITY.md">Security</a></li><li><a href="{GH}/issues">Report an issue</a></li>
        <li><a href="/llms.txt">llms.txt</a></li></ul></nav>
    </div>
    <p class="legal">MCP for Primavera P6 is an independent project, not affiliated with, endorsed or sponsored by Oracle. Oracle, Primavera and P6 are trademarks of Oracle and/or its affiliates. The demo project shown on this site is synthetic.</p>
  </div>
</footer>'''

DIALOGS = f'''<div class="scrim" data-scrim></div>
<div class="visually-hidden" role="status" aria-live="polite" id="sr-status"></div>
<dialog class="search-dlg" id="search-dlg" aria-label="Search the docs" data-index="/assets/search.json?v=__SEARCH_V__">
  <div class="search-head">{icon("search")}<input type="text" id="search-q" placeholder="Search pages, tools and skills" autocomplete="off" spellcheck="false" role="combobox" aria-expanded="false" aria-controls="search-res" aria-autocomplete="list" aria-label="Search"><button class="icon-btn" type="button" data-search-close aria-label="Close the search">{icon("x")}</button></div>
  <p class="search-empty" id="search-msg" hidden></p>
  <ul class="search-res" id="search-res" role="listbox" aria-label="Results"></ul>
  <p class="visually-hidden" role="status" aria-live="polite" id="search-status"></p>
  <div class="search-foot"><span><kbd>Enter</kbd> open</span><span><kbd>&#8593;</kbd> <kbd>&#8595;</kbd> move</span><span><kbd>Esc</kbd> close</span></div>
</dialog>
<dialog class="lightbox" id="lightbox" aria-label="Image at full size">
  <button class="icon-btn lb-close" type="button" data-lb-close aria-label="Close the image">{icon("x")}</button>
  <img alt="" width="2360" height="1800">
  <p aria-hidden="true"></p>
</dialog>'''

NOSCRIPT = ('<noscript><style>@media (max-width: 64rem) { .sidebar { position: static !important; transform: none !important; '
            'visibility: visible !important; width: auto !important; height: auto !important; border-right: 0 !important; '
            'border-bottom: 1px solid var(--line) !important; } }</style></noscript>')


def head(meta, extra):
    canon = f'<link rel="canonical" href="{meta["canonical"]}">\n' if meta.get("canonical") else ""
    rob = f'<meta name="robots" content="{meta["robots"]}">\n' if meta.get("robots") else ""
    d = f'<meta name="description" content="{html.escape(meta["description"], quote=True)}">\n' if meta.get("description") else ""
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(meta["title"])}</title>
{d}{rob}{canon}<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="preload" href="/assets/fonts/ibm-plex-sans-var-latin.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/assets/site.css?v={ver("assets/site.css")}">
<script src="/assets/theme.js?v={ver("assets/theme.js")}"></script>
{NOSCRIPT}
{extra}</head>'''


def page(meta, extra, content, toc_items):
    url = meta["url"]
    toc_html = toc(toc_items, CTA[meta["cta"]] if meta.get("cta") else "") if toc_items else ""
    layout = "layout" if toc_items else "layout layout-wide"
    return (head(meta, extra) + "\n<body>\n" + sprite() + "\n" + topbar(url)
            + f'\n<div class="{layout}">\n' + sidebar(url)
            + f'\n<main class="content" id="main" tabindex="-1">\n<div class="doc">\n{content}\n</div>\n</main>\n'
            + toc_html + "\n</div>\n" + FOOTER + "\n" + DIALOGS
            + f'\n<script src="/assets/site.js?v={ver("assets/site.js")}" defer></script>\n</body>\n</html>\n')


# ---------------------------------------------------------------- page sources
def read_page(rel):
    """(front matter, head extras, content) of site-src/pages/<rel>."""
    text = read_text(os.path.join(PAGES, rel))
    m = re.match(r"<!--page\n(.*?)\n-->\n", text, re.S)
    if not m:
        raise ValueError(f"{rel}: no <!--page ... --> front matter")
    meta = json.loads(m.group(1))
    rest = text[m.end():]
    extra = ""
    h = re.match(r"<!--head-->\n(.*?)<!--/head-->\n", rest, re.S)
    if h:
        extra, rest = h.group(1), rest[h.end():]
    return meta, extra, rest.rstrip("\n")


def out_path(url):
    if url == "/404":
        return "404.html"
    return (url.strip("/") + "/index.html").lstrip("/")


def plain(s):
    return re.sub(r"<[^>]+>", "", s)


def first(pattern, text, what):
    m = re.search(pattern, text, re.S)
    if not m:
        raise ValueError(f"no {what}")
    return m.group(1)


def build():
    """Every generated file of site/ as {relative path: text}."""
    tools = read_tools()
    skills = read_skills()
    sources = []
    for dirpath, _dirs, files in os.walk(PAGES):
        for name in files:
            if name.endswith(".html"):
                sources.append(os.path.relpath(os.path.join(dirpath, name), PAGES).replace(os.sep, "/"))
    out, info, home_secs = {}, {}, []
    for rel in sorted(sources):
        meta, extra, content = read_page(rel)
        url = meta["url"]
        if url == "/":
            content = content.replace("[[toolmap]]", toolmap_html(tools)).replace("[[skills]]", skills_html(skills))
            content = content.replace("[[gallery]]", gallery_html())
        content = macros(content)
        left = re.findall(r"\[\[[^\]]+\]\]", content)
        if left:
            raise ValueError(f"{rel}: unknown placeholders {left}")
        if url == "/":
            # the visible FAQ and the FAQPage JSON-LD must ask the same questions, in the same order
            ld = json.loads(first(r'<script type="application/ld\+json">(.*?)</script>', extra, "JSON-LD on the home page"))
            faq = [q["name"] for g in ld["@graph"] if g["@type"] == "FAQPage" for q in g["mainEntity"]]
            vis = [html.unescape(q) for q in re.findall(r"<summary>(.*?)</summary>", content.split('<div class="faq">')[1])]
            if vis != faq:
                raise ValueError(f"visible FAQ {vis} differs from the FAQPage JSON-LD {faq}")
            home_secs = re.findall(r'<section class="doc-section" id="([a-z-]+)"[^>]*>\s*<h2[^>]*><a class="anchor"[^>]*>.*?</a>(.*?)</h2>', content, re.S)
            toc_items = [("top", "Introduction")] + [(i, html.unescape(plain(t))) for i, t in home_secs]
        else:
            toc_items = [tuple(x) for x in meta["toc"]] if meta.get("toc") else None
        out[out_path(url)] = page(meta, extra, content, toc_items)
        if url.startswith("/guides/"):
            h1 = first(r'<h1 class="page-title" id="page-title">(.*?)</h1>', content, f"h1 in {rel}")
            lede = first(r'<p class="lede">(.*?)</p>', content, f"lede in {rel}")
            info[url] = {"title": plain(h1), "lede": html.unescape(plain(lede)), "toc": toc_items or []}

    idx = [{"g": "Pages", "t": "Introduction", "d": "MCP for Primavera P6: what it is and how it works", "u": "/"},
           {"g": "Pages", "t": info["/guides/"]["title"], "d": info["/guides/"]["lede"], "u": "/guides/"}]
    for url in GUIDES:
        idx.append({"g": "Pages", "t": info[url]["title"], "d": info[url]["lede"], "u": url})
    for sid, t in home_secs:
        idx.append({"g": "Sections", "t": html.unescape(plain(t)), "d": "Introduction", "u": f"/#{sid}"})
    for url in GUIDES:
        for sid, t in info[url]["toc"]:
            idx.append({"g": "Sections", "t": t, "d": info[url]["title"], "u": f"{url}#{sid}"})
    for g in tools:
        for t in g["tools"]:
            idx.append({"g": "Tools", "t": t["name"], "d": f'{t["desc"]} ({t["acc_label"]}{", Basic profile" if t["basic"] else ""})', "u": f'/#t-{t["name"]}', "m": 1})
    for s in skills:
        idx.append({"g": "Skills", "t": f'{s["title"]} (p6-{s["name"]})', "d": s["text"], "u": "/#skills"})
    index_text = json.dumps(idx, ensure_ascii=False, separators=(",", ":"))
    search_v = hashlib.sha256(index_text.encode("utf-8")).hexdigest()[:10]
    for rel in list(out):
        out[rel] = out[rel].replace("__SEARCH_V__", search_v)
    out["assets/search.json"] = index_text
    for rel, text in out.items():
        bad = [c for c in FORBIDDEN if c in text]
        if bad:
            raise ValueError(f"{rel}: forbidden characters {bad!r}")
    return out


def stale(files=None):
    """Generated files whose content in site/ differs from the sources (line endings ignored)."""
    files = build() if files is None else files
    diff = []
    for rel, text in files.items():
        path = os.path.join(SITE, rel)
        if not os.path.exists(path) or read_text(path) != text:
            diff.append(rel)
    return diff


def main(argv):
    files = build()
    if "--check" in argv:
        diff = stale(files)
        for rel in diff:
            print(f"out of date: site/{rel}")
        return 1 if diff else 0
    for rel, text in files.items():
        path = os.path.join(SITE, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    print(f"{len(files)} files written to site/")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
