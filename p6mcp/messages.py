"""Tool messages in the language of the settings (English and French).

A text a person reads in a tool reply (an error or refusal, a dry-run plan line, the `next` instruction, a note,
caveat or warning, p6_diagnose's advice) is built with `_t(template, **values)`. It returns a `Msg`: a str whose
value is the English text, so everything that reads it in the server (edit log entries, plan digests of the
two-step flow, caches, tests) keeps the English text, and that remembers its template and values. The reply is
rendered in the language of the call at the edge (`localize` in `registry._gated`, the SDK wrapper and the remote
server): each Msg it holds, in the result or in the exception, becomes the template of the catalog
`locales/tools/<lang>.json` (keys = the English templates) filled with its values, themselves rendered.

    raise ValueError(_t("Activity {code} not found in {project}.", code=code, project=project))

Rules: a template is a str.format template (literal braces doubled), a literal (tests/test_tool_messages.py
extracts every template from the code and checks the catalogs); English is the reference and the fallback (a
template missing from a catalog, or one that does not format, gives the English text). Keys, field names, enum
and status codes, P6 names and the confirmation_text a user must type stay English (machine read).
"""

from __future__ import annotations

import functools
import json
import os
from typing import Any, Optional

from p6mcp import i18n

TOOL_LOCALE_DIR = os.path.join(i18n.LOCALE_DIR, "tools")
ENV_LANGUAGE = "P6MCP_TOOL_LANGUAGE"  # overrides the setting (tests pin "en")


class Msg(str):
    """English text of a tool message that keeps its template and values for rendering in another language."""

    def __new__(cls, template: str, values: Optional[dict] = None):
        values = dict(values or {})
        try:
            text = template.format(**values)
        except (KeyError, IndexError, ValueError):  # a broken template never hides the message
            text = template
        self = super().__new__(cls, text)
        self.template = template
        self.values = values
        return self

    def __str__(self) -> str:  # str(msg) and str(ValueError(msg)) keep the Msg (and so its translation)
        return self

    def __reduce__(self):  # worker processes and copies get a Msg again
        return (Msg, (self.template, self.values))

    def render(self, lang: str) -> str:
        """The message in language lang (English when the catalog has no usable entry)."""
        values = {k: render(v, lang) for k, v in self.values.items()}
        text = catalog(lang).get(self.template) if lang != "en" else None
        if text:
            try:
                return text.format(**values)
            except (KeyError, IndexError, ValueError):
                pass
        try:
            return self.template.format(**values)
        except (KeyError, IndexError, ValueError):
            return str.__str__(self)


def _t(template: str, /, **values: Any) -> Msg:
    """A tool message: English now, rendered in the user's language in the reply (see the module docstring)."""
    return Msg(template, values)


class _Joined(Msg):
    """Messages (or plain strings) joined by a separator, each rendered on its own (see _join)."""

    def __new__(cls, sep: str, parts):
        parts = tuple(parts)
        self = str.__new__(cls, sep.join(str.__str__(p) if isinstance(p, str) else str(p) for p in parts))
        self.template, self.values, self.sep, self.parts = "", {}, sep, parts
        return self

    def __reduce__(self):
        return (_Joined, (self.sep, self.parts))

    def render(self, lang: str) -> str:
        return self.sep.join(str(render(p, lang)) for p in self.parts)


def _join(sep: str, parts) -> Msg:
    """sep.join(parts) that keeps the translation of each part (a plain join gives English text)."""
    return _Joined(sep, parts)


@functools.lru_cache(maxsize=None)
def catalog(lang: str) -> dict:
    """English template -> template in lang ({} for English or a language without a tool catalog)."""
    path = os.path.join(TOOL_LOCALE_DIR, f"{lang}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, str) and v}


@functools.lru_cache(maxsize=1)
def _system_language() -> str:
    return i18n.system_language()


def resolve(setting: Optional[str]) -> str:
    """'en' or a language with a tool catalog, from the Language setting ('auto' = the Windows display language)."""
    code = (setting or "auto").strip().lower() or "auto"
    if code == "auto":
        code = _system_language()
    return code if code != "en" and catalog(code) else "en"


def language() -> str:
    """Language of the tool messages of this call: P6MCP_TOOL_LANGUAGE, else the Language setting (config
    `language`, read again on every call with the other settings, so a change in Settings applies at once)."""
    env = os.environ.get(ENV_LANGUAGE)
    if env and env.strip():
        return resolve(env)
    from p6mcp.core import state as _state  # call time: state imports the layers that import this module
    raw = getattr(_state.SETTINGS, "raw", None) or {}
    return resolve(raw.get("language") if isinstance(raw, dict) else None)


def render(value: Any, lang: str) -> Any:
    """A Msg rendered in lang; any other value as it is."""
    return value.render(lang) if isinstance(value, Msg) else value


def localize(obj: Any, lang: Optional[str] = None) -> Any:
    """A tool result with every Msg it holds (in dicts, lists and tuples) rendered in lang (default: language()).
    English keeps the result as it is (a Msg is its English text)."""
    lang = lang or language()
    if lang == "en":
        return obj
    return _walk(obj, lang)


def _walk(obj: Any, lang: str) -> Any:
    if isinstance(obj, Msg):
        return obj.render(lang)
    if isinstance(obj, dict):
        return {k: _walk(v, lang) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, lang) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_walk(v, lang) for v in obj)
    return obj


def localize_error(exc: BaseException, lang: Optional[str] = None) -> BaseException:
    """The exception with its Msg arguments rendered in lang (in place, so its type and traceback stay)."""
    if exc.args and any(isinstance(a, Msg) for a in exc.args):
        lang = lang or language()
        if lang != "en":
            exc.args = tuple(render(a, lang) for a in exc.args)
    return exc
