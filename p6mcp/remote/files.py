"""File handoff of remote access: one-time download links for exports, a one-time upload page for an XER.

A remote client (claude.ai, ChatGPT) cannot name a folder of this PC, and the user's browser cannot reach one. With
the remote switch remote_allow_files (off by default):

- Downloads: p6_export_xer, p6_export_dashboard and p6_export_lookahead, called remotely, write into a private folder
  of the connection (remote\\files\\<grant digest>\\<file id>\\<name> under %LOCALAPPDATA%\\P6MCP) and return
  download_url = https://<public host>/files/<token>. GET shows a page with a Download button (no side effect: a
  link preview or a prefetch never spends the link); POST sends the file once (attachment, no caching), then the
  file and its record are deleted.
- Uploads: p6_remote_upload_link returns upload_url = https://<public host>/upload/<token> and an upload_id. The page
  takes one .xer (UPLOAD_MAX), streamed to the connection's folder, checked as an XER (_xer_read_meta) on arrival;
  then p6_inspect_xer and p6_import_xer take upload_id instead of a path (the import is still a write: remote edits
  switch, scope p6:write, dry run and confirmation).

Links carry only a random token (TOKEN_BYTES, 256 bits). Tokens are stored as SHA-256 hashes in auth.db (table files,
the OAuth store), compared in constant time; a link works once and for LINK_TTL. An uploaded XER stays usable by its
upload_id for UPLOAD_KEEP, by the connection that asked for the link only. Revoking a connection deletes its rows
(Store.revoke); the sweep (at start, on access and every SWEEP_SECONDS) removes expired rows and every folder no
live row names. The audit log gets link minted / used / expired and upload received / refused events, never a token,
a file name or file contents.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import ntpath
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from p6mcp import config, i18n, policy
from p6mcp.remote import audit, public_url
from p6mcp.remote.store import Store, token_hash

log = logging.getLogger("p6mcp.remote.files")

LINK_TTL = 900  # a download or upload link works once, for 15 minutes
UPLOAD_KEEP = 3600  # an uploaded XER stays usable by its upload_id for an hour after it arrived
WRITING_TTL = 3600  # an export still being written
UPLOAD_MAX = 50 * 1024 * 1024  # bytes of one XER (a 3,120 activity project is 3.4 MB; Cloudflare takes 100 MB)
UPLOAD_BODY_CAP = UPLOAD_MAX + 1024 * 1024  # the request body of an upload (multipart envelope included)
MAX_PER_CONNECTION = 10  # files waiting at once for one connection (downloads not taken, uploads open or received)
SWEEP_SECONDS = 60
TOKEN_BYTES = 32  # 256-bit link tokens
UPLOAD_TOOL = "p6_remote_upload_link"
UPLOAD_HINTS = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
CONTENT_TYPES = {".xer": "application/octet-stream",
                 ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
DEFAULT_SUFFIX = {"p6_export_xer": "", "p6_export_dashboard": "_dashboard", "p6_export_lookahead": "_lookahead"}
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.I)
NOT_FOUND = ("upload_id {id!r} is unknown, expired or not uploaded yet. Ask the user to send the XER on the page of "
             "p6_remote_upload_link first (one page per file, 15 minutes), then call again.")
FILE_NAME_BAD = ("file_name is the name of the download only, without a folder: for example 'DEMO-A{ext}'. The file "
                 "is written in a private folder on the PC and handed over through a one-time download link.")
FULL = ("Too many files are waiting for this connection ({n}). Let the user download them, or wait 15 minutes, then "
        "try again.")
# table files of auth.db (p6mcp.remote.store.SCHEMA):
# kind download: writing (the export runs) -> ready (link live) -> sending (link spent, file on its way) -> deleted
# kind upload:   open (link live) -> receiving -> received (link spent, upload_id usable) -> deleted


def _now() -> int:
    return int(time.time())


def root() -> str:
    return os.path.join(config.localdata_dir(), "remote", "files")


def _grant_dir(grant: str) -> str:
    return hashlib.sha256(grant.encode("utf-8")).hexdigest()[:32]


def safe_name(name: Optional[str], ext: str, default: str) -> str:
    """A file name for this PC and for Content-Disposition: base name, no control or reserved characters, at most
    100 characters, ending with ext."""
    base = ntpath.basename(str(name or "").replace("/", "\\")).strip().strip(".")
    base = _BAD_CHARS.sub("_", base)[:100].strip() or default
    if _RESERVED.match(base):
        base = "_" + base
    if not base.lower().endswith(ext):
        base = base[:100 - len(ext)] + ext
    return base


def _size_text(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, round(n / 1024))} KB"


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event(what: str, **fields) -> None:
    audit.write("file", what=what, ip=audit.CLIENT_IP.get(), **fields)


class Handoff:
    """Rows of the files table (auth.db) and the folders they name."""

    def __init__(self, store: Store, folder: Optional[str] = None):
        self.store = store
        self.folder = folder or root()
        self._swept = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------- paths
    def _dir(self, row) -> str:
        return os.path.join(self.folder, _grant_dir(row["grant_id"]), row["id"])

    def _path(self, row) -> str:
        return os.path.join(self._dir(row), row["name"])

    def _remove(self, row) -> None:
        shutil.rmtree(self._dir(row), ignore_errors=True)

    def _row(self, con, **where):
        keys = list(where)
        return con.execute("SELECT * FROM files WHERE " + " AND ".join(f"{k} = ?" for k in keys),
                           [where[k] for k in keys]).fetchone()

    def _room(self, con, grant: str) -> None:
        n = con.execute("SELECT COUNT(*) FROM files WHERE grant_id = ? AND exp >= ?", (grant, _now())).fetchone()[0]
        if n >= MAX_PER_CONNECTION:
            raise ValueError(FULL.format(n=n))

    def _link(self, row, token: str) -> bool:
        return bool(row and row["link"]) and hmac.compare_digest(row["link"], token_hash(token))

    # ------------------------------------------------------------- downloads
    def new_download(self, grant: str, tool: str, file_name: Optional[str], project: Optional[str]) -> tuple[str, str]:
        """(file id, local path) of a new export of this connection; the tool writes the file there."""
        ext = policy.REMOTE_DOWNLOAD_TOOLS[tool]
        if file_name is not None and (not isinstance(file_name, str) or re.search(r"[\\/:]", file_name)
                                      or file_name.strip().strip(".") == ""):
            raise ValueError(FILE_NAME_BAD.format(ext=ext))
        default = safe_name(f"{project or 'export'}{DEFAULT_SUFFIX.get(tool, '')}", ext, "export" + ext)
        name = safe_name(file_name, ext, default)
        fid = secrets.token_urlsafe(12)
        self.sweep_soon()
        with closing(self.store._con()) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                self._room(con, grant)
                con.execute("INSERT INTO files (id, grant_id, kind, ext, name, state, created, exp) "
                            "VALUES (?, ?, 'download', ?, ?, 'writing', ?, ?)",
                            (fid, grant, ext, name, _now(), _now() + WRITING_TTL))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            row = self._row(con, id=fid)
        os.makedirs(self._dir(row), exist_ok=True)
        return fid, self._path(row)

    def discard(self, fid: str) -> None:
        """The export failed: its folder and row go."""
        with closing(self.store._con()) as con:
            row = self._row(con, id=fid)
            con.execute("DELETE FROM files WHERE id = ?", (fid,))
        if row is not None:
            self._remove(row)

    def publish(self, fid: str, grant: str, host: str) -> dict:
        """The one-time link of an export this connection wrote (another connection cannot mint it)."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        with closing(self.store._con()) as con:
            row = self._row(con, id=fid, kind="download")
            if row is None or row["grant_id"] != grant or row["state"] != "writing":
                raise ValueError("This export belongs to another connection or is already handed over.")
            path = self._path(row)
            if not os.path.isfile(path):
                raise ValueError("The export wrote no file.")
            size, exp = os.path.getsize(path), _now() + LINK_TTL
            con.execute("UPDATE files SET link = ?, state = 'ready', size = ?, exp = ? WHERE id = ?",
                        (token_hash(token), size, exp, fid))
        _event("link_minted", kind="download", grant=grant, ext=row["ext"], size=size)
        return {"download_url": f"{public_url(host)}/files/{token}", "file_name": row["name"], "size_bytes": size,
                "expires_at": _iso(exp), "expires_in_s": LINK_TTL, "single_use": True,
                "download_note": ("Give this link to the user: it opens a download page in their browser, works once "
                                  "and expires in 15 minutes. Export again for a new link.")}

    def peek(self, token: str, kind: str):
        """The live row of a link (no side effect), or None."""
        if not _TOKEN.match(token or ""):
            return None
        state = "ready" if kind == "download" else "open"
        with closing(self.store._con()) as con:
            row = self._row(con, link=token_hash(token))
        if row is None or not self._link(row, token) or row["kind"] != kind or row["state"] != state:
            return None
        if row["exp"] < _now():
            self.expire(row)
            return None
        return row

    def claim_download(self, token: str):
        """Spend a download link: its row (state sending, link gone) or None. Only one caller wins."""
        row = self.peek(token, "download")
        if row is None:
            return None
        with closing(self.store._con()) as con:
            n = con.execute("UPDATE files SET state = 'sending', link = NULL WHERE id = ? AND state = 'ready' "
                            "AND link = ?", (row["id"], row["link"])).rowcount
        return row if n == 1 else None

    def finish(self, row, what: str = "link_used") -> None:
        """The file was sent (or cannot be): file, folder and row go."""
        with closing(self.store._con()) as con:
            con.execute("DELETE FROM files WHERE id = ?", (row["id"],))
        self._remove(row)
        if what:
            _event(what, kind=row["kind"], grant=row["grant_id"], ext=row["ext"], size=row["size"])

    def expire(self, row) -> None:
        self.finish(row, "link_expired" if row["state"] in ("ready", "open") else "file_expired")

    # ------------------------------------------------------------- uploads
    def new_upload(self, grant: str, host: str) -> dict:
        uid, token = secrets.token_urlsafe(12), secrets.token_urlsafe(TOKEN_BYTES)
        exp = _now() + LINK_TTL
        self.sweep_soon()
        with closing(self.store._con()) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                self._room(con, grant)
                con.execute("INSERT INTO files (id, grant_id, kind, ext, link, state, created, exp) "
                            "VALUES (?, ?, 'upload', '.xer', ?, 'open', ?, ?)", (uid, grant, token_hash(token),
                                                                                 _now(), exp))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        _event("link_minted", kind="upload", grant=grant)
        return {"upload_url": f"{public_url(host)}/upload/{token}", "upload_id": uid, "expires_at": _iso(exp),
                "expires_in_s": LINK_TTL, "max_bytes": UPLOAD_MAX, "single_use": True,
                "next": ("Give upload_url to the user: they open it in their browser and send one .xer file (up to "
                         f"{UPLOAD_MAX // 1048576} MB) within 15 minutes. Once they say it is sent, call "
                         f"p6_inspect_xer with upload_id=\"{uid}\" (then p6_import_xer with the same upload_id, dry "
                         "run first). The file stays usable for an hour.")}

    def claim_upload(self, token: str):
        row = self.peek(token, "upload")
        if row is None:
            return None
        with closing(self.store._con()) as con:
            n = con.execute("UPDATE files SET state = 'receiving' WHERE id = ? AND state = 'open'",
                            (row["id"],)).rowcount
        return row if n == 1 else None

    def release_upload(self, row, reason: str) -> None:
        """A refused file: the page takes another one (same link, same expiry)."""
        shutil.rmtree(self._dir(row), ignore_errors=True)
        with closing(self.store._con()) as con:
            con.execute("UPDATE files SET state = 'open', name = '', size = NULL WHERE id = ? AND state = 'receiving'",
                        (row["id"],))
        _event("upload_refused", kind="upload", grant=row["grant_id"], reason=reason)

    def upload_dir(self, row) -> str:
        d = self._dir(row)
        os.makedirs(d, exist_ok=True)
        return d

    def complete_upload(self, row, name: str, size: int) -> bool:
        """The XER arrived and was checked: the link is spent, the upload_id usable. False when the row went in
        the meantime (expired, connection revoked)."""
        with closing(self.store._con()) as con:
            n = con.execute("UPDATE files SET state = 'received', link = NULL, name = ?, size = ?, exp = ? "
                            "WHERE id = ? AND state = 'receiving'", (name, size, _now() + UPLOAD_KEEP,
                                                                     row["id"])).rowcount
        if n:
            _event("upload_received", kind="upload", grant=row["grant_id"], size=size)
        return bool(n)

    def upload_path(self, grant: str, upload_id: Any) -> str:
        """The local path of an XER this connection uploaded (ValueError otherwise, the same for another
        connection's upload_id: nothing is said about it)."""
        uid = str(upload_id or "").strip()
        row = None
        if _ID.match(uid):
            with closing(self.store._con()) as con:
                row = self._row(con, id=uid, kind="upload")
        if row is None or row["grant_id"] != grant or row["state"] != "received" or row["exp"] < _now():
            raise ValueError(NOT_FOUND.format(id=uid[:40]))
        path = self._path(row)
        if not os.path.isfile(path):
            raise ValueError(NOT_FOUND.format(id=uid))
        return path

    # ------------------------------------------------------------- cleanup
    def sweep(self, everything: bool = False) -> int:
        """Expired rows (and every row when everything) go with their folders, then every folder no row names.
        Returns the rows removed."""
        with self._lock:
            self._swept = time.monotonic()
            now = _now()
            with closing(self.store._con()) as con:
                rows = con.execute("SELECT * FROM files").fetchall()
            old = [r for r in rows if everything or r["exp"] < now]
            live = {(_grant_dir(r["grant_id"]), r["id"]) for r in rows if not everything and r["exp"] >= now}
            for row in old:
                self.expire(row)
            try:
                grants = os.listdir(self.folder)
            except OSError:
                return len(old)
            for g in grants:
                gpath = os.path.join(self.folder, g)
                if not os.path.isdir(gpath) or not re.fullmatch(r"[0-9a-f]{32}", g):
                    continue
                for f in os.listdir(gpath):
                    if (g, f) not in live:
                        shutil.rmtree(os.path.join(gpath, f), ignore_errors=True)
                try:
                    os.rmdir(gpath)  # empty only
                except OSError:
                    pass
            return len(old)

    def sweep_soon(self, every: float = 10.0) -> None:
        if time.monotonic() - self._swept >= every:
            try:
                self.sweep()
            except Exception:  # cleanup never breaks a request
                log.exception("file handoff sweep failed")


