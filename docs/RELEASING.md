# Building and releasing P6 MCP

## Artifacts

| File | What it is |
|---|---|
| `P6MCP-Setup-<version>.exe` | Per-user installer (Inno Setup 6), no administrator rights. Installs to `%LOCALAPPDATA%\Programs\P6 MCP`, offers to register P6 MCP with every MCP client it detects (each one can be unticked), optional start with Windows. The uninstaller removes the `p6mcp` client entries and the startup entry; settings, backups and logs are kept. |
| `P6MCP-<version>-portable.zip` | The same two programs without installation. |
| `p6mcp-<version>.mcpb` | MCP bundle for Claude Desktop (server only, Windows). The database, the tool profile and the write permission are chosen in Claude's extension settings. |
| `SHA256SUMS.txt` | SHA-256 of every file above. |

`p6mcp-server.exe` is the MCP server (stdio, `--http`, and the `clients` / `setup` commands the
installer uses); `p6mcp-settings.exe` is the Settings window and notification area app.

## Build locally

Prerequisites: Python 3.12 or 3.13, Inno Setup 6, then:

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .[dev] pyinstaller==6.22.3
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

Output in `packaging\dist`. The version comes from `p6mcp/__init__.py`.

## Code signing

Unsigned builds work but Windows SmartScreen warns about them. Signing is in place behind a flag:

- **Certificate file (.pfx)**: `packaging\build.ps1 -Sign pfx` with `P6MCP_PFX_PATH` and
  `P6MCP_PFX_PASSWORD` set (optional `P6MCP_TIMESTAMP_URL`). The executables, the installer and the
  uninstaller are signed with `signtool` (`packaging\sign.ps1`), then verified.
- **Azure Artifact Signing** (formerly Trusted Signing), in the release workflow only: set the
  repository variable `P6MCP_SIGNING` to `azure`, the variables `AZURE_SIGNING_ENDPOINT`,
  `AZURE_SIGNING_ACCOUNT`, `AZURE_CERT_PROFILE`, and the secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
  `AZURE_SUBSCRIPTION_ID` (OpenID Connect; the app needs the "Artifact Signing Certificate Profile
  Signer" role). The executables and the installer are signed; the uninstaller is not (Inno Setup signs
  it only through its own SignTool hook).
- In the workflow, `P6MCP_SIGNING` = `pfx` uses the secrets `P6MCP_PFX_BASE64` and `P6MCP_PFX_PASSWORD`.

Status: no certificate yet. The signing steps have not run against a real certificate; check the first
signed build with `signtool verify /pa /v`.

## Release

1. Update the version in `p6mcp/__init__.py` and `CHANGELOG.md`, commit.
2. Tag and push: `git tag v3.0.0 && git push origin v3.0.0`.
3. The **Release** workflow checks that the tag matches the version, runs the tests, builds, signs
   (if configured), and creates a **draft** GitHub release with the artifacts and checksums.
4. Run the manual checklist below on the draft artifacts, then publish the release.

A manual run of the workflow (Actions, Release, Run workflow) builds without publishing and keeps the
artifacts for 90 days.

## Manual test checklist (before publishing)

On a clean Windows 10 or 11 user account, with no Python installed:

- [ ] The installer runs without administrator rights; SmartScreen shows the publisher (signed builds).
- [ ] The Finished page lists only the clients installed on the PC; unticking one leaves its config alone.
- [ ] The Settings window opens; with no database selected, a client's tool call answers
      "No database configured. Open P6 MCP Settings to select one."
- [ ] Database: the suggestions list P6's connections; Test connection shows the P6 version and the
      project count; Use this database, Save.
- [ ] **Claude Desktop**: restart it, list the projects, run a health check, run a write dry run, confirm it
      with the token, check the backup in the backup folder and the edit log.
- [ ] **VS Code or Cursor**: the p6mcp entry is present, the tools list, one read call works.
- [ ] **LM Studio with a small local model** (Basic profile): list projects and the critical path.
- [ ] Local HTTP: switch the transport, Save; the tray icon shows the server running; a client with the
      HTTP entry connects; a request without the token gets 401.
- [ ] XER-only mode: load two XER files, compare the two updates.
- [ ] Run at startup: sign out and in, the tray icon is there (minimized).
- [ ] Upgrade over the previous version while the tray app runs: setup asks to close it; settings kept.
- [ ] Uninstall: the p6mcp client entries and the startup entry are gone; settings, backups and logs stay.
