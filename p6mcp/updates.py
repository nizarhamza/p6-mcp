"""Updates: the only network calls P6 MCP makes. check() reads the latest published GitHub release;
download() fetches its installer and SHA256SUMS.txt and refuses a file whose hash does not match;
launch_install() runs that installer silently once the tray app has quit. No Qt here: the tray's
updater (settings_app/updater.py) drives these steps; the tests fake the network and the processes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from typing import Optional

from p6mcp import __version__

REPO = "nizarhamza/p6-mcp"
REPO_URL = f"https://github.com/{REPO}"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"{REPO_URL}/releases/download/"  # assets are only fetched from this repository's releases
TIMEOUT = 8
DOWNLOAD_TIMEOUT = 60
SUMS = "SHA256SUMS.txt"
SERVER_EXE = "p6mcp-server.exe"
INSTALL_ARGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/AUTOUPDATE=1")  # see packaging/installer.iss


def installer_name(version: str) -> str:
    return f"P6MCP-Setup-{version}.exe"


def parse(v: str) -> tuple:
    """'v1.2.3', '1.2.3', '1.2.3-rc.1', '1.2.3rc1', '1.2.3.dev1' -> sortable tuple (a pre-release sorts first)."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)(?:[.-]?(dev|a|alpha|b|beta|rc)\.?(\d+))?", v.strip())
    if not m:
        raise ValueError(f"not a version: {v!r}")
    rank = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "rc": 3}.get(m[4] or "", 4)
    return int(m[1]), int(m[2]), int(m[3]), rank, int(m[5] or 0)


def check(current: str = __version__, opener=urllib.request.urlopen) -> dict:
    """{current, latest, newer, url, assets {name: download url}, error}. Never raises."""
    out = {"current": current, "latest": None, "newer": False, "url": f"{REPO_URL}/releases", "assets": {},
           "error": None}
    try:
        req = urllib.request.Request(LATEST_API, headers={"Accept": "application/vnd.github+json",
                                                          "User-Agent": f"p6mcp/{current}"})
        with opener(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag: Optional[str] = data.get("tag_name")
        out["latest"] = tag.lstrip("v") if tag else None
        out["url"] = data.get("html_url") or out["url"]
        out["newer"] = bool(tag) and parse(tag) > parse(current)
        out["assets"] = {a["name"]: a["browser_download_url"] for a in data.get("assets") or []
                         if isinstance(a, dict) and a.get("name") and a.get("browser_download_url")}
    except Exception as e:  # offline, rate limited, no release yet (404)...
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ----------------------------------------------------------------- download and verify

def can_self_update(executable: Optional[str] = None) -> bool:
    """Only an installed copy updates itself (frozen build next to its uninstaller), not the portable
    zip nor a source checkout."""
    if executable is None:
        if not getattr(sys, "frozen", False):
            return False
        executable = sys.executable
    return sys.platform == "win32" and os.path.isfile(os.path.join(os.path.dirname(executable), "unins000.exe"))


def updates_dir() -> str:
    from p6mcp import config
    return os.path.join(config.localdata_dir(), "updates")


def _fetch(url: str, dest: str, opener) -> str:
    """Download url to dest (through dest.part) and return the SHA256 of the bytes."""
    if not url.startswith(DOWNLOAD_PREFIX):
        raise ValueError(f"refusing to download from {url!r}: not a release of {REPO}")
    h, tmp = hashlib.sha256(), dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": f"p6mcp/{__version__}"})
    try:
        with opener(req, timeout=DOWNLOAD_TIMEOUT) as resp, open(tmp, "wb") as fh:
            for chunk in iter(lambda: resp.read(1 << 16), b""):
                fh.write(chunk)
                h.update(chunk)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return h.hexdigest()


def parse_sums(text: str) -> dict[str, str]:
    """'<sha256>  <name>' lines (sha256sum format, '*' binary marker allowed) -> {name: sha256}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if m:
            out[m[2]] = m[1].lower()
    return out


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def download(info: dict, folder: Optional[str] = None, opener=urllib.request.urlopen) -> str:
    """Installer of the release in info (a check() result), verified against the release's
    SHA256SUMS.txt; returns its path. A file already there with the right hash is reused; a
    mismatch deletes the file and raises."""
    version = info.get("latest")
    if not version or not info.get("newer"):
        raise ValueError("no newer release to download")
    name = installer_name(version)
    assets = info.get("assets") or {}
    if name not in assets or SUMS not in assets:
        raise ValueError(f"release {version} has no {name} or {SUMS}")
    folder = folder or updates_dir()
    os.makedirs(folder, exist_ok=True)
    for old in os.listdir(folder):  # earlier downloads
        if old != name and old.lower().endswith((".exe", ".part")):
            try:
                os.remove(os.path.join(folder, old))
            except OSError:
                pass
    sums_path = os.path.join(folder, SUMS)
    _fetch(assets[SUMS], sums_path, opener)
    with open(sums_path, encoding="utf-8", errors="replace") as fh:
        want = parse_sums(fh.read()).get(name)
    if not want:
        raise ValueError(f"{SUMS} of release {version} has no line for {name}")
    dest = os.path.join(folder, name)
    if os.path.isfile(dest) and sha256_file(dest) == want:
        return dest
    got = _fetch(assets[name], dest, opener)
    if got != want:
        os.remove(dest)
        raise ValueError(f"{name}: SHA256 {got} does not match {SUMS} ({want}); the file was deleted")
    return dest


# ----------------------------------------------------------------- servers started by MCP clients

def processes() -> list[tuple[int, int, str]]:
    """(pid, parent pid, exe name) of every process (Windows; empty elsewhere)."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PE32))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    out, e = [], PE32()
    e.dwSize = ctypes.sizeof(PE32)
    try:
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), e.szExeFile))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def client_servers(procs: Optional[list] = None, me: Optional[int] = None) -> list[dict]:
    """P6 MCP servers started by MCP clients: every p6mcp-server.exe that is not a descendant of this
    process (the tray's own HTTP server is), with the program that started it."""
    procs = processes() if procs is None else procs
    me = os.getpid() if me is None else me
    parent = {p: pp for p, pp, _ in procs}
    names = {p: n for p, _, n in procs}

    def mine(pid: int) -> bool:
        seen = set()
        while pid and pid not in seen:
            if pid == me:
                return True
            seen.add(pid)
            pid = parent.get(pid, 0)
        return False

    return [{"pid": p, "client": names.get(pp) or "?"} for p, pp, n in procs
            if n.lower() == SERVER_EXE and not mine(p)]


def stop_servers(pids: list[int]) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=flags,
                       timeout=15)


# ----------------------------------------------------------------- install

def install_command(installer: str, log: str) -> str:
    """cmd line that waits about 3 s (the tray app quits and releases the installer's AppMutex), then
    runs the installer silently; /AUTOUPDATE=1 keeps the client registrations as they are and starts
    the tray app again."""
    return f'cmd.exe /d /s /c "ping -n 4 127.0.0.1 >nul & "{installer}" {" ".join(INSTALL_ARGS)} /LOG="{log}""'


def launch_install(installer: str, popen=subprocess.Popen) -> str:
    """Start the delayed silent install, detached from this process; returns the setup log path.
    The caller quits right after."""
    if not os.path.isfile(installer):
        raise FileNotFoundError(installer)
    log = os.path.join(os.path.dirname(installer), "setup.log")
    flags = 0
    if sys.platform == "win32":
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    popen(install_command(installer, log), creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return log