def start_sweeper(handoff: Handoff, every: int = SWEEP_SECONDS) -> threading.Event:
    """Sweep every `every` seconds in a daemon thread (the remote access server process); set the event to stop."""
    stop = threading.Event()

    def run():
        while not stop.wait(every):
            handoff.sweep_soon(0)
    threading.Thread(target=run, name="p6mcp-files-sweep", daemon=True).start()
    return stop


# ----------------------------------------------------------------- remote tool wrappers

def remote_signature(name: str, gated: Callable):
    """The signature remote clients see: the exports take file_name (a name, no folder) instead of path and
    overwrite; p6_inspect_xer and p6_import_xer take upload_id instead of path."""
    import inspect
    sig = inspect.signature(gated, eval_str=True)
    P = inspect.Parameter
    out = []
    for p in sig.parameters.values():
        if p.name == "path" and name in policy.REMOTE_UPLOAD_TOOLS:
            out.append(P("upload_id", p.kind, annotation=str))
        elif p.name == "path":
            out.append(P("file_name", P.POSITIONAL_OR_KEYWORD, default=None, annotation=Optional[str]))
        elif p.name == "overwrite" and name in policy.REMOTE_DOWNLOAD_TOOLS:
            continue
        else:
            out.append(p)
    return sig.replace(parameters=out)


