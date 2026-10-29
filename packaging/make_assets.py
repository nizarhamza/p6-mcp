"""Build assets: the application icon (assets/brand/ico/p6mcp.ico, and its 256 px PNG for the MCPB bundle)
and the Windows version resource of both executables. Output in packaging/build/.

    python packaging/make_assets.py
"""

from __future__ import annotations

import os
import re
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "build")


def version() -> tuple[str, tuple[int, int, int, int]]:
    text = open(os.path.join(ROOT, "p6mcp", "__init__.py"), encoding="utf-8").read()
    v = re.search(r'__version__ = "([^"]+)"', text)[1]
    nums = [int(x) for x in re.findall(r"\d+", v.split(".dev")[0].split("rc")[0])[:3]]
    return v, tuple((nums + [0, 0, 0])[:3]) + (0,)


def brand(*parts: str) -> str:
    return os.path.join(ROOT, "assets", "brand", *parts)


VERSION_TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Nizar Hamza'),
      StringStruct('FileDescription', '{desc}'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', '{name}'),
      StringStruct('LegalCopyright', 'Copyright 2026 Nizar Hamza. Apache License 2.0. Not affiliated with Oracle.'),
      StringStruct('OriginalFilename', '{name}.exe'),
      StringStruct('ProductName', 'MCP for Primavera P6'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    shutil.copyfile(brand("ico", "p6mcp.ico"), os.path.join(OUT, "p6mcp.ico"))
    shutil.copyfile(brand("png", "p6mcp-icon-256.png"), os.path.join(OUT, "icon.png"))
    v, t = version()
    for name, desc in (("p6mcp-server", "MCP for Primavera P6 server"),
                       ("p6mcp-settings", "MCP for Primavera P6 Settings")):
        with open(os.path.join(OUT, f"version_{name}.txt"), "w", encoding="utf-8") as fh:
            fh.write(VERSION_TEMPLATE.format(t=t, v=v, name=name, desc=desc))
    with open(os.path.join(OUT, "version.txt"), "w", encoding="utf-8") as fh:
        fh.write(v)
    print(f"assets for {v} in {OUT}")


if __name__ == "__main__":
    main()
