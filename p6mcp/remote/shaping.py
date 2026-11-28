"""What a remote result may carry.

- Local paths are redacted to their file name: a remote client has no use for the folder layout of this PC, and
  the result travels through the user's Cloudflare account and the AI vendor. A value that is a whole absolute
  path becomes its file name; a path inside a sentence (a plan line, a message) is replaced the same way, up to
  the first space.
- Size budget: a result must stay under MAX_CHARS characters of JSON (claude.ai documents about 150,000; M0
  measured 200,000 accepted by both clients, the budget keeps a margin). A list page past the budget is cut
  short at the last row that fits and continues at next_offset, with a note; any other result past it is refused
  with a message that says how to ask for less.
"""

from __future__ import annotations

import json
import ntpath
import re
from typing import Any, Optional

from p6mcp.messages import _t

MAX_CHARS = 100_000
_WHOLE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/])")
_INNER = re.compile(r"(?:(?<![\w])[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/])[^\s\"'<>|*?]*")
PAGE_NOTE = ("Page cut short at {n} rows to stay under the remote result budget ({limit} characters): continue with "
             "offset={next}, or ask for fewer fields.")
TOO_LARGE = ("The result is too large for a remote client ({size} characters, budget {limit}). Ask for less: a "
             "smaller limit, fields=, one project or a narrower filter.")


class TooLarge(ValueError):
    """A result that cannot be brought under the budget."""


def _name(path: str) -> str:
    return ntpath.basename(path.rstrip("\\/")) or path


def known_roots(folders: dict) -> list[tuple[str, str]]:
    """(folder, label) pairs, longest first: a folder inside a sentence becomes its label even when it has spaces
    ("C:\\Users\\John Smith\\...\\backups" -> "backups"); the user profile becomes "~"."""
    out = []
    for path, label in folders.items():
        if path and len(path) > 3:
            out.append((ntpath.normpath(path).rstrip("\\/"), label or _name(path)))
    return sorted(out, key=lambda p: -len(p[0]))


def _roots_sub(text: str, roots: list[tuple[str, str]]) -> str:
    low = text.lower()
    for path, label in roots:
        i = low.find(path.lower())
        while i >= 0:
            end = i + len(path)
            if end == len(text) or text[end] in "\\/ ,;:.'\")":
                text = text[:i] + label + text[end:]
                low = text.lower()
                i = low.find(path.lower(), i + len(label))
            else:
                i = low.find(path.lower(), end)
    return text


def redact(obj: Any, roots: Optional[list[tuple[str, str]]] = None) -> Any:
    """A copy of obj with every absolute local path reduced to its file name (known folders to their label)."""
    if isinstance(obj, str):
        if _WHOLE.match(obj):
            return _name(obj)
        if roots:
            obj = _roots_sub(obj, roots)
        return _INNER.sub(lambda m: _name(m.group(0)), obj) if ("\\" in obj or ":/" in obj) else obj
    if isinstance(obj, dict):
        return {k: redact(v, roots) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v, roots) for v in obj]
    return obj


def size(obj: Any) -> int:
    """Characters of the JSON a client receives (indented as the SDK writes it)."""
    return len(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _paged(obj: Any) -> bool:
    return (isinstance(obj, dict) and isinstance(obj.get("rows"), list) and "next_offset" in obj
            and isinstance(obj.get("offset", 0), int))


def fit(obj: Any, limit: int = MAX_CHARS) -> Any:
    """obj itself when it is under the budget, a shorter page of it, or TooLarge."""
    n = size(obj)
    if n <= limit:
        return obj
    if _paged(obj) and obj["rows"]:
        rows, offset = obj["rows"], obj.get("offset") or 0

        def page(k: int) -> dict:
            return dict(obj, rows=rows[:k], returned=k, next_offset=offset + k,
                        remote_note=_t(PAGE_NOTE, n=k, limit=limit, next=offset + k))

        lo, hi = 0, len(rows) - 1  # largest k in [1, len - 1] whose page fits (len rows did not)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if size(page(mid)) <= limit:
                lo = mid
            else:
                hi = mid - 1
        if lo >= 1:
            return page(lo)
    raise TooLarge(_t(TOO_LARGE, size=n, limit=limit))


def shape(obj: Any, limit: int = MAX_CHARS, roots: Optional[list[tuple[str, str]]] = None) -> Any:
    """Redacted, then fitted to the budget."""
    return fit(redact(obj, roots), limit)