DOWNLOAD_DOC = ("\n\nRemote clients: the file is written in a private folder of this connection on the PC (never a "
                "path you choose); file_name only names the download (default: the Project ID). The result gives "
                "download_url, a one-time link (single use, 15 minutes) to give to the user, who opens it in their "
                "browser.")
UPLOAD_DOC = ("\n\nRemote clients: pass upload_id instead of a path: call p6_remote_upload_link, give the user its "
              "upload_url to send the XER from their browser, then use the upload_id it returned.")


def remote_doc(name: str, doc: Optional[str]) -> str:
    extra = UPLOAD_DOC if name in policy.REMOTE_UPLOAD_TOOLS else DOWNLOAD_DOC
    return (doc or "").rstrip() + extra


UPLOAD_LINK_DOC = """Make a one-time upload page for an XER file: the user opens upload_url in their browser and
sends one .xer (up to 50 MB) from their computer or phone to MCP for Primavera P6 on the PC (15 minutes, single
use). Then pass the returned upload_id to p6_inspect_xer and p6_import_xer instead of a path (the import still
needs a dry run and the user's confirmation). Nothing is imported by the upload itself."""


# ----------------------------------------------------------------- browser pages and routes

def _pages():
    from p6mcp.remote import pages
    return pages


def _headers(form: bool = False) -> dict:
    from p6mcp.remote.auth import PAGE_HEADERS
    return dict(PAGE_HEADERS, **{"content-security-policy": _pages().csp(form=form)})


