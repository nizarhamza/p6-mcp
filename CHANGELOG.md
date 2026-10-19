# Changelog

All notable changes to P6 MCP. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and versions follow [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-10-19

First public release.

### Added

- MCP server for Primavera P6 Professional standalone (SQLite) databases: 46 tools for projects, WBS,
  activities, relationships, calendars, critical path, driving path, float paths and float origin,
  look-ahead, progress and S-curves, resources, grouping, DCMA 14-point health check and its comparison
  over time, update comparison, dangling and redundant logic, a CPM engine that reproduces P6's F9 for
  validation and what-if scenarios, XER inspection, import and export, backups and edit log.
- Tool policy enforced by the server: Basic (read-only, 11 tools) and Full profiles; write tools need
  write permission, a known or trusted P6 schema, a dry run and its single-use confirmation token;
  destructive tools also need a confirmation text. Backup before every write, refused while P6 runs.
- XER-only mode: read-only analysis of one or more XER files without a P6 database or P6 installed,
  including the comparison of two updates.
- Settings window and notification area app (English and French): database selection with a read-only
  connection test, startup, server, MCP clients, tools and safety, logs and updates, about.
- Automatic registration with Claude Desktop, Cursor, VS Code, VS Code Insiders, LM Studio and Cline,
  editing only the p6mcp entry of their configuration files (comments and formatting kept, backup first),
  plus configuration snippets for other clients.
- Local Streamable HTTP transport on 127.0.0.1 with a bearer token kept in the Windows Credential
  Manager, Host and Origin checks, supervised by the notification area app.
- Per-user Windows installer, portable zip and MCP bundle (.mcpb) for Claude Desktop; GitHub Actions
  for tests and releases, with optional code signing.

[1.0.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.0.0
