# PyInstaller spec: dist/P6MCP (p6mcp-server.exe + p6mcp-settings.exe sharing one folder) and
# dist/P6MCP-server (the server alone, without Qt, for the MCPB bundle).
#   pyinstaller packaging/p6mcp.spec --noconfirm --distpath packaging/dist --workpath packaging/build/pyi
# Run packaging/make_assets.py first (version resources). Icon and images: assets/brand.
import os

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ASSETS = os.path.join(SPECPATH, "build")
BRAND = os.path.join(ROOT, "assets", "brand")
ICON = os.path.join(BRAND, "ico", "p6mcp.ico")

# the core skills bundled for offline first starts (p6mcp.core_skills): both exes offer them as MCP prompts
datas = [(os.path.join(ROOT, "p6mcp", "locales", "*.json"), os.path.join("p6mcp", "locales")),
         (os.path.join(ROOT, "p6mcp", "data", "core_skills", "*.json"), os.path.join("p6mcp", "data", "core_skills"))]
for dist in ("mcp", "pydantic", "pydantic_core", "uvicorn", "starlette", "anyio", "keyring", "jsonschema",
             "sse-starlette", "httpx2", "p6mcp"):
    try:
        datas += copy_metadata(dist)
    except Exception:  # a distribution that is not installed in this environment
        pass
# the Settings app reads its window, tray and header images from assets/brand
gui_datas = datas + [(ICON, os.path.join("assets", "brand", "ico")),
                     (os.path.join(BRAND, "png", "p6mcp-icon-*.png"), os.path.join("assets", "brand", "png")),
                     (os.path.join(BRAND, "png", "p6mcp-lockup*.png"), os.path.join("assets", "brand", "png"))]
# openpyxl: imported on use by p6_export_dashboard (inside functions), collected explicitly
hidden = (collect_submodules("mcp") + collect_submodules("uvicorn") + collect_submodules("keyring.backends")
          + collect_submodules("openpyxl")
          + ["win32ctypes.core", "p6mcp.server", "p6mcp.http_server", "p6mcp.clients.__main__", "p6mcp.setup_cli"])
excl = ["tkinter", "matplotlib", "numpy", "pytest", "PyInstaller"]

srv = Analysis([os.path.join(ROOT, "p6mcp", "__main__.py")], pathex=[ROOT], datas=datas, hiddenimports=hidden,
               excludes=excl + ["PySide6", "shiboken6"], noarchive=False)
gui = Analysis([os.path.join(ROOT, "p6mcp", "settings_app", "__main__.py")], pathex=[ROOT], datas=gui_datas,
               hiddenimports=hidden + ["p6mcp.settings_app.window", "p6mcp.settings_app.tray"], excludes=excl,
               noarchive=False)

srv_exe = EXE(PYZ(srv.pure), srv.scripts, [], exclude_binaries=True, name="p6mcp-server", console=True,
              icon=ICON, version=os.path.join(ASSETS, "version_p6mcp-server.txt"), upx=False)
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="p6mcp-settings", console=False,
              icon=ICON, version=os.path.join(ASSETS, "version_p6mcp-settings.txt"), upx=False)

COLLECT(srv_exe, srv.binaries, srv.datas, gui_exe, gui.binaries, gui.datas, name="P6MCP", upx=False)
COLLECT(srv_exe, srv.binaries, srv.datas, name="P6MCP-server", upx=False)