def _page(title_key: str, body: str, status: int = 200, form: bool = False):
    from starlette.responses import HTMLResponse
    return HTMLResponse(_pages().page(i18n.language(), i18n.tr(title_key), body), status_code=status,
                        headers=_headers(form))


def _message(key: str, status: int, title_key: str = "remote.files.title_problem", **fmt):
    title = i18n.tr(title_key)
    return _page(title_key, _pages().message_body(title, i18n.tr(key, **fmt)), status)


def download_body(name: str, size: int, minutes: int) -> str:
    e, tr = html.escape, i18n.tr
    return (f"<h1>{e(tr('remote.files.download_heading'))}</h1>"
            f'<p class="lead">{e(tr("remote.files.download_lead", name=name, size=_size_text(size)))}</p>'
            '<form method="post"><button type="submit" class="btn btn-primary">'
            f"<span>{e(tr('remote.files.download_button'))}</span></button></form>"
            f'<p class="hint">{e(tr("remote.files.download_hint", minutes=minutes))}</p>')


def upload_body(minutes: int, problem: Optional[str] = None) -> str:
    e, tr = html.escape, i18n.tr
    warn = f'<p class="status warn" role="alert">{e(problem)}</p>' if problem else ""
    return (f"<h1>{e(tr('remote.files.upload_heading'))}</h1>"
            f'<p class="lead">{e(tr("remote.files.upload_lead"))}</p>{warn}'
            '<form method="post" enctype="multipart/form-data">'
            f'<label class="step" for="xer">{e(tr("remote.files.upload_pick", max=UPLOAD_MAX // 1048576))}</label>'
            '<input class="file" id="xer" name="file" type="file" accept=".xer" required>'
            '<button type="submit" class="btn btn-primary">'
            f"<span>{e(tr('remote.files.upload_button'))}</span></button></form>"
            f'<p class="hint">{e(tr("remote.files.upload_hint", minutes=minutes))}</p>')


