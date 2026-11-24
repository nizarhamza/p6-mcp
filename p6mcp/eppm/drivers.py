"""SQL drivers installed on demand (decision D3 of docs/EPPM_PLAN.md).

The drivers are not part of every install: when the user sets up a SQL connection, the Settings app asks, then
`install(driver)` downloads the wheels from PyPI (the JSON API, https only), checks each file against the SHA-256
that PyPI publishes, and unpacks them into %LOCALAPPDATA%\\P6MCP\\drivers\\<python tag> (one folder per Python
version, since the drivers hold compiled modules). `activate()` puts that folder on sys.path; it works in a
checkout and in the frozen exe alike. Nothing here runs without a call from the user's action; tests pass their
own opener and never reach the network.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from typing import Callable, Optional

from p6mcp import config

PYPI = "https://pypi.org/pypi/{name}/json"
# driver -> the distributions to install, dependencies after the driver (oracledb thin mode needs cryptography)
PACKAGES = {"mssql": ("pymssql",), "oracle": ("oracledb", "cryptography", "cffi", "pycparser")}
MAX_WHEEL = 60 * 1024 * 1024


class DriverError(RuntimeError):
    """A driver could not be installed; the message is meant for the user."""


def py_tag() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def folder() -> str:
    return os.path.join(config.localdata_dir(), "drivers", py_tag())


def activate() -> None:
    """Make the installed drivers importable (idempotent)."""
    path = folder()
    if os.path.isdir(path) and path not in sys.path:
        sys.path.append(path)


def _pick(files: list[dict]) -> Optional[dict]:
    """The wheel for this Python on 64-bit Windows: exact CPython tag, then abi3, then pure Python."""
    tag = py_tag()
    wheels = [f for f in files if f.get("packagetype") == "bdist_wheel" and f.get("filename", "").endswith(".whl")]

    def abi3(n: str) -> bool:  # name-version-cp3N-abi3-win_amd64.whl, built for 3.N or older
        parts = n[:-4].split("-")
        return (len(parts) >= 5 and parts[-2] == "abi3" and parts[-1] == "win_amd64" and parts[-3].startswith("cp3")
                and parts[-3][3:].isdigit() and int(parts[-3][3:]) <= sys.version_info.minor)

    for test in (lambda n: n.endswith(f"-{tag}-{tag}-win_amd64.whl"), abi3, lambda n: n.endswith("-py3-none-any.whl")):
        hit = [f for f in wheels if test(f["filename"])]
        if hit:
            return hit[0]
    return None


def _get(opener, url: str, limit: int) -> bytes:
    if not url.startswith("https://"):
        raise DriverError(f"Refused a download that is not https: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "p6mcp-driver-install"})
    try:
        with opener.open(req, timeout=120) as r:
            data = r.read(limit + 1)
    except OSError as e:
        raise DriverError(f"Download failed ({url}): {e}") from e
    if len(data) > limit:
        raise DriverError(f"Download too large: {url}")
    return data


def install(driver: str, opener: Optional[urllib.request.OpenerDirector] = None,
            progress: Optional[Callable[[str], None]] = None) -> dict:
    """Download, check and unpack the driver's wheels. Returns {driver, folder, wheels: [{name, version, sha256}]}.
    The folder is replaced only once every wheel is checked and unpacked (a failed install leaves the old one)."""
    names = PACKAGES.get(driver)
    if not names:
        raise DriverError(f"Unknown SQL driver {driver!r}")
    opener = opener or urllib.request.build_opener()
    dest = folder()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    work = tempfile.mkdtemp(prefix="drivers.", dir=os.path.dirname(dest))
    done = []
    try:
        if os.path.isdir(dest):  # keep the other driver already installed
            shutil.copytree(dest, work, dirs_exist_ok=True)
        for name in names:
            meta = json.loads(_get(opener, PYPI.format(name=name), 5 * 1024 * 1024).decode("utf-8"))
            version = meta["info"]["version"]
            wheel = _pick(meta.get("urls") or [])
            if wheel is None:
                raise DriverError(f"{name} {version} has no wheel for this Python ({py_tag()}, 64-bit Windows).")
            if progress:
                progress(f"{name} {version}")
            data = _get(opener, wheel["url"], MAX_WHEEL)
            want = (wheel.get("digests") or {}).get("sha256", "")
            got = hashlib.sha256(data).hexdigest()
            if not want or got != want:
                raise DriverError(f"{wheel['filename']}: checksum mismatch, nothing installed.")
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for m in z.namelist():
                    if m.startswith(("/", "\\")) or ".." in m.replace("\\", "/").split("/"):
                        raise DriverError(f"{wheel['filename']}: unsafe path {m!r}, nothing installed.")
                z.extractall(work)
            done.append({"name": name, "version": version, "sha256": got, "file": wheel["filename"]})
        old = dest + ".old"
        if os.path.isdir(dest):
            shutil.rmtree(old, ignore_errors=True)
            os.replace(dest, old)
        os.replace(work, dest)
        shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    activate()
    for name in list(sys.modules):  # a failed import attempt may have cached a missing module
        if name in ("pymssql", "oracledb"):
            sys.modules.pop(name, None)
    return {"driver": driver, "folder": dest, "wheels": done}
