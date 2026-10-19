"""User interface strings: one JSON catalog per language in p6mcp/locales (en is the reference and
the fallback). A right-to-left language (Arabic...) only needs its catalog: is_rtl() tells the UI
to mirror its layout.

    tr("db.test")                      -> "Test connection"
    tr("db.projects", n=3)             -> "3 projects"
"""

from __future__ import annotations

import json
import locale
import os
import sys
from typing import Optional

LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
NAMES = {"en": "English", "fr": "Français", "ar": "العربية"}
RTL = {"ar", "he", "fa", "ur"}

_lang = "en"
_cat: dict[str, str] = {}
_ref: dict[str, str] = {}


def _load(code: str) -> dict[str, str]:
    path = os.path.join(LOCALE_DIR, f"{code}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, str)}


def available() -> list[str]:
    """Languages with a non-empty catalog, offered in the UI (en first)."""
    codes = sorted(f[:-5] for f in os.listdir(LOCALE_DIR) if f.endswith(".json"))
    codes = [c for c in codes if _load(c)]
    return ["en"] + [c for c in codes if c != "en"]


def system_language() -> str:
    """Two-letter code of the Windows display language (else the process locale), 'en' when unknown."""
    tag = None
    if sys.platform == "win32":
        try:
            import ctypes
            tag = locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage())
        except (OSError, AttributeError):
            tag = None
    if not tag:
        tag = (locale.getlocale()[0] or "en")
    return tag.split("_")[0].split("-")[0].lower() or "en"


def resolve(lang: Optional[str]) -> str:
    code = (lang or "auto").lower()
    if code == "auto":
        code = system_language()
    return code if os.path.isfile(os.path.join(LOCALE_DIR, f"{code}.json")) else "en"


def set_language(lang: Optional[str]) -> str:
    global _lang, _cat, _ref
    _lang = resolve(lang)
    _ref = _load("en")
    _cat = dict(_ref, **_load(_lang)) if _lang != "en" else _ref
    return _lang


def language() -> str:
    return _lang


def is_rtl(code: Optional[str] = None) -> bool:
    return (code or _lang) in RTL


def tr(key: str, **kw) -> str:
    if not _ref:
        set_language("en")
    text = _cat.get(key) or _ref.get(key) or key
    return text.format(**kw) if kw else text