def received_body(name: str, size: int) -> str:
    e, tr = html.escape, i18n.tr
    return (f"<h1>{e(tr('remote.files.received_heading'))}</h1>"
            f'<p class="status ok" role="status">{e(tr("remote.files.received", name=name, size=_size_text(size)))}'
            "</p>")


def _left(row) -> int:
    return max(1, (row["exp"] - _now() + 59) // 60)


class _UploadTooLarge(Exception):
    pass


async def _receive_xer(request, folder: str) -> tuple[Optional[str], Optional[str], int]:
    """Stream the multipart body: the first part with a file name is written to folder\\upload.part (at most
    UPLOAD_MAX bytes). Returns (part path, the browser's file name, bytes); (None, None, 0) without a file."""
    from python_multipart.multipart import MultipartParser, parse_options_header
    _, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if not boundary:
        return None, None, 0
    part_path = os.path.join(folder, "upload.part")
    st: dict[str, Any] = {"fh": None, "name": None, "size": 0, "field": b"", "value": b"", "headers": {}, "done": False,
                          "this": False}

    def on_part_begin():
        st.update(headers={}, this=False)

    def on_header_field(data, start, end):
        st["field"] += data[start:end]

    def on_header_value(data, start, end):
        st["value"] += data[start:end]

    def on_header_end():
        st["headers"][st["field"].decode("latin-1").lower()] = st["value"]
        st.update(field=b"", value=b"")

    def on_headers_finished():
        _, opts = parse_options_header(st["headers"].get("content-disposition", b""))
        fname = opts.get(b"filename")
        if fname is not None and opts.get(b"name") == b"file" and not st["done"]:
            st["name"] = fname.decode("utf-8", "replace")
            st["fh"] = open(part_path, "wb")
            st["this"] = True

    def on_part_data(data, start, end):
        if st["this"]:
            st["size"] += end - start
            if st["size"] > UPLOAD_MAX:
                raise _UploadTooLarge()
            st["fh"].write(data[start:end])

    def on_part_end():
        if st["this"]:
            st["fh"].close()
            st.update(this=False, done=True)

    parser = MultipartParser(boundary, {"on_part_begin": on_part_begin, "on_header_field": on_header_field,
                                        "on_header_value": on_header_value, "on_header_end": on_header_end,
                                        "on_headers_finished": on_headers_finished, "on_part_data": on_part_data,
                                        "on_part_end": on_part_end}, max_size=UPLOAD_BODY_CAP)
    try:
        async for chunk in request.stream():
            parser.write(chunk)
        parser.finalize()
    finally:
        if st["fh"] is not None and not st["fh"].closed:
            st["fh"].close()
    if not st["done"]:
        return None, None, 0
    return part_path, st["name"], st["size"]


def add_routes(srv: Any, handoff: Handoff, settings: Callable[[], config.Settings] = config.load) -> None:
    """The download and upload pages (custom routes: no bearer token, the link's token is the credential)."""
    import anyio
    from starlette.background import BackgroundTask
    from starlette.requests import Request
    from starlette.responses import FileResponse, Response

    def files_on() -> bool:
        try:
            return bool(settings().remote_allow_files)
        except Exception:
            return False

    async def gate():
        if not files_on():
            return _message("remote.files.off", 404)
        await anyio.to_thread.run_sync(handoff.sweep_soon)
        return None

    @srv.custom_route("/files/{token}", methods=["GET"])
    async def download_page(request: Request) -> Response:
        if (r := await gate()) is not None:
            return r
        row = await anyio.to_thread.run_sync(handoff.peek, request.path_params["token"], "download")
        if row is None:
            return _message("remote.files.gone", 404)
        return _page("remote.files.title_download", download_body(row["name"], row["size"] or 0, _left(row)),
                     form=True)

    @srv.custom_route("/files/{token}", methods=["POST"])
    async def download(request: Request) -> Response:
        if (r := await gate()) is not None:
            return r
        row = await anyio.to_thread.run_sync(handoff.claim_download, request.path_params["token"])
        if row is None:
            return _message("remote.files.gone", 404)
        path = handoff._path(row)
        if not os.path.isfile(path):
            await anyio.to_thread.run_sync(handoff.finish, row, "file_missing")
            return _message("remote.files.gone", 404)
        headers = {"cache-control": "no-store", "pragma": "no-cache", "x-content-type-options": "nosniff",
                   "content-security-policy": "default-src 'none'; sandbox", "referrer-policy": "no-referrer"}
        resp = FileResponse(path, headers=headers, media_type=CONTENT_TYPES.get(row["ext"], "application/octet-stream"),
                            filename=row["name"], background=BackgroundTask(handoff.finish, row))
        resp.headers["accept-ranges"] = "none"
        return resp

    @srv.custom_route("/upload/{token}", methods=["GET"])
    async def upload_page(request: Request) -> Response:
        if (r := await gate()) is not None:
            return r
        row = await anyio.to_thread.run_sync(handoff.peek, request.path_params["token"], "upload")
        if row is None:
            return _message("remote.files.gone_upload", 404)
        return _page("remote.files.title_upload", upload_body(_left(row)), form=True)

    @srv.custom_route("/upload/{token}", methods=["POST"])
    async def upload(request: Request) -> Response:
        if (r := await gate()) is not None:
            return r
        row = await anyio.to_thread.run_sync(handoff.claim_upload, request.path_params["token"])
        if row is None:
            return _message("remote.files.gone_upload", 404)

        def again(key: str, status: int, reason: str, **fmt):
            handoff.release_upload(row, reason)
            return _page("remote.files.title_upload", upload_body(_left(row), i18n.tr(key, **fmt)), status, form=True)

        mb = UPLOAD_MAX // 1048576
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > UPLOAD_BODY_CAP:
            return await anyio.to_thread.run_sync(lambda: again("remote.files.too_large", 413, "too_large", max=mb))
        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            return await anyio.to_thread.run_sync(lambda: again("remote.files.no_file", 400, "no_file"))
        folder = await anyio.to_thread.run_sync(handoff.upload_dir, row)
        try:
            part, fname, size = await _receive_xer(request, folder)
        except _UploadTooLarge:
            return await anyio.to_thread.run_sync(lambda: again("remote.files.too_large", 413, "too_large", max=mb))
        except Exception as e:  # a broken body or a client that went away
            log.warning("upload not received: %s", type(e).__name__)
            return await anyio.to_thread.run_sync(lambda: again("remote.files.failed", 400, "failed"))
        if part is None or size == 0:
            return await anyio.to_thread.run_sync(lambda: again("remote.files.no_file", 400, "no_file"))

        def check() -> Optional[str]:
            from p6mcp.core.xer import _xer_read_meta
            try:
                _xer_read_meta(part)
            except (ValueError, OSError):
                return "not_xer"
            return None
        if await anyio.to_thread.run_sync(check):
            return await anyio.to_thread.run_sync(lambda: again("remote.files.not_xer", 400, "not_xer"))
        name = safe_name(fname, ".xer", "upload.xer")
        await anyio.to_thread.run_sync(os.replace, part, os.path.join(folder, name))
        if not await anyio.to_thread.run_sync(handoff.complete_upload, row, name, size):
            await anyio.to_thread.run_sync(lambda: shutil.rmtree(folder, ignore_errors=True))
            return _message("remote.files.gone_upload", 404)
        return _page("remote.files.title_upload", received_body(name, size))


__all__ = ["Handoff", "LINK_TTL", "UPLOAD_KEEP", "UPLOAD_MAX", "UPLOAD_BODY_CAP", "UPLOAD_TOOL", "add_routes",
           "remote_doc", "remote_signature", "safe_name", "start_sweeper"]
