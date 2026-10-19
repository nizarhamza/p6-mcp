"""Build assets: the application icon (.ico with PNG images, and a 256 px PNG for the MCPB bundle)
and the Windows version resource of both executables. Output in packaging/build/.

    python packaging/make_assets.py
"""

from __future__ import annotations

import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "build")
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
sys.path.insert(0, ROOT)


def version() -> tuple[str, tuple[int, int, int, int]]:
    text = open(os.path.join(ROOT, "p6mcp", "__init__.py"), encoding="utf-8").read()
    v = re.search(r'__version__ = "([^"]+)"', text)[1]
    nums = [int(x) for x in re.findall(r"\d+", v.split(".dev")[0].split("rc")[0])[:3]]
    return v, tuple((nums + [0, 0, 0])[:3]) + (0,)


def pngs() -> dict[int, bytes]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QGuiApplication

    from p6mcp.settings_app.tray import badge
    _app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 (painting needs an application)
    out = {}
    for size in SIZES:
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        badge(size).save(buf, "PNG")
        out[size] = bytes(data)
    return out


def write_ico(images: dict[int, bytes], path: str) -> None:
    """ICO with PNG-compressed entries (supported since Windows Vista)."""
    entries, offset, blob = [], 6 + 16 * len(images), b""
    for size, png in sorted(images.items()):
        dim = 0 if size >= 256 else size
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(blob)))
        blob += png
    with open(path, "wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries) + blob)


VERSION_TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'P6 MCP project'),
      StringStruct('FileDescription', '{desc}'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', '{name}'),
      StringStruct('LegalCopyright', 'Apache License 2.0. Not affiliated with Oracle.'),
      StringStruct('OriginalFilename', '{name}.exe'),
      StringStruct('ProductName', 'P6 MCP for Primavera P6'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    images = pngs()
    write_ico(images, os.path.join(OUT, "p6mcp.ico"))
    with open(os.path.join(OUT, "icon.png"), "wb") as fh:
        fh.write(images[256])
    v, t = version()
    for name, desc in (("p6mcp-server", "P6 MCP server"), ("p6mcp-settings", "P6 MCP Settings")):
        with open(os.path.join(OUT, f"version_{name}.txt"), "w", encoding="utf-8") as fh:
            fh.write(VERSION_TEMPLATE.format(t=t, v=v, name=name, desc=desc))
    with open(os.path.join(OUT, "version.txt"), "w", encoding="utf-8") as fh:
        fh.write(v)
    print(f"assets for {v} in {OUT}")


if __name__ == "__main__":
    main()
