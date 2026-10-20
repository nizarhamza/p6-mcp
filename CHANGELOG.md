# Changelog

All notable changes to P6 MCP. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and versions follow [Semantic Versioning](https://semver.org/).

## [1.0.1] - 2026-10-20

Bug fixes from a full review of the server.

### Fixed

- XER export: the path must end with `.xer` in an existing folder, so the database, a backup or a log can
  never be overwritten; the file is written through a temporary file; rows with characters outside
  cp1252 are reported in `warnings` instead of being replaced silently; floats are never written as
  `7.`; dates with a `T` and UTF-8 text blobs are exported correctly.
- XER import of a file with several projects: activity codes of another project's code types are no
  longer imported without a type (the pre-flight lists them); a relationship repeated in the file is
  imported once; a soft-deleted row matched by the import comes back.
- New ids: NEXTKEY is matched on its exact key only (a loose match could lower another table's counter).
- `p6_create_eps` refuses a name already used by a project; `p6_delete_project` marks the projects on
  the other side of deleted cross-project links as needing F9; `p6_delete_activity` checks actual
  dates again under the write lock; `p6_delete_relationships` accepts one link named by id and by pair.
- Health check: the critical path test only delays an activity whose duration can move its finish;
  key_dates percentage counts constraints with an issue; `p6_health_check_compare(after="current")`
  compares against the snapshot it just saved.
- CPM engine: elapsed lag dropped on links to other projects too; constraints without a readable calendar
  keep their duration; faster on large projects.
- `p6_progress_check` leaves LOE and WBS summary activities out of the out-of-sequence links;
  `p6_compare_projects` counts a duplicated link once; `p6_float_origin` says `none` when no source is
  found within `max_depth`.
- Read connections are always closed when a tool returns (the database file is no longer kept open);
  several reports now read their settings and data from one snapshot; a failed backup leaves no partial
  file; a restore no longer marks as undone a change made just before the backup.

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
